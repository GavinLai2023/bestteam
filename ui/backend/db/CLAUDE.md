# bestteam — `ui/backend/db/` (persistence layer)

Per-deployment SQLite via SQLAlchemy 2.0 (`pip install 'bestteam[ui]'`).
`db/models.py` defines the schema. Root `CLAUDE.md` for the overview;
`ui/backend/CLAUDE.md` for the API layer that uses it.

**Current schema only.** Reasoning: `docs/DECISIONS.md`. Per-feature narrative:
the dated specs under `docs/superpowers/specs/` and git history.

## Engine and wiring

`db/database.py`: `make_engine(db_path)`, `init_db(engine)`, `session_factory`.
`ui/backend/db_session.py` wires the per-deployment engine (default
`ui/backend/data/bestteam.db`, override `BESTTEAM_DB_PATH`) and the `get_db()`
dependency.

- `":memory:"` uses a `StaticPool` so all connections share one database —
  needed for tests and dry runs.
- A file engine sets **`PRAGMA journal_mode=WAL`** on every connection, so
  readers aren't blocked by the one writer the run workers / ingestion / poller /
  requests take turns being. ⚠️ The `-wal`/`-shm` siblings are why
  `scripts/backup-db.sh` goes through the **online backup API**, not a file copy.
- **SQLite foreign-key enforcement is off** — several notes below depend on
  knowing that nothing catches a dangling FK for you.

## Org multi-tenancy

`organizations` (`db/orgs.py`) — the `default` org is seeded at bootstrap, so a
single-customer deployment just has one. Org-owned tables carry a nullable
`org_id` FK; `users.org_id IS NULL` = platform operator; `skills.org_id IS NULL`
= platform built-in visible to every org.

The five named component tables swap a global `unique(name)` for a composite
`UniqueConstraint("org_id", "name")`. Migration `b7c8d9e0f1a2` guards every op by
inspection (`db_session` runs `create_all` at import, so `organizations` may
already exist) and backfills only NULL `org_id`s: non-admin users and org-owned
rows → `default`; admins and built-in skills stay NULL.

## Components and versioning

- **`knowledge_bases` / `skills` / `pipelines`** — each row's `config` is a JSON
  `raw` dict. `pipelines.status` is CHECK-constrained to
  `draft`/`ready_for_testing`/`deployed`; **only `deployed` rows are runnable or
  listed**. The migration backfilled every pre-existing non-`deployed` row to
  `deployed`, so upgrading doesn't retroactively break runnable pipelines.

  ⚠️ **A migrated database keeps old constraint names** — `ck_workflows_status`,
  `uq_workflow_versions_…`, `uq_workflow_dependencies_…`. The
  `workflows`→`pipelines` rename (`o2p3q4r5s6t7`) deliberately doesn't rebuild
  named constraints: SQLite constraint names are diagnostic labels, never queried
  against, and a fresh database gets the new names from `create_all()`. Don't
  "fix" this.

  ⚠️ **`pipelines.active` is the customer's reversible pause and is NOT a
  fourth `status`** — a paused team is still `deployed` (it has a published
  version), it just must not run. False is enforced at every entry point:
  `GET /api/pipelines`, `POST /api/runs`, `build_trigger_pipeline`, the
  trigger-enable route and `share_chat`'s pipeline resolution. Migration
  `v9w0x1y2z3a4`, `server_default="1"`, so an upgrade pauses nothing.

  A `pipelines` row is the **stable team head**: `current_version_id` points at
  the latest immutable `pipeline_versions` snapshot and `config` mirrors it.
  Deploy appends a version via `publish_pipeline_version` — it never overwrites
  history in place.

- **`skill_versions`** — immutable snapshots appended by `publish_skill_version`
  on every save; `skills` is the stable library head. Migration `c4d5e6f7a8b9`
  backfills every existing skill as v1 without changing content, and adds the
  same FKs on upgraded databases that `create_all` produces for fresh ones
  (including retry repair when a column exists without its constraint).

