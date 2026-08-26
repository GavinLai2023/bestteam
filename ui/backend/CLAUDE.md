# bestteam — `ui/backend/` (FastAPI backend)

Directory-scoped notes. Root `CLAUDE.md` has the project overview and commands;
`db/CLAUDE.md` has the persistence schema; `ui/frontend/CLAUDE.md` the React app.

**This file records current invariants, not history.** The reasoning behind a
design lives in `docs/DECISIONS.md`; the full narrative of how each feature was
built lives in its dated spec under `docs/superpowers/specs/` (cited per section
below) and in git history. Keep this file short — it auto-loads in full.

## Org multi-tenancy (row-level isolation)

Spec: `specs/2026-07-15-org-multi-tenancy-design.md`. Rules every endpoint follows:

- Org-owned rows carry `org_id`. Org users see only their own org's data plus
  platform built-ins (`skills.org_id IS NULL`) and, when
  `BESTTEAM_DEMO_PIPELINES` is on, the global YAML demos.
- **Cross-org access is a 404, never a 403** — existence is never revealed. The
  WS stream closes 4404. This includes an explicit `run_id` passed to a
  *list* filter (`GET /api/runs?run_id=`, `GET /api/automation-results?run_id=`):
  both 404 up front rather than returning an empty list, which would still
  distinguish "not yours" from "doesn't exist".
- Scoping is centralised: `get_current_org`, `load_skills(db, org_id)` (an org's
  own shadows a same-named built-in), `load_knowledge_base_tools(..., org_id=)`,
  org-filtered crud/builder/main queries. Pipeline cache key is
  `(org_id, name)`; YAML demos cache under `(None, name)`.
- Component names are unique per `(org_id, name)`. KB uploads live at
  `data/knowledge_base_uploads/<org_id>/<name>` (legacy un-prefixed dirs still
  work — KB configs embed absolute paths).
- **Platform admins are org-less accounts.** `set_admin_status` refuses to
  promote org members; `get_current_admin` and the run GET/stream passthrough
  require `is_admin AND org_id IS NULL`. An org-bound `is_admin` is never honoured.
- Admin surfaces (`/api/config`, `/api/memory`) are platform-wide: lists label
  each item's org and take `?org=`; item routes require explicit `?org=<name>`.
- Runs and `usage_records` carry `org_id` (denormalised — the future billing
  dimension). Runs also persist `username` (audit only; ownership stays org-level).
- Memory is keyed by globally-unique username — no org dimension needed.
- Test net: `tests/test_org_isolation.py` plus per-surface tests in
  `test_crud_api`/`test_ws_stream`/`test_builder_api`.

### Per-org email

Each org connects its own mailbox (`admin set-email <org>`), stored encrypted in
`org_email_credentials` (password via `secret_store` / `BESTTEAM_SECRETS_KEY`, a
key distinct from the JWT one). `email_tools.load_email_tools(db, org_id)` is
merged into `extra_tools` at every pipeline-build site, overriding `REGISTRY`'s
env-based `email_*` by name. `_dependency_freshness` includes
`OrgEmailCredential`, so rotating a mailbox invalidates that org's cached
pipelines. **Startup refuses to boot if stored credentials exist but the secrets
key can't decrypt them.**

**There is exactly ONE credential→connector implementation, and that is
load-bearing.** The seam is entirely in `email_tools.py`:
`token_provider_for(auth_type, ...)` (the one place `microsoft_oauth` means
"app-only token, not a LOGIN"), `build_imap_backend(...)` (the one
`_ImapBackend` construction, hence the one place `restrict_to_public=True` is
set), and `build_backend_for_credential(cred, secret)` /
`build_org_imap_backend(db, org_id)`. A second implementation once existed in
`email_trigger.py` and silently broke every OAuth org's polling — do not add
another. `org_settings.py` validates *unsaved* requests and so delegates to the
primitives rather than mirroring them.

Self-service: `org_settings.py` (`/api/org/email`, `get_current_org`, no admin
role) — GET status (never the password), PUT set/rotate, POST `/test`, DELETE.
Customer-supplied hosts are SSRF-checked (`http_client.check_host_allowed`). For
`microsoft_oauth` the host is forced server-side to `outlook.office365.com`.
`_mailbox_problem` fetches the token as its **own step before connecting**, so a
credential problem is distinguishable from a mailbox-access problem — they have
different fixes and Microsoft's error text can't tell them apart. It also checks
`check_drafts_writable()` (SELECT read-write, reject `[READ-ONLY]`), because
every reply is an APPEND and a login-only test passed mailboxes that then failed.

Process-wide `BESTTEAM_EMAIL_*` env vars remain the single-mailbox path for
SDK/CLI and single-org deployments, and are **refused** on a multi-org
deployment (`db/orgs.py::ensure_email_single_org`, at startup and in `create-org`).

## Autonomous email trigger (`email_trigger.py`, `email_trigger_api.py`)

Opt-in per org. An asyncio poller from `main._lifespan` checks each enabled org's
mailbox every `BESTTEAM_TRIGGER_POLL_SECONDS` (default 120) and starts ONE run
per cycle, attributed to the sentinel username `email-trigger`. Dedup is a
per-org IMAP UID baseline in `email_triggers` — **never UNSEEN**, since the
toolkit never marks mail seen. The baseline is set to current max UID at enable
time so the backlog never triggers.

Specs: `2026-08-17-email-phase-{0-hardening,1-inbox-events,4a-filtering-budgets}-design.md`,
`2026-08-22-email-poller-oauth-and-claim-scoping-design.md`.

**Single-process is enforced, not assumed.** `_lifespan` takes an exclusive OS
lock on `<db>.lock` (`process_lock.py`) before the startup sweeps, so a second
process refuses to start rather than running a second poller. `":memory:"` skips
it. The claim is atomic, but `RunRegistry` is in-process, so the overlap guard
and cancellation still assume one process. Real scale-out is blocked on Postgres
(`make_engine` hardcodes SQLite and takes a path, not a URL).

### Detection vs. execution

`poll_org` records one `inbox_events` row per detected message **in the same
commit that advances `last_uid`**; `_start_triggered_run` then *claims* up to
`BESTTEAM_TRIGGER_BATCH_SIZE` of them. Consequences: `last_uid` is **not**
written by the CAS (detection already advanced it), and "no message was
consumed" is an assertion about event status, not the cursor.

**`attempts` is charged at dispatch, never at claim** — so a build failure is
penalty-free and a broken team config retries instead of dead-lettering an org's
whole day of mail. `BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS` (default 3) bounds
infrastructure retries; on exhaustion the message is dead-lettered and
`trigger.last_error` says so. Detection is bounded at `BATCH_SIZE * 10` rows per
cycle so a long outage can't open an unbounded transaction.

Failure handling splits by class — **the rule to keep in mind when touching any
of it**:

- **Infrastructure-class** (dispatch failure, stale-run watchdog, build failure,
  trigger disabled mid-build): no model spend, messages are innocent, so
  `release_events` hands them back. These paths never reach `runtime`, so they
  release at the site.
- **Pipeline-class** (anything reaching `runtime._maybe_normalize` — the model
  actually ran): `_safe_complete_inbox_events` marks messages with a proven
  draft `done` and the rest `failed`, awaiting human retry.

**A claim is scoped to the mailbox AND its generation.** `claim_events` and
`has_pending_events` take `mailbox_identity` and `mailbox_generation` as
*required* keyword args — required, not defaulted, because the defect they close
is a caller omitting the mailbox entirely. Otherwise an org that replaced or
rebuilt its mailbox kept `pending` rows from the previous one and handed them to
a run bound to the new mailbox (against a rebuilt mailbox UID 7 exists and is a
different message). `abandon_superseded_events` retires such rows and is called
at **all three** moments the mailbox or generation can change:
`on_mailbox_saved` (every save, not only identity changes — a
disconnect-then-connect arrives with `prior_identity=None`), the enable in
`email_trigger_api` (the only site knowing mailbox *and* generation), and
`poll_org`'s UIDVALIDITY re-baseline (which also names the count on
`last_error`, since a rebuild is not something the customer did). `filtered` rows
are retired alongside `pending` ones, or a released false positive would answer
`released: true` and then be unclaimable for ever.

**Every orphaned claim is released at startup.** `runtime.fail_interrupted_runs`
resolves `running` rows and releases their claims; `_release_orphaned_claims`
sweeps whatever is *still* `claimed` (orphaned by definition — the executor is
per-process). Deliberately not a lease with a scavenger: the process boundary
*is* the lease, and `_release_stale_run` covers a run that is alive but hung.

### Guards and scoping

