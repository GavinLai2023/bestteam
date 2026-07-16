# Code Review Triage Register

Date: 2026-07-14
Scope: repository-wide independent audit issue list and post-fix verification
Reviewed against: implementation, tests, `README.md`, `CLAUDE.md`, project instructions, dependency manifests, deployment documentation, and commit `62ca0ae` against `62ca0ae^`.

## Codex triage definitions

- **Accepted**: Codex found that the cited implementation supports the claim and provisionally recommends correction, subject to developer validation.
- **Needs verification**: the implementation behavior is confirmed, but the expected product or business contract is not established strongly enough to approve a change.
- **Rejected**: the cited evidence does not support the claim or the reported behavior is demonstrably correct.
- **Deferred**: the claim is valid, but the correction belongs to an explicitly deferred architectural phase or should not be implemented independently.

These are Codex's provisional classifications, not authorization to implement a fix. Claude Code must provide developer-side evidence before the owner records a final decision.

## Validation workflow

`Reported` -> `Developer validation` -> `Agreed / Disputed` -> `Accepted / Rejected / Deferred` -> `In progress` -> `Fixed - awaiting independent verification` -> `Verified` -> `Closed`

Claude validation should cite implementation paths, tests, reproduced behavior, and project-specific requirements. It supplies a second technical opinion; it does not make the final decision.

## Codex triage summary

| Status | Count |
|---|---:|
| Accepted | 15 |
| Needs verification | 1 |
| Rejected | 0 |
| Deferred | 1 |

There are no exact duplicate findings. The principal overlaps are:

- Authentication and session security: CR-001, CR-002, CR-013.
- Run lifecycle and terminal-state consistency: CR-003, CR-010, CR-011, CR-012.
- Knowledge-base lifecycle and data integrity: CR-005, CR-008.
- Container and dependency packaging: CR-006, CR-007, CR-014.
- Documentation and supported-surface accuracy: CR-014, CR-015, CR-016, CR-017.

## Issue register

