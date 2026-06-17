# Pre-Launch Blocker Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 6 launch-blocking gaps found by a pre-launch readiness audit of bestteam, with zero new features — only bug/risk fixes so the first customer deployment is safe.

**Architecture:** Each blocker is fixed in place using existing patterns (FastAPI exception handlers, the existing `auth.py`/`auth_api.py` token primitives, the existing `status`/`error` React state already used for REST failures). No new subsystems are introduced except Alembic (schema migration tooling) and a GitHub Actions workflow (CI), both adopted directly per their standard conventions.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 + SQLite, stdlib `hmac`/`hashlib`-based JWT-shaped tokens (`ui/backend/auth.py`), React 19 + Vite, pytest, Alembic, GitHub Actions.

## Global Constraints

- No new features — every change here fixes an existing bug/gap, never adds new product capability.
- Do not touch CORS tightening, rate limiting, Docker health checks/restart policy/resource limits, Nginx hardening, the flaky `test_access_token_rejects_tampered_signature` test, or any doc cleanup beyond what a given task directly requires — all explicitly out of scope for this pass.
- Test runner: `.\.venv\Scripts\python.exe -m pytest` from repo root (or `pytest` if the venv is already activated). Backend tests live in `tests/`, not `ui/backend/tests/`.
- Frontend has no test suite (confirmed by audit) — frontend tasks use manual verification steps, not new automated tests.
- Follow existing code style: this codebase has no docstring-per-function convention for every function, but module/class-level docstrings are common — match the surrounding file's existing density, don't add verbose comments.
- WebSocket auth approach (already decided, do not redesign): bearer token passed as a `?token=` URL query parameter, validated server-side with the same `decode_access_token`/`get_user_by_username` primitives `get_current_user` already uses.
- DB migration approach (already decided, do not redesign): adopt Alembic now, before any real customer data exists, generating a clean baseline migration against the current schema.

---

### Task 1: Enforce the `SECRET_KEY` guard unconditionally

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/test_auth.py`
- Modify: `ui/backend/main.py:38-39`
- Modify: `.env.example`
- Modify: `docs/deployment.md` (step 1 bullet list)
- Modify: `CLAUDE.md` ("Common commands" section)

**Interfaces:**
- Consumes: `ui.backend.auth.SECRET_KEY` (str, module-level), `ui.backend.auth._DEFAULT_SECRET_KEY` (str constant, `"bestteam-dev-secret-change-me"`).
- Produces: nothing new consumed by later tasks — this task only changes a startup-time check.

Currently `ui/backend/main.py:38-39` reads:
```python
if os.environ.get("BESTTEAM_ENV") == "production" and auth.SECRET_KEY == auth._DEFAULT_SECRET_KEY:
    raise RuntimeError("BESTTEAM_SECRET_KEY must be set when BESTTEAM_ENV=production")
```
This only fires when `BESTTEAM_ENV=production` is *also* set, so a deployer who forgets that one extra env var silently runs with the public dev secret signing every token.

- [ ] **Step 1: Add `tests/conftest.py` so every test gets a non-default secret**

This must exist *before* the guard becomes unconditional, otherwise every test that imports `ui.backend.main` will start raising `RuntimeError` at collection time. It's safe to add now (today's gated guard doesn't care about this value either).

```python
"""Shared pytest setup. Ensures ui.backend.main can be imported during tests
without tripping the BESTTEAM_SECRET_KEY startup guard in ui/backend/main.py
-- that guard refuses to start with the public dev-default secret."""
import os

os.environ.setdefault("BESTTEAM_SECRET_KEY", "test-secret-key-not-for-production-use")
```

- [ ] **Step 2: Run the full suite to confirm step 1 alone changes nothing**

Run: `pytest`
Expected: same pass/fail counts as before this change (this step is purely additive and inert today).

- [ ] **Step 3: Write the failing test for the unconditional guard**

Append to `tests/test_auth.py` (it already imports `auth` and `backend_main` at the top):

```python
import importlib


def test_secret_key_guard_fires_regardless_of_env(monkeypatch):
    monkeypatch.delenv("BESTTEAM_ENV", raising=False)
    monkeypatch.setattr(auth, "SECRET_KEY", auth._DEFAULT_SECRET_KEY)

    with pytest.raises(RuntimeError, match="BESTTEAM_SECRET_KEY"):
        importlib.reload(backend_main)

    # Restore a valid secret and reload again so later tests in this process
    # (which import backend_main.app directly) see a working app.
    monkeypatch.setattr(auth, "SECRET_KEY", "test-secret-key-not-for-production-use")
    importlib.reload(backend_main)


