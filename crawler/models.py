import logging
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, List
from urllib.parse import urlparse

# Single, unified DB layer (WAL + pragmas + migrations) lives in api.models.
# The crawler image bundles api/, so this import works in both processes.
from api.models import _column_exists, get_db

logger = logging.getLogger(__name__)

RECRAWL_DAYS_DEFAULT = 7
RECRAWL_DAYS_FORUM = 1
FORUM_PATTERNS = ["forum", "board", "thread", "topic", "chan"]

# How long a dead (is_alive=0) page waits before revive_check() gives it another
# crawl attempt. Raised from 7 to 30 days for v1.4: dead sites rarely return, so
# re-checking them weekly wastes slow Tor circuits that fresh/live content needs.
# A 30-day cadence still resurrects a genuinely-returned site within a month.
# This bound applies only to is_alive=0 rows; live-page recrawl cadence
# (RECRAWL_DAYS_DEFAULT / RECRAWL_DAYS_FORUM, via should_recrawl) is untouched.
REVIVE_DEAD_DAYS = 30

# A site is only marked dead after this many consecutive fetch failures, so a
# transient outage doesn't immediately drop it from the index.
MAX_FAIL_COUNT = 3


def _recrawl_days(url: str) -> int:
    url_lower = url.lower()
    if any(p in url_lower for p in FORUM_PATTERNS):
        return RECRAWL_DAYS_FORUM
    return RECRAWL_DAYS_DEFAULT


def checkpoint_wal() -> None:
    """Truncate the WAL so its sidecar file can't grow without bound.

    TRUNCATE checkpoints every committed frame back into the main DB and then
    shrinks -wal to zero bytes. Called periodically by the crawler during long
    continuous cycles (and by the memory watchdog before a forced restart).
    """
    with get_db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()


