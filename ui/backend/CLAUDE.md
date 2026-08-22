# bestteam — `ui/backend/` (FastAPI backend)

Directory-scoped notes for the FastAPI + WebSocket backend. See the root
`CLAUDE.md` for project overview, architecture, and commands; see
`ui/backend/db/CLAUDE.md` for the persistence schema and
`ui/frontend/CLAUDE.md` for the React frontend this API serves.

## Org multi-tenancy (row-level isolation)

One deployment can serve several customer **organizations** (see
`docs/DECISIONS.md`, "org-scoped multi-tenancy"; spec:
`docs/superpowers/specs/2026-07-15-org-multi-tenancy-design.md`). The rules
every endpoint follows:

- Org-owned rows carry `org_id`; org users only ever see their own org's
  data plus platform built-ins (`skills.org_id IS NULL`), and — only where
  `BESTTEAM_DEMO_PIPELINES` is on — the global YAML demo pipelines.
  **Cross-org access is a 404** (and the WS stream closes 4404 ==
  unknown-run) — existence is never revealed. This applies to an explicit
  `run_id` passed to a list-style filter too, not just a path parameter:
  `GET /api/runs?run_id=` and `GET /api/automation-results?run_id=` both
  404 up front (before running the filtered query) when that id belongs to
  another org or doesn't exist, rather than silently returning an empty
  list — an empty list there would still let a caller distinguish "not
  yours" from "doesn't exist" against the 404 every other explicit-id route
  gives (Codex review finding).
- Scoping is centralized: `get_current_org` (auth_api),
  `load_skills(db, org_id)` (org's own shadows a same-named built-in),
  `load_knowledge_base_tools(..., org_id=)`, org-filtered queries in
  crud/builder/main. The pipeline cache is keyed `(org_id, name)`; YAML
  demos cache under `(None, name)`.
- Component names are unique per `(org_id, name)`. KB upload dirs are
  `data/knowledge_base_uploads/<org_id>/<name>` (legacy un-prefixed dirs
  keep working — KB configs embed absolute paths).
- Admin surfaces (`/api/config`, `/api/memory`) are platform-wide: lists
  label each item's org and take an optional `?org=` filter; item routes
  require explicit `?org=<name>` (skills may omit it = built-in tier).
  **Platform admins are org-less accounts** (CR-030): `set_admin_status`
  refuses to promote org members, and `get_current_admin` + the run
  GET/stream passthrough require `is_admin AND org_id IS NULL` — an
  org-bound `is_admin` flag is never honored.
- Runs and usage_records carry `org_id` (denormalized — the future
  per-customer billing dimension); run GET/stream check org ownership with
  platform-admin read passthrough. Builder sessions are org-scoped. Runs
  also persist `username` — who started them (CR-032, audit-only; ownership
  stays org-level, and builder sandbox runs record it without a memory
  `user_id`).
- **Per-org email** is the multi-tenant path: each org connects its own
  mailbox (`admin set-email <org>`), stored encrypted in
  `org_email_credentials` (`db/email_credentials.py`; password via
  `secret_store` / `BESTTEAM_SECRETS_KEY`, a key distinct from the JWT one).
  `email_tools.load_email_tools(db, org_id)` resolves the running org's
  mailbox and is merged into `extra_tools` beside `load_knowledge_base_tools`
  at every pipeline-build site (main/builder/crud), overriding the env-based
  `email_*` tools in `REGISTRY` by name — so org A's agents reach only org A's
  inbox. `_dependency_freshness` includes `OrgEmailCredential`, so connecting/
  rotating a mailbox invalidates that org's cached pipelines. An org with no
  stored mailbox gets `{}` when `BESTTEAM_EMAIL_BACKEND` is set (env single-
  mailbox path still applies) or friendly "no mailbox connected" tools
  otherwise. Startup refuses to boot if stored credentials exist but the
  secrets key can't decrypt them.
- **Self-service mailbox connection** (`org_settings.py`, `/api/org/email`,
  guarded by `get_current_org` — the org's own user, no admin role): GET status
  (never returns the password), PUT set/rotate, POST `/test` (IMAP login on the
  posted creds without saving), DELETE. The customer-supplied host is SSRF-
  checked (`http_client.check_host_allowed`). The wizard shows the connect step
  only when the team uses email: `email_tools.spec_uses_email` resolves each
  agent's `tools` + skill tools and drives the `uses_email` flag on the builder
  session response. Drafts use current skill heads; deployed/synthetic team
  responses use the pipeline's pinned skill versions, matching runtime. The
  hard mailbox gate runs inside `component_mutation_lock` with validation and
  publication, so a concurrent skill edit cannot change capability after the
  check but before the pinned dependency is written.
- Process-wide email env vars (`BESTTEAM_EMAIL_*`) remain the single-mailbox
  path for the SDK/CLI and single-org deployments, and are still **refused**
  on a multi-org deployment (CR-031): `db/orgs.py::ensure_email_single_org`
  raises at startup and in `create-org` when `BESTTEAM_EMAIL_BACKEND` is set
  with more than one org. Graph/OAuth per-org and a customer-facing
  self-service settings UI are the next sub-project.
- Memory stays keyed by globally-unique username (no org dimension needed).
- The isolation test net: `tests/test_org_isolation.py` plus per-surface
  tests in test_crud_api/test_ws_stream/test_builder_api.

## Autonomous email trigger (`email_trigger.py` + `email_trigger_api.py`)

Opt-in per org (wizard Deploy page; `/api/org/email-trigger`): an asyncio
poller started from `main._lifespan` checks each enabled org's mailbox every
`BESTTEAM_TRIGGER_POLL_SECONDS` (default 120) and starts ONE run per cycle
covering that cycle's new messages, attributed to the sentinel username
`email-trigger`. Dedup is a per-org IMAP UID baseline in `email_triggers`
(never UNSEEN -- the toolkit never marks mail seen); the baseline is set to
the mailbox's current max UID at enable time so the backlog never triggers.
Guards: per-org daily cap (`BESTTEAM_TRIGGER_DAILY_CAP`, default 50),
platform kill switch (`BESTTEAM_TRIGGERS_DISABLED=1`), overlap guard (skips a
cycle while the previous triggered run is still `running`), and per-org
try/except so one org's mail-server failure never stops the loop (stored as
customer-readable `last_error` on the row). Single-process poller: if the
backend ever runs multiple workers, it needs a leader lock (known limitation).
An automatic run is confined to the poller-detected UID batch: it runs an
UNCACHED pipeline (`email_trigger.build_trigger_pipeline`) whose email tools
are UID-scoped (`make_email_tools(backend, allowed_uids=)`), so the triage
skill's `email_find` can only see that batch. State advances (baseline, cap)
only after the pipeline builds and a durable `runs` row is written; a build
failure consumes nothing. The advance is a compare-and-swap
(`UPDATE ... WHERE enabled = 1`): if the customer/operator disabled the trigger
or replaced the mailbox (a replacement disables via
`disable_trigger_on_identity_change`) between the enabled-check and the commit,
the update matches no row and the built run is discarded
(`registry.discard`) rather than dispatched against a just-disconnected mailbox.
Batch size: `BESTTEAM_TRIGGER_BATCH_SIZE` (default 20).

**Phase 0 hardening** (`docs/superpowers/specs/2026-08-17-email-phase-0-hardening-design.md`).
Five changes to the above:

1. **Draft idempotency no longer depends on the property-maintenance
   template.** `already_drafted_uids` now unions its `automation_item_results`
   lookup with `_trace_confirmed_uids`, which reads the retry family's
   persisted `tool_completed` trace events (`outcome == "draft_created"`) --
   evidence *every* run records. Previously a generic `email_triage_reply`
   team had no result rows at all, so a retry resubmitted a partially-drafted
   batch and created a second draft for every message already replied to, with
   no crash needed. `retry_triggered_run` additionally unions
   `_mailbox_drafted_uids`, a best-effort Drafts search on the
   `X-BestTeam-Source-Key` header that `build_trigger_pipeline` now stamps on
   every draft (`make_email_tools(..., draft_marker_prefix=)`, prefix shape
   identical to `automation_results._source_key`) -- the only way to see a
   draft that was APPENDed but whose trace event never got persisted. A scan
   failure is logged and ignored; it must never block a legitimate retry.
2. **Stuck-run watchdog.** `_release_stale_run` (both overlap guards) releases
   a run still `running` past `BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS` (default
   1800, validated at startup, minimum 60): cooperative cancel, mark the row
   failed, normalize, record the fault. A run cannot be forcibly killed, so
   this makes it non-blocking rather than stopping it. Previously one hung run
   closed an org's guard permanently and silently.
3. **Run outcomes reach trigger health.** `runtime._safe_record_trigger_health`
   (called from `_maybe_normalize`, so every terminal path) sets a sticky
   `workflow`-kind `last_error` (the stored value stays `"workflow"` — only
   the constant/identifier names around it were renamed, see the `Phase 3a`
   section below) when a triggered run fails/cancels and clears
   it on success. A `mailbox`-kind fault is left alone -- it is owned by the
   connectivity check. Before this, `runtime.py` never referenced
   `EmailTrigger` at all, so a team failing every run still showed "Active".
4. **Deploy refuses email + egress on one agent**
   (`deploy_validation.find_email_egress_conflicts` over
   `email_tools.resolve_agent_tool_sets`, wired into `builder.deploy_session`
   and `crud.upsert_pipeline_config`). The draft-only bound ("worst case is a
   bad draft") only holds while the agent reading attacker-controlled mail has
   no other route out; `http_get`/`web_search` is such a route. Deploy-time
   only, matching `validate_agent_models`.
5. **A mailbox is only "connected" if it is usable.** `PUT /api/org/email` now
   validates before storing, and both it and `POST /api/org/email/test` go
   through `_mailbox_problem`, which checks login *and*
   `_ImapBackend.check_drafts_writable()` (resolve the folder, SELECT it
   read-write, reject `[READ-ONLY]`; writes nothing). Every reply is an APPEND
   to that folder, so a login-only test passed mailboxes that failed on the
   first real draft.

**Phase 1: the durable inbox ledger**
(`docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md`).
Detection and execution are no longer the same act. `poll_org` records one
`inbox_events` row per detected message **in the same commit that advances
`last_uid`** (see `ui/backend/db/CLAUDE.md` for the schema), and
`_start_triggered_run` then *claims* up to `BESTTEAM_TRIGGER_BATCH_SIZE` of
them. This closes the window `_start_triggered_run` used to concede in its own
docstring: it advanced the cursor, persisted the run row and burned the cap in
one commit, then handed the pipeline to a thread pool -- a process killed
between those two points consumed the mail forever.

The failure handling splits by class, which is the rule to keep in mind when
touching any of it:

- **Infrastructure-class** (dispatch failure, the stale-run watchdog, a build
  failure, a trigger disabled mid-build): no model spend was incurred and the
  messages are innocent, so `release_events` hands them back. These paths never
  reach `runtime`, so they release at the site.
- **Pipeline-class** (anything reaching `runtime._maybe_normalize` -- the model
  actually ran): `_safe_complete_inbox_events` marks the messages Phase 0's
  `already_drafted_uids` proves a draft exists for as `done`, and the rest
  `failed`, awaiting the existing human retry. That is today's product
  behaviour, and it is why Phase 0's evidence layer is a dependency of Phase 1
  rather than something it replaces.

`attempts` is charged at **dispatch, never at claim**, so a build failure is
penalty-free and a broken team config retries forever instead of dead-lettering
an org's whole day of mail. `BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS` (default 3,
minimum 1, validated at startup) bounds the infrastructure-class retries; on
exhaustion the message is dead-lettered and `trigger.last_error` says so,
because nothing else would surface it. Detection is bounded at
`BESTTEAM_TRIGGER_BATCH_SIZE * 10` rows per cycle so a long outage cannot open
an unbounded transaction.

Two consequences worth knowing before editing: `last_uid` is **no longer
written by the CAS** in `_start_triggered_run` (detection already advanced it,
past a superset of the claimed batch), and "no message was consumed" is now an
assertion about event status, not about the cursor.

**This does not make the poller multi-worker safe.** The claim is atomic, which
removes one class of cross-process duplication, but `RunRegistry` is still
in-process, so the overlap guard and cooperative cancellation still assume a
single process and `_dispatch_lock` stays. Real horizontal scale-out is blocked
on a Postgres migration -- `make_engine` hardcodes SQLite and takes a file path,
not a URL.

**Phase 3a: trigger health and alerting**

`trigger_health.py` is a **pure** module -- one function, no I/O, no clock, no
DB. `evaluate(outcome, consecutive_faults, alerted_fingerprint, threshold)`
returns a `HealthDecision` (new counter, new fingerprint, optional
`NotificationDraft`). The whole noise-control policy lives there, so it is
tested by folding a sequence of outcomes rather than by driving a mailbox.

Two rules are load-bearing and easy to break by "simplifying":

- **Alerts fire on transitions, not occurrences.** `alerted_fingerprint` is the
  *set* of problems currently reported, sorted and comma-joined into the one
  column; a condition already alerted for stays quiet until it clears.
  Removing it turns every poll cycle into an alert. It is a set because two
  domains can be broken at once: when it held a single value, a mailbox fault
  overwrote an outstanding pipeline one and the next successful mailbox check
  then cleared it and announced a recovery that had not happened. A
  pre-existing single value parses as a one-element set, so no migration.
- **Recovery is domain-specific.** `OUTCOME_MAILBOX_OK` clears only a `mailbox`
  alert; `OUTCOME_PIPELINE_OK` clears the `workflow` fingerprint value (kept
  as `"workflow"` on purpose -- see below) and `run_timeout`. A single
  generic "healthy" outcome would let a successful mailbox check clear a
  pipeline alert -- exactly the "healthy trigger, every run failing" state
  Phase 0's item 0.5 exists to prevent (the same asymmetry `last_error_kind`
  already encodes as F5).

Three sites feed it and each keeps its existing `last_error`/`last_error_kind`
write untouched -- those drive the dashboard's error surface and are pinned by
Phase 0's tests; alerting is additive:
`runtime._safe_record_trigger_health` (pipeline outcomes, and note it now
returns early unless `trigger.last_run_id == run_row.id`, so a superseded run's
late outcome is ignored), `email_trigger`'s connectivity check (mailbox), and
`_release_stale_run` (timeout, which alerts immediately -- it has already been
stuck for the full run timeout).

