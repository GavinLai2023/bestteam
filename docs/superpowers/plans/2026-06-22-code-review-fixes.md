# Code Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 7 confirmed findings from the 2026-06-22 full-project code
review: two correctness bugs and one efficiency regression in
`ui/backend/main.py`'s workflow cache, a DB-session lifetime bug in the
WebSocket stream route, and four independent frontend/SDK minors.

**Architecture:** Each finding gets its own small, independently-testable
change. The two `_get_workflow` fixes (cache staleness + redundant
`load_skills`) are combined into one task since they touch the same few
lines. Everything else is a standalone task.

**Tech Stack:** FastAPI + SQLAlchemy (backend), React 19 (frontend),
pytest (backend tests). No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-22-code-review-fixes-design.md` —
  read it for the "why" behind each fix; this plan is the "how."
- Do not touch anything listed under that spec's "Refuted findings"
  section — those were investigated and are not bugs.
- Frontend pages in this plan have no existing automated test harness
  (confirmed: no `*.test.jsx` files exist under `ui/frontend/src/`) — their
  tasks end with a manual dev-server verification step instead of an
  automated test, per the spec's Testing section.
- Run `./.venv/Scripts/python.exe -m pytest -q` after every backend task
  and `cd ui/frontend && npm run lint && npm run build` after every
  frontend task, in addition to each task's own targeted test command.

---

### Task 1: Fix workflow-cache staleness and the redundant `load_skills` call

**Files:**
- Modify: `ui/backend/main.py:16` (imports), `:33` (db.models import),
  `:84-130` (`_get_workflow`)
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: existing `load_skills(db) -> Dict[str, SkillSpec]`
  (`ui/backend/skills.py`), `load_knowledge_base_tools(db, raw, source) ->
  Dict[str, Any]` (`ui/backend/knowledge_bases.py`), `SkillRecord` /
  `KnowledgeBaseRecord` (`ui/backend/db/models.py`, both have an
  `updated_at: Mapped[datetime]` column with `onupdate=_utcnow`).
- Produces: a new module-level helper
  `_dependency_freshness(db: Session) -> Tuple[Optional[Any], Optional[Any]]`
  in `ui/backend/main.py`, used only by `_get_workflow`.

- [ ] **Step 1: Write the failing staleness test**

Add to `tests/test_crud_api.py` (anywhere after the other `_get_workflow`
tests, e.g. after `test_inline_knowledge_base_wins_over_standalone_of_same_name`):

```python
def test_cached_workflow_picks_up_skill_update(client):
    client.put(
        "/api/config/skills/greeting",
        json={"description": "How to greet", "instructions": "Say hello warmly.", "tools": []},
    )
    workflow_config = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi", "skills": ["greeting"]}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    client.put("/api/config/workflows/skill_wf", json=workflow_config)

    from ui.backend.main import _get_workflow
    from ui.backend.db_session import get_db as real_get_db

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        wf1 = _get_workflow("skill_wf", db)
        assert "Say hello warmly." in wf1.steps[0].agents[0].backstory

        client.put(
            "/api/config/skills/greeting",
            json={"description": "How to greet", "instructions": "Say hello formally.", "tools": []},
        )

        wf2 = _get_workflow("skill_wf", db)
        assert "Say hello formally." in wf2.steps[0].agents[0].backstory
        assert wf2 is not wf1
    finally:
        db_gen.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_cached_workflow_picks_up_skill_update -v`
Expected: FAIL — `wf2.steps[0].agents[0].backstory` still contains
`"Say hello warmly."` because the cache key never changed.

- [ ] **Step 3: Write the failing call-count test for the redundant `load_skills` call**

Add to `tests/test_crud_api.py`, next to the test above:

```python
def test_load_skills_only_runs_on_workflow_cache_miss(client, monkeypatch):
    workflow_config = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi"}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    client.put("/api/config/workflows/cached_wf", json=workflow_config)

    calls = []
    original = backend_main.load_skills

    def counting_load_skills(db):
        calls.append(1)
        return original(db)

    monkeypatch.setattr(backend_main, "load_skills", counting_load_skills)

    from ui.backend.main import _get_workflow
    from ui.backend.db_session import get_db as real_get_db

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        _get_workflow("cached_wf", db)
        _get_workflow("cached_wf", db)
    finally:
        db_gen.close()

    assert len(calls) == 1
