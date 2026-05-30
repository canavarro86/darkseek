import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/db/darkseek.db")

# Ensure DB directory exists
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)


@dataclass
class Page:
    id: int
    url: str
    title: Optional[str]
    description: Optional[str]
    category: Optional[str]
    lang: Optional[str]
    indexed_at: str
    last_seen: str
    score: float
    is_alive: int


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Safe for concurrent reader + writer
    try:
        yield conn
    finally:
        conn.close()


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


def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
    if not os.path.exists(schema_path):
        return
    with get_db() as conn:
        with open(schema_path) as f:
            conn.executescript(f.read())
        conn.commit()


init_db()
