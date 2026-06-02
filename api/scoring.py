"""Composite result scoring: BM25 relevance + freshness + a freshness-tier multiplier.

FTS5 gives us BM25 relevance for free via the `rank` column (a negative float;
more negative = more relevant). On its own it ignores two things that matter a
lot on the dark web, where sites churn constantly:

  * freshness  — a page last seen months ago is likely stale or already dead.
  * liveness   — a page we know is unreachable should rank below a live one.

`compute_score` folds all three into one 0..1 number, higher = better. The
search layer re-ranks the BM25 candidate window by this score (see
api/search.py) and returns it in the API `score` field for debugging/tuning.

Freshness tiers (the v1.4 "fresh content" feature) classify each page into one
of three buckets, surfaced to the frontend via `freshness_tier`:

  * fresh    — is_alive=1 AND last_seen within FRESH_WINDOW_DAYS. No penalty.
  * alive    — is_alive=1 but last_seen older than that. No penalty.
  * archived — is_alive=0 (unreachable at last crawl). Score multiplied by 0.7
               and (in api/search.py) sorted after every alive result.

The weights here intentionally reproduce the design handed down with this
change; tune the 0.6 / 0.4 split and the tier multipliers here.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

# Pages seen within this many days are "fresh": they incur no freshness penalty
# in the score curve, and form the boundary between the FRESH and ALIVE tiers.
FRESH_WINDOW_DAYS = 7

# Relevance vs. recency split of the base score (before the tier multiplier).
W_BM25 = 0.6
W_FRESHNESS = 0.4

# Freshness-tier multipliers, applied to the weighted base score:
#   FRESH and ALIVE (both is_alive=1) keep their full score — no penalty.
#   ARCHIVED (is_alive=0) is demoted to 70% so live results outrank it.
ALIVE_MULTIPLIER = 1.0
ARCHIVED_MULTIPLIER = 0.7


def _coerce_datetime(value: Union[str, datetime, None]) -> Optional[datetime]:
    """Best-effort parse of a SQLite TIMESTAMP into a tz-aware datetime (UTC).

    SQLite stores `last_seen` as 'YYYY-MM-DD HH:MM:SS' (naive, UTC by
    convention). Returns None when the value is missing/unparseable so the
    caller can fall back to a neutral freshness.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace(" ", "T", 1))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_since_scan(
    last_seen: Union[str, datetime, None],
    now: Optional[datetime] = None,
) -> int:
    """Whole days since the page was last successfully crawled.

    today -> 0, yesterday -> 1, eight days ago -> 8. Returns 0 when last_seen is
    missing/unparseable; in practice last_seen carries a NOT-NULL DB default, so
    that fallback is effectively unreachable.
    """
    now = now or datetime.now(timezone.utc)
    seen = _coerce_datetime(last_seen)
    if seen is None:
        return 0
    return max(0, (now - seen).days)


def freshness_tier(
    is_alive: int,
    last_seen: Union[str, datetime, None],
    now: Optional[datetime] = None,
) -> str:
    """Classify a page into 'fresh' | 'alive' | 'archived'.

    archived: is_alive == 0 (unreachable at last crawl).
    fresh:    alive AND last_seen >= now - FRESH_WINDOW_DAYS.
    alive:    alive but last_seen older than the fresh window (or unknown age).
    """
    if not is_alive:
        return "archived"
    now = now or datetime.now(timezone.utc)
    seen = _coerce_datetime(last_seen)
    if seen is not None and seen >= now - timedelta(days=FRESH_WINDOW_DAYS):
        return "fresh"
    return "alive"


def compute_score(
    bm25_rank: float,
    last_seen: Union[str, datetime, None],
    is_alive: int,
    now: Optional[datetime] = None,
) -> float:
    """Combine BM25 rank, freshness, and liveness into one score (higher=better).

    Args:
        bm25_rank: FTS5 `rank` value (negative float; closer to 0 = less
            relevant, more negative = more relevant).
        last_seen: when the page was last successfully crawled.
        is_alive: 1 if the page is currently reachable, else 0.
        now: injectable clock for testing; defaults to current UTC time.

    Worked examples (the W_BM25=0.6 / W_FRESHNESS=0.4 split is unchanged):
        # FRESH, perfect relevance: bm25_rank=0 -> bm25_score=1.0; seen today ->
        # freshness=1.0; is_alive=1. base = 1.0*0.6 + 1.0*0.4 = 1.0; *1.0 = 1.0.
        assert compute_score(0.0, NOW, 1, now=NOW) == 1.0          # not penalized
        # ARCHIVED, same relevance/age but is_alive=0: 1.0 * 0.7 = 0.7.
        assert compute_score(0.0, NOW, 0, now=NOW) == 0.7          # score *= 0.7
        # ALIVE-but-stale, seen 8 days ago: freshness = 1/(1+log1p(1)) ~= 0.5906;
        # base = 0.6 + 0.5906*0.4 ~= 0.8362; *1.0 ~= 0.8362 (> the 0.7 archived).
    """
    # BM25: map the unbounded negative rank to (0, 1], higher = more relevant.
    bm25_score = 1.0 / (1.0 + abs(bm25_rank))

    # Freshness: full score inside the fresh window, then logarithmic decay so
    # an old page is demoted gently rather than falling off a cliff.
    now = now or datetime.now(timezone.utc)
    seen = _coerce_datetime(last_seen)
    if seen is None:
        freshness = 0.5  # unknown age -> neutral, don't reward or punish
    else:
        days_old = (now - seen).days
        freshness = 1.0 / (1.0 + math.log1p(max(0, days_old - FRESH_WINDOW_DAYS)))

    # Freshness-tier multiplier: ARCHIVED (dead) pages are demoted to 70%; every
    # alive page (FRESH and ALIVE alike) keeps its full base score.
    tier_multiplier = ALIVE_MULTIPLIER if is_alive else ARCHIVED_MULTIPLIER
    return (bm25_score * W_BM25 + freshness * W_FRESHNESS) * tier_multiplier