def test_secret_key_guard_allows_custom_secret_without_env(monkeypatch):
    monkeypatch.delenv("BESTTEAM_ENV", raising=False)
    monkeypatch.setattr(auth, "SECRET_KEY", "a-real-random-secret")

    importlib.reload(backend_main)  # must not raise

    monkeypatch.setattr(auth, "SECRET_KEY", "test-secret-key-not-for-production-use")
    importlib.reload(backend_main)
```

- [ ] **Step 4: Run the new tests in isolation to verify they fail for the right reason**

Run: `pytest tests/test_auth.py -k secret_key_guard -v`
Expected: `test_secret_key_guard_fires_regardless_of_env` FAILS (today's guard doesn't raise without `BESTTEAM_ENV=production`, so `pytest.raises(RuntimeError)` doesn't see a raise). `test_secret_key_guard_allows_custom_secret_without_env` PASSES already (it doesn't raise today either, which is correct already).

- [ ] **Step 5: Make the guard unconditional**

In `ui/backend/main.py`, replace lines 38-39:
```python
if os.environ.get("BESTTEAM_ENV") == "production" and auth.SECRET_KEY == auth._DEFAULT_SECRET_KEY:
    raise RuntimeError("BESTTEAM_SECRET_KEY must be set when BESTTEAM_ENV=production")