- Per-org daily cap `BESTTEAM_TRIGGER_DAILY_CAP` (default 50) — the *operator's*
  deployment-wide rail.
- Platform kill switch `BESTTEAM_TRIGGERS_DISABLED=1`.
- Overlap guard: skips a cycle while the previous triggered run is `running`.
- Per-org try/except, so one org's mail-server failure never stops the loop
  (stored as customer-readable `last_error`).
- `_release_stale_run` releases a run still `running` past
  `BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS` (default 1800, min 60): cooperative
  cancel, mark failed, normalise, record the fault. A run can't be forcibly
  killed, so this makes it non-blocking rather than stopping it.
- `BESTTEAM_TRIGGER_*` values are validated at startup
  (`validate_trigger_env()`), not left to kill the poller mid-loop.

An automatic run is confined to the detected UID batch: an **uncached** pipeline
(`build_trigger_pipeline`) whose email tools are UID-scoped
(`make_email_tools(backend, allowed_uids=)`). State advances only after the
pipeline builds and a durable `runs` row is written. The advance is a
compare-and-swap (`UPDATE ... WHERE enabled = 1` and org active): if the trigger
was disabled or the mailbox replaced between check and commit, the built run is
discarded (`registry.discard`) rather than dispatched at a disconnected mailbox.

**Staleness is re-checked inside the lock.** The overlap-check-through-dispatch
section of both `poll_org` and `retry_triggered_run` is serialised behind a
per-org `threading.Lock` (`_dispatch_lock(org_id)`). The lock alone is
insufficient: an ORM `trigger` loaded before acquisition keeps a stale
`last_run_id`. `_current_last_run_id(db, trigger)` does a fresh column-only
SELECT inside the lock — deliberately **not** `db.refresh(trigger)`, which would
discard the function's own pending daily-cap reset. `_at_daily_cap` re-checks the
same way. Cap advances are atomic (`SET runs_today = runs_today + 1`), never a
Python-level `+=`, which would lose a concurrent increment.

`_start_triggered_run` stamps `Run.trigger_context` (credential id, host,
username, UIDVALIDITY, the exact UID batch, folder, trigger time) — **the
server's own ground truth; never trust a model's claim about which messages it
processed.** Host/username are stamped alongside the credential id because
`set_email_credentials` upserts one row per org, so the row id alone never
changes even when the mailbox is replaced.

### Retry

`retry_triggered_run(db, run_row)` reruns a **failed** (never `completed` — that
may have real mailbox side effects) triggered run over its original UID batch as
a brand-new `Run` (`retry_of_run_id` set, history untouched). It revalidates
host/username against `trigger_context` (not just UIDVALIDITY — a replaced
mailbox could share one), that the mailbox decrypts and connects, the pipeline
builds, the cap isn't hit, and the overlap guard is clear; any failure raises
`RetryError` with a customer-facing message. Exposed as `POST /api/runs/{id}/retry`.

It excludes UIDs already drafted (`already_drafted_uids`) — `email_draft_reply`
has no dedup of its own. That check spans the whole **retry family**
(`_retry_family_run_ids`: back to the root, then forward to everything
reachable), because a family is a tree, not a line — the original can be retried
more than once, creating sibling branches. Evidence has three sources:
`automation_item_results`, `_trace_confirmed_uids` (persisted `tool_completed`
events with `outcome == "draft_created"` — evidence *every* run records, so this
no longer depends on the property-maintenance template), and
`_mailbox_drafted_uids` (a best-effort Drafts search on the
`X-BestTeam-Source-Key` header stamped on every draft — the only way to see a
draft that was APPENDed but whose trace event never persisted; a scan failure is
logged and ignored, it must never block a legitimate retry).

The narrowed `retry_uids` — not the original batch — is what reaches
`build_trigger_pipeline`, the new `trigger_context["uids"]`, **and** the retry
input (`_trigger_input(retry_uids)`). Reusing the original input text would
instruct the agent to work on messages its own scoped tools then reject.
Narrowing `trigger_context` too is what stops `normalize_run_result` synthesising
spurious error rows for intentionally-excluded UIDs.

Known holes, recorded so they aren't rediscovered as bugs: `retry_triggered_run`
**enforces neither per-org budget cap** and doesn't advance `messages_today`; the
submit-failure path **double-charges `messages_today`** (the CAS commits,
`release_events` returns the messages, a later cycle charges again); and a
`completed` run whose output failed *normalisation* has no retry path at all.

### Pre-LLM filtering (`email_filter.py`)

A **pure** evaluator — no I/O, clock or DB, so every rule is testable by calling
a function. `evaluate(headers, settings) -> Optional[str]` plus `describe()`.
Storage in `db/email_filter_settings.py`. Reasoning: `docs/DECISIONS.md`.

