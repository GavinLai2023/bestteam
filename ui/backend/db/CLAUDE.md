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
- `skill_versions` — immutable snapshots appended by
  `db/skills.py::publish_skill_version` on every skill save. `skills` is the
  stable library head: `current_version_id` selects the latest snapshot and
  `config` remains its compatibility mirror. Migration `c4d5e6f7a8b9`
  backfills every existing skill as v1 without changing its content and adds
  the same `skill_versions.id` foreign keys on upgraded databases that
  `create_all` produces for fresh databases (including retry repair when a
  column exists without its constraint).
- `workflow_versions` — immutable published snapshots of a workflow's `config`
  (`WorkflowVersion`; `id`, `workflow_id` FK, `version_number`, `config`,
  `created_by`, `created_at`; `(workflow_id, version_number)` unique). Deploy
  appends one row and moves the parent's `current_version_id`; a row is never
  updated after insert. This freezes the inline config blob; referenced skills
  are frozen through the dependency's `resource_version_id` (below).
  Standalone KBs/models are still resolved by name at load.
  Migration `c3f5a1b8e2d4` creates the table and backfills one v1 per existing workflow.
  Deleting a workflow head (`DELETE /api/config/workflows/{name}`) refuses
  (`409`) while any `Run` records one of its versions -- deletion removes the
  version history those runs reference (FK enforcement is off, no DB cascade), so
  provenance is preserved. A never-run head deletes cleanly, cascading its
  (unreferenced) `workflow_versions` and nulling any `builder_sessions.workflow_id`
  that pointed at it (self-heals on next deploy), under `component_mutation_lock`,
  so no orphaned version rows or dangling session pointers survive. Known gap
  (deferred, design spec): an in-flight run whose `runs` row is written by the
  worker *after* dispatch isn't seen by the delete guard, so deleting a workflow
  at the instant a run of it starts can dangle that run's provenance pointer --
  closed only by soft-delete/archive (deletion-lifecycle sub-project).
- `workflow_dependencies` — one typed row per (published version, skill|standalone-KB)
  it depends on (`WorkflowDependency`; `workflow_version_id` FK, `resource_kind`,
  `resource_name`, `resource_id` = the resolved `skills`/`knowledge_bases` id,
  and `resource_version_id` = the immutable `skill_versions.id` for skills;
  `(workflow_version_id, resource_kind, resource_name)` unique). Written
  once at deploy in `db/workflows.py::publish_workflow_version` via
  `db/dependencies.py::record_version_dependencies` (resolves names exactly as the
  loader: org skill shadows platform built-in; KBs org-scoped; a built-in tool /
  email tool / inline KB is not a KB dep — an inline KB shadows a *same-named*
  standalone KB too, so the standalone isn't recorded when the workflow defines
  its own). The skill/KB `DELETE` guard now queries these rows by `resource_id`
  for the **current** version (`workflows_referencing`) instead of scanning JSON —
  non-regressing, and the stable id makes the platform-built-in-skill cross-org
  case fall out without an all-orgs scan. Skill dependencies are immutable:
  editing a skill or creating an org override never rewrites a deployed team;
  redeploy is the explicit opt-in to the then-current resolved skill version.
  Migration `d4e6b2c9f1a7` creates the table and backfills each workflow's current
  version; `c4d5e6f7a8b9` adds and backfills skill-version pins. Model/tool deps
  and standalone-KB content pinning remain deferred.