- **`pipeline_versions`** — immutable published snapshots (`id`, `pipeline_id`,
  `version_number`, `config`, `created_by`, `created_at`;
  `(pipeline_id, version_number)` unique). **A row is never updated after
  insert.** This freezes the inline config blob; referenced skills freeze through
  the dependency's `resource_version_id`. Standalone KBs and models are still
  resolved by name at load.

  Deleting a pipeline head refuses (`409`) while any `Run` records one of its
  versions — deletion would remove version history those runs reference (**no DB
  cascade, FK enforcement is off**), so provenance is preserved. A never-run head
  deletes cleanly, cascading its versions and nulling any
  `builder_sessions.pipeline_id` (self-heals on next deploy), under
  `component_mutation_lock`. **Known gap**: an in-flight run whose `runs` row is
  written by the worker *after* dispatch isn't seen by the delete guard, so
  deleting a pipeline at the instant a run starts can dangle that run's
  provenance pointer — closed only by soft-delete/archive.

- **`pipeline_dependencies`** — one typed row per (published version,
  skill|standalone-KB): `resource_kind`, `resource_name`, `resource_id` (the
  resolved id), and `resource_version_id` (the immutable `skill_versions.id`, for
  skills). Written once at deploy by `record_version_dependencies`, resolving
  names **exactly as the loader does**: an org skill shadows a platform built-in;
  KBs are org-scoped; a built-in tool, email tool or inline KB is not a dep — and
  an inline KB shadows a *same-named* standalone one, so the standalone isn't
  recorded when the pipeline defines its own.

  The skill/KB `DELETE` guard queries these by `resource_id` for the **current**
  version (`pipelines_referencing`) rather than scanning JSON — the stable id
  makes the platform-built-in cross-org case fall out without an all-orgs scan.
  **Skill dependencies are immutable**: editing a skill or creating an org
  override never rewrites a deployed team; redeploy is the explicit opt-in.
  Model/tool deps and standalone-KB content pinning remain deferred.

- **`agents` / `teams`** — **removed** (migration `57b13700d5df`). Nothing read
  them: a pipeline carries agents/teams inline in its own `config`, and
  `_build_pipeline` accepts only `extra_tools`/`extra_skills`. Writable CRUD
  existed historically, so the drop migration **refuses if either table holds
  rows** and only drops when empty — a drop is not data-reversible.

## Knowledge-base ingestion

- **`knowledge_ingestion_jobs`** (`IngestionJob`) — one async ingestion run.
  `status` CHECK-constrained `queued`/`running`/`completed`/`failed`; **a KB's
  live document set is always its most recent `completed` job's rows — the
  `completed` flip IS the atomic swap** for this path (unlike the legacy
  file-based `CURRENT`-pointer swap). `version` matches the on-disk
  version-directory name, for traceable job↔directory correspondence.

  ⚠️ **`kb_type`/`embedding_model`/`chunk_size`/`chunk_overlap` record the shape
  this job's chunks were actually ingested under, and the read path resolves from
  THESE, never from `knowledge_bases.config`** — `config` advances to the new
  spec at upload-dispatch time while the previous generation is still live. A
  re-upload changing type would otherwise `json.loads` a NULL `embedding_json`,
  and one changing only the embedding model would silently query a mismatched
  vector space. `chunk_size`/`chunk_overlap` (migration `r5s6t7u8v9w0`, nullable)
  are there for the same reason: incremental ingestion carries an unchanged
  document's chunks forward only if this job would have cut them the same way,
  and `config` can't answer that either. **NULL = pre-column job = not reusable**
  (`_carryable`), so an upgrade re-embeds once rather than reusing on a guess.

  Indexed `(kb_id, status, completed_at)` for the "most recent completed job"
  query.

- **`knowledge_documents`** — one uploaded file's outcome within a job
  (`content_hash`, `size_bytes`, CHECK-constrained
  `pending`/`parsing`/`chunked`/`failed`, capped `error`). ⚠️ **`content_hash` is
  read, not merely recorded** — it's what `_reusable_documents` matches on to
  carry an unchanged document's chunks and embeddings forward. Per-document
  status is the partial-failure unit: one bad file is `failed` without aborting
  the job. Indexed on `ingestion_job_id`.

- **`knowledge_chunks`** — one chunk (`chunk_index`, `text`, optional
  `page`/`heading`, optional `embedding_json`/`embedding_model`).
  `embedding_json` is a JSON-encoded `List[float]` (same TEXT shape as
  `memories.embedding_json`), populated only for `vector`/`hybrid`.
  `page`/`heading` are what a retrieval cites beyond the filename — `page` for a
  PDF (chunked per page, so exact), `heading` for Markdown (approximate). Both
  nullable; **no backfill**, since either value can only be recovered by
  re-parsing, which a re-upload already does. Reconstructed via `from_chunks(...)`
  at read time rather than re-parsing files on every load. Indexed
  `(document_id, chunk_index)` and `kb_id`.

