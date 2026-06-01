"""Composite result scoring: BM25 relevance + freshness + alive boost.

FTS5 gives us BM25 relevance for free via the `rank` column (a negative float;
more negative = more relevant). On its own it ignores two things that matter a
lot on the dark web, where sites churn constantly:

  * freshness  — a page last seen months ago is likely stale or already dead.
  * liveness   — a page we know is unreachable should rank below a live one.

`compute_score` folds all three into one 0..~1.5 number, higher = better. The
search layer re-ranks the BM25 candidate window by this score (see
api/search.py) and returns it in the API `score` field for debugging/tuning.

The weights here intentionally reproduce the design handed down with this
change; tune the 0.6 / 0.4 split and the alive multipliers there.
"""

import math
from datetime import datetime, timezone
from typing import Optional, Union

# Pages seen within this many days incur no freshness penalty at all.
FRESH_WINDOW_DAYS = 7

# Relevance vs. recency split of the base score (before the alive multiplier).
W_BM25 = 0.6
W_FRESHNESS = 0.4

# Multipliers applied after the weighted base score.
ALIVE_BOOST = 1.5
DEAD_PENALTY = 0.3


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

    alive_boost = ALIVE_BOOST if is_alive else DEAD_PENALTY
    return (bm25_score * W_BM25 + freshness * W_FRESHNESS) * alive_boost
