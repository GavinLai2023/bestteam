# Postgres cutover, the ops half — design

**Date:** 2026-09-10
**Status:** approved by the owner in conversation (sections 1–2 explicitly;
the rest delegated: "make the decisions best for the project and finish the
whole task"). Amends the 2026-09-07 entry in `docs/DECISIONS.md`.
**Code half:** `2026-09-07-database-engine-portability-design.md`, merged
as PRs #123–#125 (`a223235`, `e3706f6`, `aca571c`) plus
`scripts/check-orphans.sh` (#126, `4c8ce62`).

## 1. Context and goal

The code half made the backend engine-neutral: one URL
(`BESTTEAM_DATABASE_URL`, else the legacy `BESTTEAM_DB_PATH`) selects
SQLite or Postgres; the whole backend suite runs against `postgres:16` on
every backend PR; `admin migrate-db` copies a database into an empty one
with pre-flight checks and a count/primary-key verification. Nothing in
production runs Postgres yet. The 2026-09-07 decision deferred the ops half
— provisioning, backup/restore, runbook, rehearsal, cutover — to a trigger:
"the first quiet week after all three first customers are live and have run
for two weeks, or 2026-12-01".

**Why now instead.** On 2026-09-09 the owner observed that the beta VPS is
stable and idle: the three first customers have accounts but have not
started using the system, and walkthroughs are being scheduled — real data
arrives in **two to four weeks**. The cost of a cutover rises with every row
a customer has written (a rehearsal, a rollback that loses nothing, an
announced window); today that cost is at its floor. `check-orphans.sh`
reported the live file **clean** the same day, closing the code-half spec's
one open risk ("the live file's orphan state is unknown"). The owner chose
to do the whole ops half now and cut over before customer data lands. This
spec is that ops half; the DECISIONS entry gets a dated amendment rather
than a re-litigation.

**The host.** One VPS (`BestTeam-beta`, `/opt/bestteam`, Docker Compose):
3.8 GiB RAM, **no swap**, 826 MiB used and 3.0 GiB available on
2026-09-10; 77 GB disk, 63 GB free. `docker-compose.yml` has two services
(`backend`, capped at 2 GB; `frontend`) and one named volume
(`bestteam_data`). `docker-entrypoint.sh` runs `alembic upgrade head`
before `uvicorn`, and only then. The image installs the `ui` extra, so
`psycopg[binary]` is already in it.

**Goal.** After this work the beta deployment runs on a Postgres 16 server
in the same compose stack, its nightly backup is a `pg_dump` archive that
has been restored once, the operator scripts cannot back up or restore the
wrong engine, and the SQLite file has been retired. Everything the owner
does on the VPS is a command to paste with an expected output beside it.

## 2. Rulings

1. **Postgres lives in the compose stack on the same VPS**, not a managed
   instance. Capacity allows it (3.0 GiB available, 63 GB free); it is the
   version CI verifies (`postgres:16`); it adds no bill and no vendor; the
   failure domain stays "this box", which it already is for the SQLite
   file. A managed database buys operational headroom the deployment does
   not need at 1–3 customers, and the memory store and KB files would
   still need the operator's own backups.
2. **Approach A — the scripts follow the running backend.** Every script
   asks the running backend which database it uses and branches on that;
   `restore.sh` identifies a backup by its content and refuses a mismatch.
   The cutover is one line in `.env`; the rollback is removing it.
   Rejected: folding the cutover into `deploy.sh` (a one-time event bound
   to the everyday upgrade tool, with no checkpoint between "db healthy"
   and "data moved"); Postgres-only scripts swapped in at cutover (a window
   in which the wrong script runs against the wrong engine and cron keeps
   reporting success — the silent failure this whole design is against);
   starting from an empty Postgres and recreating orgs, users, teams,
   triggers, catalog and skills by hand (error-prone, loses history, and
   `migrate-db` exists and is CI-tested).
3. **No separate rehearsal database.** `migrate-db` never writes its
   source (it opens a SQLite file `mode=ro`) and the target is disposable,
   so a failure on the day costs nothing but a `DROP DATABASE`; a dry run
   into a second database would tell us nothing the real run does not. The
   procedure has checkpoints and a documented reset and rollback instead.
   What *must* be proven before the window closes is the Postgres
   backup/restore round trip — it is the safety net for everything after —
   and that drill is part of the cutover day.
4. **Filenames never lie.** `backup-db.sh` decides the extension from the
   engine it found (`.db` for SQLite, `.pgdump` for Postgres), treating the
   caller's path minus any such extension as a stem. The existing cron
   line keeps working across the cutover with no edit.
5. **The rollback window is 7 days or the first customer write, whichever
   comes first.** Until then the SQLite file stays on the volume untouched
   and a rollback loses nothing but what the idle poller wrote. After it,
   the file is copied out as the final SQLite backup and removed.
6. **The two pre-cutover code items STATUS.md assigned to the ops half**
   are closed here (§4.4): one small guard with a test; one verification
   that every delete path already runs under foreign-key enforcement in
   the suite, recorded rather than re-implemented.
7. **The runbook lives in `docs/deployment.md` §3** ("Moving to a server
   database" already opens the subject). `docs/deployment.zh-CN.md` gets a
   pointer, not a translation.
8. **Two pull requests**, both from `main`: (A) this spec plus the code
   item; (B) the compose service, the scripts, the docs and the DECISIONS
   amendment. One deploy carries both.

## 3. Scope

**In:** the `db` service and its volume; `.env`/`.env.example`;
`backup-db.sh`, `restore.sh`, `deploy.sh`; the runbook and every doc that
states SQLite is the production engine; the DECISIONS amendment; the STATUS
entries; the guard in `runtime._safe_record_knowledge_generation` and its
test; the delete-path coverage check.

**Out, deliberately:**
- Off-site backups. Still owed (`project_beta_vps_deployment` memory); the
  new archives land in the same directory the nightly cron already writes
  to, so whatever sync is added later picks them up unchanged.
- Postgres tuning, WAL archiving / point-in-time recovery, replication,
  connection pooling, TLS on the compose network, major-version upgrades,
  password rotation. `postgres:16` floats its minor with `docker compose
  pull`, which Postgres handles in place; a major is its own procedure.
- The per-user memory store (`BESTTEAM_MEMORY_DB`) stays SQLite, backed up
  as today. The vector KB files stay on the data volume.
- Multi-host (ADR 2) and the data platform (ADR 3).
- A Chinese translation of the runbook; a full "Moving to a server
  database" section in `deployment.zh-CN.md` (the gap predates this spec).
- Dropping SQLite support: it stays the development default and a valid
  single-file deployment.

## 4. Design

### 4.1 The `db` service and `.env`

`docker-compose.yml` gains:

```yaml
  db:
    image: postgres:16            # the tag the CI lane runs
    restart: unless-stopped
    environment:
      POSTGRES_USER: bestteam
      POSTGRES_DB: bestteam
      # A missing value makes compose refuse to parse the file -- deploy.sh
      # stops at its step 4, before anything is rebuilt -- rather than let
      # the image initialise a server with no password.
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
      # C collation, like SQLite's byte order and the CI lane: the
      # order-sensitive endpoints the suite verifies there behave the same
      # on this server.
      POSTGRES_INITDB_ARGS: "--locale=C --encoding=UTF8"
    volumes:
      - bestteam_pg:/var/lib/postgresql/data
    # No `ports:` -- reachable only as `db` on the compose network.
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bestteam -d bestteam"]
      interval: 5s
      timeout: 5s
      retries: 10
    shm_size: 128m                # the official image's documented floor
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

`backend` gains `depends_on: db: condition: service_healthy`, and the
`volumes:` block gains `bestteam_pg:`.

- `db` receives only its three variables through `environment:`, never
  `env_file: .env` — that would hand every backend secret to the database
  container. `${POSTGRES_PASSWORD}` is interpolated by compose from the
  same `.env` the backend reads, exactly as the frontend's `VITE_*` build
  arguments already are.
- `.env` gains `POSTGRES_PASSWORD`, generated with `openssl rand -hex 24`:
  hex only, so the same value pastes into the URL with nothing to escape.
  It is read once, when the volume is initialised; changing it later does
  not change the role's password (`.env.example` says so).
- The cutover line is
  `BESTTEAM_DATABASE_URL=postgresql+psycopg://bestteam:<POSTGRES_PASSWORD>@db:5432/bestteam`.
  It stays empty at deploy time and is filled at cutover — `.env.example`
  is reworded to say that.
- Memory: 512 MiB cap. At this data volume Postgres sits at one or two
  hundred MiB resident (`shared_buffers` default 128 MB). Limits sum to
  2 GiB + 512 MiB + the frontend, inside 3.8 GiB with no swap.
- The new volume is never file-copied; `pg_dump` is its only backup.
  `backup-files.sh` tars the backend's data volume and is untouched.
- After the deploy that ships this file the server initialises the
  volume (role `bestteam`, database `bestteam`, locale C), reports
  healthy, and holds no tables. The backend, still on SQLite, does not
  touch it — `migrate-db` runs the migration chain on it first, the order
  `deployment.md` §3 already prescribes (the `o2p3q4r5s6t7` rename cannot
  replay over a `create_all`-built schema on Postgres).

### 4.2 Scripts follow the running backend

**The probe.** `backup-db.sh` and `restore.sh` embed the same short Python
snippet, run in the backend image, that prints one line each: engine
(`sqlite`/`postgresql`), host, username, database, the SQLite file path
(empty for a server), and `BESTTEAM_MEMORY_DB`. It never prints a
password. `backup-db.sh` runs it with `exec -T` in the *running* container
(what the backend is actually using); `restore.sh` runs it in a one-off
`run --rm --no-deps` container *before stopping anything* (the backend may
already be down — that is when one restores), as it already does for the
memory path. Not worth a shared library file; `check-orphans.sh` keeps its
own.

**`backup-db.sh [path]`**
- Stem rule (ruling 4): the argument minus a trailing `.db` or `.pgdump`
  is the stem; default `backups/bestteam-<timestamp>`. Output is
  `<stem>.db` or `<stem>.pgdump`; the memory database stays
  `<stem>-memory.db`. The last line names the engine and the file.
- SQLite branch: the existing online-backup API, at the probed path rather
  than a hard-coded one.
- Postgres branch: refuses unless the URL's host is `db` — the script
  `exec`s into that container, and backing up any other host would be the
  wrong thing reported as success. Then
  `docker compose exec -T db pg_dump -U <user> -d <database> --format=custom`
  streamed straight to the host file (no temporary file in the container,
  the shape `backup-files.sh` uses). `pg_dump` takes a consistent snapshot,
  so the backend keeps running. A non-zero exit removes the partial file
  and exits with that status. Inside the container the Unix socket is
  `trust`, so no password is involved.
- Memory branch: unchanged.

**`restore.sh <backup> [files.tgz] [memory.db]`**
- Identifies the backup by its first bytes — `PGDMP` or
  `SQLite format 3` — never by name; anything else is refused.
- Probes the configured engine before stopping the backend and refuses a
  mismatch: a SQLite file copied into a Postgres deployment is not read by
  anything, yet the old script would have printed "Restore complete".
- Postgres branch, after `docker compose stop backend`: drop a leftover
  `<database>_restore` if any, create it, `pg_restore --exit-on-error
  --no-owner --no-privileges` from stdin into it, then — only on success —
  `DROP DATABASE <database> WITH (FORCE)` and `ALTER DATABASE
  <database>_restore RENAME TO <database>`. A failed `pg_restore` leaves
  the live database untouched; re-run after fixing the cause. All of it as
  the `POSTGRES_USER`, which the official image makes a superuser.
- SQLite branch: unchanged, at the probed path.
- Files archive, memory database, `chown`, start and the health wait:
  unchanged. `alembic_version` travels inside the dump; the entrypoint's
  `alembic upgrade head` brings an older backup forward on the next start,
  as it does for SQLite.

**`deploy.sh`**: passes a stem to `backup-db.sh` and finds which of
`<stem>.db` / `<stem>.pgdump` was produced, so the rollback line it prints
names a file that exists. Nothing else changes: step 4's
`docker compose run` parses the new compose file and is where a missing
`POSTGRES_PASSWORD` stops the upgrade; step 5's `up -d` creates `db`,
waits for it, then recreates the backend as today.

**Unchanged:** `backup-files.sh`, `check-orphans.sh` (already
engine-aware), `check-health-cron.sh`.

### 4.3 The cutover runbook (`docs/deployment.md` §3)

Every step is a command with its expected output. Phases:

**Phase 1 — deploy the release that adds `db`.** `./scripts/deploy.sh`.
At step 3 the `.env.example` diff shows `POSTGRES_PASSWORD`; the operator
adds it (`openssl rand -hex 24`) and leaves `BESTTEAM_DATABASE_URL` empty.
Verify: `docker compose ps` shows `db` healthy; `admin check-env` still
reports `[OK] database: sqlite file /app/ui/backend/data/bestteam.db`;
`docker compose exec -T db psql -U bestteam -d bestteam -c "show lc_collate"`
prints `C`. (Corrected in the runbook on 2026-09-11, during the first walk
through it: Postgres 16 removed that server variable, so the collation has to
be read from `pg_database`. The check itself stands.)

**Phase 2 — cutover** (any quiet hour; the system is idle, so no
announcement):
1. `./scripts/check-orphans.sh` → `clean`. `./scripts/backup-db.sh
   /var/backups/bestteam/pre-cutover-<date>` and `backup-files.sh` → the
   last SQLite backup.
2. `docker compose stop backend` — the copy needs a source nobody writes.
3. `docker compose run --rm --no-deps -e BESTTEAM_DATABASE_URL= backend
   python -m ui.backend.admin migrate-db --to
   "postgresql+psycopg://bestteam:${PW}@db:5432/bestteam"` with `PW` read
   from `.env` by `grep`/`cut` (the shell history keeps the unexpanded
   text; `-e BESTTEAM_DATABASE_URL=` pins the source to SQLite whatever
   `.env` says). Expected: `source: sqlite file …`, `target: postgresql
   host db database bestteam`, `alembic upgrade head on the target`, the
   per-table `copying rows` lines, `verified: every table's row count and
   primary keys match the source`, exit 0. A `[FAIL]` is a refusal with
   its reason; a partially populated target is reset with `DROP DATABASE
   bestteam WITH (FORCE)` + `CREATE DATABASE bestteam OWNER bestteam` from
   the `postgres` maintenance database, then rerun.
4. Add the `BESTTEAM_DATABASE_URL` line to `.env`.
5. `docker compose up -d backend` — a recreate, so the new `.env` is read
   (`start` would not re-read it). The entrypoint's `alembic upgrade head`
   finds the target at head and does nothing.
6. Verify: `admin check-env` → `[OK] database: postgresql host db database
   bestteam` and `schema: at head`; `curl localhost:8000/api/health` →
   `{"status":"ok","database":"ok"}`; log in, open the Activity page (the
   copied history), run the team used for walkthroughs and open its trace;
   `admin check-health` → OK (the poller now polls Postgres);
   `./scripts/check-orphans.sh` → `database: postgresql host db database
   bestteam` + `clean`; `docker compose logs --since 15m backend` has no
   traceback.
7. Rollback at any point: delete the URL line, `docker compose up -d
   backend`. The SQLite file is exactly as it was — the copy opened it
   read-only and nothing has written to it since `stop`. Reset the
   Postgres database (step 3) before trying again.

**Phase 3 — prove the safety net, same day.**
`./scripts/backup-db.sh /var/backups/bestteam/post-cutover-<date>` →
`… .pgdump`; `head -c 5` of it → `PGDMP`. Then the restore drill from
`docs/PRELAUNCH_DRILLS_RUNBOOK.md` §3 with the new file: `admin create-org
afterbackup`, `admin list-orgs` shows it, `./scripts/restore.sh <the
.pgdump>` → `Restore complete: the backend is healthy.`, `admin list-orgs`
no longer shows `afterbackup`. Next morning: `ls /var/backups/bestteam/`
holds `bestteam-<date>.pgdump` and `/var/log/bestteam-backup.log` says
`Backed up postgresql database bestteam`. The cron line is not edited.

**Phase 4 — close the window** (ruling 5): take the file's final copy
through SQLite's backup API into
`/var/backups/bestteam/retired-sqlite-<date>.db`, then remove `bestteam.db`
and its `-wal`/`-shm`/`-journal` siblings from the data volume. From then on
a rollback is a restore of a SQLite backup into a SQLite-configured
deployment, i.e. an ordinary restore, not a flip.

### 4.4 The two pre-cutover code items

**`runtime._safe_record_knowledge_generation`.** A KB `tool_completed`
event reaches the recorder after the adapter's node buffering, so the
generation it names may have been pruned meanwhile. On the SQLite file
(keys off) the insert lands with a dangling pointer — the very thing
`check-orphans.sh` reports and `migrate-db` refuses on, so this path could
dirty a clean file between now and the window. On Postgres the insert is
refused; the helper's `except` logs a WARNING with a traceback and rolls
back the run's session (harmless in practice — every other helper commits
its own work — but noise in the production log on every occurrence).
Change: before inserting, `db.get(IngestionJob, ingestion_job_id)`; when it
is `None`, log at INFO and return. The `try/except` stays for the race
between check and insert. Test (FK-enforcing test engine, so the SQLite
suite and the Postgres lane both prove it): a pruned id inserts nothing,
logs nothing at WARNING or above, and leaves the session usable; an
existing id still inserts its row.

**Delete order under foreign-key enforcement.** Every delete path is
enumerated (`crud.delete_item` for skills and knowledge bases,
`delete_pipeline_config`, `delete_model_catalog_entry`, document removal,
`delete_knowledge_base`, `ingestion._prune_old_ingestion_versions` and the
per-KB purge, `retention.purge_run`/`purge_org_runs`, builder-session
deletion, `clear-email`, `delete-user`) and matched to the test that
exercises it under `make_test_engine()`, which enforces keys and, on the
`backend-postgres` lane, runs on Postgres. The table lands in
`docs/STATUS.md`; a path without a test gets one. Expected outcome from
the survey during design: every path has one; the STATUS note was written
before that coverage was confirmed.

(Corrected 2026-09-11, the day after the cutover: the survey was true of
the paths and still missed a case. No delete test ran with a `kb:ingest`
usage row present, and `usage_records.ingestion_job_id` was a foreign key
to the very job row both the prune and the KB delete remove. Under
enforcement the prune failed on every upload after the second billed one
and the KB delete was refused. Migration `c6d7e8f9g0h1` makes it a loose
pointer, like `inbox_events.run_id`; see STATUS.)

### 4.5 Documents and decisions

- `docs/deployment.md`: §1 "Database engine" (both engines supported; the
  compose `db` service; SQLite the default for a single-file deployment);
  §2's list (the `db` service, its cap, `POSTGRES_PASSWORD` interpolation);
  §3 "Moving to a server database" becomes the runbook above; "Data
  persistence" gains the `bestteam_pg` volume; "Backup and restore" gains
  the engine rule, the stem/extension rule, `pg_dump`, the content-sniffing
  restore and its mismatch refusal — the cron line is unchanged; "Updating"
  shows the stem form of the pre-upgrade backup.
- `docs/deployment.zh-CN.md`: one paragraph in §3 pointing at the English
  runbook, one sentence in 备份与恢复 on `.pgdump`.
- `.env.example`: `POSTGRES_PASSWORD`; the URL comment.
- `docs/ARCHITECTURE.md` (persistence row), `ui/backend/db/CLAUDE.md`
  (the "CI-verified, not yet operated" sentence): one line each.
- `docs/DECISIONS.md`: a dated amendment under the 2026-09-07 entry —
  trigger brought forward and why; Postgres in the compose stack and why;
  scripts follow the running backend and why. The original text stays.
- `docs/STATUS.md`: a Done entry for the ops half; the migrate-db entry's
  "waits for the trigger" sentence; the two pre-cutover bullets in Known
  issues marked resolved with the coverage table.

## 5. Verification

- **Python:** the guard's tests in `tests/test_run_knowledge_generations.py`
  (`integration` marker), run locally on the FK-enforcing SQLite engine
  and on the Postgres lane in CI. Full `-m "not e2e"` run before pushing.
- **Scripts:** the workstation has no Docker and no shellcheck. `bash -n`
  on every changed script; the pure-bash logic (stem/extension, header
  sniffing, the two-candidate lookup in `deploy.sh`) exercised in a
  scratchpad harness; the Docker paths are verified on the VPS by the
  runbook itself, whose expected outputs are the acceptance test.
- **Compose file:** parsed with PyYAML locally; on the VPS `deploy.sh`
  step 4 parses it for real before anything is rebuilt. `docker-compose.yml`,
  `.env.example` and `scripts/**` are in CI's `shared` filter, so the
  backend lanes and `e2e-smoke` run on the PR.
- **The runbook:** each phase names the output that proves it; phase 3 is
  the restore drill that the deployment checklist (row 14) has always
  demanded.

## 6. Risks

- *`POSTGRES_PASSWORD` forgotten at the first deploy.* Compose refuses to
  parse; `deploy.sh` stops at step 4 with the message, old containers
  still serving.
- *The backend started against the empty server before `migrate-db`.*
  Its `alembic upgrade head` seeds rows and the copy refuses "the target
  already holds rows". The runbook orders it and gives the reset.
- *Source still written to during the copy.* `verify_copy` fails rather
  than copying silently; the runbook stops the backend first.
- *Data Postgres rejects that SQLite accepted* (a NUL byte in text, an
  invalid UTF-8 sequence). `migrate-db` fails on the row and names the
  table; the target is reset and the row fixed by hand. The live file is
  small and clean; not expected, but the reset makes it cheap.
- *No swap.* The three limits sum under physical memory; if `db` is ever
  OOM-killed it restarts (`unless-stopped`) and the backend's
  `pool_pre_ping` reconnects.
- *`db` restarts while the backend runs.* `pool_pre_ping` replaces the
  dead connection; a request in flight fails once.
- *A stale rollback.* After the window closes a flip back would lose
  Postgres-side writes; ruling 5 removes the file so the flip is no longer
  possible and the ordinary restore path is the only way back.

## 7. Delivery

- **PR A** (`fix/pre-cutover-knowledge-generation-guard`): this spec, the
  guard and its tests, the STATUS bullet for the two items.
- **PR B** (`feat/postgres-cutover-ops`): compose, `.env.example`, the
  three scripts, the docs, the DECISIONS amendment, the STATUS entry, and
  the implementation plan.
- **On the VPS**, by the owner, after both merge: `deploy.sh` (phase 1),
  the cutover (phase 2), the drill (phase 3), and in a week the retirement
  (phase 4). Done means: `check-env` names Postgres; a `.pgdump` from the
  nightly cron has been restored once; the SQLite file is gone from the
  volume.
