# Draft Session Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a customer permanently delete a never-deployed draft team (`spec`/`solution`/`testing` session with `workflow_id IS NULL`) from the My Teams page.

**Architecture:** One new backend route, `DELETE /api/builder/sessions/{id}`, guarded by `workflow_id IS NULL` (409 otherwise) and org ownership (404 otherwise); it deletes the DB row and the session's on-disk workspace directory. The frontend exposes a "Delete" button only on cards whose session has no `workflow_id`, gated by a `window.confirm` dialog.

**Tech Stack:** FastAPI + SQLAlchemy (backend), React + Vitest/Testing Library (frontend). No new dependencies, no migration.

## Global Constraints

- Full design: `docs/superpowers/specs/2026-07-31-draft-session-deletion-design.md`.
- `workflow_id`, not `status`, is the only valid "never deployed" signal — a session's `status` can regress below `deployed` while still linked to a live team (see design doc, "Why `workflow_id`, not `status`").
- Deleting a session with `workflow_id` set must be refused (`409`), even though the frontend won't offer that path — defense in depth.
- Deleting must also remove the session's on-disk workspace directory (`ui/backend/data/builder_sessions/<id>/`), best-effort (log on failure, don't fail the request).
- No schema change, no new dependency.

---

### Task 1: Backend — `DELETE /api/builder/sessions/{id}`

**Files:**
- Modify: `ui/backend/db/builder_sessions.py`
- Modify: `ui/backend/builder.py`
- Test: `tests/test_builder_api.py`

**Interfaces:**
- Consumes: existing `_get_session_or_404(db, session_id, org_id)` (`ui/backend/builder.py`), existing `get_session(db, session_id)` (`ui/backend/db/builder_sessions.py`), existing `_SESSIONS_DIR` constant (`ui/backend/builder.py`, a `pathlib.Path`).
- Produces: `delete_session(db: Session, session_id: str) -> None` (`ui/backend/db/builder_sessions.py`, raises `LookupError` if the id is unknown — mirrors `update_session`/`append_feedback` in the same file). `_session_to_dict(...)` now includes `"workflow_id": Optional[int]` in its returned dict — Task 2 (frontend) reads this field to decide whether to show the Delete button.

- [ ] **Step 1: Write the failing tests**

Open `tests/test_builder_api.py`. Add these five test functions at the end of the file (after the last existing test, `test_deploy_stamps_workflow_with_session_org`):

```python
def test_delete_never_deployed_session_removes_it(client, tmp_path, monkeypatch):
    from ui.backend import builder as builder_module

    monkeypatch.setattr(builder_module, "_SESSIONS_DIR", tmp_path)

    session_id = client.post("/api/builder/sessions", json={"intent_text": "abandoned idea"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    workspace = tmp_path / session_id
    assert workspace.exists()

    resp = client.delete(f"/api/builder/sessions/{session_id}")
    assert resp.status_code == 204
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 404
    assert not workspace.exists()


def test_delete_deployed_session_is_refused(client):
    session_id = _make_deployable_session(client)
    assert client.post(f"/api/builder/sessions/{session_id}/deploy").status_code == 200

    resp = client.delete(f"/api/builder/sessions/{session_id}")
    assert resp.status_code == 409
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 200


def test_delete_unknown_session_is_404(client):
    assert client.delete("/api/builder/sessions/does-not-exist").status_code == 404


def test_delete_another_orgs_session_is_404(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "Org A's bot"}).json()["id"]
    bob_token = create_user_and_login(client, username="bob", org="orgb")
    bob = {"Authorization": f"Bearer {bob_token}"}

    assert client.delete(f"/api/builder/sessions/{session_id}", headers=bob).status_code == 404
    # Still there -- the owning org can still see it (delete didn't leak through).
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 200


def test_session_dict_exposes_workflow_id(client):
    session_id = _make_deployable_session(client)
    assert client.get(f"/api/builder/sessions/{session_id}").json()["workflow_id"] is None

    client.post(f"/api/builder/sessions/{session_id}/deploy")
    assert client.get(f"/api/builder/sessions/{session_id}").json()["workflow_id"] is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_builder_api.py -k "delete_never_deployed or delete_deployed_session or delete_unknown or delete_another_orgs or exposes_workflow_id" -v`

Expected: all 5 FAIL. `test_delete_*` should fail with a `405 Method Not Allowed` (no `DELETE` route exists yet) surfacing as an assertion error (`assert 405 == 204` etc.); `test_session_dict_exposes_workflow_id` should fail with a `KeyError: 'workflow_id'`. If any test errors instead (e.g. `NameError`, import error), fix the test code itself before moving on — don't proceed with a wrong-reason failure.

- [ ] **Step 3: Add `delete_session` to the DB layer**

In `ui/backend/db/builder_sessions.py`, add this function after `append_feedback` (at the end of the file):

```python
def delete_session(db: Session, session_id: str) -> None:
    """Permanently remove a builder session. Callers are responsible for any
    ownership or "never deployed" guard before calling this -- see
    `ui/backend/builder.py::delete_builder_session`."""
    session = get_session(db, session_id)
    if session is None:
        raise LookupError(f"Unknown builder session '{session_id}'")
    db.delete(session)
    db.commit()
```

