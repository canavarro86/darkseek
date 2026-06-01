import logging
import sqlite3
from datetime import datetime, timezone
from typing import List

# Single, unified DB layer (WAL + pragmas + migrations) lives in api.models.
# The crawler image bundles api/, so this import works in both processes.
from api.models import get_db

logger = logging.getLogger(__name__)

RECRAWL_DAYS_DEFAULT = 7
RECRAWL_DAYS_FORUM = 1
FORUM_PATTERNS = ["forum", "board", "thread", "topic", "chan"]

# A site is only marked dead after this many consecutive fetch failures, so a
# transient outage doesn't immediately drop it from the index.
MAX_FAIL_COUNT = 3


def _recrawl_days(url: str) -> int:
    url_lower = url.lower()
    if any(p in url_lower for p in FORUM_PATTERNS):
        return RECRAWL_DAYS_FORUM
    return RECRAWL_DAYS_DEFAULT


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
                    "UPDATE pages SET last_seen = CURRENT_TIMESTAMP WHERE url = ?",
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
                INSERT INTO pages (url, title, description, category, lang, score, is_alive, last_seen, content_hash, page_type, enrichment_method)
                VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title             = excluded.title,
                    description       = excluded.description,
                    category          = excluded.category,
                    lang              = excluded.lang,
                    score             = excluded.score,
                    is_alive          = 1,
                    fail_count        = 0,
                    content_hash      = excluded.content_hash,
                    page_type         = excluded.page_type,
                    enrichment_method = excluded.enrichment_method,
                    last_seen    = CASE
                        WHEN excluded.content_hash IS NOT NULL
                             AND excluded.content_hash != COALESCE(pages.content_hash, '')
                        THEN CURRENT_TIMESTAMP
                        ELSE pages.last_seen
                        END
                """,
                (url, title, description, category, lang, score, content_hash, page_type, enrichment_method),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost a race to another writer inserting the same content_hash under
            # a different URL (DB-level UNIQUE index caught it). Re-sight instead.
            if content_hash:
                conn.execute(
                    "UPDATE pages SET last_seen = CURRENT_TIMESTAMP WHERE content_hash = ?",
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
            SET fail_count = fail_count + 1,
                is_alive   = CASE WHEN fail_count + 1 >= ? THEN 0 ELSE is_alive END
            WHERE url = ?
            """,
            (MAX_FAIL_COUNT, url),
        )
        conn.commit()


def mark_alive(url: str) -> None:
    """Reset failure state after a successful crawl."""
    with get_db() as conn:
        conn.execute(
            "UPDATE pages SET is_alive = 1, fail_count = 0 WHERE url = ?", (url,)
        )
        conn.commit()


def revive_check() -> List[str]:
    """Give long-dead sites another chance.

    Finds sites marked dead whose last successful sighting is older than 7 days,
    resets them to alive with a clean fail counter, and returns their URLs so the
    crawler can re-queue them.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT url FROM pages "
            "WHERE is_alive = 0 AND last_seen < datetime('now', '-7 days')"
        ).fetchall()
        urls = [r["url"] for r in rows]
        if urls:
            conn.executemany(
                "UPDATE pages SET is_alive = 1, fail_count = 0 WHERE url = ?",
                [(u,) for u in urls],
            )
            conn.commit()
    if urls:
        logger.info("Revived %d dead sites for re-crawl", len(urls))
    return urls


def get_crawl_urls(include_dead: bool = False) -> List[str]:
    """URLs to feed into a scheduled crawl.

    Daily crawl (include_dead=False): live sites not seen in the last 23 hours.
    Weekly full crawl (include_dead=True): every known URL, dead ones included.
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
