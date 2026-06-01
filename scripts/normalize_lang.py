"""Normalize the `lang` column to lowercase ISO-639-1 codes.

The corpus accumulated variants like en, en-US, en-GB, en_US, ru-RU, zh-cn,
zh-CN. This rewrites them all through the single source of truth,
``crawler.ai_describe.normalize_lang``, so search/facets see one code per
language.

Memory-bounded: works off the DISTINCT set of raw lang values (a few dozen
rows), issuing one set-based UPDATE per value. No per-row Python loop over the
whole table.

Idempotent: a second run finds everything already normalized and updates 0 rows.

Usage:
    python -m scripts.normalize_lang
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import get_db  # noqa: E402
from crawler.ai_describe import normalize_lang  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("normalize_lang")


def normalize() -> int:
    """Rewrite non-normalized lang values. Returns rows changed."""
    changed = 0
    with get_db() as conn:
        raw_values = [
            r["lang"]
            for r in conn.execute(
                "SELECT DISTINCT lang FROM pages WHERE lang IS NOT NULL"
            ).fetchall()
        ]
        logger.info("Found %d distinct lang values", len(raw_values))

        for raw in raw_values:
            norm = normalize_lang(raw)
            if norm == raw:
                continue  # already canonical
            cur = conn.execute(
                "UPDATE pages SET lang = ? WHERE lang = ?", (norm, raw)
            )
            conn.commit()
            changed += cur.rowcount
            logger.info("%r -> %r (%d rows)", raw, norm, cur.rowcount)

    logger.info("Normalization complete: %d rows updated", changed)
    return changed


if __name__ == "__main__":
    normalize()
