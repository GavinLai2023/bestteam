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
- **Specification = loader schema + wizard-only friendly fields**
  (`core/specification.py`): `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`/
  `WorkflowSpec`/`Specification` are pydantic models that mirror the YAML
  loader's raw dict (see `core/loader.py::_build_workflow`), plus
  presentation-only fields (`display_name`, `friendly_description`) that
  `to_raw()` strips before validation. `validate_specification()` compiles
  the stripped dict via `_build_workflow()` and raises `ConfigurationError`
  on an invalid design. `generate_specification()` drives a "Solution
  Architect" model via `with_structured_output(Specification)` and
  self-corrects on `ConfigurationError` (up to `max_attempts`) — the
  Specification-stage engine described in
  `docs/team_builder_methodology.md`.
- **Requirements = Business Analyst's structured output** (`core/requirements.py`):
  `Requirements` (summary/pain_points/goals/success_criteria/constraints/
  clarifying_questions) is the Requirements-stage counterpart to
  `Specification`. `generate_requirements(model, intent_text, as_is_text,
  feedback=...)` calls `model.with_structured_output(Requirements)` —
  no `_build_workflow` validation applies at this stage (it's a plain-language
  summary, not yet a team design). `Requirements.to_prompt()` renders it as
  text for the Solution Architect's `requirements` argument.

## Built-in tools (`src/bestteam/tools/`)

Four ready-made tools clients can attach directly to any Agent:

| Tool | Import | Env var required | Extra dep |
|---|---|---|---|
| `web_search(query, max_results=5)` | `from bestteam import web_search` | `TAVILY_API_KEY` | `pip install 'bestteam[tools-search]'` |
| `parse_file(path)` | `from bestteam import parse_file` | — | `pip install 'bestteam[tools-files]'` (PDF/Excel/Word) |
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

**Trust boundaries**: `parse_file` reads any local path it's given (no
sandboxing) and `http_get` fetches any URL whose host doesn't resolve to a
private/internal address (see `_check_host_allowed` in `http_client.py`).
Both are intentional — the tools' purpose is to read files / fetch URLs the
agent is told to — but callers exposing these tools to an LLM agent are
responsible for constraining which paths/URLs the agent can be prompted to
access.

Tier 2 tools (SQL executor, Python sandbox) and email integration are planned but not yet implemented.

## Knowledge bases (`core/knowledge_base.py`, `core/vector_knowledge_base.py`)

The most common client request is "connect our agents to the client's
knowledge base." The loader supports two knowledge base `type:`s, both
backed by a folder of documents (`tools.parse_file` + chunking):

- `local_folder` (default, `core/knowledge_base.py`): indexes chunks in
  memory with **BM25 keyword search** (`rank-bm25`). No API key, no vector
  store, no persistence. Best for the common case — a handful to a couple
  dozen documents.
- `vector` (`core/vector_knowledge_base.py`): embeds each chunk and ranks by
  **cosine similarity** for semantic search (e.g. a query about "refunds"
  matches a chunk that says "money back" with no shared keywords). Retrieval
  is single-stage — no query rewriting/expansion and no reranking (see
  "Known limitations").

Both expose the resulting knowledge base to agents as an ordinary tool (named
after the KB), so it slots into the existing `tools:` / `REGISTRY` mechanism
with no `LangGraphAdapter` changes — `query()` returns the same formatted
`"...results for: <query>\n\n1. [source: ...]\n<text>..."` / `"No results
found..."` string shape regardless of type.

**YAML usage — `local_folder`:**
```yaml
knowledge_bases:
  - name: product_docs
    path: ./docs/product   # relative to the workflow YAML's directory
    # optional: chunk_size (default 1000), chunk_overlap (default 100), top_k (default 5)

agents:
  - name: support_agent
    role: Support Specialist
    goal: Answer customer questions using the product documentation
    model: "openai:gpt-4o-mini"
    tools: [product_docs, calculator]
```

Requires `pip install 'bestteam[tools-rag]'`. See
`ui/backend/workflows/knowledge_base_demo.yaml` for a runnable example.