```

- [ ] **Step 4: Run it to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_load_skills_only_runs_on_workflow_cache_miss -v`
Expected: FAIL — `len(calls) == 2` (called on both the miss and the
subsequent hit).

- [ ] **Step 5: Update imports in `ui/backend/main.py`**

`Tuple`/`Optional`/`Any` are already imported at line 16
(`from typing import Any, Dict, Optional, Tuple`) — no edit needed there.

Change line 33 from:

```python
from .db.models import User, WorkflowRecord
```

to:

```python
from .db.models import KnowledgeBaseRecord, SkillRecord, User, WorkflowRecord
```

Add a new import line directly after line 22 (`from sqlalchemy.orm import
Session`):

```python
from sqlalchemy import func
```

- [ ] **Step 6: Add the `_dependency_freshness` helper**

Add this function in `ui/backend/main.py` directly above `def _get_workflow`
(i.e. right after the `class RunRequest(BaseModel):` block, before line 84):

```python
def _dependency_freshness(db: Session) -> Tuple[Optional[Any], Optional[Any]]:
    """Max `updated_at` across all SkillRecords and all KnowledgeBaseRecords,
    folded into a cached Workflow's cache key so editing either invalidates
    any already-cached workflow that might depend on them.

    Deliberately global rather than scoped to only the names a given
    workflow references -- see
    docs/superpowers/specs/2026-06-22-code-review-fixes-design.md, "Design
    > A" for why."""
    skills_max = db.query(func.max(SkillRecord.updated_at)).scalar()
    kb_max = db.query(func.max(KnowledgeBaseRecord.updated_at)).scalar()
    return (skills_max, kb_max)
```

- [ ] **Step 7: Rewrite `_get_workflow`'s database-record branch**

Replace lines 99-130 of `ui/backend/main.py` (from `source = WORKFLOWS_DIR /
f"{name}.yaml"` through the `return _workflow_cache[name][0]` that ends the
`if record is not None:` block) with:

```python
    source = WORKFLOWS_DIR / f"{name}.yaml"
    if db is not None:
        record = db.query(WorkflowRecord).filter_by(name=name).one_or_none()
        dependency_freshness = _dependency_freshness(db) if record is not None else None
    else:
        with SessionLocal() as session:
            record = session.query(WorkflowRecord).filter_by(name=name).one_or_none()
            dependency_freshness = _dependency_freshness(session) if record is not None else None

    if record is not None:
        cache_key: Any = ("db", record.updated_at, *dependency_freshness)
        cached = _workflow_cache.get(name)
        if cached is None or cached[1] != cache_key:
            # Only load skills and build standalone KB tools (which may
            # re-chunk files and, for type: vector, call a paid embedding
            # model) on a cache miss -- not on every request.
            if db is not None:
                skill_lookup = load_skills(db)
                kb_tools = load_knowledge_base_tools(db, record.config, source)
            else:
                with SessionLocal() as session:
                    skill_lookup = load_skills(session)
                    kb_tools = load_knowledge_base_tools(session, record.config, source)
            try:
                workflow = _build_workflow(
                    record.config,
                    source=source,
                    extra_tools=kb_tools,
                    extra_skills=skill_lookup,
                )
            except (KeyError, TypeError, BestTeamError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _workflow_cache[name] = (workflow, cache_key)
        return _workflow_cache[name][0]
```

Leave everything below this (the file-based fallback path, starting at
`path = WORKFLOWS_DIR / f"{name}.yaml"`) unchanged.