- [ ] **Step 4: Wire the route and expose `workflow_id`**

In `ui/backend/builder.py`:

1. Add two stdlib imports at the top of the file (after the `"""..."""` module docstring, before `from __future__ import annotations` stays first — add these two lines right after `from __future__ import annotations` and its blank line, before `from pathlib import Path`):

```python
import logging
import shutil
```

2. Add a module logger right after the `router = APIRouter(...)` line (currently line 49):

```python
logger = logging.getLogger(__name__)
```

3. Change the import line (currently line 27):

```python
from .db.builder_sessions import append_feedback, create_session, get_session, list_sessions, update_session
```

to:

```python
from .db.builder_sessions import append_feedback, create_session, delete_session, get_session, list_sessions, update_session
```

4. In `_session_to_dict` (currently lines 72-92), add `"workflow_id"` to the returned dict, right after `"status"`:

```python
    return {
        "id": session.id,
        "intent_text": session.intent_text,
        "as_is_text": session.as_is_text,
        "requirements_json": session.requirements_json,
        "specification_json": session.specification_json,
        "status": session.status,
        "workflow_id": session.workflow_id,
        "feedback_history": session.feedback_history,
        "uses_email": uses_email,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }
```

5. Add the new route right after `get_builder_session` (currently ends at line 292, right before the blank lines leading into `class CreateRequirementsRequest`-style request models further down — insert immediately after the `get_builder_session` function):

```python
@router.delete("/{session_id}", status_code=204)
def delete_builder_session(
    session_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> None:
    """Delete a session that was never deployed (`workflow_id IS NULL`) --
    the "abandoned draft" case. A session that has ever gone live has no
    delete path here (see docs/superpowers/specs/2026-07-31-draft-session-deletion-design.md);
    the frontend never offers this for a `workflow_id`-linked session, but
    this guard holds even if the route is called directly."""
    session = _get_session_or_404(db, session_id, org.id)
    if session.workflow_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This team is live -- it can't be deleted from here yet.",
        )
    delete_session(db, session_id)
    try:
        shutil.rmtree(_SESSIONS_DIR / session_id)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning(
            "Failed to remove workspace directory for deleted session %s", session_id, exc_info=True
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_builder_api.py -v`

Expected: all tests in the file PASS (the 5 new ones plus every pre-existing test in `test_builder_api.py` still green — no regressions).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/db/builder_sessions.py ui/backend/builder.py tests/test_builder_api.py
git commit -m "feat(builder): allow deleting a never-deployed draft session

DELETE /api/builder/sessions/{id} removes a session and its on-disk
workspace, guarded to sessions with no workflow_id (never deployed).
Refuses 409 for a live-linked session, 404 for unknown/other-org.
_session_to_dict now exposes workflow_id so the frontend can tell
which sessions are safe to offer deletion for."
```

---

### Task 2: Frontend — Delete button on My Teams

**Files:**
- Modify: `ui/frontend/src/lib/api.js`
- Modify: `ui/frontend/src/pages/wizard/SessionsPage.jsx`
- Modify: `ui/frontend/src/pages/wizard/SessionsPage.css`
- Test: `ui/frontend/src/pages/wizard/SessionsPage.test.jsx`

**Interfaces:**
- Consumes: `api.deleteSession(id)` (new, this task), `session.workflow_id` (from Task 1's `_session_to_dict`), existing `sessions`/`setSessions`/`error`/`setError` state in `SessionsPage.jsx`.
- Produces: nothing consumed by a later task (this is the last task in the plan).

- [ ] **Step 1: Write the failing tests**

Open `ui/frontend/src/pages/wizard/SessionsPage.test.jsx`. First, add `deleteSession: vi.fn()` to the `vi.mock('../../lib/api', ...)` block at the top of the file, so it reads:

```js
vi.mock('../../lib/api', () => ({
  api: {
    listSessions: vi.fn(),
    getEmailTrigger: vi.fn(),
    deleteSession: vi.fn(),
  },
}))
```

Then add this new `describe` block at the end of the file:

```js
describe('SessionsPage draft deletion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })
  })

  it('shows a Delete button for a session that was never deployed', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: null })] })

    renderPage()

    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('does not show a Delete button for a session linked to a live team', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: 7 })] })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('does nothing if the user cancels the confirmation', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: null })] })
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(api.deleteSession).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('deletes the session and removes its card when confirmed', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ id: 's1', workflow_id: null })] })
    api.deleteSession.mockResolvedValue(null)
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(api.deleteSession).toHaveBeenCalledWith('s1')
    await waitFor(() => expect(screen.queryByText('my-team')).not.toBeInTheDocument())
  })

  it('shows an error banner and keeps the card if deletion fails', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: null })] })
    api.deleteSession.mockRejectedValue(new Error("Can't delete right now"))
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(await screen.findByText("Can't delete right now")).toBeInTheDocument()
    expect(screen.getByText('my-team')).toBeInTheDocument()
  })
})
```

This test file doesn't import `waitFor` yet. Update its import line (currently `import { act, fireEvent, render, screen } from '@testing-library/react'`) to:

```js
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui/frontend && npx vitest run src/pages/wizard/SessionsPage.test.jsx -t "draft deletion"`

Expected: all 5 new tests FAIL. The first ("shows a Delete button...") should fail with a `TestingLibraryElementError: Unable to find role="button" ... name "Delete"` (no Delete button exists yet) — that's the right kind of failure. If any test errors for an unrelated reason (e.g. a syntax error from the edit), fix that first.

- [ ] **Step 3: Add `api.deleteSession`**

In `ui/frontend/src/lib/api.js`, add this line right after `listSessions: () => request('/api/builder/sessions'),` (currently line 222):

```js
  deleteSession: (id) => request(`/api/builder/sessions/${id}`, { method: 'DELETE' }),