- `knowledge_ingestion_jobs` (`IngestionJob`) — one async ingestion run for an
  upload-managed `knowledge_bases` row (`ui/backend/ingestion.py`). `status`
  (CHECK-constrained `queued`/`running`/`completed`/`failed`) tracks the job;
  a KB's live document set is always its most recent `completed` job's rows —
  the `status="completed"` flip **is** the atomic swap for this path, unlike
  the legacy file-based upload's `CURRENT`-pointer-file swap. `version`
  matches the on-disk version-directory name the uploaded files were staged
  into, for traceable job↔directory correspondence. `kb_type`/
  `embedding_model` record the shape this job's chunks were actually ingested
  under — the read path resolves the KB's subclass and query-time embedding
  model from **these**, never from `knowledge_bases.config`, which advances to
  the new spec at upload-dispatch time while the previous generation is still
  the live one (a re-upload that changes type would otherwise `json.loads`
  a NULL `embedding_json`, and one that changes only the embedding model would
  silently query a mismatched vector space). `file_count`/
  `documents_succeeded`/`documents_failed` and a capped `error` summarize the
  outcome; indexed on `(kb_id, status, completed_at)` for the "most recent
  completed job" resolution query. See `ui/backend/CLAUDE.md`.
- `knowledge_documents` (`KnowledgeDocument`) — one uploaded file's ingestion
  outcome within an `IngestionJob` (`kb_id`/`ingestion_job_id` FKs, `filename`,
  `content_hash`, `size_bytes`, CHECK-constrained `status`
  `pending`/`parsing`/`chunked`/`failed`, capped `error`). Per-document status
  is the partial-failure unit: one bad file (parse error, or zero chunks
  produced) is recorded here as `failed` without aborting the rest of the
  job. Indexed on `ingestion_job_id`.
- `knowledge_chunks` (`KnowledgeChunk`) — one chunk of a `KnowledgeDocument`'s
  parsed text (`document_id`/`kb_id` FKs, `chunk_index`, `text`, optional
  `embedding_json`/`embedding_model`). `embedding_json` is a JSON-encoded
  `List[float]` — same TEXT-column shape as `memories.embedding_json` —
  populated only for `vector`/`hybrid` KBs. Reconstructed into the matching
  `KnowledgeBase` subclass via its `from_chunks(...)` alternate constructor
  at read time (`ui/backend/knowledge_bases.py::resolve_knowledge_base`, see
  `src/bestteam/core/CLAUDE.md`) rather than re-parsing files on every load.
  Indexed on `(document_id, chunk_index)` and `kb_id`. Deleting a KB cascades
  to delete all three of these tables' rows for it
  (`ingestion.delete_kb_ingestion_data`). See
  `docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`.
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
  `auth_type` is `'password'` or `'microsoft_oauth'` (Exchange Online no longer
  accepts basic auth), with `oauth_tenant_id`/`oauth_client_id` holding the
  Entra identifiers — those are identifiers, not secrets, and are stored in the
  clear. **Load-bearing:** `password_encrypted` holds the mailbox password for
  `'password'` and the Entra **client secret** for `'microsoft_oauth'` — one
  encrypted column either way, so there is exactly one place a secret is
  written and exactly one place `ensure_secrets_key_for_stored_credentials`
  has to check at boot. A second secret column would be a second thing to
  forget. `set_email_credentials` assigns every field unconditionally, so
  switching a mailbox between auth types can't leave the previous type's
  fields behind.
- `email_triggers` — one org's autonomous new-mail trigger: opt-in flag +
  target `workflow_name`, UID dedup baseline (`last_uid`/`uidvalidity`),
  the two date-scoped counters (`runs_today`, the operator's runs/day rail,
  and `messages_today`, the customer's daily message cap — both reset off the
  one shared `runs_date`, on purpose, so a rollover can never leave them
  disagreeing about which day it is), overlap guard
  (`last_run_id`), and health (`last_checked_at`/`last_error`). Unique
  `org_id` — at most one auto-running team per org. CRUD in
  `db/email_triggers.py`; poll-state mutations in `ui/backend/email_trigger.py`.
