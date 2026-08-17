# bestteam

A commercial multi-agent framework wrapper around LangGraph (package name
`bestteam`). Philosophy: **把复杂留给自己，把简单留给客户** — wrap LangGraph's
power behind a simple, business-friendly surface so clients write workflows,
not orchestration code.

## Architecture (three layers)

1. **SDK** (`src/bestteam/`) — `Agent`/`Team`/`Workflow` business-facing
   dataclasses, decoupled from the engine via an `EngineAdapter` ABC
   (`adapters/base.py`). The only implementation today is
   `LangGraphAdapter` (`adapters/langgraph_adapter.py`); the seam exists so a
   CrewAI (or other) adapter could be added later without touching the
   public API. Details: `src/bestteam/CLAUDE.md`.
2. **CLI** (`src/bestteam/cli/`) — Typer + Rich. Commands: `init` (scaffold a
   project), `run` (execute a YAML workflow), `graph` (render Mermaid).
   Thin wrappers over `load_workflow()` + `Workflow.run()/.visualize()`.
3. **UI** (`ui/`) — runtime monitoring dashboard *and* a guided "Team
   Builder" wizard for non-technical customers. FastAPI + WebSocket backend
   (`ui/backend/`) reuses the SDK directly (no duplicated logic); React +
   Vite frontend (`ui/frontend/`) streams live agent trace events and drives
   the wizard. Details: `ui/backend/CLAUDE.md`, `ui/frontend/CLAUDE.md`.

Workflows are declarative YAML (parsed by `core/loader.py`) — see
`ui/backend/workflows/*.yaml` for examples of sequential and parallel
collaboration modes.

## Common commands

Run everything through the project venv (`./.venv/Scripts/python.exe` on
Windows).

```powershell
# Tests
.\.venv\Scripts\python.exe -m pytest

# CLI: scaffold / run / visualize a workflow
.\.venv\Scripts\python.exe -m bestteam init my_project
.\.venv\Scripts\python.exe -m bestteam run workflow.yaml "some input"
.\.venv\Scripts\python.exe -m bestteam graph workflow.yaml

# Monitoring dashboard — needs BOTH running simultaneously
$env:BESTTEAM_SECRET_KEY = "dev-only-secret-change-me-for-real-use"
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
cd ui\frontend && npm run dev   # http://localhost:5173, talks to backend on :8000
```

## Where to find more

This file covers project-wide context. Claude Code automatically loads the
relevant file below the first time it reads a file in that directory:

- `src/bestteam/CLAUDE.md` — SDK core: `Agent`/`Team`/`Workflow`, the
  `EngineAdapter`/`LangGraphAdapter` seam, state-reducer / `fake:` model spec
  / error-handling design decisions, HIERARCHICAL collaboration mode.
- `src/bestteam/core/CLAUDE.md` — `Specification`/`Requirements` structured
  outputs for the Team Builder, and the `local_folder`/`vector` knowledge
  base implementations.
- `src/bestteam/tools/CLAUDE.md` — built-in tools (`web_search`,
  `local_business_search`, `parse_file`,
  `http_get`, `calculator`, and the draft-only `email_find`/`email_read`/
  `email_draft_reply` toolkit) and their trust boundaries.
- `ui/backend/CLAUDE.md` — FastAPI backend: builder/config APIs, auth, model
  catalog, usage metering, sync-to-async streaming bridge.
- `ui/backend/db/CLAUDE.md` — SQLAlchemy persistence schema.
- `ui/frontend/CLAUDE.md` — React/Vite monitoring dashboard and Team Builder
  wizard, including the login UI.
- `docs/ARCHITECTURE.md` — 5-minute orientation: architecture diagram,
  module map, tech stack and rationale.
- `docs/STATUS.md` — living kanban: done / in progress / known issues and
  tech debt / next steps.
- `docs/DECISIONS.md` — why significant decisions were made (e.g. LangGraph
  vs CrewAI, org-scoped multi-tenancy), so they aren't re-litigated.

## Known limitations / unimplemented extension points

These are intentionally abstracted behind interfaces but **not yet
implemented** — don't assume they exist:

- **Knowledge bases have no external vector store, no DMS connectors.**
  Three types are supported: `local_folder` (BM25), `vector` (cosine), and
  `hybrid` (BM25 + vector, RRF-fused). All three support opt-in query
  expansion (`query_expansion_model`/`query_expansion_count`, MultiQueryRetriever-style,
  unmetered) and opt-in reranking (`rerank_model`/`candidate_k`) —
  see `src/bestteam/core/CLAUDE.md`.
- **Per-user memory recall is BM25-only by default; opt-in hybrid (BM25 +
  vector, RRF-fused, with type-aware recency decay) is available via
  `BESTTEAM_MEMORY_EMBEDDING_MODEL`** — still no query rewriting/expansion or
  reranking either way. Semantic records get exact-dedup on write plus
  LLM-mediated near-duplicate/update resolution; procedural records still
  have no dedup/consolidation.
  Admins can view/search/delete a user's memory via the admin-only Memory page
  (`/api/memory`); there's no manual add/edit and no retention/quota policy. Disabled by default
  (`BESTTEAM_MEMORY_DB`) — see `core/memory.py` and `src/bestteam/core/CLAUDE.md`.
