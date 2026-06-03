"""FTS5-backed page search: parse -> match -> composite re-rank.

Pipeline per request:
  1. parse_query()  turns raw input into a safe FTS5 MATCH expression, a list
     of terms to exclude, and the plain terms (for the frontend to highlight).
  2. FTS5 MATCH selects candidate rows ordered by BM25 `rank`.
  3. compute_score() re-ranks the candidate window by BM25 + freshness + tier,
     with every alive page sorted ahead of any archived (is_alive=0) page, and
     that score is returned in the API `score` field.

The public contract is unchanged: search_pages(query, page, page_size, category)
-> (list[dict], total). The result dicts gain a meaningful `score` (previously
the always-0.0 pages.score column) but keep every existing key, so the API JSON
shape is backward-compatible.
"""

from typing import List, Optional, Tuple

from .models import get_db
from .scoring import compute_score, days_since_scan, freshness_tier
from .search_parser import parse_query
from .stemmer import stem_word
from .search_parser import _quote  # FTS5 string-literal quoting (shared helper)

PAGE_SIZE = 10

# How many BM25-best candidates to pull before re-ranking in Python. The re-rank
# only reorders within this window, so it must comfortably exceed a normal page
# offset; deep paging widens it on demand, capped to bound memory.
CANDIDATE_LIMIT = 300
CANDIDATE_LIMIT_MAX = 2000


def _exclude_expression(terms: List[str]) -> str:
    """Build an FTS5 sub-expression matching any excluded term (stem OR exact).

    Used as the right-hand side of an FTS5 `NOT`, so a page is dropped if it
    contains any excluded term in either its surface or stemmed form. Synonyms
    are deliberately NOT expanded here — excluding "scam" should not also drop
    every page mentioning a scam synonym the user never typed.
    """
    parts: List[str] = []
    for term in terms:
        forms = [term]
        stemmed = stem_word(term)
        if stemmed != term:
            forms.append(stemmed)
        parts.extend(_quote(f) for f in forms)
    return " OR ".join(parts)


def _build_match(query: str) -> Tuple[Optional[str], List[str]]:
    """Return (fts_match_expression, raw_terms) or (None, terms) for no-op.

    The match expression folds in exclusions via FTS5's native `NOT` operator.
    Returns None for the expression when there is nothing positive to search
    (e.g. the input was only punctuation, or only an exclusion like "-scam").
    """
    parsed = parse_query(query)
    if not parsed.fts_query:
        return None, parsed.raw_terms

    match = parsed.fts_query
    if parsed.exclude_terms:
        excl = _exclude_expression(parsed.exclude_terms)
        if excl:
            # Parenthesise both sides: NOT binds tighter than OR in FTS5, so the
            # positive side must be grouped to avoid changing its meaning.
            match = f"({match}) NOT ({excl})"
    return match, parsed.raw_terms


def search_pages(
    query: str,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    category: Optional[str] = None,
    safe_mode: bool = True,
) -> Tuple[List[dict], int]:
    """Search alive pages, composite-ranked. Backward-compatible contract.

    Returns (results, total) where results is the requested page of dicts and
    total is the full match count. `raw_terms` for highlighting is also attached
    to each result dict under the `terms` key (additive; existing keys intact).

    safe_mode (default True) hides pages tagged 'illegal'/'nsfw'; 'unknown'
    (the default tag) and all other tags always pass. safe_mode=False disables
    the filter entirely.
    """
    match, raw_terms = _build_match(query)
    if not match:
        return [], 0

    offset = (page - 1) * page_size
    # Widen the candidate window so the requested page falls inside it, capped.
    window = min(max(CANDIDATE_LIMIT, offset + page_size), CANDIDATE_LIMIT_MAX)

    params: List = [match]
    where_extra = ""
    if category:
        where_extra += " AND p.category = ?"
    if safe_mode:
        # 'unknown' is the default and always passes; only illegal/nsfw are hidden.
        # Literal predicate (no bound param) so count/fetch param lists stay aligned.
        where_extra += (
            " AND (p.content_tag NOT IN ('illegal','nsfw') OR p.content_tag = 'unknown')"
        )

    # Alive (FRESH + stale) and archived (is_alive=0) pages are all selected; the
    # composite re-rank below sorts every alive result ahead of any archived one.
    sql = f"""
        SELECT p.id, p.url, p.title, p.description, p.category,
               p.lang, p.indexed_at, p.last_seen, p.is_alive,
               p.onion_score, p.is_active, p.last_scanned_at, p.content_tag,
               pages_fts.rank AS fts_rank
        FROM pages_fts
        JOIN pages p ON pages_fts.rowid = p.id
        WHERE pages_fts MATCH ?{where_extra}
        ORDER BY pages_fts.rank
        LIMIT ?
    """
    count_sql = f"""
        SELECT COUNT(*)
        FROM pages_fts
        JOIN pages p ON pages_fts.rowid = p.id
        WHERE pages_fts MATCH ?{where_extra}
    """

    fetch_params = list(params)
    count_params = list(params)
    if category:
        fetch_params.append(category)
        count_params.append(category)
    fetch_params.append(window)

    with get_db() as conn:
        rows = conn.execute(sql, fetch_params).fetchall()
        total = conn.execute(count_sql, count_params).fetchone()[0]

    # Composite re-rank within the BM25 candidate window.
    scored = []
    for r in rows:
        d = {
            "id": r["id"],
            "url": r["url"],
            "title": r["title"],
            "description": r["description"],
            "category": r["category"],
            "lang": r["lang"],
            "indexed_at": r["indexed_at"],
            "last_seen": r["last_seen"],
            "is_alive": r["is_alive"],
            "onion_score": r["onion_score"],
            "is_active": r["is_active"],
            "last_scanned_at": r["last_scanned_at"],
            "content_tag": r["content_tag"],
            "freshness_tier": freshness_tier(r["is_alive"], r["last_seen"]),
            "days_since_scan": days_since_scan(r["last_seen"]),
            "score": round(
                compute_score(r["fts_rank"], r["last_seen"], r["is_alive"]), 4
            ),
            "terms": raw_terms,
        }
        scored.append(d)

    # Alive pages (FRESH + stale) always rank above archived ones, regardless of
    # score; within each group, sort by composite score desc, breaking ties on
    # url for stability.
    scored.sort(key=lambda d: (0 if d["is_alive"] else 1, -d["score"], d["url"]))

    return scored[offset : offset + page_size], total