**YAML usage — `vector`:**
```yaml
knowledge_bases:
  - name: product_docs
    type: vector
    path: ./docs/product
    embedding_model: "openai:text-embedding-3-small"  # or "fake:<dim>" for $0 dry runs
    # optional: chunk_size (default 1000), chunk_overlap (default 100), top_k (default 5)
    # optional: score_threshold — cosine-similarity cutoff in [-1, 1]; if set and no
    #           chunk meets it, query() returns the same "No results found" message
    # optional: cache_path — JSON file persisting per-chunk embeddings (keyed by a
    #           sha256 of the embedding-model spec + chunk text) across runs, so
    #           load_workflow() doesn't re-embed unchanged chunks every time. Only
    #           applies when embedding_model is a string spec; if you pass a live
    #           Embeddings instance, caching is skipped with a warning. Resolved
    #           relative to the workflow YAML's directory, like `path`.
    cache_path: ./.bestteam_cache/product_docs.json
```

`embedding_model` is resolved like `Agent.model`: a `langchain_core.embeddings.Embeddings`
instance is used as-is, `"fake:<dim>"` (dim optional, default 32) gives a $0
deterministic embedding for dry runs/tests, and other provider strings (e.g.
`"openai:..."`) are resolved via `langchain.embeddings.init_embeddings`
(requires `pip install langchain`). Requires `pip install 'bestteam[tools-rag-vector]'`
(numpy). See `ui/backend/workflows/vector_knowledge_base_demo.yaml` for a $0
dry-run example using `"fake:"` specs, or
`ui/backend/workflows/vector_knowledge_base_demo_live.yaml` for the same
workflow wired to real OpenAI embeddings + chat model
(`text-embedding-3-small` + `gpt-4o-mini`), which demonstrates true semantic
retrieval (e.g. matching "money back" queries to a "refund" policy doc with
no shared keywords). The live variant requires `OPENAI_API_KEY`.

## Persistence layer (`ui/backend/db/`)

Per-deployment SQLite database via SQLAlchemy 2.0 (`pip install
'bestteam[ui]'`). `db/models.py` defines the full Phase 1 schema from
`docs/team_builder_methodology.md`:

- `agents` / `teams` / `knowledge_bases` / `workflows` — each row's `config`
  is a JSON `raw` dict (the technical fields from `AgentSpec`/`TeamSpec`/
  `KnowledgeBaseSpec`/`Specification.to_raw()`, see `core/specification.py`);
  `workflows.status` tracks `draft` / `ready_for_testing` / `deployed`.
- `builder_sessions` — the wizard's session state machine. `status` is one
  of `intent | requirements | spec | solution | testing | deployed`
  (`db/builder_sessions.py::STATUSES`); `requirements_json`/
  `specification_json` hold the Business Analyst / Solution Architect
  agents' structured outputs; `feedback_history` is an append-only JSON list
  recording each round of customer feedback.
- `runs` / `trace_events` — persisted replacement for `RunRegistry`'s
  in-memory state (wired up in Phase 5).
- `usage_records` — per-agent token usage per run, for usage metering
  (Phase 3).
- `users` — simple per-deployment login (Phase 3).

`db/database.py` provides `make_engine(db_path)` (`":memory:"` uses a
`StaticPool` so all connections share one database — needed for tests/dry
runs), `init_db(engine)` (`Base.metadata.create_all`), and
`session_factory(engine)`. `db/builder_sessions.py` has the
`builder_sessions` CRUD (`create_session`/`get_session`/`update_session`/
`append_feedback`); CRUD for `agents`/`teams`/`knowledge_bases`/`workflows`
lives in `ui/backend/crud.py` (Phase 2, see "Backend API" below).
`ui/backend/db_session.py` wires up the per-deployment engine (default
`ui/backend/data/bestteam.db`, override with `BESTTEAM_DB_PATH`) and a
`get_db()` FastAPI dependency.

## Backend API (`ui/backend/`)

Beyond the existing monitoring endpoints (`/api/health`, `/api/workflows`,
`/api/workflows/{name}/graph`, `/api/runs`, the `/api/runs/{id}/stream`
WebSocket — all in `main.py`), Phase 2 adds two routers:

- **`builder.py`** (`/api/builder/sessions`) — the wizard's session
  state machine, a thin layer over `db/builder_sessions.py` plus
  `core/requirements.py` / `core/specification.py`:
  - `POST /` — start a session (Stage 1, Intent: `intent_text`/`as_is_text`).
  - `GET /{id}` — fetch session state.
  - `POST /{id}/requirements` — Stage 2: pass `requirements` (a confirmed/
    edited `Requirements` dict) to store directly, or `model` (+ optional
    `feedback`) to call `generate_requirements()`.
  - `POST /{id}/specification` — Stage 3: pass `specification` (a
    `Specification` dict, validated via `validate_specification()`) or
    `model` (+ optional `feedback`) to call `generate_specification()`
    against the session's requirements.
  - `POST /{id}/solution` — Stage 4: like `/specification`, but requires
    `feedback` and always records it via `append_feedback()`; with `model`,
    the current Specification + feedback are fed back to the architect.
  - `POST /{id}/test-runs` — Stage 5: validates `specification_json` and
    runs it through the same `RunRegistry`/`Workflow.stream()`/
    `ThreadPoolExecutor` machinery as `/api/runs` (factored into
    `ui/backend/runtime.py` so both routers can use it without a circular
    import).
  - `POST /{id}/deploy` — Stage 6: upserts a `WorkflowRecord` (`status=
    deployed`) from `specification.to_raw()` and marks the session
    `deployed`.
  - All generation endpoints (`model=...`) translate `BestTeamError` (e.g.
    an invalid spec the architect couldn't self-correct) to `400`, and any
    other exception (e.g. a real provider call without an API key) to `502`
    — see `_call_model()`.
- **`crud.py`** (`/api/config/...`) — the "advanced view": `GET`/`PUT`/
  `DELETE` for `agents`/`teams`/`knowledge_bases` (validated as standalone
  components via `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec` — field shape
  only, not cross-referenced into any workflow) and `workflows` (a complete
  `Specification.to_raw()`-shaped dict, validated via `_build_workflow()`
  exactly like the wizard's Specification stage).
- **`_get_workflow()`** (`main.py`) now checks for a `WorkflowRecord` in the
  DB first (cached on `updated_at`) and falls back to
  `WORKFLOWS_DIR/<name>.yaml` (cached on mtime) — so a workflow
  deployed via the wizard or edited via `/api/config/workflows` is
  immediately runnable through `/api/runs`, alongside the YAML demo
  workflows.

## Known limitations / unimplemented extension points

These are intentionally abstracted behind interfaces but **not yet
implemented** — don't assume they exist:

- **Vector knowledge base retrieval is single-stage**: `VectorKnowledgeBase`
  does cosine-similarity search only — no query rewriting/expansion (e.g.
  LLM-based rewrite or HyDE) and no reranking (cross-encoder or LLM-based
  re-scoring of an over-fetched candidate set). It's also in-memory plus an
  optional JSON embedding cache (`cache_path`) — no external vector store
  (Chroma/FAISS/Pinecone) and no hierarchical/"small-to-big" indexing. Without
  `cache_path`, every workflow load re-embeds all chunks (real embedding APIs
  incur cost/latency on each run). There's no DMS connector
  (SharePoint/Confluence/Google Drive) for either knowledge base type.
  `core/memory.py`'s `Memory` ABC (`remember`/`recall`) is similarly unused
  beyond the in-process `InMemoryStore`.
- **Persistent run state**: `ui/backend/registry.py`'s `RunRegistry` is
  still in-process memory only — runs vanish on restart. The SQLite schema
  to replace it (`runs`/`trace_events`, see "Persistence layer" below)
  exists but isn't wired into `RunRegistry` or `main.py` yet (Phase 5).
- **General-purpose cache**: only local caches exist (`_workflow_cache` in
  `ui/backend/main.py`, `Workflow._compiled`) — no shared/cross-request cache
  layer.
- **CrewAI adapter, DEBATE collaboration mode, deployment templates**:
  planned on the roadmap, not started. (HIERARCHICAL is implemented: the
  `manager` agent gets a `delegate_to_<name>(task)` tool per subordinate,
  bound alongside its own `tools` and run through the same tool-calling loop
  as SEQUENTIAL/PARALLEL agents — see `_hierarchical_node` in
  `adapters/langgraph_adapter.py`.)

## Testing notes

- All current tests use `FakeListChatModel` / `fake:` specs — zero API cost,
  deterministic. Live-model examples (`examples/code_review_demo_live.py` and
  `ui/backend/workflows/vector_knowledge_base_demo_live.yaml`, both
  `ChatOpenAI`/OpenAI-embeddings-based) exist but require real API quota to
  run.
