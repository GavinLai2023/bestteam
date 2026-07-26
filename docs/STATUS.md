# Project status

> **Living doc.** Update **Done** / **In Progress** / **Next steps** when
> you finish or start meaningful work, so this stays a true snapshot of
> "where are we now".

## Done

- SDK core: `Agent`/`Team`/`Workflow`, `EngineAdapter` ABC, `LangGraphAdapter`,
  SEQUENTIAL/PARALLEL/HIERARCHICAL collaboration modes.
- CLI: `init` / `run` / `graph`.
- YAML loader, including `local_folder` (BM25) and `vector` knowledge bases.
- Built-in tools: `web_search`, `parse_file`, `http_get`, `calculator`, and
  the draft-only email toolkit (`email_find`/`email_read`/`email_draft_reply`).
- UI backend: monitoring API, 6-stage builder session state machine, config
  CRUD ("advanced view"), model catalog, usage metering.
- UI frontend: monitoring dashboard, 4-stage Team Builder wizard, login UI.
- Phase 3 auth: per-deployment users, bearer tokens, route protection on
  `/api/builder`, `/api/config`, and the monitoring endpoints.
- Docker packaging for per-customer deployment (`docker-compose.yml`,
  `Dockerfile`s, nginx for the frontend).
- `CLAUDE.md` split into root + per-directory files for progressive
  disclosure.
- Sub-project 2: Agent Skills Library — persistent DB-backed skills via
  `SkillRecord`, `/api/config/skills` CRUD, Solution Architect auto-assignment
  of skills to agents, frontend Skills tab in AdvancedPage.
- HIERARCHICAL mode improvements: manager gets explicit delegation guidance
  injected into system prompt + `tool_choice="required"` on first call so real
  LLMs actually delegate; subordinates with tools also force tool use on first
  call; tool call failures logged as warnings.
- KB-aware builder: Solution Architect told about existing KB records so it
  references them by name; KB tools passed through `validate_specification` at
  every builder stage; KB names validated as tool-safe identifiers.
- BM25 CJK tokenization: bigram fallback for Chinese/Japanese/Korean text.
- "My teams" page (`/teams`): lists resumable builder sessions, sorted by
  most-recently-updated; nav link in sidebar.
- Interview recording upload: consultant uploads audio/video of customer
  interview; Whisper API transcribes it; LLM extracts `intent_text`/`as_is_text`
  to pre-fill IntentPage; files >25 MB split via ffmpeg into 10-min chunks.
- Per-user memory system: `Memory` ABC + `SqliteBM25Memory` store +
  `MemoryManager`, wired end-to-end (JWT user → run → recall/record). Four
  types — working (`_TeamState`), episodic (always-on, $0), semantic +
  procedural (opt-in via `BESTTEAM_MEMORY_MODEL`). Disabled by default
  (`BESTTEAM_MEMORY_DB`); swappable behind the ABC (e.g. future mem0). See
  `src/bestteam/core/CLAUDE.md`.
