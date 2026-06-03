#!/usr/bin/env python3
"""v2.0 schema migration — idempotent, safe to re-run on the live DB.

Adds the community voting / reporting columns + tables. Mirrors the set that
api.models.migrate() also converges at import time; running this standalone is
for explicit ops use (a maintenance window) and to verify state. Prints
"Migration complete" on first run, "Already up to date" thereafter.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from api.models import DATABASE_PATH  # single source of truth for the path

# (column, declaration). onion_score stays NULL until a page receives votes.
NEW_COLUMNS = [
    ("fresh_votes",     "INTEGER DEFAULT 0"),
    ("rotten_votes",    "INTEGER DEFAULT 0"),
    ("onion_score",     "REAL DEFAULT NULL"),
    ("last_scanned_at", "TIMESTAMP"),
    ("is_active",       "BOOLEAN DEFAULT 1"),
    ("content_tag",     "TEXT DEFAULT 'unknown'"),
]

TABLES = [
    """CREATE TABLE IF NOT EXISTS votes (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      page_id    INTEGER NOT NULL,
      pow_hash   TEXT UNIQUE NOT NULL,
      vote_type  TEXT CHECK(vote_type IN ('fresh','rotten')) NOT NULL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (page_id) REFERENCES pages(id)
    )""",
    """CREATE TABLE IF NOT EXISTS reports (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      page_id    INTEGER NOT NULL,
      reason     TEXT CHECK(reason IN ('scam','offline','illegal','spam')) NOT NULL,
      pow_hash   TEXT UNIQUE NOT NULL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    # Cross-worker PoW challenge store (replaces a per-process in-memory dict
    # that cannot work under gunicorn --workers 2). TTL enforced on read.
    """CREATE TABLE IF NOT EXISTS pow_challenges (
      challenge  TEXT PRIMARY KEY,
      page_id    INTEGER NOT NULL,
      expires_at TIMESTAMP NOT NULL
    )""",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pages_is_active   ON pages(is_active)",
    "CREATE INDEX IF NOT EXISTS idx_pages_content_tag ON pages(content_tag)",
    "CREATE INDEX IF NOT EXISTS idx_pages_onion_score ON pages(onion_score)",
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def main() -> None:
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        changed = False
        for name, decl in NEW_COLUMNS:
            if not _column_exists(conn, "pages", name):
                conn.execute(f"ALTER TABLE pages ADD COLUMN {name} {decl}")
                changed = True
        # CREATE IF NOT EXISTS is a no-op when already present; the column check
        # above is what distinguishes a first run from a re-run.
        for ddl in TABLES + INDEXES:
            conn.execute(ddl)
        conn.commit()
        print("Migration complete" if changed else "Already up to date")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
