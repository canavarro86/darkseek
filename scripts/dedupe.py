"""One-shot cleanup of duplicate content_hash rows.

The crawler now holds the "one content_hash -> one URL" invariant at write time,
but the live DB still carries ~1,215 historical duplicates. This script collapses
them, keeping the EARLIEST indexed_at per hash (the original discovery), then
creates the partial UNIQUE index so the DB enforces the invariant going forward.

Properties:
  * Re-runnable / idempotent — a second run removes 0 rows and the index create
    is a no-op.
  * Memory-bounded — the delete set is materialized in a temp table and deleted
    in batches; no full result set is pulled into Python.
  * FTS stays in sync automatically via the AFTER DELETE trigger on `pages`.

Usage:
    python -m scripts.dedupe
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import ensure_content_hash_unique_index, get_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dedupe")

DELETE_BATCH = 500  # rows deleted per statement, to keep transactions small


def dedupe() -> int:
    """Collapse duplicate content_hash rows. Returns the number removed."""
    removed = 0
    with get_db() as conn:
        # Build the delete set once: every hashed row that is NOT the earliest
        # (indexed_at, id) for its content_hash. Window function runs in SQLite's
        # temp store (PRAGMA temp_store=MEMORY) — a few thousand ints at most.
        conn.execute("DROP TABLE IF EXISTS _dupe_delete")
        conn.execute(
            """
            CREATE TEMP TABLE _dupe_delete AS
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY content_hash
                           ORDER BY indexed_at ASC, id ASC
                       ) AS rn
                FROM pages
                WHERE content_hash IS NOT NULL
            )
            WHERE rn > 1
            """
        )
        total = conn.execute("SELECT COUNT(*) FROM _dupe_delete").fetchone()[0]
        logger.info("Found %d duplicate rows to remove", total)

        # Delete in bounded batches so the transaction/journal never balloons.
        while True:
            cur = conn.execute(
                "DELETE FROM pages WHERE id IN "
                "(SELECT id FROM _dupe_delete LIMIT ?)",
                (DELETE_BATCH,),
            )
            if cur.rowcount == 0:
                break
            removed += cur.rowcount
            conn.execute(
                "DELETE FROM _dupe_delete WHERE id IN "
                "(SELECT id FROM _dupe_delete LIMIT ?)",
                (DELETE_BATCH,),
            )
            conn.commit()
            logger.info("Removed %d/%d", removed, total)

        conn.execute("DROP TABLE IF EXISTS _dupe_delete")
        conn.commit()

    # Now that the data is clean, the UNIQUE index can be created.
    with get_db() as conn:
        created = ensure_content_hash_unique_index(conn)
    logger.info(
        "Dedupe complete: removed %d rows; content_hash UNIQUE index %s",
        removed,
        "present" if created else "STILL BLOCKED (check for remaining dups)",
    )
    return removed


if __name__ == "__main__":
    dedupe()
