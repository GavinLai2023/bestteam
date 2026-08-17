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
  land via the operator CLI/admin API. **SP-3** (instrumentation, PR #32):
  extraction spend metered (`agent="memory:extraction"`, billed even on total
  write failure), run/version provenance in each record's `metadata`, and
  `memory_recalled`/`memory_recorded`/`memory_failed` TraceEvents; recording runs
  after the terminal event so a hung extraction can't wedge a run; `run()` reaches
  parity via `WorkflowResult.memory`/`.recall`. **SP-4** (quality & scale, PR #33):
  recall bounded to the most-recent N (`BESTTEAM_MEMORY_RECALL_MAX_CANDIDATES`,
  default 1000) + covering indexes; atomic per-type exact dedup on write
  (`add_if_absent`); and opt-in episodic retention cap
  (`BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER`, `SqliteBM25Memory.prune_user_type`).
  All four memory sub-projects are now done.
  Deletion-lifecycle sub-project (immutable-principal keying + in-flight-run
  write-fence) is now done — see below. Deferred within SP-4 (documented,
  disproportionate for a BM25/opt-in store): embedding/LLM near-dup + conflict
  resolution + consolidation, age-based TTL, per-org quotas, background sweep.
- Deletion-lifecycle sub-project (merged to `main`, PR #42 `d82fdba` —
  the integration commit combining PR #40 `fix/memory-legacy-scope` (MEM-14)
  with PR #41 `feat/memory-principal-lifecycle`, whose independent
  `SqliteBM25Memory.all()`/`search()` changes conflicted and couldn't both
  land as separate merges): closes deletion-lifecycle findings 1 & 2.
  Immutable `users.principal_id` (set once at creation, never rotated —
  unlike `security_stamp`, which rotates on password reset) stamps every
  memory record, so a deleted-then-recreated same-`(org, username)` account
  gets a new principal and can't recall the old one's rows (finding 1).
  Account deletion retires the principal into a `retired_principals` table;
  the retirement check is folded atomically into the write itself
  (`INSERT ... WHERE NOT EXISTS`, not a separate pre-check), so an in-flight
  run finishing after the purge can't re-create rows behind it (finding 2) —
  no run-drain fence or distributed lock needed, since the shared SQLite
  file is the cross-process coordination point.
  `retire_and_delete_user` retires + purges in one store transaction
  (rollback on failure), and a fenced/deduped write reports as unrecorded
  rather than falsely audited as persisted. MEM-14 (admin "legacy (no org)"
  scope was ambiguous) also lands here: `?org=legacy` reads only NULL-org
  rows, distinct from `?org=` omitted (all orgs). The integration PR adds
  regression coverage combining both scopes (org `all`/`legacy`/concrete ×
  principal admin-unfiltered/concrete, multiple principals per org). Full
  suite: 819 passed. Deferred (documented, disproportionate): durable
  authoritative memory-store state, a one-time historical-legacy-provenance
  sweep. Specs: `2026-07-30-memory-principal-lifecycle-design.md`;
  `docs/MEMORY_REVIEW_TRIAGE.md`.
- Semantic near-duplicate/update resolution (closes the M-08 gap SP-4 left
  open, for `semantic` records): extraction is shown the user's existing
  semantic memories as candidates and each extracted fact now carries an
  `action` (`add`/`update`/`noop`) instead of always appending, so a
  changed/corrected preference replaces the old record instead of
  accumulating alongside it. `procedural` consolidation and cross-run
  concurrency remain deferred. See `src/bestteam/core/CLAUDE.md`,
  `docs/MEMORY_REVIEW_TRIAGE.md`.
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
  columns and backfills one v1 per existing workflow. The later skill-version
  work freezes referenced skill content as well; standalone KBs/models remain
  name-resolved. Workflow version-history/rollback UI and rollback execution
  are still deferred. Spec:
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
  current version. Skill dependencies now additionally pin an immutable
  `skill_versions.id` (migration `c4d5e6f7a8b9`); edits and org overrides
  affect a team only after redeploy. Standalone-KB/model/tool content pinning
  stays deferred. Post-review hardening makes deployed `uses_email` metadata
  resolve pinned skills (including Advanced-deployed synthetic team cards),
  moves the mailbox gate into the deploy/version lock, enforces fresh/upgrade
  FK parity with interrupted-column repair, and sorts the combined My Teams
  payload most-recent-first. See `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md` ("Implemented this
  pass").
- Frontend hardening + role-aware routing (merged to `main`, PR #34 `d86e966`,
  PR #37 `78fa013`): `lib/api.js` now carries the HTTP status
  on thrown errors so a 403 no longer renders as "backend unreachable"; added the
  frontend's first JS test harness (vitest + jsdom + testing-library). A new
  `RequireOrgMember` guard (mirror of `RequireAdmin`) routes platform operators
  (`is_admin`, `org_id NULL`) to their admin home `/advanced` instead of the
  org-scoped customer pages that 403 them, and `Layout` shows customer vs admin
  nav links by role. Specs:
  `docs/superpowers/specs/2026-07-27-platform-operator-routing-design.md`.
- Admin org/user management UI + org deactivation (merged to `main`, PR #36
  `d1ee951`): new `/api/admin` router + `/accounts` page for
  everyday provisioning — list/create orgs, deactivate/reactivate (reversible
  full suspend), and create/reset-password/move/delete each org's member;
  platform accounts shown read-only (promote/demote + operator/admin lifecycle
  stay CLI-only). **Org deactivation** (`organizations.active`) is enforced at
  login, in `get_current_user` (every authenticated route), and in the
  email-trigger enumeration **and** dispatch CAS. **Session security**: a random
  per-account `security_stamp` in every token + WS ticket (regenerated on
  password reset, fresh on account creation) so resets revoke sessions and a
  recreated username can't inherit the deleted account's credentials. Identifier
  grammar (URL-safe, no `.`/`..`) enforced server-side. Hardened across three
  external-review rounds. Spec:
  `docs/superpowers/specs/2026-07-27-admin-org-user-management-design.md`.
- CI maintenance (merged to `main`, PR #38 `483dd59`): bumped
  `actions/checkout` + `actions/setup-node` to v5 and `actions/setup-python` to
  v6, clearing the GitHub Actions Node.js-20 runtime deprecation warning.

- Runtime monitoring redesign — granular trace events, run history/cancellation,
  four-page nav: the dashboard nav is now Build a team / My teams / Run a team
  (renamed from "Talk to your team") / Activity; My teams drops the embedded
  Automatic Runs history for a one-line automation status tag; the new Activity
  page has an Automations tab (unchanged) and a Runs tab (manual + automatic
  history, filterable, opens a run's persisted or live trace). Backend: the
  LangGraph adapter now emits granular per-node events (`agent_started`,
  `tool_started`/`tool_completed` with a truncated business-safe summary,
  `agent_progress`, HIERARCHICAL `delegation_*`/`subagent_*`) buffered per node
  and flushed before that node's `agent_completed`; every event is now persisted
  (`trace_events` table, `seq`-ordered) alongside a `run_queued` bookend
  published to the live registry too (not DB-only); `GET /api/runs` (filterable,
  paginated) and `GET /api/runs/{id}/trace` serve history; `POST
  /api/runs/{id}/cancel` adds real cooperative cancellation (a per-run
  `threading.Event` checked between yielded events, skipped only for a node's
  own already-paid-for buffered events so cancelling mid-node can't drop that
  node's usage from `usage_records`, but still honored immediately at every
  other checkpoint including before the first agent starts). Frontend: running
  timer/connection-status/waiting-hint/stale-run banner + a race-safe Stop
  button on the monitor page; the Activity page's Runs tab polls every 5s while
  a row is still shown running (stale-filter-safe). See `ui/backend/CLAUDE.md`
  ("Granular trace events, cancellation, and run history") and
  `ui/backend/db/CLAUDE.md` ("Run persistence and history API"). Two rounds of
  independent review closed 9 findings total (usage-preserving + promptly-honored
  cancellation, UTC-qualified timestamps, NULL-username filter correctness,
  bounded `/api/runs` pagination, stale-status polling, the Stop-button race,
  and the live/historical `run_queued` mismatch) — all via TDD regressions; 847
  backend + 40 frontend tests green, eslint clean. **Known gap** (accepted
  tradeoff, not yet built): pagination's default 50-row page has no frontend
  "load more"/pager yet, so an org with 50+ matching runs can't reach older
  ones from the Activity page today — see Known issues below.

- Property Maintenance Inbox — Release 1A (`feat/property-maintenance-inbox-phase1`):
  a vertical solution template on top of the existing multi-agent platform,
  not a new `Case`/work-item subsystem. Three versioned platform Skills
  (`email_input_security_core_v1`, `property_maintenance_intake_v1`,
  `property_maintenance_response_v1`) + a two-agent SEQUENTIAL demo template
  (`ui/backend/workflows/property_maintenance_inbox_demo.yaml`) wire the
  Intake Analyst (email_find/email_read only) to the Response Coordinator
  (email_draft_reply only, draft-only — nothing in this vertical can send).
  A new `automation_item_results` table (`ui/backend/automation_results.py`)
  is populated server-side after a triggered run completes: the Response
  Coordinator's final message is parsed as a JSON envelope, validated against
  Pydantic models, and reconciled against the run's own `trigger_context`
  (the poller-detected UID batch) — never trusting the model for
  org/run/source identity. Every UID in the batch gets exactly one
  (`run_id`, `source_key`)-unique row, including a synthesized `error`/
  `needs_attention` row for a missing/invalid/out-of-batch item, so nothing
  silently disappears; `possible_emergency`/`unknown` priority is
  server-enforced to always need a human, regardless of what the model itself
  claimed. `Run.trigger_context`/`retry_of_run_id` (migration `c1d2e3f4a5b6`)
  let a failed/errored triggered run be safely retried
  (`POST /api/runs/{id}/retry`, revalidates UIDVALIDITY + mailbox + daily cap
  before rebuilding — always a new run, history stays immutable). New
  org-scoped `GET /api/automation-results` (+ `/summary`) back a Maintenance
  Inbox summary card and Needs-attention list on the Activity page, and a Run
  Detail automation-results section. Email-tool `tool_completed` trace
  summaries are now redacted (message id/count only — never subject/body/
  draft text) for `email_find`/`email_read`/`email_draft_reply`, closing a
  pre-existing leak where the generic 200-char summary could include body
  text. Scope: Release 1A only, per
  `docs/superpowers/specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md` —
  explicitly deferred: WP0 (real customer discovery/offline eval, needs live
  customers), WP6 (attachment reading, per-org Microsoft Graph/OAuth), and the
  guided Policy-Skill wizard form (an org's own policy skill can already be
  authored today via the existing Advanced Skills CRUD).

  **Post-implementation review hardening** (Codex review against `main`):
  email-tool trace redaction now correctly distinguishes a UID-scoped tool's
  out-of-batch rejection from a real success (it used to mislabel a rejection
  as "Read message"/"Draft reply saved"), and the model-controlled
  `message_id` field is length-bounded before it reaches the trace.
  `automation_results.py`'s `action.draft_created` is no longer trusted from
  the model alone — `runtime.py` collects the run's own confirmed
  `email_draft_reply` successes from its trace events and
  `normalize_run_result` downgrades any claimed-but-unconfirmed draft to
  `draft_created: false` + `needs_attention: true`. `retry_triggered_run` now
  (a) only accepts a `failed` run (a `completed` run may already have real
  mailbox side effects, so retrying it risked duplicate drafts), (b) checks
  the current mailbox's host/username against `trigger_context`, not just
  UIDVALIDITY (`OrgEmailCredential` is upserted per org, so the credential row
  id alone never changes even when the mailbox is fully replaced), and (c)
  registers itself on `EmailTrigger.last_run_id` so the poller's overlap guard
  can see an in-flight manual retry. `GET /api/automation-results/summary`
  gained an `ever_used` flag so the Activity page's summary card renders
  nothing for an org that has never used this template, instead of an
  always-present empty-looking card. Two lower-severity review findings were
  deliberately **not** fixed — see Known issues below (retry admission race,
  retry-eligibility for a completed run with per-item errors).

  **Second review pass** (Codex review against `main`, post-hardening):
  `Envelope.schema_version` now rejects anything other than the one supported
  version instead of silently accepting an unrecognized future schema (whose
  unknown fields `extra: "ignore"` would otherwise drop quietly) — an
  unsupported version fails the whole envelope with the normal error-row
  treatment. `draft_type` is now length-capped like every other free-text
  payload field. `RunDetail.jsx` refetches `automation-results` when a live
  run's terminal event arrives (previously only fetched once on mount, so
  opening a still-running run's detail could leave the section empty forever
  — `normalize_run_result` only writes after the run finishes) and a
  successful retry now navigates the Activity page to the newly created run
  instead of discarding its id. The Activity summary card's default date
  moved from the backend's UTC "today" to the browser's local date — pushed
  back on the review's literal ask (a per-org timezone setting) since no
  `Organization.timezone` concept exists anywhere in this app and the UTC-day
  convention is consistent with `email_trigger.py`'s existing daily-cap
  reset; the `?date=` override already existed on the endpoint; the frontend
  just wasn't using it.

  **Third review pass** (Codex review against `main`, post-second-pass):
  `RunDetail`'s Retry section now also keys off the run's own `run_failed`
  trace event, not just the `status` prop `ActivityPage` set at click time —
  a run that fails while its detail panel is still open no longer requires
  closing and reopening the panel before Retry appears. `retry_triggered_run`
  now checks the same overlap guard `poll_org` enforces before dispatching:
  if `EmailTrigger.last_run_id` still points at a registered run that's
  actually running, the retry is rejected rather than silently overwriting
  that registration (which could otherwise let a concurrent automatic poll
  dispatch a second run against the same mailbox once the retry finished
  first) — and a successful retry dispatch now clears `last_error`/
  `last_error_kind`, matching `_start_triggered_run`'s "a run is going out:
  clear any prior fault" behavior, so a resolved workflow-kind error no
  longer keeps reporting failure indefinitely. `GET /api/automation-results
  /summary` now takes `tz_offset_minutes` (the browser's own
  `Date.getTimezoneOffset()`) and bounds "today" by the caller's local day
  instead of always UTC midnight-to-midnight — the prior local-date-only fix
  still misdated rows created in a timezone-ahead-of-UTC org's first few
  local hours of a new day. The summary card now also refreshes on the same
  30s cadence as the adjacent needs-attention list, instead of only fetching
  once on mount. 908 backend + 89 frontend tests, lint/build green.

  **Fourth review pass** (Codex review against `main`, post-third-pass): a
  run that crashes (`run_failed`) before producing any JSON no longer drops
  its whole UID batch from Needs-attention — `_start_triggered_run` now
  stamps `trigger_context["result_contract"]` at dispatch time whenever the
  deployed workflow's config gives an agent the `property_maintenance_response_v1`
  skill (a positive, persisted signal a plain failure string can't carry),
  and `_normalize` synthesizes the usual per-UID error rows for that case
  instead of leaving the batch untouched; a workflow without that skill (an
  unrelated org's `email_triage_reply` trigger, say) is still left alone, so
  this doesn't regress the "only engage for a declared Property Maintenance
  Inbox run" scoping decision. `action.draft_created` is now also *upgraded*
  to `true` when the run's trace confirms a real `email_draft_reply` success
  the model's envelope claimed `false` for (previously only the opposite —
  downgrading a claimed-but-unconfirmed `true` — was reconciled), so a
  mis-reporting model can no longer make the stored result and daily summary
  under-count a draft that genuinely exists. A failed `email_read`/
  `email_draft_reply` tool call now retains its (bounded) `message_id` in the
  trace even on the exception path, and `runtime.py`/`automation_results.py`
  use that to force `needs_attention` for the UID regardless of what the
  model's own item claims (spec 9.5 "Tool failure -> needs_attention: yes"),
  the same distrust-the-model boundary already applied to draft claims.
  `normalize_run_result`'s commit now happens *before* the terminal event is
  published to WS subscribers (previously after) — a live Run Detail could
  otherwise refetch automation-results the instant it saw `run_completed`/
  `run_failed` and race ahead of the rows actually being written, with no
  retry to pick them up later. `RunDetail`'s Retry button is now gated on the
  run being autonomous (`GET /api/runs`' existing `autonomous` flag, threaded
  through `ActivityPage`'s `selectedRun`) — it previously rendered for any
  failed run including manual ones, which always 400s since
  `POST /api/runs/{id}/retry` requires a recorded `trigger_context`. The
  automation-results card in Run Detail now also renders `classification`/
  `category`/`missing_information`/`risk_reasons`, previously visible only in
  the raw trace. 920 backend + 91 frontend tests, lint clean.

  **Fifth review pass** (Codex review against `main`, post-fourth-pass):
  `normalize_run_result` now runs on *every* terminal path, not just the
  `run_completed`/`run_failed` branch of the streaming loop — a cancelled
  triggered run (`_mark_cancelled`) and a run that crashes before the first
  stream event (the outer exception fallback) previously left their UID batch
  entirely absent from Needs-attention instead of getting the usual synthetic
  error rows (spec 10.1's "a model-omitted UID must never silently
  disappear" applies equally to a run that never reached the model at all).
  `run_in_background` also now commits the terminal status *before* setting
  `terminal_seen = True` on all three paths, so a commit failure on the
  primary terminal write correctly falls through to the outer exception
  handler's `run_failed` fallback instead of leaving the run looking
  permanently "running" to a live subscriber. An `email_read`/
  `email_draft_reply` tool outcome of `not_found`/`out_of_batch` now forces
  `needs_attention` the same way an outright tool failure does — previously
  only `success: false` was escalated, so a soft rejection stayed hidden
  unless the model self-reported it. `retry_triggered_run` now excludes any
  UID the original run already confirmed-drafted (new `already_drafted_uids`
  helper, matched via each UID's mailbox/UIDVALIDITY-scoped `source_key`
  against `AutomationItemResult.payload.action.draft_created`) before
  resubmitting the batch, and rejects the retry outright if every UID already
  has a confirmed draft — `email_draft_reply` has no dedup of its own, so
  blindly resubmitting a partially-drafted batch risked a second draft in the
  mailbox. That check depends on `AutomationItemResult` rows reliably
  existing for a `failed` run's whole batch, which is what the
  normalize-on-every-path fix above guarantees; the two synthetic-error-row
  fallback paths in `_normalize`/`_write_error_rows` (unparseable output,
  model-omitted UID) were also fixed to record `draft_created: true` when a
  confirmed draft exists, since they previously hardcoded `false`
  unconditionally and would have made `already_drafted_uids` silently wrong
  for exactly the crash/omission cases it most needs to protect. Finally, the
  overlap-check-through-dispatch section of both `poll_org` and
  `retry_triggered_run` is now serialized behind a new per-org
  `threading.Lock` (`_dispatch_lock`), with a fresh `SELECT` of
  `EmailTrigger.last_run_id` inside the lock (`_current_last_run_id`, not
  `db.refresh` — that would also discard each function's own pending
  uncommitted daily-cap-reset change) rather than trusting a possibly-stale
  ORM attribute from an object loaded before lock acquisition — closing a
  real gap where two concurrent dispatches (retry-vs-poller, or
  retry-vs-retry) could both pass the "no run already in flight for this
  trigger" check and both fire against the same mailbox. This also closes the
  narrower, previously-deferred "two near-simultaneous retry clicks on the
  same run" race documented below (removed from Known issues) as a side
  effect, since the second click's check is now serialized behind the
  first's. 932 backend + 91 frontend tests, lint clean.

  **Sixth review pass** (two Codex runs, one against `main` and one against
  the working tree, post-fifth-pass): a property-maintenance run's raw agent
  output (`agent_completed`/`run_completed`'s text, which the envelope's
  free-text fields can quote directly from customer email) is now redacted
  to a fixed placeholder before it ever reaches a live WS broadcast,
  persisted `trace_events`, or `runs.output` — the same trust boundary
  `tool_completed` already had for `email_find`/`email_read`/
  `email_draft_reply`, applied in `runtime.py` since only it knows a run's
  `trigger_context`; `normalize_run_result` still gets the real text via a
  new `raw_output_override` parameter, so the structured result is
  unaffected. `already_drafted_uids` now checks a run's whole **retry
  family** (walk back to the root via `retry_of_run_id`, then forward to
  every descendant — a tree, not always a line, since the *original* run can
  be retried more than once), not just the one run being retried — a second
  retry off the original, rather than off a first retry that also failed,
  previously couldn't see what that first retry had already drafted. The
  daily-cap check now has the same fresh-read/atomic-update treatment as the
  `last_run_id` overlap guard: `_at_daily_cap` re-checks with a bare `SELECT`
  right before dispatch inside the per-org lock (the earlier check is only a
  fast-path to skip mailbox/workflow work when obviously already at cap),
  and `retry_triggered_run`'s advance is now `UPDATE ... SET runs_today =
  runs_today + 1` instead of a Python-level `+= 1` that could silently lose
  a concurrent dispatch's increment for the same org. A retry's dispatched
  input text is now built from the narrowed `retry_uids`, not reused from
  the original run's `input` (which named the full original batch, including
  any UID just excluded as already-drafted). If `_executor.submit` itself
  raises in either `_start_triggered_run` or `retry_triggered_run`, the
  worker never starts and so never normalizes either — both branches now
  call `normalize_run_result` explicitly, or a declared batch was marked
  failed with zero `automation_item_results` rows and silently vanished from
  Needs-attention. One review finding was investigated and **not** applied:
  a claimed nested-JSON-inside-a-markdown-fence parsing bug in
  `_JSON_FENCE_RE` — verified false by direct reproduction (Python's `re` is
  a backtracking engine, so `\{.*?\}` followed by the closing-fence literal
  correctly extends past inner `}` characters until the whole pattern,
  including the trailing ` ``` `, matches; a nested envelope parses
  correctly). 940 backend + 91 frontend tests, lint clean.

  **Seventh review pass** (two Codex runs, against `main` and the working
  tree, post-sixth-pass): `retry_triggered_run`'s dispatch-time update now
  requires `EmailTrigger.enabled` and the org still active, same guard as
  `_start_triggered_run`'s own CAS — the previous unconditional update didn't
  detect a customer disconnecting/replacing the mailbox (or an operator
  deactivating the org) during this call's own pre-lock credential/mailbox
  check, so a retry could dispatch a real `email_draft_reply` against a
  mailbox the customer had already disconnected; a rejected CAS now rolls
  back the pending new run row and raises a customer-facing `RetryError`
  instead. The "a retry of this run is already in progress" and
  already-drafted-UID checks are now re-run fresh immediately before
  dispatch, inside the per-org lock — the equivalent checks further up
  (before the mailbox connectivity check) are only a fast-path and can be
  stale by the time execution reaches the lock: two retry requests racing
  the same failed run could otherwise both pass on data that predates the
  first one's own dispatch/normalization, and since `email_draft_reply` has
  no dedup, both would create a duplicate draft. 943 backend + 91 frontend
  tests, lint clean.

  **Eighth review pass** (Codex run against `main`, post-seventh-pass): the
  email tools' own `.strip()` normalization of `message_id` (`email_client.py`'s
  `_read_impl`/`_draft_impl`) wasn't mirrored in the trace-evidence side —
  `_bounded_message_id` now strips too, so a model calling with `" 42 "`
  can't produce a trace `message_id` that fails to match the envelope's
  (already-stripped) claimed id during normalization; the previous mismatch
  meant a real draft could go unrecognized as confirmed, risking a duplicate
  on retry. Both `_start_triggered_run`'s and `retry_triggered_run`'s
  submission-failure branches now call `normalize_run_result` **before**
  `registry.publish`-ing `run_failed`, matching the ordering already fixed in
  the normal `runtime.py` terminal path — publishing first left a window
  where a live Run Detail view reacting to the terminal event could fetch
  zero `automation_item_results` rows, with no later terminal transition to
  prompt a re-fetch. Pydantic's `ValidationError.__str__` embeds the
  offending input value, so logging `exc` directly on an invalid envelope
  could put raw customer content (a prompt-injected email steering the model
  into putting body text into an invalid enum/id field) into server logs
  despite the trace redaction; the warning now logs only `loc`/`type` per
  error, never the value. 947 backend + 91 frontend tests, lint clean.

  **Ninth review pass** (Codex run against `main`, post-eighth-pass): the
  out-of-batch `message_id` warning in `normalize_run_result` logged the raw,
  unbounded envelope value with `%r` — since `EnvelopeItem.message_id` has no
  length cap and is entirely model-controlled, a prompt-injected email could
  steer the model into putting arbitrary body content into that field for an
  id outside `trigger_context`'s UID batch, landing it unbounded in server
  logs. The warning now logs a 64-char-capped value instead (reusing the
  existing `_cap` helper). 948 backend tests, lint clean.

  **Tenth review pass** (Codex run against `main`, post-ninth-pass): three
  findings. (1) The PM-contract trace redaction (`runtime.py`) only covered
  `agent_completed`/`run_completed` — a declared maintenance workflow using
  HIERARCHICAL mode also emits `subagent_started`/`subagent_completed`/
  `delegation_started`/`delegation_completed` events carrying the same
  customer-email-derived text (the manager's delegated task, the
  subordinate's raw output), which leaked around the redaction boundary; all
  six event types are now redacted uniformly. (2) `_declares_property_
  maintenance_contract` (`email_trigger.py`) matched the
  `property_maintenance_response_v1` skill name only, but `load_skills`
  intentionally lets an org's own skill shadow a same-named platform
  built-in — an org that named its own, unrelated skill the same thing would
  get its runs wrongly redacted and stamped with synthetic maintenance error
  rows; a new `_resolves_to_platform_skill` re-applies `load_skills`' own
  shadowing precedence to confirm the name still resolves to the
  platform-tier (`org_id IS NULL`) row before stamping the contract. (3)
  `ActivityPage.jsx`'s Needs-attention "View run" always opened the run at a
  hardcoded `status: 'completed'`, but a dispatch failure still synthesizes
  needs_attention error rows for its UIDs — that hardcoding permanently hid
  the Retry button for a needs-attention item that came from a genuinely
  failed run. `GET /api/runs` gained an org-scoped `run_id` filter (the
  existing `GET /api/runs/{id}` route only ever sees the in-memory registry,
  unreliable for a historical run), and `onOpenRun` now looks up the real
  persisted status before opening the panel. 950 backend + 92 frontend
  tests, lint clean.

  **Eleventh review pass** (Codex run against `main`, post-tenth-pass): the
  tenth pass's HIERARCHICAL redaction fix (`_PM_REDACTED_EVENT_TYPES`) missed
  a second leak path -- the manager's `delegate_to_<name>` tool call also
  produces its OWN `tool_completed` event (`adapters/langgraph_adapter.py`'s
  generic tool-calling loop, distinct from the `on_event`-driven
  subagent_completed/delegation_completed events already covered), whose
  `summary` is the same raw subordinate output. `runtime.py` now also
  redacts a `tool_completed` event whose `tool` starts with `delegate_to_`
  (`_is_delegate_tool_completed`); a non-delegate `tool_completed` (e.g.
  `email_read`) is unaffected. 951 backend tests, lint clean.

  **Twelfth review pass** (Codex run against working tree, post-eleventh-pass
  commit): three findings. (1) `retry_triggered_run` was spreading the
  original run's `trigger_context` (including a stale `result_contract`)
  into the retry's new run instead of recomputing it against the workflow
  actually redeployed since -- a workflow that gained or lost the platform
  maintenance skill between the original run and the retry would then get
  the wrong redaction/normalization behavior; it now re-derives
  `result_contract` via `_declares_property_maintenance_contract` at retry
  time, same as a fresh dispatch. (2) the demo template's Response
  Coordinator only had `property_maintenance_response_v1`, not
  `email_input_security_core_v1` -- since it drafts from the Intake
  Analyst's write-up (which can itself quote injected instructions from the
  original email), it needed the same prompt-injection defenses, not just
  the Intake Analyst; both platform agents now carry the security skill.
  (3) `GET /api/runs` and `GET /api/automation-results`, when given an
  explicit `run_id` for a run belonging to another org, returned HTTP 200
  with an empty list instead of a 404 -- inconsistent with the org
  multi-tenancy contract (`ui/backend/CLAUDE.md`'s "cross-org access is a
  404, existence is never revealed") and distinguishable from a real 404
  elsewhere; both routes now 404 an explicit cross-org/unknown `run_id`
  before running the list query. 958 backend + 92 frontend tests, lint
  clean.

- E2E harness + CI test tiering (`2026-08-13-e2e-and-ci-test-tiering-design.md`):
  a self-contained Playwright E2E suite (`tests/e2e/`) whose `e2e_backend`
  session fixture provisions its own temp SQLite DB, spawns real
  `uvicorn`/`vite` dev-server subprocesses, auto-provisions accounts, and
  reshapes the model catalog to a deterministic `fake-architect:` model
  (never present in `DEFAULT_MODEL_CATALOG`) so the Team Builder wizard's
  AI-generation steps are covered without a real LLM key. Every test file
  now carries a `pytestmark` (`unit`/`integration`/`e2e`/`optional`,
  optionally `slow`), enforced by `tests/test_marker_completeness.py`. CI
  is split into 6 jobs: 4 fast PR-gate jobs run on every PR/push, 2
  full-regression jobs (`backend-full`, `e2e-full`) are gated to `main`
  (also now manually dispatchable). Final whole-branch review fix round:
  the marker-completeness guard's collection-summary parser was fixed
  (it never matched real pytest output, so it asserted nothing); the E2E
  fixture was hardened with a port pre-flight check, a liveness check
  while polling for health, `--strictPort` for vite, and explicit
  neutralization of `BESTTEAM_MEMORY_*`/`BESTTEAM_EMAIL_BACKEND` env vars
  so a developer's real dev stack/config can't be silently attached to or
  leaked into; `workflow_dispatch` added to CI. See
  `.superpowers/sdd/2026-08-13-e2e-and-ci-test-tiering/`.

- **Anonymous team sharing with continuous chat** (spec:
  `docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md`).
  An org member generates a revocable `/share/:token` link for one deployed
  team; a colleague opens it, never logs in, and gets a real multi-turn
  conversation. Three tables (`share_links`/`share_sessions`/
  `share_messages`), two routers (`share_links_api.py` org-side,
  `share_chat.py` public), a signed session cookie instead of a JWT, and
  transcript replay into the existing single-shot `Workflow.run()` (no
  engine change, no checkpointing). Rate-limited by `daily_cap` at both the
  session and the link-aggregate level. See `ui/backend/CLAUDE.md`,
  `ui/backend/db/CLAUDE.md`, `ui/frontend/CLAUDE.md`.

  **Final whole-branch review** (after every task had passed its own review):
  the per-session cap alone capped nobody (a cookie-less client got a fresh
  allowance per request -- fixed with a link-level aggregate cap); the dev
  defaults pointed the API at `127.0.0.1` while Vite serves `localhost`, so
  the `SameSite=Lax` visitor cookie never round-tripped in this app's own
  default setup; the replayed transcript was forgeable by visitor input; raw
  trace/tool/model data was streamed verbatim to anonymous visitors; a failed
  `_executor.submit` wedged a visitor's chat permanently; and a wildcard
  `BESTTEAM_CORS_ORIGINS` became a real CSRF exposure once
  `allow_credentials=True` was needed for the cookie (now refused at
  startup). All fixed on the branch.

  **Second Codex review pass** (post-merge follow-up, against `main`): the
  link-wide daily cap was consumed before the visitor's session existed, so
  a cap-exhausted link still minted a fresh `ShareSession` (and its lock)
  per request -- claiming order flipped and rejections now refund the
  link-level turn. Transcript replay was bounded by turn count only, not by
  size, so a long history could still blow past a smaller model's context
  window -- added a character budget (`MAX_HISTORY_CHARS`) trimmed
  oldest-first. `_share_session_dict`'s timestamps omitted the UTC offset,
  which `SharedSessionsPanel`'s `new Date(...)` then misread as local time
  for non-UTC viewers -- switched to the existing `iso_utc()` helper. A slow
  initial history fetch could resolve after a visitor's own send and
  clobber it -- `ShareChatPage` now guards with an `ignore` flag plus a
  has-sent ref. A failed share-reply write had no repair path -- it now
  retries a bounded number of times before giving up. 1290 backend + 169
  frontend tests, `tsc`/eslint clean.

- **Knowledge-base document/chunk ingestion** (spec:
  `docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`):
  KB uploads (both the admin and org self-service routes) now dispatch an
  async ingestion job instead of parsing/chunking/embedding on the request
  thread — `ui/backend/ingestion.py`'s own `ThreadPoolExecutor` does the
  work, persisting parsed chunks (and, for `vector`/`hybrid`, embeddings)
  into three new tables (`knowledge_ingestion_jobs`/`knowledge_documents`/
  `knowledge_chunks`) instead of only ever living on disk. All three SDK
  `KnowledgeBase` classes gained a `from_chunks(...)` alternate constructor
  so the backend can rebuild a KB straight from those rows
  (`knowledge_bases.py::resolve_knowledge_base`) rather than re-reading files
  on every load; a KB with no completed ingestion job (pre-existing,
  never re-uploaded) falls back to the original file-based construction
  unchanged. Per-document partial failure means one bad file doesn't fail
  the whole batch. Two new read-only endpoints
  (`GET .../knowledge_bases/{name}/ingestion-jobs/{job_id}` admin,
  `GET .../knowledge-bases/{name}/ingestion-jobs/{job_id}` org self-service)
  let the frontend poll a job to completion; both `DocumentsPage.tsx` (the
  wizard) and `AdvancedPage.tsx` (admin) now do so before proceeding, showing
  an "ingesting" busy stage. KB deletion cascades to delete all three new
  tables' rows. Along the way this also picked up a prerequisite
  Standard/Enhanced smart-search toggle (`kb_type`/`embedding_model`/
  `rerank_model`/`query_expansion_model` on the org self-service upload) that
  had been implemented and tested earlier but never committed, and fixed a
  CR-005-shaped workflow-cache staleness recurrence (the cache is now also
  invalidated when an ingestion job completes, not just on KB
  create/update/delete). 1376 backend + 176 frontend tests, `tsc` clean. See
  `docs/KNOWLEDGE_BASES.md`, `src/bestteam/core/CLAUDE.md`,
  `ui/backend/CLAUDE.md`, `ui/backend/db/CLAUDE.md`.

- Test architecture remediation (branch `fix/test-architecture-remediation`;
  spec `docs/superpowers/specs/2026-08-17-test-architecture-remediation-design.md`).
  Started from "the tests feel slow" and found that the felt slowness was
  barely about tiering at all. **The backend suite went from 13m09s to 3m14s
  serial and 2m05s under `-n auto`, and CI went from red on `main` for most of
  a month to green.** Nothing in `ui/`, `src/` or `alembic/` changed — the
  entire diff is tests, docs and CI config.
  Three findings, in order of what they cost us:
  (1) **69% of the suite was PBKDF2.** `auth.py` hashes at the production
  260,000 iterations (~0.76s each) and function-scoped fixtures register and
  log in several users per test — 543 of 789 seconds. `tests/conftest.py` now
  lowers the module attribute for the test process only; deliberately NOT an
  env var or config key, which would make a security parameter misconfigurable
  in production forever. `tests/e2e/` drives a real uvicorn subprocess that
  never imports conftest, so that tier still exercises genuine 260k hashing,
  and `test_auth.py::test_production_pbkdf2_iterations_are_unchanged` reads the
  literal out of `auth.py`'s source so a weakened shipped default still fails.
  (2) **`make_engine(":memory:")` gives every Session in the process one shared
  transaction**, because an in-memory SQLite engine must use a `StaticPool`. A
  worker thread's `Session.close()` issues a `ROLLBACK` that discards a
  request's flushed-but-uncommitted writes; the request's `commit()` then
  commits nothing and the endpoint answers 200/204 having written nothing. The
  loss is silent, so it surfaced as unrelated assertions failing intermittently
  much later — this one defect explains every flaky test we had. Sixteen
  exposed files moved to a shared
  `tests/helpers.py::make_concurrent_safe_engine(tmp_path)` (file-backed,
  `PRAGMA synchronous=OFF`), which matches production's `QueuePool` semantics;
  ten stay on `:memory:` with a recorded per-file reason. Which is which was
  settled by a call-graph audit, not by keyword screening — the first pass
  screened for `threading`/`run_in_background` and missed
  `test_share_chat_ws.py`, which CI then failed. The backend opens a second
  Session in exactly four places (`runtime.py:340`, `ingestion.py:95`,
  `main.py:958`, `share_chat.py:401`) and spawns threads from one
  (`runtime._executor`); every file is checked against the routes it actually
  exercises. `test_email_trigger.py` deliberately stays on `:memory:` — it
  simulates a concurrent writer by committing through a second Session while
  the first holds an uncommitted write, which is a real deadlock on a file
  database.
  (3) **Eleven flaky tests fixed at their actual race**, no sleeps, retries, or
  loosened assertions: two in `test_builder_api`, three in `test_share_chat_api`
  (fixing the engine made the harness genuinely parallel, which exposed two
  more), two in `test_crud_api`, one each in `test_org_knowledge_bases` and
  `test_share_chat_ws`, `test_marker_completeness` (its `--collect-only`
  subprocess now stays off the parent's `.pytest_cache`), and
  `tests/e2e/test_smoke.py`, where `page.goto` waited for a `load` the auth
  guard's redirect would never let arrive — fixed at all three sites, not just
  the one that failed, and tolerating the aborted navigation outright after
  `wait_until="commit"` proved to only narrow the window. A twelfth, in the
  frontend, surfaced on CI after the backend work was done:
  `AdvancedPage.test.tsx` waited for `listOrgs` to have been *called* and then
  clicked a tab, when what it needed was the orgs having *landed* — different
  moments, and clicking in between defers the awaited call to the load
  effect's next re-run. The suite-wide `asyncUtilTimeout` also went 1s → 5s,
  since testing-library's default is tuned for an idle laptop rather than 24
  files sharing two CI cores; unlike a sleep it costs nothing when tests pass,
  as `waitFor` returns the moment its condition holds.
  Also: `test_marker_completeness` ran two full collect-only subprocesses to
  learn two numbers pytest already reports in one, making a single test 54s;
  CI now path-filters every job so a docs-only commit runs nothing, the PR
  backend gate runs under `-n auto`, and `backend-full` stays serial on purpose
  as the job that still asserts the suite is order-independent.
  Two product findings were escalated rather than fixed, both in Known issues
  below: KB deletion has no interlock with an in-flight ingestion job, and
  `_active_kb_dir`'s `max(st_mtime)` version resolution is theoretically
  ambiguous on Windows.

- Email automation Phase 0 hardening (`feat/email-phase-0-hardening`):
  the joint output of an internal and an external architecture review of the
  email monitoring/reply capability, scoped to defects reachable on ordinary
  paths today. Seven items: draft idempotency no longer depends on the
  property-maintenance template (trace-event evidence + an
  `X-BestTeam-Source-Key` mailbox marker, closing a duplicate-draft bug that
  needed no crash to fire); a stuck-run watchdog
  (`BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS`) so a hung run stops wedging an
  org's trigger silently forever; run outcomes now reach trigger health so a
  team failing every run no longer reports "Active"; deploy refuses email +
  `http_get`/`web_search` on one agent (an injected email's exfiltration route
  around the draft-only bound); mailbox save validates login *and* drafts
  writability before storing; and a regression guard pinning the SDK-layer
  email trace redaction as contract-independent. Two review items were
  **re-scoped during design** rather than implemented as proposed -- see the
  spec's "Judgement calls". Spec:
  `docs/superpowers/specs/2026-08-17-email-phase-0-hardening-design.md`.

- Email automation Phase 1, the durable inbox ledger
  (`feat/email-phase-1-inbox-events`): a new `inbox_events` table records one
  row per detected message **in the same commit that advances the mailbox
  cursor**, and a run then claims its batch from it. This closes the window
  `_start_triggered_run` conceded in its own docstring — it advanced
  `last_uid`, persisted the run row and burned the daily cap in one commit and
  only then handed the workflow to a thread pool, so a process killed between
  the two consumed mail that nothing ever ran. Batching survives as a claim
  policy rather than a coupling, so the daily cap, the property-maintenance
  batch contract and `trigger_context`'s shape are all unchanged. Failure
  handling splits by class: infrastructure failures (dispatch failure, the
  stale-run watchdog, a build failure) release the messages, workflow failures
  stay terminal for the existing human retry, and Phase 0's
  `already_drafted_uids` is what decides per message which is which. Attempts
  are charged at dispatch, never at claim, so a broken team config retries
  forever instead of dead-lettering a day of an org's mail;
  `BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS` (default 3) bounds the rest. **The
  joint review's Phase 1 also bundled leader election and multi-worker safety;
  those were dropped during design as not reachable** — see the sharpened
  Known-issues entry below. Spec:
  `docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md`.
- **Email automation Phase 2 — Microsoft 365 mailbox connections.** An org on
  Exchange Online could not connect a mailbox at all: `build_org_imap_backend`
  only ever built a basic-auth login, which Microsoft removed. Mailboxes now
  record an `auth_type`; `microsoft_oauth` authenticates with app-only client
  credentials over SASL `XOAUTH2` (`bestteam/tools/_oauth.py`, stdlib `urllib`
  — no new dependency), configurable in the wizard, the org settings API and
  `admin set-email --auth microsoft-oauth`. The connect flow fetches the token
  as a separate step so a credential problem and a mailbox-access problem get
  different, actionable messages. **`email_trigger.py` and `runtime.py` are
  untouched** — after `AUTHENTICATE` the session is an ordinary IMAP session,
  so the UID cursor, Phase 0's draft markers and Phase 1's ledger all keep
  working. **The roadmap's "MailboxConnector abstraction + Graph/Gmail OAuth"
  was deliberately not built** — see `docs/DECISIONS.md` for why an abstraction
  over one and a half implementations was the wrong trade, and why Graph-native
  would have regressed Phase 0. Spec:
  `docs/superpowers/specs/2026-08-17-email-phase-2-microsoft-oauth-design.md`.

- **Email automation Phase 3a — trigger health, alerting, and two correctness
  fixes.** Phase 0 made a failing trigger *visible* (`last_error`); nothing
  ever *told* anyone, so a customer whose automation stopped a week ago found
  out by opening the dashboard. A pure evaluator (`ui/backend/trigger_health.py`)
  now decides state transitions from four outcomes — workflow fault, mailbox
  fault, watchdog release, and domain-specific recovery — and the three
  existing fault sites persist its decision and append a `Notification`.
  **Alerts fire on transitions, not occurrences**: a trigger remembers the
  fingerprint of the alert most recently sent, so a condition already reported
  stays quiet until it clears. Delivery is in-app plus an optional per-org
  webhook (stdlib `urllib`, HMAC-signed, HTTPS + `check_host_allowed`, health
  information only — never a subject, address or body). There is no SMTP
  anywhere and there is not going to be. An admin-entered Microsoft 365 secret
  expiry drives warnings at 30 days, 7 days and expiry.

  Recovery is deliberately domain-specific: a successful mailbox check proves
  connectivity is fine but says nothing about whether the team still builds,
  so it must not clear a workflow alert — flattening that would have
  re-created the "healthy trigger, every run failing" state Phase 0 fixed.

  Also closed three findings from a review of the Phase 0–2 branch: the
  egress-tool check is now **workflow-level**, because splitting email and
  `http_get` across separate agents (the previously documented remedy)
  contains nothing — `_agent_node` feeds each agent's output into the next
  agent's context; draft creation is **idempotent per source key** under a
  process-wide lock, which closes the watchdog/retry duplicate-draft race
  because both racers are threads of one process; and trigger health ignores
  a **superseded** run's late outcome. The review's proposal to keep a
  timed-out run non-retriable was rejected — the wedged worker never
  acknowledges cancellation, so that would have reinstated the permanent
  trigger blockage Phase 0 exists to fix. Spec:
  `docs/superpowers/specs/2026-08-17-email-phase-3a-health-alerting-design.md`.

- **Email automation Phase 3b — retention, deletion and export.** A generic
  email team's model output (names, subjects, body excerpts) persisted
  indefinitely; raw bodies were already redacted at the adapter layer, but a
  generic team's output *is* the product, so the only lever is time. Each org
  now has a run-history retention period (`org_retention_settings`, NULL =
  keep forever, so an upgrade deletes nothing), a "delete now" batch purge over
  an explicitly stated window, per-run deletion
  (`POST /api/runs/{id}/purge`), and a JSON export
  (`GET /api/org/export`) — all on a new **Data** tab.

  **A purge clears content and keeps accounting** (`ui/backend/retention.py`):
  `runs.input`/`output`, the run's `trace_events` and
  `automation_item_results.payload` go; the `runs` row, `usage_records`,
  `trigger_context` and an item result's `status`/`source_key` stay, stamped
  `runs.content_purged_at`. Deleting the row would have taken the org's cost
  history with it, and clearing an item's `status`/`source_key` would have made
  a sweep cause duplicate drafts on retry — `CONFIRMED_DRAFT_OUTCOMES` excludes
  already-drafted UIDs by exactly those two fields.

  Export exists so deletion is safe enough to enable, and the two are coupled
  by a test: `PURGED_FIELDS` declares the purge surface once and
  `test_export_covers_everything_purge_clears` fails if the export stops
  covering it. The sweep runs from the poller's maintenance tail, and
  `poll_forever`'s disabled branch now runs `maintenance_once` instead of
  skipping the cycle — a platform-wide pause of *automation* is not a pause of
  *data deletion*. Spec:
  `docs/superpowers/specs/2026-08-17-email-phase-3b-retention-export-design.md`.

- **Email automation Phase 4a — pre-LLM filtering and real budgets.** Two holes
  that both cost a customer money they never agreed to spend. *Every* message
  reached the model: a newsletter, a delivery receipt and a "do not reply"
  notification each cost the same as a real enquiry. And the only ceiling
  counted the wrong thing — `BESTTEAM_TRIGGER_DAILY_CAP` caps *runs* per day,
  and a run processes up to 20 messages, so the customer-visible promise was
  "at most 1,000 messages a day, at an unknown price", which is not a budget.

  A **header-only, rule-based filter** (`ui/backend/email_filter.py`, pure —
  no I/O, no clock, no DB, the same shape as `trigger_health.py`) now decides
  before any model is involved whether a message is worth processing, and
  records why. **Rules and not a classifier**, for three reasons: a cheap
  gatekeeper model still bills per message (paying less for junk is a discount,
  not a fix); it reads attacker-controlled text, and a model that decides
  *whether a message is processed* is one an attacker has a direct incentive to
  talk past, which widens the injection surface the draft-only bound exists to
  contain; and an admin cannot audit it — "the sender matches
  `*@newsletter.example.com`" is a rule someone can read and change, "the
  classifier scored it 0.31" is not. Headers suffice for the junk that actually
  dominates an inbox, because bulk mail identifies itself: RFC 3834
  `Auto-Submitted`, RFC 2919 `List-Id`, RFC 8058 `List-Unsubscribe` and the
  de-facto `Precedence` exist precisely so automated agents recognise it.

  **Filtering changes an `inbox_events` row's *status*, never whether the row
  is inserted.** Phase 1's durability guarantee — the commit that consumes the
  mail is the commit that records it — is untouched, and `claim_events` already
  selects `pending` only, so the claim, dispatch, retry and completion paths
  change not at all. Release is one `filtered` → `pending` flip. That is why
  "record, show, allow release" beat "drop and count": a rule-based filter
  *will* have false positives, and the cost of one has to be "an admin clicks
  Release", not "the enquiry was silently lost and nobody ever knew". The
  evaluation order is fixed and is the behaviour (blocked sender → not
  allowlisted → blocked subject → bulk), the blocklist outranks the allowlist,
  and **the allowlist deliberately does not exempt a sender from the bulk
  check** — an allowlisted domain that starts sending a newsletter is still
  sending a newsletter. Patterns are two forms only (a full address,
  `*@domain`), matched against the parsed address and never the attacker-chosen
  display name; there are **no regular expressions**, because a customer-supplied
  regex brings catastrophic backtracking into the poll loop and no admin can be
  told why theirs did not match.

  **`skip_bulk` defaults to on** — the one deliberate behaviour change on
  upgrade. A safety feature nobody switches on protects nobody, and the default
  is recoverable: one checkbox turns it off, and every filtered message stays
  visible and releasable. Both budget caps default to NULL, because an upgrade
  must not start refusing a customer's mail over a limit they never set.

  **Two real per-org budgets** (`org_email_budget_settings`): a daily message
  cap enforced at claim (`limit=min(batch_size(), remaining)`, and no run
  dispatched at all when nothing remains), and a monthly spend cap checked
  inside `_dispatch_lock` beside the existing run-cap re-check. Spend is
  *queried* (`SUM(cost_estimate)` over `usage_records` for the UTC month), never
  counted into a column that would need its own reset, backfill and drift bug.
  Three limits, two audiences: `BESTTEAM_TRIGGER_DAILY_CAP` stays as the
  operator's deployment-wide rail (it measures the wrong thing for a customer
  promise, which is why the other two exist, but it bounds a runaway poller
  regardless of what any org configured); the message and spend caps are the
  customer's. Hitting one stops dispatch, alerts once, and resumes
  automatically — not a hard disable, because a budget reached on a Saturday
  must not need a human on Monday, and a self-disabled trigger is
  indistinguishable in the UI from one the customer turned off.

  **Budget alerts bypass `trigger_health.evaluate` deliberately**: a ceiling is
  a normal operating state, not a fault, and routing it through the fault
  evaluator would corrupt `consecutive_faults` and compete with real faults for
  `alerted_fingerprint`. Their fingerprints are period-scoped
  (`budget_messages:<UTC date>`, `budget_cost:<UTC month>`) following
  `_expiry_fingerprint`'s precedent, because `has_fingerprint` searches an org's
  *entire* history — a bare name alerts once ever and every later period is
  silent. Unpriced models are handled in three parts rather than by refusing to
  run (one missing catalogue row would wedge a customer's automation) or by
  silence: the budget API names them, NULL contributes 0 at runtime so the cap
  is a floor on reality rather than a phantom ceiling, and the UI states how
  many runs this month were unpriced.

  Cost, stated plainly: `summaries_for` now fetches the four bulk headers, so
  detection costs **one extra IMAP login per poll cycle that finds new mail**
  (`BODY.PEEK` preserved — the draft-only toolkit never marks mail seen and
  this must not become the thing that does). A UID whose headers cannot be
  fetched is recorded `pending`, not `filtered`: **fail open**, since the worst
  case of failing open is one junk message processed and the worst case of
  failing closed is a customer's mail silently discarded. Spec:
  `docs/superpowers/specs/2026-08-17-email-phase-4a-filtering-budgets-design.md`.

## In Progress

- _Nothing actively in progress._ See "Next steps / roadmap" below.

## Known issues / tech debt

- **Microsoft 365 mailbox support has never touched a live tenant.** Every test
  for it runs against fakes: they pin the SASL byte string, the token
  lifecycle, the storage round-trip and the four error mappings, but nothing in
  CI can prove that Exchange Online accepts the resulting
  `AUTHENTICATE XOAUTH2` — that depends on Microsoft's current policy for
  OAuth-over-IMAP, which they have been changing in stages.
  `docs/email-smoke-test.md` §9 is the only verification and **must be run
  against a real tenant before this is sold to an M365 customer**. The risk is
  cheap to carry: if Exchange ever refuses the flow, only `_connect()` and the
  credential shape are affected.
- **`_GraphBackend._token()` caches its access token forever.**
  `if self._access_token is None:` never refetches, but Microsoft's tokens
  expire in about an hour, so a long-lived process using
  `BESTTEAM_EMAIL_BACKEND=graph` starts failing after that until it restarts.
  Pre-existing and unrelated to the per-org path Phase 2 changed — the poller
  never constructs a `_GraphBackend` — so it was deliberately left alone rather
  than editing code that phase otherwise did not touch. The per-org OAuth
  provider (`tools/_oauth.py`) does *not* have this bug; it refreshes 60
  seconds before expiry.
- **Horizontal scale-out of the email poller is blocked on a Postgres
  migration, not on the poller.** `make_engine` hardcodes SQLite and takes a
  *file path*, not a URL (`ui/backend/db/database.py:41`), and there is no
  Postgres driver in `pyproject.toml` — replicas cannot share the file, so no
  amount of work inside `email_trigger.py` makes multi-host workers possible.
  The joint review's Phase 1 bundled leader election with the durable ledger;
  only the ledger was reachable, and it shipped (above). What remains
  in-process is `RunRegistry`, so the overlap guard and cooperative
  cancellation still assume one process and `_dispatch_lock` stays — `uvicorn
  --workers N` is still unsupported even on one host. Phase 1's claim *is*
  atomic (`UPDATE ... WHERE status='pending'`), so message-level double-
  processing is already excluded; making the overlap guard DB-authoritative is
  the next reachable step and is now cheaper than it was, since Phase 0's
  stale-run watchdog removed the original objection to it. The Postgres
  migration itself is platform-wide and unrelated to email — already raised
  and deferred once in `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.
- **The email data model is not platformised** — the joint review's Phases
  2-5. Polling is serial across orgs and every email
  tool call opens its own IMAP connection (a 20-message batch is ~41 logins);
  multi-tenant connection is IMAP username/password only (the Graph backend is
  unreachable outside the process-env single-mailbox path, which multi-org
  deployments refuse); **pre-LLM filtering is header-only**, so a human-written
  but entirely irrelevant email is not filtered and is still billed at model
  rates — that is the acknowledged ceiling of the rule approach, and the reason
  a classifier stays on the table for a later phase if a customer's inbox
  actually demands it; **attachments are still invisible** (Phase 4b); and one
  org gets exactly one mailbox, one trigger, one team. Phase 4a's header fetch
  also adds one more login to any cycle that finds new mail.
- **Erasure by data subject is not possible** — a customer asking to delete
  everything about `alice@example.com` cannot be served. The address is not
  stored anywhere indexed; it exists only inside `runs.output` free text the
  model may have paraphrased. Matching it would both miss (rewritten text) and
  over-delete (an unrelated run that mentions the address), so Phase 3b
  deliberately offers age-based retention and per-run deletion instead of a
  promise it cannot keep. Tell customers this plainly.
- **A purge is not a secure erase** — SQLite leaves the old page contents on
  disk until `VACUUM`, which nothing runs. Adequate for "stop keeping it";
  not adequate for an adversary with the database file.
- **`inbox_events` is never purged and grows without bound** — the row holds
  an IMAP UID, the customer's own mailbox address and a status, so there is no
  data-subject content in it and deleting it would break `resolve_retry_events`
  for no privacy gain. It is still an unbounded table on a busy mailbox.
  **Phase 4a's filtered rows are never purged either**; they are rows the mail
  would have produced anyway, so filtering changes the rate at which this table
  grows, not the property.
- **A spend cap bounds an estimate, not a bill.** `usage_records.cost_estimate`
  derives from `model_catalog` prices that an operator maintains by hand and
  that no provider invoice is ever reconciled against, and a model with no
  catalogue row contributes NULL, i.e. 0. The budget API and UI name the
  unpriced models and count the unpriced runs so the blind spot is visible
  rather than inferred — but `unpriced_models_for_org` scopes to
  `status="deployed"` workflows, so an undeployed team's models are not warned
  about. Anyone quoting the figure to a customer should say "at least".
- **The spend cap is enforced between runs, not within one.** A single run that
  blows through a customer's monthly cap is not interrupted; the cap stops the
  *next* dispatch. Interrupting mid-run would mean cancelling a partly-drafted
  batch, which costs the money already spent and delivers nothing for it.
- **The daily message counter over-counts on a submit failure.** The CAS
  advances `messages_today` by the claimed batch and commits; the submit-failure
  branch then hands those messages back to `pending` via `release_events`, so a
  later cycle claims and charges them a second time. `runs_today` has had
  identical semantics on that branch since it existed. Decrementing was
  rejected deliberately: it would add a second write site outside the CAS to
  correct an over-count bounded at one batch, on a branch that only fires while
  the executor is shutting down. Separately, **`retry_triggered_run` enforces
  neither new cap** and does not advance `messages_today`, so a human-initiated
  retry of one failed batch can put an org past its daily message cap —
  arguably right (a human asking to redo one batch is not autonomous spend),
  but it is a real hole in the number the customer is shown.
- **The Filtered ("Mail we skipped") UI section renders only when a trigger
  with a `workflow_name` is configured**, because it lives inside
  `EmailTriggerActivity` after its two early returns. Correct today — filtered
  rows cannot exist before a trigger has polled — but a customer who later
  disconnects their mailbox loses the only route to their historical filtered
  rows, and to the Release button on them. The section also identifies a
  skipped message by **UID rather than sender and subject**, which the design
  sketched: `inbox_events` deliberately holds no message content (that is why
  it needs no retention purge), so an admin decides whether to release on the
  strength of the rule that fired rather than of the message itself.
- **Retention is per-org and opt-in, so the default deployment still keeps
  everything forever** — `run_retention_days` defaults to NULL by design (an
  upgrade must delete nothing), and `BESTTEAM_RUN_RETENTION_DAYS` only supplies
  a default for *newly created* orgs. An operator who wants existing customers
  bounded has to set each one.
- **Alert delivery is in-app + one optional webhook per org, and nothing
  else** — no SMTP anywhere (by design), no per-user preferences, no digests
  or quiet hours, and one webhook URL per org rather than per fault kind.
  Anything finer is speculative until a customer asks. A webhook that fails
  five times is marked `failed` and stays visible in-app; nothing retries it
  afterwards and nothing tells the admin their webhook is broken except that
  row's own state.
- **Draft idempotency is process-wide, not deployment-wide** — the per-source-key
  lock closes the duplicate-draft race between a watchdog-released worker and
  its retry only because both are threads of one uvicorn process. A
  multi-worker deployment reopens the window; closing it needs the
  DB-authoritative overlap guard already listed above.
- **A Microsoft 365 secret's expiry date is admin-entered and unverified** —
  nothing checks it against Entra, so a wrong or stale date warns at the wrong
  time or not at all. Reading the real value needs `Application.Read.All`, a
  directory-wide read over every app registration in the customer's tenant,
  which is far broader than the single-mailbox `IMAP.AccessAsApp` the
  connection itself uses — a permission a customer's IT should refuse.
- **Draft outcomes are never observed** — nothing records whether a human sent,
  edited or discarded a generated draft, so there is no quality signal and no
  ROI evidence. The `X-BestTeam-Source-Key` header added in Phase 0 is what a
  future Sent-folder reconciliation would key on.

- **Deleting a knowledge base has no interlock with an in-flight ingestion
  job.** `crud.py`'s delete calls `delete_kb_ingestion_data` + `rmtree`, but
  the ingestion worker thread keeps running and afterwards commits
  `KnowledgeDocument`/`KnowledgeChunk` rows against a `kb_id` /
  `ingestion_job_id` that no longer exist — orphan rows, silently, since FK
  enforcement is off. On Windows the same race also leaks the upload directory
  (`WinError 32`: `rmtree` against the worker's still-open read handle; the
  route logs and continues by design). Found while fixing the flaky tests
  (17 Aug 2026) and deliberately left alone there — it is a product
  concurrency gap, not a test bug.
- **`_active_kb_dir` (tests/test_crud_api.py) resolves the active KB version
  by `max(st_mtime)`**, which is theoretically ambiguous: Windows file
  timestamps come from a ~15.6 ms-granularity clock, so two version
  directories written close together can tie and `max` then picks arbitrarily.
  Never observed failing (uploads are separated by a full ingestion job) and
  shared by five tests. `IngestionJob.version` records the directory name and
  would make it exact.
- **Vector knowledge base retrieval is single-stage** — no query
  rewriting/expansion or reranking, no external vector store, no DMS
  connectors. See `src/bestteam/core/CLAUDE.md`.
- **Per-user memory recall is single-stage BM25** — no rerank/expansion.
  Semantic records get exact-dedup on write plus LLM-mediated near-duplicate/
  update resolution (M-08); procedural records still have no dedup/
  consolidation. Admin view/search/delete UI exists (`/api/memory`), but
  there's no manual add/edit and no retention/quota
  policy. `GET /api/memory/users` is unpaginated (CR-029, deferred P3): fine
  today (admin-only, opt-in, operator-provisioned accounts), but the
  shared-platform ceiling is the sum of memory-enabled users across all orgs
  — add a limit/cursor if a customer reaches ~hundreds. See `core/memory.py`.
  Memory is now org- **and** principal-scoped (SP-2 + deletion-lifecycle,
  PR #42): account deletion retires the principal and purges atomically, and
  the store's write-fence drops any in-flight run's late write, closing the
  prior cross-process gap. Remaining: durable authoritative memory-store
  state, and pre-SP-2/pre-principal legacy rows with no recorded provenance
  still need a one-time operator sweep — tracked in
  `docs/MEMORY_REVIEW_TRIAGE.md`.
- **`RunRegistry` remains the in-memory *live* layer, not rehydrated from the
  DB** — a restart still loses in-flight/live run state. History no longer
  depends on that, though: `trace_events` are now persisted per run (`seq`-
  ordered) alongside `usage_records`, with a read API (`GET /api/runs`,
  `GET /api/runs/{id}/trace`) and cooperative cancellation (`POST
  /api/runs/{id}/cancel`). Growth is bounded (terminal-run eviction, see Done
  above). See `ui/backend/db/CLAUDE.md`.
- **`GET /api/runs` pagination has no frontend consumer yet** — the endpoint
  is bounded (`limit`/`offset`, default 50/max 200, `total` in the response)
  but the Activity page's Runs tab doesn't pass `limit`/`offset` or expose a
  "load more"/pager, so an org with more than 50 matching runs can't reach
  older ones from the UI today (flagged by independent review; accepted as a
  follow-up, not blocking, since the backend bound itself is the load-bearing
  fix). See `ui/backend/CLAUDE.md`.
- **Share-link tokens travel in URL paths** — `/share/:token` and
  `/api/share/{token}/...` put the secret in the path, so it lands in
  reverse-proxy/access logs and outbound `Referer` headers. Inherent to
  link-based sharing (the link *is* the credential), not a bug, but worth
  operational awareness: treat access logs for these routes as containing
  credentials, and rely on revoke/expiry rather than log hygiene.
- **Visitor transcripts have no retention or deletion policy** —
  `share_sessions`/`share_messages` accumulate indefinitely, and an org user
  can read a session's transcript (Activity → Shared) but has no way to
  delete one. Revoking a link stops new turns; it doesn't remove history.
  Needs a retention/erasure story before a customer with a real privacy
  policy uses this at volume.
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

- **Property Maintenance Inbox retry doesn't pre-check every UID still exists
  in the mailbox** — `POST /api/runs/{id}/retry` revalidates UIDVALIDITY (the
  mailbox wasn't rebuilt/migrated) but not that each individual UID is still
  present; a since-deleted message degrades gracefully (`email_read` returns
  "no message found" for that id, scoped tools refuse ids outside the batch)
  rather than erroring the retry. Full per-UID existence pre-check deferred as
  an easy follow-up, not required for Release 1A's Definition of Done.

- **A `completed` run with per-item normalization errors has no retry path**
  — `retry_triggered_run` only accepts `status == "failed"` (tightened by the
  review hardening above, to close a duplicate-draft risk on a `completed`
  run that already has real mailbox side effects). A run whose model output
  failed *normalization* (invalid JSON/enum) still ends `completed` with every
  item as a synthetic `error` row, and currently has no safe one-click retry
  — doing that safely needs the backend to prove no real draft side effect
  already happened for that batch (the `confirmed_draft_message_ids`
  mechanism added above could back this, but isn't wired into retry
  eligibility yet). Deferred as a follow-up, not implemented speculatively
  (Codex review finding).

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
- Phase 5 (partially done): `runs`/`trace_events` are now persisted with a
  history API; remaining — rehydrate `RunRegistry`'s live layer from the DB
  across restarts, and a frontend pager/"load more" for `GET /api/runs`.
- Phase 6: multi-customer update-distribution strategy (see
  `team_builder_methodology.md`).
- Refresh `ui/frontend/CLAUDE.md` to describe the current 4-stage wizard.
