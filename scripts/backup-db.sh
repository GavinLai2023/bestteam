#!/usr/bin/env bash
# Back up the deployment database from the running backend container, plus the
# per-user memory database when the deployment has one.
#
# Usage:
#   ./scripts/backup-db.sh [output-path]
#
# The script asks the RUNNING backend which database it uses and follows it:
# a SQLite file is copied through SQLite's online backup API; the compose `db`
# service (Postgres) is dumped with `pg_dump --format=custom`. It decides the
# extension itself, so a file's name always says what is inside: the argument
# minus any trailing `.db` / `.pgdump` is the stem, and the output is
# `<stem>.db` (SQLite) or `<stem>.pgdump` (Postgres). Default stem:
# ./backups/bestteam-<timestamp>. A memory database, when enabled, is written
# beside it as `<stem>-memory.db`. A cron line written in the SQLite days
# (`... bestteam-$(date +\%F).db`) therefore keeps working after a cutover to
# Postgres and simply starts producing `.pgdump` files.
set -euo pipefail

ARG="${1:-backups/bestteam-$(date +%Y%m%d-%H%M%S)}"
case "$ARG" in
  *.db)     STEM="${ARG%.db}" ;;
  *.pgdump) STEM="${ARG%.pgdump}" ;;
  *)        STEM="$ARG" ;;
esac
mkdir -p "$(dirname "$STEM")"

# Which database is the backend actually using? Ask the running container,
# never .env: an edit never applied with `up -d --force-recreate` is not what
# the backend reads. Six lines, never the password.
PROBE=$(docker compose exec -T backend python -c "
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

case "$ENGINE" in
  sqlite)
    if [ -z "$SQLITE_PATH" ]; then
      echo "The backend uses an in-memory SQLite database; there is nothing to back up." >&2
      exit 1
    fi
    OUT_PATH="$STEM.db"
    # Use sqlite3's online backup API (Python stdlib) -- safe against a live
    # database, unlike a raw file copy which could race with an in-progress
    # write. The sqlite3 CLI binary isn't installed in the python:3.11-slim
    # base image, so this uses the Python module instead.
    docker compose exec -T backend python -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect('/tmp/bestteam-backup.db')
src.backup(dst)
dst.close()
src.close()
" "$SQLITE_PATH"
    docker compose cp backend:/tmp/bestteam-backup.db "$OUT_PATH"
    docker compose exec -T backend rm -f /tmp/bestteam-backup.db
    echo "Backed up sqlite database $SQLITE_PATH to $OUT_PATH"
    ;;
  postgresql)
    # The dump is taken by exec-ing into the compose `db` service; a backend
    # pointed at any other host would be backed up from the wrong server and
    # reported as a success.
    if [ "$DB_HOST" != "db" ]; then
      echo "The backend uses postgresql host '$DB_HOST'; this script backs up the compose 'db' service only." >&2
      exit 1
    fi
    OUT_PATH="$STEM.pgdump"
    # pg_dump takes a consistent snapshot, so the backend keeps running. The
    # archive streams straight out of the container -- no temp file inside
    # it. The Unix socket inside the container is `trust`: no password.
    set +e
    docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom > "$OUT_PATH"
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
      rm -f "$OUT_PATH"
      echo "Backup failed: pg_dump exited with status $status" >&2
      exit "$status"
    fi
    echo "Backed up postgresql database $DB_NAME (compose service db) to $OUT_PATH"
    ;;
  *)
    echo "The backend uses the '$ENGINE' engine; this script knows sqlite and postgresql." >&2
    exit 1
    ;;
esac

# Per-user memory (BESTTEAM_MEMORY_DB) is a second, separate SQLite database,
# present only when the operator enabled it -- whichever engine the deployment
# database uses. It needs the same online-backup treatment (a raw tar of it in
# backup-files.sh can catch a half-written page), so take it here rather than
# leaving it to that archive. The path came from the running container's
# environment with the probe above.
if [ -z "$MEM_PATH" ]; then
  echo "Per-user memory is not enabled (BESTTEAM_MEMORY_DB unset); nothing further to back up."
elif ! docker compose exec -T backend test -f "$MEM_PATH"; then
  # Enabled but not yet written to: backing up a missing source would create an
  # empty database and hand it over as if it were a backup.
  echo "Per-user memory is enabled but $MEM_PATH does not exist yet; skipping it."
else
  MEM_OUT="$STEM-memory.db"
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