Note: the underlying `fingerprint`/`last_error_kind` STRING VALUE that
identifies this class of fault is still the literal `"workflow"` (not
renamed to `"pipeline"`) -- only the Python constant names that hold it
(`FINGERPRINT_WORKFLOW`→`FINGERPRINT_PIPELINE`,
`_ERROR_KIND_WORKFLOW`→`_ERROR_KIND_PIPELINE`,
`OUTCOME_WORKFLOW`/`OUTCOME_WORKFLOW_OK`→`OUTCOME_PIPELINE`/
`OUTCOME_PIPELINE_OK`) were renamed, deliberately, to avoid a data backfill
of every already-stored fingerprint/`last_error_kind` value for a purely
cosmetic rename. See the `db/CLAUDE.md` `notifications` section and the
`email_trigger.py`/`trigger_health.py`/`runtime.py` source.

`notifications.py` delivers. Stdlib `http.client` (no new dependency),
HMAC-SHA256 over the exact posted body, five attempts then `failed` (still
readable in-app). HTTPS + `check_host_allowed` **at connect time, dialling the
validated IP** (`_PinnedHTTPSConnection`) and following **no** redirects:
`urlopen` re-resolved the hostname and followed redirects automatically, so a
tenant admin could point a webhook at a rebinding host or a public URL that
302s inward and walk past the SSRF check. Same pinning as `http_get` and the
per-org IMAP path (CR-023); a webhook receiver that redirects is unsupported.
Drained from the end of `poll_once`, not a thread of its own. **The payload carries health information only** -- adding a
subject or body to it would turn an alerting channel into an email-content
exfiltration path. An admin-configured webhook is not the model-chosen egress
`deploy_validation` refuses; the destination is fixed by a human.

`sweep_secret_expiry` warns at 30/7/0 days before a Microsoft 365 client
secret expires, keyed on an **admin-entered** date (`oauth_secret_expires_at`).
It is not read from Entra on purpose -- that needs `Application.Read.All`, a
directory-wide read over every app registration in the tenant.

**Phase 3b: retention, deletion and export**

`retention.py` is the engine; policy lives in `org_retention_settings`
(`db/retention.py`), NULL = keep forever, which is the default so an upgrade
deletes nothing. `retention_default_days()` (`BESTTEAM_RUN_RETENTION_DAYS`) is
applied by `db/orgs.py::create_org` to **newly created** orgs only.

The rule that is easy to break by "simplifying": **a purge clears content and
keeps accounting.** Content is `runs.input`/`output`, the run's `trace_events`,
and `automation_item_results.payload`. Accounting is the `runs` row itself
(deleting it would orphan every `usage_records` row that names it -- `run_id`
is nullable only so KB *ingestion* spend can omit it, and those rows back
`run_analytics_api.py`), `usage_records`, `trigger_context`, and an item
result's `status`/`source_key` -- those two are what
`automation_results.CONFIRMED_DRAFT_OUTCOMES` uses to exclude already-drafted
UIDs from a retry, so clearing them would make a retention sweep cause
duplicate drafts. `runs.content_purged_at`, not an empty field, is what marks a
run purged. `purge_run` refuses a `running` run (its worker is mid-write) and
is idempotent, because the sweep re-selects rows on overlapping cycles. It also
scrubs the run's entry in `RunRegistry` (`registry.purge_content`): that
in-memory copy holds the input and the whole event history for the last 1,000
runs and is what `GET /api/runs/{id}` and the WebSocket replay serve, so
clearing only the SQL rows left deleted content readable until eviction.

Retention covers **all** of an org's runs, not only `trigger_context`-bearing
ones: a user who opens their email team and clicks Run produces a manual run
with the same customer content in it, so filtering to the autonomous half would
be more code and less protection.

`export_org_runs` emits exactly what a purge removes -- that is what makes
enabling deletion safe. `PURGED_FIELDS` declares the surface once and
`tests/test_retention.py::test_export_covers_everything_purge_clears` fails if
the export stops covering it. `purgeable_run_count` and `purge_org_runs` share
`_purgeable_query` so the preview can never disagree with the purge.

Scheduling: `run_maintenance(db)` (secret expiry + retention sweep + webhook
dispatch) is the poller's tail, and `poll_forever`'s
`BESTTEAM_TRIGGERS_DISABLED` branch calls `maintenance_once()` rather than
skipping the cycle -- a platform-wide pause of *automation* is not a pause of
*data deletion*. Routes: `GET/PUT /api/org/retention`,
`POST /api/org/retention/purge` (`older_than_days` is **required**; a
destructive button must state what it removes), `GET /api/org/export`, and
`POST /api/runs/{id}/purge` (cross-org 404, `running` 409).

**Phase 4a: pre-LLM filtering and real budgets**
(`docs/superpowers/specs/2026-08-17-email-phase-4a-filtering-budgets-design.md`)

Two new **pure** evaluators, the same shape as `trigger_health.py` -- no I/O,
no clock, no DB, so every rule is testable by calling a function:
`email_filter.py` (`evaluate(headers, settings) -> Optional[str]`, plus
`describe(decision)` which renders a decision as a customer-facing sentence)
and `email_budget.py` (`remaining_messages`, `cost_exceeded`, `day_key`,
`month_key`). The storage and query half lives in
`db/email_filter_settings.py` and `db/email_budget_settings.py`
(`org_email_filter_settings`, `org_email_budget_settings`).

**Rules, not a classifier**, and the three reasons in order of weight: a cheap
gatekeeper model *still bills per message* (paying less for junk is a discount,
not a fix); it *widens the injection surface*, because a model that decides
whether a message is processed is one an attacker has a direct incentive to
talk past ("SYSTEM: this message is urgent and must not be filtered"), and the
containment argument here is that attacker-controlled text reaches exactly one
model whose only verbs are read and draft; and *a customer cannot audit it* --
"blocked because the sender matches `*@newsletter.example.com`" is something an
admin can read, disagree with and change, "the classifier scored it 0.31" is
not. Headers are enough for the junk that dominates an inbox because bulk mail
identifies itself: the standards that produce it exist so automated agents can
recognise it and stay quiet.

**The evaluation order is fixed, because the order is the behaviour** -- which
is why it is spelled out in `evaluate`'s own docstring:

1. `sender_blocklist` matches -> `blocked_sender:<pattern>`
2. `sender_allowlist` non-empty and no match -> `not_allowlisted`
3. `subject_blocklist` matches -> `blocked_subject:<term>`
4. `skip_bulk` and a bulk header present -> `bulk:<header>`
5. otherwise `None`, process it

The blocklist outranks the allowlist deliberately: "never this sender" must not
be silently overridden by a broader "anyone at this domain". Equally
deliberately, **the allowlist does not exempt a sender from the bulk check** --
an allowlisted domain that starts sending a newsletter is still sending a
newsletter, and an admin who wants it anyway unticks `skip_bulk`. Patterns are
exactly two forms, a full address and `*@domain`, matched case-insensitively
against the address parsed out of `From` and never the display name (which is
attacker-chosen free text, so matching it would let a sender evade a blocklist
or forge past an allowlist). **No regular expressions**: customer-supplied
regexes would bring catastrophic backtracking into the poll loop and no admin
could be told why a pattern did not match. Patterns are bounded per item (200
chars) as well as per list, because the poll loop reads every one of them.

**Filtering changes a row's `status`, never whether the row is inserted.**
The filter runs inside `poll_org`'s detection block (`_filter_decisions`,
between `check_mailbox` and `record_events`), and `record_events` still emits
one row per detected UID in the same commit that advances `last_uid` -- Phase
1's durability guarantee, that the commit consuming the mail is the commit
recording it, is untouched. A filtered row is simply written `status="filtered"`
with the decision in `decision`. `claim_events` already selects `pending` only,
so **the claim, dispatch, retry and completion paths change not at all**, and
releasing a false positive is a single `filtered` -> `pending` flip
(`release_filtered_event`) that the next cycle claims like anything else. That
is why "record, show, allow release" beat "drop and count": a rule-based filter
*will* have false positives (a real supplier does send from `noreply@`), and
the cost of one has to be "an admin clicks Release", not "the enquiry was
silently lost and nobody ever knew".

**`skip_bulk` defaults to `True`** for an org with no row -- the one deliberate
behaviour change on upgrade. A safety feature nobody switches on protects
nobody, and the default is recoverable: one checkbox turns it off, and every
filtered message stays visible and releasable. Both budget caps, by contrast,
default to NULL: an upgrade must not start refusing to process a customer's
mail because a limit they never set appeared.

`_ImapBackend.summaries_for` now also fetches `AUTO-SUBMITTED`, `PRECEDENCE`,
`LIST-ID` and `LIST-UNSUBSCRIBE` (still `BODY.PEEK` -- the draft-only toolkit
never marks mail seen and this must not become the thing that does), which
**costs one extra IMAP login per poll cycle that finds new mail**. Accepted:
threading the open connection out of `check_mailbox` reshapes an interface used
by three call sites to save one login on cycles that are by definition not the
common case, and connection pooling is separately identified work. A UID whose
headers cannot be fetched is recorded `pending`, not `filtered` -- **fail
open**, since the worst case of failing open is one junk message processed,
while the worst case of failing closed is a customer's mail silently discarded.

**Three limits, two audiences.** `BESTTEAM_TRIGGER_DAILY_CAP` (runs/day,
default 50) is the *operator's* deployment-wide rail and is unchanged: it
measures the wrong thing for a customer promise, which is why the other two
exist, but it bounds a runaway poller regardless of what any org configured.
`daily_message_cap` and `monthly_cost_cap` (`org_email_budget_settings`) are
the *customer's*, set by an admin in the UI. Both are read in
`_start_triggered_run`, inside the caller's `_dispatch_lock` and immediately
before the claim -- the same staleness rationale `_at_daily_cap`'s own docstring
gives, since the pre-lock fast-path read can be stale by the time dispatch
happens. The message cap then truncates the claim
(`limit=min(batch_size(), remaining)`) and returns before `claim_events`
entirely at `remaining == 0`, so no run is dispatched and nothing is even
claimed; `messages_today` advances in the same CAS `update` that advances
`runs_today`, so it counts messages genuinely handed to a model (the existing
"charged at dispatch, never at claim" rule). Monthly spend is
**queried, never counted into a column**
(`SUM(cost_estimate)` over `usage_records` from the first instant of the UTC
month; `usage_records.org_id` is denormalised for exactly this) -- a stored
counter would need its own reset, backfill and drift bug. `messages_today`
shares `runs_date`, so **both** rollover sites (`poll_org` and
`retry_triggered_run`) reset both counters; a site that rolls one without the
other carries a stale count into the new day and nothing later clears it.

Hitting a cap stops dispatch, alerts **once**, and resumes automatically when
the period rolls over. Not a hard disable: a budget reached on a Saturday must
not need a human on Monday, and a trigger that disables itself is
indistinguishable in the UI from one the customer turned off. Unprocessed
messages stay `pending`, so the backlog drains rather than being lost.

**"The next check picks it up" is only true because `poll_org` no longer
returns on an empty detection.** Both of this phase's new features create
`pending` rows for a reason other than mail arriving -- an admin releasing a
filtered false positive, and a backlog a cap declined to dispatch -- and
`_start_triggered_run` used to be reachable only from a cycle that found *new*
UIDs, so on a quiet mailbox neither drained until unrelated mail happened to
land. The empty-detection branch now consults
`db/inbox_events.py::has_pending_events` (a scoped `SELECT ... LIMIT 1`, so an
idle poll stays as cheap as it was) and falls through to the ordinary
cap-check-then-dispatch tail when this org has claimable work. The durability
sequence is untouched: `record_events` -> `last_uid` -> `commit` still happens
only on the detection branch, and still as one unit.

**Budget alerts bypass `trigger_health.evaluate` deliberately.**
`_raise_budget_alert` calls `has_fingerprint` + `create_notification` directly
(`kind="budget"`), the way `sweep_secret_expiry` already does. Two reasons: a
budget ceiling is a *normal operating state, not a fault*, and feeding it to
the fault evaluator would corrupt `consecutive_faults` and compete with real
faults for `alerted_fingerprint`. The fingerprints are **period-scoped** --
`budget_messages:<UTC date>`, `budget_cost:<UTC month>` -- following
`_expiry_fingerprint`'s precedent, because `has_fingerprint` searches an org's
*entire* notification history: a bare name would alert once ever and every
later period would be silent.