**The evaluation order is fixed, because the order is the behaviour** (spelled
out in `evaluate`'s own docstring):

1. `sender_blocklist` matches → `blocked_sender:<pattern>`
2. `sender_allowlist` non-empty and no match → `not_allowlisted`
3. `subject_blocklist` matches → `blocked_subject:<term>`
4. `skip_bulk` and a bulk header present → `bulk:<header>`
5. otherwise `None`, process it

Blocklist outranks allowlist deliberately, and **the allowlist does not exempt a
sender from the bulk check**. Patterns are exactly two forms (full address,
`*@domain`), matched case-insensitively against the address parsed from `From`
and **never the display name** (attacker-chosen free text). No regular
expressions. Bounded at 200 chars per pattern — the poll loop reads every one.

**Filtering changes a row's `status`, never whether the row is inserted.** It
runs inside `poll_org`'s detection block between `check_mailbox` and
`record_events`; the durability guarantee (the commit consuming the mail is the
commit recording it) is untouched. `claim_events` selects `pending` only, so
claim/dispatch/retry/completion change not at all, and releasing a false positive
is one `filtered` → `pending` flip (`release_filtered_event`).

`summaries_for` also fetches `AUTO-SUBMITTED`, `PRECEDENCE`, `LIST-ID`,
`LIST-UNSUBSCRIBE` (still `BODY.PEEK`). A UID whose headers can't be fetched is
recorded `pending`, not `filtered` — **fail open**: the worst case of failing
open is one junk message processed; of failing closed, a customer's mail silently
discarded. `skip_bulk` defaults to `True` (the one behaviour change on upgrade).

### Per-org budgets (`email_budget.py`)

Also pure: `remaining_messages`, `cost_exceeded`, `day_key`, `month_key`.
`daily_message_cap` and `monthly_cost_cap` are the *customer's* caps, alongside
the operator's `BESTTEAM_TRIGGER_DAILY_CAP`. Both default NULL.

Both are read in `_start_triggered_run` **inside `_dispatch_lock` and immediately
before the claim**. The message cap truncates the claim
(`limit=min(batch_size(), remaining)`) and returns before `claim_events` entirely
at `remaining == 0`, so nothing is even claimed. `messages_today` advances in the
same CAS that advances `runs_today`.

**`messages_today` shares `runs_date`, so both rollover sites (`poll_org` *and*
`retry_triggered_run`) must reset both counters** — a site that rolls one without
the other carries a stale count into the new day and nothing clears it.

Monthly spend is **queried, never counted into a column** (`SUM(cost_estimate)`
over `usage_records` from the first instant of the UTC month;
`usage_records.org_id` is denormalised for exactly this) — a stored counter would
need its own reset, backfill and drift bug.

Hitting a cap stops dispatch, alerts **once**, and resumes automatically on
period rollover. Not a hard disable: a budget reached on a Saturday must not need
a human on Monday, and a self-disabling trigger is indistinguishable in the UI
from one the customer turned off. Unprocessed messages stay `pending`.

**Budget alerts bypass `trigger_health.evaluate` deliberately.**
`_raise_budget_alert` calls `has_fingerprint` + `create_notification` directly
(`kind="budget"`): a ceiling is a normal operating state, not a fault, and
feeding it to the fault evaluator would corrupt `consecutive_faults`.
Fingerprints are **period-scoped** (`budget_messages:<UTC date>`,
`budget_cost:<UTC month>`) because `has_fingerprint` searches an org's *entire*
history — a bare name would alert once ever.

**Unpriced models.** `record_usage` writes `cost_estimate = NULL` with no
catalogue entry, so a naive `SUM` under-counts. `unpriced_models_for_org`
resolves the agent models of **every `status="deployed"` team in the org**, the
billable `embedding_model`/`query_expansion_model` of the KBs those teams search,
every standalone KB the org owns, plus `BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL`.
KBs are in that list because `kb:ingest` and `kb:search` rows have `run_id NULL`,
so the run-shaped half can't see them at all. `fake:` specs are excluded
(`billable_spec`); `rerank_model` is absent (local, $0, never recorded). The
helper is wrapped in one `except Exception -> []` — advisory copy must never fail
a save. At runtime NULL contributes 0, so **the cap is a floor on reality rather
than a phantom ceiling**, and the whole cap bounds an *estimate*.

**`poll_org` no longer returns on an empty detection.** Both filtering and
budgets create `pending` rows for reasons other than mail arriving (a released
false positive; a backlog a cap declined), and `_start_triggered_run` used to be
reachable only from a cycle that found new UIDs. The empty branch now consults
`has_pending_events` (a scoped `SELECT ... LIMIT 1`) and falls through to the
ordinary dispatch tail. The durability sequence is untouched.

### Health, alerting, maintenance

`trigger_health.py` is **pure** — `evaluate(outcome, consecutive_faults,
alerted_fingerprint, threshold) -> HealthDecision`. Two rules are load-bearing
and easy to break by "simplifying":

- **Alerts fire on transitions, not occurrences.** `alerted_fingerprint` is the
  *set* of currently-reported problems, sorted and comma-joined. Removing it
  turns every poll cycle into an alert. It is a **set** because two domains can
  break at once — as a single value, a mailbox fault overwrote a pipeline one and
  the next successful mailbox check announced a recovery that hadn't happened.
- **Recovery is domain-specific.** `OUTCOME_MAILBOX_OK` clears only a `mailbox`
  alert; `OUTCOME_PIPELINE_OK` clears `workflow` and `run_timeout`. A generic
  "healthy" outcome would let a mailbox check clear a pipeline alert — exactly
  the "healthy trigger, every run failing" state this exists to prevent.

⚠️ **The stored string value is still the literal `"workflow"`**, not
`"pipeline"`. Only the Python constant *names* were renamed
(`FINGERPRINT_WORKFLOW`→`_PIPELINE`, etc.) — deliberately, to avoid backfilling
every stored `fingerprint`/`last_error_kind` for a cosmetic rename.

Feeds: `runtime._safe_record_trigger_health` (returns early unless
`trigger.last_run_id == run_row.id`, so a superseded run's late outcome is
ignored), the connectivity check (mailbox), `_release_stale_run` (timeout, alerts
immediately). `last_error_kind` (`"mailbox" | "workflow" | None`) distinguishes a
connectivity fault, which auto-clears on the next good mailbox check, from a
pipeline fault, which persists until a real successful dispatch.

`evaluate_backlog` is a *level*, not a fault streak: fires once when the oldest
**`pending`** event outlives `BESTTEAM_BACKLOG_ALERT_MINUTES` (default 30) —
covering the case where nothing ever fails (a cap paused dispatch). `claimed`
mail is deliberately not counted (in-flight; a wedged run is the timeout alert's
job).

`trigger_metrics.py` collects (pure over persisted rows: poll lag, backlog age,
24h done/failed, detection-to-draft latency); `admin check-health` prints them
(exit 1 on FAIL). **That CLI is deliberately the only watcher for a stalled
poller** — notifications are delivered *by* the poll loop, so an in-process alert
about the poller being wedged could never leave the process. Cron it from
outside (`docs/deployment.md`, "Watching the watcher").

`notifications.py` delivers: stdlib `http.client`, HMAC-SHA256 over the exact
posted body, five attempts then `failed`. HTTPS + `check_host_allowed` **at
connect time, dialling the validated IP** (`_PinnedHTTPSConnection`), following
**no** redirects — `urlopen` re-resolved the hostname and followed redirects, so
a tenant admin could point a webhook at a rebinding host. A webhook receiver that
redirects is unsupported. **The payload carries health information only** —
adding a subject or body would turn alerting into an email-content exfiltration
path. Drained from the end of `poll_once`.

`sweep_secret_expiry` warns at 30/7/0 days before an M365 client secret expires,
keyed on an **admin-entered** date — not read from Entra on purpose, which would
need `Application.Read.All` over every app registration in the tenant.

`run_maintenance(db)` (secret expiry + retention sweep + webhook dispatch) is the
poller's tail. `poll_forever`'s `BESTTEAM_TRIGGERS_DISABLED` branch calls
`maintenance_once()` rather than skipping — **a pause of automation is not a
pause of data deletion.**

### Retention (`retention.py`)

Policy in `org_retention_settings`, NULL = keep forever (the default, so an
upgrade deletes nothing). `retention_default_days()` applies to **newly created**
orgs only. Spec: `2026-08-17-email-phase-3b-retention-export-design.md`.

**The rule that is easy to break by "simplifying": a purge clears content and
keeps accounting.** Content = `runs.input`/`output`, the run's `trace_events`,
its `run_knowledge_generations` refs, `automation_item_results.payload`.
Accounting = the `runs` row itself (deleting it orphans every `usage_records` row
naming it), `usage_records`, `trigger_context`, and an item result's
`status`/`source_key` — those two are what excludes already-drafted UIDs from a
retry, so clearing them would make a retention sweep cause **duplicate drafts**.

`runs.content_purged_at`, not an empty field, marks a run purged. `purge_run`
refuses a `running` run and is idempotent. It also scrubs the run's
`RunRegistry` entry (`registry.purge_content`) — that in-memory copy holds the
input and full event history and is what `GET /api/runs/{id}` and WS replay
serve, so clearing only SQL left deleted content readable until eviction.

Retention covers **all** of an org's runs, not only `trigger_context`-bearing
ones. `export_org_runs` emits exactly what a purge removes — `PURGED_FIELDS`
declares the surface once and `test_export_covers_everything_purge_clears` fails
if the export stops covering it. `purgeable_run_count` and `purge_org_runs` share
`_purgeable_query` so preview and purge can never disagree.

Routes: `GET/PUT /api/org/retention`, `POST /api/org/retention/purge`
(`older_than_days` is **required** — a destructive button must state what it
removes), `GET /api/org/export`, `POST /api/runs/{id}/purge` (404 cross-org, 409
`running`).

### Routes

`GET/PUT /api/org/email-filter`, `GET/PUT /api/org/email-budget`
(`org_settings.py`); `GET /api/org/email-trigger/filtered`,
`POST /api/org/email-trigger/filtered/{id}/release` (`email_trigger_api.py`).
Release is idempotent and returns **404** for an unknown id, another org's row
and an already-released row alike — never 403, which would confirm existence.
`GET /api/org/email-budget` reports `messages_today` as 0 unless
`trigger.runs_date` is still today, or an admin would see yesterday's total on
the very card explaining their cap.

## Property Maintenance Inbox (`automation_results.py`)

A two-agent SEQUENTIAL template built from three platform Skills
(`email_input_security_core_v1`, `property_maintenance_intake_v2`,
`property_maintenance_response_v1`). `_intake_v1` is still seeded but
unreferenced — attachment reading shipped as a new *version* so a team pinned to
`_v1` keeps what it deployed with. Deliberately **not** a `Case`/work-item
entity — see `docs/DECISIONS.md`. Spec:
`specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md`.

`email_input_security_core_v1` is attached to **both** agents. The Response
Coordinator never reads the mailbox, but it drafts from the Intake Analyst's
free-text write-up, which can quote injected instructions from the original email.

### Normalisation

`normalize_run_result(db, run_row)` is called from `runtime.run_in_background` on
**every** terminal path a `trigger_context`-bearing run can take — the streaming
loop, `_mark_cancelled()`, and the outer exception fallback — via a shared
`_maybe_normalize()` closure, so a cancelled or pre-stream-crash run's UID batch
never silently disappears from Needs-attention.

Two ordering rules, both load-bearing:

- Each path commits the terminal status **before** setting `terminal_seen`, so a
  raising commit still lets the outer handler's fallback run rather than leaving
  the run looking permanently "running".
- Each `_maybe_normalize()` runs **before** `registry.publish` for that event — a
  live Run Detail reacting to the terminal event could otherwise fetch zero
  automation rows with no later transition to prompt a re-fetch.

It only proceeds if `result_type == "property_maintenance_email_batch"`; any
other `trigger_context` run is left untouched. A run that crashed before
producing JSON looks identical from the output alone, so `_start_triggered_run`
stamps `trigger_context["result_contract"]` at dispatch whenever the deployed
config gives an agent `property_maintenance_response_v1` **and** that name still
resolves to the platform-tier row, not an org skill shadowing it (advisory only,
never blocks dispatch). `retry_triggered_run` **re-derives** this against the
pipeline as currently deployed rather than carrying the marker forward.

Trust boundaries — the whole point of this module:

- `source_key` is **always server-generated**
  (`mailbox:<credential-id>:uidvalidity:<value>:uid:<uid>`), so a model can never
  fabricate which input it processed.
- Every UID in the batch gets **exactly one** `automation_item_results` row,
  including a synthesised `status="error", needs_attention=True` row for one the
  model omitted or for a whole-envelope failure. Nothing silently disappears.
- `needs_attention` is **server-computed** regardless of what the model claimed.
- `action.draft_created` is **never trusted from the model alone**:
  `confirmed_draft_message_ids` comes from the run's own `tool_completed` trace
  events. A claimed-but-unconfirmed draft is downgraded and forces
  `needs_attention`; a claimed-`false` the trace confirms is **upgraded** — the
  model misreports in both directions and under-reporting a real draft is just as
  wrong as over-reporting one.
- Tool *failure* counts too: `failed_tool_message_ids` forces `needs_attention`,
  and includes `outcome in ("not_found", "out_of_batch")` even though the call
  didn't raise (`_redacted_email_tool_data` labels those `success: True`, so a
  soft rejection would otherwise stay hidden).