def should_recrawl(url: str) -> bool:
    """Return True if URL is new or last_seen is older than RECRAWL_DAYS."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_seen FROM pages WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return True
        try:
            last_seen = datetime.fromisoformat(row["last_seen"])
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last_seen).days >= _recrawl_days(url)
        except Exception:
            return True


def upsert_page(
    url: str,
    title: str,
    description: str,
    category: str,
    lang: str,
    score: float = 0.0,
    content_hash: str | None = None,
    page_type: str = "other",
    enrichment_method: str = "heuristic",
    content_tag: str = "unknown",
) -> None:
    with get_db() as conn:
        # Dedup invariant: the same content_hash must exist under exactly one URL.
        # If this content already lives under a *different* URL, treat the new
        # sighting as a re-sighting of that row — bump last_seen, do not insert a
        # second copy. (The DB also enforces this via the UNIQUE index once
        # scripts/dedupe.py has run; this app-level path keeps the invariant on a
        # not-yet-deduped DB and avoids relying on ON CONFLICT(content_hash)
        # erroring when the index is absent.)
        if content_hash:
            dup = conn.execute(
                "SELECT url FROM pages WHERE content_hash = ? AND url != ? LIMIT 1",
                (content_hash, url),
            ).fetchone()
            if dup is not None:
                conn.execute(
                    "UPDATE pages SET last_seen = CURRENT_TIMESTAMP, "
                    "last_scanned_at = CURRENT_TIMESTAMP, is_active = 1 WHERE url = ?",
                    (dup["url"],),
                )
                conn.commit()
                logger.debug(
                    "Duplicate content_hash %s: bumped last_seen on %s (skipped %s)",
                    content_hash, dup["url"], url,
                )
                return
        try:
            conn.execute(
                """
                INSERT INTO pages (url, title, description, category, lang, score, is_alive, is_active, last_seen, last_scanned_at, content_hash, page_type, enrichment_method, content_tag)
                VALUES (?, ?, ?, ?, ?, ?, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title             = excluded.title,
                    description       = excluded.description,
                    category          = excluded.category,
                    lang              = excluded.lang,
                    score             = excluded.score,
                    is_alive          = 1,
                    is_active         = 1,
                    fail_count        = 0,
                    last_scanned_at   = CURRENT_TIMESTAMP,
                    content_hash      = excluded.content_hash,
                    page_type         = excluded.page_type,
                    enrichment_method = excluded.enrichment_method,
                    content_tag       = excluded.content_tag,
                    last_seen    = CASE
                        WHEN excluded.content_hash IS NOT NULL
                             AND excluded.content_hash != COALESCE(pages.content_hash, '')
                        THEN CURRENT_TIMESTAMP
                        ELSE pages.last_seen
                        END
                """,
                (url, title, description, category, lang, score, content_hash, page_type, enrichment_method, content_tag),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost a race to another writer inserting the same content_hash under
            # a different URL (DB-level UNIQUE index caught it). Re-sight instead.
            if content_hash:
                conn.execute(
                    "UPDATE pages SET last_seen = CURRENT_TIMESTAMP, "
                    "last_scanned_at = CURRENT_TIMESTAMP, is_active = 1 WHERE content_hash = ?",
                    (content_hash,),
                )
                conn.commit()
            else:
                raise


def mark_dead(url: str) -> None:
    """Record a fetch failure. Only flips is_alive=0 after MAX_FAIL_COUNT
    consecutive failures so a single transient error doesn't drop the site."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE pages
            SET fail_count      = fail_count + 1,
                last_scanned_at = CURRENT_TIMESTAMP,
                is_alive   = CASE WHEN fail_count + 1 >= ? THEN 0 ELSE is_alive END,
                is_active  = CASE WHEN fail_count + 1 >= ? THEN 0 ELSE is_active END
            WHERE url = ?
            """,
            (MAX_FAIL_COUNT, MAX_FAIL_COUNT, url),
        )
        conn.commit()


def mark_alive(url: str) -> None:
    """Reset failure state after a successful crawl."""
    with get_db() as conn:
        conn.execute(
            "UPDATE pages SET is_alive = 1, is_active = 1, fail_count = 0 WHERE url = ?",
            (url,),
        )
        conn.commit()


def revive_check() -> List[str]:
    """Give long-dead sites another chance.

    Finds sites marked dead whose last successful sighting is older than
    REVIVE_DEAD_DAYS (30), resets them to alive with a clean fail counter, and
    returns their URLs so the crawler can re-queue them. Only is_alive=0 rows are
    touched, so live pages are unaffected.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT url FROM pages "
            "WHERE is_alive = 0 AND last_seen < datetime('now', ?)",
            (f"-{REVIVE_DEAD_DAYS} days",),
        ).fetchall()
        urls = [r["url"] for r in rows]
        if urls:
            conn.executemany(
                "UPDATE pages SET is_alive = 1, is_active = 1, fail_count = 0 WHERE url = ?",
                [(u,) for u in urls],
            )
            conn.commit()
    if urls:
        logger.info("Revived %d dead sites for re-crawl", len(urls))
    return urls


# Max user-submitted URLs pulled from crawl_queue per cycle. Bounds how much of
# a cycle's frontier the queue can occupy so seeds/recrawls still get airtime.
QUEUE_BATCH_LIMIT = 500


