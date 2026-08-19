#!/usr/bin/env bash
# Back up everything on the backend's data volume EXCEPT the live SQLite
# database -- knowledge-base uploads (the original documents behind every
# collection), builder-session workspaces, and a memory store if the operator
# put one there. The database itself is backed up by ./scripts/backup-db.sh,
# which uses SQLite's online backup API; a raw tar of a database in use could
# capture a half-written page, so it is excluded here on purpose (with its
# -wal/-shm/-journal siblings). Run the two together from the same cron entry.
#
# Usage:
#   ./scripts/backup-files.sh [output-path]
#
# Defaults to ./backups/bestteam-files-<timestamp>.tgz if no path is given.
set -euo pipefail

OUT_PATH="${1:-backups/bestteam-files-$(date +%Y%m%d-%H%M%S).tgz}"
mkdir -p "$(dirname "$OUT_PATH")"

# Stream the archive straight out of the container: no temp file inside it,
# nothing to clean up. Uploads in flight are staged into their own version
# directory and only become live when the database says so, so a partial
# directory in this archive is harmless on restore. For the same reason GNU
# tar's exit status 1 ("file changed/removed as we read it") is not a
# failure here -- the archive is complete and usable -- only status 2 (a
# real error) is. Without that distinction a nightly run that overlaps an
# upload would log a spurious backup failure.
set +e
docker compose exec -T backend tar czf - \
  -C /app/ui/backend/data \
  --warning=no-file-changed \
  --exclude='bestteam.db' \
  --exclude='bestteam.db-*' \
  . > "$OUT_PATH"
status=$?
set -e
if [ "$status" -gt 1 ]; then
  echo "Backup failed: tar exited with status $status" >&2
  rm -f "$OUT_PATH"
  exit "$status"
fi

echo "Backed up data files to $OUT_PATH"
