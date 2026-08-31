#!/usr/bin/env bash
# Back up the per-customer SQLite database from the running backend container,
# plus the per-user memory database when the deployment has one.
#
# Usage:
#   ./scripts/backup-db.sh [output-path]
#
# Defaults to ./backups/bestteam-<timestamp>.db if no path is given. A memory
# database, when enabled, is written beside it as <output-path>-memory.db.
set -euo pipefail

OUT_PATH="${1:-backups/bestteam-$(date +%Y%m%d-%H%M%S).db}"
mkdir -p "$(dirname "$OUT_PATH")"

# Use sqlite3's online backup API (Python stdlib) -- safe against a live
# database, unlike a raw file copy which could race with an in-progress
# write. The sqlite3 CLI binary isn't installed in the python:3.11-slim
# base image, so this uses the Python module instead.
docker compose exec -T backend python -c "
import sqlite3
src = sqlite3.connect('/app/ui/backend/data/bestteam.db')
dst = sqlite3.connect('/tmp/bestteam-backup.db')
src.backup(dst)
dst.close()
src.close()
"
docker compose cp backend:/tmp/bestteam-backup.db "$OUT_PATH"
docker compose exec -T backend rm -f /tmp/bestteam-backup.db

echo "Backed up to $OUT_PATH"

# Per-user memory (BESTTEAM_MEMORY_DB) is a second, separate SQLite database,
# present only when the operator enabled it. It needs the same online-backup
# treatment -- a raw tar of it in backup-files.sh can catch a half-written
# page -- so take it here rather than leaving it to that archive. The path
# comes from the *running container's* environment: a .env edit that was never
# applied with `up -d --force-recreate` is not what the backend is using.
MEM_PATH=$(docker compose exec -T backend \
  python -c "import os; print(os.environ.get('BESTTEAM_MEMORY_DB', '').strip())" | tr -d '\r')

if [ -z "$MEM_PATH" ]; then
  echo "Per-user memory is not enabled (BESTTEAM_MEMORY_DB unset); nothing further to back up."
elif ! docker compose exec -T backend test -f "$MEM_PATH"; then
  # Enabled but not yet written to: backing up a missing source would create an
  # empty database and hand it over as if it were a backup.
  echo "Per-user memory is enabled but $MEM_PATH does not exist yet; skipping it."
else
  MEM_OUT="${OUT_PATH%.db}-memory.db"
  docker compose exec -T backend python -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect('/tmp/memory-backup.db')
src.backup(dst)
dst.close()
src.close()
" "$MEM_PATH"
  docker compose cp backend:/tmp/memory-backup.db "$MEM_OUT"
  docker compose exec -T backend rm -f /tmp/memory-backup.db
  echo "Backed up per-user memory to $MEM_OUT"
fi
