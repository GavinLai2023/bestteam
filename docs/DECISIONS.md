# Technical decision log

This file records **why** significant decisions were made — not **what**
the current behavior is (that's what the `CLAUDE.md` files are for). The
goal is to stop future sessions (human or Claude) from re-litigating
settled questions or "fixing" something that was a deliberate trade-off.

Append new entries at the bottom using this template:

```markdown
## <Short title>

- **Status**: Accepted / Superseded by <link>
- **Context**: what problem or question prompted this decision
- **Decision**: what was decided
- **Consequences**: what this enables, what it rules out or defers
```

---

## Engine: LangGraph (not CrewAI) as the orchestration engine

- **Status**: Accepted
- **Context**: bestteam needs an engine to execute `Agent`/`Team`/`Pipeline`
  under the SEQUENTIAL/PARALLEL/HIERARCHICAL collaboration modes, with
  tool-calling, structured outputs, and streaming trace events for the
  monitoring dashboard. LangGraph and CrewAI were both viable candidates.
- **Decision**: LangGraph was chosen as the engine from the project's first
  commit.
- **Reasons**:
  - LangGraph's graph/state-machine model maps directly onto the
    `Agent`/`Team`/`Pipeline` design and its collaboration modes — a team's
    "who talks to whom, in what order" is naturally a graph, which fits
    SEQUENTIAL/PARALLEL/HIERARCHICAL better than CrewAI's crew/task
    abstraction.
  - bestteam already builds on `langchain_core` for model specs, tools, and
    `with_structured_output`. LangGraph is a natural extension of that same
    stack, rather than adopting a second framework's conventions on top.
- **Consequences**: The `EngineAdapter` ABC (`src/bestteam/adapters/base.py`)
  keeps all LangGraph-specific code inside `LangGraphAdapter`, so a CrewAI
  (or other) adapter could be added later behind the same public API. This
  is currently **unimplemented and not prioritized** — see `STATUS.md`.

## Deployment: per-customer instance, no multi-tenancy

- **Status**: **Superseded** (2026-07-16) by "Deployment: org-scoped
  multi-tenancy, one codebase for both models" below.
- **Context**: bestteam needs a deployment model for delivering the Team
  Builder + monitoring UI to customers.
- **Decision (original)**: bestteam ships as **one independent instance per
  customer** (Docker Compose, its own SQLite database), not a shared
  multi-tenant SaaS.
- **Why superseded**: the business now anticipates one shared hosted
  platform serving several customer organisations (one or more accounts per
  customer org, each org on its own external services). A shared instance
  under the original model had zero cross-customer isolation.

## Deployment: org-scoped multi-tenancy, one codebase for both models

- **Status**: Accepted (2026-07-16)
- **Context**: several customer organisations should be servable from one
  hosted deployment, with multiple employee accounts per org, without
  giving up the existing per-customer-instance option.
- **Decision**: row-level multi-tenancy via an `organizations` table and
  `org_id` columns on every org-owned resource (users, agents, teams,
  knowledge bases, skills, pipelines, builder sessions, runs, usage), with
  API-layer scoping through a `get_current_org` dependency. **The same code
  serves both deployment models** — a per-customer instance is simply a
  deployment with one org (the migration backfills `default`), a shared
  platform is one with many.
- **Consequences**:
  - Public registration is removed; orgs and accounts are provisioned by
    the platform operator via the `ui.backend.admin` CLI. Platform
    operators are org-NULL users; org users never see another org's data
    (cross-org access is 404 — existence is not revealed).
  - Component names are unique per `(org_id, name)`, not globally; skills
    have a platform tier (`org_id IS NULL` = built-ins visible to all).
  - Isolation is enforced in the API layer (central loaders + dependency),
    not the database engine; Postgres row-level security can be layered
    onto the same `org_id` columns later if the DB moves off SQLite.
  - Per-org secrets were NOT part of this decision. Per-org **email**
    credentials are now implemented (encrypted secrets store + `admin
    set-email`; spec `2026-07-18-per-org-email-credentials-design.md`);
    per-org LLM credentials and a self-service settings UI remain future.
    Process-env email credentials still must not be set on a shared instance
    (the env path stays single-org and is refused on multi-org).
  - **Interim: one member per org is enforced at the schema level** — a
    partial unique index on `users.org_id WHERE org_id IS NOT NULL`
    (migration `e1f2a3b4c5d6`), with `create_user` doing a friendly
    pre-check. The architecture allows multiple accounts per org, but
    org-scoped resources — notably the self-service shared mailbox — have no
    per-member privilege separation yet: any member can
    connect/redirect/disconnect them. Until a per-org admin role exists, a
    second member would mean unprivileged co-management of the org's mailbox,
    so the invariant is enforced (not merely assumed) and a race or a bypass
    of `create_user` still can't create one. A database upgraded from the
    earlier multi-member architecture is not mutated automatically: the
    migration **refuses and names the offending orgs**, and — because
    `create_all` never adds the index to an existing table — the **backend
    also refuses HTTP startup** while a violation exists, so the pre-migration
    rollout window can't be served through. Recovery is via the operator CLI
    (`admin delete-user` / `move-user`, runnable in a throwaway container while
    HTTP is blocked); accounts are never auto-deleted. See
    `docs/deployment.md` ("Recovering a legacy multi-member org"). Lifting the
    constraint is gated on the per-org admin role (deferred sub-project C).
    Platform operators (org-NULL) are exempt — there can be several.
  - See `docs/superpowers/specs/2026-07-15-org-multi-tenancy-design.md`.

## Memory: SQLite + BM25 in-house, not the mem0 library

- **Status**: Accepted
- **Context**: The platform needed per-user memory so runs can recall a
  user's preferences/history across sessions (working / episodic / semantic /
  procedural). `mem0` was evaluated as an off-the-shelf option.