- **`run_knowledge_generations`** — one row per (run, ingestion job): *this run's
  trace names chunk/document ids from this generation*. Written by
  `run_in_background` from a KB tool's `tool_completed` the moment it arrives (a
  cancelled run has still read the generation). Read by
  `_prune_old_ingestion_versions`: **a completed job outside the newest-two window
  that some run references keeps its job/document/chunk rows with
  `embedding_json` set NULL and its version directory deleted** — an audit
  resolves a chunk id to text, page, heading and filename, never a vector.
  Released by `retention.purge_run` (with the trace; deliberately **not** in
  `PURGED_FIELDS`, being an index over exported content) and by
  `delete_kb_ingestion_data`. No backfill. Spec:
  `2026-08-24-kb-generation-audit-retention-and-restore-design.md`.

Deleting a KB cascades to all four tables (`delete_kb_ingestion_data`).

## Email

- **`org_email_credentials`** — one org's mailbox (unique `org_id`; IMAP
  host/port/username + encrypted password + optional drafts folder). The password
  is a Fernet token (`secret_store`), never plaintext. `auth_type` is `'password'`
  or `'microsoft_oauth'`, with `oauth_tenant_id`/`oauth_client_id` in the clear —
  those are identifiers, not secrets.

  ⚠️ **Load-bearing: `password_encrypted` holds the mailbox password for
  `'password'` and the Entra client secret for `'microsoft_oauth'`** — one
  encrypted column either way, so there is exactly one place a secret is written
  and one place `ensure_secrets_key_for_stored_credentials` has to check at boot.
  A second secret column would be a second thing to forget.
  `set_email_credentials` assigns **every field unconditionally**, so switching
  auth types can't leave the previous type's fields behind.