- [ ] **Step 8: Run both new tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k "test_cached_workflow_picks_up_skill_update or test_load_skills_only_runs_on_workflow_cache_miss" -v`
Expected: both PASS

- [ ] **Step 9: Run the full backend test suite to check for regressions**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass (previously 218, now 220)

- [ ] **Step 10: Commit**

```bash
git add ui/backend/main.py tests/test_crud_api.py
git commit -m "fix: invalidate cached workflows when a referenced skill or KB changes"
```

---

### Task 2: Stop holding a DB session open for the whole WebSocket stream

**Files:**
- Modify: `ui/backend/main.py:185-204` (`stream_run`)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `SessionLocal` (already imported in `main.py`, line 35),
  `get_user_by_username(db, username) -> Optional[User]`
  (`ui/backend/db/users.py`, already imported as `get_user_by_username`).
- Produces: no interface change — `stream_run`'s route signature drops its
  `db` parameter, but its external behavior (close codes, accept timing) is
  unchanged.

- [ ] **Step 1: Write the regression test**

Add to `tests/test_auth.py`, after `test_stream_run_accepts_valid_token_for_known_run`:

```python
def test_stream_run_rejects_token_for_deleted_user(client, workflows_dir):
    from tests.test_ui_backend import _write_workflow
    from ui.backend.db.models import User

    _write_workflow(workflows_dir / "demo.yaml", "demo", "hello there")

    token = client.post("/api/auth/register", json={"username": "carol", "password": "hunter2"}).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    run_id = client.post("/api/runs", json={"workflow": "demo", "input": "hi"}).json()["run_id"]

    db_gen = backend_main.app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        db.query(User).filter_by(username="carol").delete()
        db.commit()
    finally:
        db_gen.close()

    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/runs/{run_id}/stream?token={token}") as ws:
            ws.receive_json()
```

- [ ] **Step 2: Run it to verify it already passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_auth.py -k test_stream_run_rejects_token_for_deleted_user -v`
Expected: PASS — this documents *existing* behavior (the bug being fixed
is the session's lifetime, not this rejection logic, which already works).
This test exists to guard against a regression during the refactor below.

- [ ] **Step 3: Release the DB session right after the existence check instead of holding it for the whole connection**

**Correction during implementation:** the original plan called for dropping
`db: Session = Depends(get_db)` and using `with SessionLocal() as session:`
instead. That breaks test overridability: `SessionLocal` is a fixed
module-level sessionmaker bound to the real production engine at import
time (`ui/backend/db_session.py`), and tests only override the `get_db`
FastAPI dependency, not `SessionLocal` itself — calling `SessionLocal()`
directly inside the handler silently queries the real DB instead of the
test's in-memory one (confirmed by `test_stream_run_accepts_valid_token_for_known_run`
failing with that approach). The actual fix: keep `Depends(get_db)` for
test compatibility, and just call `db.close()` as soon as the existence
check is done, well before the long-lived streaming loop, instead of
leaving it open for the framework to close at request teardown.

In `ui/backend/main.py`, change (current lines 202-204):

```python
    if get_user_by_username(db, username) is None:
        await websocket.close(code=4401)
        return

    run = registry.get(run_id)
    if run is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
```

to:

```python
    if get_user_by_username(db, username) is None:
        db.close()
        await websocket.close(code=4401)
        return

    run = registry.get(run_id)
    if run is None:
        db.close()
        await websocket.close(code=4404)
        return

    # Release the DB connection now -- it's only needed for the checks
    # above, but `Depends(get_db)` would otherwise hold it open for the
    # entire streaming connection below, which can run for a long time.
    db.close()

    await websocket.accept()
```

Leave the route's signature (`db: Session = Depends(get_db)`) unchanged.

