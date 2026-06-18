#!/usr/bin/env bash
# Back up the per-customer SQLite database from the running backend container.
#
# Usage:
#   ./scripts/backup-db.sh [output-path]
#
# Defaults to ./backups/bestteam-<timestamp>.db if no path is given.
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
