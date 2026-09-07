# Database engine portability: SQLite today, Postgres-ready code, cutover later

**Date:** 2026-09-07
**Branch:** `feat/db-engine-portability` (cut from `main` at `244da8b`)
**Follows:** `docs/DECISIONS.md` "Beta runs single-process on SQLite; Postgres
is not planned before GA" (2026-08-22), which this spec does **not** overturn:
production stays single-process on SQLite until the cutover trigger in §12.
**Status:** approved in chat on 2026-09-07 (design sections, then the eight
rulings, all taken with the recommended answer). Every ruling is recorded
inline under **Ruling** so a disagreement has one place to land.

## Why

Asked what "scalability" means for the platform, the owner answered: all four
readings at once — more organisations and load, more than one host, data that
outside tools (reporting, a second product) can reach, and a team larger than
one. SQLite cannot deliver the last three: a file has no network interface, so
nothing but the process that opens it can read it, and only one host can hold
it. Postgres is therefore not a question of *whether* but of *when*.

The 2026-08-22 decision lists only benefit-side triggers (more than ten orgs,
concurrent runs above four, a second account per org, an HA/SLA commitment).
It has no cost-side trigger, and the cost of a cutover rises with every
customer whose data is in the file: today the live database holds no customer
organisation and a failed cutover costs nothing but time; at ten orgs the same
cutover needs a rehearsal, a rollback plan and an announced window. The
cheapest moment is before the first customer's data lands, which is weeks
away — and those weeks are reserved, by the 2026-09-02 freeze, for onboarding.