- [ ] **Step 4: Run the new test and the existing WS tests to verify no regression**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_auth.py -k stream_run -v`
Expected: all 4 pass (`rejects_missing_token`, `rejects_invalid_token`,
`accepts_valid_token_for_known_run`, `rejects_token_for_deleted_user`)

- [ ] **Step 5: Confirm `db.close()` is now called before the streaming loop**

Run: `grep -n "db.close()" ui/backend/main.py`
Expected output: three matches inside `stream_run` (the two early-return
branches plus the one right before `await websocket.accept()`).

- [ ] **Step 6: Run the full backend test suite to check for regressions**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add ui/backend/main.py tests/test_auth.py
git commit -m "fix: stop holding a DB session open for the entire WebSocket stream connection"
```

---

### Task 3: Warn (instead of silently dropping) a `ConfigurationError` while loading a KB document

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py:160-166` (`_load_document_chunks`)
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: existing `ConfigurationError` (`bestteam.exceptions`),
  `warnings.warn` (already imported in this file).
- Produces: no interface change — same `_load_document_chunks` behavior,
  now with a warning on this path too.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_knowledge_base.py`, directly after
`test_knowledge_base_skips_corrupt_file_with_warning`:

```python
def test_skips_file_with_configuration_error_and_warns(tmp_path, monkeypatch):
    import bestteam.core.knowledge_base as kb_module

    (tmp_path / "good.txt").write_text("Apples are great fruit.", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("irrelevant", encoding="utf-8")

    real_parse_file = kb_module.parse_file

    def fake_parse_file(path):
        if str(path).endswith("bad.txt"):
            raise ConfigurationError("simulated parse failure")
        return real_parse_file(path)

    monkeypatch.setattr(kb_module, "parse_file", fake_parse_file)

    with pytest.warns(UserWarning, match="bad.txt"):
        kb = LocalFolderKnowledgeBase("kb", tmp_path)

    sources = {chunk.source for chunk in kb._chunks}
    assert "good.txt" in sources
    assert "bad.txt" not in sources
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_knowledge_base.py -k test_skips_file_with_configuration_error_and_warns -v`
Expected: FAIL with `DID NOT WARN` — the `ConfigurationError` branch
currently does a bare `continue` with no warning.

- [ ] **Step 3: Add the warning**

In `src/bestteam/core/knowledge_base.py`, change (current lines 160-166):

```python
        try:
            text = parse_file(str(file_path))
        except ConfigurationError:
            continue
        except Exception as exc:
            warnings.warn(f"Skipping unreadable file '{file_path}': {exc}", stacklevel=2)
            continue
```

to:

```python
        try:
            text = parse_file(str(file_path))
        except ConfigurationError as exc:
            warnings.warn(f"Skipping unreadable file '{file_path}': {exc}", stacklevel=2)
            continue
        except Exception as exc:
            warnings.warn(f"Skipping unreadable file '{file_path}': {exc}", stacklevel=2)
            continue
```

- [ ] **Step 4: Run it to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_knowledge_base.py -k test_skips_file_with_configuration_error_and_warns -v`
Expected: PASS

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "fix: warn when a knowledge-base document is skipped for a ConfigurationError"
```

---

### Task 4: `MonitorPage` — handle `createRun` failures instead of hanging on "Running…"

**Files:**
- Modify: `ui/frontend/src/pages/MonitorPage.jsx`

**Interfaces:**
- Consumes: existing `api.createRun(workflow, input)` (`ui/frontend/src/lib/api.js`).
- Produces: no interface change — internal to `MonitorPage`.

- [ ] **Step 1: Add error state**

In `ui/frontend/src/pages/MonitorPage.jsx`, add a new state variable
directly after line 19 (`const [status, setStatus] = useState('idle') //
idle | running | completed | failed | unreachable`):

```javascript
  const [error, setError] = useState(null)
```

- [ ] **Step 2: Wrap `startRun`'s body in try/catch**

Replace the current `startRun` function (lines 40-67) with:

```javascript
  const startRun = async () => {
    if (!selected || !input.trim() || status === 'running') return

    setEvents([])
    setStatus('running')
    setError(null)
    wsRef.current?.close()

    try {
      const { run_id: runId } = await api.createRun(selected, input)

      const token = localStorage.getItem(TOKEN_KEY)
      const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?token=${encodeURIComponent(token ?? '')}`)
      wsRef.current = ws
      ws.onmessage = (message) => {
        const event = JSON.parse(message.data)
        setEvents((prev) => [...prev, event])
        if (event.type === 'run_completed') setStatus('completed')
        if (event.type === 'run_failed') setStatus('failed')
      }
      ws.onerror = () => {
        setStatus('unreachable')
      }
      ws.onclose = () => {
        // onclose always fires, including after a clean run_completed/run_failed
        // that onmessage already handled -- only downgrade to 'unreachable' if
        // the socket closed while still running.
        setStatus((current) => (current === 'running' ? 'unreachable' : current))
      }
    } catch (e) {
      setError(e.message)
      setStatus('idle')
    }
  }
```

- [ ] **Step 3: Render the error banner**

In the same file, directly after the existing `{status === 'unreachable' &&
...}` block (lines 78-82), add:

```jsx
      {error && <p className="banner banner-error">{error}</p>}
```

- [ ] **Step 4: Verify build and lint**

Run: `cd ui/frontend && npm run lint && npm run build`
Expected: no errors

- [ ] **Step 5: Manual verification**

No test harness exists for this page. Start both dev servers (per root
`CLAUDE.md`'s "Common commands"), open `/`, stop the backend process, then
click "Run" with a workflow selected and some input text. Confirm an error
banner appears with a network-error message and the button returns to
"Run" (not stuck on "Running…"). Restart the backend and confirm a normal
run still completes and the banner clears (it's cleared at the start of
the next `startRun()` call).

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/pages/MonitorPage.jsx
git commit -m "fix: surface createRun() failures in MonitorPage instead of hanging on Running"
```

---

### Task 5: `IntentPage` — disable "Try again" while a retry is in flight

**Files:**
- Modify: `ui/frontend/src/pages/wizard/IntentPage.jsx:91-93`

**Interfaces:** None — single-attribute change, no new interfaces.

- [ ] **Step 1: Add the `disabled` guard**

In `ui/frontend/src/pages/wizard/IntentPage.jsx`, change (current lines 91-93):

```jsx
            <button className="btn btn-secondary" onClick={retry}>
              Try again
            </button>
```

to:

```jsx
            <button className="btn btn-secondary" onClick={retry} disabled={submitting}>
              Try again
            </button>
```

- [ ] **Step 2: Verify build and lint**

Run: `cd ui/frontend && npm run lint && npm run build`
Expected: no errors

- [ ] **Step 3: Manual verification**

No test harness exists for this page. Start both dev servers, walk through
the wizard's intent stage, force an error (e.g. stop the backend right
after submitting), then rapid-double-click "Try again" once the backend is
back up. Confirm the button visibly greys out immediately on the first
click (no window where a second click can fire).

- [ ] **Step 4: Commit**

```bash
git add ui/frontend/src/pages/wizard/IntentPage.jsx
git commit -m "fix: disable IntentPage's Try again button while a retry is in flight"
```

---

### Task 6: `AdvancedPage` — don't let a stale tab's reload overwrite the current tab

**Files:**
- Modify: `ui/frontend/src/pages/AdvancedPage.jsx`

**Interfaces:** None — internal to `AdvancedPage`, no new interfaces.

- [ ] **Step 1: Add an `activeKey` ref and capture the started-for tab in each action**

In `ui/frontend/src/pages/AdvancedPage.jsx`, change the import line (line 1) from:

```javascript
import { useEffect, useState } from 'react'
```

to:

```javascript
import { useEffect, useRef, useState } from 'react'
```

Add a ref directly after `const kind = KINDS.find((k) => k.key === activeKey)` (current line 43):