def claim_queue_batch(limit: int = QUEUE_BATCH_LIMIT) -> List[str]:
    """Claim a batch of pending crawl_queue URLs for the current crawl cycle.

    Selects the highest-priority, oldest-first pending rows, flips them to
    'processing' so a concurrent or subsequent cycle won't re-claim them, and
    returns their URLs to be folded into the crawl frontier. If the cycle aborts
    before crawling, call requeue_pending() to release the claim.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT url FROM crawl_queue WHERE status = 'pending' "
            "ORDER BY priority DESC, added_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        urls = [r["url"] for r in rows]
        if urls:
            conn.executemany(
                "UPDATE crawl_queue SET status = 'processing' WHERE url = ?",
                [(u,) for u in urls],
            )
            conn.commit()
    if urls:
        logger.info("Claimed %d URLs from crawl_queue", len(urls))
    return urls


def requeue_pending(urls: List[str]) -> None:
    """Return claimed URLs to 'pending' (cycle aborted before crawling them)."""
    if not urls:
        return
    with get_db() as conn:
        conn.executemany(
            "UPDATE crawl_queue SET status = 'pending' WHERE url = ?",
            [(u,) for u in urls],
        )
        conn.commit()


def reconcile_queue(urls: List[str]) -> None:
    """Resolve claimed crawl_queue rows after a cycle finishes.

    A URL now present in `pages` was crawled and indexed successfully -> 'done'.
    Anything still missing (unreachable, thin page, dead-cache skip) -> 'failed'
    so it isn't retried every cycle but stays visible for diagnostics.
    """
    if not urls:
        return
    with get_db() as conn:
        for url in urls:
            indexed = conn.execute(
                "SELECT 1 FROM pages WHERE url = ?", (url,)
            ).fetchone()
            status = "done" if indexed is not None else "failed"
            conn.execute(
                "UPDATE crawl_queue SET status = ? WHERE url = ?", (status, url)
            )
        conn.commit()


def cleanup_inactive_pages() -> int:
    """Daily GC of long-inactive pages, tiered by community trust.

    Only is_active=0 rows are eligible (a page reachable at last crawl is never
    deleted here). The retention window scales with trust: well-rated sites are
    kept longest, low-rated / scam / illegal the shortest, on the bet that a
    high-onion_score site is worth waiting on for a possible return.

      onion_score 2.0..3.9              -> delete after 30 days inactive
      onion_score < 2.0 OR scam/illegal -> delete after  7 days inactive
      onion_score >= 4.0                -> delete after 45 days inactive
      onion_score IS NULL (unrated)     -> delete after 30 days inactive

    Deleting a page fires the pages_ad trigger, so its FTS row is removed too.
    Returns the number of rows deleted.
    """
    rules = [
        (30, "onion_score BETWEEN 2.0 AND 3.9"),
        (7,  "(onion_score < 2.0 OR content_tag IN ('scam','illegal'))"),
        (45, "onion_score >= 4.0"),
        (30, "onion_score IS NULL"),
    ]
    deleted = 0
    with get_db() as conn:
        for days, predicate in rules:
            cur = conn.execute(
                f"DELETE FROM pages "
                f"WHERE is_active = 0 "
                f"AND last_scanned_at IS NOT NULL "
                f"AND last_scanned_at < datetime('now', '-{days} days') "
                f"AND {predicate}"
            )
            deleted += cur.rowcount
        conn.commit()
    if deleted:
        logger.info("cleanup: deleted %d inactive pages", deleted)
    return deleted


# CSAM keyword blocklist for retroactive purges. Mirrors spider.BLOCKED_KEYWORDS;
# kept here as a literal (no cross-module import) so the purge is self-contained
# and can run even if the crawler package layout changes. Lowercase substrings.
PURGE_KEYWORDS = (
    'loli', 'lolita', 'pedo', 'pedophil', 'preteen', 'pre-teen',
    'jailbait', 'childporn', 'child porn', 'cp porn', 'toddlercon',
    'underage', 'minor porn', 'kids porn', 'kiddie', 'shota', 'shotacon',
    'sophie webcam', 'tweenfan',
)


def purge_illegal_pages() -> int:
    """Retroactively delete any indexed CSAM/illegal pages. Idempotent.

    Removes rows whose title or description contains a blocked keyword
    (case-insensitive substring), plus any row already tagged illegal/csam.
    Deleting from `pages` fires the pages_ad trigger, so the matching FTS rows
    are removed in lockstep. Safe to run repeatedly: a clean DB deletes nothing.
    Returns the total number of rows deleted and logs it.
    """
    deleted = 0
    with get_db() as conn:
        # Keyword match on title/description. lower() on both sides makes the
        # LIKE explicit and locale-independent for the ASCII keyword set.
        keyword_clause = " OR ".join(
            "lower(title) LIKE ? OR lower(description) LIKE ?"
            for _ in PURGE_KEYWORDS
        )
        params: List[str] = []
        for keyword in PURGE_KEYWORDS:
            pattern = f"%{keyword}%"
            params.extend((pattern, pattern))
        cur = conn.execute(
            f"DELETE FROM pages WHERE {keyword_clause}", params
        )
        deleted += cur.rowcount

        # Tag-based sweep, guarded: content_tag may not exist on a legacy DB.
        if _column_exists(conn, "pages", "content_tag"):
            cur = conn.execute(
                "DELETE FROM pages WHERE content_tag IN ('illegal', 'csam')"
            )
            deleted += cur.rowcount

        conn.commit()
    logger.warning("purge_illegal_pages: deleted %d illegal pages", deleted)
    return deleted


def get_crawl_urls(include_dead: bool = False) -> List[str]:
    """URLs to feed into a scheduled crawl.

    Daily crawl (include_dead=False): live sites not seen in the last 23 hours.
    Weekly full crawl (include_dead=True): every known URL, dead ones included.

    NOTE: superseded by the v3 self-feeding get_next_batch() below. Kept for any
    legacy/maintenance scripts that still call it.
    """
    with get_db() as conn:
        if include_dead:
            rows = conn.execute("SELECT url FROM pages").fetchall()
        else:
            rows = conn.execute(
                "SELECT url FROM pages "
                "WHERE is_alive = 1 AND last_seen < datetime('now', '-23 hours')"
            ).fetchall()
    return [r["url"] for r in rows]


# ===========================================================================
# v3.0 SELF-FEEDING CRAWLER DB LAYER (PART A)
# ===========================================================================
# The crawler no longer relies on the in-code SEED_URLS each cycle. Instead it
# pulls work from the DB in priority tiers, writes discovered links straight
# back into the DB as never-crawled rows, and reschedules every URL via
# next_crawl_at. SEED_URLS are used only to bootstrap a completely empty DB.

# One refill batch is intentionally small (a few hundred URLs) so memory and a
# single Tor circuit stay bounded on the 1GB box.
TIER1_NEW_LIMIT = 100      # tier 1: never-crawled URLs (last_seen IS NULL)
TIER2_STALE_LIMIT = 50     # tier 2: alive pages not refreshed in > 7 days
TIER3_DEAD_LIMIT = 20      # tier 3: dead pages, < N attempts, cooled down
DEAD_MAX_ATTEMPTS = 3      # tier 3 stops retrying a URL after this many failures
FAILURE_BACKOFF_DAYS = 3   # next_crawl_at = now + attempts * this, on failure


def count_pages() -> int:
    """Total rows in `pages` — used to decide whether to bootstrap from seeds."""
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup before insert.

    Lowercases scheme + host, strips a trailing slash and any #fragment. Path and
    query case are preserved (they can be significant on some onion services).
    """
    p = urlparse(url.strip())
    scheme = (p.scheme or "http").lower()
    netloc = p.netloc.lower()
    path = p.path.rstrip("/")
    norm = f"{scheme}://{netloc}{path}"
    if p.query:
        norm += f"?{p.query}"
    return norm