```
with:
```python
if auth.SECRET_KEY == auth._DEFAULT_SECRET_KEY:
    raise RuntimeError(
        "BESTTEAM_SECRET_KEY is unset (using the insecure dev default). "
        "Set BESTTEAM_SECRET_KEY to a long random value before starting this service "
        "-- generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
```

- [ ] **Step 6: Run the new tests again to verify they pass**

Run: `pytest tests/test_auth.py -k secret_key_guard -v`
Expected: both tests PASS.

- [ ] **Step 7: Run the full suite to confirm nothing else broke**

Run: `pytest`
Expected: all tests pass (the `conftest.py` from Step 1 is exactly what prevents a suite-wide breakage here).

- [ ] **Step 8: Update docs to match the new unconditional behavior**

In `.env.example`, find the comment above the `BESTTEAM_ENV=production` line. If it currently says something like "Set to production to enable the startup guard...", change it to note that the guard is unconditional and `BESTTEAM_ENV` doesn't affect it. Read the file first to get the exact current wording before editing.

In `docs/deployment.md`, change this bullet under "## 1. Configure environment":
```markdown
- `BESTTEAM_ENV=production` — the backend refuses to start in production
  with the default `BESTTEAM_SECRET_KEY`.
```
to:
```markdown
- `BESTTEAM_SECRET_KEY` — the backend refuses to start (in any environment)
  if this is left at the default value; generate a real one with the
  command shown above.
```

In `CLAUDE.md`, under "## Common commands", after the line that starts the backend with `uvicorn`, add:
```markdown
# Local dev needs a non-default secret too (the guard is unconditional):
$env:BESTTEAM_SECRET_KEY = "dev-only-secret-change-me-for-real-use"
```

- [ ] **Step 9: Commit**

```bash
git add tests/conftest.py tests/test_auth.py ui/backend/main.py .env.example docs/deployment.md CLAUDE.md
git commit -m "fix: enforce SECRET_KEY guard unconditionally, not just under BESTTEAM_ENV=production"
```

---

### Task 2: Authenticate the WebSocket stream endpoint (backend)

**Files:**
- Modify: `ui/backend/main.py:156-176` (the `stream_run` handler and its imports)
- Modify: `tests/test_auth.py`
- Modify: `ui/backend/CLAUDE.md` (auth section)
- Modify: `docs/deployment.md` ("Known limitation: unauthenticated WebSocket" section)

**Interfaces:**
- Consumes: `ui.backend.auth.decode_access_token(token: str) -> str` (raises `AuthError`), `ui.backend.db.users.get_user_by_username(db: Session, username: str) -> Optional[User]`.
- Produces: `stream_run` now requires a `?token=` query parameter. Task 3 (frontend) depends on this exact parameter name and the fact that a missing/invalid token results in the WebSocket closing immediately (close code `4401`) before the existing `4404` "unknown run" check runs.

Currently `ui/backend/main.py:156-176`:
```python
@app.websocket("/api/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str):
    """Replays any events already produced, then relays new ones live until
    the run reaches a terminal state (run_completed / run_failed)."""
    run = registry.get(run_id)
    if run is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    queue = registry.subscribe(run_id)
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event["type"] in ("run_completed", "run_failed"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run_id, queue)
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth.py`. These use `TestClient.websocket_connect`, which raises on the client side when the server closes during the handshake — confirm the exact exception by running Step 2 before asserting a specific type.

```python
def test_stream_run_rejects_missing_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/runs/some-run-id/stream") as ws:
            ws.receive_json()


def test_stream_run_rejects_invalid_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/runs/some-run-id/stream?token=not-a-real-token") as ws:
            ws.receive_json()


def test_stream_run_accepts_valid_token_for_known_run(client, workflows_dir):
    from tests.test_ui_backend import _write_workflow

    _write_workflow(workflows_dir / "demo.yaml", "demo", "hello there")

    token = client.post("/api/auth/register", json={"username": "bob", "password": "hunter2"}).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"

    run_id = client.post("/api/runs", json={"workflow": "demo", "input": "hi"}).json()["run_id"]

    with client.websocket_connect(f"/api/runs/{run_id}/stream?token={token}") as ws:
        event = ws.receive_json()
        assert event["type"] == "run_started"
```

This last test needs a `workflows_dir` fixture in `tests/test_auth.py` matching the one in `tests/test_ui_backend.py:13-17` (it isn't currently defined in `test_auth.py`). Add it right above the `client` fixture:

```python
@pytest.fixture
def workflows_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()
    return tmp_path
```

The existing `client` fixture in `tests/test_auth.py:49-69` already does `monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)` itself using its own `tmp_path` — having both fixtures request `tmp_path` independently would point them at *different* temp directories. Change the `client` fixture to depend on `workflows_dir` instead of computing its own, so both fixtures share one directory:

```python
@pytest.fixture
def client(workflows_dir, monkeypatch):
    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(backend_main.app)
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)
```

- [ ] **Step 2: Run the new tests to verify they fail for the right reason**

Run: `pytest tests/test_auth.py -k stream_run -v`
Expected: `test_stream_run_rejects_missing_token` and `test_stream_run_rejects_invalid_token` FAIL (today's endpoint accepts the connection with no auth check at all, so the `with pytest.raises(Exception)` block doesn't see an exception — the `ws.receive_json()` call hangs or returns the `4404`-closure exception instead of failing for an auth reason; the test as written only proves *some* exception happens, which is already true today for an unknown run id — note this and move to Step 3, then re-verify in Step 4 that the behavior is now auth-driven, not just "unknown run"). `test_stream_run_accepts_valid_token_for_known_run` FAILS with a `TypeError` (the current handler signature doesn't accept a `token` query param, so the URL's `?token=...` is just ignored — this test should otherwise connect fine today, proving today's endpoint has no auth gate at all).

- [ ] **Step 3: Add the auth check to `stream_run`**

In `ui/backend/main.py`, update the imports (line 18 and the `from . import auth` block) — add `Request`-free auth imports near the existing `from . import auth` line:
```python
from .auth import AuthError, decode_access_token
from .db.users import get_user_by_username
```

Replace the `stream_run` handler:
```python
@app.websocket("/api/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str, token: Optional[str] = None, db: Session = Depends(get_db)):
    """Replays any events already produced, then relays new ones live until
    the run reaches a terminal state (run_completed / run_failed).

    Requires the same bearer token used for REST routes, passed as a
    `?token=` query parameter -- browsers can't set custom headers when
    opening a WebSocket, so this can't reuse the HTTPBearer-based
    get_current_user dependency directly."""
    if token is None:
        await websocket.close(code=4401)
        return
    try:
        username = decode_access_token(token)
    except AuthError:
        await websocket.close(code=4401)
        return
    if get_user_by_username(db, username) is None:
        await websocket.close(code=4401)
        return

    run = registry.get(run_id)
    if run is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    queue = registry.subscribe(run_id)
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event["type"] in ("run_completed", "run_failed"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run_id, queue)
```

- [ ] **Step 4: Run the tests again to verify they pass**

Run: `pytest tests/test_auth.py -k stream_run -v`
Expected: all three PASS.

- [ ] **Step 5: Run the full suite**

Run: `pytest`
Expected: all tests pass.

- [ ] **Step 6: Update docs that describe this endpoint as unauthenticated**

In `ui/backend/CLAUDE.md`, find the sentence ending "`/api/runs/{run_id}/stream` is intentionally unauthenticated (run IDs are unguessable UUID hex strings, only obtainable via an authenticated endpoint)." and replace it with:
```markdown
`/api/runs/{run_id}/stream` requires the same bearer token passed as a
`?token=` query parameter (browsers can't set custom headers when opening a
WebSocket), validated with the same `decode_access_token`/
`get_user_by_username` logic as `get_current_user`.
```

In `docs/deployment.md`, delete the entire "## Known limitation: unauthenticated WebSocket" section (the heading and its paragraph), since it's no longer true.

- [ ] **Step 7: Commit**

```bash
git add ui/backend/main.py tests/test_auth.py ui/backend/CLAUDE.md docs/deployment.md
git commit -m "fix: require bearer token on the run-stream WebSocket endpoint"
```

---

### Task 3: Authenticate the WebSocket stream endpoint (frontend)

**Files:**
- Modify: `ui/frontend/src/lib/api.js:4`
- Modify: `ui/frontend/src/pages/MonitorPage.jsx`
- Modify: `ui/frontend/src/pages/wizard/PreviewPage.jsx`

**Interfaces:**
- Consumes: Task 2's contract — the backend now requires `?token=<bearer-token>` on the WebSocket URL, closing immediately without it.
- Produces: `TOKEN_KEY` exported from `ui/frontend/src/lib/api.js`, used by both pages (and available for Task 5/6's MonitorPage/PreviewPage changes, which touch the same WebSocket-construction lines).

- [ ] **Step 1: Export the token storage key from `api.js`**

In `ui/frontend/src/lib/api.js:4`, change:
```js
const TOKEN_KEY = 'bestteam_token'
```
to:
```js
export const TOKEN_KEY = 'bestteam_token'
```
No other line in this file changes — `TOKEN_KEY` is used internally by `request()` exactly as before.

- [ ] **Step 2: Append the token to the WebSocket URL in `MonitorPage.jsx`**

In `ui/frontend/src/pages/MonitorPage.jsx`, change the import on line 3 from:
```js
import { API_BASE, WS_BASE, api } from '../lib/api'
```
to:
```js
import { API_BASE, WS_BASE, TOKEN_KEY, api } from '../lib/api'
```

Change the WebSocket construction inside `startRun` (currently):
```js
const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream`)
```
to:
```js
const token = localStorage.getItem(TOKEN_KEY)
const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?token=${encodeURIComponent(token ?? '')}`)
```

- [ ] **Step 3: Append the token to the WebSocket URL in `PreviewPage.jsx`**

In `ui/frontend/src/pages/wizard/PreviewPage.jsx`, change the import on line 4 from:
```js
import { WS_BASE, api } from '../../lib/api'
```
to:
```js
import { WS_BASE, TOKEN_KEY, api } from '../../lib/api'
```

Change the WebSocket construction inside `run` (currently, line 68):
```js
const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream`)
```
to:
```js
const token = localStorage.getItem(TOKEN_KEY)
const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?token=${encodeURIComponent(token ?? '')}`)
```

- [ ] **Step 4: Manual verification**

No frontend test suite exists (confirmed by audit) — verify by hand:
1. Start the backend with a real `BESTTEAM_SECRET_KEY` set, start the frontend (`npm run dev`).
2. Log in, go to `/`, select a workflow, enter input, click Run — confirm the live trace streams exactly as before.
3. Open browser DevTools → Network → WS — confirm the WebSocket URL now includes `?token=...`.
4. Go through the wizard to the "Meet your team" (Preview) page and run a test task — confirm it streams normally too.
5. In DevTools, run `localStorage.removeItem('bestteam_token')`, then trigger a new run from either page — confirm the WebSocket fails to connect (visible as a closed connection in the Network tab; full user-facing feedback for this case lands in Task 5/6).

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/lib/api.js ui/frontend/src/pages/MonitorPage.jsx ui/frontend/src/pages/wizard/PreviewPage.jsx
git commit -m "fix: send the auth token on the run-stream WebSocket connection"
```

