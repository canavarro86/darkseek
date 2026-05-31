from typing import List, Optional, Tuple

from .models import get_db

PAGE_SIZE = 10


def search_pages(
    query: str,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    category: Optional[str] = None,
) -> Tuple[List[dict], int]:
    offset = (page - 1) * page_size

    # Escape FTS5 special characters so raw user input doesn't break the query
    safe_query = _escape_fts(query)
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
