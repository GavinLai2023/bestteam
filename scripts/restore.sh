#!/usr/bin/env bash
# Restore a database backup (from ./scripts/backup-db.sh) and, optionally, a
# data-files archive (from ./scripts/backup-files.sh) into the running
# deployment. This is the procedure documented in docs/deployment.md
# ("Backup and restore"), as one command so the steps cannot be run out of
# order or with the chown forgotten.
#
# Usage:
#   ./scripts/restore.sh <backup.db|backup.pgdump> [files.tgz] [memory.db]
#
# The backup is identified by its content -- a SQLite header or pg_dump's
# `PGDMP` -- never by its name, and it must match the engine the backend is
# configured for: a SQLite file copied into a Postgres deployment would be
# read by nothing, and "Restore complete" would still print.
#
# What it does, in order:
#   1. asks the backend image which database .env configures (a one-off
#      container, BEFORE anything is stopped) and refuses a mismatch;
#   2. stops the backend so nothing writes during the restore;
#   3. SQLite: copies the file into the data volume (dropping the old file's
#      WAL/journal siblings first). Postgres: restores into a fresh
#      `<database>_restore` and, only once that succeeded, drops the live
#      database and renames the new one into its place -- a failed
#      pg_restore leaves the live database untouched;
#   4. unpacks the files archive, if given, then puts back the per-user
#      memory database if one was passed -- last, because the files archive
#      carries its own raw-tar copy of that file and this one is the copy to
#      trust;
#   5. hands the restored files back to uid 1000 -- `docker cp` writes as
#      root and the backend runs unprivileged, so it could otherwise read but
#      not write (or migrate) them;
#   6. starts the backend and waits for /api/health to answer 200.
#
# Remember: a database backup is useless for email without the
# BESTTEAM_SECRETS_KEY that was in force when it was taken -- restore that
# into .env separately (see docs/deployment.md).
set -euo pipefail

DB_BACKUP="${1:?usage: restore.sh <backup.db|backup.pgdump> [files.tgz] [memory.db]}"
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

# What kind of backup is this? The first bytes say; the name may not.
if [ "$(head -c 5 "$DB_BACKUP")" = "PGDMP" ]; then
  FORMAT=postgresql
elif [ "$(head -c 15 "$DB_BACKUP")" = "SQLite format 3" ]; then
  FORMAT=sqlite
else
  echo "$DB_BACKUP is neither a SQLite database nor a pg_dump archive (not a backup-db.sh file)" >&2
  exit 1
fi

# Which database does .env configure, and where does a memory database go?
# Ask the image rather than assuming -- and ask BEFORE stopping anything, so a
# mismatch cannot leave the backend down with a half-done restore. A one-off
# container reading the same .env the backend reads; `python` (not `uvicorn`),
# so the entrypoint does not run migrations. Six lines, never the password.
PROBE=$(docker compose run --rm --no-deps backend python -c "
import os
from sqlalchemy.engine import make_url
from ui.backend.db.database import resolve_database_url, sqlite_path_of
raw = resolve_database_url(os.environ)
url = make_url(raw)
print(url.get_backend_name())
print(url.host or '')
print(url.username or '')
print(url.database or '')
print(sqlite_path_of(raw) or '')
print(os.environ.get('BESTTEAM_MEMORY_DB', '').strip())
" | tr -d '\r')
# Command substitution drops trailing empty lines (a server database has no
# SQLite path; memory may be unset), so index with defaults rather than
# `read` line by line, which would hit EOF under `set -e`.
mapfile -t PROBE_LINES <<< "$PROBE"
ENGINE="${PROBE_LINES[0]:-}"; DB_HOST="${PROBE_LINES[1]:-}"; DB_USER="${PROBE_LINES[2]:-}"
DB_NAME="${PROBE_LINES[3]:-}"; SQLITE_PATH="${PROBE_LINES[4]:-}"; MEM_PATH="${PROBE_LINES[5]:-}"

if [ "$FORMAT" != "$ENGINE" ]; then
  echo "$DB_BACKUP is a $FORMAT backup, but the backend is configured for $ENGINE" >&2
  echo "(BESTTEAM_DATABASE_URL / BESTTEAM_DB_PATH in .env); nothing would read it after the restore." >&2
  exit 1
fi
if [ "$ENGINE" = "postgresql" ] && [ "$DB_HOST" != "db" ]; then
  echo "The backend uses postgresql host '$DB_HOST'; this script restores into the compose 'db' service only." >&2
  exit 1
fi
if [ "$ENGINE" = "sqlite" ] && [ -z "$SQLITE_PATH" ]; then
  echo "The backend is configured for an in-memory SQLite database; there is nothing to restore into." >&2
  exit 1
fi
if [ -n "$MEM_BACKUP" ] && [ -z "$MEM_PATH" ]; then
  echo "BESTTEAM_MEMORY_DB is unset: nothing would ever read the restored memory database." >&2
  echo "Set it in .env first (docs/deployment.md, \"Per-user memory\"), then re-run." >&2
  exit 1
fi

echo "Stopping the backend..."
docker compose stop backend

if [ "$ENGINE" = "sqlite" ]; then
  echo "Restoring the database from $DB_BACKUP into $SQLITE_PATH..."
  # The database's WAL/journal siblings belong to the old file; left behind,
  # SQLite would replay them over the restored one.
  docker compose run --rm --no-deps --user root backend \
    sh -c "rm -f $SQLITE_PATH-wal $SQLITE_PATH-shm $SQLITE_PATH-journal"
  docker compose cp "$DB_BACKUP" "backend:$SQLITE_PATH"
else
  # Maintenance statements run from the `postgres` database, never from
  # inside the one being dropped or renamed. POSTGRES_USER is a superuser in
  # the official image, and the Unix socket inside the container needs no
  # password.
  psql_admin() {
    docker compose exec -T db psql -v ON_ERROR_STOP=1 -q -U "$DB_USER" -d postgres "$@"
  }
  TMP_DB="${DB_NAME}_restore"
  echo "Restoring the database from $DB_BACKUP into a fresh $TMP_DB..."
  psql_admin -c "DROP DATABASE IF EXISTS \"$TMP_DB\" WITH (FORCE)" \
             -c "CREATE DATABASE \"$TMP_DB\" OWNER \"$DB_USER\""
  if ! docker compose exec -T db pg_restore -U "$DB_USER" -d "$TMP_DB" \
         --no-owner --no-privileges --exit-on-error < "$DB_BACKUP"; then
    echo "pg_restore failed; the live database $DB_NAME is untouched." >&2
    echo "Bring the backend back with 'docker compose up -d backend', fix the cause, then re-run." >&2
    exit 1
  fi
  echo "Swapping $TMP_DB in as $DB_NAME..."
  psql_admin -c "DROP DATABASE \"$DB_NAME\" WITH (FORCE)" \
             -c "ALTER DATABASE \"$TMP_DB\" RENAME TO \"$DB_NAME\""
fi

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

echo "Bringing the backend back..."
# `up -d`, not `start`: the probe above read .env, and a container created
# before an edit to .env still carries the old environment -- `start` would
# bring back a backend pointed at the other engine and report this restore
# complete. `up -d` recreates it when its configuration changed and starts it
# otherwise, waiting for db's health check on the way.
docker compose up -d backend

echo "Waiting for /api/health..."
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/api/health > /dev/null 2>&1; then
    echo "Restore complete: the backend is healthy."
    echo "Now log in with a user that existed when the backup was taken."
    exit 0
  fi
  sleep 2
done
echo "The backend has not answered /api/health within 120s -- check 'docker compose logs backend'." >&2
exit 1