def get_next_batch() -> List[str]:
    """Priority-tiered refill from the DB (PART A).

    Returns a de-duplicated, priority-ordered URL list assembled from three
    tiers. next_crawl_at gates every tier (a URL is never returned before its
    scheduled time), which is also how "skip re-crawl if next_crawl_at > now"
    and the dead-page back-off are enforced.

      Tier 1: never crawled (last_seen IS NULL)            — limit 100
      Tier 2: alive, not refreshed in > 7 days             — limit 50
      Tier 3: dead, attempts < 3, cooled down (next due)   — limit 20
    """
    urls: List[str] = []
    with get_db() as conn:
        urls += [r["url"] for r in conn.execute(
            "SELECT url FROM pages "
            "WHERE last_seen IS NULL "
            "AND (next_crawl_at IS NULL OR next_crawl_at <= CURRENT_TIMESTAMP) "
            "ORDER BY crawl_priority DESC, id ASC LIMIT ?",
            (TIER1_NEW_LIMIT,),
        ).fetchall()]
        urls += [r["url"] for r in conn.execute(
            "SELECT url FROM pages "
            "WHERE is_alive = 1 AND last_seen IS NOT NULL "
            "AND last_seen < datetime('now', '-7 days') "
            "AND (next_crawl_at IS NULL OR next_crawl_at <= CURRENT_TIMESTAMP) "
            "ORDER BY last_seen ASC LIMIT ?",
            (TIER2_STALE_LIMIT,),
        ).fetchall()]
        urls += [r["url"] for r in conn.execute(
            "SELECT url FROM pages "
            "WHERE is_alive = 0 AND crawl_attempts < ? "
            "AND (next_crawl_at IS NULL OR next_crawl_at <= CURRENT_TIMESTAMP) "
            "ORDER BY next_crawl_at ASC LIMIT ?",
            (DEAD_MAX_ATTEMPTS, TIER3_DEAD_LIMIT),
        ).fetchall()]
    # De-dup, preserving tier/priority order.
    seen = set()
    ordered: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def insert_discovered_links(urls: Iterable[str]) -> int:
    """Insert newly-discovered .onion URLs as never-crawled rows (PART A).

    URLs are normalized for dedup. last_seen is forced NULL so the tier-1 refill
    picks them up next round; existing rows are left untouched (INSERT OR IGNORE
    on the UNIQUE url). Returns the count of genuinely new rows.
    """
    norm = {normalize_url(u) for u in urls if u}
    if not norm:
        return 0
    inserted = 0
    with get_db() as conn:
        for u in norm:
            cur = conn.execute(
                "INSERT OR IGNORE INTO pages "
                "(url, is_alive, is_active, crawl_priority, crawl_attempts, "
                " last_seen, next_crawl_at) "
                "VALUES (?, 0, 0, 5, 0, NULL, CURRENT_TIMESTAMP)",
                (u,),
            )
            inserted += cur.rowcount
        conn.commit()
    if inserted:
        logger.info("discovered %d new URLs queued for crawl", inserted)
    return inserted


