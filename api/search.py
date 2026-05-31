import re
from typing import List, Optional, Tuple

import Stemmer

from .models import get_db

PAGE_SIZE = 10

# Snowball stemmers for the two languages we index meaningfully.
_stemmers = {
    "ru": Stemmer.Stemmer("russian"),
    "en": Stemmer.Stemmer("english"),
}

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def _stem_query(query: str) -> str:
    """Stem query tokens: Russian for Cyrillic tokens, English otherwise.

    Best-effort — any PyStemmer failure returns the original query unchanged so
    search never breaks because of the stemmer.
    """
    try:
        out = []
        for token in query.split():
            stemmer = _stemmers["ru"] if _CYRILLIC_RE.search(token) else _stemmers["en"]
            out.append(stemmer.stemWord(token))
        return " ".join(out)
    except Exception:
        return query


def search_pages(
    query: str,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    category: Optional[str] = None,
) -> Tuple[List[dict], int]:
    offset = (page - 1) * page_size

    # Build an FTS5 MATCH expression that searches both the stemmed and the
    # original token, so thin Russian descriptions still match on exact tokens.
    safe_query = _build_fts_query(query)
    # After escaping the query can be empty (e.g. input was only punctuation);
    # MATCH '' raises, so short-circuit to an empty result set.
    if not safe_query:
        return [], 0

    with get_db() as conn:
        if category:
            rows = conn.execute(
                """
                SELECT p.id, p.url, p.title, p.description, p.category,
                       p.lang, p.score, p.indexed_at, p.last_seen,
                       pages_fts.rank AS fts_rank
                FROM pages_fts
                JOIN pages p ON pages_fts.rowid = p.id
                WHERE pages_fts MATCH ? AND p.category = ? AND p.is_alive = 1
                ORDER BY (pages_fts.rank * 0.6 + (julianday(p.last_seen) - julianday('2024-01-01')) * 0.4) DESC
                LIMIT ? OFFSET ?
                """,
                (safe_query, category, page_size, offset),
            ).fetchall()
            total = conn.execute(
                """
                SELECT COUNT(*)
                FROM pages_fts
                JOIN pages p ON pages_fts.rowid = p.id
                WHERE pages_fts MATCH ? AND p.category = ? AND p.is_alive = 1
                """,
                (safe_query, category),
            ).fetchone()[0]
        else:
            rows = conn.execute(
                """
                SELECT p.id, p.url, p.title, p.description, p.category,
                       p.lang, p.score, p.indexed_at, p.last_seen,
                       pages_fts.rank AS fts_rank
                FROM pages_fts
                JOIN pages p ON pages_fts.rowid = p.id
                WHERE pages_fts MATCH ? AND p.is_alive = 1
                ORDER BY (pages_fts.rank * 0.6 + (julianday(p.last_seen) - julianday('2024-01-01')) * 0.4) DESC
                LIMIT ? OFFSET ?
                """,
                (safe_query, page_size, offset),
            ).fetchall()
            total = conn.execute(
                """
                SELECT COUNT(*)
                FROM pages_fts
                JOIN pages p ON pages_fts.rowid = p.id
                WHERE pages_fts MATCH ? AND p.is_alive = 1
                """,
                (safe_query,),
            ).fetchone()[0]

    return [dict(r) for r in rows], total


def _build_fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression matching both stemmed and exact tokens.

    For each token, stem it; if the stem differs from the original, emit
    '"stem" OR "original"' so a query like "форум" (stem "фор") still matches
    pages that only store the exact token "форум". Tokens are joined by spaces
    (FTS5 AND). Any failure falls back to plain _escape_fts() behaviour.
    """
    try:
        token_exprs = []
        for token in query.split():
            if not token:
                continue
            stemmer = _stemmers["ru"] if _CYRILLIC_RE.search(token) else _stemmers["en"]
            stemmed = stemmer.stemWord(token)
            orig_safe = token.replace('"', '""')
            if stemmed != token:
                stem_safe = stemmed.replace('"', '""')
                token_exprs.append(f'"{stem_safe}" OR "{orig_safe}"')
            else:
                token_exprs.append(f'"{orig_safe}"')
        return " ".join(token_exprs)
    except Exception:
        return _escape_fts(query)


def _escape_fts(query: str) -> str:
    """Turn raw user input into a safe FTS5 MATCH expression.

    Every token is wrapped in double quotes so FTS operators (AND/OR/NEAR/*/-)
    are treated as literal text. Internal double quotes are doubled per the FTS5
    string-literal grammar, otherwise a token like `a"b` would break out of the
    quoting and inject operators. Returns "" for empty/whitespace-only input;
    callers must treat "" as "no query" rather than passing it to MATCH.
    """
    tokens = query.split()
    escaped = []
    for token in tokens:
        if not token:
            continue
        safe = token.replace('"', '""')
        escaped.append(f'"{safe}"')
    return " ".join(escaped)