- **`email_triggers`** — one org's autonomous trigger: opt-in flag, target
  `pipeline_name`, UID baseline (`last_uid`/`uidvalidity`), overlap guard
  (`last_run_id`), health (`last_checked_at`/`last_error`/`last_error_kind`),
  and two date-scoped counters. ⚠️ **`runs_today` (the operator's runs/day rail)
  and `messages_today` (the customer's daily message cap) reset off the ONE
  shared `runs_date`, on purpose**, so a rollover can never leave them
  disagreeing about which day it is. Unique `org_id` — at most one auto-running
  team per org.

- **`inbox_events`** — the durable per-message ledger. Detection records one
  `pending` row per detected message **in the same commit that advances
  `last_uid`**; before this, the cursor advanced while the work existed only
  inside a thread-pool submission, so a process killed in that window consumed
  mail nothing ever ran. A run then *claims* rows (one atomic
  `UPDATE ... WHERE status='pending'`) — **batching is a claim policy now, not a
  coupling.**

  Identity is `UniqueConstraint(org_id, connector_type, mailbox_identity,
  mailbox_generation, external_id)`. ⚠️ **`mailbox_generation` (the IMAP
  UIDVALIDITY) is IN the key** because a UID is only meaningful within one: after
  a rebuild, UID 7 is a different message and must not look like a duplicate. It
  is `""` and **never NULL** — SQLite treats NULLs as distinct in a UNIQUE
  constraint, so a nullable column would silently disable dedup for any connector
  with no generation concept. Because re-insertion is a no-op, the cursor
  degrades from a correctness requirement to a performance optimisation: losing
  it re-examines messages, never reprocesses them.

  `status` is `pending | claimed | done | failed | filtered`. `filtered` is a
  message the pre-LLM filter skipped, recorded by the *same* `record_events` call
  in the *same* commit as everything else that cycle — **filtering chooses a
  row's status, never whether the row exists.** `decision` holds the short reason
  (`bulk:list-id`, `blocked_sender:*@news.example.com`, `not_allowlisted`).

  **`attempts` is charged at dispatch, never at claim**, so a pipeline that fails
  to *build* releases its messages penalty-free — a broken team config must not
  dead-letter a day of an org's mail.

  `release_filtered_event` is the whole release path: one `filtered` → `pending`
  flip that also clears `decision`, scoped by `org_id`, so a false positive
  rejoins the ordinary claim queue with no second dispatch path to keep correct.
  Because a row can become claimable **with no new mail having arrived**,
  `has_pending_events` sits next to `claim_events` — a scoped `LIMIT 1` existence
  check letting `poll_org` dispatch on an otherwise-empty cycle.

  ⚠️ **Both take `mailbox_identity` and `mailbox_generation` as REQUIRED
  arguments and filter on them.** Both columns were written from the beginning
  and read by nothing, so a mailbox replaced or rebuilt mid-backlog left `pending`
  rows the next quiet cycle happily claimed for the *new* mailbox. Required rather
  than optional because the defect is that a caller could omit the mailbox.

  Such rows are marked terminal by `abandon_superseded_events` — every `pending`
  **or `filtered`** row not on the current mailbox (and optionally not the current
  generation), expressed that way so it needs no memory of the previous identity.
  `filtered` is in scope because release is a bare flip: a superseded row left
  `filtered` stays in the release list, reports `released: true`, and is then
  unclaimable for ever. `claimed` rows are left alone — one belongs to a run that
  will complete, be released by the stale-run watchdog, or be released at startup.

  **CRUD in `db/inbox_events.py` commits nothing** — callers own the transaction
  boundary, since the durability guarantee *is* the single commit.

- **`org_email_filter_settings`** — `skip_bulk` plus three JSON lists
  (`sender_blocklist`, `sender_allowlist`, `subject_blocklist`). **`skip_bulk`
  defaults to `True` for an org with no row** — the one deliberate behaviour
  change on upgrade, recoverable by one checkbox, and every filtered message stays
  visible and releasable. Sender entries are a full address or `*@domain` and
  **never a regular expression**. The evaluation order lives in the pure
  `email_filter.py`, not here.

- **`org_email_budget_settings`** — `daily_message_cap` (int) and
  `monthly_cost_cap` (float). ⚠️ **Both NULL by default — the opposite default to
  `skip_bulk`, for the opposite reason**: an upgrade must never start *refusing*
  to process a customer's mail because of a limit they never set. Neither is a
  stored counter: the day's messages are on `email_triggers.messages_today`, and
  the month's spend is **queried, never stored** (`SUM(usage_records.cost_estimate)`
  from the first instant of the UTC month — what `usage_records.org_id` is
  denormalised for). A spend column would need its own reset, backfill and drift
  bug. **A `SUM` of NULL prices means "nothing priced", not "nothing spent"**,
  which is why `unpriced_models_for_org`/`unpriced_run_count` exist beside it.

## Runs and results

- **`runs` / `trace_events`** — the persisted replacement for `RunRegistry`'s
  in-memory state. `username` records who started the run (audit-only; ownership
  is org-level via `org_id`). `pipeline_version_id` records the exact immutable
  snapshot a production run executed — NULL for sandbox test-runs (they run the
  session spec, not a published version) and pre-migration rows.
  `trigger_context` (JSON) is the server's record of an autonomous run's
  mailbox/UIDVALIDITY/UID batch; `retry_of_run_id` links a retry back to the run
  it retried — **a retry always inserts a new row, never mutates the original**;
  `diagnostic_of_run_id` does the same for an admin diagnostic re-run.
  ⚠️ **`output` and `internal_error` are two different audiences for one
  failure**: a provider's own text can name the model, the provider and the
  account's billing state, so `output` carries the customer's sanitized copy
  and `internal_error` the operator's real one. It is served only to a platform
  admin, and it is **purged as content but never exported** — hence
  `retention.PURGED_OPERATOR_FIELDS`, separate from `PURGED_FIELDS`, whose
  contract is that the export covers everything the purge clears.

- **`automation_item_results`** — one immutable row per input item per Run for a
  vertical solution template. **Deliberately not a `Case`/work-item table** — no
  status transitions, no owner, no close action. `source_type` (fixed `"email"`),
  a **server-generated** `source_key` (never taken from a model's output),
  `result_type`, `status` (`processed | needs_attention | skipped | error`),
  `needs_attention`, and a length-capped `payload` that **never holds a raw email
  body**. `UniqueConstraint(run_id, source_key)` makes writing the same item twice
  a no-op. Indexed `(org_id, created_at)` and
  `(org_id, needs_attention, created_at)`.

- **`builder_sessions`** — the wizard state machine. `status` ∈
  `intent | requirements | spec | solution | testing | deployed`.
  `requirements_json`/`specification_json` hold the two agents' structured
  outputs; `feedback_history` is an append-only JSON list. `pipeline_id` is the
  stable head this session deploys to — set on first deploy so a redeploy
  versions the same head and two same-named sessions converge on one team.

### History API

`run_in_background` persists one `runs` row per run (committed **before** any
usage record, updated to its terminal status/output) and every `TraceEvent` as a
`trace_events` row in `seq` order, starting with a synthesised `run_queued`
bookend. So `usage_records`/`trace_events` FKs reference a real row and history
survives past the live view.

`GET /api/runs` (org-scoped; filter `pipeline`/`status`/`manual`/`since`/`until`;
`limit`/`offset` + `total`, default 50 / max 200 — **no frontend "load more" yet,
so a page beyond the default is only reachable by widening filters**),
`GET /api/runs/{id}/trace`, `POST /api/runs/{id}/cancel`.

⚠️ `RunRegistry` remains the authoritative in-process **live** layer and **is not
rehydrated from the DB on restart** — an in-flight run's live state is lost.
Still deferred alongside enabling SQLite FK enforcement.

## Sharing

- **`share_links`** — one revocable anonymous entry point to one deployed team
  (unique random `token`, `active`, optional **naive-UTC** `expires_at`,
  `daily_cap`). `turns_today`/`turns_date` is the link-wide **aggregate** CAS.
  ⚠️ **`daily_cap` is both the per-session and the per-link-per-day ceiling**,
  because a visitor who never stores the session cookie would otherwise get a
  fresh allowance on every request. An active link blocks deleting the team.
- **`share_sessions`** — one visitor's browser against one link (unique
  `session_token`, carried in an HMAC-signed cookie — **never a `users` row**).
  `turns_today`/`turns_date` is the per-session CAS, same shape as
  `EmailTrigger.runs_today`. Never cross-visible to another session on the link.
- **`share_messages`** — one turn of the transcript (`turn_number`, `role`,
  `content`, nullable `run_id`). `UniqueConstraint(share_session_id, turn_number)`
  makes a duplicate reply-recording call a no-op. **Deliberately separate from the
  replay-formatted string actually sent as a run's `input`**: this is the clean
  chat log the visitor UI and the org's audit view render. No retention policy yet.
- **`feedback`** — one defect report or suggestion for the **platform operator**
  (`db/feedback.py`). ⚠️ **`org_id` is provenance, never ownership** — there is no
  org-facing read surface. Exactly one of `submitted_by` (users FK) /
  `share_session_id` (share_sessions FK, indexed — the share route's per-day cap
  counts on it) is set, enforced by `create_feedback`, **not by a CHECK**. `kind`
  (`defect`|`suggestion`) and `status` (`new`|`acknowledged`|`resolved`|
  `dismissed`, default `new`) are CHECK-constrained; plus `admin_note` and a
  whitelisted `context` JSON. No delete verb, no retention policy — the row is the
  record. Migration `u8v9w0x1y2z3`.

## Accounts

**`users`** — `is_admin` gates the Advanced config and Memory pages; `org_id`
(NULL = platform operator) scopes everything else. Both are granted only via the
operator CLI — **there is no public registration.**

⚠️ **`is_admin` and a non-NULL `org_id` are mutually exclusive**:
`set_admin_status` refuses to promote org members, and the API guards ignore the
flag on org-bound rows anyway. Usernames stay globally unique across orgs (JWT
`sub` + memory keying).

⚠️ **One member per org is a schema invariant** — a partial unique index
`uq_users_org_id_not_null` on `org_id WHERE org_id IS NOT NULL`; platform
operators (NULL) are excluded, so there can be many. That migration **refuses if
an upgraded DB already has multi-member orgs** (names them; never auto-deletes).

`principal_id` is the **immutable per-account memory principal**: set once at
creation and **never rotated** (unlike `security_stamp`, which rotates on
password reset), so a deleted-then-recreated username gets a fresh value that
can't reach the old account's memory. See `core/memory.py`'s `retired_principals`
fence.

`security_stamp` is a random per-account credential generation embedded in every
token and WS ticket — an immutable random value, **not a timestamp**, so there's
no ordering race.

## Catalogue and metering

- **`model_catalog`** — maps a model `spec` to `display_name`, complexity `tier`
  (`fast`/`balanced`/`advanced`), and per-1K-token input/output pricing. Seeded
  with `DEFAULT_MODEL_CATALOG` on first use of the production engine (idempotent).
  ⚠️ **A fourth tier, `"embedding"`, marks an embedding model** — it is here only
  so `record_usage` can price a KB's embedding spend, and `list_chat_entries()`
  excludes it from every surface offering a *chat* model. None is seeded, since
  prices depend on the provider.

- **`usage_records`** — one metered LLM/embedding call plus a `cost_estimate`
  from `model_catalog` where the spec matches. **One ledger for every kind of
  spend an org incurs**, which is what lets the monthly cap be a single `SUM` over
  `org_id`.

  ⚠️ **`run_id` is nullable and a nullable `ingestion_job_id` sits beside it, so a
  row names one of THREE sources:**

  | Source | `run_id` | `ingestion_job_id` |
  |---|---|---|
  | A run (incl. a KB's query-time spend, riding `agent_completed.usage`) | set | NULL |
  | Ingestion — one `agent="kb:ingest"` row per job | NULL | set |
  | Ad-hoc `agent="kb:search"` from "Try a search" | NULL | NULL |

  Consumers that key on runs (`run_analytics_api.py`, `GET /api/runs/{id}`,
  `unpriced_run_count`) filter or count by run id, so NULL-`run_id` rows drop out
  naturally; the monthly `SUM(cost_estimate) WHERE org_id` deliberately includes
  them.

  ⚠️ **`ingestion_job_id` is a provenance label, not a joinable key.** Both
  generation pruning and KB deletion delete job rows, and the usage row survives
  them **on purpose** — the same "keep the accounting" rule a retention purge
  follows: an org's spend history must not change retroactively because it
  deleted a knowledge base.

## Alerting and retention settings

- **`notifications`** — **one table serving two jobs**: the in-app alert list and
  the webhook outbox. Deliberate — delivery needs retry state (`delivery_state`,
  `delivery_attempts`, `last_delivery_error`) and the list needs the same rows, so
  two tables would immediately produce "shown in the app but never delivered" with
  no single place to reconcile.

  ⚠️ **`delivery_state = "skipped"` means the org configured no webhook. It is a
  normal configuration, NOT a failure, and the UI must not present it as one.**

  `fingerprint` identifies the *problem*, not the occurrence
  (`workflow`/`mailbox`/`run_timeout`/`secret_expiry_30`/…). `EmailTrigger`
  carries the health state for the first three; the secret-expiry sweep has no
  such row, so it dedups via `has_fingerprint`.

- **`org_notification_settings`** — one webhook per org. The signing secret is a
  Fernet token via `secret_store`, same scheme as mailbox credentials, so the
  boot check that refuses to start when a rotated key can't read stored
  credentials covers it too. `set_notification_settings(keep_existing_secret=)`
  exists because the API never returns the secret — an update omitting it must
  keep it rather than wipe it.

- **`org_retention_settings`** — `run_retention_days` (NULL = keep forever, the
  default, so an upgrade deletes nothing), plus `last_swept_at` and
  `last_purged_count`. Those two are **not decoration**: a retention policy whose
  job silently stopped is indistinguishable from one that is working, until an
  audit. `set_retention_days(db, org_id, None)` turns the policy off but **keeps
  the row** — the sweep history outlives any one policy value.

  ⚠️ **`runs.content_purged_at` is what marks a run purged — never the emptiness
  of `input`/`output`**, since a genuinely empty output is possible. A purge
  deletes that run's `trace_events` and empties each
  `automation_item_results.payload`, but never touches `usage_records` (it carries
  the org's cost history and its rows name the run) nor an item result's
  `status`/`source_key` (those exclude already-drafted UIDs from a retry).
  **`inbox_events` is deliberately never purged**: a UID plus the customer's own
  mailbox address is not data-subject content, and deleting it would break
  `resolve_retry_events`.

These settings tables all follow one shape: unique `org_id` FK, no row = the
default policy, the row kept when a value is cleared, and **no CRUD function
commits** — callers own the transaction.
