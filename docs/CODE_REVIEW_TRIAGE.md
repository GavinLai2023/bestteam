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
| CR-008 | KB upload replacement is non-atomic and has destructive rollback | Confirmed | Agree | Versioned-dir + atomic `CURRENT` pointer keeps a complete prior version through commit; a per-KB lock now serialises the promotion/commit/cleanup so simultaneous same-KB uploads can't leave CURRENT dangling (unique temp file too). Rollback, concurrent-reader, and per-KB-serialisation regressions pass | Accept fix | Fixed - awaiting independent verification |
| CR-009 | Interview upload and ffmpeg processing are unbounded | Confirmed | Agree | ASGI body limiter rejects declared, chunked, and understated oversized uploads before multipart parsing; ffmpeg remains time-bounded | Accept fix | Verified |
| CR-010 | RunRegistry subscription can miss a terminal event | Confirmed | Agree | Append, replay, and subscription registration share one lock | Accept fix | Verified |
| CR-011 | Tool-loop exhaustion completes successfully with empty output | Confirmed behavior; existing test asserts intentional empty-success | Agree it needs a product decision | No contract change; empty-success behavior remains | Retain product decision | Deferred |
| CR-012 | Usage rows reference runs that are never persisted | Confirmed; runs are RunRegistry-only | Agree | No persistent-run-state implementation; usage FK inconsistency remains | Retain Phase 5 dependency | Deferred |
| CR-013 | Long-lived bearer token is stored in localStorage and placed in the WebSocket URL | Confirmed | Agree | Single-use, short-lived WS tickets replace URL bearer tokens; ticket store is locked | Accept WS-ticket fix; localStorage migration remains deferred | Verified |
| CR-014 | The aggregate tools extra omits the dependency required by http_get | Confirmed | Agree | `httpx` is included in HTTP and aggregate tools extras | Accept fix | Verified |
| CR-015 | Documented python -m bestteam entry point is missing | Confirmed | Agree | Package module entry point delegates to the existing Typer app | Accept fix | Verified |
| CR-016 | Legacy .xls support is advertised but not implemented | Confirmed (openpyxl can't read .xls) | Agree | `.xls` removed from parser, KB discovery, and documentation | Accept fix | Verified |
| CR-017 | README and marketing claims drift from current implementation | Confirmed | Agree | Vector-KB, test-count, and streaming/retention wording now match implementation | Accept fix | Verified |

## Post-fix independent verification

**Overall verdict: Verified within the accepted scope.** Follow-up remediation and regression checks have verified 15 accepted findings. CR-011 and CR-012 remain intentional deferrals pending their documented product and persistent-state decisions.

- **CR-001:** `ui/backend/knowledge_bases.py` now resolves cache targets against the backend-owned `_kb_cache` directory; `ui/backend/builder.py` applies that guard to submitted, persisted, and model-generated candidates before SDK validation can construct a vector KB.
- **CR-003 / CR-010 / CR-013:** `ui/backend/runtime.py:78`, `src/bestteam/core/workflow.py:119`, `ui/backend/registry.py:54`, and `ui/backend/ws_tickets.py:28` provide the terminal-event, subscription-lock, and ticket-lock guarantees covered by the new regression tests.
- **CR-005 / CR-008 / CR-009:** generation-aware workflow-cache invalidation, the atomic KB `CURRENT` pointer layout, and the ASGI streamed-body limiter are covered by focused deletion, rollback, concurrent-reader, and chunked-upload regressions.
- **CR-002 / CR-004 / CR-006 / CR-007 / CR-014 / CR-015 / CR-016 / CR-017:** the startup secret guard, team-scoped aggregation, image/package declarations, CLI entry point, `.xls` removal, and documentation changes are present and consistent with their regression coverage.
- **CR-011 / CR-012:** no behavior change was introduced; their documented product and persistent-run-state deferrals remain valid.

### CR-001 and CR-009 follow-up verification (2026-07-14)

- **CR-001 — Verified:** model-generated candidates are pre-validated before `validate_specification()` constructs a KB; API, persisted, and inline configs resolve their cache target under the backend-owned `_kb_cache` directory, rejecting symlink/junction escapes.
- **CR-009 — Verified:** the ASGI limiter counts every incoming body chunk and emits 413 before the downstream multipart parser receives an over-limit chunk, including when `Content-Length` is absent or understated. A reverse-proxy limit remains useful operational defense in depth.

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