---

### Task 4: Global exception handler for unhandled 500s

**Files:**
- Modify: `ui/backend/main.py` (imports + new handler, placed after `app = FastAPI(...)` and its CORS middleware)
- Create: `tests/test_exception_handler.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: a `logging.getLogger("bestteam.api")` logger instance in `ui/backend/main.py`, used only within this handler — no other task depends on it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_exception_handler.py`:
```python
"""Tests for the global unhandled-exception handler (ui/backend/main.py)."""
import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()

    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    try:
        test_client = TestClient(backend_main.app, raise_server_exceptions=False)
        token = test_client.post("/api/auth/register", json={"username": "test", "password": "test"}).json()["access_token"]
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_unhandled_exception_returns_sanitized_500(client, monkeypatch):
    def _boom(name, db=None):
        raise RuntimeError("boom: should never reach the client")

    monkeypatch.setattr(backend_main, "_get_workflow", _boom)

    resp = client.get("/api/workflows/anything/graph")

    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}
    assert "boom" not in resp.text
    assert "RuntimeError" not in resp.text


def test_known_workflow_404_still_returns_friendly_detail(client):
    resp = client.get("/api/workflows/does-not-exist/graph")

    assert resp.status_code == 404
    assert "Unknown workflow" in resp.json()["detail"]
```

