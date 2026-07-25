# bestteam — `ui/backend/db/` (persistence layer)

Directory-scoped notes for the SQLAlchemy persistence layer. See the root
`CLAUDE.md` for project overview, architecture, and commands; see
`ui/backend/CLAUDE.md` for the API layer that uses this schema.

## Persistence layer

Per-deployment SQLite database via SQLAlchemy 2.0 (`pip install
'bestteam[ui]'`). `db/models.py` defines the schema:

- `organizations` — customer orgs for the multi-tenancy model
  (`db/orgs.py`: `create_org`/`get_or_create_org`/`seed_default_org`; the
  `default` org is seeded at bootstrap so a single-customer deployment just
  has one org). Org-owned tables carry a nullable `org_id` FK; users with
  `org_id IS NULL` are platform operators; skills with `org_id IS NULL` are
  platform built-ins visible to every org. The five named component tables
  swap their global `unique(name)` for named composite
  `UniqueConstraint("org_id", "name", name="uq_<table>_org_id_name")`
  (the repo's first `__table_args__`). Migration `b7c8d9e0f1a2` guards every
  op by inspection (because `db_session` runs `create_all` at import, the
  `organizations` table may already exist when it runs) and backfills only
  NULL `org_id`s: non-admin users and all org-owned rows → `default`;
  admins and built-in skills stay NULL.
- `knowledge_bases` / `skills` / `workflows` — each row's `config` is a JSON
  `raw` dict (the technical fields from `KnowledgeBaseSpec`/`SkillSpec`/
  `Specification.to_raw()`, see `core/specification.py`); `workflows.status`
  tracks `draft` / `ready_for_testing` / `deployed` and is CHECK-constrained
  to that set (`ck_workflows_status`, migration `b1d7e4f2a9c8`, P1-06). Only
  `status="deployed"` rows are runnable (`_get_workflow`) or listed
  (`GET /api/workflows`) — see `ui/backend/CLAUDE.md`. The migration
  backfilled every pre-existing non-`deployed` row to `deployed` so upgrading
  a deployment doesn't retroactively hide/break previously-runnable
  workflows. A deployed workflow's references now block deletion of the
  records it depends on: a `knowledge_bases`/`skills` row can't be deleted
  (`409`) while a `status="deployed"` `workflows` row's `config` still
  references it (KB via an agent's `tools`, skill via an agent's `skills`);
  and a KB may not be named after a built-in tool (rejected `400` at deploy),
  since both resolve through one flat name lookup. See
  `ui/backend/CLAUDE.md`. A `workflows` row is now the **stable team head**: it
  carries `current_version_id` pointing at the latest immutable
  `workflow_versions` snapshot, and `config` is a mirror of that current
  version. Deploy no longer overwrites history in place — it appends a version
  via `db/workflows.py::publish_workflow_version` (P1-01/02/03).
- `workflow_versions` — immutable published snapshots of a workflow's `config`
  (`WorkflowVersion`; `id`, `workflow_id` FK, `version_number`, `config`,
  `created_by`, `created_at`; `(workflow_id, version_number)` unique). Deploy
  appends one row and moves the parent's `current_version_id`; a row is never
  updated after insert. This freezes the **inline config blob only** —
  standalone Skills/KBs/models are still resolved by name at load, so a
  fully-resolved dependency snapshot is deferred to P1-04. Migration
  `c3f5a1b8e2d4` creates the table and backfills one v1 per existing workflow.
  Deleting a workflow head (`DELETE /api/config/workflows/{name}`) also deletes
  its `workflow_versions` rows, under `component_mutation_lock` (so it can't
  interleave with a concurrent publish) -- SQLite FK enforcement is off, so the
  application cascades rather than leaving orphaned version rows.
- `agents` / `teams` — **removed** (migration `57b13700d5df`). Nothing ever
  read them and their `/api/config` routes had already been removed: a
  workflow carries its agents/teams inline in its own `config`, and
  `_build_workflow` accepts only `extra_tools`/`extra_skills`, so a standalone
  row could never reach a run. Writable CRUD routes existed historically
  (`78c7a8a`..`036e1d6`), so the drop migration is guarded: it **refuses** if
  either table holds rows and only drops when empty (a drop is not
  data-reversible).
- `org_email_credentials` — one org's mailbox connection for the email tools
  (unique `org_id`; IMAP host/port/username + encrypted password + optional
  drafts folder). The password is a Fernet token (`secret_store`), never
  plaintext. CRUD in `db/email_credentials.py`; resolved at run time by
  `email_tools.load_email_tools`. The multi-tenant replacement for the
  process-wide `BESTTEAM_EMAIL_*` env vars.
- `email_triggers` — one org's autonomous new-mail trigger: opt-in flag +
  target `workflow_name`, UID dedup baseline (`last_uid`/`uidvalidity`),
  daily-cap counters (`runs_today`/`runs_date`), overlap guard
  (`last_run_id`), and health (`last_checked_at`/`last_error`). Unique
  `org_id` — at most one auto-running team per org. CRUD in
  `db/email_triggers.py`; poll-state mutations in `ui/backend/email_trigger.py`.
- `builder_sessions` — the wizard's session state machine. `status` is one
  of `intent | requirements | spec | solution | testing | deployed`
  (`db/builder_sessions.py::STATUSES`); `requirements_json`/
  `specification_json` hold the Business Analyst / Solution Architect
  agents' structured outputs; `feedback_history` is an append-only JSON list
  recording each round of customer feedback. `workflow_id` (nullable FK) is the
  stable team head this session deploys to — set on first deploy so a redeploy
  versions the same head and two same-named sessions converge on one team
  (P1-02).
- `runs` / `trace_events` — persisted replacement for `RunRegistry`'s
  in-memory state (wired up in Phase 5). `runs.username` (migration
  `c9d0e1f2a3b4`) records who started the run (CR-032, audit-only —
  ownership is org-level via `org_id`). `runs.workflow_version_id` (nullable FK,
  migration `c3f5a1b8e2d4`) records the exact immutable `workflow_versions`
  snapshot a production run executed (P1-03/P1-15); NULL for sandbox test-runs
  (they run the session spec, not a published version) and pre-migration rows.
- `model_catalog` — maps a model `spec` string (e.g. `"openai:gpt-4o-mini"`,
  `"fake:ok"`) to a customer-friendly `display_name`, complexity `tier`
  (`fast`/`balanced`/`advanced`), and per-1K-token input/output pricing
  (Phase 3). Seeded with `DEFAULT_MODEL_CATALOG` (`db/model_catalog.py`) on
  first use of the production engine via `seed_default_catalog()`
  (idempotent — no-op if the table is non-empty).
- `usage_records` — per-agent token usage per run, plus a `cost_estimate`
  computed from `model_catalog` pricing where the model's spec matches an
  entry (Phase 3, `db/usage.py::record_usage`).
- `users` — logins (`db/users.py` + `ui/backend/auth.py`/`auth_api.py`).
  `is_admin` (migration `a1b2c3d4e5f6`) gates the Advanced config and
  Memory pages; `org_id` (migration `b7c8d9e0f1a2`, NULL = platform
  operator) scopes everything else. Both are granted/assigned only via the
  `ui.backend.admin` operator CLI — there is no public registration.
  `is_admin` and a non-NULL `org_id` are mutually exclusive (CR-030):
  `set_admin_status` refuses to promote org members, and the API guards
  ignore the flag on org-bound rows anyway.
  Usernames stay globally unique across orgs (JWT `sub` + memory keying).
  **One member per org** is a schema invariant: a partial unique index
  `uq_users_org_id_not_null` on `org_id WHERE org_id IS NOT NULL` (migration
  `e1f2a3b4c5d6`) — platform operators (NULL) are excluded, so there can be
  many. That migration **refuses** if an upgraded DB already has multi-member
  orgs (names them; never auto-deletes). See `db/users.py::create_user` (the
  friendly pre-check) and `docs/DECISIONS.md`.

`db/database.py` provides `make_engine(db_path)` (`":memory:"` uses a
`StaticPool` so all connections share one database — needed for tests/dry
runs), `init_db(engine)` (`Base.metadata.create_all`), and
`session_factory(engine)`. `db/builder_sessions.py` has the
`builder_sessions` CRUD (`create_session`/`get_session`/`update_session`/
`append_feedback`); CRUD for `knowledge_bases`/`skills`/`workflows` lives in
`ui/backend/crud.py` (Phase 2, see `ui/backend/CLAUDE.md`).
`ui/backend/db_session.py` wires up the per-deployment engine (default
`ui/backend/data/bestteam.db`, override with `BESTTEAM_DB_PATH`) and a
`get_db()` FastAPI dependency.

## Known limitation: persistent run state

`ui/backend/registry.py`'s `RunRegistry` is still the authoritative in-process
live layer — runs vanish on restart and there's no run-history API. As of
CR-012, `ui/backend/runtime.py::run_in_background` does persist one `runs` row
per run (committed before any usage record and updated to its terminal
status/output), so `usage_records`/`trace_events` foreign keys reference a real
row rather than a phantom id. Still deferred to Phase 5: persisting
`trace_events`, rehydrating `RunRegistry` from the DB across restarts, a
history API, and enabling SQLite foreign-key enforcement.
