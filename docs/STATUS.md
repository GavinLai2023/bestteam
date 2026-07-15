# Project status

> **Living doc.** Update **Done** / **In Progress** / **Next steps** when
> you finish or start meaningful work, so this stays a true snapshot of
> "where are we now".

## Done

- SDK core: `Agent`/`Team`/`Workflow`, `EngineAdapter` ABC, `LangGraphAdapter`,
  SEQUENTIAL/PARALLEL/HIERARCHICAL collaboration modes.
- CLI: `init` / `run` / `graph`.
- YAML loader, including `local_folder` (BM25) and `vector` knowledge bases.
- Built-in tools: `web_search`, `parse_file`, `http_get`, `calculator`.
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

## In Progress

- Email toolkit (branch `feat/email-toolkit`): three draft-only built-in
  tools — `email_find` / `email_read` / `email_draft_reply` — over one
  env-configured mailbox, with MS Graph (M365/Exchange Online, app-only
  OAuth, `createReply`) and generic IMAP (stdlib, `BODY.PEEK`, threaded
  MIME draft APPENDed to Drafts) backends behind one seam
  (`src/bestteam/tools/email_client.py`, `bestteam[tools-email]`). No send
  verb / no SMTP by design — drafts are reviewed and sent by a human from
  their own mail client. Ships with a seeded `email_triage_reply` built-in
  Skill (`ui/backend/skills.py::seed_default_skills`, per-row
  seed-if-absent) so Team Builder customers just pick the skill. Deferred:
  real sending, ambient run-on-new-mail, per-user OAuth/secrets store,
  attachments. Spec:
  `docs/superpowers/specs/2026-07-15-email-toolkit-design.md`.

## Known issues / tech debt

- **Vector knowledge base retrieval is single-stage** — no query
  rewriting/expansion or reranking, no external vector store, no DMS
  connectors. See `src/bestteam/core/CLAUDE.md`.
- **Per-user memory recall is single-stage BM25** — no rerank/expansion;
  semantic/procedural records have no auto-dedup. Admin view/search/delete UI
  exists (`/api/memory`), but there's no manual add/edit and no retention/quota
  policy. See `core/memory.py`.
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

## Next steps / roadmap

- CrewAI adapter, DEBATE collaboration mode, deployment templates — all
  "planned, not started" (see `DECISIONS.md` for why CrewAI isn't the
  current engine).
- Phase 5: wire `RunRegistry` to persistent `runs`/`trace_events`.
- Phase 6: multi-customer update-distribution strategy (see
  `team_builder_methodology.md`).
- Refresh `ui/frontend/CLAUDE.md` to describe the current 4-stage wizard.