```

- [ ] **Step 4: Add the Delete button and handler to `SessionsPage.jsx`**

Open `ui/frontend/src/pages/wizard/SessionsPage.jsx`. Inside the `SessionsPage` component, add this handler right after the `statusGroups` computation (after the line `})).filter((group) => group.sessions.length > 0)`, before the `return (`):

```js
  const handleDelete = async (session) => {
    const label = session.specification_json?.name ?? session.intent_text
    if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return
    try {
      await api.deleteSession(session.id)
      setSessions((prev) => prev.filter((s) => s.id !== session.id))
    } catch (e) {
      setError(e.message)
    }
  }
```

Then change the card's `<li>` (currently `<li key={session.id}>`) and the markup around the card's closing `</button>` so the Delete button is a **sibling** of the card button, not nested inside it (a `<button>` cannot contain another interactive `<button>`). Replace this block:

```jsx
                return (
                  <li key={session.id}>
                    <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                      <h3>{teamName ?? session.intent_text}</h3>
                      <p className="subtitle">{descriptionFor(session)}</p>
                      {isAutomated && (
                        <p className="hint automation-tag">
                          {AUTOMATION_STATUS_LABELS[trigger.status] ?? trigger.status}
                        </p>
                      )}
                      <div className="session-card-footer">
                        <span className="session-updated">Updated {formatDateTime(session.updated_at)}</span>
                      </div>
                    </button>
                  </li>
                )
```

with:

```jsx
                return (
                  <li key={session.id} className="session-item">
                    <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                      <h3>{teamName ?? session.intent_text}</h3>
                      <p className="subtitle">{descriptionFor(session)}</p>
                      {isAutomated && (
                        <p className="hint automation-tag">
                          {AUTOMATION_STATUS_LABELS[trigger.status] ?? trigger.status}
                        </p>
                      )}
                      <div className="session-card-footer">
                        <span className="session-updated">Updated {formatDateTime(session.updated_at)}</span>
                      </div>
                    </button>
                    {session.workflow_id == null && (
                      <button
                        type="button"
                        className="session-delete-button"
                        onClick={() => handleDelete(session)}
                      >
                        Delete
                      </button>
                    )}
                  </li>
                )
```

- [ ] **Step 5: Add CSS for the Delete button**

In `ui/frontend/src/pages/wizard/SessionsPage.css`, add this at the end of the file:

```css
.session-item {
  position: relative;
}

.session-delete-button {
  position: absolute;
  top: 14px;
  right: 14px;
  background: none;
  border: none;
  color: #b91c1c;
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
  padding: 4px 8px;
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd ui/frontend && npx vitest run src/pages/wizard/SessionsPage.test.jsx`

Expected: all tests in the file PASS (the 5 new ones plus every pre-existing test in this file still green).

- [ ] **Step 7: Run the full frontend suite and lint**

Run: `cd ui/frontend && npx vitest run`

Expected: every test file passes, no regressions elsewhere (e.g. in `ActivityPage.test.jsx`, which shares no code with this change but must stay green).

Run: `cd ui/frontend && npx eslint src/lib/api.js src/pages/wizard/SessionsPage.jsx src/pages/wizard/SessionsPage.test.jsx`

Expected: no output (clean).

- [ ] **Step 8: Commit**

```bash
git add ui/frontend/src/lib/api.js ui/frontend/src/pages/wizard/SessionsPage.jsx ui/frontend/src/pages/wizard/SessionsPage.css ui/frontend/src/pages/wizard/SessionsPage.test.jsx
git commit -m "feat(teams): let customers delete never-deployed draft teams

Delete button on My Teams cards, shown only when workflow_id is null
(never deployed -- see docs/superpowers/specs/2026-07-31-draft-session-deletion-design.md).
Confirms via window.confirm (matching the app's existing destructive-
action pattern), removes the card optimistically on success, and
surfaces a banner without removing the card on failure."
```

---

## Final check (after both tasks)

- [ ] Run the full backend test suite (`.\.venv\Scripts\python.exe -m pytest`) and the full frontend suite (`cd ui/frontend && npx vitest run`) one more time together — both green, no regressions.
- [ ] Confirm `git log --oneline -5` shows both commits from this plan on top of the design-doc commit.
