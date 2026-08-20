#!/bin/sh
# Weekly SQLite/FTS5 maintenance. Run inside the `backup` container by crond,
# Sundays 04:00 UTC (see Dockerfile.backup), or manually:
#   docker compose exec backup /scripts/maintenance.sh
#
# Two cheap, non-exclusive-lock operations against the live WAL-mode DB:
#   1. FTS5 'optimize' merges the index's b-tree segments into one. Deletes
#      against an external-content FTS5 table (pages_fts here) leave tombstones
#      in old segments rather than reclaiming space immediately; with a churny
#      index (dead-page GC deletes daily) those accumulate and both bloat the
#      index and slow MATCH queries over time. 'optimize' is safe to run
#      repeatedly and does not need exclusive access.
#   2. ANALYZE refreshes the query planner's statistics (sqlite_stat1) so the
#      planner keeps picking good indexes as the table's size/shape drifts —
#      important on a table that has gone from a few thousand to 400k+ rows.
#
# Deliberately does NOT run VACUUM: VACUUM rewrites the entire DB file under an
# exclusive lock, which would stall the API and crawler for as long as it takes
# to copy a many-hundred-MB file, and temporarily needs up to ~2x the DB size in
# free disk space. Run it manually, during a maintenance window, if the DB
# grows enough that on-disk bloat from ordinary page churn (not FTS tombstones,
# which 'optimize' already handles) becomes a real space concern.
set -eu

DB="${DATABASE_PATH:-/opt/darkseek_db/darkseek.db}"

if [ ! -f "$DB" ]; then
  echo "[maintenance] ERROR: database not found at $DB" >&2
  exit 1
fi

echo "[maintenance] $(date -u +%FT%TZ) starting FTS5 optimize + ANALYZE"
sqlite3 "$DB" "INSERT INTO pages_fts(pages_fts) VALUES('optimize'); ANALYZE;"
echo "[maintenance] done"
