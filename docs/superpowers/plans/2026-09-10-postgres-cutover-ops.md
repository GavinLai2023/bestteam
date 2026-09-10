# Postgres Cutover (ops half) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship everything the beta VPS needs to move from its SQLite file to a Postgres 16 server in the same compose stack — the service, engine-following backup/restore scripts, and a runbook with expected outputs — before customer data arrives.

**Architecture:** `docker-compose.yml` gains a `db` service (`postgres:16`, own volume, no published port) that the backend depends on but does not use until `BESTTEAM_DATABASE_URL` names it. `backup-db.sh` and `restore.sh` ask the backend which engine it uses and branch on that; `restore.sh` identifies a backup by its bytes and refuses a mismatch. The cutover is one `.env` line plus `admin migrate-db`; the rollback is blanking that line.

**Tech Stack:** Docker Compose v2, `postgres:16` (official image; `pg_dump`/`pg_restore`/`psql` inside it), bash (`set -euo pipefail`, `mapfile`), the existing `admin migrate-db` / `check-env` / `check-orphans.sh`.

**Spec:** `docs/superpowers/specs/2026-09-10-postgres-cutover-ops-design.md` (lands with PR #127).

## Global Constraints

- Prose in British English; code comments in English; Chinese only in `docs/deployment.zh-CN.md`, natively written.
- Never print a `.env` value; the probe snippet in the scripts prints engine, host, user, database, SQLite path and memory path — never the password.
- Never point any command at `ui/backend/data/bestteam.db`; nothing in this plan opens the development database.
- No Docker and no shellcheck on the workstation: every script change gets `bash -n` plus the stubbed-`docker` harness (`scratchpad/prB/harness.sh`, reproduced in Task 2); the Docker paths are verified on the VPS by the runbook.
- `backup-db.sh` names the output `<stem>.db` (SQLite) or `<stem>.pgdump` (Postgres); the memory copy stays `<stem>-memory.db`. Both scripts support a Postgres backend only when its URL host is `db`.
- The `db` service is `postgres:16` with `POSTGRES_INITDB_ARGS: "--locale=C --encoding=UTF8"` (the CI lane's), no `ports:`, `deploy.resources.limits.memory: 512m`, `shm_size: 128m`.
- Commit messages end with the session's `Co-Authored-By` / `Claude-Session` trailers; nothing is merged by this plan.

---

### Task 1: The `db` service and `.env.example`

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example:47-52`

**Interfaces:**
- Produces: compose service `db` (host `db`, port 5432, role `bestteam`, database `bestteam`); volume `bestteam_pg`; `.env` key `POSTGRES_PASSWORD`; the URL shape `postgresql+psycopg://bestteam:<POSTGRES_PASSWORD>@db:5432/bestteam` that Tasks 2–5 assume.

- [ ] **Step 1: Add the service, the dependency and the volume**

Insert before `  frontend:` in `docker-compose.yml`:

```yaml
  db:
    # The version the CI lane tests against. No `ports:` -- only the compose
    # network reaches it, as host `db`. It gets its own three variables and
    # never `.env`, which would hand every backend secret to the database.
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: bestteam
      POSTGRES_DB: bestteam
      # Interpolated from .env like the frontend's VITE_* below. `:?` makes
      # compose refuse to parse the file without it -- deploy.sh then stops at
      # its step 4, before anything is rebuilt -- rather than let the image
      # initialise a server with no password. Read once, when the volume is
      # initialised; changing it later does not change the role's password.
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
      # C collation, like SQLite's byte order and the CI lane: the
      # order-sensitive endpoints the suite verifies there behave the same
      # on this server.
      POSTGRES_INITDB_ARGS: "--locale=C --encoding=UTF8"
    volumes:
      - bestteam_pg:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bestteam -d bestteam"]
      interval: 5s
      timeout: 5s
      retries: 10
    # The official image's documented floor; Docker's default /dev/shm is 64m.
    shm_size: 128m
    # Enough for this data volume (Postgres sits at one or two hundred MB
    # resident); with the backend's 2g the limits stay inside a 4 GB host
    # that has no swap.
    deploy:
      resources:
        limits:
          memory: 512m
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

```

In `backend`, after `env_file: .env`:

```yaml
    # Wait for the server's health check before starting. The backend only
    # USES it once BESTTEAM_DATABASE_URL names it (docs/deployment.md, 3).
    depends_on:
      db:
        condition: service_healthy
```

At the bottom:

```yaml
volumes:
  bestteam_data:
  bestteam_pg:
```

- [ ] **Step 2: Parse it**

Run: `./.venv/Scripts/python.exe -c "import yaml; d = yaml.safe_load(open('docker-compose.yml')); print(sorted(d['services']), sorted(d['volumes']))"`
Expected: `['backend', 'db', 'frontend'] ['bestteam_data', 'bestteam_pg']`

- [ ] **Step 3: `.env.example`**

Replace the block from `# The deployment database.` through `BESTTEAM_DATABASE_URL=` with:

```
# The deployment database. Leave unset for the default SQLite file on the data
# volume (ui/backend/data/bestteam.db; BESTTEAM_DB_PATH overrides the path).
# A server database is selected with a SQLAlchemy URL. docker-compose.yml
# ships a Postgres 16 service, `db`, whose URL is
#   BESTTEAM_DATABASE_URL=postgresql+psycopg://bestteam:<POSTGRES_PASSWORD>@db:5432/bestteam
# Leave it empty until the copy in docs/deployment.md section 3 ("Moving to
# Postgres") has run; `check-env` reports which database is in use.
BESTTEAM_DATABASE_URL=

# Password for the `db` service's `bestteam` role. REQUIRED once the compose
# file has that service -- the stack refuses to start without it. Hex only
# (`openssl rand -hex 24`), so the same value pastes into the URL above with
# nothing to escape. Read once, when the database volume is initialised:
# changing it later does not change the role's password.
POSTGRES_PASSWORD=
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "ops(compose): a postgres:16 db service the backend waits for but does not use yet"
```

### Task 2: `backup-db.sh` follows the running backend

**Files:**
- Modify: `scripts/backup-db.sh` (rewrite)
- Harness (not committed): `<scratchpad>/prB/harness.sh`

**Interfaces:**
- Consumes: the `db` service (Task 1); `ui.backend.db.database.resolve_database_url` / `sqlite_path_of`.
- Produces: `<stem>.db` or `<stem>.pgdump` (+ `<stem>-memory.db`); a last stdout line `Backed up sqlite database <path> to <file>` or `Backed up postgresql database <name> (compose service db) to <file>`; exit 1 on a non-`db` host, the `pg_dump` status on a failed dump (partial file removed).

- [ ] **Step 1: Replace the script with**

```bash
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
```

- [ ] **Step 2: Syntax and the harness**

The harness stubs `docker`, `curl` and `sleep` with exported functions: the probe answers six lines according to `MODE`/`HOST`/`MEMDB`, `pg_dump` prints a `PGDMP` header (or fails with `PG_DUMP_FAILS=1`), `cp backend:/tmp/...` creates the destination, `test -f` follows `MEM_EXISTS`, `pg_restore` follows `PG_RESTORE_FAILS`; everything is logged to `work/docker.log`. Cases: SQLite writes `<stem>.db` and `<stem>-memory.db` at the probed path; Postgres turns `x.db`, `x.pgdump` and `x` into `x.pgdump` with `PGDMP` inside and `pg_dump -U bestteam -d bestteam --format=custom` in the log; default stem under `./backups`; other host refused before any dump; a failed dump leaves no file and exits with its status; memory enabled-but-absent is skipped.

Run: `bash -n scripts/backup-db.sh && bash <scratchpad>/prB/harness.sh`
Expected: `passed 39, failed 0` (the restore and deploy cases included once Tasks 3–4 are in; the harness points at the scratchpad copies — copy the repo scripts over them first).

- [ ] **Step 3: Commit**

```bash
git add scripts/backup-db.sh
git commit -m "ops(backup): backup-db.sh follows the running backend's engine and names the file after it"
```

### Task 3: `restore.sh` identifies the backup and refuses a mismatch

**Files:**
- Modify: `scripts/restore.sh` (rewrite)

**Interfaces:**
- Consumes: `<stem>.db` / `<stem>.pgdump` from Task 2; the `db` service.
- Produces: the same CLI (`restore.sh <backup> [files.tgz] [memory.db]`), the same final lines (`Restore complete: the backend is healthy.`); exit 1 before stopping anything on a mismatch, an unknown file, a non-`db` host, or a memory backup without `BESTTEAM_MEMORY_DB`; on Postgres the live database is untouched by a failed `pg_restore`.

- [ ] **Step 1: Replace the script with**

```bash
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
    echo "Start the backend again with 'docker compose start backend', fix the cause, then re-run." >&2
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
```

- [ ] **Step 2: Syntax and the harness** (cases: a `.pgdump` restores through `bestteam_restore` with the swap only after `pg_restore`; a failed `pg_restore` exits 1 without the live `DROP`; a SQLite file into a Postgres deployment and a `.pgdump` into a SQLite one are refused before `stop backend`; junk and an empty file are refused before the probe; a non-`db` host is refused; the SQLite path removes the siblings, copies to the probed path, unpacks files and puts memory back; a memory backup without `BESTTEAM_MEMORY_DB` is refused before `stop`).

Run: `bash -n scripts/restore.sh && bash <scratchpad>/prB/harness.sh` → `passed 39, failed 0`.

- [ ] **Step 3: Commit**

```bash
git add scripts/restore.sh
git commit -m "ops(restore): restore.sh identifies a backup by its bytes and refuses the wrong engine"
```

### Task 4: `deploy.sh` names the pre-upgrade backup after the engine

**Files:**
- Modify: `scripts/deploy.sh:9-12,32-37`

- [ ] **Step 1: Header and step 1**

In the header, replace the two lines of item 1 with:

```
#   1. backs the database up to <backup-dir>/pre-upgrade-<timestamp>.db or
#      .pgdump (scripts/backup-db.sh follows the running backend's engine);
```

Replace

```bash
BACKUP="$BACKUP_DIR/pre-upgrade-$(date +%F-%H%M%S).db"
echo "1/5 Backing up the database to $BACKUP..."
./scripts/backup-db.sh "$BACKUP"
```

with

```bash
STEM="$BACKUP_DIR/pre-upgrade-$(date +%F-%H%M%S)"
echo "1/5 Backing up the database to $STEM.db (SQLite) or $STEM.pgdump (Postgres)..."
./scripts/backup-db.sh "$STEM"
# backup-db.sh names the file after the engine it found; the rollback line
# printed below must name the one that exists.
BACKUP=""
for candidate in "$STEM.db" "$STEM.pgdump"; do
  if [ -f "$candidate" ]; then BACKUP="$candidate"; fi
done
[ -n "$BACKUP" ] || { echo "backup-db.sh wrote neither $STEM.db nor $STEM.pgdump" >&2; exit 1; }
```

- [ ] **Step 2: `bash -n scripts/deploy.sh`; the candidate loop is the harness's last three cases.**

- [ ] **Step 3: Commit**

```bash
git add scripts/deploy.sh
git commit -m "ops(deploy): name the pre-upgrade backup after the engine backup-db.sh found"
```

### Task 5: `docs/deployment.md` — engine, service, runbook, backups

**Files:**
- Modify: `docs/deployment.md` §1 (the "Database engine" bullet), §2 (the container list and the `.env` interpolation paragraph), §3 (replace "Moving to a server database"), "Updating an existing deployment" (the pre-upgrade line), "Data persistence", "Backup and restore".

- [ ] **Step 1: §1 bullet** — replace the "Database engine" bullet with:

```
- **Database engine.** By default the backend uses a SQLite file on the data
  volume (`ui/backend/data/bestteam.db`, or `BESTTEAM_DB_PATH`). Setting
  `BESTTEAM_DATABASE_URL` selects a server database instead; the URL wins
  when both are set, and Alembic, the operator CLI, `check-env` and the
  backup/restore scripts all follow the same setting. `docker-compose.yml`
  ships a Postgres 16 service, `db`, for exactly this: its URL is
  `postgresql+psycopg://bestteam:<POSTGRES_PASSWORD>@db:5432/bestteam`, and
  `POSTGRES_PASSWORD` (`openssl rand -hex 24`) must be in `.env` before the
  stack starts. Both engines are supported in production. A deployment
  starts on SQLite and moves to Postgres with the procedure in section 3
  ("Moving to Postgres"); `check-env` prints which database it resolved to
  (`[OK] database: ...`).
```

- [ ] **Step 2: §2** — after the "The backend applies migrations on every start" bullet add:

```
- **A Postgres 16 server runs beside the backend** (`db`, `postgres:16` --
  the version CI tests against -- with C collation like SQLite's byte
  order). It publishes no port: only the compose network reaches it, as host
  `db`. It is capped at 512 MB and keeps its data in the `bestteam_pg`
  volume. It receives only its own three variables, never `.env`;
  `POSTGRES_PASSWORD` is interpolated from `.env` the way `VITE_*` is below,
  and the compose file refuses to parse without it. That value is read once,
  when the volume is initialised -- changing it later does not change the
  role's password. The backend waits for the server's health check before
  starting, but only *uses* it once `BESTTEAM_DATABASE_URL` names it
  (section 3).
```

and in the closing paragraph change "to substitute `${VITE_API_BASE}`/`${VITE_WS_BASE}`" to "to substitute `${POSTGRES_PASSWORD}` and `${VITE_API_BASE}`/`${VITE_WS_BASE}`".

- [ ] **Step 3: §3** — replace everything from `### Moving to a server database` up to (not including) `## 4. Provision orgs and users` with the runbook (the text is in the spec §4.3, expanded here with the commands; see the committed file for the final wording). Phases: 1 deploy (`deploy.sh`, `POSTGRES_PASSWORD`, three checks), 2 cutover (`check-orphans.sh`, the last SQLite backups, `stop backend`, `migrate-db` with `PW` from `.env` and `-e BESTTEAM_DATABASE_URL=`, the reset, the `.env` edit that sets the key once, `up -d backend`, the verification list, the rollback), 3 the restore drill with `afterbackup`, 4 retiring the file after 7 days or the first customer write.

- [ ] **Step 4: "Updating"** — change `./scripts/backup-db.sh /var/backups/bestteam/pre-upgrade-$(date +%F).db` to `./scripts/backup-db.sh /var/backups/bestteam/pre-upgrade-$(date +%F)   # .db or .pgdump, after the engine`.

- [ ] **Step 5: "Data persistence"** — after the bullet list add:

```
A deployment moved to Postgres (section 3) keeps the database in a second
named volume, `bestteam_pg`, mounted in the `db` container. Nothing on it
is ever copied as files -- `backup-db.sh` dumps it with `pg_dump` -- and the
data volume above then holds no `bestteam.db` (the file is retired at the
end of the cutover) but everything else unchanged.
```

- [ ] **Step 6: "Backup and restore"** — replace the opening code block's first line and the explicit-path examples with the stem form, add the "`backup-db.sh` follows the running backend" paragraph (the engine rule, the stem/extension rule, the `db`-only refusal, the last line), change the memory sentence to `<stem>-memory.db`, and add the `restore.sh` paragraph (bytes not names; mismatch refused; `bestteam_restore` then swap). The cron block stays as it is, with a sentence saying why it needs no edit.

- [ ] **Step 7: Commit**

```bash
git add docs/deployment.md
git commit -m "docs(deployment): the Postgres runbook, the db service, and engine-following backups"
```

### Task 6: The other documents

**Files:**
- Modify: `docs/deployment.zh-CN.md` (§3 pointer paragraph; one sentence in 备份与恢复)
- Modify: `docs/ARCHITECTURE.md:51`
- Modify: `ui/backend/db/CLAUDE.md:19-20`

- [ ] **Step 1: zh-CN** — at the end of §3 (before `## 4.`) add:

```
### 迁移到 Postgres

`docker-compose.yml` 自带一个 Postgres 16 服务（`db`）。把一套部署从 SQLite
文件迁到它上面，是一次「复制」而不是「迁移」：`admin migrate-db` 只读地打开
SQLite 文件，在空库上跑完迁移链，逐表复制并核对行数和主键。完整的操作步骤
（部署、切换、回滚、恢复演练、退役 SQLite 文件）以及每一步应该看到的输出，见
英文版 `docs/deployment.md` 第 3 节「Moving to Postgres」。
```

and in 备份与恢复, after the first code block:

```
迁到 Postgres 之后这两条命令不用改：`backup-db.sh` 会问正在运行的后端用的是
哪个库，SQLite 走在线备份接口，Postgres 走 `pg_dump`，并按实际引擎给文件定
后缀（`.db` 或 `.pgdump`）；`restore.sh` 按文件内容识别备份类型，和后端配置
的引擎对不上会直接拒绝。
```

- [ ] **Step 2: ARCHITECTURE** — the Persistence row's second cell becomes `SQLAlchemy 2.0; SQLite by default, Postgres 16 by \`BESTTEAM_DATABASE_URL\` (the compose \`db\` service)` and the third `A single-file database needs no server; a deployment moves to Postgres with \`admin migrate-db\` (\`docs/deployment.md\` §3). Org-scoped multi-tenancy ...` (rest unchanged).

- [ ] **Step 3: db/CLAUDE.md** — replace `Postgres is\nCI-verified, not yet operated — \`docs/DECISIONS.md\`.` with `production moves to\nthe compose \`db\` service (Postgres 16) with \`docs/deployment.md\` §3, and the\nbackup/restore scripts follow whichever engine the backend uses —\n\`docs/DECISIONS.md\`.`

- [ ] **Step 4: Commit**

```bash
git add docs/deployment.zh-CN.md docs/ARCHITECTURE.md ui/backend/db/CLAUDE.md
git commit -m "docs: point the other documents at the Postgres runbook"
```

### Task 7: DECISIONS amendment and STATUS

**Files:**
- Modify: `docs/DECISIONS.md` (append to the 2026-09-07 entry's Consequences)
- Modify: `docs/STATUS.md` (a Done entry at the top; the migrate-db entry's last sentence; the "Pre-cutover (ops half)" bullet in Known issues)

- [ ] **Step 1: DECISIONS** — append the "Amended 2026-09-10" bullet (trigger brought forward and why; Postgres in the compose stack and why; scripts follow the running backend and why; no rehearsal database and why). The original text stays.

- [ ] **Step 2: STATUS** — the Done entry (what shipped, the harness catch, what is not done); `waits for the trigger in \`DECISIONS.md\`` → `shipped 2026-09-10 (entry above)`; the Known-issues bullet gains `The runbook (\`docs/deployment.md\` §3, 2026-09-10) orders it and gives the reset.`

- [ ] **Step 3: Commit**

```bash
git add docs/DECISIONS.md docs/STATUS.md
git commit -m "docs(decisions): the cutover trigger brought forward, and why Postgres lives in the compose stack"
```

### Task 8: Verification and the pull request

- [ ] **Step 1:** `bash -n` on the three scripts; the harness green; PyYAML parses the compose file; `./.venv/Scripts/python.exe -m pytest -m "not e2e" -n auto -q` green (no Python changed here; this proves the tree).
- [ ] **Step 2:** `git push -u origin feat/postgres-cutover-ops`; `gh pr create --base main` with a body that lists the compose service, the two scripts' rules, the runbook's phases, the harness catch, and what the owner runs on the VPS after merging.
