# bestteam — `ui/backend/` (FastAPI backend)

Directory-scoped notes for the FastAPI + WebSocket backend. See the root
`CLAUDE.md` for project overview, architecture, and commands; see
`ui/backend/db/CLAUDE.md` for the persistence schema and
`ui/frontend/CLAUDE.md` for the React frontend this API serves.

## Org multi-tenancy (row-level isolation)

One deployment can serve several customer **organizations** (see
`docs/DECISIONS.md`, "org-scoped multi-tenancy"; spec:
`docs/superpowers/specs/2026-07-15-org-multi-tenancy-design.md`). The rules
every endpoint follows:

- Org-owned rows carry `org_id`; org users only ever see their own org's
  data plus platform built-ins (`skills.org_id IS NULL`), and — only where
  `BESTTEAM_DEMO_WORKFLOWS` is on — the global YAML demo workflows.
  **Cross-org access is a 404** (and the WS stream closes 4404 ==
  unknown-run) — existence is never revealed.
- Scoping is centralized: `get_current_org` (auth_api),
  `load_skills(db, org_id)` (org's own shadows a same-named built-in),
  `load_knowledge_base_tools(..., org_id=)`, org-filtered queries in
  crud/builder/main. The workflow cache is keyed `(org_id, name)`; YAML
  demos cache under `(None, name)`.
- Component names are unique per `(org_id, name)`. KB upload dirs are
  `data/knowledge_base_uploads/<org_id>/<name>` (legacy un-prefixed dirs
  keep working — KB configs embed absolute paths).
- Admin surfaces (`/api/config`, `/api/memory`) are platform-wide: lists
  label each item's org and take an optional `?org=` filter; item routes
  require explicit `?org=<name>` (skills may omit it = built-in tier).
  **Platform admins are org-less accounts** (CR-030): `set_admin_status`
  refuses to promote org members, and `get_current_admin` + the run
  GET/stream passthrough require `is_admin AND org_id IS NULL` — an
  org-bound `is_admin` flag is never honored.
- Runs and usage_records carry `org_id` (denormalized — the future
  per-customer billing dimension); run GET/stream check org ownership with
  platform-admin read passthrough. Builder sessions are org-scoped. Runs
  also persist `username` — who started them (CR-032, audit-only; ownership
  stays org-level, and builder sandbox runs record it without a memory
  `user_id`).
- Process-wide email env vars (`BESTTEAM_EMAIL_*`) on a multi-org deployment
  are **refused, not just discouraged** (CR-031):
  `db/orgs.py::ensure_email_single_org` raises at backend startup and in the
  `create-org` CLI when `BESTTEAM_EMAIL_BACKEND` is set with more than one
  org. Per-org credentials are a future sub-project (encrypted secrets
  store).
- Memory stays keyed by globally-unique username (no org dimension needed).
- The isolation test net: `tests/test_org_isolation.py` plus per-surface
  tests in test_crud_api/test_ws_stream/test_builder_api.

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
- **`crud.py`** (`/api/config/...`) — the "advanced view" (operator-only):
  `GET`/`PUT`/`DELETE` for `knowledge_bases`/`skills` (validated as standalone
  components via `KnowledgeBaseSpec`/`SkillSpec` — field shape only; both are
  resolvable by name from a workflow, via `load_knowledge_base_tools` and
  `load_skills`) and `workflows` (a complete `Specification.to_raw()`-shaped
  dict carrying its own `agents:`/`teams:` inline, validated via
  `_build_workflow()` exactly like the wizard's Specification stage). Plus two
  read-only reference routes for the UI: `GET /orgs` (the org selector) and
  `GET /tools` (the built-in `bestteam.tools.REGISTRY`, name + docstring).
  **Standalone `agents`/`teams` CRUD was removed**: nothing consumed those
  records (`_build_workflow` takes only `extra_tools`/`extra_skills`), and both
  tables were empty everywhere. The models remain in `db/models.py`.
- **`_get_workflow()`** (`main.py`) checks for a `WorkflowRecord` in the DB
  first, within the caller's org (cached on `updated_at`), then falls back to
  `WORKFLOWS_DIR/<name>.yaml` (cached on mtime) — so a workflow deployed via
  the wizard or edited via `/api/config/workflows` is immediately runnable
  through `/api/runs`.
- **Demo YAML workflows are opt-in** (`main.py::demo_workflows_enabled`,
  `BESTTEAM_DEMO_WORKFLOWS`, **off by default**). The two workflow sources
  serve different audiences: YAML is the *SDK's* format (`load_workflow`,
  `bestteam run x.yaml`, unaffected by this flag and by the DB entirely),
  while DB rows are what the wizard creates per-org at runtime. The files in
  `WORKFLOWS_DIR` are our shipped fixtures — mostly `fake:` models returning
  hardcoded text, plus `*_live` ones that spend real quota and, for
  `email_triage_demo_live`, read the `BESTTEAM_EMAIL_*` mailbox — and they
  carry no `org_id`, so while enabled *every* org user sees and can run them.
  The gate covers **both** the list (`GET /api/workflows`) and resolution
  (`_get_workflow`, hence `/api/runs` and `/graph`): hiding them from the
  list alone would leave them runnable by name. Disabled ⇒ the same 404 as an
  unknown workflow.

## Auth, model catalog, and usage metering (Phase 3)

- **`ui/backend/auth.py`** — stdlib-only password hashing (PBKDF2-HMAC-SHA256,
  260,000 iterations, `pbkdf2_sha256$<iterations>$<salt>$<hash>`) and
  JWT-shaped bearer tokens (`create_access_token`/`decode_access_token`,
  HS256-equivalent via `hmac`, `sub`+`exp` claims, `AuthError` on
  malformed/tampered/expired tokens). No `passlib`/`PyJWT`/`bcrypt` dependency.
  `SECRET_KEY` and `ACCESS_TOKEN_EXPIRE_MINUTES` come from
  `BESTTEAM_SECRET_KEY` / `BESTTEAM_ACCESS_TOKEN_EXPIRE_MINUTES` env vars
  (defaults: a dev-only secret, 1440 minutes).
- **`ui/backend/auth_api.py`** (`/api/auth`) — `POST /login` (returns
  `{access_token, token_type}`), `GET /me` (requires `Authorization: Bearer
  <token>`; returns `{username, is_admin, org}`). **There is no public
  registration endpoint** — orgs, users, and admins are all provisioned via
  the operator CLI (`python -m ui.backend.admin create-org / create-user /
  promote`; tests use `tests/helpers.py::create_user_and_login`). Exports
  three dependencies: `get_current_user` (bearer → `User` row),
  `get_current_admin` (403s non-admins; router-level guards the admin-only
  `/api/config/*` and `/api/memory/*`), and `get_current_org` (the user's
  `Organization`; **403s platform operators** — org-NULL users — on org-user
  surfaces: workflows list/graph, `POST /api/runs`, the builder router).
  Admin is granted only via the operator CLI, never from a username match,
  and never read at import (a DB predating the migrations still boots; the
  module-level seeding likewise warns-and-skips on a pre-migration schema).
  `/api/health` and `/api/auth/*` stay public; the run stream WebSocket
  authenticates with a single-use `?ticket=` (see the runs section).
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
  engine=None, user_id=None)` — if `engine` is given (callers pass
  `db.get_bind()` so tests using an overridden in-memory DB still work), opens
  its own `Session` and calls `db/usage.py::record_usage()` for each `usage`
  entry on every `agent_completed` event, computing `cost_estimate` from
  `model_catalog` when the model spec matches a catalog entry (`None` otherwise).

## Per-user memory

`ui/backend/runtime.py::_make_memory()` builds a `bestteam.MemoryManager`
**on the worker thread** (so the `SqliteBM25Memory` connection is thread-local)
from env: `BESTTEAM_MEMORY_DB` (unset/empty → memory disabled, runs unchanged;
set → the SQLite path) and `BESTTEAM_MEMORY_MODEL` (optional → enables one
extraction LLM call per run for semantic/procedural records). `run_in_background`
passes it plus `user_id` into `workflow.stream(...)`; `main.py::create_run`
threads the JWT `user.username` through as `user_id` (the wizard's
`builder.py` test-runs omit it, so sandbox runs never touch memory). See
`src/bestteam/core/CLAUDE.md` for the SDK-side design.

**Admin memory management** (`ui/backend/memory_api.py`, `/api/memory`,
`get_current_admin`-guarded): `GET /users` (users with per-type record counts),
`GET /users/{user_id}/records?query=&type=&limit=` (browse via `all(limit=)`,
search via `search(top_k=limit, max_candidates=_MAX_SEARCH_SCAN)` so both the
response and the scan work are bounded over a large store),
`DELETE /records/{memory_id}`, `DELETE /users/{user_id}` (clear a
user — `store.delete_user`). A `get_memory_store` dependency opens a per-request
`SqliteBM25Memory` from `BESTTEAM_MEMORY_DB` on the threadpool thread and closes
it after (`store.close()`); when memory is disabled the read endpoints return
`enabled:false` and mutations return 409. The new SDK store primitives
`user_ids()`/`delete_user()`/`close()` back these endpoints.

## Known limitation: general-purpose cache

Only local caches exist (`_workflow_cache` in `ui/backend/main.py`,
`Workflow._compiled`) — no shared/cross-request cache layer.