- `_bounded_message_id` mirrors the email tools' `.strip()`, or a call made with
  `" 42 "` records an unstripped id in the trace while the comparison uses the
  stripped one, missing the match.
- An id outside `trigger_context["uids"]` is logged (capped to 64 chars) and
  dropped, so the model can't expand its own scope.
- The whole envelope is Pydantic-validated; an enum/shape failure fails the
  **whole batch**. A validation failure logs `loc`/`type` **only, never
  `str(exc)`** — Pydantic's error repr embeds the offending input, and an injected
  email could steer customer content into server logs.
- `schema_version` is validated against the supported version, not accepted as
  any int. `payload` is length-capped and never holds a raw email body.
- `(run_id, source_key)` is unique, so a repeated normalize call is a no-op.

### Redaction boundaries

**A KB tool's `tool_completed` carries no document body text.** It used to be the
first 200 chars of retrieved excerpts — an org's own documents in every
`trace_events` row. The adapter now builds the event from what the tool reported
through `core/tool_context.py`: `summary`, `query` (≤200), `hit_count`,
`sources` (≤10), `ingestion_job_id`, and `hits` (≤10: citation, ids, scores,
**never text**). A source is a *citation label* — filename, `, p.<n>`,
` § <heading>` (capped at 80 chars, the only document text that crosses).