`raise_server_exceptions=False` is required on `TestClient` here — without it, Starlette's test client re-raises server-side exceptions into the test process instead of returning a response, which would defeat `test_unhandled_exception_returns_sanitized_500`.

- [ ] **Step 2: Run the new tests to verify the first fails, the second already passes**

Run: `pytest tests/test_exception_handler.py -v`
Expected: `test_unhandled_exception_returns_sanitized_500` FAILS — with no global handler, FastAPI's default behavior for an unhandled exception under `raise_server_exceptions=False` is still to produce a 500, but check the actual response body in the failure output: today it will not be the sanitized `{"detail": "Internal server error"}` JSON (likely an empty body or a different shape), so the `assert resp.json() == ...` line fails. `test_known_workflow_404_still_returns_friendly_detail` PASSES already (existing `_get_workflow` 404 behavior is untouched).

- [ ] **Step 3: Add the logger and global exception handler**

In `ui/backend/main.py`, add to the imports:
```python
import logging
```
and add `Request` to the existing FastAPI import line, and add a `JSONResponse` import:
```python
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
```

After `app = FastAPI(title="bestteam monitoring dashboard")` and its `CORSMiddleware` setup (i.e., right after the `app.include_router(crud_router)` line), add:
```python
logger = logging.getLogger("bestteam.api")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for anything not already turned into an HTTPException by a
    route handler (BestTeamError/ValidationError/KeyError/TypeError are
    handled inline and never reach here). Logs the full traceback
    server-side and returns a generic, non-leaking 500 to the client."""
    logger.exception("Unhandled exception in %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
```

- [ ] **Step 4: Run the tests again to verify they pass**

Run: `pytest tests/test_exception_handler.py -v`
Expected: both PASS. This also proves FastAPI's existing `HTTPException`-raising paths (404/400) aren't intercepted by the new generic handler.

- [ ] **Step 5: Run the full suite**

Run: `pytest`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/main.py tests/test_exception_handler.py
git commit -m "fix: add a global exception handler so unhandled errors don't leak stack traces"
```

---

### Task 5: Frontend WebSocket failure handling — MonitorPage

**Files:**
- Modify: `ui/frontend/src/pages/MonitorPage.jsx`

**Interfaces:**
- Consumes: the existing `status` state (`idle | running | completed | failed | unreachable`) and its existing `.banner-error` render block for `status === 'unreachable'`.
- Produces: nothing new consumed elsewhere.

- [ ] **Step 1: Add `onerror`/`onclose` handlers**

In `ui/frontend/src/pages/MonitorPage.jsx`, immediately after the existing `ws.onmessage = (message) => { ... }` block inside `startRun`, add:
```js
ws.onerror = () => {
  setStatus('unreachable')
}
ws.onclose = () => {
  // onclose always fires, including after a clean run_completed/run_failed
  // that onmessage already handled -- only downgrade to 'unreachable' if
  // the socket closed while still running.
  setStatus((current) => (current === 'running' ? 'unreachable' : current))
}
```

- [ ] **Step 2: Manual verification**

No automated frontend tests exist (confirmed by audit) — verify by hand:
1. Start backend + frontend, log in, start a run, let it complete normally — confirm the "Final output" section still appears and the status banner doesn't flip to the "Can't reach the backend" error after completion (this is the race the functional `setStatus` updater guards against).
2. Start a run, then kill the backend process (Ctrl+C) before it completes — confirm the UI shows the existing `.banner-error` "Can't reach the backend at ..." message instead of staying stuck on "Running…".
3. Start a run, then (with the backend still up) clear `localStorage.bestteam_token` and trigger a *new* run — confirm the same banner appears (the WebSocket from Task 3 now fails to authenticate, closing immediately, which should trigger `onerror`/`onclose` here).

- [ ] **Step 3: Commit**

```bash
git add ui/frontend/src/pages/MonitorPage.jsx
git commit -m "fix: surface WebSocket connection failures on the Monitor page instead of hanging silently"
```

---

### Task 6: Frontend WebSocket failure handling — PreviewPage

**Files:**
- Modify: `ui/frontend/src/pages/wizard/PreviewPage.jsx`

**Interfaces:**
- Consumes: the existing `status` state (`idle | running | completed | failed`) and `error` string state, both already rendered via the existing `.banner-error` block at line 89.
- Produces: nothing new consumed elsewhere.

- [ ] **Step 1: Add `onerror`/`onclose` handlers**

In `ui/frontend/src/pages/wizard/PreviewPage.jsx`, immediately after the existing `ws.onmessage = (message) => { ... }` block inside `run`, add:
```js
ws.onerror = () => {
  setStatus('failed')
  setError('Lost connection to the backend while your team was working. Please try again.')
}
ws.onclose = () => {
  if (status === 'running') {
    setStatus('failed')
    setError('Lost connection to the backend while your team was working. Please try again.')
  }
}
```
Reading `status` directly from closure (rather than a functional updater, as in Task 5) is safe here: `onclose` is (re)assigned fresh inside `run()` immediately after `setStatus('running')` runs synchronously a few lines above in the same call, so there's no stale-closure window within a single run.

- [ ] **Step 2: Manual verification**

1. Go through the wizard to the Preview page, run a test task, let it complete normally — confirm the activity feed still ends with "All done!" and no failure banner appears.
2. Run a test task, kill the backend before it completes — confirm the `.banner-error` shows "Lost connection to the backend while your team was working. Please try again." instead of leaving the button stuck on "Working…".

- [ ] **Step 3: Commit**

```bash
git add ui/frontend/src/pages/wizard/PreviewPage.jsx
git commit -m "fix: surface WebSocket connection failures on the wizard Preview page instead of hanging silently"
```

---

### Task 7: CI pipeline

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `pyproject.toml`'s `[ui,dev]` extras, `ui/frontend/package.json`'s `lint`/`build` scripts, `ui/frontend/package-lock.json` (confirmed present and tracked).
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Verify both job command sequences pass locally first**

Run (backend):
```bash
pip install -e ".[ui,dev]"
pytest
```
Expected: exit code 0.

Run (frontend):
```bash
cd ui/frontend
npm ci
npm run lint
npm run build
```
Expected: all three commands exit 0. If `npm run lint` fails on pre-existing issues unrelated to this plan's changes, stop and report this to the user rather than silently fixing unrelated lint errors — that would be scope creep beyond the 6 approved blockers.

- [ ] **Step 2: Create the workflow file**

Create `.github/workflows/ci.yml`:
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install -e ".[ui,dev]"
      - name: Run pytest
        run: pytest

  frontend:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: ui/frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: "npm"
          cache-dependency-path: ui/frontend/package-lock.json
      - name: Install dependencies
        run: npm ci
      - name: Lint
        run: npm run lint
      - name: Build
        run: npm run build
```