```javascript
  const activeKeyRef = useRef(activeKey)
  activeKeyRef.current = activeKey
```

- [ ] **Step 2: Guard the `loadItems()` call in `uploadNew`**

Change `uploadNew` (current lines 84-102) from:

```javascript
  const uploadNew = async () => {
    if (!newId.trim() || uploadFiles.length === 0) return
    setUploading(true)
    setError(null)
    setMessage(null)
    try {
      const result = await api.uploadKnowledgeBaseFiles(newId.trim(), uploadFiles)
      setMessage(`Created '${result.name}' — ${result.file_count} file(s), ${result.chunk_count} chunk(s) indexed.`)
      setNewId('')
      setUploadFiles([])
      setSelectedId(result.name)
      setJsonText(JSON.stringify(result.config, null, 2))
      loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setUploading(false)
    }
  }
```

to:

```javascript
  const uploadNew = async () => {
    if (!newId.trim() || uploadFiles.length === 0) return
    const startedFor = activeKey
    setUploading(true)
    setError(null)
    setMessage(null)
    try {
      const result = await api.uploadKnowledgeBaseFiles(newId.trim(), uploadFiles)
      setMessage(`Created '${result.name}' — ${result.file_count} file(s), ${result.chunk_count} chunk(s) indexed.`)
      setNewId('')
      setUploadFiles([])
      setSelectedId(result.name)
      setJsonText(JSON.stringify(result.config, null, 2))
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setUploading(false)
    }
  }
```

- [ ] **Step 3: Guard the `loadItems()` call in `save`**

Change `save` (current lines 104-125) from:

```javascript
  const save = async () => {
    let parsed
    try {
      parsed = JSON.parse(jsonText)
    } catch {
      setError('Not valid JSON')
      return
    }

    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.putConfigItem(activeKey, selectedId, parsed)
      setMessage('Saved.')
      loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }
```

to:

```javascript
  const save = async () => {
    let parsed
    try {
      parsed = JSON.parse(jsonText)
    } catch {
      setError('Not valid JSON')
      return
    }

    const startedFor = activeKey
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.putConfigItem(activeKey, selectedId, parsed)
      setMessage('Saved.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }
```

- [ ] **Step 4: Guard the `loadItems()` call in `remove`**

Change `remove` (current lines 127-143) from:

```javascript
  const remove = async () => {
    if (!selectedId) return
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.deleteConfigItem(activeKey, selectedId)
      setSelectedId(null)
      setJsonText('')
      setMessage('Deleted.')
      loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }
```

to:

```javascript
  const remove = async () => {
    if (!selectedId) return
    const startedFor = activeKey
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.deleteConfigItem(activeKey, selectedId)
      setSelectedId(null)
      setJsonText('')
      setMessage('Deleted.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }
```

- [ ] **Step 5: Verify build and lint**

Run: `cd ui/frontend && npm run lint && npm run build`
Expected: no errors

- [ ] **Step 6: Manual verification**

No test harness exists for this page. Start both dev servers, open
`/advanced`, select "Knowledge bases", create or select an item and click
"Save" — then, before the request resolves (throttle network in devtools
if it completes too fast to switch in time, or just click fast), switch to
the "Agents" tab. Confirm the agents list is not replaced by knowledge-base
data once the save's response arrives.

- [ ] **Step 7: Commit**

```bash
git add ui/frontend/src/pages/AdvancedPage.jsx
git commit -m "fix: don't let a stale tab's save/delete/upload overwrite the current tab's list"
```

---

## Verification (end-to-end, after all 6 tasks)

1. `./.venv/Scripts/python.exe -m pytest -q` — full suite passes, including
   all new tests from Tasks 1-3 (previously 218 tests, now 222).
2. `cd ui/frontend && npm run lint && npm run build` — no errors.
3. All three frontend manual-verification steps (Tasks 4, 5, 6) re-confirmed
   in one pass through the dev server.
4. `git log --oneline -6` shows one commit per task, in order.