- `inbox_events` (`InboxEvent`) — the durable per-message ledger the email
  poller writes (Phase 1). Detection records one `pending` row per detected
  message **in the same commit that advances `email_triggers.last_uid`**, which
  is the whole point: before this, the cursor advanced and the work existed
  only inside a thread-pool submission, so a process killed in that window
  consumed mail nothing ever ran. A run then *claims* rows (one atomic
  `UPDATE ... WHERE status='pending'`), and the claimed rows' `external_id`s
  are its batch — batching is a claim policy now, not a coupling.
  Identity is `UniqueConstraint(org_id, connector_type, mailbox_identity,
  mailbox_generation, external_id)`. `mailbox_generation` (the IMAP
  UIDVALIDITY) is **in** the key because a UID is only meaningful within one:
  after a mailbox rebuild, UID 7 is a different message and must not look like
  a duplicate. It is `""` and **never NULL** — SQLite treats NULLs as distinct
  in a UNIQUE constraint, so a nullable column would silently disable dedup for
  any connector with no generation concept. Because the key makes re-insertion
  a no-op, the cursor degrades from a correctness requirement to a performance
  optimisation: losing it re-examines messages, never reprocesses them.
  `status` is `pending | claimed | done | failed | filtered`, and `filtered` is
  written today (Phase 4a): it is a message the pre-LLM filter skipped before
  any model saw it, recorded by the *same* `record_events` call in the *same*
  commit as everything else detected that cycle — filtering chooses a row's
  status, never whether the row exists. `attempts` is
  charged at **dispatch**, never at claim, so a workflow that fails to *build*
  releases its messages penalty-free and retries forever (a broken team config
  must not dead-letter a day of an org's mail). `connector_type`/
  `mailbox_generation`/`external_id` are deliberately connector-neutral for
  Phase 2 (Graph/Gmail). `decision` is written today too, alongside that
  status: the short reason the filter gave (`bulk:list-id`,
  `blocked_sender:*@news.example.com`, `not_allowlisted`, ...), which the
  activity UI renders as a sentence. `release_filtered_event` is the whole
  release path — one `filtered` → `pending` flip that also clears `decision`,
  scoped by `org_id`, so a false positive rejoins the ordinary claim queue with
  no second dispatch path to keep correct. Because a row can therefore become
  claimable with **no new mail having arrived**, `has_pending_events` exists
  next to `claim_events`: a scoped `LIMIT 1` existence check that lets
  `poll_org` go on to dispatch on an otherwise-empty cycle instead of returning
  early and leaving a released message (or a capped backlog) sitting until
  unrelated mail happens to land. CRUD in
  `db/inbox_events.py` (nothing there commits — callers own the transaction
  boundary, since the durability guarantee is the single commit).
  See `docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md`
  and `docs/superpowers/specs/2026-08-17-email-phase-4a-filtering-budgets-design.md`.
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
- `automation_item_results` (`AutomationItemResult`) — one immutable row per
  input item per Run for a vertical solution template (Property Maintenance
  Inbox is the first; Release 1A). Deliberately not a `Case`/work-item table
  (`docs/DECISIONS.md`): no status transitions, no owner, no close action.
  `org_id`/`run_id` FKs, `source_type` (fixed `"email"` today), a
  server-generated `source_key` (never taken from a model's own output --
  see `ui/backend/automation_results.py`), `result_type`, `status`
  (`processed | needs_attention | skipped | error`), `needs_attention`, and a
  length-capped/validated `payload` JSON that never holds a raw email body.
  `UniqueConstraint(run_id, source_key)` makes writing the same item twice
  (e.g. a duplicate completion callback) a no-op; indexed on
  `(org_id, created_at)` and `(org_id, needs_attention, created_at)` for the
  Activity page's summary/Needs-attention queries. `runs.trigger_context`
  (JSON, nullable) and `runs.retry_of_run_id` (migration `c1d2e3f4a5b6`,
  chained after `b8c9d0e1f2a3`) are the companion `Run` columns: the former
  is the server's own record of an autonomous email-triggered run's
  mailbox/UIDVALIDITY/UID batch (set by `email_trigger.py::_start_triggered_run`),
  the latter links a manually-retried run back to the one it retried
  (`email_trigger.py::retry_triggered_run`) -- a retry always inserts a new
  `runs` row, never mutates the original.
- `share_links` — one revocable, anonymous entry point to one deployed team
  (`db/share_links.py`; `workflow_id`/`org_id`/`created_by` FKs, a unique
  random `token`, `active`, optional naive-UTC `expires_at`, `daily_cap`).
  `turns_today`/`turns_date` is the link-wide **aggregate** daily-turn CAS
  (`try_consume_link_turn`) -- `daily_cap` is both the per-session and the
  per-link-per-day ceiling, because a visitor who never stores the session
  cookie would otherwise get a fresh allowance on every request. An active
  link blocks deleting the team it points at (`count_active_share_links`,
  used by `crud.py`).
- `share_sessions` — one anonymous visitor's browser against one
  `share_links` row (`db/share_sessions.py`; unique `session_token`, carried
  in an HMAC-signed cookie by `ui/backend/share_auth.py` -- never a `users`
  row). `turns_today`/`turns_date` is the per-session daily-turn CAS
  (`try_consume_turn`), same shape as `EmailTrigger.runs_today`/`runs_date`.
  Never cross-visible to another session on the same link.
- `share_messages` — one turn of a session's human-readable transcript
  (`db/share_messages.py`; `share_session_id` FK, `turn_number`, `role`
  (`user`|`assistant`), `content`, nullable `run_id` FK to the `runs` row
  that produced an assistant turn). `UniqueConstraint(share_session_id,
  turn_number)` makes a duplicate reply-recording call a no-op. Deliberately
  separate from the replay-formatted string actually sent as a run's `input`
  (see `ui/backend/CLAUDE.md`): this is the clean chat log the visitor UI and
  the org's audit view render. No retention/deletion policy yet — see
  `docs/STATUS.md`, Known issues.
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
  `principal_id` (migration `b8c9d0e1f2a3`, per-row random backfill) is the
  **immutable per-account memory principal** for the deletion-lifecycle: set once
  at creation via `new_principal_id()` and **never rotated** (unlike
  `security_stamp`, which rotates on password reset), so a run's memory
  recall/writes scope to it and a deleted-then-recreated username gets a fresh
  value that can't reach the old account's memory. See `core/memory.py`
  (`principal_id` store dimension + `retired_principals` fence).

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

## Run persistence and history API

`ui/backend/registry.py`'s `RunRegistry` is still the authoritative
in-process **live** layer — an in-flight run's live state is lost on
restart, and it isn't rehydrated from the DB. Since CR-012,
`ui/backend/runtime.py::run_in_background` persists one `runs` row per run
(committed before any usage record and updated to its terminal
status/output), and now also persists every `TraceEvent` as a `trace_events`
row (`TraceEventRecord`, in `seq` order, starting with a synthesized
`run_queued` bookend published to the live registry the same way every other
event is — see `ui/backend/CLAUDE.md`), so `usage_records`/`trace_events`
foreign keys reference a real row and a run's full history survives past its
live view. `GET /api/runs` (org-scoped, filterable by `workflow`/`status`/
`manual`/`since`/`until`, paginated via `limit`/`offset` + `total`, default
page size 50/max 200 — no frontend "load more" yet, so a page beyond the
default is currently only reachable by widening filters) and
`GET /api/runs/{id}/trace` (seq-ordered persisted events for one run) serve
this history; `POST /api/runs/{id}/cancel` requests cooperative cancellation
(`ui/backend/registry.py::request_cancel`/`cancel_requested`, checked in
`run_in_background` between yielded events). Still deferred: rehydrating
`RunRegistry`'s live layer from the DB across restarts, and enabling SQLite
foreign-key enforcement.

## `notifications` / `org_notification_settings` (email Phase 3a)

`notifications` is **one table serving two jobs**: the in-app alert list and
the webhook outbox. That is deliberate -- delivery needs retry state
(`delivery_state`, `delivery_attempts`, `last_delivery_error`) and the list
needs the same rows, so two tables would immediately produce "shown in the app
but never delivered" with no single place to reconcile them.

`delivery_state = "skipped"` means the org configured no webhook. It is a
normal configuration, **not** a failure, and the UI must not present it as one.

`fingerprint` identifies the *problem*, not the occurrence
(`workflow`/`mailbox`/`run_timeout`/`secret_expiry_30`/...). `EmailTrigger`
carries the health state for the first three (`consecutive_faults`,
`alerted_fingerprint`); the secret-expiry sweep has no such row to write to, so
it dedups by querying for an existing notification with that fingerprint
(`has_fingerprint`).

`org_notification_settings` holds one webhook per org. The signing secret is a
Fernet token via `secret_store`, same scheme as mailbox credentials, so the
startup check that refuses to boot when a rotated key can't read stored
credentials covers it too. `set_notification_settings(keep_existing_secret=)`
exists because the API never returns the secret -- an update that omits it must
keep it rather than wipe it.

## `org_retention_settings` + `runs.content_purged_at` (email Phase 3b)

`org_retention_settings` is one row per org: `run_retention_days` (NULL = keep
forever, the default, so an upgrade deletes nothing), plus `last_swept_at` and
`last_purged_count`. Those two are not decoration -- a retention policy whose
job silently stopped is indistinguishable from one that is working, until an
audit, so the UI shows when it last ran and what it took.

`set_retention_days(db, org_id, None)` turns the policy off but **keeps the
row**: the sweep history outlives any one policy value.

`runs.content_purged_at` is what marks a run purged -- never the emptiness of
`input`/`output`, since a genuinely empty output is possible. A purge deletes
that run's `trace_events` rows and empties each `automation_item_results.payload`,
but never touches `usage_records` (non-nullable `run_id`, and it carries the
org's cost history) nor an item result's `status`/`source_key` (those exclude
already-drafted UIDs from a retry -- see `ui/backend/CLAUDE.md`).
`inbox_events` is deliberately never purged: a UID plus the customer's own
mailbox address is not data-subject content, and deleting it would break
`resolve_retry_events`.

## `org_email_filter_settings` + `org_email_budget_settings` (email Phase 4a)

Two more one-row-per-org settings tables, following `org_retention_settings`
and `org_notification_settings` exactly: unique `org_id` FK, no row = the
default policy, and the row is kept when a value is cleared. CRUD in
`db/email_filter_settings.py` / `db/email_budget_settings.py`; neither commits.

`org_email_filter_settings` is the pre-LLM filter's policy: `skip_bulk` (the
built-in `Auto-Submitted`/`Precedence`/`List-Id`/`List-Unsubscribe` header
rules) plus three JSON lists, `sender_blocklist`, `sender_allowlist` and
`subject_blocklist`. **`skip_bulk` defaults to `True` for an org with no row**
-- the one deliberate behaviour change on upgrade, because a safety feature
nobody switches on protects nobody, and it is recoverable: one checkbox turns
it off and every filtered message stays visible and releasable in
`inbox_events`. Sender entries are a full address or `*@domain` and **never a
regular expression** (customer regexes would put catastrophic backtracking in
the poll loop, and no admin could be told why a pattern did not match); the
evaluation order that turns these columns into a decision lives in the pure
`ui/backend/email_filter.py`, not here.

`org_email_budget_settings` is the customer's two caps: `daily_message_cap`
(int) and `monthly_cost_cap` (float). **Both are NULL by default** -- the
opposite default to `skip_bulk`, and for the opposite reason: an upgrade must
never start *refusing* to process a customer's mail because of a limit they
never set. Neither is a stored counter. The day's messages are counted on
`email_triggers.messages_today` (above), and the month's spend is **queried,
never stored** -- `spent_this_month` sums `usage_records.cost_estimate` from
the first instant of the UTC month, which is what `usage_records.org_id` is
denormalised for; a spend column would need its own reset, its own backfill
and its own drift bug. A `SUM` of NULL prices means "nothing priced", not
"nothing spent", which is why `unpriced_models_for_org` and
`unpriced_run_count` exist beside it: the cap is a floor on reality, and the
blind spot is reported rather than hidden. The deployment-wide
`BESTTEAM_TRIGGER_DAILY_CAP` (runs/day) is a separate, operator-owned rail and
is unaffected by either column.
