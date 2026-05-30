import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/db/darkseek.db")

os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

RECRAWL_DAYS = 7


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def should_recrawl(url: str) -> bool:
    """Return True if URL is new or last_seen is older than RECRAWL_DAYS."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_seen FROM pages WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return True
        try:
            last_seen = datetime.fromisoformat(row["last_seen"])
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last_seen).days >= RECRAWL_DAYS
        except Exception:
            return True


def upsert_page(
    url: str,
    title: str,
    description: str,
    category: str,
    lang: str,
    score: float = 0.0,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO pages (url, title, description, category, lang, score, is_alive, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                title       = excluded.title,
                description = excluded.description,
                category    = excluded.category,
                lang        = excluded.lang,
                score       = excluded.score,
                is_alive    = 1,
                last_seen   = CURRENT_TIMESTAMP
            """,
            (url, title, description, category, lang, score),
        )
        conn.commit()


def mark_dead(url: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE pages SET is_alive = 0 WHERE url = ?", (url,))
        conn.commit()
