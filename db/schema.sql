@'
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
  is_alive     INTEGER DEFAULT 1
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
'@ | Set-Content db\schema.sql -Encoding UTF8