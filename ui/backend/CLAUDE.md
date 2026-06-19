# bestteam — `ui/backend/` (FastAPI backend)

Directory-scoped notes for the FastAPI + WebSocket backend. See the root
`CLAUDE.md` for project overview, architecture, and commands; see
`ui/backend/db/CLAUDE.md` for the persistence schema and
`ui/frontend/CLAUDE.md` for the React frontend this API serves.

## Sync-to-async streaming bridge

`Workflow.stream()` / `compiled.stream()` are blocking generators. The
FastAPI backend runs them in a `ThreadPoolExecutor` and hands events back to
the event loop via `loop.call_soon_threadsafe(queue.put_nowait, ...)`.
Each subscriber's `asyncio.Queue` is paired with the event loop captured at
`registry.subscribe()` time -- i.e. the WebSocket handler's own loop, which
stays alive for as long as that connection is open -- rather than the loop
of the `POST /api/runs` request that started the run, which is gone by the
time the background thread finishes. An earlier version captured the
request's loop at run-creation time instead; under `TestClient`'s
per-request ephemeral loops that loop was already torn down by the time the
worker thread's callback ran, so `publish()` silently never happened and
the WebSocket handler's `queue.get()` blocked forever. (A `queue.SimpleQueue`
+ `asyncio.to_thread(queue.get)` variant was tried next and rejected: a
blocking `to_thread` call isn't cancellable, so it hung the same way when a
client disconnected before the run finished.)

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
  `DELETE` for `agents`/`teams`/`knowledge_bases`/`skills` (validated as standalone
  components via `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`/`SkillSpec` — field shape
  only; `agents`/`teams`/`skills` are not cross-referenced into any workflow, but
  `knowledge_bases` are resolvable by name from a workflow's `tools:` list via
  `ui/backend/knowledge_bases.py::load_knowledge_base_tools`) and `workflows` (a complete
  `Specification.to_raw()`-shaped dict, validated via `_build_workflow()`
  exactly like the wizard's Specification stage).
- **`_get_workflow()`** (`main.py`) now checks for a `WorkflowRecord` in the
  DB first (cached on `updated_at`) and falls back to
  `WORKFLOWS_DIR/<name>.yaml` (cached on mtime) — so a workflow
  deployed via the wizard or edited via `/api/config/workflows` is
  immediately runnable through `/api/runs`, alongside the YAML demo
  workflows.

## Auth, model catalog, and usage metering (Phase 3)

- **`ui/backend/auth.py`** — stdlib-only password hashing (PBKDF2-HMAC-SHA256,
  260,000 iterations, `pbkdf2_sha256$<iterations>$<salt>$<hash>`) and
  JWT-shaped bearer tokens (`create_access_token`/`decode_access_token`,
  HS256-equivalent via `hmac`, `sub`+`exp` claims, `AuthError` on
  malformed/tampered/expired tokens). No `passlib`/`PyJWT`/`bcrypt` dependency.
  `SECRET_KEY` and `ACCESS_TOKEN_EXPIRE_MINUTES` come from
  `BESTTEAM_SECRET_KEY` / `BESTTEAM_ACCESS_TOKEN_EXPIRE_MINUTES` env vars
  (defaults: a dev-only secret, 1440 minutes).
- **`ui/backend/auth_api.py`** (`/api/auth`) — `POST /register`,
  `POST /login` (both return `{access_token, token_type}`), `GET /me`
  (requires `Authorization: Bearer <token>`). Exports `get_current_user`, a
  FastAPI dependency resolving the bearer token to a `User` row, applied via
  router-level `dependencies=[Depends(get_current_user)]` to
  `/api/builder/sessions` (`builder.py`) and `/api/config/*` (`crud.py`), and
  per-endpoint to `/api/workflows`, `/api/workflows/{name}/graph`,
  `/api/runs` (POST), and `/api/runs/{id}` (GET) in `main.py`. `/api/health`
  and `/api/auth/*` stay public;
  `/api/runs/{run_id}/stream` requires the same bearer token passed as a
  `?token=` query parameter (browsers can't set custom headers when opening a
  WebSocket), validated with the same `decode_access_token`/
  `get_user_by_username` logic as `get_current_user`.
- **Model catalog** (`ui/backend/db/model_catalog.py` + `/api/config/model-catalog`
  CRUD in `crud.py`) — `to_prompt_text(entries)` renders the catalog for the
  Solution Architect's prompt. `builder.py::_with_model_catalog(db, text)`
  appends this to the requirements text before `generate_specification()` (in
  both `submit_specification` and `submit_solution_feedback`'s `model=`
  paths), so the architect picks `AgentSpec.model` specs by role complexity
  and pricing rather than guessing provider names.
- **Skills library** (`ui/backend/skills.py` + `/api/config/skills` CRUD in `crud.py`)
  — `load_skills(db)` queries all `SkillRecord` rows and returns `Dict[str, SkillSpec]`
  keyed by name, used by `main.py`, `crud.py`, and `builder.py` to pass `extra_skills=`
  to `_build_workflow()` (returns `{}` when no skills exist, backward compatible).
  `builder.py::_with_skill_catalog(db, text)` appends "Available skills..." list
  (name/description/tools) to the requirements text before `generate_specification()`,
  so the Solution Architect knows what skills exist for assignment to agents.
- **Usage metering** — `core/trace.py::TraceEvent.usage` is a
  `List[Dict[str, Any]]` of `{"model", "input_tokens", "output_tokens"}`
  entries, populated by `adapters/langgraph_adapter.py::_record_usage()`
  whenever a model response has `usage_metadata` (real provider models;
  `fake:` models leave it empty). For `HIERARCHICAL` teams, the manager and
  all delegated subordinates share one `usage_sink` per turn, so the total
  surfaces on the manager's single `agent_completed` event.
  `ui/backend/runtime.py::run_in_background(run_id, workflow, input,
  engine=None)` — if `engine` is given (callers pass `db.get_bind()` so tests
  using an overridden in-memory DB still work), opens its own `Session` and
  calls `db/usage.py::record_usage()` for each `usage` entry on every
  `agent_completed` event, computing `cost_estimate` from `model_catalog` when
  the model spec matches a catalog entry (`None` otherwise).

## Known limitation: general-purpose cache

Only local caches exist (`_workflow_cache` in `ui/backend/main.py`,
`Workflow._compiled`) — no shared/cross-request cache layer.