**Unpriced models.** `record_usage` writes `cost_estimate = NULL` when a spec
has no `model_catalog` entry, so a naive `SUM` under-counts and the customer
believes in a ceiling that does not hold. Three-part answer, chosen over
"refuse to run" (one missing catalogue row would wedge a customer's automation)
and over silence: at configuration time `unpriced_models_for_org` resolves the
agent models of **every `status="deployed"` team in the org** -- not only the
one the trigger points at, since the cap it advises is an org-level `SUM` over
the whole ledger and a team deployed without a trigger spends into it just the
same -- **and the billable `embedding_model`/`query_expansion_model` of the
knowledge bases those teams search, every standalone knowledge base the
org owns (the "Try a search" panel spends against any of them with no team
involved), plus the operator's `BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL`** --
against the catalogue, and the budget routes return a non-blocking `unpriced_models`
list (**the cap saves either
way** -- the admin may be about to fix the catalogue); at runtime NULL
contributes 0, so the cap is a floor on reality rather than a phantom ceiling;
and the UI reports how many runs this month were unpriced. Knowledge bases are
in that list because they are the one spender the run-shaped half of this
answer cannot see at all: an *ingestion* row and a `kb:search` row are both
written with `run_id = NULL`, so `unpriced_run_count`'s `count(distinct
run_id)` never counts them, and an unpriced embedding model would otherwise be
silent in both halves. `fake:`
specs are excluded there (`core/embeddings.py::billable_spec`, the same
definition the metering uses) even though an agent's `fake:` model is not --
an unmetered $0 call is not a blind spot. `rerank_model` is absent for the same
reason: reranking is a local cross-encoder and is never recorded. The helper is
wrapped in one `except Exception -> []` (advisory copy must never fail a save),
reads `PipelineRecord.config` rather than building the pipeline, and scopes to
`status="deployed"` on purpose -- a trigger pointing at a draft cannot run, so
it cannot spend (which does mean an undeployed team's models are not warned
about). Note the whole cap bounds an **estimate**: `cost_estimate` is computed
from `model_catalog` prices an operator maintains by hand, and no provider
invoice is ever reconciled against them.

Routes, each `Depends(get_current_org)`-scoped in the file that already owns
the concern: `GET/PUT /api/org/email-filter` and `GET/PUT /api/org/email-budget`
(`org_settings.py`), `GET /api/org/email-trigger/filtered` and
`POST /api/org/email-trigger/filtered/{id}/release` (`email_trigger_api.py`).
Release is idempotent and returns **404** for an unknown id, another org's row
and an already-released row alike -- never 403, which would confirm the row
exists. `GET /api/org/email-budget` reports `messages_today` as 0 unless
`trigger.runs_date` is still today, the same guard `email_trigger_api._payload`
applies to `runs_today`: the poller resets the counters on the first cycle of a
new day, so reading the column raw would show an admin yesterday's total on the
very card that explains their cap.

Three limitations of the shipped behaviour, recorded so they are not
rediscovered as bugs. The **submit-failure path double-charges `messages_today`**: the CAS
commits, `release_events` then returns those messages to `pending`, and a later
cycle claims and charges them again. `runs_today` has always behaved
identically on that branch; decrementing was rejected because it would add a
second write site outside the CAS, to correct an over-count bounded at one
batch on a branch that only fires while the executor is shutting down.
**`retry_triggered_run` enforces neither new cap** and does not advance
`messages_today`, so a human-initiated retry of one failed batch can put an org
past its daily message cap -- arguably right (a human asking to redo one batch
is not autonomous spend), but it is a real hole in the number the UI shows.
And **the spend cap is enforced between runs, not within one**: a single run
that blows through the cap is not interrupted, because cancelling a
partly-drafted batch costs the money already spent and delivers nothing for it.

**Phase 2: Microsoft 365 mailboxes**

Exchange Online no longer accepts basic auth, so an M365 org could not connect
a mailbox at all. `org_email_credentials.auth_type` now selects how
`email_tools.build_org_imap_backend` authenticates: a password login, or an
app-only OAuth token (`bestteam.tools._oauth.MicrosoftClientCredentialsToken`)
handed to `_ImapBackend` as `token_provider=`, which makes `_connect()` use
SASL `XOAUTH2`. **Nothing in `email_trigger.py` or `runtime.py` changed** --
after `AUTHENTICATE` the session is an ordinary IMAP session, so the UID
cursor, the drafts resolution, Phase 0's source-key headers and the ledger
above all work untouched. That is why this was built as an auth strategy rather
than a Graph connector; see `docs/DECISIONS.md`.

Two things in `org_settings.py` are load-bearing. The host for
`microsoft_oauth` is set **server-side** to `outlook.office365.com` and any
client-supplied value is discarded -- the OAuth scope is bound to that
endpoint, so another host could only fail confusingly. And `_mailbox_problem`
fetches the token as its own step *before* connecting, so a credential problem
(wrong client ID, expired secret, unknown tenant) is distinguishable from a
mailbox-access problem (admin consent or `Add-MailboxPermission` missing):
those have completely different fixes and cannot be told apart from Microsoft's
error text, only from which step failed. A working token with a refused mailbox
is the likeliest outcome of a half-finished Azure setup.

Round-2 hardening (independent-reviewer follow-up on PR #22): `poll_org`
resolves the IMAP backend once per cycle and threads it into
`build_trigger_pipeline` instead of letting it re-fetch credentials
independently -- closes a race where a mid-cycle mailbox swap could detect
mail on one mailbox and build tools against another. `admin.py`'s
`set-email`/`clear-email` now call the same `email_trigger.disable_trigger`/
`disable_trigger_on_identity_change` helpers as `org_settings.py`, so the
operator CLI path disables the trigger on mailbox change too (previously
only the wizard path did). `EmailTrigger.last_error_kind` (`"mailbox" |
"workflow" | None` -- the `"workflow"` string value is unchanged by the
Workflow→Pipeline rename, see the Phase 3a note above) distinguishes a
connectivity fault, which auto-clears on
the next successful mailbox check, from a pipeline/dispatch fault, which
still persists until a real successful dispatch (F5, unchanged). A dispatch-
submission failure now marks the run failed (`last_error_kind = "workflow"`,
so it stays sticky the same way, rather than getting auto-cleared by an
unrelated successful mailbox check) instead of leaving the overlap guard
wedged. `BESTTEAM_TRIGGER_*` env values are validated at startup
(`email_trigger.validate_trigger_env()`, called from `main.py` beside the
`BESTTEAM_SECRET_KEY` guard) instead of being able to silently kill the
poller mid-loop. Deferred: awaiting in-flight polling threads on shutdown (see
`docs/STATUS.md`, Known issues). `RunRegistry` eviction (the other
previously-deferred item) is no longer deferred -- see "Sync-to-async
streaming bridge" above.

`_start_triggered_run` also stamps a `Run.trigger_context` JSON blob
(mailbox credential id, host, username, UIDVALIDITY, the exact UID batch,
folder, trigger time) -- the server's own record of what a triggered run
covered, used by both automation-result normalization and
`retry_triggered_run` below. Never trust a model's own claim about which
messages it processed; this is that ground truth. Host/username are stamped
alongside the credential id because `set_email_credentials` upserts one row
per org -- the row id alone never changes even when the org replaces its
mailbox entirely, so it isn't a usable identity check on its own.
`email_trigger.retry_triggered_run(db, run_row)` (spec section 11.2) safely
reruns a **failed** (only -- never `completed`, which may already have real
mailbox side effects like a saved draft) triggered run over its exact
original UID batch as a brand-new `Run` (`retry_of_run_id` set, history
untouched): revalidates the current mailbox's host/username still match
`trigger_context` (not just UIDVALIDITY -- a replaced mailbox could
coincidentally share a UIDVALIDITY value), the mailbox still decrypts and
connects, the pipeline still builds, the org's daily cap isn't already hit,
and -- same overlap guard `poll_org` itself checks before touching the
mailbox -- `trigger.last_run_id` isn't still a registered run that's actually
running; any of these raises `RetryError` (customer-facing message). It also
excludes any UID the original run already got a confirmed draft for
(`already_drafted_uids(db, run_row)`, matched via each UID's
mailbox/UIDVALIDITY-scoped `source_key` against
`AutomationItemResult.payload.action.draft_created`) before resubmitting the
batch, raising `RetryError` outright if every UID already has one --
`email_draft_reply` has no dedup of its own, so blindly resubmitting a
partially-drafted batch risked a second draft landing in the mailbox.
`already_drafted_uids` checks every run in `run_row`'s **retry family**
(`_retry_family_run_ids`: walk back through `retry_of_run_id` to the root,
then forward to every run reachable from that root), not just `run_row`'s
own results -- a retry family is a tree, not always a straight line, since
the *original* run can be retried more than once (e.g. a first retry fails
too, and the customer retries the original again rather than that failed
retry, creating a sibling branch); checking only `run_row` would miss a UID
a sibling branch already drafted (Codex review finding). The
narrowed `retry_uids` list (not the original full batch) is what gets passed
to `build_trigger_pipeline` and into the new run's `trigger_context["uids"]`
-- narrowing `trigger_context` too, not just the tool-visible UID set, is
what keeps `normalize_run_result` on the *new* run from treating an
intentionally-excluded already-drafted UID as "missing" and synthesizing a
spurious error row for it under the new run id. This exclusion depends on
`AutomationItemResult` rows reliably existing for the whole batch of any
`failed` run, which is what "every terminal path normalizes" (below)
guarantees -- including a run that never reached the model at all.

The overlap-check-through-dispatch section of both `poll_org` and
`retry_triggered_run` (the `last_run_id` read through the actual dispatch
call) is serialized behind a per-org `threading.Lock`
(`_dispatch_lock(org_id)`, a small lazily-created registry keyed by org id)
so two concurrent dispatches -- retry-vs-poller, or retry-vs-retry -- can't
both observe "no run in flight" and both fire against the same mailbox. The
lock alone isn't sufficient: an ORM `trigger` object loaded before lock
acquisition (a different `Session`, e.g. a different HTTP request or poll
cycle) keeps whatever stale `last_run_id` value it had at load time even
after another thread commits a newer one. `_current_last_run_id(db,
trigger)` does a fresh column-only `SELECT` inside the lock instead --
deliberately not `db.refresh(trigger)`, which would also discard each
function's own pending uncommitted daily-cap-reset change made earlier in
the same call, before the lock. On dispatch, `retry_triggered_run` sets
`trigger.last_run_id` to the new run (registering itself with that same
guard for the next poll cycle) and clears `last_error`/`last_error_kind`,
mirroring `_start_triggered_run`'s "a run is going out: clear any prior
fault" -- without it a resolved `"workflow"`-kind error kept reporting failure
indefinitely despite the successful retry. Exposed as
`POST /api/runs/{run_id}/retry` in `main.py`. The per-org lock also closes
the previously-deferred, narrower "two near-simultaneous manual retry
clicks on the same run" admission race (`docs/STATUS.md` Known issues no
longer lists it) as a side effect, since the second click's check is now
serialized behind the first's. Remaining known gap: a `completed` run whose
output failed *normalization* (not a real engine failure) currently has no
retry path at all -- see `docs/STATUS.md` Known issues.

The daily cap has the same staleness problem as `last_run_id`: the check
further up (before mailbox/pipeline work -- a fast-path to skip unnecessary
IMAP calls when obviously already at cap) reads a possibly-stale
`trigger.runs_today`, so two dispatches that both read "under cap" before
either committed its increment could both pass and push the count past the
cap. `_at_daily_cap(db, trigger, today)` re-checks with a fresh `SELECT`
immediately inside the lock, right before dispatch -- the actual gate.
`retry_triggered_run`'s cap advance is also now a single atomic
`UPDATE ... SET runs_today = runs_today + 1` (mirroring
`_start_triggered_run`'s CAS), not a Python-level `trigger.runs_today += 1`
-- the latter would silently lose a concurrent dispatch's increment for the
same org instead of merely racing the cap check. `retry_triggered_run`'s
retry input (`run_in_background`'s `input` argument and the new `Run.input`)
is built from `retry_uids` (`_trigger_input(retry_uids)`), not reused from
the original run's `input` -- the original text names every UID in the
initial batch, including any just excluded by the already-drafted check, and
passing that stale text would instruct the retrying agent to work on
messages its own scoped tools then reject as out-of-batch instead of the
UIDs that actually still need it. If submission itself fails (`_executor.submit`
raises) in either `_start_triggered_run` or `retry_triggered_run`, the worker
never starts and so never normalizes either -- both submission-exception
branches now call `normalize_run_result` explicitly after marking the run
failed, or a declared batch would be marked failed with zero
`automation_item_results` rows and silently vanish from Needs-attention
(all four gaps above: Codex review findings). In both branches,
`normalize_run_result` is now called **before** `registry.publish`-ing the
`run_failed` event -- same ordering rule as `run_in_background`'s normal
terminal path below: publishing first left a window where a live Run Detail
view reacting to the event could fetch zero automation rows before this
commits them, with no later terminal transition to prompt a re-fetch (Codex
review finding).

`retry_triggered_run`'s dispatch-time `UPDATE` also requires
`EmailTrigger.enabled` and the org still active in its `WHERE` clause,
mirroring `_start_triggered_run`'s own CAS -- without it, a customer
disconnecting/replacing the mailbox (or an operator deactivating the org)
during this call's own pre-lock credential/mailbox check went undetected,
and the retry would dispatch a real `email_draft_reply` against a mailbox
the customer had already disconnected. A rejected CAS (`rowcount == 0`)
discards the pending new run from the registry and rolls back the session
(undoing the not-yet-committed new `Run` row too) before raising a
customer-facing `RetryError`. The "retry already running"
(`Run.retry_of_run_id == run_row.id, status == "running"`) and
already-drafted-UID checks are also re-run fresh immediately before
dispatch, inside the per-org lock -- the equivalent checks earlier in the
function (before the mailbox connectivity check) are only a fast-path and
can be stale by the time execution reaches the lock: two retry requests
racing the same failed run could otherwise both pass those checks on data
that predates the first one's own dispatch/normalization, and since
`email_draft_reply` has no dedup, both would create a duplicate draft (both
gaps: Codex review findings).

## Property Maintenance Inbox (`automation_results.py`)

The first vertical solution template (Release 1A of
`docs/superpowers/specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md`):
a two-agent SEQUENTIAL Pipeline template
(`pipelines/property_maintenance_inbox_demo.yaml`) built from three platform
Skills (`email_input_security_core_v1`, `property_maintenance_intake_v2`,
`property_maintenance_response_v1`, seeded in `skills.py`; `_intake_v1` is
still seeded but no longer referenced by the template — Phase 4b added
attachment reading as a new version rather than editing the old one, so a
team already pinned to `_v1` keeps the behaviour it deployed with) on top of the
existing email-trigger/draft-only toolkit above. `email_input_security_core_v1`
is attached to BOTH agents, not just the Intake Analyst: the Response
Coordinator never calls `email_find`/`email_read` itself, but it drafts from
the Intake Analyst's free-text write-up, which can itself quote injected
instructions from the original email -- without the same defenses, a
malicious message could still steer the Response Coordinator even though it
never reads the mailbox directly (Codex review finding). Deliberately **not** a
`Case`/work-item entity -- see `docs/DECISIONS.md` ("Property Maintenance
Inbox: no Case/work-item entity in Phase 1").

`automation_results.py::normalize_run_result(db, run_row)` is called from
`runtime.py::run_in_background` on **every** terminal path a run with a
`trigger_context` can take -- not just the streaming loop's
`run_completed`/`run_failed` branch, but also `_mark_cancelled()` (a
cancelled run) and the outer exception-fallback handler (a crash before the
first stream event) -- via a shared `_maybe_normalize()` closure so a
cancelled or pre-stream-crash triggered run's UID batch never silently
disappears from Needs-attention the way it used to (spec 10.1's "a
model-omitted UID must never silently disappear" applies just as much to a
run that never reached the model). Each of the three paths commits the
terminal status to `run_row` *before* setting the loop's `terminal_seen`
flag, not after -- so if that commit itself raises, `terminal_seen` is still
`False` and the outer exception handler's own best-effort `run_failed`
fallback (and its own `_maybe_normalize()` call) still runs, instead of the
run looking permanently "running" to a live subscriber. And, since the
commit must be visible before any WS subscriber can react to the terminal
event it's about to see, each `_maybe_normalize()` call happens **before**
`registry.publish` for that event, not after (a live Run Detail refetching
automation-results the instant it observes the terminal event used to be
able to race ahead of these rows with no retry to pick them up later). It
extracts a JSON object from the
run's final output (handling a ```json fence) and only proceeds if
`result_type == "property_maintenance_email_batch"` -- any other
`trigger_context`-bearing run (e.g. another org's plain `email_triage_reply`
team, free-text output) is left completely untouched, so this never
regresses unrelated email-trigger pipelines. The one exception: a run that
crashed (`run_failed`) before producing any JSON at all looks identical, from
the output alone, to that unrelated case -- so `_start_triggered_run` stamps
`trigger_context["result_contract"] = RESULT_TYPE_BATCH_MARKER` at dispatch
time whenever the deployed pipeline's config gives an agent the
`property_maintenance_response_v1` skill AND that name still resolves to the
actual platform-tier skill row, not an org skill shadowing it
(`email_trigger._declares_property_maintenance_contract` +
`_resolves_to_platform_skill`, a small independent `PipelineRecord.config`
read -- advisory only, never blocks dispatch on failure; `skills.load_skills`
intentionally lets an org's own skill shadow a same-named platform built-in,
so a name-only check would wrongly redact/stamp an org's unrelated pipeline
that happens to name its own skill the same thing (Codex review finding)),
and an unparseable output still gets the batch's synthesized
error rows when that marker is present. `retry_triggered_run` does NOT just
carry the marker forward from the original run's `trigger_context` -- it
re-runs `_declares_property_maintenance_contract` against the pipeline as
CURRENTLY deployed and sets/clears the retry's own marker from that fresh
result, so a pipeline that gained or lost the maintenance skill between the
original run and the retry gets the right redaction/normalization behavior
either way, instead of a stale one carried over from dispatch time (Codex
review finding). Once engaged: the whole envelope is validated via Pydantic
(`Envelope`/`EnvelopeItem`; an enum/shape failure fails the *whole* batch,
not per-item). A validation failure is logged as `loc`/`type` per error only
-- never `exc`/`str(exc)` directly, since Pydantic's error repr embeds the
offending input value, and a prompt-injected email could steer the model
into putting body content or PII into an invalid enum/id field, putting raw
customer content into server logs despite the trace redaction above (Codex
review finding). Each item is matched by `message_id` against
`trigger_context["uids"]` (an id outside that set is logged, capped to 64
chars rather than the raw value -- `message_id` has no length cap on the
Pydantic model and is entirely model-controlled, so an out-of-batch id is
exactly the case a prompt-injected email could steer into arbitrary body
text (Codex review finding) -- and dropped, so the model can't expand its
own scope); every UID in the batch gets exactly
one `automation_item_results` row, including a synthesized
`status="error", needs_attention=True` row for one the model omitted or for
a whole-envelope/unparseable-output failure (nothing silently disappears);
`needs_attention` is server-computed (`possible_emergency`/`unknown`
priority, `needs_attention`/`error` status, or a confirmed tool failure for
that UID -- see below -- always forces it, regardless of what the model
itself claimed); and `payload` is length-capped and only ever holds the
validated extraction fields -- never a raw email body. `source_key` is
always server-generated (`mailbox:<credential-id>:uidvalidity:<value>:uid:<uid>`),
so a model can never fabricate which input it claims to have processed. The
`(run_id, source_key)` unique constraint makes a repeated normalize call (a
duplicate completion callback, or calling it twice) a no-op for rows already
written.

