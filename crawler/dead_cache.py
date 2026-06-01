"""Negative cache for unreachable .onion services.

Tor resolution of a dead hidden service costs ~30s (retries + HSDir lookups).
With thousands of dead onions in the seed/link graph that is the dominant cost
of a crawl cycle. This module records resolve/timeout failures and lets the
crawler skip a known-dead URL until a cooldown elapses, with an exponential
back-off so a permanently-dead host is retried geometrically less often instead
of every cycle.

State lives in the ``dead_onions`` table (created by the shared migration). All
helpers are best-effort: a DB hiccup must never crash the crawl, so failures are
logged and swallowed (``is_dead`` fails *open* — i.e. returns False, "try it").
"""

import logging

from api.models import get_db

logger = logging.getLogger(__name__)

# First failure earns a 7-day silence. Each subsequent failure doubles the
# cooldown (14, 28, 56...) capped at MAX_COOLDOWN_DAYS so a flapping host can
# still come back eventually but a truly dead one is left alone.
BASE_COOLDOWN_DAYS = 7
MAX_COOLDOWN_DAYS = 90


def _cooldown_days(fail_count: int) -> int:
    """Exponential back-off: 7, 14, 28, ... capped at MAX_COOLDOWN_DAYS."""
    exp = max(fail_count - 1, 0)
    return min(BASE_COOLDOWN_DAYS * (2 ** exp), MAX_COOLDOWN_DAYS)


def record_dead(url: str) -> None:
    """Upsert a resolve-failure/timeout for ``url`` (increment fail_count)."""
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO dead_onions (url, first_failed_at, last_failed_at, fail_count)
                VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
                ON CONFLICT(url) DO UPDATE SET
                    last_failed_at = CURRENT_TIMESTAMP,
                    fail_count     = dead_onions.fail_count + 1
                """,
                (url,),
            )
            conn.commit()
    except Exception:
        logger.exception("dead_cache.record_dead failed for %s", url)


def is_dead(url: str) -> bool:
    """True if ``url`` is cached dead and still inside its cooldown window.

    Fails open (returns False) on any error so a cache problem never blocks a
    legitimate crawl.
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT fail_count, last_failed_at FROM dead_onions WHERE url = ?",
                (url,),
            ).fetchone()
            if row is None:
                return False
            cooldown = _cooldown_days(row["fail_count"])
            still_cooling = conn.execute(
                "SELECT last_failed_at > datetime('now', ?) FROM dead_onions WHERE url = ?",
                (f"-{cooldown} days", url),
            ).fetchone()
            return bool(still_cooling and still_cooling[0])
    except Exception:
        logger.exception("dead_cache.is_dead failed for %s", url)
        return False


def clear_dead(url: str) -> None:
    """Remove ``url`` from the negative cache after a successful fetch."""
    try:
        with get_db() as conn:
            cur = conn.execute("DELETE FROM dead_onions WHERE url = ?", (url,))
            conn.commit()
            if cur.rowcount:
                logger.info("dead_cache: %s revived, cleared from negative cache", url)
    except Exception:
        logger.exception("dead_cache.clear_dead failed for %s", url)


def revive_candidates(limit: int = 500) -> list[str]:
    """URLs whose cooldown has expired — eligible for exactly one retry.

    Bounded by ``limit`` so a huge dead set can't balloon the crawl queue. The
    rows are left in place; if the retry fails again ``record_dead`` bumps
    fail_count and the cooldown grows. If it succeeds, ``clear_dead`` removes it.
    """
    try:
        with get_db() as conn:
            # Per-row cooldown comparison: expired when
            #   last_failed_at <= now - cooldown_days(fail_count)
            # Computed in SQL so we never load the whole table.
            rows = conn.execute(
                """
                SELECT url FROM dead_onions
                WHERE last_failed_at <= datetime(
                    'now',
                    '-' || MIN(?, ? * (1 << (fail_count - 1))) || ' days'
                )
                LIMIT ?
                """,
                (MAX_COOLDOWN_DAYS, BASE_COOLDOWN_DAYS, limit),
            ).fetchall()
            urls = [r["url"] for r in rows]
    except Exception:
        logger.exception("dead_cache.revive_candidates failed")
        return []
    if urls:
        logger.info("dead_cache: %d onions past cooldown, granting one retry", len(urls))
    return urls
