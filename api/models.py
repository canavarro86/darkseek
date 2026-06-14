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

    # v2.0 community-trust columns. Parallel the existing is_alive/last_seen/score
    # (kept in lockstep by the crawler). Guarded so this is a no-op on a fresh DB
    # where schema.sql already defined them, and a one-time add on legacy DBs.
    for _name, _decl in (
        ("fresh_votes",     "INTEGER DEFAULT 0"),
        ("rotten_votes",    "INTEGER DEFAULT 0"),
        ("onion_score",     "REAL DEFAULT NULL"),
        ("last_scanned_at", "TIMESTAMP"),
        ("is_active",       "BOOLEAN DEFAULT 1"),
        ("content_tag",     "TEXT DEFAULT 'unknown'"),
    ):
        if not _column_exists(conn, "pages", _name):
            conn.execute(f"ALTER TABLE pages ADD COLUMN {_name} {_decl}")

    # Community voting / reporting + cross-worker PoW challenge store. Mirrored
    # from db/schema.sql so a legacy DB converges without re-running schema.sql.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id    INTEGER NOT NULL,
            pow_hash   TEXT UNIQUE NOT NULL,
            vote_type  TEXT CHECK(vote_type IN ('fresh','rotten')) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (page_id) REFERENCES pages(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id    INTEGER NOT NULL,
            reason     TEXT CHECK(reason IN ('scam','offline','illegal','spam')) NOT NULL,
            pow_hash   TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pow_challenges (
            challenge  TEXT PRIMARY KEY,
            page_id    INTEGER NOT NULL,
            expires_at TIMESTAMP NOT NULL
        )
        """
    )

    # Search-query log for index-quality analysis (popular / zero-result terms).
    # Privacy: no IP, no user identifier — only the query text + result count.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_queries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            query       TEXT NOT NULL,
            hits        INTEGER DEFAULT 0,
            safe_mode   BOOLEAN DEFAULT 1,
            category    TEXT DEFAULT NULL,
            searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sq_searched_at "
        "ON search_queries(searched_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sq_query ON search_queries(query)")

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_is_active ON pages(is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_content_tag ON pages(content_tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_onion_score ON pages(onion_score)")
    conn.commit()

    # Best-effort: becomes a no-op once dedupe.py has collapsed duplicates.
    ensure_content_hash_unique_index(conn)


# --- Numbered, transactional migrations (FIX 1) -----------------------------
# The legacy migrate() above is idempotent ADD-COLUMN / CREATE-IF-NOT-EXISTS and
# stays as the baseline. The v3 changes layer on top as numbered migrations: each
# runs exactly once, inside its own transaction, and is recorded in
# schema_migrations. Any error rolls the WHOLE migration back (DDL included) and
# aborts startup, so a half-applied schema can never reach production.
#
# To add a migration: append a @_migration(N, "desc") function. Keep each body
# guarded (CREATE ... IF NOT EXISTS, _column_exists before ALTER) so re-running
# against a fresh DB where schema.sql already created the objects is a no-op.

_MIGRATIONS: list = []  # (version:int, description:str, fn)


def _migration(version: int, description: str):
    def _register(fn):
        _MIGRATIONS.append((version, description, fn))
        return fn
    return _register


@_migration(1, "v3 self-feeding crawler columns on pages")
def _migrate_001(conn: sqlite3.Connection) -> None:
    # Constant-default columns are safe to ALTER ADD directly.
    if not _column_exists(conn, "pages", "crawl_priority"):
        conn.execute("ALTER TABLE pages ADD COLUMN crawl_priority INTEGER DEFAULT 5")
    if not _column_exists(conn, "pages", "crawl_attempts"):
        conn.execute("ALTER TABLE pages ADD COLUMN crawl_attempts INTEGER DEFAULT 0")
    # SQLite forbids ALTER ADD COLUMN with a CURRENT_TIMESTAMP default, so add it
    # nullable. We deliberately do NOT backfill it: the crawler treats a NULL
    # next_crawl_at as "eligible now" everywhere it reads it, so existing rows are
    # immediately crawlable. (A backfill UPDATE would also fire the pages_fts
    # update trigger across the whole table for no functional gain.)
    if not _column_exists(conn, "pages", "next_crawl_at"):
        conn.execute("ALTER TABLE pages ADD COLUMN next_crawl_at TIMESTAMP")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_next_crawl ON pages(next_crawl_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_crawl_priority ON pages(crawl_priority)"
    )


@_migration(2, "rate_limits table (per-endpoint throttling)")
def _migrate_002(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            key          TEXT NOT NULL,
            window_start TIMESTAMP NOT NULL,
            count        INTEGER DEFAULT 1,
            PRIMARY KEY (key, window_start)
        )
        """
    )


