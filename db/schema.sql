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
  enrichment_method TEXT DEFAULT 'pending',
  -- v2.0 community trust. fresh/rotten_votes are raw tallies; onion_score is the
  -- derived 1..5 rating (NULL until first vote). is_active/last_scanned_at run
  -- parallel to is_alive/last_seen (kept in lockstep by the crawler) and drive
  -- the cleanup GC. content_tag: safe|nsfw|scam|illegal|unknown (default unknown).
  fresh_votes     INTEGER DEFAULT 0,
  rotten_votes    INTEGER DEFAULT 0,
  onion_score     REAL DEFAULT NULL,
  last_scanned_at TIMESTAMP,
  is_active       BOOLEAN DEFAULT 1,
  content_tag     TEXT DEFAULT 'unknown',
  -- v3.0 self-feeding crawler. crawl_priority orders the refill queue (higher
  -- first); crawl_attempts counts consecutive failed crawls (drives the
  -- exponential next_crawl_at back-off); next_crawl_at is the earliest time a
  -- row is eligible for (re)crawl. NOTE: rows DISCOVERED as links are inserted
  -- with last_seen = NULL ("never crawled") so the tier-1 refill picks them up.
  crawl_priority  INTEGER DEFAULT 5,
  crawl_attempts  INTEGER DEFAULT 0,
  next_crawl_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

-- NOTE: the v3 pages-column indexes (idx_pages_next_crawl / _crawl_priority) are
-- intentionally NOT created here. This file is replayed before migrations run,
-- so on an existing DB those columns don't exist yet and CREATE INDEX would
-- abort init. They are created in api/models.py:_migrate_001(), AFTER the
-- guarded ALTER TABLE adds the columns. (Same rule as the v2 indexes.)

-- v3.0 schema-migration ledger (FIX 1). Numbered migrations in
-- api/models.py run once each, inside a transaction, recording their version
-- here so they are never replayed. CREATE-only lives in schema.sql; the
-- migrations themselves are guarded so this is a no-op on a fresh DB.
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  description TEXT
);

-- v3.0 per-endpoint rate limiting (FIX 3). key = sha256(IP + endpoint + date)
-- so it rotates daily and never stores a raw IP; window_start is the 60-second
-- bucket. Rows older than one hour are swept lazily on each request.
CREATE TABLE IF NOT EXISTS rate_limits (
  key          TEXT NOT NULL,
  window_start TIMESTAMP NOT NULL,
  count        INTEGER DEFAULT 1,
  PRIMARY KEY (key, window_start)
);

-- v3.0 persistent URL-shortener cache (FIX 4). Survives container restarts so a
-- repeated `shorten` for the same URL never re-hits TinyURL.
CREATE TABLE IF NOT EXISTS url_cache (
  original_url TEXT PRIMARY KEY,
  short_url    TEXT NOT NULL,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- v3.0 daily unique-visitor tracking (PART C). ip_hash = sha256(IP + daily
-- salt); the salt rotates every 24h so hashes from different days cannot be
-- correlated. No raw IP is ever stored. UNIQUE(date, ip_hash) makes the daily
-- distinct-visitor count a simple COUNT(*) per date.
CREATE TABLE IF NOT EXISTS daily_visitors (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  date     TEXT NOT NULL,
  ip_hash  TEXT NOT NULL,
  UNIQUE(date, ip_hash)
);
CREATE INDEX IF NOT EXISTS idx_daily_visitors_date ON daily_visitors(date);

-- v2.0 community trust: voting / reporting + a cross-worker PoW challenge store.
-- Only CREATE ... IF NOT EXISTS lives here (this file is re-run on every boot via
-- init_db()); the v2 *columns* on pages are added in api/models.py:migrate(),
-- guarded by _column_exists, because a bare ALTER would abort the replay.
CREATE TABLE IF NOT EXISTS votes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id    INTEGER NOT NULL,
  pow_hash   TEXT UNIQUE NOT NULL,
  vote_type  TEXT CHECK(vote_type IN ('fresh','rotten')) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (page_id) REFERENCES pages(id)
);

CREATE TABLE IF NOT EXISTS reports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id    INTEGER NOT NULL,
  reason     TEXT CHECK(reason IN ('scam','offline','illegal','spam')) NOT NULL,
  pow_hash   TEXT UNIQUE NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- PoW challenges are minted by GET /api/challenge and consumed by /api/vote and
-- /api/report. DB-backed (not in-process) so any gunicorn worker can validate a
-- challenge another worker issued. Expired rows are swept lazily on consume.
CREATE TABLE IF NOT EXISTS pow_challenges (
  challenge  TEXT PRIMARY KEY,
  page_id    INTEGER NOT NULL,
  expires_at TIMESTAMP NOT NULL
);

-- NOTE: the v2 column indexes (idx_pages_is_active / _content_tag / _onion_score)
-- are intentionally NOT created here. This file is replayed before migrate() runs,
-- so on a legacy DB the columns don't exist yet and CREATE INDEX ON pages(<col>)
-- would abort init. They are created idempotently in api/models.py:migrate(),
-- AFTER the guarded ALTER TABLE adds the columns.

-- NOTE: the UNIQUE index on content_hash is intentionally NOT created here.
-- This file is replayed on every startup against the live (de-duped or not) DB;
-- a UNIQUE index would abort init on the 1,215 pre-existing duplicates. It is
-- created idempotently by scripts/dedupe.py (and best-effort in migrate())
-- only AFTER duplicates are collapsed. See db migration runbook.