def _write_success(conn: sqlite3.Connection, rec: dict) -> None:
    """Write one successful crawl result on the shared connection (no commit).

    Mirrors upsert_page's content-dedup invariant, but additionally resets the
    v3 crawl bookkeeping per PART A's "on successful crawl" rule: last_seen=now,
    is_alive=1, crawl_attempts=0, and next_crawl_at pushed to the recrawl cadence.
    """
    url = rec["url"]
    content_hash = rec.get("content_hash")
    # Cross-URL content dedup: if this content already lives under a different
    # URL, re-sight that row instead of inserting a duplicate.
    if content_hash:
        dup = conn.execute(
            "SELECT url FROM pages WHERE content_hash = ? AND url != ? LIMIT 1",
            (content_hash, url),
        ).fetchone()
        if dup is not None:
            conn.execute(
                "UPDATE pages SET last_seen = CURRENT_TIMESTAMP, "
                "last_scanned_at = CURRENT_TIMESTAMP, is_active = 1 WHERE url = ?",
                (dup["url"],),
            )
            return
    days = _recrawl_days(url)
    conn.execute(
        """
        INSERT INTO pages (url, title, description, category, lang, score,
            is_alive, is_active, last_seen, last_scanned_at, content_hash,
            page_type, enrichment_method, content_tag, crawl_attempts, next_crawl_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, 0, datetime('now', ?))
        ON CONFLICT(url) DO UPDATE SET
            title             = excluded.title,
            description       = excluded.description,
            category          = excluded.category,
            lang              = excluded.lang,
            score             = excluded.score,
            is_alive          = 1,
            is_active         = 1,
            fail_count        = 0,
            crawl_attempts    = 0,
            last_seen         = CURRENT_TIMESTAMP,
            last_scanned_at   = CURRENT_TIMESTAMP,
            next_crawl_at     = excluded.next_crawl_at,
            content_hash      = excluded.content_hash,
            page_type         = excluded.page_type,
            enrichment_method = excluded.enrichment_method,
            content_tag       = excluded.content_tag
        """,
        (
            url, rec.get("title"), rec.get("description"), rec.get("category"),
            rec.get("lang"), rec.get("score", 0.0), content_hash,
            rec.get("page_type", "other"), rec.get("enrichment_method", "heuristic"),
            rec.get("content_tag", "unknown"), f"+{days} days",
        ),
    )


