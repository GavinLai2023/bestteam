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

## In Progress

- _Nothing actively in progress._ See "Next steps / roadmap" below.

## Known issues / tech debt

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
