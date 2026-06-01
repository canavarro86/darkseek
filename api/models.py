import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/db/darkseek.db")

# Ensure DB directory exists
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

# Per-connection PRAGMAs. WAL lets a writer (crawler) and readers (API) work
# concurrently; cache_size is negative => KiB, so -32000 == 32 MiB; temp tables
# and sort/group buffers live in RAM instead of touching the SSD.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-32000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=5000",
)


@dataclass
class Page:
    id: int
    url: str
    title: Optional[str]
    description: Optional[str]
    category: Optional[str]
    lang: Optional[str]
    indexed_at: str
    last_seen: str
    score: float
    is_alive: int


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Single source of truth for SQLite connections.

    Applies the production PRAGMAs on every connection and always closes it,
    even on error. Both the API and the crawler import this helper so the
    pragma setup never drifts between the two processes.
    """
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        yield conn
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def ensure_content_hash_unique_index(conn: sqlite3.Connection) -> bool:
    """Create the partial UNIQUE index on content_hash if the data allows it.

    The index is the DB-level guarantee behind content dedup. It cannot exist
    while duplicate content_hash values are present, so this is best-effort:
    returns True if the index exists afterwards, False if duplicates still block
    it (operator must run scripts/dedupe.py first). NULL hashes are excluded so
    user-submitted / hash-less rows never collide. Idempotent.
    """
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pages_content_hash_unique "
            "ON pages(content_hash) WHERE content_hash IS NOT NULL"
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Duplicates present — expected on the legacy DB until dedupe runs.
        logger.warning(
            "content_hash UNIQUE index not created: duplicates present. "
            "Run scripts/dedupe.py to collapse them, then re-run migrate()."
        )
        return False


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent, non-destructive schema migrations.

    Only ADD COLUMN / CREATE IF NOT EXISTS — safe to run on every startup and
    safe for zero-downtime deploys against an already-populated database.
    """
    # New columns on legacy databases that predate freshness ranking.
    if not _column_exists(conn, "pages", "content_hash"):
        conn.execute("ALTER TABLE pages ADD COLUMN content_hash TEXT")
    if not _column_exists(conn, "pages", "page_type"):
        conn.execute("ALTER TABLE pages ADD COLUMN page_type TEXT DEFAULT 'other'")
    # Consecutive fetch-failure counter for dead-site retry logic.
    if not _column_exists(conn, "pages", "fail_count"):
        conn.execute("ALTER TABLE pages ADD COLUMN fail_count INTEGER DEFAULT 0")
    # Why a row holds its current category/lang (ai|heuristic|pending).
    if not _column_exists(conn, "pages", "enrichment_method"):
        conn.execute(
            "ALTER TABLE pages ADD COLUMN enrichment_method TEXT DEFAULT 'pending'"
        )
        # Best-effort backfill of historical state, in one set-based UPDATE
        # (no row loads): fully-populated legacy rows were AI-enriched before the
        # outage; rows missing category/lang stay 'pending' for the backfill job.
        conn.execute(
            "UPDATE pages SET enrichment_method = 'ai' "
            "WHERE enrichment_method = 'pending' "
            "AND category IS NOT NULL AND lang IS NOT NULL"
        )

    # Single-row table holding the latest crawler cycle metrics for /metrics.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawler_stats (
            id               INTEGER PRIMARY KEY CHECK (id = 1),
            last_run         TIMESTAMP,
            pages_last_cycle INTEGER DEFAULT 0,
            pages_per_hour   REAL DEFAULT 0.0,
            cycle_seconds    REAL DEFAULT 0.0
        )
        """
    )

    # Negative cache for dead .onion services (see crawler/dead_cache.py).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dead_onions (
            url             TEXT PRIMARY KEY,
            first_failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_failed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fail_count      INTEGER DEFAULT 1
        )
        """
    )

    # User-submitted crawl queue (see db/migrations/002_crawl_queue.sql). The API
    # writes 'pending' rows from /api/submit/bulk; the crawler claims them at the
    # top of each cycle. Kept here too so a fresh DB converges without a separate
    # migration runner.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT UNIQUE NOT NULL,
            added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            priority   INTEGER DEFAULT 0,
            status     TEXT DEFAULT 'pending'
                       CHECK(status IN ('pending','processing','done','failed')),
            source     TEXT DEFAULT 'user'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_status "
        "ON crawl_queue(status, priority DESC, added_at)"
    )

    # Indexes for the hot search/order-by paths. Idempotent.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_last_seen ON pages(last_seen DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_category ON pages(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_is_alive ON pages(is_alive)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_enrichment_method ON pages(enrichment_method)"
    )
    conn.commit()

    # Best-effort: becomes a no-op once dedupe.py has collapsed duplicates.
    ensure_content_hash_unique_index(conn)


def init_db() -> None:
    """Create the schema if missing, then apply migrations."""
    schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
    with get_db() as conn:
        if os.path.exists(schema_path):
            with open(schema_path) as f:
                conn.executescript(f.read())
        migrate(conn)
        conn.commit()


def record_crawl_stats(
    pages_last_cycle: int,
    pages_per_hour: float,
    cycle_seconds: float,
) -> None:
    """Persist the metrics from the most recent crawl cycle."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO crawler_stats
                (id, last_run, pages_last_cycle, pages_per_hour, cycle_seconds)
            VALUES (1, CURRENT_TIMESTAMP, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_run         = CURRENT_TIMESTAMP,
                pages_last_cycle = excluded.pages_last_cycle,
                pages_per_hour   = excluded.pages_per_hour,
                cycle_seconds    = excluded.cycle_seconds
            """,
            (pages_last_cycle, pages_per_hour, cycle_seconds),
        )
        conn.commit()


def get_crawl_stats() -> dict:
    """Return the latest crawler metrics, or zeros if no cycle has run yet."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_run, pages_last_cycle, pages_per_hour, cycle_seconds "
            "FROM crawler_stats WHERE id = 1"
        ).fetchone()
    if row is None:
        return {
            "last_run": None,
            "pages_last_cycle": 0,
            "pages_per_hour": 0.0,
            "cycle_seconds": 0.0,
        }
    return dict(row)


def db_size_mb() -> float:
    """Total on-disk size of the database, including WAL/SHM sidecars."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = DATABASE_PATH + suffix
        if os.path.exists(path):
            total += os.path.getsize(path)
    return round(total / (1024 * 1024), 2)


# Run migrations at import so both the API and crawler converge on the same
# schema regardless of which process starts first.
init_db()
