CREATE TABLE IF NOT EXISTS pages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  url          TEXT UNIQUE NOT NULL,
  title        TEXT,
  description  TEXT,
  category     TEXT CHECK(category IN ('forum','market','news','wiki','service','other')),
  lang         TEXT,
  indexed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_seen    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  score        REAL DEFAULT 0.0,
  is_alive     INTEGER DEFAULT 1,
  content_hash TEXT,
  page_type    TEXT DEFAULT 'other',
  fail_count   INTEGER DEFAULT 0,
  -- Why a row holds its current category/lang: 'ai' (Claude), 'heuristic'
  -- (local fallback), or 'pending' (legacy/degraded write awaiting enrichment).
  -- Supersedes NULL-as-state so the backfill job can target rows precisely.
  enrichment_method TEXT DEFAULT 'pending'
);

-- Negative cache for unreachable .onion services. Keeps the crawler from
-- spending ~30s per cycle re-resolving hosts that no longer exist.
CREATE TABLE IF NOT EXISTS dead_onions (
  url             TEXT PRIMARY KEY,
  first_failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_failed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  fail_count      INTEGER DEFAULT 1
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
  title,
  description,
  content=pages,
  content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
  INSERT INTO pages_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, title, description) VALUES('delete', old.id, old.title, old.description);
END;

CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, title, description) VALUES('delete', old.id, old.title, old.description);
  INSERT INTO pages_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;

CREATE INDEX IF NOT EXISTS idx_pages_is_alive ON pages(is_alive);
CREATE INDEX IF NOT EXISTS idx_pages_last_seen ON pages(last_seen);
CREATE INDEX IF NOT EXISTS idx_pages_category ON pages(category);
CREATE INDEX IF NOT EXISTS idx_pages_enrichment_method ON pages(enrichment_method);

-- NOTE: the UNIQUE index on content_hash is intentionally NOT created here.
-- This file is replayed on every startup against the live (de-duped or not) DB;
-- a UNIQUE index would abort init on the 1,215 pre-existing duplicates. It is
-- created idempotently by scripts/dedupe.py (and best-effort in migrate())
-- only AFTER duplicates are collapsed. See db migration runbook.