@_migration(3, "url_cache table (persistent shortener cache)")
def _migrate_003(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS url_cache (
            original_url TEXT PRIMARY KEY,
            short_url    TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


@_migration(4, "daily_visitors table (unique visitor tracking)")
def _migrate_004(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_visitors (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            date     TEXT NOT NULL,
            ip_hash  TEXT NOT NULL,
            UNIQUE(date, ip_hash)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_visitors_date ON daily_visitors(date)"
    )


@_migration(5, "admin panel tables + superadmin seed")
def _migrate_005(conn: sqlite3.Connection) -> None:
    # All CREATE TABLE statements run inside the migration's single transaction
    # (the runner has already issued BEGIN). bcrypt is imported lazily here so
    # the crawler image — which never applies this migration (the API applies it
    # first, the crawler then skips an already-recorded version) — does not need
    # bcrypt as a dependency.
    import secrets

    import bcrypt

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'admin' CHECK(role IN ('superadmin','admin')),
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login    TIMESTAMP,
            is_active     INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            id         TEXT PRIMARY KEY,
            admin_id   INTEGER REFERENCES admins(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            ip_hash    TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS removal_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT NOT NULL,
            reason      TEXT CHECK(reason IN ('dmca','illegal','other')),
            description TEXT,
            status      TEXT DEFAULT 'pending'
                        CHECK(status IN ('pending','reviewed','removed','rejected')),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            reviewed_by INTEGER REFERENCES admins(id),
            notes       TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id   INTEGER REFERENCES admins(id),
            action     TEXT NOT NULL,
            target     TEXT,
            details    TEXT,
            ip_hash    TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT NOT NULL,
            message    TEXT NOT NULL,
            is_read    INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Seed the initial superadmin exactly once. The generated password is printed
    # to the container log a single time on first run — it is never stored in
    # plaintext, only as a bcrypt hash — so the operator must capture it then.
    existing = conn.execute("SELECT id FROM admins WHERE username='admin'").fetchone()
    if not existing:
        password = secrets.token_urlsafe(16)
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        conn.execute(
            "INSERT INTO admins (username, password_hash, role) VALUES (?, ?, ?)",
            ('admin', password_hash, 'superadmin')
        )
        print(f"\n{'='*50}", flush=True)
        print(f"[DARKSEEK ADMIN PASSWORD] username: admin", flush=True)
        print(f"[DARKSEEK ADMIN PASSWORD] password: {password}", flush=True)
        print(f"[DARKSEEK ADMIN PASSWORD] shown only once - save immediately!", flush=True)
        print(f"{'='*50}\n", flush=True)


def run_numbered_migrations(conn: sqlite3.Connection) -> None:
    """Apply any not-yet-applied numbered migrations, each in its own transaction.

    Switches the connection to manual transaction control so DDL (CREATE/ALTER)
    is included in the rollback envelope — SQLite supports transactional DDL, but
    Python's sqlite3 only auto-opens transactions for DML. On any failure the
    migration is rolled back whole and the exception re-raised to abort startup.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT
        )
        """
    )
    conn.commit()
    applied = {
        r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }

    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # manual BEGIN/COMMIT/ROLLBACK around DDL+DML
    try:
        for version, description, fn in sorted(_MIGRATIONS):
            if version in applied:
                continue
            try:
                conn.execute("BEGIN")
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                    (version, description),
                )
                conn.execute("COMMIT")
                logger.info("migration %d applied: %s", version, description)
            except Exception:
                conn.execute("ROLLBACK")
                logger.exception(
                    "migration %d failed (%s); rolled back, aborting startup",
                    version, description,
                )
                raise
    finally:
        conn.isolation_level = prev_isolation


def init_db() -> None:
    """Create the schema if missing, then apply migrations."""
    schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
    with get_db() as conn:
        if os.path.exists(schema_path):
            with open(schema_path) as f:
                conn.executescript(f.read())
        migrate(conn)
        conn.commit()
        # v3 numbered migrations run after the legacy idempotent pass.
        run_numbered_migrations(conn)


def get_visitor_stats() -> dict:
    """Distinct-visitor counts for today and yesterday (PART C).

    Distinct visitors == number of daily_visitors rows for that date (the
    UNIQUE(date, ip_hash) constraint already collapses repeats). Privacy: this
    reads only opaque daily-salted hashes; no raw IP is stored or returned.
    """
    with get_db() as conn:
        today = conn.execute(
            "SELECT COUNT(*) FROM daily_visitors WHERE date = date('now')"
        ).fetchone()[0]
        yesterday = conn.execute(
            "SELECT COUNT(*) FROM daily_visitors WHERE date = date('now', '-1 day')"
        ).fetchone()[0]
    return {"visitors_today": today, "visitors_yesterday": yesterday}


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


def log_search_query(
    query: str, hits: int, safe_mode: bool, category: str | None
) -> None:
    """Record one search for index-quality analysis. Never raises.

    Wrapped end-to-end in try/except so a logging failure can never break or
    block the search response. Privacy: query stored for index quality analysis
    only — no IP, no user data.
    """
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO search_queries (query, hits, safe_mode, category) "
                "VALUES (?, ?, ?, ?)",
                (query, hits, 1 if safe_mode else 0, category),
            )
            conn.commit()
    except Exception:
        # Best-effort analytics only — swallow everything, never surface to caller.
        logger.debug("log_search_query failed", exc_info=True)


def get_search_stats() -> dict:
    """Aggregate search-log stats for the public /api/search-stats endpoint.

    Returns all-time and 24h counts plus the top and zero-result queries over
    the last 7 days. Read-only; the table is guaranteed to exist by migrate().
    """
    with get_db() as conn:
        total_searches = conn.execute(
            "SELECT COUNT(*) FROM search_queries"
        ).fetchone()[0]
        searches_today = conn.execute(
            "SELECT COUNT(*) FROM search_queries "
            "WHERE searched_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        top_rows = conn.execute(
            "SELECT query, COUNT(*) AS count, AVG(hits) AS avg_hits "
            "FROM search_queries "
            "WHERE searched_at >= datetime('now', '-7 days') "
            "GROUP BY query ORDER BY count DESC LIMIT 10"
        ).fetchall()
        zero_rows = conn.execute(
            "SELECT query, COUNT(*) AS count FROM search_queries "
            "WHERE hits = 0 AND searched_at >= datetime('now', '-7 days') "
            "GROUP BY query ORDER BY count DESC LIMIT 10"
        ).fetchall()
    return {
        "total_searches": total_searches,
        "searches_today": searches_today,
        "top_queries": [
            {
                "query": r["query"],
                "count": r["count"],
                "avg_hits": round(r["avg_hits"] or 0.0, 1),
            }
            for r in top_rows
        ],
        "zero_result_queries": [
            {"query": r["query"], "count": r["count"]} for r in zero_rows
        ],
    }


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