| ID | Codex finding | Claude validation | Agreement | Independent verification | Your decision | Status |
|---|---|---|---|---|---|---|
| CR-001 | Public registration plus unrestricted KB paths enables server-file replacement | Confirmed; boundary guards + resolved-root containment | Agree | API, persisted, and model-generated configs are contained before KB construction; resolved cache targets must remain under the app-owned cache directory | Accept containment fix | Verified |
| CR-002 | Example secret bypasses the startup sentinel | Confirmed; guard now rejects a set of insecure placeholders incl. the example | Agree | Example placeholder is rejected at startup | Accept fix | Verified |
| CR-003 | Validated workflows can be non-executable and background failures can remain running | Confirmed; stream() compiles before its handler, worker had no catch-all | Agree | Worker emits one terminal event; post-completion memory failures are best-effort | Accept fix | Verified |
| CR-004 | Multi-team contribution state contaminates parallel results and overwrites duplicate names | Confirmed; aggregate merged run-global contributions | Agree | Aggregation is scoped to the current team's declared agents | Accept contamination fix; duplicate-name history remains deferred | Verified |
| CR-005 | Deleting an older dependency can leave cached workflows serving deleted data | Confirmed; max(updated_at) key misses deletes | Agree | Dependency fingerprint plus generation-CAS invalidation correctly evicts stale KB workflows; focused deletion regression passes | Accept fix | Verified |
| CR-006 | Alembic migrations are absent from the backend image | Confirmed; alembic.ini + alembic/ not copied | Agree | Image now copies `alembic.ini` and revision tree | Accept fix | Verified |
| CR-007 | Production image omits dependencies required by advertised real-model and interview paths | Confirmed | Agree | Image and README include the `providers-openai` extra | Accept fix | Verified |
| CR-008 | KB upload replacement is non-atomic and has destructive rollback | Confirmed | Agree | Versioned-dir + atomic `CURRENT` pointer keeps a complete prior version through commit; a per-KB lock now serialises the promotion/commit/cleanup so simultaneous same-KB uploads can't leave CURRENT dangling (unique temp file too). Rollback, concurrent-reader, and per-KB-serialisation regressions pass | Accept fix | Verified |
| CR-009 | Interview upload and ffmpeg processing are unbounded | Confirmed | Agree | ASGI body limiter rejects declared, chunked, and understated oversized uploads before multipart parsing; ffmpeg remains time-bounded | Accept fix | Verified |
| CR-010 | RunRegistry subscription can miss a terminal event | Confirmed | Agree | Append, replay, and subscription registration share one lock | Accept fix | Verified |
| CR-011 | Tool-loop exhaustion completes successfully with empty output | Confirmed behavior; existing test asserts intentional empty-success | Agree; product decision taken (partial-success contract) | Exhaustion now returns an explicit `[Agent '<name>' stopped after N tool iterations…]` notice instead of a silent empty string; run still completes so downstream agents keep running. SEQUENTIAL and HIERARCHICAL regressions updated | Accept fix | Verified |
| CR-012 | Usage rows reference runs that are never persisted | Confirmed; runs are RunRegistry-only | Agree; minimal persistence scope taken | `run_in_background` persists a `runs` row (committed before any usage record) and updates its terminal status/output, so `usage_records`/`trace_events` FKs reference a real row. Persistence sits inside the worker try/finally so a persist failure still yields a terminal event (no CR-003 regression); the failed-status write is best-effort and sets an output message. Full restart-survival/history API remains future Phase 5; SQLite FK enforcement intentionally not toggled | Accept fix | Verified |
| CR-013 | Long-lived bearer token is stored in localStorage and placed in the WebSocket URL | Confirmed | Agree | Single-use, short-lived WS tickets replace URL bearer tokens; ticket store is locked | Accept WS-ticket fix; localStorage migration remains deferred | Verified |
| CR-014 | The aggregate tools extra omits the dependency required by http_get | Confirmed | Agree | `httpx` is included in HTTP and aggregate tools extras | Accept fix | Verified |
| CR-015 | Documented python -m bestteam entry point is missing | Confirmed | Agree | Package module entry point delegates to the existing Typer app | Accept fix | Verified |
| CR-016 | Legacy .xls support is advertised but not implemented | Confirmed (openpyxl can't read .xls) | Agree | `.xls` removed from parser, KB discovery, and documentation | Accept fix | Verified |
| CR-017 | README and marketing claims drift from current implementation | Confirmed | Agree | Vector-KB, test-count, and streaming/retention wording now match implementation | Accept fix | Verified |

## Post-fix independent verification

**Overall verdict: Verified within the accepted scope.** All 17 Round 1 findings are now Verified. CR-008, CR-011, and CR-012 — taken on with explicit product/scope decisions (atomic KB replacement; partial-success exhaustion contract; minimal run-row persistence) — were confirmed by re-running their regression tests green on `main` (`tests/test_crud_api.py`, `tests/test_workflow.py`/`test_hierarchical_team.py` bounded-loop, `tests/test_usage_metering.py`; 70 passed, 2026-07-15). This is developer-side verification of the accepted scope, not a separate outside audit.

- **CR-001:** `ui/backend/knowledge_bases.py` now resolves cache targets against the backend-owned `_kb_cache` directory; `ui/backend/builder.py` applies that guard to submitted, persisted, and model-generated candidates before SDK validation can construct a vector KB.
- **CR-003 / CR-010 / CR-013:** `ui/backend/runtime.py:78`, `src/bestteam/core/workflow.py:119`, `ui/backend/registry.py:54`, and `ui/backend/ws_tickets.py:28` provide the terminal-event, subscription-lock, and ticket-lock guarantees covered by the new regression tests.
- **CR-005 / CR-008 / CR-009:** generation-aware workflow-cache invalidation, the atomic KB `CURRENT` pointer layout, and the ASGI streamed-body limiter are covered by focused deletion, rollback, concurrent-reader, and chunked-upload regressions.
- **CR-002 / CR-004 / CR-006 / CR-007 / CR-014 / CR-015 / CR-016 / CR-017:** the startup secret guard, team-scoped aggregation, image/package declarations, CLI entry point, `.xls` removal, and documentation changes are present and consistent with their regression coverage.
- **CR-011:** `src/bestteam/adapters/langgraph_adapter.py::_run_agent` returns `_tool_loop_exhausted_notice(agent.name)` when the tool loop exhausts while the model is still requesting tools, so an exhausted turn no longer looks like a silent empty success. Covered by the SEQUENTIAL (`tests/test_workflow.py`) and HIERARCHICAL (`tests/test_hierarchical_team.py`) bounded-loop regressions.
- **CR-012:** `ui/backend/runtime.py::run_in_background` persists a `runs` row before draining the stream and updates its terminal status/output, so `usage_records`/`trace_events` foreign keys reference a real row (`tests/test_usage_metering.py`). The persistence runs inside the worker `try/finally`, so a failure of the up-front commit still publishes a terminal `run_failed` event and closes the session rather than leaving the run stuck `running` (CR-003 regression guarded by `test_run_in_background_still_publishes_terminal_event_if_run_row_commit_fails`); the failed-status write is best-effort (rollback + swallow) and records an output message. Restart-survival, `trace_events` persistence, a run-history API, and SQLite FK enforcement remain future Phase 5 work.

### CR-001 and CR-009 follow-up verification (2026-07-14)

- **CR-001 — Verified:** model-generated candidates are pre-validated before `validate_specification()` constructs a KB; API, persisted, and inline configs resolve their cache target under the backend-owned `_kb_cache` directory, rejecting symlink/junction escapes.
- **CR-009 — Verified:** the ASGI limiter counts every incoming body chunk and emits 413 before the downstream multipart parser receives an over-limit chunk, including when `Content-Length` is absent or understated. A reverse-proxy limit remains useful operational defense in depth.

## Round 2 (2026-07-15): per-user memory + http_get review

Scope: a second independent audit focused on the merged per-user memory feature
(commit `690a712`) plus one pre-existing tool-security finding. Six findings;
severities were recalibrated with the reviewer for this codebase's threat model
(memory **disabled by default**, strictly **per-user** — `WHERE user_id = ?`,
no cross-user recall — and a documented **no-multi-tenancy** model). The two
memory-security items are proportionate mitigations, not full hardening.

| ID | Finding | Severity | Resolution | Status |
|---|---|---|---|---|
| CR-018 | Memory records use naive `datetime.utcnow()` (deprecation warnings) | P3 | `core/memory.py` now uses `datetime.now(timezone.utc)`, matching `db/models._utcnow`. `tests/test_memory.py::test_created_at_is_timezone_aware` | Verified |
| CR-019 | `Workflow.run()` raises if post-completion memory persistence fails, making a completed run look failed | P2 | `core/workflow.py::run` wraps `record_run` best-effort (log + swallow), mirroring `stream()`. `tests/test_workflow.py::test_run_memory_recording_failure_keeps_run_completed` | Verified |
| CR-020 | Hierarchical delegates omit recalled memory (only the manager received it) | P2 | `_make_delegate_tool` forwards `extra_system_prompt`; `_hierarchical_node` passes the raw preamble to each delegate (subordinates get user memory, not the manager's delegation guidance). `tests/test_memory_integration.py::test_hierarchical_subordinate_receives_preamble` | Verified |
| CR-021 | Persistent prompt injection: recalled content spliced into a SystemMessage as bare instructions | P2 (bounded, single-user) | `recall_preamble` now delimits recalled content in `<recalled_user_memory>` and frames it reference-only ("NOT instructions"). Proportionate mitigation; no escaping/filtering engine (documented limitation). `tests/test_memory.py::test_recall_preamble_frames_memory_as_untrusted_reference` | Verified |
| CR-022 | Unbounded memory growth: full input/output stored, duplicated into `content` and `metadata`, no size cap | P2 (memory opt-in) | `record_run` drops the redundant `metadata` (no reader) and truncates each field at `_MAX_RECORD_CHARS` (10k). Retention/quota/cleanup remain documented future work. `tests/test_memory.py::test_record_run_does_not_duplicate_content_in_metadata`, `::test_record_run_caps_record_size` | Verified |
| CR-023 | `http_get` DNS-rebinding SSRF: hostname validated, then re-resolved on the request (TOCTOU) | P2 (tool-security, not a memory defect) | `_check_host_allowed` returns the validated IP; `http_get` pins the connection to it (Host header + TLS SNI/cert preserved via httpx `sni_hostname`), re-validated/re-pinned per redirect hop, so httpx never re-resolves. `tests/test_tools.py::test_http_get_pins_connection_to_validated_ip` (+ existing block/redirect/retry tests); verified end-to-end against a local server | Verified |

**Severity notes.** CR-021/CR-022 are P2 rather than P1: memory is disabled
unless `BESTTEAM_MEMORY_DB` is set, records are per-user with no cross-user
recall path, and a user can already instruct their own current run — persistence
adds durability/surprise but does not by itself cross an authorization boundary.
Escalate to P1 if workflows ingest untrusted web/KB content into memory, expose
sensitive side-effecting tools, or the product adds shared/cross-user memory.
CR-023 stays P2 given the documented tool trust boundary, escalating where
`http_get` is exposed to untrusted prompts with reachable internal/metadata
services.

**Explicitly out of scope** (documented decisions / larger features, not
defects): full memory retention/quota/cleanup and a deletion API/UI;
run-ownership/authorization (conflicts with the no-multi-tenancy decision,
`docs/DECISIONS.md`); adding CI lint/type/security tooling.

**Verification (2026-07-15).** All six marked Verified. Basis: a TDD red→green
regression per finding (named above), the full suite green at 335 passed, and
GitHub CI (backend + frontend) green on `main`; CR-023 additionally driven
end-to-end (validate→pin→connect) against a local server, with the httpx
explicit-`Host`-on-IP behavior confirmed by a local probe. Landed on `main` via
PR #7 (`d932b40`, CR-018…CR-022) and PR #9 (`6b9af52`, CR-023). This is
developer-side verification of the accepted scope, not a separate outside audit.

## Round 3 (2026-07-15): admin role + per-user memory management UI

Scope: an independent audit of the `feat/admin-memory-management` branch — a new
`is_admin` role gating the Advanced config page and a new admin-only Memory
management page (`/api/memory`). Delivered over three review passes (initial
5-finding report → partial-fix re-review → final re-review). Landed via **PR #12**
(commits `0e86334` feature, `bacd6ec` + `e8f0018` remediation); not yet merged to
`main`.

The security spine of this feature: admin is granted **only** via the operator
CLI (`python -m ui.backend.admin promote <username>`) — never from an
unauthenticated username match or from public registration — and the backend
enforces `get_current_admin` on every `/api/config` and `/api/memory` call
(frontend gating is cosmetic).

| ID | Finding | Severity | Resolution | Status |
|---|---|---|---|---|
| CR-024 | Public-registration admin takeover: username-match auto-promotion (initially at register, latently at startup) meant whoever held a listed username got admin | P1 | Removed all username-match promotion. Registration always creates a non-admin; admin is granted only via `ui/backend/admin.py` (`set_admin_status`). `tests/test_auth.py::test_register_never_grants_admin`, `::test_set_admin_status_promotes_and_demotes` | Verified |
| CR-025 | Startup crash before migration: import-time reconcile read `users.is_admin`, so a DB predating the column crashed on boot | P1 | Removed the import-time reconcile from `db_session.py`; `is_admin` is never read at import. Reviewer imported against a pre-migration DB successfully | Verified |
| CR-026 | Fresh install had no admin path once auto-promotion was removed | P1 | Operator CLI (`python -m ui.backend.admin promote <username>`) + documented post-migration step in `docs/deployment.md`. `tests/test_admin_cli.py` | Verified |
| CR-027 | `Memory` ABC compatibility break: management methods added as abstract would break a legacy four-method `Memory` implementation | P2 | `user_ids`/`user_summaries`/`delete_user`/`close` are concrete on `SqliteBM25Memory` only, not on the ABC, so a four-method store still instantiates. `tests/test_memory.py` management-method coverage | Verified |
| CR-028 | Unbounded admin reads: browse dumped a whole store; search scanned every record (built a full BM25 index) regardless of `limit` | P2 | Browse capped (`_MAX_RECORDS`/`_DEFAULT_RECORDS`); user counts via one `GROUP BY` (`user_summaries()`); `search(max_candidates=)` bounds the candidate scan (admin API passes `_MAX_SEARCH_SCAN`), so both response and work are bounded. Per-run recall's full-store scan is unchanged by default (`max_candidates=None`). `tests/test_memory.py::test_search_bounds_candidate_scan`, `tests/test_memory_api.py::test_search_endpoint_bounds_scan` | Verified |
| CR-029 | Admin user-list (`GET /api/memory/users`) has no pagination/cursor: aggregates all rows, builds every summary in Python | P3 | **Deferred (out of scope for now).** Not exploitable today: admin-only endpoint, opt-in store (disabled by default), and the DB work is already bounded by the `GROUP BY` in `user_summaries` (CR-028) — the only unbounded part is building N summary dicts into one response. Every account is operator-provisioned (public registration was removed in Round 4), and `user_summaries` lists only users who already have memory (a workflow run per account), so there is no cheap/unbounded way to inflate the list. A bare `LIMIT` would hide users from the admin (strictly worse for a management tool) and cursor pagination is disproportionate scope for a simple admin UI. **Reclassified from Rejected → Deferred in Round 4** (see the re-check note below): the shared-platform ceiling raised the response size, so this is now a tracked scalability watch-item rather than a permanent no | Deferred |

**Severity notes.** CR-024–026 are P1 because they concern the admin
authorization boundary itself (who becomes admin, and whether the app boots to
enforce it). CR-027/CR-028 are P2: an SDK-contract regression and a
resource-amplification vector on an admin-only, opt-in surface. CR-029 is P3;
originally rejected on the scaling-profile distinction above, and reclassified to
**Deferred** in Round 4 once multi-tenancy raised the response-size ceiling (see
the Round-4 re-check note) — escalate only if a customer reaches roughly hundreds
of memory-enabled users or a self-service provisioning surface is added.

**Verification (2026-07-15).** CR-024–028 marked Verified: the independent
reviewer confirmed CR-024–027 fixed and CR-028's record browse/search bounded
across the three passes, backed by a TDD regression per finding (named above)
and the full suite green at **356 passed** (one pre-existing Starlette
deprecation warning). This is developer-side verification plus the reviewer's
read-only confirmation; final independent verification on `main` follows the
**PR #12** merge. CR-029 is a documented scope decision, recorded in the PR body.

## Round 4 (2026-07-16): org multi-tenancy

Scope: an independent, read-only review of the `feat/org-multi-tenancy` branch
(`912d5b9` vs `main` `704797b`) — the `organizations` model, row-level org
isolation, operator-only provisioning, and run/builder ownership (**PR #14**).
The reviewer independently exercised the Alembic upgrade on fresh and simulated
pre-tenancy databases (backfills confirmed correct) and found no P0 defects.
Three findings, all confirmed against the code and fixed on the branch.

| ID | Finding | Severity | Resolution | Status |
|---|---|---|---|---|
| CR-030 | Org-member promotion granted cross-org platform administration: `promote alice` on an org member set `is_admin=True` while keeping `org_id`, and `get_current_admin` checked only the flag — an org-bound admin could target every org via `/api/config?org=` and `/api/memory` | P1 | Enforced at three layers: `set_admin_status` refuses to promote org members (`ValueError`; the operator creates an org-less account via `create-user --platform` instead); `get_current_admin` requires `is_admin AND org_id IS NULL` (defense in depth for hand-edited/pre-fix rows); the run GET/WS admin passthrough requires the same. Test fixtures split into platform-admin + org-user tokens. `tests/test_auth.py::test_promote_org_member_is_rejected`, `::test_org_bound_admin_flag_does_not_grant_admin_api`, `tests/test_admin_cli.py::test_promote_org_member_errors`, `tests/test_ws_stream.py::test_org_bound_admin_flag_gets_no_cross_org_run_passthrough` | Fixed |
| CR-031 | Global email capability could expose one customer's mailbox to every tenant: `email_triage_reply` is a NULL-org built-in visible to all orgs, and `BESTTEAM_EMAIL_*` credentials are process-wide — a second org's users could triage the first customer's mailbox. Previously documentation-only (`.env.example` warning) | P1 | `ensure_email_single_org(db, creating=0)` (`db/orgs.py`): hard `RuntimeError` when `BESTTEAM_EMAIL_BACKEND` is set and org count would exceed 1. Wired at backend startup (`db_session.py` bootstrap — refuses to boot) and in the `create-org` CLI (refuses the second org). Interim guard until the per-org secrets store (sub-project 2) exists. `tests/test_db.py::test_email_guard_*`, `tests/test_admin_cli.py::test_create_second_org_errors_when_email_configured` | Fixed |
| CR-032 | Persisted runs lost the initiating user: the `runs` row carried `org_id` but no initiator column (the in-memory `Run.username` dies on restart), and builder test runs omitted the username even from the registry | P2 | Nullable `runs.username` column (migration `c9d0e1f2a3b4`, guarded add-column; verified on a copy of the real dev DB); `run_in_background(username=)` stamps it — deliberately separate from `user_id` so builder sandbox runs record the initiator without touching per-user memory; `main.create_run` and builder `create_test_run` both pass it. `tests/test_usage_metering.py::test_run_in_background_stamps_usage_and_run_row_with_org`, `tests/test_builder_api.py::test_test_run_executes_validated_specification` | Fixed |

**CR-029 escalation trigger re-checked — reclassified Rejected → Deferred (P3).**
Round 3's trigger was "escalate only if the product adds multi-tenancy **or an
unbounded-registration surface that can inflate the distinct-user count without
per-account compute cost**". Both halves matter, and the half that would make
the count grow cheaply and without bound — public registration — was *removed*
in this same change. Every account is now operator-provisioned and memory is
opt-in, so there is no cheap unbounded growth path, and the DB work is already
bounded by CR-028's `GROUP BY`. That is why it is **not** a release blocker and
why pagination is **not** being built now. What *did* change: multi-tenancy
raised the ceiling — on a shared platform the list is the sum of memory-enabled
users across all customer orgs, not one org's headcount — so a flat "Rejected"
is now slightly too strong. Reclassified to **Deferred**: revisit (add a `LIMIT`
+ cursor, or an operational cap) if a customer reaches roughly hundreds of
memory-enabled users, or if any self-service account-provisioning surface is
ever added. This adjustment came out of the Round-4 release review; no code
change accompanies it.

**Severity notes.** CR-030/CR-031 are P1: both are tenant-isolation boundary
violations (privilege scope and mailbox confidentiality respectively) on the
feature whose whole purpose is tenant isolation. CR-032 is P2: an audit/ops gap
with no confidentiality impact (ownership checks were already org-level).

## Detailed Codex assessments

### CR-001 - Public registration plus unrestricted KB paths enables server-file replacement

- **Codex triage:** Accepted
- **Evidence assessment:** Supported, with one qualification. `POST /api/auth/register` has no authentication or bootstrap guard (`ui/backend/auth_api.py:43`), users have no role field (`ui/backend/db/users.py:12`), and `/api/config` only requires any valid user (`ui/backend/crud.py:52`). `KnowledgeBaseSpec` accepts `path` and `cache_path`, the loader preserves absolute values (`src/bestteam/core/loader.py:94`), and vector cache creation replaces the selected path (`src/bestteam/core/vector_knowledge_base.py:93`). `fake:` embeddings make the write path reachable without provider credentials. The container declares no non-root `USER` (`Dockerfile:1`). The qualification is that `docs/deployment.md` explicitly allows subsequent users to be created through the same endpoint, so open registration is documented behavior rather than a documentation contradiction.
- **Decision:** The documented shared-user model does not justify unauthenticated account creation combined with unrestricted filesystem write paths. The combined chain is critical even if either behavior was originally intentional.
- **Overlap:** Authentication hardening overlaps CR-002 and CR-013. Filesystem path authorization and container privilege are unique to this issue.
- **Implementation risk:** High. Incorrect path restrictions can break valid local KB deployments, and changing registration can lock out new installations.
- **Affected components:** Authentication API and user model; config CRUD; KB specification and loader; vector cache; deployment/container security; auth and KB tests.
- **Smallest safe fix unit:** First constrain KB `path` and `cache_path` to configured application-owned roots and reject absolute/traversing values at API boundaries, with path-containment tests. Track registration gating and non-root container execution as separate follow-up units within the same security epic.

### CR-002 - Example secret bypasses the startup sentinel

- **Codex triage:** Accepted
- **Evidence assessment:** Directly supported. `.env.example:16` sets `change-me-to-a-long-random-value`, while `ui/backend/auth.py:22` defines a different sentinel and `ui/backend/main.py:44` rejects only that sentinel. The documented example therefore passes startup unchanged.
- **Decision:** A publicly known signing key defeats the startup safeguard and permits token forgery.
- **Overlap:** Part of the authentication hardening epic with CR-001 and CR-013, but a distinct root cause.
- **Implementation risk:** Low.
- **Affected components:** `.env.example`; backend startup guard; authentication tests; deployment documentation.
- **Smallest safe fix unit:** Make the example value exactly equal to the rejected sentinel and add one startup test proving the copied example cannot boot unchanged.

### CR-003 - Validated workflows can be non-executable and background failures can remain running

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. `validate_specification()` returns `_build_workflow()` without compiling (`src/bestteam/core/specification.py:219`). Unsupported `debate` mode is rejected only during adapter compilation (`src/bestteam/adapters/langgraph_adapter.py:349`). `Workflow.stream()` compiles before its `BestTeamError` handler (`src/bestteam/core/workflow.py:98`), and `run_in_background()` has only `finally`, not an exception-to-terminal-event handler (`ui/backend/runtime.py:54`).
- **Decision:** Validation does not meet its stated compile-level contract, and the backend does not guarantee a terminal run status after worker failure.
- **Overlap:** Shares the run-lifecycle epic with CR-010, CR-011, and CR-012. CR-003 concerns pre-run validation and worker exception handling; CR-010 is a delivery race.
- **Implementation risk:** Medium. Error translation and event ordering affect builder validation, SDK streaming, monitoring, and tests.
- **Affected components:** Specification validation; adapter compilation; workflow streaming; background runtime; registry; builder/config APIs; tests.
- **Smallest safe fix unit:** Wrap the entire background worker body in a catch-all that publishes one sanitized `run_failed` event and add a regression test for compilation failure. Compile-during-validation can follow as a separate validation improvement.

### CR-004 - Multi-team contribution state contaminates parallel results and overwrites duplicate names

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. `_TeamState.contributions` uses a run-global dictionary reducer keyed by `agent.name` (`src/bestteam/adapters/langgraph_adapter.py:33`). `_aggregate_node()` merges every accumulated contribution (`src/bestteam/adapters/langgraph_adapter.py:315`), and final `steps` are generated from the same dictionary (`src/bestteam/adapters/langgraph_adapter.py:403`). This permits prior-team contamination and last-write-wins history loss for repeated names.
- **Decision:** The behavior violates team isolation and makes workflow output and execution history dependent on unrelated prior steps.
- **Overlap:** Relates to CR-017's observability claims but is an independent execution-correctness defect.
- **Implementation risk:** High. State schema and reducers affect sequential, parallel, hierarchical, streaming, result construction, and usage attribution.
- **Affected components:** LangGraph adapter state; graph wiring; aggregation; stream/result conversion; usage attribution; workflow tests.
- **Smallest safe fix unit:** Add a team-scoped contribution key or current-team contribution map used only by `_aggregate_node()`, plus one sequential-then-parallel regression test. Preserve the existing global event history until a separate history redesign is approved.

### CR-005 - Deleting an older dependency can leave cached workflows serving deleted data

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. Workflow cache freshness is based on global `max(updated_at)` values (`ui/backend/main.py:87`). Deleting a skill or KB does not necessarily change either maximum (`ui/backend/crud.py:87`), so a cached workflow can retain the deleted in-memory object and its document chunks.
- **Decision:** Deletion semantics are incorrect for both confidentiality and configuration consistency.
- **Overlap:** Shares the KB lifecycle epic with CR-008 but is not a duplicate: CR-005 concerns runtime cache invalidation; CR-008 concerns filesystem replacement.
- **Implementation risk:** Medium.
- **Affected components:** Workflow cache; skill/KB CRUD mutation paths; KB loading; deletion tests.
- **Smallest safe fix unit:** Explicitly clear `_workflow_cache` after every skill or KB create, update, and delete operation, with a deletion regression test. A versioned dependency fingerprint can be considered later.

### CR-006 - Alembic migrations are absent from the backend image

- **Codex triage:** Accepted
- **Evidence assessment:** Directly supported. `Dockerfile:4` copies only `pyproject.toml`, `src`, and `ui`. `docs/deployment.md:47` instructs operators to run Alembic, but neither `alembic.ini` nor the `alembic/` revision tree is copied.
- **Decision:** The canonical documented upgrade command cannot work in the shipped image. `create_all()` is not an upgrade mechanism for existing schemas.
- **Overlap:** Container-packaging epic with CR-007; no duplicate root cause.
- **Implementation risk:** Low.
- **Affected components:** Dockerfile; Alembic assets; deployment documentation; CI/container smoke tests.
- **Smallest safe fix unit:** Copy `alembic.ini` and `alembic/` into the backend image and add a container smoke check for `alembic current`.

### CR-007 - Production image omits dependencies required by advertised real-model and interview paths

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. The image installs `.[ui,tools]` (`Dockerfile:7`). Those extras omit `langchain`, provider integrations, and `openai` (`pyproject.toml:15`). String model resolution imports `langchain.chat_models.init_chat_model` (`src/bestteam/adapters/langgraph_adapter.py:42`), and interview transcription imports `openai` (`ui/backend/interview.py:158`). README examples use `openai:` model specs after the documented install (`README.md:29`).
- **Decision:** The standard image cannot execute the primary documented commercial paths with keys alone.
- **Overlap:** Packaging epic with CR-006 and CR-014. CR-007 is production-provider coverage; CR-014 is the SDK tools extra.
- **Implementation risk:** Medium. Provider selection affects image size, supported integrations, dependency compatibility, and licensing/operations.
- **Affected components:** Optional dependency definitions; Dockerfile; model resolution; interview API; README/deployment docs; container tests.
- **Smallest safe fix unit:** Add an explicit `providers-openai`/`interview` extra containing `langchain`, `langchain-openai`, and `openai`, install it in the official image, and smoke-test imports without making network calls.

### CR-008 - KB upload replacement is non-atomic and has destructive rollback

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. Upload writes directly into the existing KB directory without removing absent old files (`ui/backend/crud.py:156`). Any later exception removes the entire directory (`ui/backend/crud.py:196`), including the previously valid KB, while the prior database record can remain.
- **Decision:** Both stale-file retention and destructive rollback are concrete data-integrity failures.
- **Overlap:** Shares KB lifecycle scope with CR-005 but addresses a separate storage transaction.
- **Implementation risk:** High. Filesystem and database commits must remain coordinated across Windows and POSIX behavior.
- **Affected components:** KB upload API; local filesystem layout; KB validation; database update; cleanup logic; upload tests.
- **Smallest safe fix unit:** Write and validate into a sibling staging directory, then replace the live directory only after validation succeeds. Add tests for omitted old files and failed replacement preserving the prior KB.

### CR-009 - Interview upload and ffmpeg processing are unbounded

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. The request body is fully buffered with `file.file.read()` (`ui/backend/interview.py:138`). Files larger than 25 MB are accepted when ffmpeg is available, and ffmpeg subprocess calls have no timeout (`ui/backend/interview.py:63`, `ui/backend/interview.py:81`). The production image always installs ffmpeg (`Dockerfile:3`).
- **Decision:** Authentication does not prevent an authorized or newly registered caller from exhausting memory or worker capacity.
- **Overlap:** Amplified by CR-001's registration exposure, but independently valid for any authorized user.
- **Implementation risk:** Medium.
- **Affected components:** Interview upload API; temporary-file handling; ffmpeg subprocess wrapper; worker pool; proxy/request-size configuration; tests.
- **Smallest safe fix unit:** Enforce a hard application-level upload ceiling before transcription and add a finite timeout to both ffmpeg subprocess calls. Streaming-to-disk can be a later optimization.

### CR-010 - RunRegistry subscription can miss a terminal event

- **Codex triage:** Accepted
- **Evidence assessment:** Supported by the synchronization structure. `subscribe()` replays existing events and only then appends the subscriber (`ui/backend/registry.py:55`), while worker-thread `publish()` mutates the event list and subscribers without a shared lock (`ui/backend/registry.py:41`). A publish between replay and registration is lost to that subscriber.
- **Decision:** The race is real even though it may be timing-sensitive and was not covered by the deterministic suite.
- **Overlap:** Run-lifecycle epic with CR-003. CR-003 can leave no terminal event; CR-010 can lose an event that was correctly emitted.
- **Implementation risk:** Medium. Thread/async-loop coordination is sensitive to deadlocks and cross-loop queue behavior.
- **Affected components:** RunRegistry; WebSocket subscription; worker-thread publication; concurrency tests.
- **Smallest safe fix unit:** Add one registry lock covering event append, replay snapshot, and subscriber insertion, plus a deterministic interleaving test.

### CR-011 - Tool-loop exhaustion completes successfully with empty output

- **Codex triage:** Needs verification
- **Evidence assessment:** The behavior is confirmed. `_run_agent()` stops after `_MAX_TOOL_ITERATIONS` and returns the last response content (`src/bestteam/adapters/langgraph_adapter.py:159`), and `tests/test_workflow.py:149` explicitly expects an empty successful output. The cited evidence does not establish that failure is the required business contract.
- **Decision:** Do not change behavior until product requirements decide whether bounded exhaustion means failure, partial success, or a distinct terminal outcome. The current test indicates intentional behavior, even if it is operationally weak.
- **Overlap:** Run-terminal semantics overlap CR-003. This is not a duplicate because it follows a completed adapter call rather than an exception.
- **Implementation risk:** Medium. Changing the outcome can break customers relying on bounded best-effort completion and affects result/trace contracts.
- **Affected components:** Agent tool loop; workflow results; trace events; backend status; SDK and UI tests.
- **Smallest safe fix unit:** First document and approve the exhaustion contract in one requirement/decision entry. If failure is selected, change only the exhausted-loop branch to raise a framework error and update the single bounded-loop test.

### CR-012 - Usage rows reference runs that are never persisted

- **Codex triage:** Deferred
- **Evidence assessment:** Supported. `UsageRecord.run_id` declares a foreign key to `runs.id` (`ui/backend/db/models.py:160`), but runtime runs are created only in `RunRegistry` (`ui/backend/main.py:187`) before usage is committed (`ui/backend/runtime.py:77`). SQLite foreign-key enforcement is not enabled in `ui/backend/db/database.py`. Tests insert usage for nonexistent run IDs. The repository explicitly tracks persistent runs as deferred Phase 5 work (`docs/STATUS.md:58` and the root project instructions).
- **Decision:** The schema inconsistency is real, but enabling the foreign key or partially changing usage persistence without persisting run lifecycle would break current execution. Resolve it with the existing persistent-run phase.
- **Overlap:** Run-lifecycle epic with CR-003 and CR-010; documentation drift also overlaps CR-017.
- **Implementation risk:** High. Requires migrations, transactional ordering, restart behavior, history retention, and API compatibility.
- **Affected components:** RunRegistry replacement; `runs`, `trace_events`, and `usage_records`; runtime transactions; migrations; run APIs; tests; status/decision docs.
- **Smallest safe fix unit:** Within Phase 5, persist a minimal `Run` row before worker submission and update its terminal status before enabling SQLite foreign-key enforcement. Do not enable the FK independently.

### CR-013 - Long-lived bearer token is stored in localStorage and placed in the WebSocket URL

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. The frontend stores the bearer token in `localStorage` (`ui/frontend/src/lib/api.js:4`) and constructs `stream?token=...` (`ui/frontend/src/pages/MonitorPage.jsx:52`). The backend explicitly accepts the query parameter (`ui/backend/main.py:204`), and the default token lifetime is 24 hours (`ui/backend/auth.py:24`). Whether a specific proxy currently logs query strings needs deployment-specific confirmation, but URL exposure itself is definite.
- **Decision:** Query-string credentials and long-lived browser-readable storage create avoidable leakage and XSS consequences.
- **Overlap:** Authentication/session epic with CR-001 and CR-002.
- **Implementation risk:** Medium. Any replacement must work with browser WebSocket limitations and preserve reconnect behavior.
- **Affected components:** Auth/session design; WebSocket endpoint; frontend API storage; monitor page; proxy logging; tests.
- **Smallest safe fix unit:** Add an authenticated REST endpoint that issues a short-lived, single-use WebSocket ticket and use only that ticket in the URL. Keep the existing bearer flow for REST until cookie migration is separately approved.

### CR-014 - The aggregate tools extra omits the dependency required by http_get

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. `http_get` imports `httpx` at invocation (`src/bestteam/tools/http_client.py:67`), while `pyproject.toml:22` omits it from `tools`. README marks `http_get` as requiring no extra package (`README.md:109`), and `src/bestteam/tools/CLAUDE.md:14` incorrectly treats FastAPI as the dependency source.
- **Decision:** An SDK-only `bestteam[tools]` installation does not guarantee all documented built-ins can run.
- **Overlap:** Dependency-packaging epic with CR-007, but this affects the SDK tools contract rather than production providers.
- **Implementation risk:** Low.
- **Affected components:** Optional dependencies; HTTP tool; README/tool documentation; packaging tests.
- **Smallest safe fix unit:** Add a constrained `httpx` dependency to the aggregate `tools` extra and add an import smoke test for every built-in tool under its documented extra.

### CR-015 - Documented python -m bestteam entry point is missing

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. `pyproject.toml:24` defines the `bestteam` console script, and `src/bestteam/cli/__main__.py` supports `python -m bestteam.cli`; there is no `src/bestteam/__main__.py`. `CLAUDE.md` and project instructions use `python -m bestteam`.
- **Decision:** The documented module invocation fails even though the installed console script works.
- **Overlap:** Documentation/supported-surface epic with CR-016 and CR-017.
- **Implementation risk:** Low.
- **Affected components:** Package entry point; CLI documentation; CLI smoke tests.
- **Smallest safe fix unit:** Add `src/bestteam/__main__.py` that imports and invokes the existing Typer app, with one `--help` smoke test.

### CR-016 - Legacy .xls support is advertised but not implemented

- **Codex triage:** Accepted
- **Evidence assessment:** Supported. `parse_file()` advertises and routes `.xls` to `_parse_excel()` (`src/bestteam/tools/file_parser.py:13`, `src/bestteam/tools/file_parser.py:36`), which calls `openpyxl.load_workbook()` (`src/bestteam/tools/file_parser.py:100`). The declared `tools-files` extra includes openpyxl but no legacy `.xls` reader (`pyproject.toml:19`).
- **Decision:** The supported-format contract is incorrect.
- **Overlap:** Documentation/package-surface epic with CR-014 and CR-017.
- **Implementation risk:** Low if support is removed; Medium if a new parser is added.
- **Affected components:** File parser suffix routing; dependency extras; built-in tool documentation; parser tests.
- **Smallest safe fix unit:** Remove `.xls` from accepted suffixes and documentation and add a rejection test. Add `xlrd` only if legacy support is a confirmed customer requirement.

### CR-017 - README and marketing claims drift from current implementation

- **Codex triage:** Accepted
- **Evidence assessment:** Supported, with scope qualification. `README.md:165` reports 42 tests rather than the current 269, and `README.md:175` lists vector KBs as unimplemented despite the implemented vector KB and `docs/STATUS.md:12`. Marketing claims every action is streamed and auditable (`website/src/components/HowItWorks.astro:16`, `website/src/components/Features.astro:16`), while adapter streaming emits agent-completion events rather than prompts, tool calls, or model responses (`src/bestteam/adapters/langgraph_adapter.py:416`) and run history is explicitly in-memory (`docs/STATUS.md:58`). Whether the marketing wording is legally or commercially acceptable is a product decision, but the technical capability mismatch is objective.
- **Decision:** Accept the factual documentation drift and narrow observability claims to the events and retention actually implemented.
- **Overlap:** Persistent-history limitation overlaps CR-012; execution-history completeness overlaps CR-004. This issue is the documentation correction, not the underlying implementation work.
- **Implementation risk:** Low.
- **Affected components:** README; website copy; architecture/status documentation; release checklist.
- **Smallest safe fix unit:** Update the test count and vector-KB roadmap entry, then replace “every action” and “auditable” with wording that states agent-completion events are streamed and run history is process-local.

## Suggested implementation order

1. CR-001 path containment, then CR-002 secret sentinel.
2. CR-003 terminal failure guarantee and CR-010 subscription atomicity.
3. CR-008 atomic KB replacement and CR-005 cache invalidation.
4. CR-006, CR-007, and CR-014 packaging corrections.
5. CR-009 and CR-013 security hardening.
6. CR-004 state isolation after dedicated regression coverage is ready.
7. CR-015, CR-016, and CR-017 low-risk contract/documentation corrections.
8. Resolve CR-011's product contract before implementation.
9. Deliver CR-012 only as part of the persistent-run-state phase.

## Validation notes

- Independent review compared commit `62ca0ae` with `62ca0ae^` and inspected its relevant callers, tests, and documentation.
- The independent reviewer reported 178 changed-surface tests passing with one deprecation warning; no network calls were made.
- Current CR-001/CR-009 re-verification ran eight focused regressions successfully (one deprecation warning): cache-name containment, legacy/inline load containment, Builder containment, declared-size rejection, capped read, and ffmpeg timeout.
- A local full-suite attempt was blocked by sandbox permissions for pytest temporary directories (`C:\Users\User\AppData\Local\Temp` and `C:\tmp`), so it is not treated as product-test evidence.
- Docker build, live-provider, migration, frontend build/lint, symlink-containment, and rollback-failure injection were not executed during independent verification.
