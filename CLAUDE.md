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
- `src/bestteam/tools/CLAUDE.md` — built-in tools (`web_search`, `parse_file`,
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
- CI is split into 6 jobs: 4 fast PR-gate jobs
  (`backend-unit-integration`, `backend-optional-deps`, `frontend`,
  `e2e-smoke`) run on every PR/push; 2 full-regression jobs
  (`backend-full`, `e2e-full`) are gated to `main` only, and are also
  manually dispatchable (`workflow_dispatch`) for a pre-merge run from a
  feature branch.
- `fake-architect:` is a deterministic drop-in model for E2E coverage of
  the Team Builder wizard's AI-generation steps, and is deliberately never
  present in `DEFAULT_MODEL_CATALOG`.