Three options were weighed: write the roadmap only; migrate before the first
customer; or split the work into a **code half now** (nothing changes on the
live server, the owner's attention cost is PR review) and an **ops half
later** at a committed window. **Ruling:** the split. Its coherence depends
on the cutover being committed, not merely hoped for — that commitment is §12.

This spec is the code half. The ops half (provisioning, backup/restore,
runbook, rehearsal, the cutover itself) gets its own spec when its window
opens. Two further ADRs sit behind it and are named here only so nobody
mistakes this one for them: **ADR 2 — shared state** (moving `RunRegistry`,
the per-org dispatch locks, the draft-idempotency lock, the login throttle and
the WebSocket tickets out of the process; this is what multi-host actually
costs) and **ADR 3 — data platform** (a read-only reporting role, row-level
security on `org_id`, JSONB). Postgres is a precondition for both, not a
substitute for either.

## 1. Goal and scope

**Goal.** The backend, Alembic, the operator CLI and the test suite work
against either SQLite or Postgres, selected by one connection URL. Local
development, the test suite's default, and the live deployment all stay on
SQLite, unchanged. CI proves the Postgres path on every backend PR. A data
migration command exists and is tested, so the ops half is configuration and
operations, not code archaeology.

**In scope.**

- One URL resolver, one engine factory that accepts a path or a URL (§3).
- Dialect-neutral schema and queries where the code is SQLite-specific today
  (§4).
- Foreign-key truthfulness: the one live code path that writes a child row
  for a run that has no row, and foreign-key enforcement in the test engine
  (§5).
- `check-env` / `check-health` reading through the engine instead of the
  file (§6); the single-instance lock keyed on the data directory whenever the
  engine is not a SQLite file (§3).
- The Postgres driver shipped in the image (§7).
- A test-engine entry point, Postgres per-test databases, a migration-replay
  test on Postgres, and a CI lane (§8–§10).
- `admin migrate-db`: copy one database into another with pre-flight checks,
  orphan policy and verification (§11).
- Documentation and the decision record, including the cutover trigger (§12).

**Out of scope, deliberately.**

- Everything operational: choosing managed vs self-hosted Postgres,
  provisioning, `backup-db.sh` / `restore.sh` / `deploy.sh`, the runbooks,
  the rehearsal, the cutover. Production keeps running SQLite after this work
  merges.
- The per-user memory store (`BESTTEAM_MEMORY_DB`, stdlib `sqlite3`, its own
  hand-written SQL) and the vector knowledge-base files. They stay as they
  are; a Postgres deployment still needs file backups for them.
- ADR 2 and ADR 3 (above). In particular: no JSONB (**Ruling 3**: keep the
  portable `JSON` type now; converting to JSONB later is one migration, and
  choosing it now would force a type-altering migration just to keep the two
  fresh-schema paths consistent), no row-level security, no advisory locks.
- Turning foreign-key enforcement on for the **production** SQLite file. The
  live file's data has not been checked (§5); enforcement is a test-engine
  property here.

## 2. Findings that shaped the design

Verified on 2026-09-06/07 against `main` and the local development database
(`ui/backend/data/bestteam.db`, 8.6 MB, Alembic `y2z3a4b5c6d7`).

**Where the code is SQLite-specific** (the complete list — the ORM layer's 31
tables, 14 `JSON` columns and 52 naive-UTC `DateTime` columns are portable,
`automation_results.py` deliberately avoids JSON1, and no query uses `LIKE`,
`GROUP BY` on non-aggregated columns, or a SQLite-only SQL function):

| Coupling | Where | Cost |
|---|---|---|
| Engine takes a file path and hardcodes `sqlite:///` plus the WAL pragma | `ui/backend/db/database.py:41`, `alembic/env.py:22` | small |
| Dedup insert via `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_nothing` | `ui/backend/db/inbox_events.py:105` (its docstring already names this as a migration touch-point) | small |
| Partial unique index declared with `sqlite_where` only | `ui/backend/db/models.py:84` | small |
| Boolean `server_default=text("1")` — Postgres rejects `DEFAULT 1` on a boolean | `models.py:66`, `:328`; migrations `a3f7c9d2e6b1`, `c1d2e3f4a5b6`, `f2a3b4c5d6e7`, `v9w0x1y2z3a4` | small |
| `check-env` opens the file with stdlib `sqlite3` (three checks) | `ui/backend/env_check.py:236`, `:330`, `:369` | small–medium |
| Single-instance lock is `<db file>.lock` | `ui/backend/process_lock.py:57`, `main.py:168` | small |
| Foreign keys are declared (52) but SQLite never enforces them; one `ondelete` in the whole schema | `db/models.py`, `db/CLAUDE.md` | medium (§5) |
| 41 migrations, 33 `batch_alter_table` uses, never run on Postgres | `alembic/versions/` | small: batch mode degrades to plain `ALTER` off SQLite; only the boolean defaults above are known to fail |
| 71 `make_engine(":memory:")` call sites in 50 test files; 50 `make_concurrent_safe_engine()` sites in 25 files | `tests/` | medium, mechanical (§8) |

**Orphan rows in the development database** (`PRAGMA foreign_key_check`):

| Child → parent | Rows | What they are |
|---|---|---|
| `usage_records` → `runs` | 101 | All dated 2026-06-14 … 06-25. The oldest `runs` row is 2026-07-16 and so is the oldest non-orphan usage row: a pre-July artefact, not a live path. |
| `trace_events` → `runs` | 3 | All `run_failed` / "The run failed due to an internal error.", 2026-09-05 12:59:20, 12:59:26, 13:41:06. A run that failed before its `runs` row was committed still had its terminal event persisted. **A live path** (§5). |

No code path deletes a `runs` row (grepped `ui/backend` and `tests`).

**No evidence that SQLite is hurting today.** No `database is locked`
handling or commit mentions it; `ingestion.py` already batches into one short
transaction because of the write lock; every check-then-act the code knows
about holds a process-level lock (`component_lock`, the KB upload lock, the
share-chat session lock, the dispatch lock, the login limiter, `registry`).
The 08-22 triggers have not fired.

**Local tooling.** The workstation has no Docker, no `psql`, one interpreter
(Python 3.13) and `uv 0.11.19`. The pip-installable embedded Postgres
(`pgserver`) ships a Windows wheel for 3.12 only. The official portable
binaries zip (EDB, PostgreSQL 16.4, 339 MB; 17.2, 311 MB) is reachable and
needs no installer. §10.

## 3. Engine and URL resolution

**`resolve_database_url(env=os.environ) -> str`** in
`ui/backend/db/database.py`:

1. `BESTTEAM_DATABASE_URL` set and non-blank → returned as is.
2. Else `BESTTEAM_DB_PATH` (or the existing default
   `ui/backend/data/bestteam.db`) → `sqlite:///<path>`; the literal
   `:memory:` → `sqlite:///:memory:`.

Both variables set is not an error (`check-env` warns, §6); the URL wins.
Nothing else reads either variable directly any more: `db_session.py`,
`alembic/env.py`, `env_check.py` and `admin.py` all call the resolver.
`db_session.DB_PATH` stays exported for its remaining consumer (the lock,
below) and gains a sibling `DATABASE_URL`.

**`make_engine(target, *, echo=False) -> Engine`** keeps its signature and
accepts three shapes:

- `":memory:"` → unchanged (`StaticPool`, `check_same_thread=False`).
- a path (no `://`) → `sqlite:///<path>`, unchanged behaviour.
- a URL → `create_engine(url)`. SQLite URLs get the existing WAL listener
  (not for `:memory:`); other dialects get `pool_pre_ping=True` and nothing
  else — pool sizing stays at SQLAlchemy's defaults until the ops half
  measures a reason to change it.

A malformed URL raises at engine construction with a message that names
`BESTTEAM_DATABASE_URL`; a URL whose driver is not installed does the same,
naming the `ui` extra.

**Alembic.** `alembic/env.py` sets `sqlalchemy.url` from the resolver **only
when the config carries none**, so a caller that already set the option
programmatically (the migration tests, `migrate-db`) wins. The Docker
entrypoint's `alembic upgrade head` therefore follows the same URL as the
server, with no change to the entrypoint.

**Single-instance lock.** `process_lock.acquire_single_instance_lock` today
takes the database path and locks `<path>.lock`. For a SQLite file the lock
stays exactly where it is (same file name, so an upgrade does not change what
a second process sees); for any other engine the lock file is
`bestteam.lock` in the data directory (`ui/backend/data`, the volume that
still holds uploads and the memory store). A process-level lock is still the
right tool here — ADR 2 replaces it, not this spec.

## 4. Dialect-neutral schema and queries

- **Partial unique index** `uq_users_org_id_not_null`: add
  `postgresql_where=text("org_id IS NOT NULL")` beside the existing
  `sqlite_where`. Without it Postgres would build a full unique index, which
  still permits many `NULL`s but no longer expresses the intent.
- **Boolean defaults**: the two `active` columns in `models.py` and the four
  migrations listed in §2 switch `sa.text("1")` / `sa.text("0")` to
  `sa.true()` / `sa.false()`. On SQLite the emitted DDL is the same `1`/`0`;
  editing historical migrations is acceptable because they only run on fresh
  databases and the semantics do not change. No new migration.
- **Dedup insert** in `inbox_events.record_events`: pick the `insert`
  construct from `db.get_bind().dialect.name` — `sqlalchemy.dialects.sqlite`
  or `sqlalchemy.dialects.postgresql` — and call the same
  `.on_conflict_do_nothing(index_elements=_IDENTITY_COLUMNS)`. Any other
  dialect raises `NotImplementedError` naming the function; the project
  supports exactly these two.
- Everything else in the ORM is left alone. Naive UTC datetimes map to
  `timestamp without time zone` and round-trip exactly as they do on SQLite.

## 5. Foreign-key truthfulness

**The live orphan path.** `runtime.run_in_background` persists the `runs` row
up front (CR-012, `runtime.py:796–820`) so that `usage_records` and
`trace_events` always have a parent. The three 2026-09-05 orphans show a run
whose row never committed still reaching `_safe_record_trace_event`
(`runtime.py:471`) with its terminal `run_failed` event. On Postgres that
insert is refused by the foreign key; the helper swallows the error (by
design — trace persistence must never break a run), so the effect is a silent
loss of the terminal trace, not a crash. The fix is in the failure path
(`runtime.py:1148–1166` and the terminal-event publish that follows): **the
terminal trace row is written only after the run row exists**, i.e. the
failure path first ensures the `runs` row (inserting it with `status =
"failed"` if the up-front insert never happened), then records the event.
Test: a pipeline whose construction raises before the first event, on an
engine with foreign keys enforced, ends with exactly one `runs` row and one
`trace_events` row pointing at it.

**Enforcement in the test engine.** **Ruling 7:** `make_test_engine` (§8)
adds `PRAGMA foreign_keys=ON` to every SQLite test connection. This makes the
default SQLite suite surface the same class of defect the Postgres lane
would, without Postgres, and locally in seconds. The number of tests this
breaks is unknown until it is tried; fixtures that insert children without
parents are fixed to insert the parent. **Fallback, agreed in advance:** if
the breakage is mostly fixture plumbing rather than product defects and
exceeds what one session can clean up, the pragma is dropped from the SQLite
test engine and only the Postgres lane enforces keys. Either way the
production SQLite file keeps enforcement off.

**Historical orphans** are the migration command's business (§11), not the
application's.

## 6. `check-env` and `check-health`

- `env_check.default_db_path` becomes `default_database_url` (the resolver).
  `_stamped_revision`, `check_schema`, `check_org_retention` and
  `check_model_catalog` take a URL and read through a SQLAlchemy engine. For
  a SQLite file the engine is opened read-only
  (`sqlite:///<path>?mode=ro&uri=true`) so `check-env` still never creates
  the file, and a missing file still reports as it does today.
- New finding line, always printed: `[OK] database: sqlite file <path>` or
  `[OK] database: postgresql host <host> database <name>` — the URL is
  rendered with the password removed. `[FAIL]` when the URL cannot be parsed
  or its driver is not installed; `[WARN]` when both `BESTTEAM_DATABASE_URL`
  and `BESTTEAM_DB_PATH` are set, stating that the URL wins.
- `admin check-health`'s "no database yet; nothing to monitor" short-circuit
  applies only to a SQLite URL whose file is absent. For a server database an
  unreachable server is a `[FAIL]`, which is the correct signal from a cron
  job.
- `test_env_check.py` builds its fixtures with raw `sqlite3` and sets
  `BESTTEAM_DB_PATH`; it keeps doing so and keeps passing.

## 7. Packaging

`psycopg[binary]>=3.1` joins the `ui` extra. **Ruling 5:** it ships in the
image, so the same build connects to either engine and the cutover day needs
no rebuild. `requirements.lock` is regenerated with the `uv pip compile`
command from `CLAUDE.md`. The Dockerfile is unchanged (it installs `ui`).

## 8. Tests

**One entry point.** `tests/helpers.make_test_engine(tmp_path=None)`:

- Default (no `BESTTEAM_TEST_DATABASE_URL`): `tmp_path is None` →
  `make_engine(":memory:")`; with `tmp_path` → the file engine
  `make_concurrent_safe_engine` builds today (`synchronous=OFF`, same
  rationale). Plus the foreign-key pragma (§5).
- With `BESTTEAM_TEST_DATABASE_URL` set (a maintenance URL, e.g.
  `postgresql+psycopg://postgres:postgres@localhost:5432/postgres`): both
  shapes return an engine on a **fresh database cloned from a template**.
  Session start creates `bestteam_tmpl_<id>` and runs `init_db` on it;
  each call runs `CREATE DATABASE t_<n> TEMPLATE bestteam_tmpl_<id>` on an
  autocommit connection. An autouse function-scoped fixture in
  `tests/conftest.py` disposes every engine created during the test and
  `DROP DATABASE … WITH (FORCE)`s its database — per test, not per session,
  because a template clone is several megabytes and a session would create
  more than a thousand of them. Estimated overhead: a fraction of a second
  per database-using test; if the lane exceeds ~15 minutes, the fallback is
  one database per test module with `TRUNCATE` between tests.
- `init_db(engine)` in existing fixtures stays: `create_all` is a no-op on
  the clone.

**The sweep.** Every `make_engine(":memory:")` and
`make_concurrent_safe_engine(tmp_path)` in `tests/` becomes
`make_test_engine()` / `make_test_engine(tmp_path)`; the old helper is
removed. Tests that construct SQLite files on purpose (`test_env_check.py`,
`test_migrations.py`, the e2e harness that spawns a real backend with
`BESTTEAM_DB_PATH`) are untouched and keep running on SQLite inside the
Postgres lane. A `sqlite_only` marker exists for any test that genuinely
depends on the `:memory:` shared-connection semantics the helper's docstring
describes; the expectation is that it is applied to very few tests, each with
a one-line reason.

**Postgres migration replay.** `tests/test_migrations_postgres.py`
(`integration`, skipped without `BESTTEAM_TEST_DATABASE_URL`): on an empty
database, `alembic upgrade head` via a programmatic `Config` whose
`sqlalchemy.url` is the target; assert the table set equals what `create_all`
produces on a second empty database, and column names per table match. This
is the check that the 41 migrations replay on Postgres (**Ruling 6**: a fresh
Postgres schema is produced by `alembic upgrade head`, the same path the
Docker entrypoint takes, not by `create_all` + `stamp`).

**Markers.** Every new file carries a `pytestmark`
(`tests/test_marker_completeness.py` enforces it).

## 9. CI

One new job in `.github/workflows/ci.yml`, modelled on `backend-full`:

- `backend-postgres`: `needs: changes`, `if: needs.changes.outputs.backend ==
  'true'` — **Ruling 2:** on pull requests as well as `main`, because the
  point is to catch a new SQLite-ism before it merges. `services.postgres`
  = `postgres:16` with a `pg_isready` health check; env
  `BESTTEAM_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres`;
  install `.[ui,dev,tools,interview]`; run `python -m pytest -m "not e2e and
  not optional"` **serially** (per-test databases and `-n auto` do not mix);
  `timeout-minutes: 25`.
- Expected duration: eight to twelve minutes (the serial SQLite suite is
  3m42s; the rest is database creation and per-query round trips).

## 10. Local verification

**Ruling 4:** the portable PostgreSQL binaries zip is downloaded into the
session scratchpad, unpacked, initialised with `initdb` and started with
`pg_ctl` on a high port, all outside the repository and disposable. The lane
is then `BESTTEAM_TEST_DATABASE_URL=postgresql+psycopg://…@localhost:<port>/postgres
python -m pytest -m "not e2e and not optional"`. If the zip cannot be used
on this workstation, iteration happens through the CI lane on a draft PR.
Nothing about the portable server is committed or documented as a developer
requirement: local development needs no Postgres.

## 11. `admin migrate-db`

`python -m ui.backend.admin migrate-db --to <url> [--fix-orphans]
[--batch-size 1000]`. Source is the resolved current database. The command
**never writes to the source**.

**Pre-flight** (any failure refuses before a single row moves):

1. Source reachable and stamped at Alembic head; otherwise "run `alembic
   upgrade head` first".
2. Target reachable; target URL differs from source URL.
3. Target holds no rows in any model table (an `alembic_version` table alone
   is fine); otherwise refuse — the command populates empty databases only.
4. Orphan scan on the source: for every foreign key in `Base.metadata`,
   count child rows whose value is non-null and has no parent, printed per
   key. Any non-zero count without `--fix-orphans` refuses.

**Steps.**

1. `alembic upgrade head` against the target (programmatic `Config`, URL set
   on it), so the target's schema comes from the same path production uses.
2. Copy in `Base.metadata.sorted_tables` order using Core `select`/`insert`
   on the model tables, so JSON, boolean and datetime values go through the
   column types on both sides. Rows are read in primary-key order, except
   `runs`, read by `created_at, id` so a retry or diagnostic run follows the
   run it references. Batches of `--batch-size`.
3. **Ruling 8 (orphan policy)** — applied to the copy stream, not the
   source: a nullable dangling foreign key is written as `NULL`; a row whose
   non-nullable foreign key dangles is skipped. Both are counted and printed
   per table, with the skipped rows' primary keys.
4. On a Postgres target, reset every integer-primary-key sequence to
   `max(id) + 1` so the first insert after the copy does not collide.
5. Verify: row counts per table (source minus skipped orphans equals
   target), and per table the sorted primary keys are compared. Print one
   summary table; exit non-zero on any mismatch.
6. Final message: the memory store and the files under `ui/backend/data`
   were not copied; to switch, set `BESTTEAM_DATABASE_URL` and restart. (The
   ops runbook says the same at greater length.)

**Tests.** The copy logic is engine-agnostic, so:

- SQLite file → SQLite file, in every environment: a seeded source
  (organisations, users, a deployed pipeline with versions and
  dependencies, runs with trace/usage/automation rows, a share link, an
  inbox event, a KB with a document and chunks) plus planted orphans of both
  kinds. Asserts: refusal without `--fix-orphans`; counts and orphan
  handling with it; refusal on a non-empty target; refusal on same URL;
  primary keys preserved.
- SQLite file → Postgres, in the Postgres lane only: the same seed; plus an
  insert into an autoincrement table after the copy succeeds (the sequence
  reset).

## 12. Documentation and the decision record

- **`docs/DECISIONS.md`**, new entry "Postgres is the target engine; the code
  supports both; SQLite stays in production until the cutover trigger". It
  records: the four readings of scalability and why they make Postgres a
  when-question; the three-ADR roadmap; that local development and the
  test default stay SQLite (a new developer needs no database server); that
  "supports both" means CI-verified, not yet operated; the cost-side trigger
  added to the 08-22 list; and the **cutover trigger — Ruling 1:** *the first
  quiet week after all three first customers are live and have run for two
  weeks, or 2026-12-01, whichever comes first.* The ops-half spec is written
  when that trigger fires. The 08-22 entry's single-process ruling stands.
- **`docs/deployment.md`** §1 gains "Database engine": the two variables,
  precedence, "SQLite is the supported production engine today; Postgres
  support is code-complete and CI-verified but not yet operated in
  production". §3 notes Alembic follows the same URL.
- **`.env.example`**: a commented `BESTTEAM_DATABASE_URL=` with one line of
  explanation.
- **`ui/backend/db/CLAUDE.md`** "Engine and wiring": the resolver, the three
  `make_engine` shapes, foreign keys enforced in tests only.
- **`docs/ADMIN_GUIDE.md`** environment table: the new variable, and
  `migrate-db` in the CLI list.
- **`docs/STATUS.md`**: one entry per merged PR, plus the SQL-per-request
  numbers measured once during PR 1 (three requests: run list, run detail,
  KB list) so the ops half knows how chatty the ORM is before it pays a
  network round trip per query.

## 13. Error handling

- Bad or unsupported URL: the process fails at engine construction with a
  message naming the variable (and the extra, for a missing driver). The
  same message reaches `check-env` as a `[FAIL]`.
- Server database unreachable at startup: `db_session`'s import-time
  `create_all` fails and the process does not boot — fail fast, with the
  driver's message. `/api/health` already returns 503 when its `SELECT 1`
  fails; unchanged.
- `migrate-db`: every refusal above is a non-zero exit with the reason
  first; a failure mid-copy leaves the target partially populated and says
  so (the target was required to be empty, so "drop it and rerun" is the
  whole recovery).
- Trace and usage persistence stay best-effort (`_safe_*`); §5 only changes
  the order of writes in the failure path.

## 14. Delivery

Three PRs, in order; each is green on its own.

| PR | Contains | Done when |
|---|---|---|
| 1 `feat/db-engine-portability` | §3 resolver + engine + Alembic + lock; §4 dialect fixes; §6 `check-env`/`check-health`; §7 driver + lockfile; §12 docs and the DECISIONS entry; the SQL-per-request measurement | SQLite suite green; `BESTTEAM_DATABASE_URL=sqlite:///…` runs the backend, Alembic and `check-env` end to end; `check-env` prints the database line |
| 2 | §5 orphan-path fix and foreign keys in the test engine; §8 test entry point and sweep; §9 CI lane; every defect the lane surfaces | SQLite suite green with keys enforced; `backend-postgres` green on the PR; the Postgres migration-replay test passes |
| 3 | §11 `migrate-db` and its tests; a local rehearsal copying the development database into the portable Postgres | both `migrate-db` tests green; rehearsal output recorded in the PR |

PR 2 is where the unknowns live (how many tests the enforced keys break, what
else the lane finds); PR 1 is small and independently useful; PR 3 depends
on both.

## 15. Risks accepted

- **Concurrency semantics differ.** SQLite serialises writers; Postgres runs
  them concurrently under `READ COMMITTED`. Today's process-level locks cover
  every known check-then-act, and the deployment stays single-process, so
  the behaviour is unchanged in practice. The lane cannot prove the absence
  of a race; ADR 2 owns this properly.
- **Latency amplification.** A SQLite read is an in-process call; a server
  query is a network round trip. Chatty ORM patterns become visible. The PR 1
  measurement (§12) turns this from a guess into numbers before the ops half.
- **Foreign-key fallout is unmeasured** until PR 2 runs; the fallback is
  agreed (§5).
- **The live file's orphan state is unknown.** `PRAGMA foreign_key_check`
  on the VPS is read-only and is the ops half's first step; the command's
  pre-flight (§11) makes it impossible to overlook.
- **CI minutes**: one more eight-to-twelve-minute job per backend PR.