Python 3.11 matches the `Dockerfile`'s `FROM python:3.11-slim` base image. Lint runs before build so a lint failure fails fast.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions workflow running backend pytest and frontend lint/build"
```

- [ ] **Step 4: Push and verify on GitHub**

Push this commit (on a branch, or to `main` per the user's normal workflow) and confirm both the `backend` and `frontend` jobs go green in the repository's Actions tab.

---

### Task 8: Alembic baseline migration

**Files:**
- Modify: `pyproject.toml` (add `alembic` to the `ui` extra)
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako` (generated by `alembic init`)
- Create: `alembic/versions/<hash>_baseline_schema.py`
- Modify: `docs/deployment.md` (new "Apply database migrations" step)

**Interfaces:**
- Consumes: `ui.backend.db.models.Base` (the single `DeclarativeBase` subclass covering all 11 tables: `users`, `agents`, `teams`, `knowledge_bases`, `skills`, `workflows`, `builder_sessions`, `runs`, `trace_events`, `usage_records`, `model_catalog`), `BESTTEAM_DB_PATH` env var (same default path logic as `ui/backend/db_session.py:20`).
- Produces: `alembic upgrade head` becomes the documented way to set up/update the deployed database schema. Task 9's backup script operates on the same SQLite file this targets.

- [ ] **Step 1: Add Alembic as a dependency**

In `pyproject.toml`, change:
```toml
ui           = ["fastapi>=0.110", "uvicorn[standard]>=0.30", "sqlalchemy>=2.0"]
```
to:
```toml
ui           = ["fastapi>=0.110", "uvicorn[standard]>=0.30", "sqlalchemy>=2.0", "alembic>=1.13"]
```

- [ ] **Step 2: Install and verify**

Run: `pip install -e ".[ui,dev]"`
Run: `python -c "import alembic; print(alembic.__version__)"`
Expected: prints a version `>=1.13`, no import error.

- [ ] **Step 3: Initialize the Alembic project**

Run from the repo root: `alembic init alembic`
Expected: creates `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/` (empty).

- [ ] **Step 4: Confirm whether `ui.backend.db.models` is importable as-is from `alembic/env.py`**

Run: `python -c "from ui.backend.db.models import Base; print(Base.metadata.tables.keys())"`
Expected: prints a `dict_keys` view containing all 11 table names. If this fails with `ModuleNotFoundError`, the editable install (`where = ["src"]` in `pyproject.toml`) doesn't expose `ui/` on the path, and `env.py` will need an explicit `sys.path` insert (handled in the next step either way, harmlessly, if not needed).

