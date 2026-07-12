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
  `http_get`, `calculator`) and their trust boundaries.
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
  vs CrewAI, no multi-tenancy), so they aren't re-litigated.

## Known limitations / unimplemented extension points

These are intentionally abstracted behind interfaces but **not yet
implemented** — don't assume they exist:

- **Vector knowledge base retrieval is single-stage** (no query
  rewriting/expansion or reranking, no external vector store, no DMS
  connectors) — see `src/bestteam/core/CLAUDE.md`.
- **Per-user memory recall is single-stage BM25** (no rerank/expansion),
  semantic/procedural records have no auto-dedup, and there's no frontend
  memory-management UI. Disabled by default (`BESTTEAM_MEMORY_DB`) — see
  `core/memory.py` and `src/bestteam/core/CLAUDE.md`.
- **Persistent run state**: `RunRegistry` is in-memory only, not yet wired
  to the `runs`/`trace_events` tables — see `ui/backend/db/CLAUDE.md`.
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
