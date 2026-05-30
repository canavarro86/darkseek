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
                ORDER BY pages_fts.rank
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
                ORDER BY pages_fts.rank
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
    # Wrap each token in double quotes to treat them as phrases, not FTS operators
    tokens = query.split()
    return " ".join(f'"{t}"' for t in tokens if t)
