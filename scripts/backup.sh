#!/usr/bin/env bash
# Copy coach.db to <backup_dir>/coach-YYYYMMDD-HHMMSS.db, keep the newest 30.
# Usage: scripts/backup.sh [db_path] [backup_dir]
set -euo pipefail
DB="${1:-${COACH_DB:-./coach.db}}"
DIR="${2:-./backups}"
mkdir -p "$DIR"
OUT="$DIR/coach-$(date +%Y%m%d-%H%M%S).db"
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" ".backup '$OUT'"        # consistent even while the app is writing
else
  cp "$DB" "$OUT"
fi
echo "wrote $OUT"
ls -1t "$DIR"/coach-*.db | tail -n +31 | xargs -r rm --