`action.draft_created` is also never trusted from the model alone (post-review
hardening): `normalize_run_result(db, run_row, confirmed_draft_message_ids=)`
takes the set of message ids the run *actually* got a successful
`email_draft_reply` tool call for -- `runtime.py::run_in_background` collects
this itself from the run's own `tool_completed` trace events (`outcome ==
"draft_created"`, from `adapters/langgraph_adapter.py`'s redacted email-tool
data) while it's already streaming them, and passes it into normalization at
the terminal event. A claimed-but-unconfirmed draft is downgraded to
`draft_created: false` and forces `needs_attention: true`; symmetrically, a
claimed-`false` that the trace confirms *did* succeed is upgraded to `true`
-- the model can misreport in either direction, and a stored result (or the
daily summary's count) under-reporting a draft that genuinely exists in the
mailbox is just as wrong as over-reporting one that doesn't. The same trust
boundary now covers tool *failure*, not just draft success: a failed
`email_read`/`email_draft_reply` tool call retains its (bounded, and now
stripped -- `_bounded_message_id` mirrors the email tools' own `.strip()`
normalization, or a call made with whitespace-padded id like `" 42 "` would
record that unstripped id in trace evidence while this comparison uses the
envelope's stripped id, missing the match and leaving a real draft
unrecognized as confirmed; Codex review finding) `message_id` in the trace
even on the exception path
(`adapters/langgraph_adapter.py`), `runtime.py` collects those into a
parallel `failed_tool_message_ids` set, and `normalize_run_result` forces
`needs_attention` for that UID regardless of what the model's own item
claims (spec 9.5 "Tool failure -> needs_attention: yes" -- previously only
enforced by trusting the model to self-report it). The same collector also
adds a UID whenever its `tool_completed` event reports `outcome in
("not_found", "out_of_batch")` even though the call itself didn't raise --
`_redacted_email_tool_data` labels those `success: True` since nothing
exceptioned, so without this a soft rejection (a since-deleted message, a
scope violation) stayed hidden from Needs-attention unless the model
self-reported it too. `Envelope.schema_version`
(currently always `1`) is validated against the one supported version rather
than accepted as any int -- `extra: "ignore"` would otherwise let a future
incompatible schema's unknown fields get silently dropped instead of failing
the envelope; an unsupported version gets the same whole-batch error-row
treatment as an invalid enum. `draft_type` is length-capped like every other
free-text payload field (it was the one field that wasn't).

**A knowledge base tool's `tool_completed` no longer carries document body
text** (P0-5). It used to be `_summarize(result)` — the first 200 characters of
the retrieved excerpts, i.e. an org's own indexed documents, in every
`trace_events` row and every UI that renders one. The adapter now builds that
event from what the tool reported through `core/tool_context.py`:
`summary` plus `query` (≤200 chars), `hit_count` and `sources` (at most 10).
A source is a *citation label*, not an excerpt: the filename, then `, p.<n>`
for a PDF and ` § <heading>` for Markdown (a heading is document text, capped
at 80 characters — it is what makes a citation findable, and it is the only
document text that crosses this boundary). `summary` stays, so `lib/traceEvents.ts`,
`RunDetail`, `TracePage` and the `trace_events` persistence need no change —
the three new keys ride alongside in `data`. This is an SDK/adapter-layer
boundary like `_redacted_email_tool_data`, not a `runtime.py` one.

A property-maintenance run's raw agent output (`agent_completed`'s `data`,
and `run_completed`'s -- `core/pipeline.py`'s `last_output`, the same text)
is derived from customer email content -- the envelope's free-text
`extracted`/`missing_information`/`risk_reasons` fields can quote it
directly -- so it gets the same redaction boundary that already covers
`tool_completed` for `email_find`/`email_read`/`email_read_attachment`/
`email_draft_reply`
(`_redacted_email_tool_data`), just applied one layer up in `runtime.py`
instead of at the SDK/adapter layer (the SDK itself has no notion of
"property maintenance"; only `runtime.py` knows a run's `trigger_context`).
`run_in_background` computes `is_pm_contract_run` once, from
`run_row.trigger_context["result_contract"]`, right after the run row is
persisted; for such a run, every event whose type is in
`_PM_REDACTED_EVENT_TYPES` -- `agent_completed`/`run_completed` plus, for a
declared maintenance pipeline that happens to use HIERARCHICAL mode,
`subagent_started`/`subagent_completed`/`delegation_started`/
`delegation_completed` (the manager/subordinate delegate exchange carries
the same customer-email-derived text -- `task_summary`/`summary` -- and
previously leaked around this boundary, Codex review finding) -- OR is a
`tool_completed` event for the manager's own `delegate_to_<name>` tool call
(`_is_delegate_tool_completed`; `adapters/langgraph_adapter.py`'s generic
tool-calling loop emits this as a SECOND, separate event carrying the same
subordinate `summary`, which the `on_event`-driven redaction above doesn't
see -- Codex review finding; a non-delegate `tool_completed`, e.g.
`email_read`, is unaffected) -- has its
`data` overwritten with a fixed placeholder (`_PM_TRACE_REDACTED`)
*before* `dataclasses.asdict(event)` is built for `registry.publish`/
`_safe_record_trace_event` and before it lands in `run_row.output` -- so the
raw text never touches a live WS broadcast, persisted `trace_events`, or
`runs.output`. `normalize_run_result` still needs the real JSON: the
`run_completed` branch captures `event.data` into a local before redacting
it, then passes that through as `_maybe_normalize`'s new
`raw_output_override` (threaded down to `normalize_run_result`/`_normalize`,
which parses it instead of `run_row.output` when given) -- so the structured
result is exactly as complete as before, only the trace/output columns lose
the raw text. `agent_completed`'s `usage` is untouched by this (only `.data`
is mutated), so usage metering is unaffected. Every other run (no
`trigger_context`, or a `trigger_context` without the maintenance
`result_contract`) is completely unaffected -- this is the same
narrow-scoping discipline as the `result_contract` marker itself (Codex
review finding).

Read APIs (`main.py`): `GET /api/automation-results` (org-scoped, filterable
by `run_id`/`needs_attention`/`status`/`result_type`, offset/limit/total --
same convention as `GET /api/runs`, not the design spec's `cursor` wording)
backs the Activity page's Needs-attention list and Run Detail's
automation-results section; `GET /api/automation-results/summary` backs the
Activity page's daily counters card, including an `ever_used` flag (a cheap
all-time existence check, separate from the day's counts) so the frontend can
tell "this org has never used this template" apart from "used it, nothing
today" -- both would otherwise report `emails_read: 0`. `date` defaults to
UTC "today" server-side when omitted -- there's no org-timezone concept
anywhere in this app (consistent with `email_trigger.py`'s daily-cap reset,
also UTC-based), so the frontend passes its own local date explicitly rather
than relying on that default (`MaintenanceInboxSummary.jsx`). The endpoint
also takes `tz_offset_minutes` (the browser's own `Date.getTimezoneOffset()`;
`lib/api.js`'s `automationResultsSummary` always sends it) and
`summary_for_date` uses it to bound the day by the caller's local midnight
instead of UTC midnight -- passing a local date string alone still misdates
rows created in a timezone-ahead-of-UTC org's first few local hours of a new
day, since those rows' UTC timestamp is still the previous UTC date.

## Anonymous team sharing with continuous chat (`share_links_api.py` + `share_chat.py`)

An org member shares one deployed team via a revocable link; a colleague
opens it, never logs in, and gets a real multi-turn conversation. Full
design: `docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md`.
Schema (`share_links`/`share_sessions`/`share_messages`): `db/CLAUDE.md`.

Two routers, deliberately separate surfaces:

- **`share_links_api.py`** (`/api/pipelines/{id}/share-links`,
  `/api/share-links/{id}`, `.../sessions[/{id}/messages]`) -- org-side
  management, every route behind `get_current_org`. Create/list/revoke a
  link, and audit any visitor session's transcript. `expires_at` is
  normalized to naive UTC on the way in (the column is naive and
  `share_chat._is_expired` compares against naive UTC).
- **`share_chat.py`** (`/api/share/{token}/messages`, and a WS at
  `/api/share/{token}/stream/{run_id}`) -- the public, anonymous surface. No
  `users` row is ever created. Every route re-validates link
  active/expiry/org-active state fresh from the DB, and the WS re-checks it
  before delivering each event (same philosophy as `main.py::stream_run`'s
  per-event `_stream_access`). Every "can't use this link" case returns the
  same single 404 detail, including "the team isn't deployed" -- a
  distinguishable message there is an existence oracle.

**Auth is a signed session cookie** (`share_auth.py`), not a JWT: a visitor
has no account, no org, and no `users` row, so nothing `get_current_user`
returns could describe them -- and sharing a team must not require lifting
the one-member-per-org invariant (`docs/DECISIONS.md`). The cookie carries
only an opaque `ShareSession.session_token`, HMAC-signed with
**`auth.SECRET_KEY`** -- so rotating that key invalidates every in-progress
visitor session (as well as every JWT), by design. The WS needs no `?ticket=`
workaround: unlike an `Authorization` header, a cookie IS sent on the
handshake. **Operational note:** the cookie is `SameSite=Lax`, so the
frontend and this API must be served same-site (same registrable domain;
ports don't matter) or it never comes back and every message silently starts
a new session. A genuinely cross-site deployment needs `samesite="none"` +
`secure=True` + HTTPS -- a deliberate config decision, not a silent change.

**A turn is a normal run.** `send_share_message` builds the whole transcript
(`format_transcript`, capped at `MAX_HISTORY_TURNS`) into one input string and
dispatches it through the same `registry`/`_executor`/`run_in_background`
machinery every other run uses -- so metering, trace persistence, and
cancellation all work unchanged, and no LangGraph checkpointing is involved.
The run is stamped `trigger_context = {"share_link_id", "share_session_id",
"turn_number"}`; `runtime.py`'s `_safe_record_share_reply` keys off
`share_session_id` to append the assistant turn on **every** terminal path
(completed/failed/cancelled/crashed), so `_has_pending_turn` can never wedge
a visitor's chat shut. `record_share_reply` is idempotent per turn.

Transcript content is untrusted: each turn is wrapped in `<user>`/
`<assistant>` tags with `<`/`>` escaped inside, so a visitor can't inject a
fabricated prior assistant turn. The visitor WS is redacted to `type` only
(plus `run_completed.data`, the reply itself) -- agent names, intermediate
output, tool summaries and `usage` never leave the org, same spirit as the
`_PM_TRACE_REDACTED` boundary above.

Rate limiting is `ShareLink.daily_cap` applied **twice**: as the per-session
ceiling (`db/share_sessions.py::try_consume_turn`) and as the link-wide
aggregate ceiling (`db/share_links.py::try_consume_link_turn`), both the same
atomic reset-then-conditional-UPDATE CAS. The aggregate one is load-bearing:
a client that never stores the cookie gets a brand-new, free session on every
request, so the per-session cap alone caps nobody.

## Sync-to-async streaming bridge

`Pipeline.stream()` / `compiled.stream()` are blocking generators. The
FastAPI backend runs them in a `ThreadPoolExecutor` and hands events back to
the event loop via `loop.call_soon_threadsafe(queue.put_nowait, ...)`.
Each subscriber's `asyncio.Queue` is paired with the event loop captured at
`registry.subscribe()` time -- i.e. the WebSocket handler's own loop, which
stays alive for as long as that connection is open -- rather than the loop
of the `POST /api/runs` request that started the run, which is gone by the
time the background thread finishes. An earlier version captured the
request's loop at run-creation time instead; under `TestClient`'s
per-request ephemeral loops that loop was already torn down by the time the
worker thread's callback ran, so `publish()` silently never happened and
the WebSocket handler's `queue.get()` blocked forever. (A `queue.SimpleQueue`
+ `asyncio.to_thread(queue.get)` variant was tried next and rejected: a
blocking `to_thread` call isn't cancellable, so it hung the same way when a
client disconnected before the run finished.)

`RunRegistry` bounds its own growth (`_MAX_RETAINED_RUNS = 1000`, hardcoded
in `registry.py`): every `create()` call evicts the oldest terminal
(non-`running`), subscriber-free runs until back within the bound. Added
because the autonomous email trigger creates runs unattended and
indefinitely, unlike the previous purely human-click-triggered regime this
registry was originally sized for. A `running` run or one with an active
WebSocket subscriber is never evicted. The autonomous-trigger activity list
(`GET /api/org/email-trigger/activity`) is unaffected -- it reads the
persisted `runs` table, not the registry; only the monitoring dashboard's
`GET /api/runs/{id}` and its stream WebSocket can miss a very old, evicted
run. `GET /api/runs/{id}`'s existing `run is None` check already covers
this. The stream WebSocket needed one new check: eviction can land in the
`await websocket.accept()` yield between `stream_run`'s initial
`registry.get()` existence check and its later `registry.subscribe()` call,
so `subscribe()` now returns `None` (instead of raising `KeyError`) for a
run that's gone missing in that window, and `stream_run` closes 4404 on that
`None` the same as any other unknown-run case. Spec:
`docs/superpowers/specs/2026-07-22-run-registry-bounded-eviction-design.md`.

## Granular trace events, cancellation, and run history (Activity page)

The monitoring dashboard's live/historical trace is more than
`run_started -> agent_completed -> run_completed`: `adapters/langgraph_adapter.py`
buffers per-node events (`agent_started`, `tool_started`/`tool_completed`,
`agent_progress`, and, for HIERARCHICAL delegation,
`delegation_started`/`subagent_started`/`subagent_completed`/`delegation_completed`)
into a per-node list and flushes it immediately before that node's
`agent_completed`. `tool_completed`'s `data` carries a truncated,
business-safe `summary` -- never raw tool args or exception text (see
`src/bestteam/core/trace.py`'s `TraceEvent` docstring for the full shape of
each type). `runtime.py::run_in_background` persists every event as a
`TraceEventRecord` in `seq` order (see `ui/backend/db/CLAUDE.md`) and
publishes a synthesized `run_queued` bookend to the live registry the same
way every other event is (not just to the DB), so a live WS subscriber's
replay log and the persisted historical trace start at the same event.

**Cooperative cancellation** (`POST /api/runs/{id}/cancel`,
`registry.request_cancel`/`cancel_requested` backed by a per-run
`threading.Event`) is checked in `run_in_background` between yielded
events -- never a forceful thread kill, since a node already executing
can't be safely interrupted mid-`pipeline.stream()`. The check is skipped
for a node's own buffered event types (listed above): those describe paid
work that's already happened by the time any of them is yielded, so
stopping between them and their `agent_completed` would silently drop that
node's usage from `usage_records`. Every other event type (notably
`run_started`, reached before any node has started) is a safe checkpoint --
skipping it too would let a cancellation already known before the first
agent starts (e.g. requested during compile/memory-recall) run one whole
avoidable extra paid agent turn before stopping. `stream_iter.close()` is
safe to call there because `GeneratorExit` is a `BaseException`, not caught
by the existing `except BestTeamError`/`except Exception` handlers.

**Frontend**: the monitoring page (`Run a team`) shows a running timer,
WebSocket connection status, a "waiting for the agent/model" hint before the
first real progress event, a stale-run banner past 20s with no new event,
and a Stop button (gated on the new run's id having actually arrived --
`runIdRef` is cleared and re-armed per run so an early click can't silently
no-op or target the previous run). The **Activity** page's Runs tab lists
history via `GET /api/runs` and polls it every 5s while any listed row is
still `running` (stopping once none are; an effect-local `ignore` flag
guards against a stale poll response from before a filter change
overwriting the current, correctly-filtered rows) -- clicking a run opens
its detail (`RunDetail.jsx`): a `running` run streams live over the same
WebSocket the monitor page uses, anything else fetches
`GET /api/runs/{id}/trace` once (no live/historical merge, by design).

### Diagnostic re-runs (`POST /api/runs/{id}/diagnose`, admin-only)

The answer to "which step went wrong?" for a poor run. A normal trace
deliberately omits the system prompt, the per-agent input, the intermediate
model turns, tool-call arguments and the retrieved passages (P0-5 keeps an
org's documents out of `trace_events`). Rather than recording those on every
run, an admin **re-runs** the original input against the team as *currently
deployed* with `Pipeline.stream(diagnostic=True)` -- the SDK's
`_TeamState.diagnostic` flag, same lifecycle as `memory_preamble`, so the
cached compiled graph needs no recompile -- and the verbose events land in the
ordinary trace of the **new** run: `agent_prompt`, one `model_turn` per model
call, `args` on `tool_started`, `result` on `tool_completed` (a KB tool's
`result` is the excerpts the model read). Shapes and the 20,000-char cap are
documented on `core/trace.py`; the email tools stay redacted on every path.
`runtime.run_in_background(..., diagnostic=)` only forwards the flag.

Rules, each with a reason: **admin-only** (`get_current_admin`) because the
payload is raw prompts and documents. **Always a new `runs` row**
(`diagnostic_of_run_id`, migration `q4r5s6t7u8v9`) -- history is immutable,
same as `retry_of_run_id`. **Refused with 400 for a run carrying
`trigger_context`**: an autonomous email run would reach the org's live
mailbox with unscoped `email_*` tools, and a shared-chat turn would append a
reply to the visitor's session (`_safe_record_share_reply` keys off that
context). **Refused for a diagnostic run itself** (diagnose the original) and
**409 for a purged run** (no input left). **No `user_id`** is passed, so
per-user memory is neither recalled nor written -- the admin must not act as
the customer; the banner says so. **Built from the current version** via
`_resolve_pipeline_and_version` (cached), and `version_changed` -- on the
POST response and derived again on every `GET /api/runs` row of a diagnostic
run (original's pinned version vs the re-run's, NULL for an ordinary run) --
tells the UI when the team was redeployed since, so the admin knows the
problem may no longer reproduce and the warning survives a page refresh. **Spend is metered to the
run's org** like any run of that team, **retention applies** like any run of
that org, and the run is **filtered out of a non-admin `GET /api/runs`**
(`diagnostic_of_run_id IS NULL`) so it never appears on the customer's
Activity page -- a list-cleanliness rule, not a security boundary (the org's
own run read routes still serve it by id). Not done, on purpose for v1:
rebuilding the *pinned* version, relevance scores on KB hits (`_Chunk` has
none, and a fused/reranked order has no single meaningful number), an admin
purge of one diagnostic run, excluding them from `run_analytics.py`. Spec:
`docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md`.

## Backend API (`ui/backend/`)

Beyond the existing monitoring endpoints (`/api/health`, `/api/pipelines`,
`/api/pipelines/{name}/graph`, `/api/runs`, the `/api/runs/{id}/stream`
WebSocket — all in `main.py`), Phase 2 adds two routers:

- **`builder.py`** (`/api/builder/sessions`) — the wizard's session
  state machine, a thin layer over `db/builder_sessions.py` plus
  `core/requirements.py` / `core/specification.py`:
  - `POST /` — start a session (Stage 1, Intent: `intent_text`/`as_is_text`).
  - `GET /{id}` — fetch session state.
  - `POST /{id}/requirements` — Stage 2: pass `requirements` (a confirmed/
    edited `Requirements` dict) to store directly, or `model` (+ optional
    `feedback`) to call `generate_requirements()`.
  - `POST /{id}/specification` — Stage 3: pass `specification` (a
    `Specification` dict, validated via `validate_specification()`) or
    `model` (+ optional `feedback`) to call `generate_specification()`
    against the session's requirements.
  - `POST /{id}/solution` — Stage 4: like `/specification`, but requires
    `feedback` and always records it via `append_feedback()`; with `model`,
    the current Specification + feedback are fed back to the architect.
  - `POST /{id}/test-runs` — Stage 5: validates `specification_json` and
    runs it through the same `RunRegistry`/`Pipeline.stream()`/
    `ThreadPoolExecutor` machinery as `/api/runs` (factored into
    `ui/backend/runtime.py` so both routers can use it without a circular
    import).
  - `POST /{id}/deploy` — Stage 6: validates agent models against the model
    catalog (`deploy_validation.validate_agent_models`, `fake:` exempt; 400
    listing any agent whose model is missing/empty/non-string or not offered —
    the CRUD path builds `Agent(**spec)` directly, so this is the only guard on
    those), then **publishes a new immutable version** of the pipeline
    (`db/pipelines.py::publish_pipeline_version` — append a `pipeline_versions`
    snapshot from `specification.to_raw()`, move the head's `current_version_id`,
    keep `config` as the current mirror; `status=deployed`) and links
    `session.pipeline_id` to that head, both in the session's single commit
    (P1-14). A redeploy versions the same head and two same-named sessions
    converge on one team (P1-01/02/03). Model validation is **deploy-time
    only** — a model later removed from the catalog, or a legacy row promoted by
    the migration, fails at run, not load (see the spec's "Known limitation").
  - All generation endpoints (`model=...`) translate `BestTeamError` (e.g.
    an invalid spec the architect couldn't self-correct) to `400`, and any
    other exception (e.g. a real provider call without an API key) to `502`
    — see `_call_model()`.
- **`crud.py`** (`/api/config/...`) — the "advanced view" (operator-only):
  `GET`/`PUT`/`DELETE` for `knowledge_bases`/`skills` (validated as standalone
  components via `KnowledgeBaseSpec`/`SkillSpec` — field shape only; both are
  resolvable by name from a pipeline, via `load_knowledge_base_tools` and
  `load_skills`) and `pipelines` (a complete `Specification.to_raw()`-shaped
  dict carrying its own `agents:`/`teams:` inline, validated via
  `_build_pipeline()` exactly like the wizard's Specification stage, then
  `deploy_validation.validate_agent_models()` against the model catalog —
  the wizard's `deploy_session` and `crud.py`'s `PUT /pipelines/{name}` both
  400 listing any agent model spec not in `model_catalog` (`fake:` exempt)). An operator save
  via `PUT /pipelines/{name}` writes `status="deployed"` on both insert and
  update — **save is deploy**: there is no separate promote step, mirroring
  the wizard's `deploy_session`, which validates the same way at the same
  point. Like the wizard, it publishes an immutable version each save
  (`publish_pipeline_version`, `pipeline_id=None` → resolve-or-create the head
  by `(org_id, name)`) rather than overwriting `config` in place. Plus two read-only reference routes for the UI: `GET /orgs` (the org
  selector) and `GET /tools` (the built-in `bestteam.tools.REGISTRY`, name +
  docstring).
  **Standalone `agents`/`teams` CRUD was removed**: nothing consumed those
  records (`_build_pipeline` takes only `extra_tools`/`extra_skills`), and both
  tables were empty everywhere. The models remain in `db/models.py`.
  `DELETE /skills/{name}` and `/knowledge_bases/{name}` refuse with `409` if a
  pipeline's **current version** still depends on the item — the guard now
  queries `db/dependencies.py::pipelines_referencing(db, kind=, resource_id=item.id)`
  against typed `pipeline_dependencies` rows (populated at deploy by
  `record_version_dependencies`) instead of scanning deployed pipelines' JSON;
  the check runs before any deletion/`rmtree`, naming the referencing team(s)
  in the error. A skill dependency also pins `resource_version_id`; editing a
  platform/org skill or creating a same-named org override does not alter an
  already-deployed team. Redeploying the team explicitly adopts the skill
  version resolved then. A pipeline's inline KB likewise
  shadows a same-named standalone KB, so the standalone isn't recorded as a
  dependency of that pipeline.
  Both deploy points also reject (`400`) a pipeline whose KB name — inline or
  a referenced standalone KB — shadows a built-in tool:
  `knowledge_bases.kb_name_collisions(db, org_id, raw_spec)` resolves the
  referenced standalone KB names and delegates to the pure
  `deploy_validation.find_kb_tool_collisions(raw_spec, standalone_kb_names,
  builtin_names)` against `set(bestteam.tools.REGISTRY)`; it's name-only (no KB
  is built) so it can run before path validation. Only KB names are checked —
  the per-org email-tool override (which intentionally shadows `REGISTRY`'s
  `email_*` entries by name) is unaffected. Post-review hardening: a KB also
  can't be **created** with a built-in tool name (`_reject_builtin_kb_name` at
  KB PUT + upload), so a colliding KB can never exist to shadow the built-in at
  load; a seeded platform built-in skill (`_BUILTIN_SKILL_NAMES`, e.g.
  `email_triage_reply`) is undeletable; and the KB delete commits the row before
  `rmtree` (logging rmtree failures) so a commit failure can't destroy files
  under a rolled-back record. Raw-name matching is now resolved (P1-04: the
  guard uses typed rows keyed by stable `resource_id`); still deferred: the
  delete/deploy TOCTOU window (serialized via `component_mutation_lock`, not
  DB-enforced), model/built-in-tool dependency rows aren't recorded (no
  consumer yet), and standalone-KB content pinning.
  P1-07/P1-08, data-architecture review; see
  `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.
