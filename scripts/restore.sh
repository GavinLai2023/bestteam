#!/usr/bin/env bash
# Restore a database backup (from ./scripts/backup-db.sh) and, optionally, a
# data-files archive (from ./scripts/backup-files.sh) into the running
# deployment. This is the procedure documented in docs/deployment.md
# ("Backup and restore"), as one command so the steps cannot be run out of
# order or with the chown forgotten.
#
# Usage:
#   ./scripts/restore.sh <backup.db> [files.tgz] [memory.db]
#
# What it does, in order:
#   1. stops the backend so nothing writes during the restore;
#   2. copies the database (and unpacks the files archive, if given) into the
#      data volume, then puts back the per-user memory database if one was
#      passed -- last, because the files archive carries its own raw-tar copy
#      of that file and this one is the copy to trust;
#   3. hands the restored files back to uid 1000 -- `docker cp` writes as root
#      and the backend runs unprivileged, so it could otherwise read but not
#      write (or migrate) them;
#   4. starts the backend and waits for /api/health to answer 200.
#
# Remember: a database backup is useless for email without the
# BESTTEAM_SECRETS_KEY that was in force when it was taken -- restore that
# into .env separately (see docs/deployment.md).
set -euo pipefail

DB_BACKUP="${1:?usage: restore.sh <backup.db> [files.tgz] [memory.db]}"
FILES_BACKUP="${2:-}"
MEM_BACKUP="${3:-}"
DATA_DIR=/app/ui/backend/data

[ -f "$DB_BACKUP" ] || { echo "no such file: $DB_BACKUP" >&2; exit 1; }
if [ -n "$FILES_BACKUP" ] && [ ! -f "$FILES_BACKUP" ]; then
  echo "no such file: $FILES_BACKUP" >&2; exit 1
fi
if [ -n "$MEM_BACKUP" ] && [ ! -f "$MEM_BACKUP" ]; then
  echo "no such file: $MEM_BACKUP" >&2; exit 1
fi

# Where a memory database goes is whatever BESTTEAM_MEMORY_DB says, so ask the
# image rather than assuming a filename -- and ask BEFORE stopping anything, so
# an unset variable cannot leave the backend down with a half-done restore. A
# one-off container reading the same .env the backend reads; `python` (not
# `uvicorn`), so the entrypoint does not run migrations.
MEM_PATH=""
if [ -n "$MEM_BACKUP" ]; then
  MEM_PATH=$(docker compose run --rm --no-deps backend \
    python -c "import os; print(os.environ.get('BESTTEAM_MEMORY_DB', '').strip())" | tr -d '\r')
  if [ -z "$MEM_PATH" ]; then
    echo "BESTTEAM_MEMORY_DB is unset: nothing would ever read the restored memory database." >&2
    echo "Set it in .env first (docs/deployment.md, \"Per-user memory\"), then re-run." >&2
    exit 1
  fi
fi

echo "Stopping the backend..."
docker compose stop backend

echo "Restoring the database from $DB_BACKUP..."
# The database's WAL/journal siblings belong to the old file; left behind,
# SQLite would replay them over the restored one.
docker compose run --rm --no-deps --user root backend \
  sh -c "rm -f $DATA_DIR/bestteam.db-wal $DATA_DIR/bestteam.db-shm $DATA_DIR/bestteam.db-journal"
docker compose cp "$DB_BACKUP" "backend:$DATA_DIR/bestteam.db"

if [ -n "$FILES_BACKUP" ]; then
  echo "Restoring data files from $FILES_BACKUP..."
  # Stage the archive INSIDE the data volume: `docker compose cp` writes into
  # the stopped backend container's own filesystem, and the one-off
  # `docker compose run` container below shares only the volume with it --
  # an archive copied to the container's /tmp would not be there.
  docker compose cp "$FILES_BACKUP" "backend:$DATA_DIR/.restore-files.tgz"
  docker compose run --rm --no-deps --user root backend \
    sh -c "tar xzf $DATA_DIR/.restore-files.tgz -C $DATA_DIR && rm -f $DATA_DIR/.restore-files.tgz"
fi

if [ -n "$MEM_BACKUP" ]; then
  echo "Restoring per-user memory from $MEM_BACKUP into $MEM_PATH..."
  # A journal file left by the copy the archive just unpacked belongs to that
  # copy; SQLite would replay it over the one being restored here.
  docker compose run --rm --no-deps --user root backend \
    sh -c "rm -f $MEM_PATH-wal $MEM_PATH-shm $MEM_PATH-journal"
  docker compose cp "$MEM_BACKUP" "backend:$MEM_PATH"
fi

echo "Handing the data directory back to uid 1000..."
docker compose run --rm --no-deps --user root backend chown -R 1000:1000 "$DATA_DIR"

echo "Starting the backend..."
docker compose start backend

echo "Waiting for /api/health..."
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8000/api/health > /dev/null 2>&1; then
    echo "Restore complete: the backend is healthy."
    echo "Now log in with a user that existed when the backup was taken."
    exit 0
  fi
  sleep 2
done
echo "The backend has not answered /api/health within 60s -- check 'docker compose logs backend'." >&2
exit 1
