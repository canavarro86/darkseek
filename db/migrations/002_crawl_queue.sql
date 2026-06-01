-- 002_crawl_queue.sql
-- User-submitted URL queue, consumed by the crawler at the top of each cycle.
-- Idempotent: safe to replay on every startup. The API applies the same DDL
-- inline in api/models.migrate(); this file is the canonical migration record.

CREATE TABLE IF NOT EXISTS crawl_queue (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  url        TEXT UNIQUE NOT NULL,
  added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  priority   INTEGER DEFAULT 0,
  status     TEXT DEFAULT 'pending' CHECK(status IN ('pending','processing','done','failed')),
  source     TEXT DEFAULT 'user'
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status, priority DESC, added_at);