**A property-maintenance run's raw agent output is redacted one layer up**, in
`runtime.py` rather than the adapter (the SDK has no notion of "property
maintenance"; only `runtime` knows a run's `trigger_context`).
`run_in_background` computes `is_pm_contract_run` once; for such a run every
event in `_PM_REDACTED_EVENT_TYPES` — `agent_completed`/`run_completed` plus the
HIERARCHICAL delegate exchange (`subagent_*`/`delegation_*`) — **or** a
`tool_completed` for the manager's own `delegate_to_<name>` call
(`_is_delegate_tool_completed`; the adapter emits that as a *second* event
carrying the same subordinate summary, which the `on_event` path doesn't see) has
its `data` overwritten with `_PM_TRACE_REDACTED` **before** `asdict()` is built
for publish/persist and before it lands in `run_row.output`.

`normalize_run_result` still needs the real JSON: the `run_completed` branch
captures `event.data` into a local before redacting and passes it as
`raw_output_override`. `agent_completed.usage` is untouched (only `.data` is
mutated), so metering is unaffected.

### Read APIs

`GET /api/automation-results` (org-scoped; filter by
`run_id`/`needs_attention`/`status`/`result_type`; offset/limit/total) and
`GET /api/automation-results/summary`, which includes an `ever_used` flag (a
cheap all-time existence check) so the frontend can tell "never used this
template" from "used it, nothing today" — both otherwise report `emails_read: 0`.

There is **no org-timezone concept anywhere in this app**. `date` defaults to UTC
today server-side, and the endpoint takes `tz_offset_minutes` (the browser's own
`getTimezoneOffset()`) so `summary_for_date` bounds the day by the caller's local
midnight — a local date string alone still misdates rows created in a
timezone-ahead-of-UTC org's first local hours of a new day.

## Anonymous team sharing with continuous chat

`share_links_api.py` + `share_chat.py`. Specs:
`2026-08-14-team-sharing-continuous-chat-design.md`,
`2026-08-23-share-chat-streaming-design.md`. Schema: `db/CLAUDE.md`.

Two deliberately separate surfaces:

- **`share_links_api.py`** — org-side management, every route behind
  `get_current_org`. `expires_at` is normalised to naive UTC on the way in (the
  column is naive and `_is_expired` compares against naive UTC).
- **`share_chat.py`** — the public, anonymous surface. **No `users` row is ever
  created.** Every route re-validates link active/expiry/org-active fresh from
  the DB, and the WS re-checks before delivering each event. Every "can't use
  this link" case returns **the same single 404 detail**, including "the team
  isn't deployed" — a distinguishable message there is an existence oracle.

**Auth is a signed session cookie** (`share_auth.py`), not a JWT: a visitor has
no account, org or `users` row. The cookie carries only an opaque
`session_token`, HMAC-signed with **`auth.SECRET_KEY`** — so rotating that key
invalidates every visitor session by design. A cookie IS sent on the WS
handshake, so no `?ticket=` workaround is needed.

⚠️ **Operational: the cookie is `SameSite=Lax`**, so frontend and API must be
served same-site (same registrable domain; ports don't matter) or it never comes
back and every message silently starts a new session. Cross-site needs
`samesite="none"` + `secure=True` + HTTPS — a deliberate config decision.

**A turn is a normal run.** `send_share_message` builds the transcript
(`format_transcript`, capped at `MAX_HISTORY_TURNS`) into one input and dispatches
it through the same registry/executor/`run_in_background` machinery, so metering,
trace persistence and cancellation work unchanged and no LangGraph checkpointing
is involved. Stamped `trigger_context = {share_link_id, share_session_id,
turn_number}`; `_safe_record_share_reply` keys off `share_session_id` to append
the assistant turn on **every** terminal path, so `_has_pending_turn` can never
wedge a visitor's chat shut. `record_share_reply` is idempotent per turn.

Transcript content is untrusted: each turn is wrapped in `<user>`/`<assistant>`
with `<`/`>` escaped inside, so a visitor can't inject a fabricated prior turn.
The visitor WS is redacted to `type` only (plus `run_completed.data`, the reply
itself) — agent names, intermediate output, tool summaries and `usage` never
leave the org.

Streaming:

- **`registry.publish_transient`** is `publish`'s record-nothing twin: fans out
  to live subscribers, appends nothing to `run.events`, drives no status change,
  replayed to nobody. A long reply would otherwise put thousands of entries into
  the log every new subscriber is seeded with. Deliberate consequence: a visitor
  reconnecting mid-run sees no partial text, then the complete reply on
  `run_completed`, which IS replayed.
- **`runtime._TokenSink`** buffers deltas into `reply_delta` events — flushing at
  40 chars or 80 ms since the last flush, **evaluated on arrival, never on a
  timer**, so a provider stall leaves up to 39 chars unshown until the next delta
  (deliberate). Plus one flush before **every** event the runtime yields — that
  is what guarantees a buffered delta arrives ahead of the `agent_completed` that
  supersedes it. `STREAM_RESET` becomes `reply_reset` and drops the buffer. Built
  **only for share-chat runs**; the monitor page has no UI for deltas.
- **`visitor_safe_event`** admits `reply_delta` (by the same argument that admits
  `run_completed.data` — only one node is ever wired to stream) and `reply_reset`.
  `agent_completed` stays type-only, which is what lets the page count steps
  without learning a single name.

`GET /{token}/team` → `{"name", "steps"}`. A pure read requiring no cookie, so a
first-time visitor can render the header. It reads `PipelineRecord.config`
rather than `_resolve_pipeline_and_version`, because that cache-miss path loads
every skill/KB/email tool and a path-constructed vector KB embeds at load time —
real spend on an anonymous endpoint with no cap in front of it. `steps` is
**null if any team is HIERARCHICAL** (a manager emits one completion however many
subordinates it delegates to), and the page shows a pulse rather than a dishonest
count. The team's *name* is disclosed; agent names, roles, models and the
collaboration mode are not.

`POST /{token}/runs/{run_id}/cancel` → 202, authorised exactly like the stream WS,
same single 404. **No cap refund**: the tokens were spent, and a free retry after
a stop is an unlimited-work primitive against the org's budget.

Rate limiting is `ShareLink.daily_cap` applied **twice** — per-session
(`try_consume_turn`) and link-wide aggregate (`try_consume_link_turn`), both the
same atomic reset-then-conditional-UPDATE CAS. **The aggregate one is
load-bearing**: a client that never stores the cookie gets a brand-new free
session on every request, so the per-session cap alone caps nobody.

## Sync-to-async streaming bridge

`Pipeline.stream()` is a blocking generator. The backend runs it in a
`ThreadPoolExecutor` and hands events back via
`loop.call_soon_threadsafe(queue.put_nowait, ...)`.

**Each subscriber's `asyncio.Queue` is paired with the loop captured at
`registry.subscribe()` time** — the WebSocket handler's own loop, which lives as
long as the connection — **not** the loop of the `POST /api/runs` request that
started the run, which is gone by the time the worker finishes. Capturing the
request's loop meant `publish()` silently never happened under `TestClient`'s
per-request ephemeral loops and `queue.get()` blocked forever. (A
`SimpleQueue` + `asyncio.to_thread(queue.get)` variant was tried and rejected: a
blocking `to_thread` isn't cancellable, so it hung the same way on disconnect.)

`RunRegistry` bounds growth (`_MAX_RETAINED_RUNS = 1000`): every `create()`
evicts the oldest terminal, subscriber-free runs. A `running` run or one with an
active subscriber is never evicted. Needed because the email trigger creates runs
unattended and indefinitely. Eviction can land in the `await websocket.accept()`
yield between `stream_run`'s existence check and `subscribe()`, so `subscribe()`
returns `None` (not `KeyError`) and `stream_run` closes 4404. The trigger
activity list is unaffected — it reads the persisted `runs` table.

## Trace events, cancellation, run history

`adapters/langgraph_adapter.py` buffers per-node events (`agent_started`,
`tool_started`/`tool_completed`, `agent_progress`, and the HIERARCHICAL
delegation set) into a per-node list, flushed immediately before that node's
`agent_completed`. `tool_completed.data` carries a truncated business-safe
`summary` — never raw tool args or exception text (`core/trace.py`'s docstring
has each type's full shape). `runtime.run_in_background` persists every event as
a `TraceEventRecord` in `seq` order and publishes a synthesised `run_queued`
bookend to the live registry too, so replay and persisted history start at the
same event.

**Cooperative cancellation** (`POST /api/runs/{id}/cancel`,
`registry.request_cancel`) is checked between yielded events — never a forceful
thread kill. **The check is skipped for a node's own buffered event types**:
those describe paid work that already happened, so stopping between them and
their `agent_completed` would silently drop that node's usage from
`usage_records`. Every other type is a safe checkpoint — notably `run_started`,
skipping which would let a cancellation known before the first agent runs one
whole avoidable paid turn. `stream_iter.close()` is safe there because
`GeneratorExit` is a `BaseException`, not caught by the existing handlers.

Frontend: the monitor page polls `GET /api/runs` every 5s while any row is
`running` (an effect-local `ignore` flag guards a stale response from before a
filter change). A `running` run streams live; anything else fetches
`GET /api/runs/{id}/trace` once — no live/historical merge, by design.

### Diagnostic re-runs (`POST /api/runs/{id}/diagnose`, admin-only)

A normal trace deliberately omits the system prompt, per-agent input,
intermediate model turns, tool args and retrieved passages. Rather than recording
those on every run, an admin **re-runs** the original input against the team as
*currently deployed* with `Pipeline.stream(diagnostic=True)`, and the verbose
events (`agent_prompt`, `model_turn`, `args`, `result`) land in the ordinary
trace of the **new** run. Cap 20,000 chars; email tools stay redacted on every
path. Spec: `specs/2026-08-21-diagnostic-rerun-design.md`.

Rules, each with a reason: **admin-only** (raw prompts and documents). **Always a
new `runs` row** (`diagnostic_of_run_id`) — history is immutable. **400 for an
autonomous email run** (it would reach the org's live mailbox with unscoped
`email_*` tools); a shared-chat turn is allowed, since the diagnostic row has no
`trigger_context`. **400 for a diagnostic run itself**, **409 for a purged run**
(no input left). **No `user_id`** — per-user memory is neither recalled nor
written; the admin must not act as the customer. `version_changed` tells the UI
the team was redeployed since, so the admin knows the problem may not reproduce.
Spend is metered and retention applies like any run. **Filtered out of a
non-admin `GET /api/runs`** — a list-cleanliness rule, not a security boundary.
Not done for v1: rebuilding the *pinned* version, purging one diagnostic run,
excluding them from `run_analytics.py`.

## Backend API

`main.py` holds `/api/health`, `/api/pipelines`, `/api/pipelines/{name}/graph`,
`/api/runs`, and the `/api/runs/{id}/stream` WebSocket. Two routers add the rest:

### `builder.py` (`/api/builder/sessions`) — the wizard state machine

Thin over `db/builder_sessions.py` + `core/requirements.py`/`core/specification.py`.

| Route | Stage |
|---|---|
| `POST /` | 1 Intent (`intent_text`/`as_is_text`) |
| `GET /{id}` | fetch session state |
| `POST /{id}/requirements` | 2 — pass `requirements` to store, or `model` (+`feedback`) to generate |
| `POST /{id}/specification` | 3 — pass `specification` (validated) or `model` to generate |
| `POST /{id}/solution` | 4 — like `/specification` but requires `feedback`, always `append_feedback()` |
| `POST /{id}/refine` | **the Confirm page's single action** |
| `POST /{id}/test-runs` | 5 — same runtime machinery as `/api/runs` |
| `POST /{id}/deploy` | 6 — validate models, publish an immutable version |

`/solution` with blank feedback deliberately skips the architect entirely (no
drift) and only re-pins models.

`/refine` takes the edited `requirements` draft plus free-text `feedback` and
updates understanding *and* team in one call. The Business Analyst runs only when
`feedback` is non-blank, with the edited draft as
`generate_requirements(current=...)` — so a hand edit survives the round it
triggered. The Solution Architect always runs (the button says the team will be
updated). **Both halves land in one `update_session()`**, so a failed redesign
cannot leave a saved understanding the team has never seen — the split state that
made the old two-button page misleading. Requires an existing
`specification_json` (400 otherwise).

`/deploy` validates agent models against the catalogue
(`deploy_validation.validate_agent_models`, `fake:` exempt; 400 listing any
agent whose model is missing/empty/non-string or not offered — the CRUD path
builds `Agent(**spec)` directly, so this is the only guard), then publishes a new
immutable version (`publish_pipeline_version`) and links `session.pipeline_id`,
both in the session's single commit. **Model validation is deploy-time only** — a
model later removed from the catalogue fails at run, not load.

All `model=` endpoints translate `BestTeamError` → `400` and any other exception
(e.g. a real provider call with no API key) → `502`, via `_call_model()`.

### `crud.py` (`/api/config/...`) — operator-only advanced view

`GET`/`PUT`/`DELETE` for `knowledge_bases`/`skills` (validated as standalone
components — field shape only) and `pipelines` (a complete
`Specification.to_raw()`-shaped dict, validated via `_build_pipeline()` exactly
like the wizard, then `validate_agent_models()`).

**Save is deploy**: `PUT /pipelines/{name}` writes `status="deployed"` on insert
and update — no separate promote step, mirroring the wizard. Each save publishes
an immutable version rather than overwriting `config` in place.

Also `GET /orgs` (the org selector) and `GET /tools` (`REGISTRY` name + docstring).

**Standalone `agents`/`teams` CRUD was removed** — nothing consumed those records
and both tables were empty everywhere. The models remain in `db/models.py`.

Deletion guards: `DELETE /skills/{name}` and `/knowledge_bases/{name}` refuse with
`409` if a pipeline's **current version** still depends on the item, via typed
`pipeline_dependencies` rows (`pipelines_referencing`) rather than scanning JSON.
The check runs before any deletion/`rmtree` and names the referencing team(s). A
skill dependency also pins `resource_version_id`, so editing a skill or creating a
same-named org override does not alter an already-deployed team — redeploying
explicitly adopts the version resolved then. A pipeline's inline KB shadows a
same-named standalone one, so the standalone isn't recorded as its dependency.

Name-collision guards: both deploy points reject (`400`) a pipeline whose KB name
shadows a built-in tool (`kb_name_collisions` → the pure
`find_kb_tool_collisions` against `set(REGISTRY)`; name-only, so it runs before
path validation). Only KB names are checked — the per-org email-tool override
intentionally shadows `REGISTRY`'s `email_*`. A KB also can't be **created** with
a built-in tool name, so a colliding KB can never exist to shadow one at load.
Seeded platform built-in skills (`_BUILTIN_SKILL_NAMES`) are undeletable. KB
delete commits the row **before** `rmtree` (logging rmtree failures), so a commit
failure can't destroy files under a rolled-back record.

Still deferred: the delete/deploy TOCTOU window (serialised via
`component_mutation_lock`, not DB-enforced), model/built-in-tool dependency rows,
and standalone-KB content pinning. See `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.

### Pipeline resolution

`_get_pipeline()` checks for a `PipelineRecord` in the DB first, within the
caller's org and filtered to `status == "deployed"` (cached on `updated_at`), then
falls back to `PIPELINES_DIR/<name>.yaml` (cached on mtime). A non-`deployed`
record is treated as unknown — same 404 as absent, no existence oracle.
`GET /api/pipelines` applies the same filter; `/api/config/pipelines` does not
(operators see all statuses).

**Demo YAML pipelines are opt-in** (`BESTTEAM_DEMO_PIPELINES`, **off by
default**). YAML is the *SDK's* format (`load_pipeline`, `bestteam run x.yaml`,
unaffected by this flag and by the DB entirely); DB rows are what the wizard
creates per-org. The shipped fixtures carry no `org_id`, so while enabled *every*
org user sees and can run them — including `*_live` ones that spend real quota and
read the running org's mailbox. The gate covers **both** the list and resolution;
hiding them from the list alone would leave them runnable by name.

## Async knowledge-base ingestion (`ingestion.py`)

Spec: `specs/2026-08-16-kb-document-chunk-ingestion-design.md`. Reference:
`docs/KNOWLEDGE_BASES.md`.

Both upload routes (admin `crud.py`, self-service `org_knowledge_bases.py`, via
the shared `upload_knowledge_base()`) validate synchronously, write files to a
fresh on-disk version directory, upsert the `KnowledgeBaseRecord`, create a
`queued` `IngestionJob`, and submit to `ingestion.py`'s own 4-worker
`ThreadPoolExecutor` (separate from the run executor) — returning immediately
with `{"name", "job_id", "status": "queued"}`. The worker opens its own `Session`
on the passed-in `engine` (a `Session` isn't thread-safe).

**Everything is buffered in plain Python and written in ONE short transaction at
the end.** Flushing per file would take SQLite's RESERVED write lock on the first
file and hold it through everything after, blocking every other writer in the
process (runs, trace events, usage, share messages) for the whole upload.

`_executor.submit` sits *after* that commit and outside the handler that rmtree's
the staged directory: the rows are durable by then, so cleaning up files on a
submit failure would strand a permanently `queued` job pointing at a deleted
directory. The job is resolved `failed` (caller gets 503) instead.

Self-service uploads refuse a confirmed upload while a `queued`/`running` job
exists for that KB, inside the same per-KB lock as the existence/cap checks —
without it a member retrying a slow upload could pile up unbounded work, each
retry having already staged up to `_MAX_TOTAL_SIZE_BYTES` and queued an embedding
call. The trusted admin path has no per-caller limit and doesn't need this.

### Incremental and additive ingestion

`upload_knowledge_base(..., mode=)` takes `"replace"` (default) or `"add"`.
**"Add" is implemented at the staging layer, not the retrieval one**:
`_stage_previous_generation` copies the live generation's files into the new
version directory beside the new ones, skipping any name this upload supersedes —
matched **case-insensitively**, because the filesystem underneath may be (on
Windows/macOS a carried `Policy.txt` and an uploaded `policy.txt` are one path,
and treating them as two copied the old file over the new upload). The new job
therefore still owns a complete document set, so the atomic-swap invariant,
pruning, retention and `resolve_knowledge_base` are all untouched — **there is no
notion anywhere of a collection spanning two jobs.** `_MAX_DOCUMENTS_PER_KB`
bounds the merged set (200 admin, 30 self-service).

`_reusable_documents` carries an unchanged file's chunks — **embeddings
included** — forward from the previous completed job, matched on
`(filename, content_hash)`. Only genuinely new chunks reach
`embed_documents_in_batches` and only their tokens are metered.

`_carryable` gates the lookup on the previous job's shape matching: `kb_type`,
`embedding_model`, `chunk_size`, `chunk_overlap`. Those last two sit on the job
row for the same reason the first two do — **`KnowledgeBaseRecord.config` has
already advanced to the new upload's spec by the time the worker runs, so only
the job can say what its chunks were actually cut with.** A job predating the
columns reads NULL and is deliberately not reusable, so the first upload after an
upgrade re-embeds once. Only a `completed` job is ever a candidate.

The self-service confirmation gate carries the choice: `mode` is one field with
three states (`""` unconfirmed → 409, then `"add"`/`"replace"`), replacing a
boolean, because a boolean cannot express three answers.

### Atomicity: the `IngestionJob.status` flip IS the swap

There is no CURRENT-pointer file. Retrieval resolves a KB's most recent
`IngestionJob` with `status="completed"`; a `queued`/`running`/`failed` job's rows
are invisible by construction.

⚠️ **"Most recent" is by `id`, not `completed_at`.** Overlapping uploads can
finish out of submission order, and `id` — assigned inside the serialised
`_kb_upload_lock` staging block — is the only field guaranteed monotonic with
submission order. Both pruning steps order the same way, for the same reason.

A KB with `IngestionJob` rows but **no completed one** does NOT fall back to
legacy file-based construction — `resolve_knowledge_base` falls back only for a
KB with **zero** jobs ever (a true pre-feature legacy KB). Falling back otherwise
would scan `config`'s `path`, the upload root, which recursively contains every
version subdirectory *including the one currently staging*, serving un-vetted,
partial or entirely un-embedded content. It raises `ConfigurationError` instead.

Which subclass a resolved job is rebuilt as, and which model embeds a query, come
from the **job row's** `kb_type`/`embedding_model` — not `config`, which is
already the *next* generation's spec during the whole ingestion window (and
permanently, if the new job fails). `config` still supplies
`top_k`/`rerank_model`/`candidate_k`/`query_expansion_*`, which apply uniformly.

A successful job invalidates the pipeline cache (at job completion, not
upload-dispatch — that's when content actually changes) and best-effort prunes
older generations: the current one plus one grace-window generation stay intact;
an older one loses its directory and, unless a run's trace references it
(`run_knowledge_generations`), its rows — a referenced "audit-only" generation
keeps document/chunk rows with **vectors nulled**. `_reusable_documents` looks at
exactly that intact window, never at an audit-only one.
`_prune_failed_ingestion_versions` runs on the **failed** path too (the only
cleanup that does, since a customer retrying an unparseable upload never produces
a completed job) and keeps the most recent failed directory as a diagnostic copy;
failed jobs' *rows* are always kept as the customer-visible error record.
**Cache invalidation and both pruning steps are isolated in their own
try/excepts** so a failure can never retroactively mark a committed successful
ingestion as failed.

### Per-document partial failure

One bad file (unsupported type, parse error, no extractable text, zero chunks)
doesn't fail the job — it's a `failed` `KnowledgeDocument` row with a capped
message, and the job continues.

**The parse loop walks every staged file, not only supported suffixes.**
Filtering first meant an unsupported file left no row at all, so
`documents_succeeded + documents_failed` silently disagreed with `file_count` and
nothing told the customer their `.png` was dropped. The suffix check raises inside
the loop's `except Exception`, as does `_has_extractable_text` — that second one
stops a **scanned PDF** (which parses to its `[PDF: …]` header and nothing else)
from becoming a content-free chunk instead of a reported failure.

The job ends `failed` only if every document failed, or if the embedding call
itself raises — in which case already-flushed-but-uncommitted objects are
discarded, since a vector/hybrid KB with no embeddings can't serve queries.
Embedding is `embed_documents_in_batches`: 100 chunks per call, three attempts
each (1s then 2s backoff), and **only the failing batch is retried**. A batch
returning the wrong number of vectors is rejected immediately, no retry. Metering
is computed once from the chunk texts, so a retried batch is never billed twice.

A document's error text is scrubbed of the server's absolute upload path
(`_scrubbed`) — third-party parsers embed the path they were handed, and
`job_status_payload` returns that text verbatim to a self-service member. Any
unexpected worker exception is caught and recorded on the job row; **the job
never raises uncaught.**

### Org self-service (`org_knowledge_bases.py`)

`GET /api/org/knowledge-bases`, `GET`/`DELETE /{name}`, `POST /{name}/search`,
`DELETE /{name}/documents/{filename}`, `POST /{name}/restore`,
`POST /{name}/ingestion-jobs/{job_id}/retry` — all `get_current_org`-scoped.

`_kb_summary` reports `used_by`, `servable`, `documents` (newest completed job's
rows) and `latest_job` (newest of *any* status, **`config` stripped** — that field
carries the server's absolute upload path and this list is customer-facing).

**Per-document delete** is the upload pipeline with no new files:
`_stage_previous_generation` with the named file superseded, a job under the live
job's own shape so everything else carries forward unmetered. 409 while a job is
in flight, 409 for the last document (an empty collection can't be built), 404 for
a name not in the live generation — **exact match**, since the carry drops a
case-variant on Windows/macOS and "removed `policy.txt`" must not be true of a
file the customer never named.

**Restore** is the same machinery with the *previous* completed job as source and
shape, so every chunk and vector carries forward and nothing is metered. One
generation back only — it is the only one whose files are still on disk.
`record.config` is untouched, so a restore undoing a type change serves under the
previous type while `config` keeps the new one.

**Retry** re-runs a **failed** job **in place on the SAME row** over its
still-staged files (status back to `queued`, counters cleared, the failed
attempt's diagnostic rows deleted). The same row on purpose: a second job sharing
the failed one's directory would have it reclaimed by
`_prune_failed_ingestion_versions`. All three job-creation sites now write
`chunk_size`/`chunk_overlap` at creation (previously only the worker did), so an
interrupted-while-`queued` job is retryable after a restart; `_job_shape` is the
one fallback, shared with removal and restore so the three can't drift. Only the
collection's newest job can be retried (409 otherwise), **plus** an explicit
queued/running 409 for the case the newest-job check can't cover: the admin path
has no in-flight guard, so a newer job can fail fast while an older worker is
still ingesting. Nothing is double-billed — ingestion usage is recorded only on
completion.

Two consequences elsewhere: `resolve_knowledge_base` reads the *latest* job rather
than asking whether any exists, and reports a `failed` one's own error instead of
telling the customer to wait for something that will never finish; and
`builder._all_knowledge_base_tools` **skips** a KB that won't resolve (logged) —
it runs over every KB in the org, so one unparseable upload used to 4xx spec
generation for everybody. `load_knowledge_base_tools` still **fails closed**,
because there the KB is one an agent actually references.

**Try a search**: `POST /{name}/search`, `{"query": <1..500>, "top_k": <1..10>}`,
each result `text` capped at 1,500 chars — enough to judge retrieval by, not a
document reader. It resolves with **no `source`**, which turns the legacy
file-based fallback off for this surface: rebuilding a disk-backed collection
would re-parse every file and re-embed a `vector` one unmetered on every click.
That refusal, "still processing" and "the last upload failed" are all
`KnowledgeBaseNotReady`, and the route maps **only** that subclass to `409` — any
other `ConfigurationError` (a missing `rank-bm25` extra, a bad `rerank_model`) is
an operator's deployment problem the customer can't act on, so it falls through to
a logged `500` rather than masquerading as a conflict to wait out. A provider
failure inside `kb.search` is `502`. No cache and no rate limit, deliberately.

**Deleting a KB is refused (`409`) while an upload is processing**, and the whole
sequence lives in `knowledge_bases.py::delete_knowledge_base`, not `crud.py` — it
needs the per-KB `_kb_upload_lock` and takes `component_mutation_lock` itself,
which is **not reentrant**, so `crud.delete_item`'s branch must return *before*
entering its own `with component_mutation_lock` block. Refusing rather than
cancelling is what makes "a KB being deleted has no worker" true: a cancel flag
would leave the worker holding an open file handle, `rmtree` then fails with
`WinError 32` and silently leaks the directory, and the worker's final commit
writes rows against a `kb_id` that no longer exists (FK enforcement is off, so
nothing catches them). `fail_interrupted_jobs(engine)` at startup is the other
half — without it one killed process makes that KB permanently undeletable.

`delete_kb_ingestion_data` participates in the caller's existing
delete+commit+rmtree transaction rather than committing separately.

## Auth, model catalog, usage metering

### `auth.py` / `auth_api.py`

Stdlib-only: PBKDF2-HMAC-SHA256 at 260,000 iterations
(`pbkdf2_sha256$<iterations>$<salt>$<hash>`) and JWT-shaped bearer tokens (HS256
via `hmac`, `sub`+`exp`). **No `passlib`/`PyJWT`/`bcrypt` dependency.**
`BESTTEAM_SECRET_KEY` / `BESTTEAM_ACCESS_TOKEN_EXPIRE_MINUTES` (defaults: a
dev-only secret, 1440).

Routes: `POST /login`, `GET /me`, `POST /password`. Three things about
`/password` are load-bearing: it **shares `_LOGIN_LIMITER` with `/login`**
(both ration guesses at the same secret, and it is reachable from an unattended
logged-in browser, which `/login` is not); it **rotates `security_stamp`**, so
every token and WS ticket for the account dies — the caller's included, which is
why a fresh one comes back; and it requires **8 characters**, a floor the
operator's `POST /api/admin/users/{username}/password` deliberately does **not**
have (that sets a temporary password for an account the operator already
controls, and a floor there would break every provisioning fixture while
protecting nothing).

**There is no public registration endpoint** — orgs, users and admins are all
provisioned via the operator CLI. The operator reset is the only path for a
forgotten password: there is no email channel to recover one with, by design.

Three exported dependencies: `get_current_user` (bearer → `User`),
`get_current_admin` (403s non-admins), `get_current_org` (**403s platform
operators** — org-NULL users — on org-user surfaces: pipelines list/graph,
`POST /api/runs`, the builder router). Admin is granted only via the CLI, never
from a username match, and never read at import. `/api/health` and `/api/auth/*`
stay public; the run-stream WS authenticates with a single-use `?ticket=`.

**`POST /login` is throttled** (`login_rate_limit.py`): a process-wide in-memory
sliding window over *failed* attempts — 5 per lower-cased username and 20 per
client address in 15 minutes. A throttled attempt gets 429 + `Retry-After`
**before PBKDF2 runs** (the CPU half of the defence — 0.76s per hash), with the
same message whether the username exists or not. **The check reserves**:
`reserve()` counts the attempt as a failure in the same locked step that admits
it — a check-then-record pair would let a concurrent burst hash once per thread.
Only `record_success()` takes it back, clearing the username's failures and
releasing the one slot the attempt took from the address. Username keys are a
SHA-256 digest (the request puts no bound on name length and a key lives a whole
window); the expired-key sweep runs when the dict has doubled (amortised O(1)).
Behind a reverse proxy the address is the proxy's unless uvicorn is told to trust
it — which is why the username key exists.

### Model catalog

`db/model_catalog.py` + `/api/config/model-catalog` CRUD. `to_prompt_text()`
renders it for the Solution Architect's prompt.

⚠️ **`tier="embedding"` marks an entry as an embedding model, not a chat model.**
It lives in the same table so `record_usage` can price KB embedding spend from one
catalogue, and **`list_chat_entries(db)` — not `list_entries` — is what every
chat-model surface uses**, so an embedding model can never be handed to an agent.
Admin CRUD lists everything (somebody maintains those prices) and no embedding
entry is seeded into `DEFAULT_MODEL_CATALOG`.

The **list is exposed read-only at `/api/model-catalog`** to any authenticated
user: the wizard runs as an org member and without it the frontend falls back to a
`fake:` model and generation fails. Both generation steps translate that into a
clear `ConfigurationError` ("needs a real AI model") rather than raw
`NotImplementedError`.

### Skills library (`skills.py`)

Every PUT appends an immutable `SkillVersion` and moves
`SkillRecord.current_version_id`. `load_skills(db)` returns current heads (drafts,
deploy validation, YAML); `load_skills(..., pipeline_version_id=)` returns only
the versions pinned by that deployed pipeline. `_with_skill_catalog` appends the
available-skills list to the requirements text before `generate_specification()`.

### Usage metering

`TraceEvent.usage` is a list of `{"model", "input_tokens", "output_tokens"}`,
populated by the adapter whenever a response has `usage_metadata` (`fake:` models
leave it empty). For HIERARCHICAL teams the manager and all subordinates share one
`usage_sink` per turn, so the total surfaces on the manager's single
`agent_completed`.

`run_in_background(..., engine=)` opens its own `Session` and calls
`record_usage()` for each entry on every `agent_completed`, computing
`cost_estimate` from `model_catalog` (`None` otherwise). Memory calls are metered
too, with the agent label picked **per-event** so a recall-side call is never
mis-attributed as extraction:

| Event | `agent` label | Timing |
|---|---|---|
| `memory_recorded` / `memory_failed(data="record")` | `memory:extraction` | **after** `run_completed` |
| `memory_recalled` / `memory_failed(data="recall")` | `memory:query_expansion` | **before** `run_started` |

Extraction events arrive post-terminal (so a hung extraction can't wedge the run)
but `run_in_background` drains the whole stream, so they're still recorded;
`registry.publish` tolerates a run evicted in that window. All persistence goes
through `_safe_record_usage`, which isolates a write failure so **metering can
never flip a successful run to `run_failed`**.

**KB spend reaches the same ledger by three routes, and `runtime.py` needed no
change for any of them** — making `usage_records` a three-source ledger (run /
ingestion job / ad-hoc search), the wording to keep consistent across
`db/models.py`, `db/usage.py`, `db/CLAUDE.md` and migration `n1o2p3q4r5s6`:

| Route | Row |
|---|---|
| Query time (query embedding, expansion LLM) | rides the existing `agent_completed.usage`; ordinary run rows attributed to the searching agent |
| Ingestion (`_safe_record_ingestion_usage`) | **one** row per completed job — `agent="kb:ingest"`, `run_id=None`, `ingestion_job_id` set |
| Test search (`_safe_record_search_usage`) | `agent="kb:search"`, **both** FKs NULL |

The query-time path drains `tool_ctx.usage` **on the failure path too** — the paid
call already happened. Ingestion and search metering are best-effort in their own
try/excepts. The org's monthly `SUM(cost_estimate)` counts all three naturally;
every run-keyed consumer drops the FK-NULL ones the same way.

Two caveats: **embedding token counts are estimated** (±30%) because no provider
reports embedding usage through LangChain's `Embeddings` interface — expansion
tokens are the model's own reported `usage_metadata`, not an estimate. And
**nothing billable means nothing recorded**: `billable_spec()` (a non-`fake:`
string) is the one definition, shared by SDK and `ingestion.py`. Reranking is a
local cross-encoder, $0, deliberately never recorded.

## Per-user memory

`runtime._make_memory()` builds a `MemoryManager` **on the worker thread** (so
the SQLite connection is thread-local) from env. SDK-side design:
`src/bestteam/core/CLAUDE.md`.

| Env var | Effect |
|---|---|
| `BESTTEAM_MEMORY_DB` | unset/empty → memory disabled, runs unchanged |
| `BESTTEAM_MEMORY_MODEL` | enables one extraction LLM call per run |
| `BESTTEAM_MEMORY_EMBEDDING_MODEL` | opt-in hybrid recall (unset → plain BM25, byte-for-byte unchanged) |
| `BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS` | default 14, only meaningful with an embedding model |
| `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL` / `_COUNT` | opt-in expansion, default 3 phrasings |
| `BESTTEAM_MEMORY_RERANK_MODEL` / `_RERANK_CANDIDATE_K` | opt-in rerank, default `top_k * 4` |

⚠️ **Which vars `memory_api.py::get_memory_store` reads is deliberate and
asymmetric.** The two hybrid-recall vars ARE read, so admin search reflects the
same ranking a live run gets. The query-expansion vars are **not** — admin search
stays literal-only by design.

Failure shapes also differ deliberately: `BESTTEAM_MEMORY_EMBEDDING_MODEL` is
eagerly resolved at store construction, so a bad spec disables memory entirely; the
expansion and rerank models are resolved lazily per call and a bad spec only
degrades that feature.

`create_run` passes `pipeline_id` alongside `pipeline_version_id`. Unlike
`pipeline_version_id` (pure provenance), **`pipeline_id` scopes recall and
writes**: episodic/procedural memory is isolated per pipeline; semantic stays
org-wide. `main.py::create_run` threads the JWT username through as `user_id`;
the wizard's test-runs omit it, so sandbox runs never touch memory.

### Admin memory management (`memory_api.py`, `/api/memory`)

`get_current_admin`-guarded. `GET /users`, `GET /users/{user_id}/records`
(browse via `all(limit=)`, search via `search(top_k=, max_candidates=)` so both
response and scan work are bounded), `DELETE /records/{id}`,
`DELETE /users/{user_id}`, `DELETE /orgs/{org_id}`.

Org erasure deletes org-scoped rows **plus** current members' legacy NULL-org
rows. **The legacy purge is deliberately NULL-org-scoped, not an unscoped
`delete_user`**, which would also destroy the same username's rows under other
orgs (moved user / reused username).

`?org=` on the records route: **omitted = across all orgs, an int = that org, and
the literal `?org=legacy` = only pre-SP-2 NULL-org rows** (422 otherwise). The
`legacy` sentinel exists because a legacy identity's `org_id` is null — without
it, selecting "legacy (no org)" would omit `org` and read the username across
*every* org, a cross-tenant over-fetch.

Memory is **org-scoped** (a run only ever sees its own org; the admin surface
reads across) and **principal-scoped**: each record carries the run's immutable
`users.principal_id`, so a recreated same-username account can't recall the
deleted account's rows. Account deletion **retires the principal** alongside the
purge, so an in-flight run's late write is dropped by the store fence; both run
before the username is released and **fail closed**.
`account_memory.purge_user_memory(username, principal_id=)` does both. The
`delete-user` CLI warns loudly when `BESTTEAM_MEMORY_DB` is unset rather than
implying a clean purge, and never creates a missing store. `move-user` first binds
legacy NULL-org rows to the source org so pre-SP-2 data stays attributable.
Pre-stamping NULL-principal rows aren't recalled by a stamped run; the opt-in
`admin backfill-memory-principals` binds them. Spec:
`specs/2026-07-30-memory-principal-lifecycle-design.md`.

## Admin org/user management (`admin_api.py`)

`/api/admin`, `get_current_admin`-guarded — the web counterpart of the CLI.
`GET/POST /orgs`, `PATCH /orgs/{name}` (deactivate/reactivate), `GET/POST /users`,
`POST /users/{username}/password`, `POST /users/{username}/move`,
`DELETE /users/{username}`. Spec:
`specs/2026-07-27-admin-org-user-management-design.md`.

**No route can escalate privilege or mutate a platform account**:
`promote`/`demote` and the whole operator/admin lifecycle stay CLI-only, and every
user route refuses (`409`) a non-org-member target. `delete`/`move` run the same
fail-closed memory work as the CLI, through the shared `account_memory.py` helpers.

**Org deactivation** is a reversible full suspend, enforced in three places:

1. `login` refuses a token, and **`get_current_user` rejects (403) an
   inactive-org member on *every* authenticated route** — centralised there
   rather than only in `get_current_org`, so `/me`, `/model-catalog`, run reads,
   the WS-ticket mint and transcription are all covered.
2. `list_enabled_triggers` filters inactive orgs **and** the final dispatch CAS
   requires an active org in its atomic predicate, closing the
   deactivate-after-enumeration race.
3. The run-stream WS re-authorises before **every** event (`_stream_access`), so a
   mid-stream deactivate/move/delete/password-reset/username-reuse stops delivery
   immediately — no cross-tenant leak on a move.

Admin cross-org surfaces are **not** blocked, so an admin can still reactivate a
suspended org.

**Session revocation via security stamp**: `users.security_stamp` is a random
per-account credential generation embedded in every token (`sec` claim) and WS
ticket, verified against the current row on use. A password reset regenerates it;
a deleted-then-recreated username gets a fresh one, so old credentials can't reach
the new same-named account. **An immutable random value, not a timestamp**, so
there's no ordering race.

**Identifier validation**: `db/validators.py::clean_identifier` enforces a
URL-safe grammar server-side (`[A-Za-z0-9._-]`, ≤64, and not the `.`/`..`
dot-segments proxies collapse), so a direct API call can't create an unmanageable
or path-unaddressable record.

## Logging and error reporting

`main.py` calls `logging.basicConfig` at import (level `BESTTEAM_LOG_LEVEL`,
default INFO) — a no-op when the root logger already has handlers, so pytest's
capture and an operator's own `dictConfig` win.

`error_reporting.py` is the one off-box channel: opt-in by
`BESTTEAM_SENTRY_DSN`, initialised with `default_integrations=False`,
`send_default_pii=False`, `max_request_body_size="never"`,
`include_local_variables=False`, no tracing — so the SDK adds **no** capture
points of its own.

**Exactly two call sites, and adding a third is a deliberate decision, not a
convenience. The rule is ids and names, never content.**
`main.unhandled_exception_handler` (tags method + the matched route *template* —
never the concrete path, whose parameter can be a capability token) and
`runtime.py` (the `run_failed` branch, tags only; and the worker catch-all).
`_scrub_event` drops every exception *message* — an output parser echoes the
model's text, an HTTP error carries the URL a tool fetched — keeping type, stack
(no locals) and tags.

Both helpers are no-ops without a DSN or SDK and never raise. `sentry-sdk` is in
the `ui` extra; a malformed DSN makes `init` raise at import (the backend refuses
to start), which `admin check-env` catches beforehand.

## Known limitation: general-purpose cache

Only local caches exist (`_pipeline_cache` in `main.py`, `Pipeline._compiled`) —
no shared or cross-request cache layer.
