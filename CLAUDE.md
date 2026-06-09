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
   public API.
2. **CLI** (`src/bestteam/cli/`) — Typer + Rich. Commands: `init` (scaffold a
   project), `run` (execute a YAML workflow), `graph` (render Mermaid).
   Thin wrappers over `load_workflow()` + `Workflow.run()/.visualize()`.
3. **UI** (`ui/`) — runtime monitoring dashboard. FastAPI + WebSocket backend
   (`ui/backend/`) reuses the SDK directly (no duplicated logic); React +
   Vite frontend (`ui/frontend/`) streams live agent trace events.

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
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
cd ui\frontend && npm run dev   # http://localhost:5173, talks to backend on :8000
```

## Key design decisions worth knowing

- **State reducer for parallel agents**: `_TeamState.contributions` uses
  `Annotated[Dict[str, str], operator.or_]` so concurrent node writes merge
  instead of raising `InvalidUpdateError`.
- **`fake:<response>` model spec**: a custom convenience added to
  `_resolve_model()` so YAML workflows can declare zero-cost,
  deterministic dry-run models (`FakeListChatModel` under the hood) without
  needing real API keys. Use this for demos/tests — see
  `ui/backend/workflows/*.yaml`.
- **Preserve `BestTeamError` subtypes through adapter boundaries**: always
  `except BestTeamError: raise` *before* a broad `except Exception` in
  adapter code, otherwise `ConfigurationError`/etc. get masked as a generic
  `EngineError` and the real cause is lost.
- **Sync-to-async streaming bridge**: `Workflow.stream()` /
  `compiled.stream()` are blocking generators. The FastAPI backend runs them
  in a `ThreadPoolExecutor` and hands events back to the event loop via
  `loop.call_soon_threadsafe(registry.publish, ...)`.

## Built-in tools (`src/bestteam/tools/`)

Four ready-made tools clients can attach directly to any Agent:

| Tool | Import | Env var required | Extra dep |
|---|---|---|---|
| `web_search(query, max_results=5)` | `from bestteam import web_search` | `TAVILY_API_KEY` | `pip install 'bestteam[tools-search]'` |
| `parse_file(path)` | `from bestteam import parse_file` | — | `pip install 'bestteam[tools-files]'` (PDF/Excel only) |
| `http_get(url, headers_json="{}")` | `from bestteam import http_get` | — | `httpx` (indirect dep via FastAPI) |
| `calculator(expression)` | `from bestteam import calculator` | — | none (stdlib only) |

**Code usage:**
```python
from bestteam import Agent, calculator, web_search
agent = Agent(..., tools=[web_search, calculator])
```

**YAML usage** — tools are referenced by name; loader resolves them via `tools.REGISTRY`:
```yaml
agents:
  - name: researcher
    role: Research Analyst
    goal: Find the latest news on a topic
    model: "openai:gpt-4o-mini"
    tools: [web_search, calculator]
```

Tier 2 tools (SQL executor, Python sandbox) and email integration are planned but not yet implemented.

## Known limitations / unimplemented extension points

These are intentionally abstracted behind interfaces but **not yet
implemented** — don't assume they exist:

- **Vector memory**: `core/memory.py` defines an abstract `Memory`
  (`remember`/`recall`); the only implementation is `InMemoryStore` (a plain
  dict). No embeddings or vector store (Chroma/FAISS/Pinecone) are wired up.
- **Persistent run state**: `ui/backend/registry.py`'s `RunRegistry` is
  in-process memory only — runs vanish on restart. Designed to be swapped
  for a Redis/Postgres-backed implementation behind the same interface.
- **General-purpose cache**: only local caches exist (`_workflow_cache` in
  `ui/backend/main.py`, `Workflow._compiled`) — no shared/cross-request cache
  layer.
- **CrewAI adapter, hierarchical/debate collaboration modes, deployment
  templates**: planned on the roadmap, not started.

## Testing notes

- All current tests use `FakeListChatModel` / `fake:` specs — zero API cost,
  deterministic. A live-model example (`examples/code_review_demo_live.py`,
  `ChatOpenAI`-based) exists but requires real API quota to run.
