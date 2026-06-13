#!/bin/sh
# Daily SQLite backup (FIX 7). Run inside the `backup` container by crond at
# 03:00 UTC, or manually: `docker compose exec backup /scripts/backup.sh`.
#
# Uses the SQLite online-backup API (.backup) so the snapshot is consistent even
# while the crawler/API are writing (WAL mode). Keeps the most recent N backups.
set -eu

DB="${DATABASE_PATH:-/opt/darkseek_db/darkseek.db}"
BACKUP_DIR="${BACKUP_DIR:-/opt/darkseek_backups}"
KEEP="${BACKUP_KEEP:-7}"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB" ]; then
  echo "[backup] ERROR: database not found at $DB" >&2
  exit 1
fi

TS="$(date -u +%Y%m%d_%H%M%S)"
DEST="$BACKUP_DIR/darkseek_${TS}.db"

# Consistent online snapshot (folds in the WAL); never copies a torn file.
sqlite3 "$DB" ".backup '$DEST'"
echo "[backup] wrote $DEST"

# Retention: keep only the newest $KEEP backups, delete the rest.
ls -1t "$BACKUP_DIR"/darkseek_*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
  echo "[backup] pruned $old"
done

echo "[backup] done; retaining newest $KEEP"
