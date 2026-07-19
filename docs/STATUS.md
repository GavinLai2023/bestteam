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
- **`RunRegistry` remains the in-memory live layer** — a `runs` row is now
  persisted per run (CR-012) so usage/trace foreign keys are valid, but
  `trace_events` persistence, restart recovery, and a run-history API remain
  Phase 5. See `ui/backend/db/CLAUDE.md`.
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