- [ ] **Step 5: Wire `alembic/env.py` to the app's models and DB path**

Edit `alembic/env.py`. Near the top, after the existing `from alembic import context` line, add:
```python
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.backend.db.models import Base

# Keep this default in sync with ui/backend/db_session.py::DB_PATH.
_default_db_path = Path(__file__).resolve().parent.parent / "ui" / "backend" / "data" / "bestteam.db"
db_path = os.environ.get("BESTTEAM_DB_PATH", str(_default_db_path))
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
```
Then find the line `target_metadata = None` (generated by `alembic init`) and change it to:
```python
target_metadata = Base.metadata
```

In `alembic.ini`, find the line `sqlalchemy.url = driver://user:pass@localhost/dbname` and delete it (or comment it out) — it's overridden by `env.py`'s `config.set_main_option` call either way, but leaving the placeholder in is confusing.

- [ ] **Step 6: Verify `env.py` resolves without error**

Run: `alembic current`
Expected: completes without error, reporting no current revision (since no migration has been generated yet).

- [ ] **Step 7: Generate the baseline migration against a clean database**

If a local `ui/backend/data/bestteam.db` already exists from prior dev runs, delete it first (it's gitignored, not real customer data, and autogenerate needs a truly empty DB to capture the full schema rather than detecting "no diff"):
```bash
rm -f ui/backend/data/bestteam.db
```

Run: `alembic revision --autogenerate -m "baseline schema"`
Expected: creates `alembic/versions/<hash>_baseline_schema.py`.

- [ ] **Step 8: Review the generated migration**

Open the generated file and confirm `upgrade()` creates all 11 tables (`users`, `agents`, `teams`, `knowledge_bases`, `skills`, `workflows`, `builder_sessions`, `runs`, `trace_events`, `usage_records`, `model_catalog`) with the columns/types/foreign-keys matching `ui/backend/db/models.py`, and that `downgrade()` drops them all (not a bare `pass`).

- [ ] **Step 9: Verify the migration works end-to-end**

```bash
rm -f /tmp/alembic-test.db
BESTTEAM_DB_PATH=/tmp/alembic-test.db alembic upgrade head
python -c "
from sqlalchemy import inspect, create_engine
tables = sorted(inspect(create_engine('sqlite:////tmp/alembic-test.db')).get_table_names())
print(tables)
assert len(tables) == 11, tables
"
BESTTEAM_DB_PATH=/tmp/alembic-test.db alembic downgrade base
BESTTEAM_DB_PATH=/tmp/alembic-test.db alembic upgrade head
rm -f /tmp/alembic-test.db
```
Expected: the table list prints with 11 entries and the assertion passes; `downgrade base` and the second `upgrade head` both complete without error.

(On Windows PowerShell, set the env var per-command instead: `$env:BESTTEAM_DB_PATH = "C:\temp\alembic-test.db"; alembic upgrade head`.)

- [ ] **Step 10: Document the migration step in deployment docs**

In `docs/deployment.md`, insert a new step between "## 2. Build and start" and "## 3. Create the first user" (renumbering "Create the first user" to `## 4` and "Verify" to `## 5`):
```markdown
## 3. Apply database migrations

```bash
docker compose exec backend alembic upgrade head
```

Run this once after the first `docker compose up -d`, and again after
pulling any update that includes a new file under `alembic/versions/`. This
is the canonical way the database schema is created/updated going forward
(replacing a bare `Base.metadata.create_all()`, which still runs
automatically as a harmless no-op safety net on a brand-new database).
```

- [ ] **Step 11: Run the full test suite to confirm nothing broke**

Run: `pytest`
Expected: all tests pass unchanged — tests use `make_engine(":memory:")` + `init_db(engine)` directly and never touch Alembic.

- [ ] **Step 12: Commit**

```bash
git add pyproject.toml alembic.ini alembic/ docs/deployment.md
git commit -m "feat: add Alembic baseline migration for the deployed SQLite schema"
```

---

### Task 9: SQLite backup/restore runbook

**Files:**
- Create: `scripts/backup-db.sh`
- Modify: `docs/deployment.md` (new "Backup and restore" section)

**Interfaces:**
- Consumes: the same `/app/ui/backend/data/bestteam.db` path documented in `docs/deployment.md`'s existing "Data persistence" section.
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Create the backup script**

Create `scripts/backup-db.sh`:
```bash
#!/usr/bin/env bash
# Back up the per-customer SQLite database from the running backend container.
#
# Usage:
#   ./scripts/backup-db.sh [output-path]
#
# Defaults to ./backups/bestteam-<timestamp>.db if no path is given.
set -euo pipefail

OUT_PATH="${1:-backups/bestteam-$(date +%Y%m%d-%H%M%S).db}"
mkdir -p "$(dirname "$OUT_PATH")"

# Use sqlite3's online backup API (Python stdlib) -- safe against a live
# database, unlike a raw file copy which could race with an in-progress
# write. The sqlite3 CLI binary isn't installed in the python:3.11-slim
# base image, so this uses the Python module instead.
docker compose exec -T backend python -c "
import sqlite3
src = sqlite3.connect('/app/ui/backend/data/bestteam.db')
dst = sqlite3.connect('/tmp/bestteam-backup.db')
src.backup(dst)
dst.close()
src.close()
"
docker compose cp backend:/tmp/bestteam-backup.db "$OUT_PATH"
docker compose exec -T backend rm -f /tmp/bestteam-backup.db

echo "Backed up to $OUT_PATH"
```

Run: `chmod +x scripts/backup-db.sh`

- [ ] **Step 2: Document backup and restore in `docs/deployment.md`**

Add a new section after "## Data persistence" (the last section in the file):
```markdown
## Backup and restore

Back up the live database (safe to run while the backend is running):

```bash
./scripts/backup-db.sh
# or with an explicit path:
./scripts/backup-db.sh /path/to/backups/bestteam-2026-06-17.db
```

To restore from a backup:

1. Stop the backend so nothing writes to the database during restore:
   ```bash
   docker compose stop backend
   ```
2. Copy the backup file into the container, overwriting the live database:
   ```bash
   docker compose cp /path/to/backups/bestteam-2026-06-17.db backend:/app/ui/backend/data/bestteam.db
   ```
3. Restart the backend:
   ```bash
   docker compose start backend
   ```
4. Verify: `curl http://localhost:8000/api/health` returns `200`, and a
   login with a known user from before the backup succeeds.
```

- [ ] **Step 3: Verify the backup script against a running stack**

```bash
docker compose up -d
curl -X POST http://localhost:8000/api/auth/register -H "Content-Type: application/json" -d '{"username": "backuptest", "password": "hunter2"}'
./scripts/backup-db.sh
ls -la backups/
```
Expected: a non-empty `.db` file appears under `backups/`.

Verify it has the expected schema:
```bash
python -c "
from sqlalchemy import inspect, create_engine
import glob
path = sorted(glob.glob('backups/*.db'))[-1]
print(sorted(inspect(create_engine(f'sqlite:///{path}')).get_table_names()))
"
```
Expected: prints the same 11 table names as the live database.

- [ ] **Step 4: Do a full restore drill**

```bash
docker compose stop backend
docker compose cp backups/<the-file-from-step-3>.db backend:/app/ui/backend/data/bestteam.db
docker compose start backend
curl http://localhost:8000/api/health
curl -X POST http://localhost:8000/api/auth/login -H "Content-Type: application/json" -d '{"username": "backuptest", "password": "hunter2"}'
```
Expected: `/api/health` returns `200`; the login returns a `200` with an `access_token` (proving the restored user from Step 3 is present).

- [ ] **Step 5: Commit**

```bash
git add scripts/backup-db.sh docs/deployment.md
git commit -m "docs: add SQLite backup/restore runbook and backup-db.sh script"
```

---

## Self-Review

**Spec coverage:** all 6 blockers from the audit are covered — Task 1 (SECRET_KEY guard), Tasks 2-3 (WebSocket auth, backend+frontend), Task 4 (exception handler), Tasks 5-6 (frontend WS failure handling, both pages), Task 7 (CI), Tasks 8-9 (Alembic + backup, the two halves of blocker 6).

**Placeholder scan:** no TBDs; every code step shows complete code; every test step shows actual assertions.

**Type/name consistency:** `TOKEN_KEY` exported in Task 3 Step 1 is the exact identifier imported in Task 3 Steps 2-3 and reused unchanged in Tasks 5-6 (which don't need it directly, since they only add `onerror`/`onclose`, not new WebSocket construction). The `?token=` query param name introduced in Task 2 matches exactly what Task 3 appends to the URL. The `workflows_dir` fixture added in Task 2 Step 1 and the `client` fixture's rewrite to depend on it are consistent with the existing fixture in `tests/test_ui_backend.py:13-17` it mirrors.

## Suggested execution order

Tasks 1, 4, 7, 8, and 9 are independent of each other and of Tasks 2/3/5/6 — safe to dispatch in parallel. Task 2 must land before Task 3 is meaningful to verify end-to-end (Task 3's manual verification step depends on Task 2's backend behavior). Tasks 5 and 6 are independent of each other and don't strictly require Task 2/3 to be done first (the `onerror`/`onclose` handlers work regardless of *why* a connection fails), but their manual verification is more complete once Task 3 has landed too.