- **Decision**: Implement memory in-house on **stdlib `sqlite3` + BM25**
  (reusing the CJK-aware tokenizer already shared with the knowledge base),
  behind the existing `Memory` ABC — **no `mem0` dependency**.
- **Reasons**:
  - Matches the project's established "no vector store, no extra service, own
    SQLite file" posture (same stack as `local_folder` knowledge bases), so it
    deploys with zero new infrastructure.
  - Default path is **$0 and offline** (episodic recall needs no LLM); richer
    semantic/procedural extraction is opt-in via `BESTTEAM_MEMORY_MODEL`.
  - mem0 would pull in a vector store + per-run LLM extraction calls,
    contradicting the zero-infra default.
- **Consequences**:
  - The `Memory` ABC keeps the store swappable — a `Mem0Memory(Memory)` (or
    Redis/Postgres-backed) implementation can drop in later with **no changes
    to agents, the adapter, or the API**.
  - Recall started as single-stage BM25 with no rerank/expansion, and no
    dedup of semantic/procedural records — accepted trade-offs for the
    in-house MVP. Most of that has since been lifted **without** taking on a
    vector store or a new service, keeping this decision intact: recall is
    still BM25 by default, with opt-in hybrid BM25+vector (RRF-fused, with
    type-aware recency decay) via `BESTTEAM_MEMORY_EMBEDDING_MODEL`, opt-in
    query expansion via `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL`, and opt-in
    reranking via `BESTTEAM_MEMORY_RERANK_MODEL`. Semantic records get exact
    dedup on write plus LLM-mediated near-duplicate/update resolution.
    **Procedural records still have no dedup or consolidation** — the
    remaining piece, tracked in `STATUS.md`.

## Property Maintenance Inbox: no `Case`/work-item entity in Phase 1

- **Status**: Accepted
- **Context**: The first vertical solution template for a property
  management pilot needed a way to answer "what did the AI do with today's
  maintenance emails" without turning the platform into a property
  management system. The natural-seeming design is a `MaintenanceCase` /
  `WorkOrder` with a status lifecycle (new → in progress → resolved), an
  owner, and a close action.