- **`_get_pipeline()`** (`main.py`) checks for a `PipelineRecord` in the DB
  first, within the caller's org and filtered to `status == "deployed"`
  (cached on `updated_at`), then falls back to `PIPELINES_DIR/<name>.yaml`
  (cached on mtime) — so a pipeline deployed via the wizard or saved via
  `/api/config/pipelines` is immediately runnable through `/api/runs`, and a
  non-`deployed` record is treated as unknown (same 404 as absent, no
  existence oracle). `GET /api/pipelines` applies the same `status ==
  "deployed"` filter to its DB-backed listing. `/api/config/pipelines` (the
  admin CRUD list) is **not** filtered — operators see all configs
  regardless of status. P1-06, data-architecture review; see
  `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.
- **Demo YAML pipelines are opt-in** (`main.py::demo_pipelines_enabled`,
  `BESTTEAM_DEMO_PIPELINES`, **off by default**). The two pipeline sources
  serve different audiences: YAML is the *SDK's* format (`load_pipeline`,
  `bestteam run x.yaml`, unaffected by this flag and by the DB entirely),
  while DB rows are what the wizard creates per-org at runtime. The files in
  `PIPELINES_DIR` are our shipped fixtures — mostly `fake:` models returning
  hardcoded text, plus `*_live` ones that spend real quota and, for
  `email_triage_demo_live`, read the running org's connected mailbox (per-org
  stored credentials, env fallback on single-org) — and they carry no `org_id`,
  so while enabled *every* org user sees and can run them.
  The gate covers **both** the list (`GET /api/pipelines`) and resolution
  (`_get_pipeline`, hence `/api/runs` and `/graph`): hiding them from the
  list alone would leave them runnable by name. Disabled ⇒ the same 404 as an
  unknown pipeline.

## Async knowledge-base document ingestion (`ingestion.py`)

Both KB upload routes (`crud.py`'s admin
`POST /knowledge_bases/{name}/upload` and `org_knowledge_bases.py`'s
self-service `POST /knowledge-bases/{name}/upload`, both via the shared
`knowledge_bases.py::upload_knowledge_base()`) validate synchronously (name,
size limits, `kb_type`, chunk params), write the uploaded files to a fresh
on-disk version directory, upsert the `KnowledgeBaseRecord`, create a
`queued` `IngestionJob` row, and submit `ingestion.run_ingestion_job` to
`ingestion.py`'s own `ThreadPoolExecutor` (4 workers, separate from the
run-execution executor) — then return immediately with `{"name", "job_id",
"status": "queued"}`. The actual parse → chunk → (embed, for `vector`/
`hybrid`) work happens on that worker thread, opening its own `Session` on
the passed-in `engine` (a `Session` isn't safe to share across threads).
Every `KnowledgeDocument`/`KnowledgeChunk` is buffered in plain Python
through the parse loop AND the embedding call, then written in **one short
transaction** at the end — flushing per file would take SQLite's RESERVED
write lock on the first file and hold it through everything after, blocking
every other writer in the process (runs, trace events, usage records, share
messages) for the whole duration of a large upload. The `_executor.submit`
call sits *after* that commit and outside the handler that rmtree's the
staged version directory: the rows are durable by then, so cleaning up the
files on a submit failure would strand a permanently `queued` job pointing
at a deleted directory — the job is resolved `failed` (and the caller gets a
503) instead. Self-service uploads (`org_knowledge_bases.py`) additionally
refuse a confirmed upload while a `queued`/`running` job already exists
for that KB, inside the same per-KB lock as the existence/cap checks: without
it, a member retrying a stalled or slow upload could pile up unbounded work
on the 4-worker executor, each retry having already staged up to
`_MAX_TOTAL_SIZE_BYTES` to disk and queued an embedding call before anything
else would catch it (Codex review finding). The trusted admin upload path
(`crud.py`) has no such per-caller-count limit and doesn't need this guard.

**Incremental ingestion, and adding documents rather than replacing them.**
Every upload replaces a collection wholesale -- which meant a self-service
collection could never hold more than one upload's worth of files (10), and
changing one document in ten cost a re-parse and a re-embed of the other nine.
`content_hash`, computed and stored on every `KnowledgeDocument` since the
schema was written, was never read by anything; it is what fixes both.

`upload_knowledge_base(..., mode=)` takes `"replace"` (the previous and still
default behaviour) or `"add"`. "Add" is implemented at the **staging** layer,
not the retrieval one: `_stage_previous_generation` copies the live
generation's files into the new version directory beside the newly uploaded
ones, skipping any whose name this upload supersedes -- matched
**case-insensitively**, because the filesystem underneath may be: on Windows
and macOS a carried `Policy.txt` and an uploaded `policy.txt` are one path, so
treating them as two names copied the old file over the new upload and left
the collection serving the previous text under the new name. The new job therefore
still owns a complete document set, so the atomic-swap invariant above,
pruning, retention and `resolve_knowledge_base` are all untouched -- there is
no notion anywhere of a collection spanning two jobs. `_MAX_DOCUMENTS_PER_KB`
bounds the merged set (200 admin, 30 self-service); without it "add" is
unbounded growth per collection, and the existing per-org cap counts
collections, not what is in them.

Restaging is cheap because `ingestion._reusable_documents` then carries an
unchanged file's chunks -- **embeddings included** -- forward from the previous
completed job, matched on `(filename, content_hash)`. Only chunks that are
genuinely new reach `embed_documents_in_batches`, and only their tokens are
metered, so a nine-document collection gaining a tenth bills for one document.
`_carryable` gates the whole lookup on the previous job's shape matching this
one's: `kb_type`, `embedding_model`, and the two new
`chunk_size`/`chunk_overlap` columns (migration `r5s6t7u8v9w0`), which exist
for exactly the reason `kb_type`/`embedding_model` already sat on the job row
-- the `KnowledgeBaseRecord`'s `config` has already advanced to the new
upload's spec by the time the worker runs, so only the job can say what its
chunks were actually cut with. A job predating the columns reads back NULL and
is deliberately **not** reusable, so the first upload after an upgrade
re-embeds once and every one after that is incremental. Only a `completed` job
is ever a candidate -- a failed job's rows are a diagnostic record of
something that was never served. `run_ingestion_job` writes all four values
from its own arguments at the top of the run rather than trusting the row.

The self-service route's confirmation gate carries the choice: `mode` is one
form field with three states (`""` unconfirmed -> 409, then `"add"` or
`"replace"`), replacing the old `replace` boolean, because a boolean cannot
express three answers and `replace=true, mode=add` would be a contradiction
the server had to pick a winner for.

**Atomicity model: the `IngestionJob.status` flip is the swap, not a
CURRENT-pointer file.** Unlike the legacy file-based upload path (which
atomically swaps a `CURRENT` pointer file so a concurrent reader never sees
a half-written version), the DB-backed path writes nothing analogous —
retrieval simply resolves a KB's most recent `IngestionJob` with
`status="completed"` (`knowledge_bases.py::resolve_knowledge_base`), and a
`queued`/`running`/`failed` job's rows are invisible to retrieval by
construction (nothing queries them). "Most recent" is by **`id`, not
`completed_at`**: overlapping uploads for the same KB (rapid `replace=true`
retries, or two jobs racing on the executor) can finish out of submission
order, and `id` — assigned inside the serialized `_kb_upload_lock` staging
block — is the only field guaranteed monotonic with submission order; a
`completed_at`-ordered query could let an older, slower upload's job "win"
over a newer one that already completed (Codex review finding).
`_prune_old_ingestion_versions` (below) orders the same way, for the same
reason — it already matched `_prune_failed_ingestion_versions`'s `id`
ordering, just not `resolve_knowledge_base`'s. A KB with `IngestionJob` rows
but **no completed one yet** (still queued/running, or every attempt has
failed) does NOT fall back to the legacy file-based construction either:
`resolve_knowledge_base` only takes that fallback for a KB with **zero**
`IngestionJob` rows ever (a true pre-feature legacy KB). Falling back
whenever there's merely no completed job would scan
`KnowledgeBaseRecord.config`'s `path` — the KB's upload root, which
recursively contains every version subdirectory including the one currently
staging — serving un-vetted, partial, or entirely un-embedded content
instead of treating the KB as not yet servable; it raises `ConfigurationError`
instead (Codex review finding). Which KB subclass that resolved job is
rebuilt as, and which model embeds a query against it, come from the **job
row's** own `kb_type`/`embedding_model` — not the `KnowledgeBaseRecord`'s
`config`, which is already the *next* generation's spec during the whole
ingestion window (and permanently, if the new job fails); `config` still
supplies `top_k`/`rerank_model`/`candidate_k`/`query_expansion_*`, which
apply uniformly to whichever generation is live. A successful job also invalidates the
pipeline cache (`_invalidate_pipeline_cache()`, called at job completion,
not at upload-dispatch time — that's the point the KB's live content
actually changes) and best-effort prunes older completed generations,
keeping the current one plus one grace-window generation, mirroring the
legacy path's "prior version kept only until the new one is durable"
precedent. A parallel step (`_prune_failed_ingestion_versions`, which runs on
the **failed** path too — the only cleanup that does, since a customer
retrying an unparseable upload never produces a completed job) reclaims every
failed job's on-disk version directory except the most recent one's, kept as
a diagnostic copy; failed jobs' *rows* are always kept, as the
customer-visible error record. Cache invalidation and both pruning steps are
isolated in their own `try/except`s so a failure in any of them can never
retroactively mark an already-committed successful ingestion as failed.

**Chunk location metadata and `description`** (P0-3). The parse loop calls
`bestteam.core.knowledge_base._chunk_document` rather than `_chunk_text`, so
each `KnowledgeChunk` row also stores `page` (PDF, chunked per page) and
`heading` (Markdown section); `_build_knowledge_base_from_job` reads both back
into the rebuilt `_Chunk`s, and a retrieval result cites
`[source: handbook.pdf, p.3 § Refunds]`. Both upload routes also accept an
optional `description` (≤500 chars — `Form(...)` on the self-service route,
`Query(...)` on the admin one, capped there so a long one is a 422 naming the
field rather than a 500 from `KnowledgeBaseSpec`'s own validation). It is
stored on the KB's `config`, so `_build_knowledge_base_from_job` takes it from
`config` and not from the job — an edited description takes effect at once
rather than waiting for the next ingestion — and it surfaces in three places:
the agent tool's own docstring, `builder._with_knowledge_base_catalog`'s
listing (`- name (type: X): description`), and `_kb_summary`'s
customer-facing payload.

**Changing a KB's shape, and reporting the shape that serves** (P1-3).
`upload_knowledge_base()`'s `kb_type` is `Optional[str] = None`, meaning
"whatever this collection already is": before the type validation (and
outside the per-KB lock -- the in-lock re-query still decides what is
written), it reads the existing `KnowledgeBaseRecord` and inherits the whole
shape group `type`/`embedding_model`/`rerank_model`/`query_expansion_model`,
falling back to `local_folder` for a name that doesn't exist yet;
`description` is inherited independently on `None`. That is what `crud.py`'s
admin upload route sends -- it has no way to name a shape -- so before this,
an operator replacing a `hybrid` collection's documents silently rebuilt it
as `local_folder` and blanked the customer's description with it. A caller
that passes `kb_type` names the whole group from that call
(`org_knowledge_bases.py` always does, derived from the wizard's toggle), and
`chunk_size`/`chunk_overlap`/`top_k` are never inherited -- both routes
always send them, so there is no `None` to interpret.

The flip side is reporting: `config` is the *next* upload's shape, so
`org_knowledge_bases.py::_live_kb_type(db, record)` answers "what can be
searched today" from the latest **completed** job's own `kb_type`, falling
back to `config` when there is none. `_kb_summary`'s `type` and `servable`
both derive from that same completed job, and the self-service upload's
replace `409` names it in words the wizard's audience can act on ("It
currently uses Enhanced search.", `hybrid` -> Enhanced). The wizard's own
confirmation adds what the collection would *become*
(`DocumentsPage.tsx`), so one dialog carries both halves of the change and
cancelling to flip the toggle is an informed choice -- there is deliberately
no mount-time probe of the KB's shape, because the wizard has no name to
probe with until the customer types a label. `job_status_payload`'s `config`
is unchanged: that one is the configuration intent, by design.

**Per-document partial-failure model.** One bad file (unsupported file type,
parse error, no extractable text, or zero chunks produced) doesn't fail the
whole job — it's recorded as a `failed` `KnowledgeDocument` row with a capped
error message, and the job continues processing the rest. The parse loop
walks **every** staged file, not only the ones whose suffix is in
`_SUPPORTED_SUFFIXES` (P0-6): filtering first meant an unsupported file left
no row at all, so `documents_succeeded + documents_failed` silently
disagreed with `file_count` and nothing ever told the customer their `.png`
was dropped. The suffix check now raises inside the loop's existing
`except Exception`, as does `_has_extractable_text` (shared with
`bestteam.core.knowledge_base`, along with both messages) — that second one
is what stops a **scanned PDF**, which parses to its `[PDF: …]` header line
and nothing else, from becoming a content-free chunk instead of a reported
failure. The job itself only ends `failed` if every document
failed (zero chunks total) or, for `vector`/`hybrid`, if the embedding call
itself raises — in which case the job's already-flushed-but-uncommitted
document/chunk objects are discarded before anything is written (a
vector/hybrid KB with no embeddings can't serve queries, so a total
embedding failure must leave no partial rows behind). That embedding call is
`bestteam.core.embeddings.embed_documents_in_batches` (P1-2): 100 chunks per
provider call, each batch given up to three attempts (two retries, a 1s then
2s backoff),
and **only the failing batch is retried** — a provider hiccup partway through a
large upload now costs one batch rather than every chunk embedded before it.
Only an exception that survives all three attempts, or a batch that comes back
with the wrong number of vectors (rejected immediately, no retry), fails the
job. Metering is unchanged: the `kb:ingest` token estimate is computed once
from the chunk texts, so a retried batch is never billed twice. A document's error
text is scrubbed of the server's absolute upload path before it's stored
(`ingestion._scrubbed`) — third-party parsers embed the path they were
handed, and `job_status_payload` returns that text verbatim to a
self-service org member. Any unexpected exception on the worker thread is
caught and recorded on the job row as a generic failure — the job never
raises uncaught, mirroring `runtime.py::run_in_background`'s shape.

**Two read-only endpoints** (`ingestion.job_status_payload`, shared by
both): `GET /api/config/knowledge_bases/{name}/ingestion-jobs/{job_id}`
(admin, `?org=`) and `GET /api/org/knowledge-bases/{name}/ingestion-jobs/{job_id}`
(org self-service, org from the bearer token) — both org-scoped and 404 on
an unknown KB or job. The response reports `status`, `file_count`,
`documents_succeeded`/`documents_failed`, the live `chunk_count`, up to 10
`{filename, error}` rows for failed documents, and — only once
`status == "completed"` — the KB's `config`. A whole-job failure (the embed
call raised, or the job died on the worker thread before any document was
processed) sets `job.error` but writes no per-document `failed` rows, so
`errors` falls back to a single `{filename: None, error: job.error}` entry
in that case — otherwise a `failed` job with an empty `documents_failed`
count returned `errors: []` and neither frontend had anything to show the
customer beyond a bare "failed" status (Codex review finding; `job.error` is
already scrubbed/capped at write time, so it's safe to return as-is). See
`docs/KNOWLEDGE_BASES.md` for the response shape and
`docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`
for the full design.

Deleting a KB cascades to delete its `IngestionJob`/`KnowledgeDocument`/
`KnowledgeChunk` rows (`ingestion.delete_kb_ingestion_data` — participates in
the caller's existing delete+commit+rmtree transaction rather than
committing separately). See `ui/backend/db/CLAUDE.md` for the three tables'
schema.

**An org manages its own knowledge bases** (`org_knowledge_bases.py`:
`GET /api/org/knowledge-bases`, `GET`/`DELETE /api/org/knowledge-bases/{name}`,
`POST /api/org/knowledge-bases/{name}/search`,
all `get_current_org`-scoped). `_kb_summary` reports `used_by`
(`pipelines_referencing`), `servable` and `latest_job` -- the newest attempt of
*any* status, `config` stripped, since that field carries the server's absolute
upload path and this list is customer-facing. The DELETE is the same
`knowledge_bases.delete_knowledge_base` the admin route calls, so both 409s
(deployed dependency, in-flight ingestion) hold here too. Two consequences
elsewhere: `resolve_knowledge_base` now reads the *latest* job rather than only
asking whether any exists, and reports a `failed` one's own error instead of
telling the customer to wait for something that will never finish; and
`builder._all_knowledge_base_tools` skips a KB that won't resolve (logged
warning) with `_with_knowledge_base_catalog(..., names=)` listing only the ones
that built -- it runs over every KB in the org, so one unparseable upload used
to 4xx spec generation for everybody. `load_knowledge_base_tools` still fails
closed, because there the KB is one an agent actually references.

**A customer can try a search against their own collection** (P1-4):
`POST /api/org/knowledge-bases/{name}/search`, body
`{"query": <1..500 chars>, "top_k": <1..10>}`, returning
`{"query", "hit_count", "results": [{"citation", "source", "page", "heading",
"text"}]}` with each `text` capped at 1,500 characters -- enough to judge the
retrieval by, not a document reader. It resolves the knowledge base through
`resolve_knowledge_base(db, record)` with **no `source`**, which is what
turns the legacy file-based fallback off for this surface: rebuilding a
disk-backed collection would re-parse every file, and re-embed a `vector` one
unmetered, on every click. That refusal, "still processing" and "the last
upload failed" are all `KnowledgeBaseNotReady` (a `ConfigurationError`
subclass raised at exactly those three sites in `knowledge_bases.py`), and
the route maps **only** that subclass to `409`. Any other
`ConfigurationError` out of `_build_knowledge_base_from_job` -- a missing
`rank-bm25` extra, a bad `rerank_model` -- is an operator's deployment
problem the customer cannot act on, so it falls through to the app's generic
logged `500` rather than masquerading as a conflict to wait out. A provider
failure inside `kb.search` is a `502`; another org's name is the usual `404`.
The search runs inside `tool_call_context()` and whatever the knowledge base
reports there is metered (see the usage section below). No cache and no rate
limit, deliberately -- see `docs/KNOWLEDGE_BASES.md`, "Trying a search".

**Deleting a knowledge base is refused (`409`) while an upload is still
processing**, and the whole sequence lives in
`knowledge_bases.py::delete_knowledge_base`, not in `crud.py` — it needs this
module's per-KB `_kb_upload_lock`, and it takes `component_mutation_lock`
itself, which is **not reentrant**, so `crud.delete_item`'s `knowledge_bases`
branch has to return *before* entering its own `with component_mutation_lock`
block. The in-flight check (a `queued`/`running` `IngestionJob` for this KB)
runs inside the per-KB lock, alongside the delete, commit and `rmtree` it
guards. Refusing, rather than cancelling, is what makes "a KB being deleted
has no worker" true: uploads and deletes both serialize on that lock and only
an upload creates a job, whereas a cancel flag would still leave the worker
holding an open file handle — `rmtree` then fails with `WinError 32` and
silently leaks the directory, and the worker's final commit writes
Document/Chunk rows against a `kb_id` that no longer exists (FK enforcement is
off, so nothing catches them). `ingestion.fail_interrupted_jobs(engine)`,
called from `main.py::_lifespan`, is the other half: the executor is
per-process, so a job still `queued`/`running` at startup belongs to a dead
process and is marked `failed` — without it, one killed process would make
that KB permanently undeletable.

## Auth, model catalog, and usage metering (Phase 3)

- **`ui/backend/auth.py`** — stdlib-only password hashing (PBKDF2-HMAC-SHA256,
  260,000 iterations, `pbkdf2_sha256$<iterations>$<salt>$<hash>`) and
  JWT-shaped bearer tokens (`create_access_token`/`decode_access_token`,
  HS256-equivalent via `hmac`, `sub`+`exp` claims, `AuthError` on
  malformed/tampered/expired tokens). No `passlib`/`PyJWT`/`bcrypt` dependency.
  `SECRET_KEY` and `ACCESS_TOKEN_EXPIRE_MINUTES` come from
  `BESTTEAM_SECRET_KEY` / `BESTTEAM_ACCESS_TOKEN_EXPIRE_MINUTES` env vars
  (defaults: a dev-only secret, 1440 minutes).
- **`ui/backend/auth_api.py`** (`/api/auth`) — `POST /login` (returns
  `{access_token, token_type}`), `GET /me` (requires `Authorization: Bearer
  <token>`; returns `{username, is_admin, org}`). **There is no public
  registration endpoint** — orgs, users, and admins are all provisioned via
  the operator CLI (`python -m ui.backend.admin create-org / create-user /
  promote`; tests use `tests/helpers.py::create_user_and_login`). Exports
  three dependencies: `get_current_user` (bearer → `User` row),
  `get_current_admin` (403s non-admins; router-level guards the admin-only
  `/api/config/*` and `/api/memory/*`), and `get_current_org` (the user's
  `Organization`; **403s platform operators** — org-NULL users — on org-user
  surfaces: pipelines list/graph, `POST /api/runs`, the builder router).
  Admin is granted only via the operator CLI, never from a username match,
  and never read at import (a DB predating the migrations still boots; the
  module-level seeding likewise warns-and-skips on a pre-migration schema).
  `/api/health` and `/api/auth/*` stay public; the run stream WebSocket
  authenticates with a single-use `?ticket=` (see the runs section).
  **`POST /login` is throttled** (`login_rate_limit.py`, beta gate G2): a
  process-wide in-memory sliding window over *failed* attempts, 5 per
  username (lower-cased) and 20 per client address in 15 minutes; a
  throttled attempt gets 429 + `Retry-After` **before** PBKDF2 runs (the CPU
  half of the defence -- 0.76 s per hash), with the same message whether the
  username exists or not. The check *reserves*: `reserve()` counts the
  attempt as a failure in the same locked step that admits it (a
  check-then-record pair would let a concurrent burst -- the route runs in
  the thread pool -- hash once per thread), and only `record_success()`
  takes it back: it clears the username's failures and releases the one
  slot the attempt took from the address, not the address's other failures.
  Username keys are a SHA-256 digest (the request puts no bound on the
  name's length and a key lives a whole window), and the expired-key sweep
  runs when the dict has doubled since the last one (amortised O(1) per
  attempt -- an unknown username never reaches PBKDF2, so keys can arrive
  as fast as the per-address budget allows).
  Behind a reverse proxy the address is the proxy's unless uvicorn
  is told to trust it (`docs/deployment.md`), which is why the username key
  exists. `tests/conftest.py` swaps in a fresh limiter per test.
- **Model catalog** (`ui/backend/db/model_catalog.py` + `/api/config/model-catalog`
  CRUD in `crud.py`) — `to_prompt_text(entries)` renders the catalog for the
  Solution Architect's prompt. **`tier="embedding"` marks an entry as an
  embedding model, not a chat model**: it lives in the same table so
  `record_usage` can price a knowledge base's embedding spend from one
  catalog, and `list_chat_entries(db)` (not `list_entries`) is what every
  chat-model surface uses -- the public listing below,
  `builder.py::_with_model_catalog`, and
  `org_knowledge_bases.py::_default_chat_model` -- so an embedding model can
  never be handed to an agent. Admin CRUD still lists everything (somebody
  maintains those prices), and no embedding entry is seeded into
  `DEFAULT_MODEL_CATALOG`. `builder.py::_with_model_catalog(db, text)`
  appends this to the requirements text before `generate_specification()` (in
  both `submit_specification` and `submit_solution_feedback`'s `model=`
  paths), so the architect picks `AgentSpec.model` specs by role complexity
  and pricing rather than guessing provider names. CRUD is admin-only, but the
  **list is also exposed read-only at `/api/model-catalog`** (`crud.public_router`,
  any authenticated user): the Team Builder wizard runs as an org member and
  needs the catalog to pick a real model — without it the frontend falls back
  to a `fake:` model and generation fails (`with_structured_output`). The two
  generation steps translate that fake-model failure into a clear
  `ConfigurationError` ("needs a real AI model") rather than the raw
  `NotImplementedError`.
- **Skills library** (`ui/backend/skills.py` + `/api/config/skills` CRUD in `crud.py`)
  — every PUT appends an immutable `SkillVersion`, moves
  `SkillRecord.current_version_id`, and exposes the current `version` plus
  `GET /skills/{name}/versions` history. `load_skills(db)` returns current
  heads for drafts/deploy validation/YAML; `load_skills(...,
  pipeline_version_id=)` returns only the exact skill versions pinned by that
  deployed pipeline. Both return `Dict[str, SkillSpec]` for `_build_pipeline()`.
  `builder.py::_with_skill_catalog(db, text)` appends "Available skills..." list
  (name/description/tools) to the requirements text before `generate_specification()`,
  so the Solution Architect knows what skills exist for assignment to agents.
- **Usage metering** — `core/trace.py::TraceEvent.usage` is a
  `List[Dict[str, Any]]` of `{"model", "input_tokens", "output_tokens"}`
  entries, populated by `adapters/langgraph_adapter.py::_record_usage()`
  whenever a model response has `usage_metadata` (real provider models;
  `fake:` models leave it empty). For `HIERARCHICAL` teams, the manager and
  all delegated subordinates share one `usage_sink` per turn, so the total
  surfaces on the manager's single `agent_completed` event.
  `ui/backend/runtime.py::run_in_background(run_id, pipeline, input,
  engine=None, user_id=None)` — if `engine` is given (callers pass
  `db.get_bind()` so tests using an overridden in-memory DB still work), opens
  its own `Session` and calls `db/usage.py::record_usage()` for each `usage`
  entry on every `agent_completed` event, computing `cost_estimate` from
  `model_catalog` when the model spec matches a catalog entry (`None` otherwise).
  It also meters the per-user memory extraction call: a `memory_recorded` (or,
  when every write failed, `memory_failed` with `data="record"`) event (SP-3)
  carries the extraction LLM's `usage`, recorded as a `usage_records` row with
  `agent="memory:extraction"` (that call bypasses the adapter's usage path, so
  it arrives on the memory event instead). The SDK attaches the usage to
  exactly one event, so it's billed once even on total write failure. These
  memory events arrive AFTER `run_completed` (recording runs post-terminal so
  a hung extraction can't wedge the run), but `run_in_background` drains the
  whole event stream so they're still metered/recorded; `registry.publish`
  tolerates a run evicted in that window. Symmetrically, the per-user memory
  **query-expansion** call is metered the same way on the recall side: a
  `memory_recalled` (or, when the search that followed a successful expansion
  failed, `memory_failed` with `data="recall"`) event carries the expansion
  LLM's `usage`, recorded as a `usage_records` row tagged
  `agent="memory:query_expansion"` — `run_in_background`'s usage block picks
  the agent label per-event (`event.type == "memory_recalled"`, or
  `memory_failed` with `event.data == "recall"`) so a recall-side call is
  never mis-attributed as extraction, and vice versa. These events arrive
  BEFORE `run_started` (recall happens before the agents), so unlike the
  extraction/record events they're metered promptly, not post-terminal. All
  usage persistence goes through `_safe_record_usage`, which isolates a
  `usage_records` write failure (logs + rolls back) so metering can never
  flip a successful run to `run_failed`.
- **Knowledge-base spend** (P0-4, extended by P1-4) reaches the same ledger by
  three routes, and `runtime.py` needed no change for any of them:
  - *Query time* (the query embedding for `vector`/`hybrid`, and the
    query-expansion LLM call for all three types) rides the **existing**
    `agent_completed.usage` list. A KB tool reports its spend through
    `core/tool_context.py::add_usage`, and the adapter's tool loop drains
    `tool_ctx.usage` into the node's `usage_sink` -- on the failure path too,
    since the paid call already happened. So these are ordinary run rows,
    attributed to the agent that searched, with `model` set to the embedding
    or expansion spec. No new event field, no new metering branch.
  - *Ingestion* (`ui/backend/ingestion.py::_safe_record_ingestion_usage`)
    writes **one** row per completed job -- not per chunk -- with
    `agent="kb:ingest"`, `run_id=None` and `ingestion_job_id` set. It runs
    after the job's own commit and is best-effort in its own `try/except`,
    like the cache invalidation and pruning beside it: a metering failure
    must never turn a completed ingestion into a failed one.
  - *A test search* (`org_knowledge_bases.py::_safe_record_search_usage`)
    drains the same `tool_call_context()` the run-time path uses, but writes
    `agent="kb:search"` rows with **both** `run_id` and `ingestion_job_id`
    NULL: a customer clicking "Try a search" spends against no run and no
    upload. That makes `usage_records` a three-source ledger (run / ingestion
    job / ad-hoc `kb:search`), which is the wording to keep consistent in
    `db/models.py`, `db/usage.py`, `db/CLAUDE.md` and migration
    `n1o2p3q4r5s6`. The org's monthly `SUM(cost_estimate) WHERE org_id`
    counts these naturally; every run-keyed consumer drops them the same way
    it drops ingestion rows. Best-effort in its own `try/except`, and applied
    on the search's failure path too -- a query expansion is paid for before
    the embedding call that raised.

  Two things to keep in mind. **Embedding token counts are estimated**
  (`core/embeddings.py::estimate_embedding_tokens`, ±30%) because no provider
  reports embedding usage through LangChain's `Embeddings` interface --
  expansion tokens are the model's own reported `usage_metadata`, not an
  estimate. And **nothing billable means nothing recorded**:
  `core/embeddings.py::billable_spec()` is the one definition of billable (a
  non-`fake:` string spec), shared by the SDK and `ingestion.py`. Reranking
  is a local cross-encoder, $0, and is deliberately never recorded.

## Per-user memory

`ui/backend/runtime.py::_make_memory()` builds a `bestteam.MemoryManager`
**on the worker thread** (so the `SqliteBM25Memory` connection is thread-local)
from env: `BESTTEAM_MEMORY_DB` (unset/empty → memory disabled, runs unchanged;
set → the SQLite path) and `BESTTEAM_MEMORY_MODEL` (optional → enables one
extraction LLM call per run for semantic/procedural records). `run_in_background`
passes it plus `user_id` into `pipeline.stream(...)`; `main.py::create_run`
threads the JWT `user.username` through as `user_id` (the wizard's
`builder.py` test-runs omit it, so sandbox runs never touch memory). See
`src/bestteam/core/CLAUDE.md` for the SDK-side design.

Two more optional env vars enable opt-in hybrid recall: `BESTTEAM_MEMORY_EMBEDDING_MODEL`
(same spec convention as the vector knowledge base — unset → plain BM25
recall, byte-for-byte unchanged) and `BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS`
(default 14, only meaningful when an embedding model is set). Both are read
identically by `_make_memory` and `memory_api.py::get_memory_store` (the admin
search dependency below), so admin search reflects the same ranking behavior
a live run gets. See `src/bestteam/core/CLAUDE.md`'s "Known limitations
(per-user memory)" for the hybrid-recall design.

Two further optional env vars enable opt-in query expansion:
`BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL` (same `_resolve_model` spec
convention as `BESTTEAM_MEMORY_MODEL`/extraction — unset → recall is
byte-for-byte unchanged) and `BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT` (default
3, how many alternative phrasings to request). **Unlike the two hybrid-recall
vars above, these are NOT read by `memory_api.py::get_memory_store`** — admin
search stays literal-only by design (see `src/bestteam/core/CLAUDE.md`).
Also unlike `BESTTEAM_MEMORY_EMBEDDING_MODEL` (eagerly resolved at store
construction, so a bad spec disables memory entirely), a bad
`BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL` never disables memory — it's resolved
lazily per-call and degrades to searching the literal query alone, same
failure shape as a bad `BESTTEAM_MEMORY_MODEL`. See `src/bestteam/core/CLAUDE.md`'s
"Known limitations (per-user memory)" for the full query-expansion design.

Two more optional env vars enable opt-in reranking of the fused recall
candidates: `BESTTEAM_MEMORY_RERANK_MODEL` (same spec-string convention as
the knowledge bases' `rerank_model` — `"fake:"` for $0 tests,
`"cross-encoder:<model-name>"` for a real local
`sentence_transformers.CrossEncoder`; unset → `recall()` is byte-for-byte
unchanged) and `BESTTEAM_MEMORY_RERANK_CANDIDATE_K` (default `top_k * 4`,
clamped — how many fused candidates reach the reranker). Like
`BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL`, `BESTTEAM_MEMORY_RERANK_MODEL` is
resolved lazily per-run and a bad spec never disables memory — it just
disables rerank for that run. See `src/bestteam/core/CLAUDE.md`'s "Known
limitations (per-user memory)" for the full reranking design (weighted RRF
re-fusion, literal-query-only scoring, deferred v1 items).

`create_run` also resolves and passes `pipeline_id` (`PipelineRecord.id`, the
deployed team's stable head) alongside `pipeline_version_id` — see
`_resolve_pipeline_and_version`. Unlike `pipeline_version_id` (pure
provenance metadata), `pipeline_id` scopes recall/writes: episodic/procedural
memory is isolated per pipeline, semantic stays org-wide. See
`src/bestteam/core/CLAUDE.md`'s "Known limitations (per-user memory)" for the
full design.

**Admin memory management** (`ui/backend/memory_api.py`, `/api/memory`,
`get_current_admin`-guarded): `GET /users` (users with per-type record counts),
`GET /users/{user_id}/records?query=&type=&limit=` (browse via `all(limit=)`,
search via `search(top_k=limit, max_candidates=_MAX_SEARCH_SCAN)` so both the
response and the scan work are bounded over a large store),
`DELETE /records/{memory_id}`, `DELETE /users/{user_id}` (clear a
user — `store.delete_user`), and `DELETE /orgs/{org_id}` (org-level compliance
erasure — `store.delete_org` for org-scoped rows plus `store.delete_legacy_for_users`
for the current members' legacy NULL-org rows, members resolved from the main DB
`users` table, SP-2). The legacy purge is deliberately NULL-org-scoped, not an
unscoped `delete_user`, which would also destroy the same username's rows under
other orgs (moved user / reused username). A `get_memory_store` dependency opens a
per-request `SqliteBM25Memory` from `BESTTEAM_MEMORY_DB` on the threadpool thread
and closes it after (`store.close()`); when memory is disabled the read endpoints
return `enabled:false` and mutations return 409. `GET /users/{id}/records`
accepts `?org=` to scope to one `(org_id, user_id)` identity (a moved user has
rows under several orgs; the admin UI keys/selects by both): **omitted = across
all orgs, an int = that org, and the literal `?org=legacy` = only pre-SP-2
NULL-org rows** (`_parse_org_read`; 422 otherwise). The `legacy` sentinel exists
because a legacy identity's `org_id` is null — without it, selecting the
"legacy (no org)" row would omit `org` and read the username across *every* org
(cross-tenant over-fetch); the store's `all`/`search` map the sentinel to
`org_id IS NULL` via `core/memory.py::LEGACY_ORG`. The SDK store
primitives `user_ids()`/`delete_user()`/`delete_org()`/`delete_org_and_legacy()`/
`assign_legacy_to_org()`/`close()` back these endpoints; org erasure resolves the
member set then deletes scoped + attributable-legacy rows in one store transaction
(`delete_org_and_legacy`). The operator `delete-user` CLI (`admin.py`) validates
the account first, then purges the deleted principal's memory (`store.delete_user`)
before releasing the username, failing closed on error (SP-2 review r2 #2 / r3
#1,#4); it warns loudly when `BESTTEAM_MEMORY_DB` is unset/absent for the
invocation rather than implying a clean purge, and never creates a missing store.
`move-user` first binds the user's legacy NULL-org rows to their source org
(`assign_legacy_to_org`) so pre-SP-2 data stays attributable (r3 #3). Memory is
org-scoped (SP-2): `user_summaries()` and each record carry `org_id`, and the
admin surface reads across orgs (`org_id=None`) while a run only ever sees its own
org — see `src/bestteam/core/CLAUDE.md`.
Memory is also **principal-scoped** (deletion-lifecycle): each record carries the
run's immutable `users.principal_id`, so recall/writes touch only that account
instance and a recreated same-username account can't recall the deleted account's
rows (finding 1). Account deletion (`delete-user` CLI and `DELETE /api/admin/users/{u}`)
now **retires the principal** (`store.retire_principal`) alongside the
`store.delete_user` purge, so an in-flight run's late write is dropped by the store
fence (finding 2); purge/retire run before the username is released, still
fail-closed. `account_memory.purge_user_memory(username, principal_id=)` does both.
Pre-stamping (NULL-principal) rows aren't recalled by a stamped run; the opt-in
`python -m ui.backend.admin backfill-memory-principals` binds each current user's
NULL-principal rows to their principal (`store.assign_null_principal`). See
`src/bestteam/core/CLAUDE.md` and
`docs/superpowers/specs/2026-07-30-memory-principal-lifecycle-design.md`.

## Admin org/user management API (`admin_api.py`)

`/api/admin`, `get_current_admin`-guarded — everyday provisioning for platform
admins (the web counterpart of the `ui.backend.admin` CLI). Endpoints: `GET/POST
/orgs` (list with each org's member; create — `create_org` + the `ensure_email_
single_org` CR-031 guard), `PATCH /orgs/{name}` (deactivate/reactivate via
`db/orgs.py::set_org_active`), `GET/POST /users` (list all logins incl.
read-only platform accounts; create an **org member**), `POST
/users/{username}/password` (reset), `POST /users/{username}/move` (org→org),
`DELETE /users/{username}`. **No route can escalate privilege or mutate a
platform account:** `promote`/`demote` and the whole operator/admin lifecycle
stay CLI-only, and every user route refuses (`409`) a non-org-member target.
`delete`/`move` run the same fail-closed per-user-memory work as the CLI —
delete: purge-and-retire-principal-before-release; move: reconcile-legacy-to-source
— through the shared `account_memory.py` helpers (`purge_user_memory(username,
principal_id=)` / `reconcile_legacy_org`, factored out of `admin.py`, which still
calls them under its old `_`-prefixed names). Retiring the deleted account's
`principal_id` engages the memory store's write-fence so an in-flight run's late
write is dropped (deletion-lifecycle finding 2).

**Org deactivation** (`organizations.active`, migration `f3a4b5c6d7e8`) is a
reversible full suspend, enforced in three ways (external-review hardening,
r-ext): (1) `login` refuses a deactivated org's member a token, and
**`get_current_user` rejects (`403`) an inactive-org member on *every*
authenticated route** — centralized there rather than only in `get_current_org`,
so `/me`, `/model-catalog`, run reads, the ws-ticket mint, and the transcription
path are all covered; (2) `db/email_triggers.py::list_enabled_triggers` filters
inactive orgs so the autonomous trigger pauses **and** the final dispatch CAS
(`email_trigger.py::_start_triggered_run`) requires an active org in its atomic
predicate, closing the deactivate-after-enumeration race (r-ext2 #2); (3) the
run-stream WebSocket re-authorizes before **every** event
(`main.py::_stream_access`) so a mid-stream deactivate/move/delete/
password-reset/username-reuse stops delivery immediately (no cross-tenant leak
on a move). Admin cross-org surfaces (`/api/config?org=`, `/api/memory`) are
**not** blocked, so an admin can still manage/reactivate a suspended org. CLI
parity: `admin.py` gains `activate-org`/`deactivate-org`.

**Session revocation via security stamp** (r-ext / r-ext2 #1/#3):
`users.security_stamp` (migration `a7b8c9d0e1f2`) is a random per-account
credential generation embedded in every access token (`sec` claim) and WS ticket
and verified against the current row on use (`get_current_user`, `_stream_access`
per event). A password reset regenerates it — revoking all existing
tokens/tickets — and a deleted-then-recreated username gets a fresh stamp, so the
old account's credentials can't reach the new same-named account (an immutable
random value, not a timestamp, so there's no ordering race). **Identifier
validation:** `db/validators.py::clean_identifier` (used by the `admin_api`
request models and by `create_user`/`create_org`) trims and enforces a URL-safe
grammar on org/user names (`[A-Za-z0-9._-]`, ≤64, and not the `.`/`..`
dot-segments proxies collapse), server-side, so a direct API call can't create an
unmanageable or path-unaddressable record.
Spec: `docs/superpowers/specs/2026-07-27-admin-org-user-management-design.md`.

## Logging and error reporting (beta gate G4)

`main.py` calls `logging.basicConfig` at import (level `BESTTEAM_LOG_LEVEL`,
default INFO, `timestamp LEVEL logger: message`) -- a no-op when the root
logger already has handlers, so pytest's capture and an operator's own
`dictConfig` win. `error_reporting.py` is the one off-box channel: opt-in by
`BESTTEAM_SENTRY_DSN`, initialised with `default_integrations=False`,
`send_default_pii=False`, `max_request_body_size="never"`,
`include_local_variables=False`, no tracing, so the SDK adds **no** capture
points of its own. Exactly two call sites: `main.unhandled_exception_handler`
(`report_exception`, tags method + the matched route *template* --
`/api/share/{token}/messages`, never the concrete path, whose parameter can
be a capability token) and `runtime.py` -- the streaming
loop's `run_failed` branch (`report_message("Run failed: <pipeline name>")`, tags
only; the reason is an exception's text and stays in the run's persisted
trace) and the worker-thread catch-all (`report_exception`, tags
run_id/pipeline). A `before_send` hook (`_scrub_event`) drops every exception
*message* from an event -- an output parser echoes the model's text, an HTTP
error carries the URL a tool fetched -- keeping type, stack (no locals) and
tags. Both helpers are no-ops without a DSN or without the SDK and never
raise. Adding a third call site is a deliberate decision, not a convenience:
the rule is ids and names, never content. `sentry-sdk` is in the `ui` extra;
a malformed DSN makes `sentry_sdk.init` raise at import (the backend refuses
to start), which `admin check-env` catches beforehand. `BESTTEAM_LOG_LEVEL`
blank (as `.env.example` ships it) means INFO. Tests:
`tests/test_error_reporting.py` (a fake `sentry_sdk` module in `sys.modules`).

## Known limitation: general-purpose cache

Only local caches exist (`_pipeline_cache` in `ui/backend/main.py`,
`Pipeline._compiled`) — no shared/cross-request cache layer.
