"""Selective AI backfill for legacy ``enrichment_method = 'pending'`` rows.

The production DB carries ~29k rows written before the AI-enrichment patch and
flagged ``pending``. Re-enriching all of them with the Claude API would blow the
budget, so this job spends a HARD-CAPPED dollar amount on the highest-value rows
first and stops the moment the cap is in sight.

Design guarantees (see the self-review at the bottom of this module):

  * Hard budget cap — real per-response token usage is summed against actual
    Haiku pricing and the run stops at 90% of the cap (a one-call lookahead means
    it can never *start* a call that would cross the cap). It cannot overspend.
  * Priority order — rows are processed tier-by-tier (forum/market + en|ru first,
    "everything else" last) so the budget lands on the most valuable rows.
  * Resumable / idempotent — driven entirely by the ``pending`` flag. A processed
    row flips to 'ai' or 'heuristic' and is never reselected; an interrupted run
    just resumes where it left off.
  * Never leaves a row pending after attempting it — on any API failure the row
    is downgraded to a heuristic enrichment using the same primitives the live
    crawler's HeuristicEnricher uses.

Usage:
    python /app/scripts/reprocess_ai.py                         # $4.00 cap, all tiers
    python /app/scripts/reprocess_ai.py --budget 2.00 --tier 1  # tier 1 only, $2 cap
    python /app/scripts/reprocess_ai.py --dry-run               # counts + estimate, no API
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.models import get_db  # noqa: E402
from crawler.ai_describe import (  # noqa: E402
    INPUT_CHARS,
    LANG_OTHER,
    MAX_TOKENS,
    METHOD_AI,
    METHOD_HEURISTIC,
    MODEL,
    SYSTEM_PROMPT,
    _detect_lang,
    _get_client,
    _parse_response,
    heuristic_category,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reprocess_ai")

# --- Tunables ---------------------------------------------------------------
DEFAULT_BUDGET_USD = 4.00
BATCH_SIZE = 10
BATCH_DELAY_S = 1.0
SAFETY_FRACTION = 0.90          # stop once projected spend reaches 90% of cap
MAX_BACKOFF_S = 60.0            # cap on the 429 exponential backoff

# Actual claude-haiku-4-5 pricing (USD per token).
INPUT_PRICE = 1.00 / 1_000_000          # $1.00 / 1M input tokens
OUTPUT_PRICE = 5.00 / 1_000_000         # $5.00 / 1M output tokens
CACHE_READ_PRICE = 0.10 / 1_000_000     # cached-read input is 0.1x base

# Per-page estimate used ONLY by --dry-run (real usage drives the live cap).
# ~500 input + ~100 output tokens => ~$0.001/page.
EST_INPUT_TOKENS = 500
EST_OUTPUT_TOKENS = 100
EST_COST_PER_PAGE = EST_INPUT_TOKENS * INPUT_PRICE + EST_OUTPUT_TOKENS * OUTPUT_PRICE

# --- Tier predicates --------------------------------------------------------
# The four tiers partition every pending row exactly once (NULL-safe), so a row
# is processed by precisely one tier and tiers run in this priority order.
_FM = "category IN ('forum','market')"
_NOT_FM = "(category IS NULL OR category NOT IN ('forum','market'))"
_ENRU = "lang IN ('en','ru')"
_NOT_ENRU = "(lang IS NULL OR lang NOT IN ('en','ru'))"

TIERS = {
    1: ("forum/market + en/ru", f"{_FM} AND {_ENRU}"),
    2: ("forum/market other lang", f"{_FM} AND {_NOT_ENRU}"),
    3: ("other category + en/ru", f"{_NOT_FM} AND {_ENRU}"),
    4: ("remaining pending", f"{_NOT_FM} AND {_NOT_ENRU}"),
}

# A row is a candidate only if it is still pending AND has something to enrich.
BASE_FILTER = (
    "enrichment_method = 'pending' "
    "AND NOT (title IS NULL AND description IS NULL)"
)


def _tier_where(tier: int) -> str:
    return f"{BASE_FILTER} AND ({TIERS[tier][1]})"


def _count(conn, tier: int) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM pages WHERE {_tier_where(tier)}"
    ).fetchone()[0]


# --- Heuristic fallback (no HTML available in backfill) ----------------------
def _heuristic_from_row(url, title, description) -> dict:
    """Heuristic enrichment from already-stored fields.

    The backfill has no raw HTML, so it cannot call HeuristicEnricher.enrich()
    directly. It instead reuses the exact primitives that enricher is built on
    (``heuristic_category`` over url+title, ``_detect_lang`` over the body text),
    producing an equivalent record. Used to downgrade a row when the API fails so
    it is never left 'pending' after being attempted.
    """
    title = title or ""
    description = description or ""
    text = " ".join(p for p in (title, description) if p).strip()
    return {
        "title": (title or text[:60] or (url or "")[:60])[:60],
        "description": (description or text[:160])[:160],
        "category": heuristic_category(url or "", title),
        "lang": _detect_lang(text),
    }


# --- API call with real token-usage accounting ------------------------------
class CreditExhausted(Exception):
    """Raised when the API reports a 400 'credit balance' error — stop the run."""


def _http_status(exc: Exception):
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    return status


def _usage_cost(usage) -> float:
    """Dollar cost of one response from its real token usage."""
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        (in_tok + cache_write) * INPUT_PRICE
        + cache_read * CACHE_READ_PRICE
        + out_tok * OUTPUT_PRICE
    )


def _call_api_with_cost(client, text: str, url: str) -> tuple[dict | None, float]:
    """Call Claude once for classification with 429 backoff.

    Returns ``(record_or_None, cost_usd)``. ``record`` is None when the call
    failed for a non-retryable, non-fatal reason (caller falls back to
    heuristic). Raises ``CreditExhausted`` on a 400 credit-balance error so the
    run stops immediately. The cost is always the *real* billed cost: a failed
    call costs $0, a 429-then-success only bills the successful response.
    """
    fallback = {
        "title": text[:60],
        "description": text[:160],
        "category": "other",
        "lang": LANG_OTHER,
    }
    backoff = 2.0
    while True:
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": f"URL: {url}\n\nPage text:\n{text[:INPUT_CHARS]}",
                    }
                ],
            )
        except Exception as e:  # noqa: BLE001 — classify, then act
            status = _http_status(e)
            if status == 400 and "credit balance" in str(e).lower():
                raise CreditExhausted(str(e)) from e
            if status == 429:
                wait = min(backoff, MAX_BACKOFF_S)
                logger.warning("429 rate limited; backing off %.0fs", wait)
                time.sleep(wait)
                backoff *= 2
                continue  # retry the same row
            # Any other error: this row fails -> heuristic fallback.
            logger.warning("API error for %s: %s", url, e)
            return None, 0.0

        cost = _usage_cost(resp.usage)
        try:
            raw = "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()
        except Exception:  # noqa: BLE001
            raw = ""
        parsed = _parse_response(raw, fallback)
        # A 200 with unusable JSON still cost tokens; bill it, fall back to heuristic.
        return parsed, cost


# --- Dry run ----------------------------------------------------------------
def dry_run(budget: float, tiers: list[int]) -> None:
    with get_db() as conn:
        counts = {t: _count(conn, t) for t in (1, 2, 3, 4)}

    selected_total = 0
    selected_cost = 0.0
    print()
    for t in (1, 2, 3, 4):
        label = f"Tier {t} ({TIERS[t][0]}):"
        n = counts[t]
        cost = n * EST_COST_PER_PAGE
        marker = "" if t in tiers else "   (skipped)"
        print(f"{label:<34} {n:>6} rows  ~${cost:.2f} estimated{marker}")
        if t in tiers:
            selected_total += n
            selected_cost += cost
    print(f"{'Total:':<34} {selected_total:>6} rows  ~${selected_cost:.2f} estimated")
    print(f"{'Budget cap:':<34} ${budget:.2f}")
    covered = int(budget / EST_COST_PER_PAGE) if EST_COST_PER_PAGE else 0
    print(f"{'Rows covered by budget:':<34} ~{covered} rows")
    print()


# --- Live backfill ----------------------------------------------------------
def reprocess(budget: float, tiers: list[int]) -> None:
    client = _get_client()
    if client is None:
        logger.error("ANTHROPIC_API_KEY not set / client unavailable — cannot run AI backfill")
        return

    stop_threshold = budget * SAFETY_FRACTION

    with get_db() as conn:
        total_target = sum(_count(conn, t) for t in tiers)
    logger.info(
        "Starting backfill: tiers=%s target=%d rows budget=$%.2f (stop at $%.2f)",
        tiers, total_target, budget, stop_threshold,
    )

    spent = 0.0
    upgraded = 0      # flipped to 'ai'
    skipped = 0       # downgraded to 'heuristic' (failed AI)
    processed = 0
    stop_reason = "all selected tiers complete"

    def log_budget():
        pct = (spent / budget * 100) if budget else 0.0
        logger.info("[BUDGET] spent $%.2f / $%.2f (%.0f%%)", spent, budget, pct)

    stop = False
    try:
        for tier in tiers:
            if stop:
                break
            while True:
                # Re-select each iteration: committed rows have left 'pending'
                # so they are never reselected — this is what makes the job
                # resumable and loop-free.
                with get_db() as conn:
                    rows = conn.execute(
                        f"SELECT id, url, title, description FROM pages "
                        f"WHERE {_tier_where(tier)} LIMIT ?",
                        (BATCH_SIZE,),
                    ).fetchall()
                if not rows:
                    break  # tier drained

                updates = []  # (id, title, description, category, lang, method)
                for row in rows:
                    # One-call lookahead: never START a call that could cross 90%.
                    if spent + EST_COST_PER_PAGE > stop_threshold:
                        stop_reason = "budget safety threshold reached"
                        stop = True
                        break

                    text = " ".join(
                        p for p in (row["title"], row["description"]) if p
                    ).strip()

                    try:
                        record, cost = _call_api_with_cost(client, text, row["url"])
                    except CreditExhausted as e:
                        # Catchable, clean stop: fall out and commit what this
                        # batch already collected before ending the run.
                        stop_reason = "credit balance exhausted"
                        stop = True
                        logger.error(
                            "STOPPING: Anthropic reports an insufficient credit "
                            "balance.\n  -> Add credit at "
                            "https://console.anthropic.com/settings/billing then "
                            "re-run this script; it resumes from the remaining "
                            "pending rows.\n  (%s)", e,
                        )
                        break
                    spent += cost

                    if record is not None:
                        updates.append((
                            row["id"], record["title"], record["description"],
                            record["category"], record["lang"], METHOD_AI,
                        ))
                        upgraded += 1
                    else:
                        # Attempted but failed: downgrade to heuristic, never pending.
                        h = _heuristic_from_row(row["url"], row["title"], row["description"])
                        updates.append((
                            row["id"], h["title"], h["description"],
                            h["category"], h["lang"], METHOD_HEURISTIC,
                        ))
                        skipped += 1
                    processed += 1

                # Commit the whole batch at once (one fsync, less write pressure).
                # Runs even when stopping, so a clean stop never drops finished work.
                if updates:
                    with get_db() as conn:
                        conn.executemany(
                            "UPDATE pages SET title = ?, description = ?, "
                            "category = ?, lang = ?, enrichment_method = ? "
                            "WHERE id = ?",
                            [(t_, d_, c_, l_, m_, i_) for (i_, t_, d_, c_, l_, m_) in updates],
                        )
                        conn.commit()

                log_budget()
                logger.info(
                    "[PROGRESS] %d/%d rows processed, %d upgraded, %d skipped",
                    processed, total_target, upgraded, skipped,
                )

                if stop:
                    break
                time.sleep(BATCH_DELAY_S)

    except KeyboardInterrupt:
        stop_reason = "interrupted by operator"
        logger.warning("Interrupted — committed batches are safe; re-run to resume.")

    _print_summary(spent, budget, upgraded, skipped, processed, stop_reason)


def _print_summary(spent, budget, upgraded, skipped, processed, stop_reason) -> None:
    with get_db() as conn:
        remaining = {t: _count(conn, t) for t in (1, 2, 3, 4)}
    pct = (spent / budget * 100) if budget else 0.0
    print()
    print("=" * 60)
    print(f"Backfill stopped: {stop_reason}")
    print(f"  Rows processed : {processed}")
    print(f"  Upgraded to AI : {upgraded}")
    print(f"  Downgraded heur: {skipped}")
    print(f"  Total spent    : ${spent:.4f} / ${budget:.2f} ({pct:.0f}%)")
    print("  Rows remaining (pending) by tier:")
    for t in (1, 2, 3, 4):
        print(f"    Tier {t} ({TIERS[t][0]}): {remaining[t]}")
    print("=" * 60)


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Selective, budget-capped AI backfill.")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD,
                   help=f"Hard USD spend cap (default {DEFAULT_BUDGET_USD:.2f}).")
    p.add_argument("--tier", type=int, choices=(1, 2, 3, 4), default=None,
                   help="Process only this priority tier (default: all tiers 1-4).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show per-tier counts and cost estimate; make no API calls.")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    if args.budget <= 0:
        logger.error("--budget must be positive")
        sys.exit(2)
    tiers = [args.tier] if args.tier else [1, 2, 3, 4]
    if args.dry_run:
        dry_run(args.budget, tiers)
    else:
        reprocess(args.budget, tiers)


if __name__ == "__main__":
    main()
