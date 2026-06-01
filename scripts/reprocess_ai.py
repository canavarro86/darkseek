"""Backfill job: upgrade heuristic-enriched rows to AI enrichment.

When the Claude credit balance is restored, this re-enriches rows that were
written by the local fallback (``enrichment_method = 'heuristic'``). It does NOT
re-crawl — it classifies from the title + description already stored, so it is
cheap and safe to run anytime.

Design:
  * Resumable / idempotent — driven entirely by the DB flag. A successful row
    flips to 'ai' and is never reselected; an interrupted run just resumes.
  * Rate-limited & circuit-aware — uses the same token bucket and breaker as the
    live crawler (via classify_text), so it can't blow the budget or hammer a
    dead API. If the circuit opens (credits gone again) the run stops cleanly.
  * Memory-bounded — processes in batches of BATCH_SIZE; failures are remembered
    in a small in-memory set so the run can't loop on them.

Usage:
    python -m scripts.reprocess_ai [max_rows]
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import get_db  # noqa: E402
from crawler.ai_describe import classify_text  # noqa: E402
from crawler.ai_describe import _breaker  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reprocess_ai")

BATCH_SIZE = 10


def _heuristic_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM pages WHERE enrichment_method = 'heuristic'"
    ).fetchone()[0]


def reprocess(max_rows: int | None = None) -> int:
    """Upgrade heuristic rows to AI. Returns the number successfully upgraded."""
    upgraded = 0
    failed_ids: set[int] = set()  # bounded by rows touched this run
    start = time.monotonic()

    with get_db() as conn:
        total = _heuristic_count(conn)
    target = total if max_rows is None else min(total, max_rows)
    logger.info("Heuristic rows: %d; will attempt up to %d this run", total, target)

    while True:
        if _breaker.is_open:
            logger.warning("AI circuit is open (credits/API down) — stopping run")
            break
        if max_rows is not None and upgraded >= max_rows:
            logger.info("Reached max_rows=%d for this run", max_rows)
            break

        # Exclude rows that already failed this run so we don't loop on them.
        with get_db() as conn:
            if failed_ids:
                placeholders = ",".join("?" * len(failed_ids))
                rows = conn.execute(
                    f"SELECT id, url, title, description FROM pages "
                    f"WHERE enrichment_method = 'heuristic' "
                    f"AND id NOT IN ({placeholders}) LIMIT ?",
                    (*failed_ids, BATCH_SIZE),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, url, title, description FROM pages "
                    "WHERE enrichment_method = 'heuristic' LIMIT ?",
                    (BATCH_SIZE,),
                ).fetchall()

        if not rows:
            logger.info("No more heuristic rows to process")
            break

        for row in rows:
            text = " ".join(p for p in (row["title"], row["description"]) if p).strip()
            result = classify_text(text, row["url"])
            if result is None:
                # Leave as 'heuristic' for a later run; remember so we advance.
                failed_ids.add(row["id"])
                continue
            with get_db() as conn:
                conn.execute(
                    "UPDATE pages SET title = ?, description = ?, category = ?, "
                    "lang = ?, enrichment_method = 'ai' WHERE id = ?",
                    (
                        result["title"],
                        result["description"],
                        result["category"],
                        result["lang"],
                        row["id"],
                    ),
                )
                conn.commit()
            upgraded += 1

        # Progress + ETA based on observed throughput.
        elapsed = time.monotonic() - start
        rate = upgraded / elapsed if elapsed > 0 and upgraded else 0.0
        remaining = max(target - upgraded, 0)
        eta = remaining / rate if rate > 0 else float("inf")
        logger.info(
            "Progress: upgraded=%d failed=%d remaining~%d rate=%.2f/s eta=%s",
            upgraded,
            len(failed_ids),
            remaining,
            rate,
            f"{eta/60:.1f}m" if eta != float("inf") else "n/a",
        )

    logger.info("Backfill run done: upgraded %d rows (%d left as heuristic)",
                upgraded, len(failed_ids))
    return upgraded


if __name__ == "__main__":
    arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    reprocess(arg)