- **Alerting is in-app + one optional per-org webhook, and nothing else.**
  No SMTP anywhere (deliberate: the draft-only toolkit's containment argument
  is that there is no send verb in the process), no per-user preferences, no
  digests. A trigger raises a notification on a health *transition*
  (`ui/backend/trigger_health.py`), never per occurrence.
- **Run history has retention, but erasure by data subject does not exist.**
  Each org sets a period (`org_retention_settings`, NULL = keep forever by
  default) and a purge clears *content* — `runs.input`/`output`, the run's
  `trace_events`, an `automation_item_results.payload` — while keeping
  *accounting*: the `runs` row, `usage_records`, and an item result's
  `status`/`source_key` (clearing those would make a sweep cause duplicate
  drafts on retry). Deleting everything about one email address is **not**
  offered and won't be: the address is only in free text the model may have
  paraphrased. A purge is also not a secure erase — no `VACUUM`. See
  `ui/backend/retention.py`.
- **Live run state (`RunRegistry`) isn't rehydrated from the DB on restart**:
  a killed/restarted process still loses in-flight/live run state. History no
  longer disappears, though — every `trace_events` row is now persisted per
  run alongside `usage_records`, with a read API (`GET /api/runs`,
  `GET /api/runs/{id}/trace`) and cooperative cancellation
  (`POST /api/runs/{id}/cancel`) — see `ui/backend/db/CLAUDE.md`.
- **General-purpose cache**: only local per-process caches exist — see
  `ui/backend/CLAUDE.md`.
- **CrewAI adapter, DEBATE collaboration mode, deployment templates**:
  planned, not started (HIERARCHICAL is implemented) — see
  `src/bestteam/CLAUDE.md`.

## Testing notes

- All current tests use `FakeListChatModel` / `fake:` specs — zero API cost,
  deterministic. Live-model examples (`examples/code_review_demo_live.py` and
  `ui/backend/workflows/vector_knowledge_base_demo_live.yaml`, both
  `ChatOpenAI`/OpenAI-embeddings-based) exist but require real API quota to
  run.
- Every test file needs a `pytestmark` (`unit`/`integration`/`e2e`/
  `optional`, optionally also `slow`) — `tests/test_marker_completeness.py`
  fails the suite if any collected test carries none of those markers, so a
  new test file can't silently fall outside every CI job's `-m` selection.
- Running the E2E suite requires the `test` extra
  (`pip install -e ".[ui,dev,tools,test]"`), `playwright install chromium`,
  and `npm` on PATH — `tests/e2e/conftest.py`'s fixture spawns real
  `uvicorn`/`vite` dev-server subprocesses. It needs ports 8000 and 5173
  free; it now fails loudly (naming the conflicting port) rather than
  silently attaching to a developer's own running dev stack if they're not.
- **Password hashing is deliberately cheap in-process.** `tests/conftest.py`
  lowers `ui.backend.auth._PBKDF2_ITERATIONS` to 1,000 for the test process:
  at the production 260,000 it was 543 of the suite's 789 seconds (69%).
  Production code is untouched — there is no env var or config key, so the
  real iteration count cannot be misconfigured in a deployment, and
  `test_auth.py::test_production_pbkdf2_iterations_are_unchanged` reads the
  literal out of `auth.py`'s source so a weakened default still fails.
  `tests/e2e/` drives a real uvicorn subprocess that never imports conftest,
  so that tier still exercises genuine 260k hashing.
- **`-n auto` for a fast local run** (`pytest-xdist`, in the `dev` extra):
  ~2m11s vs ~3m17s serial. Not in `addopts` on purpose — it breaks `-x`,
  `--pdb` and readable tracebacks, so plain `pytest` stays serial and
  debuggable. Never use it on `tests/e2e/` (fixed ports 8000/5173).
- CI is 6 jobs plus a `changes` job that path-filters them. Note what it
  filters against: on a **push** it compares with the previous commit, so a
  docs-only commit to `main` runs nothing; on a **pull request** it compares
  with the base branch, so the whole PR's diff decides — a docs-only commit
  on top of a PR that touches backend still runs the backend jobs, which is
  the point. The jobs: 4 PR-gate (`backend-unit-integration` — under
  `-n auto`, `backend-optional-deps`, `frontend`, `e2e-smoke`) on every
  PR/push; 2 gated to `main` (`backend-full`, `e2e-full`). Both guard on
  `github.ref == 'refs/heads/main'`, which is the *ref*, not the event — so
  `workflow_dispatch` from a feature branch does **not** enable them (verified
  empirically: dispatched on `feat/email-phase-0-hardening`, both still
  skipped). To get that coverage before merging, run the same command locally:
  `python -m pytest -m "not e2e"`, serial, no `-n auto`.
  `backend-full` runs **serially and in one process on purpose** —
  with the PR gate distributed and out of order, it is what still catches
  ordering and cross-test isolation bugs. The path filters are allowlists;
  adding a new top-level directory means adding it there too.
- `fake-architect:` is a deterministic drop-in model for E2E coverage of
  the Team Builder wizard's AI-generation steps, and is deliberately never
  present in `DEFAULT_MODEL_CATALOG`.