def write_crawl_batch(results: List[dict]) -> None:
    """Write a batch of successful crawl results in ONE transaction (PART A).

    Accumulating ~10 results per write (vs one connection per page) cuts SQLite
    lock contention between the crawler and the API. Per-row IntegrityError (a
    racing content_hash UNIQUE collision) degrades to a re-sight, not a failure.
    """
    if not results:
        return
    with get_db() as conn:
        for rec in results:
            try:
                _write_success(conn, rec)
            except sqlite3.IntegrityError:
                ch = rec.get("content_hash")
                if ch:
                    conn.execute(
                        "UPDATE pages SET last_seen = CURRENT_TIMESTAMP, "
                        "last_scanned_at = CURRENT_TIMESTAMP, is_active = 1 "
                        "WHERE content_hash = ?",
                        (ch,),
                    )
        conn.commit()
    logger.info("batch wrote %d crawl results", len(results))


def record_crawl_failure(url: str) -> None:
    """On a failed crawl (PART A): mark dead, bump attempts, push next_crawl_at
    out by attempts * FAILURE_BACKOFF_DAYS. The row is NEVER removed."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE pages SET "
            "is_alive = 0, is_active = 0, "
            "fail_count = fail_count + 1, "
            "crawl_attempts = crawl_attempts + 1, "
            "last_scanned_at = CURRENT_TIMESTAMP, "
            "next_crawl_at = datetime('now', '+' || ((crawl_attempts + 1) * ?) || ' days') "
            "WHERE url = ?",
            (FAILURE_BACKOFF_DAYS, url),
        )
        # A failure on a URL not yet in `pages` (e.g. a queued user URL): record
        # it so it still earns tiered retries instead of vanishing.
        if cur.rowcount == 0:
            conn.execute(
                "INSERT OR IGNORE INTO pages "
                "(url, is_alive, is_active, crawl_priority, crawl_attempts, "
                " last_seen, last_scanned_at, next_crawl_at) "
                "VALUES (?, 0, 0, 5, 1, NULL, CURRENT_TIMESTAMP, datetime('now', ?))",
                (url, f"+{FAILURE_BACKOFF_DAYS} days"),
            )
        conn.commit()


def record_crawl_skip(url: str) -> None:
    """Reachable but not indexable (thin page, or content-blocked post-fetch).

    Keeps the URL alive but defers its next crawl by the normal cadence so it
    leaves the never-crawled tier instead of being re-fetched every batch.
    """
    days = _recrawl_days(url)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE pages SET last_seen = CURRENT_TIMESTAMP, "
            "last_scanned_at = CURRENT_TIMESTAMP, is_alive = 1, is_active = 1, "
            "crawl_attempts = 0, next_crawl_at = datetime('now', ?) WHERE url = ?",
            (f"+{days} days", url),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT OR IGNORE INTO pages "
                "(url, is_alive, is_active, last_seen, last_scanned_at, next_crawl_at) "
                "VALUES (?, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, datetime('now', ?))",
                (url, f"+{days} days"),
            )
        conn.commit()