- Per-user memory review remediation (`docs/MEMORY_REVIEW_TRIAGE.md`) — 13
  findings triaged into SP-1…SP-4. **SP-1** (hardening, PR #30): recall is
  best-effort like record; run-path store closed; `add()` rejects malformed
  `type`. **SP-2** (org multi-tenancy, PR #31): memory carries `org_id`
  (idempotent in-place migration), the run binds its org into `MemoryManager`,
  and org-level compliance erasure (`delete_org_and_legacy`) + account-deletion
  memory purge (`delete-user` fail-closed) + `move-user` legacy reconciliation
  land via the operator CLI/admin API. Deferred to a deletion-lifecycle
  sub-project: in-flight-run drain fence, immutable-principal keying, durable
  memory-store state, historical-legacy-provenance sweep (SP-3 metering/
  observability and SP-4 dedup/retention/recall-bound remain registered).
- Code-review triage remediation: all 17 findings (CR-001…CR-017) resolved
  across PRs #4 and #5 — KB path containment + atomic versioned uploads,
  startup secret-key guard, team-scoped aggregation, terminal-run guarantees,
  `RunRegistry` subscription locking, packaging/entry-point/`.xls`/doc
  corrections, single-use WS tickets, oversized-upload middleware, tool-loop
  exhaustion notice, and up-front `runs`-row persistence. No deferrals remain.
  See `docs/CODE_REVIEW_TRIAGE.md`.
- Code-review round 2 (per-user memory + `http_get`): CR-018…CR-023 verified
  and merged to `main` (PR #7 `d932b40` for CR-018…CR-022, PR #9 `6b9af52` for
  CR-023) — timezone-aware timestamps, `Workflow.run()` best-effort memory
  recording, recalled memory reaching hierarchical delegates, injection-resistant
  recall framing, bounded episodic records (no content/metadata duplication +
  per-field size cap), and `http_get` pinning the connection to the validated IP
  (Host/SNI preserved) so httpx can't re-resolve (DNS-rebinding TOCTOU closed).
  Verified via per-finding TDD regressions, the full suite (335 passed), and
  green CI on `main`. Severities recalibrated to P2/P3 for this codebase's
  disabled-by-default, per-user, no-multi-tenancy model. See
  `docs/CODE_REVIEW_TRIAGE.md` (Round 2).

- Admin role + per-user memory management UI (merged to `main`, PR #12
  `7fd9209`): `is_admin` column (Alembic migration), granted only via the
  `ui.backend.admin` operator CLI (no env/username auto-promotion),
  `get_current_admin` guard now gating **both** the Advanced config page and a
  new **Memory** page. Admins can list users with per-type record counts
  (SQL-aggregated), browse/search a user's episodic/semantic/procedural records
  (bounded response *and* bounded search scan — `search(max_candidates=)`),
  delete individual records, and clear a user's whole memory
  (`ui/backend/memory_api.py`, `/api/memory`; frontend `MemoryPage.jsx` +
  `RequireAdmin`/`useMe`). No manual add/edit; memory stays opt-in
  (`BESTTEAM_MEMORY_DB`).
- Code-review round 3 (admin role + memory-management UI): CR-024…CR-028
  verified and merged via PR #12 — public-registration admin takeover closed
  (operator-CLI-only promotion), pre-migration startup crash removed, fresh-
  install admin path (CLI + docs), `Memory` ABC compatibility preserved
  (management methods concrete-only), and unbounded admin reads bounded (capped
  browse + `max_candidates`-bounded search). CR-029 (unpaginated
  `GET /api/memory/users`) explicitly rejected as YAGNI/out-of-scope (P3) —
  human-scale user count on a no-multi-tenancy tool. Verified via per-finding
  TDD regressions, full suite (356 passed), and frontend build/lint. See
  `docs/CODE_REVIEW_TRIAGE.md` (Round 3).

- Email toolkit (merged to `main`, PR #13 `704797b`): three draft-only
  built-in tools — `email_find` / `email_read` / `email_draft_reply` — over
  one env-configured mailbox; MS Graph (app-only OAuth, `createReply`) and
  generic IMAP (stdlib, `BODY.PEEK`, threaded MIME drafts, UTF-8-literal
  CJK search) backends behind one seam
  (`src/bestteam/tools/email_client.py`, `bestteam[tools-email]`). No send
  verb / no SMTP by design. Seeded `email_triage_reply` built-in Skill.
  Deferred: real sending, attachments. Spec:
  `docs/superpowers/specs/2026-07-15-email-toolkit-design.md`.

- Org multi-tenancy — sub-project 1 (merged to `main`, PR #14): `organizations`
  table + `org_id` row-level isolation across users/agents/teams/KBs/skills/
  workflows/builder sessions/runs/usage; `get_current_org` dependency;
  cross-org access → 404 (WS 4404, no existence oracle); public registration
  REMOVED (operator CLI `create-org`/`create-user`/`list-orgs`); `(org_id, name)`
  uniques; org-scoped loaders + `(org_id, name)` workflow cache; per-org KB
  uploads; admin `/api/config` targets orgs via `?org=`. Also closed two
  pre-existing holes (run GET/WS-stream and builder sessions had no ownership
  checks). Same code serves one-org and many-org deployments. Triage Round 4
  (CR-030…032). Spec: `2026-07-15-org-multi-tenancy-design.md`.

- Advanced page + demo-workflow gate (merged, PR #15): org selector (fixes the
  Save 422), read-only Tools tab, removed the dead Agents/Teams config API +
  tabs, renamed the workflows tab AI Teams → Workflows, CSS overlap fix;
  `GET /api/config/orgs` + `/tools`; shipped demo workflows gated **off by
  default** (`BESTTEAM_DEMO_WORKFLOWS`) so they don't leak into customers'
  dropdowns.

- Email smoke-test + triage rule (merged, PR #16): `docs/email-smoke-test.md`
  runbook; the `email_triage_reply` bulk-mail rule keys off agent-visible
  signals (no-reply sender, unsubscribe wording), not headers the backends
  don't expose to the agent.

- Per-org email credentials — foundation (merged to `main`, PR #17 `0dd8b44`):
  encrypted per-org mailbox store (`org_email_credentials`, Fernet under
  `BESTTEAM_SECRETS_KEY`, separate from the JWT key); `email_tools.load_email_tools`
  resolves the running org's mailbox (overrides the env tools by name; per-org
  workflow cache keyed on `OrgEmailCredential` freshness); `admin
  set-email`/`clear-email` CLI; startup refuses to boot if stored credentials
  can't be decrypted (operator CLI stays usable for recovery). The env
  `BESTTEAM_EMAIL_*` path stays single-org and is still refused on multi-org.
  Spec: `2026-07-18-per-org-email-credentials-design.md`.

- Self-service mailbox connection in the Team Builder wizard (merged to `main`,
  PR #18 `e04b3b4`): customers connect/test/rotate/disconnect their own IMAP
  mailbox inside the wizard, shown only when the built team uses email
  (`spec_uses_email` resolves each agent's tools + skills) — soft at Preview
  (test against the real inbox), hard-gated at Deploy (backend refuses to deploy
  an email team without a mailbox). `/api/org/email` endpoints guarded by the
  org's own login; SSRF guard on the customer-supplied host. Deliberately built
  **without** a per-org admin role: **one member per org is enforced** instead
  (partial unique index + non-destructive migration audit + ASGI startup guard;
  `admin delete-user`/`move-user` recovery). Also hardened the IMAP transport
  (verified TLS, bounded timeouts, connect-time IP pinning vs DNS rebinding) and
  bounded the IMAP port. Three code-review rounds (12 findings) resolved; 500
  tests, green CI. Spec: `2026-07-18-wizard-email-connect-design.md`.

- Autonomous email-triggered runs (feature/email-trigger-autonomous-runs):
  customers opt in at Deploy ("Run automatically when new email arrives") and
  the platform polls their mailbox (default 120s) and runs their deployed
  email team on new mail — no prompt. Per-org UID-baseline dedup (backlog
  never triggers), one run per cycle, daily cap (default 50) with midnight
  reset, operator kill switch (`BESTTEAM_TRIGGERS_DISABLED`), overlap guard,
  activity list on "My teams" from persisted `runs` rows (sentinel username
  `email-trigger`). Spec: `2026-07-19-email-trigger-autonomous-runs-design.md`.

- Autonomous email-trigger correctness fixes: runs are hard-confined to the
  poller-detected UID batch (scoped tools + uncached per-run workflow), bounded
  by `BESTTEAM_TRIGGER_BATCH_SIZE` with carry-over; state advances only through
  a durable run; workflow faults persist across empty polls; mailbox
  change/disconnect disables the trigger (rotation keeps it). Also: per-org poll
  rollback, server-side autonomous activity filter, reserved sentinel username.
  Spec: `2026-07-20-email-trigger-correctness-redesign-design.md`. Remaining P2
  hardening (env validation, shutdown thread-stop, run-source enum, RunRegistry
  eviction) tracked in Known issues.

- Autonomous email-trigger hardening round 2 (independent-reviewer follow-up
  on PR #22): mailbox-replacement race closed (one IMAP backend resolved per
  poll cycle, threaded through to workflow-building instead of re-fetched);
  operator CLI (`admin set-email`/`clear-email`) now disables the trigger on
  mailbox change/disconnect too, matching the wizard path; dispatch-
  submission failures mark the run failed instead of wedging the overlap
  guard; mailbox connectivity errors and workflow/dispatch errors are
  tracked separately (`last_error_kind`) so a resolved mailbox outage clears
  instead of showing "error" forever; `BESTTEAM_TRIGGER_*` env values
  validated at startup (fail-fast, matching the `BESTTEAM_SECRET_KEY` guard);
  "My teams" activity card distinguishes a failed status/activity fetch from
  "off" or "no runs yet" and now shows `last_checked_at`. `RunRegistry`
  eviction and shutdown thread-stop remain deferred (see Known issues).

- Autonomous email-trigger concurrency fix + `uses_email` list correction
  (merged to `main`, PR #24 `1cde55f`): the poller's run-advance is now a
  compare-and-swap (`UPDATE email_triggers ... WHERE enabled = 1`), so a
  disable or mailbox replacement landing between the enabled-check and the
  commit matches no row and the built run is discarded (`registry.discard`)
  instead of dispatched against a just-disconnected mailbox — closing the
  read-then-commit window a separate `refresh` left open (password rotation
  keeps the same identity and is unaffected). Separately, the builder-session
  *list* endpoint now passes db/org context to `_session_to_dict`, so
  `uses_email` resolves per session instead of always reporting false
  (detail/mutation responses were already correct). TDD regressions for both.

- Bounded `RunRegistry` eviction: a third-round independent review
  re-raised the RunRegistry unbounded-growth finding (previously deferred
  twice) on the basis that the autonomous trigger removes the
  human-rate-limiter that made those deferrals safe -- unattended,
  continuous run creation vs. click-driven. `create()` now evicts the
  oldest terminal, subscriber-free runs once the registry exceeds
  `_MAX_RETAINED_RUNS` (1000, hardcoded). Spec:
  `2026-07-22-run-registry-bounded-eviction-design.md`.

- Wizard model-catalog access fix (merged to `main`, PR #20
  `fix/wizard-model-catalog-access`, commits `4d1e39b`+`78f1d82`): non-admin
  org users hit "Model call failed: with_structured_output is not implemented"
  on "start building my team" because the model catalog was served only from
  the admin-only endpoint — the wizard's fetch 403'd, silently fell back to a
  `fake:ok` model, and structured-output generation then crashed. Fix: a
  non-admin `GET /api/model-catalog` read endpoint (`crud.public_router`, any
  authenticated org member) with the frontend repointed to it, plus the
  underlying `NotImplementedError` from a `fake:` model translated to a clear
  `ConfigurationError` ("needs a real AI model that can produce structured
  output…") in `requirements.py`/`specification.py` so a misconfigured model
  never surfaces the cryptic message again. TDD (5 tests); 505 backend tests +
  frontend lint/build green. NOTE: the wizard now reaches a real model, which
  still needs a valid provider API key on the instance to actually generate.

  Follow-up (commit `302cf12`, same PR): independent review found that the
  catalog fetch's own loading/failure states were never gated on -- the Start
  button (and the interview-upload path) stayed clickable while the catalog
  was still loading or had failed to fetch, silently falling through
  `pickDefaultModel([])` to `fake:ok` and hitting the same "unsupported
  model" 400 with no visible model picker, no retry, and no recovery short
  of a page reload. `useModelCatalog` now exposes `failed`/`retry`;
  `IntentPage` disables generation while loading, failed, or genuinely empty
  (no real model configured yet), with a retry banner in each case.

- Wizard mailbox-connect UX fix (PR #21 `fix/wizard-email-connect-ux`,
  **open / CI green, pending merge**): raw socket/OS errors (e.g.
  `[WinError 10060] ...`) surfaced verbatim to non-technical customers
  testing/saving a mailbox connection. `_friendly_connect_error` now maps
  timeout/refusal/login/DNS failures to plain-language, actionable
  messages; a missing `BESTTEAM_SECRETS_KEY` or other unexpected save
  failure is logged server-side and reported to the customer as "contact
  your administrator" (503/500), never the raw exception. Port/drafts
  fields moved under an "Advanced settings" disclosure to prevent
  scroll-wheel port mistakes.

  Follow-up (commit `a88aff4`, same PR): independent review found
  `check_host_allowed()` (the shared SSRF guard) still embedded the raw OS
  resolver exception and the resolved private/internal IP directly into its
  `ConfigurationError` messages, which reach the customer via two paths
  `_friendly_connect_error` doesn't cover — the preliminary host-validation
  check (`_reject_private_host`, outside `_friendly_connect_error` entirely)
  and its own non-login `ConfigurationError` passthrough branch. Sanitized
  at the source in `http_client.py`: the customer-supplied hostname stays in
  the message, the raw exception text and resolved IP don't.

- Data architecture review triage (`docs/DATA_ARCHITECTURE_REVIEW_REPORT.md`,
  35 findings; disposition in `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`):
  the report proposes a full commercial-SaaS rearchitecture (~25 new entity
  types, RBAC, a generic tool-connection framework, PostgreSQL) framed as
  multi-week Phase 1/Phase 2 program work, not a single review cycle.
  Implemented the two findings that were genuine, narrow, low-risk defects:
  deployment is now a single atomic transaction (`WorkflowRecord` write and
  the BuilderSession status update share one commit, P1-14), and the
  vestigial `AgentRecord`/`TeamRecord` tables (no current reader or writer)
  were dropped (migration `57b13700d5df`, P1-09).
  A follow-up code review (PR #23) hardened both against data-safety edge
  cases: (F1) writable `/api/config/agents`/`teams` routes existed historically
  (`78c7a8a`..`036e1d6`), so the drop migration now **refuses** (with export
  guidance) if either table holds rows and drops only when empty — a drop is
  not data-reversible; (F2) `db_session` runs `create_all` at import and the
  docs have the operator start the backend before `alembic upgrade head`, so
  the baseline (`884e80106da7`) and add-`is_admin` (`a1b2c3d4e5f6`) migrations
  are now inspection-guarded like the later ones, making that documented
  sequence idempotent instead of failing with "table … already exists".
  Covered by `tests/test_migrations.py` (real alembic upgrade runs).
  Explicitly did **not** implement SQLite foreign-key enforcement (P1-13),
  trace persistence / crash recovery (P1-16/P1-17), or one-member-per-org
  removal (P2-01/P2-02) — each restates a tradeoff already made deliberately
  elsewhere in this project, with its own documented rationale; see the
  triage doc for citations. The remaining 29 findings were spot-checked as
  accurate but out of scope for a narrow fix pass — each needs its own
  brainstorm → spec → plan cycle as a future sub-project.

- Data architecture review triage, second pass — "deploy is the gate"
  (P1-06 + P1-11): P1-06, workflow lifecycle status was decorative
  (`_get_workflow`/`GET /api/workflows` ignored `status`, so a draft could
  run as production) — only `status="deployed"` `WorkflowRecord`s are now
  resolved/listed; `workflows.status` is CHECK-constrained to `('draft',
  'ready_for_testing', 'deployed')` (migration `b1d7e4f2a9c8`, guarded/
  idempotent, with existing non-`deployed` rows backfilled to `deployed` so
  upgrade doesn't retroactively hide previously-runnable workflows); an
  operator save via `/api/config/workflows` now writes `status="deployed"`
  (save = deploy, matching the wizard's `deploy_session`) — `/api/config/workflows`
  (admin CRUD list) stays unfiltered by design. P1-11, deploy-time validation
  didn't check agent model specs against what the platform actually offers —
  `deploy_validation.validate_agent_models()` now checks every agent model
  against the model catalog (`fake:` exempt) at both deploy points
  (`builder.py::deploy_session`, `crud.py::upsert_workflow_config`), 400ing
  with the full list of unknown models. TDD regressions: `tests/test_deploy_validation.py`
  (the helper), `tests/test_crud_api.py::test_only_deployed_workflows_are_listed_and_runnable`
  / `test_workflow_put_rejects_agent_model_not_in_catalog`, `tests/test_builder_api.py::test_deploy_rejects_agent_model_not_in_catalog`
  / `test_deployed_workflow_can_be_run_via_get_workflow`, and `tests/test_migrations.py::test_existing_non_deployed_workflow_backfilled_to_deployed`
  / `test_status_check_rejects_invalid_value`. Spec:
  `docs/superpowers/specs/2026-07-24-deploy-is-the-gate-design.md`. See
  `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md` ("Implemented this pass").
  Post-merge review hardening (3 P1 findings): (F1) `validate_agent_models`
  now rejects an agent whose model is missing/`None`/empty/non-string — the
  operator CRUD path builds `Agent(**spec)` directly (`Agent.model` is
  `ModelSpec | None`), so such a model previously deployed and failed at first
  run; a non-string model also crashed the check (`42.startswith` → 500). (F3)
  `build_trigger_workflow` now filters `status="deployed"`, extending the gate
  to the autonomous poller (it queried by name+org only). (F2) Model validation
  is deploy-time only by design — a catalog-removed or legacy model fails at
  run, not load; documented as a known limitation (load-time re-validation
  deliberately out of scope).

- Data architecture review triage, third pass — dependency-namespace
  integrity (P1-07 + P1-08): P1-07, deleting a skill/KB via
  `/api/config/{skills,knowledge_bases}` orphaned any deployed workflow still
  referencing it — `crud._deployed_workflows_referencing(db, org_id, kind,
  name)` scanned `status="deployed"` `WorkflowRecord`s' `agents[*].skills`
  (skill) / `agents[*].tools` (standalone KB) and `delete_item` 409s naming
  the referencing team(s), checked before any deletion or KB `rmtree`
  (platform skills, `org_id is None`, are checked across all orgs).
  **Superseded by P1-04** (see the P1-04 entry below): `_deployed_workflows_referencing`
  was removed; the guard now queries typed `workflow_dependencies` rows via
  `db/dependencies.py::workflows_referencing(db, kind=, resource_id=)`.
  P1-08, every tool resolved through one flat name lookup so a KB could
  silently shadow a built-in tool — `deploy_validation.find_kb_tool_collisions`
  (pure) plus `knowledge_bases.kb_name_collisions(db, org_id, raw_spec)`
  (resolves referenced standalone KB names, calls the pure helper with
  `set(bestteam.tools.REGISTRY)`) are now called name-only, before any KB
  build, at both deploy points (`builder.py::deploy_session`,
  `crud.py::upsert_workflow_config`), 400ing on a collision; scoped to
  collision detection, not the reviewer's full typed-namespace rename, and
  only KB names are checked so the per-org email-tool override never
  false-positives. TDD regressions: `tests/test_deploy_validation.py::test_inline_kb_name_shadowing_builtin_flagged`
  / `test_standalone_kb_name_shadowing_builtin_flagged` / `test_non_colliding_kb_names_pass`
  / `test_collisions_sorted_and_deduped` (the pure helper); `tests/test_crud_api.py::test_delete_skill_referenced_by_deployed_workflow_is_409`
  / `test_delete_kb_referenced_by_deployed_workflow_is_409` / `test_delete_unreferenced_skill_still_204`
  / `test_workflow_put_rejects_kb_named_after_builtin`, `tests/test_org_settings.py::test_deploy_rejects_kb_named_after_builtin`.
  Spec: `docs/superpowers/specs/2026-07-24-dependency-namespace-integrity-design.md`.
  See `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md` ("Implemented this pass").
  Post-review hardening (6 findings): (F1) a KB also can't be *created* with a
  built-in tool name (KB PUT + upload), closing a post-deploy shadow bypass;
  (F4) seeded platform built-in skills are undeletable (bundled YAML demos may
  depend on them); (F6) the delete reference-scan skips malformed workflows
  instead of 500-ing; (F3) KB delete commits before `rmtree` and logs rmtree
  failures. A second review round closed further gaps: the KB delete now holds
  the per-KB lock across delete+commit+rmtree (concurrent-upload race); the
  delete scan matches dict-shaped `tools`/`skills` (the loader normalizes them);
  `load_knowledge_base_tools` fails closed on a legacy KB shadowing a built-in;
  and a process-wide `component_mutation_lock` serializes component-delete
  against deploy (the delete/deploy TOCTOU — feasible even single-worker via the
  threadpool — is now mitigated, not "near-impossible" as first stated).
  Two further review rounds closed the same classes comprehensively: the delete
  scan now uses the loader's own `list(refs)` normalization (any list/dict/string
  shape, not special-cases); the inline-KB path also fails closed at load
  (`core/loader._build_workflow`, covering SDK/manual/autonomous); and the KB
  upload holds its lock across staging→validation→promote→commit so it fully
  serializes with delete. Remaining, deferred to P1-04 (typed dependency
  records): raw-name matching can over-block (fail-closed, safe), and
  upload-file cleanup is best-effort. Delivered via PR #27 over four review
  rounds; spec: `2026-07-24-dependency-namespace-integrity-design.md`.
- Data architecture review triage, fourth pass — versioning keystone
  (P1-01 + P1-02 + P1-03): a deployed team had no stable identity and no
  version history — `WorkflowRecord` was keyed `(org_id, name)` and every
  deploy overwrote `config` in place, so prior configs were lost, two
  builder sessions with the same name silently clobbered one row, and a
  `Run` recorded only the workflow name (not the config it ran).
  `WorkflowRecord` is now the stable team head with an immutable
  `workflow_versions` child table (`WorkflowVersion`; `(workflow_id,
  version_number)` unique); deploy calls
  `db/workflows.py::publish_workflow_version` — append a new immutable
  version, move `current_version_id`, keep `config` as the current mirror —
  at both deploy points (`builder.py::deploy_session`,
  `crud.py::upsert_workflow_config`), replacing the in-place overwrite.
  `deploy_session` links `BuilderSession.workflow_id` to the head in the
  same commit (P1-14 atomicity preserved), so a redeploy bumps a version
  under the same head and two sessions with the same name converge on one
  head (P1-02, first config preserved as v1). Each production `Run` is
  stamped with `workflow_version_id` (`current_version_id(db, org_id, name)`
  resolved in `create_run` and the email trigger); sandbox test-runs stay
  NULL. Migration `c3f5a1b8e2d4` (guarded/idempotent) creates the table +
  columns and backfills one v1 per existing workflow. **Scope:** freezes the
  inline config blob + run linkage only — standalone Skills/KBs/models are
  still resolved by name at load, so behavioral drift (P1-05) and a
  fully-resolved dependency snapshot remain deferred to P1-04 (typed
  dependency records); version-history/rollback UI and rollback execution
  are also deferred. Spec:
  `docs/superpowers/specs/2026-07-25-versioning-keystone-design.md`. See
  `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md` ("Implemented this pass").
  Hardened via two external-review rounds: BOTH run paths record the version
  from the same record read that builds the config — the manual path
  (`_resolve_workflow_and_version`) and the autonomous poller
  (`build_trigger_workflow` returns `(workflow, version_id)`) — so a run can't
  record a version it didn't execute under a concurrent redeploy; workflow
  deletion refuses (409) while any run records one of the head's versions
  (preserving provenance) and otherwise cascades its version history under the
  mutation lock (no orphans) and nulling any builder session that pointed at the
  head; and the migration creates `created_at` NOT NULL to match the ORM.
  Remaining known limitations (design spec): deleting a workflow at the instant a
  run of it starts can dangle that in-flight run's provenance pointer (the run's
  row is written by the worker after dispatch) -- closed only by soft-delete/
  archive, deferred to a deletion-lifecycle sub-project; rename-onto-existing-name
  is a 500; and FK constraints on the new columns follow the project's bare-column
  precedent (SQLite FK enforcement off, P1-13).
- Data architecture review triage, fifth pass — typed dependency records
  (P1-04: skills + KBs): a new `workflow_dependencies` table (`WorkflowDependency`)
  records one typed row per (published workflow version, skill|standalone-KB)
  it depends on (`resource_kind`, `resource_name`, resolved `resource_id`),
  populated once at deploy in `db/workflows.py::publish_workflow_version` via
  `db/dependencies.py::record_version_dependencies` (resolves names exactly as
  the loader: org skill shadows platform built-in; KBs org-scoped; built-in
  tool / email tool / inline KB is not a KB dep). The skill/KB `DELETE` guard
  is rewired to `workflows_referencing(db, kind=, resource_id=item.id)`,
  querying these rows for each workflow's current version instead of scanning
  deployed workflows' JSON — non-regressing, and the stable id makes the
  platform-built-in-skill cross-org case fall out with no all-orgs scan.
  Migration `d4e6b2c9f1a7` creates the table and backfills each workflow's
  current version. **Scope:** skills and standalone KBs only; model/tool
  dependency rows and skill/KB content pinning to freeze behavior (P1-05) stay
  deferred. See `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md` ("Implemented this
  pass").

## In Progress

- _Nothing actively in progress._ See "Next steps / roadmap" below.

## Known issues / tech debt

- **Vector knowledge base retrieval is single-stage** — no query
  rewriting/expansion or reranking, no external vector store, no DMS
  connectors. See `src/bestteam/core/CLAUDE.md`.
- **Per-user memory recall is single-stage BM25** — no rerank/expansion;
  semantic/procedural records have no auto-dedup. Admin view/search/delete UI
  exists (`/api/memory`), but there's no manual add/edit and no retention/quota
  policy. `GET /api/memory/users` is unpaginated (CR-029, deferred P3): fine
  today (admin-only, opt-in, operator-provisioned accounts), but the
  shared-platform ceiling is the sum of memory-enabled users across all orgs
  — add a limit/cursor if a customer reaches ~hundreds. See `core/memory.py`.
  Memory is now org-scoped (SP-2), but a **deletion-lifecycle** gap remains: an
  in-flight run can record memory *after* an account/org purge (no cross-process
  drain fence), and pre-SP-2 legacy rows with no recorded provenance need a
  one-time operator sweep — both tracked in `docs/MEMORY_REVIEW_TRIAGE.md`.
- **`RunRegistry` remains the in-memory live layer** — a `runs` row is now
  persisted per run (CR-012) so usage/trace foreign keys are valid, but
  `trace_events` persistence, restart recovery, and a run-history API remain
  Phase 5. Growth is now bounded (terminal-run eviction, see Done above) —
  what's left is that a restart still loses all live trace-event history,
  not that it grows unbounded. See `ui/backend/db/CLAUDE.md`.
- **No general-purpose cache layer** — only local per-process caches
  (`_workflow_cache`, `Workflow._compiled`). See `ui/backend/CLAUDE.md`.
- **`ui/frontend/CLAUDE.md`'s wizard section describes the old 6-stage
  wizard** (`/wizard/:sessionId/{requirements|team|refine|test|deploy}`),
  not the current 4-stage flow introduced in commit `0d2490a` (flagged in
  that file already).
- **Hard-restart orphans** — `runs` rows left `status="running"` by a killed
  process are never swept. The email trigger's overlap guard now consults
  the in-process registry instead of that row, so the trigger self-recovers
  and doesn't wedge, but the activity list can still show a stale "running"
  run. A startup sweep is future work.
- **Autonomous trigger residuals:** `asyncio.to_thread` poll cycles aren't
  awaited on shutdown, so a mailbox check/commit/dispatch already in flight
  can keep running briefly after the ASGI shutdown handler returns; a process
  killed between a trigger's state commit and dispatch orphans a `runs` row
  (overlap guard self-recovers on restart; no reconciliation sweep yet).

## Next steps / roadmap

- **Deployment model — RESOLVED 2026-07-16:** the shared-hosted-platform
  question (raised 2026-07-15) was decided in favour of **org-scoped
  multi-tenancy, one codebase for both models** — see `DECISIONS.md`
  (supersedes "per-customer instance, no multi-tenancy"). Program status:
  sub-project 1 (org isolation, PR #14), the per-org **email** secrets store
  (PR #17), and customer self-service mailbox connection in the wizard
  (PR #18) are done. Remaining: a **per-org admin role** (which would lift the
  interim one-member-per-org constraint — see `DECISIONS.md`), per-org LLM
  credentials, per-org Microsoft Graph / OAuth ("connect your inbox"), an
  in-place secrets-key rekey command, and infra hardening / Postgres when real
  usage numbers demand it.
- CrewAI adapter, DEBATE collaboration mode, deployment templates — all
  "planned, not started" (see `DECISIONS.md` for why CrewAI isn't the
  current engine).
- Phase 5: wire `RunRegistry` to persistent `runs`/`trace_events`.
- Phase 6: multi-customer update-distribution strategy (see
  `team_builder_methodology.md`).
- Refresh `ui/frontend/CLAUDE.md` to describe the current 4-stage wizard.