- **Decision**: Release 1A introduces a generic, **immutable**
  `automation_item_results` table (one row per input item per Run — `status`,
  `needs_attention`, a capped/validated `payload`) instead of any
  business-entity table. There is no state machine, no owner/assignment, no
  close/reopen action, and no cross-run aggregation of "this tenant's
  ongoing issue." A result row describes what one Run did to one input; it
  never becomes true "case data" a human is expected to advance.
- **Reasons**:
  - A `Case`/`WorkOrder` immediately implies a lifecycle, ownership, SLAs,
    and eventually leases/properties/vendors/invoices as first-class
    entities — a property-management-system rewrite of this platform's
    scope, not an incremental automation feature.
  - The platform's job is to process one input well and hand the human a
    clear, auditable "here's what happened" record; long-running business
    state is the customer's existing PMS's job, not this platform's.
  - `automation_item_results` is deliberately vertical-agnostic (`source_type`,
    `result_type` columns) so the same infrastructure can back a future
    consulting/quoting/invoice-inbox template without a new table.
- **Consequences**:
  - The Activity page's Needs-attention list has no Approve/Assign/Close
    buttons by design — a customer reviews and sends drafts in their own
    mailbox and continues business process in their existing PMS.
  - Server-side normalization (`ui/backend/automation_results.py`) only
    engages for a run whose output parses as JSON declaring itself this
    vertical's envelope (`result_type ==
    "property_maintenance_email_batch"`); every other org's existing,
    unrelated email-trigger workflow (free-text output) is completely
    unaffected. This is a narrower reading than a literal "always synthesize
    an error result for every trigger run" — see the result module's own
    docstring for the reasoning.
  - If pilot data later shows customers genuinely lack a usable PMS and need
    the platform to track state across days, that is a distinct, larger
    product decision (Case/work-item entity) to be made on its own — not a
    silent scope-creep of the email-automation feature. See
    `docs/superpowers/specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md`
    section 5.

## Email: OAuth over IMAP for Microsoft 365, not a Graph connector

- **Status**: Accepted (2026-08-17)
- **Context**: Exchange Online no longer accepts basic authentication, so an
  M365 org could not connect a mailbox at all — `build_org_imap_backend` only
  ever built a password login. The email roadmap's Phase 2 named a
  "MailboxConnector abstraction + Graph/Gmail OAuth".
- **Decision**: Reach Exchange Online through IMAP with SASL XOAUTH2 and
  app-only client credentials, stored per org. No connector protocol, no
  Graph-native code, no Gmail, no interactive authorisation-code OAuth.
- **Reasons**:
  - The roadmap item optimised for the abstraction rather than the blocker.
    Graph-native would mean a second polling implementation (delta queries), a
    second draft implementation, and migrating `EmailTrigger.last_uid`/
    `uidvalidity` to opaque cursors — and it would *regress* Phase 0, because
    Graph's server-side `createReply` cannot carry the `X-BestTeam-Source-Key`
    header that retry reconciliation reads from the Drafts folder.
  - XOAUTH2 changes one function. Everything above `_connect()` — the UID
    cursor, drafts resolution, the source-key headers, Phase 1's event ledger,
    the whole of `email_trigger.py` — is untouched, because after
    `AUTHENTICATE` succeeds the session is an ordinary authenticated IMAP
    session.
  - A `MailboxConnector` protocol would abstract over one and a half
    implementations (`_ImapBackend`, plus a `_GraphBackend` the multi-tenant
    path still would not use). The right time to extract it is while writing a
    genuine second connector, so it is derived from two real ones rather than
    invented ahead of one.
  - Gmail is not blocked — app passwords still authenticate there. App-only
    Gmail needs a service account with domain-wide delegation, covering every
    mailbox in the customer's domain: a strictly worse blast radius than
    Exchange's per-mailbox Application Access Policy, for a customer who is not
    blocked.
  - Interactive OAuth needs a multi-tenant app registration this project would
    own and a stable public HTTPS redirect URI. bestteam ships per-customer
    with operator-provisioned orgs and no public registration (see "org-scoped
    multi-tenancy" above), so app-only credentials stored per org fit the
    existing model with no new infrastructure.
  - The token provider is stdlib `urllib`, not httpx: the per-org IMAP path has
    no third-party HTTP dependency today and `backend-optional-deps` runs
    without optional extras.
- **Consequences**: Customers must do a one-time Azure app registration
  (`docs/deployment.md` → "Microsoft 365 mailboxes"); the platform cannot do it
  for them. No test can prove a live tenant accepts the flow, so
  `docs/email-smoke-test.md` §9 is the gate before selling M365 support. If
  Graph-native ever becomes necessary, nothing here blocks it — but it should
  be justified by a capability Graph has and IMAP lacks, not by the connector
  abstraction on its own.

## Alerts go in-app and to a webhook, never by email (email Phase 3a)

- **Date**: 17 Aug 2026
- **Status**: accepted
- **Context**: Phase 0 made a failing trigger visible on the trigger row, but
  nothing notified anyone. The obvious channel for "your email automation is
  broken" is email — which this product deliberately cannot send.
- **Decision**: Alerts land in an in-app list, and optionally on one
  admin-configured webhook per org. No SMTP is added.
- **Why**:
  - The draft-only toolkit's entire containment argument is that the worst
    outcome is a bad draft a human reviews, because there is no send verb
    anywhere. Adding SMTP to deliver alerts would put a send capability in the
    process purely for the convenience of the alerting feature, and every
    future reader would reasonably ask why the email agent may not use it.
  - In-app alone is too weak: an admin who does not log in for a week does not
    learn for a week, which is the exact failure being fixed.
  - A webhook reaches Slack, Teams or an on-call rota without this codebase
    knowing about any of them, and the customer already has one of those.
  - An admin-configured webhook is **not** the model-chosen egress that
    `find_email_egress_conflicts` refuses. The destination is fixed by a human;
    the refused case is an agent choosing a URL while reading
    attacker-controlled mail. It is still HTTPS-only and still goes through
    `check_host_allowed`, because a tenant admin is not trusted to reach the
    host's internal network.
- **Consequences**: Self-hosted internal webhook endpoints are unsupported.
  Payloads carry health information only — never a subject, address or body —
  so a webhook cannot become an email-content exfiltration path. There are no
  per-user preferences, digests or quiet hours until a customer asks.

## Email and egress tools are refused per PIPELINE, not per agent (email Phase 3a)

- **Date**: 17 Aug 2026
- **Status**: accepted, supersedes the per-agent rule shipped in Phase 0 (0.6)
- **Context**: Phase 0 refused an agent holding both an email tool and
  `http_get`/`web_search`, and documented "split them across two agents" as the
  remedy. A review pointed out that the split contains nothing.
- **Decision**: Refuse the combination anywhere in a pipeline, regardless of
  which agent holds what, and drop the splitting advice.
- **Why**:
  - `_agent_node` (`adapters/langgraph_adapter.py`) feeds each agent's output
    into the next agent's context, and a pipeline's steps share state. An
    injected instruction the mail agent reads therefore arrives in the egress
    agent's prompt as ordinary text.
  - The check is deliberately blunt — it does not reason about ordering or
    collaboration mode. That reasoning would have to be redone, correctly,
    every time routing changes, and a wrong answer is an exfiltration path.
  - Nothing legitimate is refused: no shipped pipeline combines the two.
- **Consequences**: A customer who genuinely needs both must run them as
  separate pipelines against separate deployments. The old advice is gone from
  the rejection message and from `tests/test_deploy_validation.py`, whose
  `..._is_fine` test now asserts the opposite.

## A timed-out run stays retriable; drafting became idempotent instead (email Phase 3a)

- **Date**: 17 Aug 2026
- **Status**: accepted
- **Context**: Phase 0's watchdog marks a run that outlived the run timeout as
  failed so the trigger stops being blocked, but it cannot stop the worker — a
  node inside `pipeline.stream()` is not interruptible. A review found that the
  released run is therefore retriable while its worker may still draft, and
  proposed keeping it non-retriable until the worker acknowledges cancellation.
- **Decision**: Rejected that proposal. Instead, `email_draft_reply` checks the
  Drafts folder for the message's source key under a process-wide per-key lock
  before `APPEND`, and reports `outcome: "draft_exists"` when it skips.
- **Why**:
  - The wedged worker never acknowledges cancellation — being wedged is the
    premise. Waiting for it reinstates the permanent, silent trigger blockage
    that Phase 0 item 0.4 exists to fix, trading a rare duplicate draft for a
    guaranteed silent stop.
  - The defect is not in when a retry is planned; it is that `APPEND` is not
    idempotent. Fixes belong at the point of writing.
  - Both racers are threads of the **same** uvicorn process, so a process-wide
    lock closes the window rather than narrowing it.
- **Consequences**: One extra Drafts search per drafted message, on a path that
  already opens an IMAP connection per tool call. A multi-worker deployment
  reopens the window; that needs the DB-authoritative overlap guard already
  tracked in `STATUS.md`, and the email capability is single-instance by
  design. `CONFIRMED_DRAFT_OUTCOMES` keeps the two readers of confirmed drafts
  in agreement so a skipped draft still excludes its message from the next
  retry.

## Retention purges a run's content and keeps its accounting (email Phase 3b)

- **Date**: 17 Aug 2026
- **Status**: accepted
- **Context**: A generic email team's model output — names, subjects, body
  excerpts — persisted indefinitely in `runs.output` and `trace_events`. Raw
  email bodies were already redacted at the adapter layer, but a generic team's
  *output* is the product, so redaction cannot reach it. The only lever is time.
- **Decision**: A purge clears `runs.input`/`output`, deletes the run's
  `trace_events`, and empties `automation_item_results.payload`, then stamps
  `runs.content_purged_at`. The `runs` row itself, `usage_records`,
  `trigger_context`, and an item result's `status`/`source_key` all survive.
- **Why**:
  - `usage_records.run_id` is non-nullable and `run_analytics_api.py` reports
    over exactly those rows. Deleting run rows would destroy the organisation's
    token and cost history — a worse bug than the one being fixed. The
    customer's concern is the personal data, not that a run happened at 03:14
    and cost $0.02.
  - `automation_item_results.status`/`source_key` are what
    `CONFIRMED_DRAFT_OUTCOMES` uses to exclude already-drafted UIDs from a
    retry. Clearing them would make a retention sweep cause duplicate drafts —
    the exact defect Phase 3a's per-source-key lock exists to prevent.
  - It is honest to state: we stop keeping what was in the email; we keep that
    the work happened.
- **Consequences**: A purged run renders as an explicit "content was removed"
  rather than an empty timeline, in `RunDetail` and in both automation-result
  lists. A purge is not a secure erase — SQLite leaves the old page contents on
  disk until `VACUUM`.

## Retention covers all of an org's runs, not only email-triggered ones (email Phase 3b)

- **Date**: 17 Aug 2026
- **Status**: accepted
- **Context**: The problem was raised about autonomous email runs, and
  `Run.trigger_context` identifies exactly those.
- **Decision**: The policy applies to every run belonging to the organisation.
- **Why**: There is no reliable "is this an email run" predicate. A user who
  opens their email team and clicks Run produces a *manual* run whose output
  contains the same names, subjects and excerpts. Filtering to the autonomous
  half would be more code and less protection, and would leave the customer
  believing they were covered when they were not. One uniform rule is also the
  one a customer can state back to you: run history is kept for N days.
- **Consequences**: An organisation that turns retention on loses old manual
  run history too. That is why the default is NULL — keep forever — so an
  upgrade deletes nothing and enabling it is always a deliberate act.

## Per-data-subject erasure was refused rather than approximated (email Phase 3b)

- **Date**: 17 Aug 2026
- **Status**: accepted
- **Context**: The natural request behind a retention feature is "delete
  everything about alice@example.com".
- **Decision**: Not built. Retention is by age; deletion is by run or by batch.
- **Why**: The address is not stored anywhere indexed. It exists only inside
  `runs.output` free text that the model may have paraphrased or summarised.
  Matching it would both miss (rewritten text) and over-delete (an unrelated
  run that merely mentions the address). Shipping that as an erasure feature
  would be a compliance promise that cannot be kept, which is worse than not
  offering it.
- **Consequences**: A customer with a genuine subject-access erasure obligation
  has age-based retention and per-run deletion, and must be told plainly that
  identifier-based erasure is not available. Recorded in `STATUS.md`.

## Beta runs single-process on SQLite; Postgres is not planned before GA

- **Status**: Accepted (2026-08-22). Supersedes nothing; it writes down a
  ruling that had been made twice in review and re-proposed three times.
- **Context**: Two architecture reviews (2026-08-17, 2026-08-19) and an
  external email-architecture review (2026-08-22) each recommended migrating
  to PostgreSQL, in the last case bundled with a persistent queue, leader
  election and multi-host workers. The question was answered the same way
  each time and kept coming back, so it is recorded here rather than in a
  review thread.
- **Decision**: The beta ships on **one uvicorn process against one SQLite
  file**. No Postgres, no second process, no `--workers N`.
- **Reasons**:
  - **Postgres alone does not lift the ceiling it is proposed to lift.** The
    things that actually confine this deployment to one process are in
    memory, not in the database: `RunRegistry` (live run state and WebSocket
    subscriptions), `email_trigger._dispatch_locks` (the per-org overlap
    guard, one `threading.Lock` per org, `email_trigger.py:108`), the per-source-key draft-idempotency lock, the login throttle and
    the WebSocket ticket store. Moving the rows to Postgres leaves every one
    of them process-local, so the deployment would still be single-process
    and would additionally need a database server.
  - **The migration is not small and not started.** `make_engine` hardcodes
    SQLite and takes a *file path*, not a URL
    (`ui/backend/db/database.py:41`), and `pyproject.toml` carries no
    Postgres driver.
  - **"One member per org" is an authorisation gap, not a database one.** It
    is the constraint customers will hit first, and RBAC fixes it on SQLite
    exactly as well as on Postgres.
  - **The beta's load is known and small**: a handful of orgs, a mailbox
    polled every 120 s, at most four concurrent runs
    (`runtime._executor`, `max_workers=4`). WAL is on, so readers do not
    block behind the one writer.
- **Consequences**:
  - `uvicorn --workers N` and multi-host replicas stay **unsupported**, and
    the horizontal scale-out of the email poller stays blocked — see
    `docs/STATUS.md`. Message-level double-processing is already excluded
    (`db/inbox_events.py:112`'s `claim_events` is a single UPDATE, so under
    SQLite's write lock two claimants cannot be handed the same message), so
    what multi-process would break is the overlap guard and cooperative
    cancellation, not the correctness of the claim itself.
  - **Stop arguing new features from process-local locks.** A correctness
    argument that holds only because two threads share a process is a
    liability the day this decision is revisited; prefer a
    DB-authoritative guard where the cost is comparable.
  - **Revisit when any of these is true** — they are the upgrade triggers,
    and hitting one means writing the migration ADR, not re-opening this one:
    - more than ~10 active organisations;
    - concurrent runs routinely above 4 (the executor's width);
    - a customer needing a second account in one org — **do RBAC first**, it
      is the real blocker and is independent of the engine;
    - any HA or uptime-SLA commitment, which single-process cannot meet.
  - The next reachable step, if pressure arrives before a migration is
    justified, is making the overlap guard DB-authoritative. That became
    cheaper once the startup stale-run sweep landed
    (`runtime.fail_interrupted_runs`), which removed the original objection
    to it.
