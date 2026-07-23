# Email-Trigger Correctness Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an autonomous email-trigger run process exactly the poller-detected UIDs (never the triage skill's "latest 20 UNSEEN"), advance state only through a durable run, keep workflow errors until a team actually runs, and re-baseline/disable the trigger when the mailbox changes.

**Architecture:** Add a scoped mode to the SDK email toolkit that confines the three tools to an explicit IMAP-UID set; build a per-run (uncached) trigger workflow wired to those scoped tools; rewrite `poll_org` to take a bounded batch, build-first, persist a durable run row before advancing state; make the credential-write endpoints trigger-aware.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0, stdlib `imaplib` (via `_ImapBackend`), asyncio; React/Vite frontend. Spec: `docs/superpowers/specs/2026-07-20-email-trigger-correctness-redesign-design.md`.

## Global Constraints

- Python is ALWAYS `./.venv/Scripts/python.exe` (Windows venv); run backend tests as `./.venv/Scripts/python.exe -m pytest <path> -q` from repo root `C:/Projects/MyBestTeam`.
- Branch: `feature/email-trigger-autonomous-runs` (already checked out; the spec is committed there). NEVER commit `docs/STATUS.md` unless a task explicitly says so, and never any `.superpowers/` scratch or scratch `.db` files.
- The full backend suite must be run with a scratch DB because the local dev DB has stored encrypted credentials that trip the import-time secrets guard: `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q`, then delete the scratch file.
- TDD for all backend logic: failing test first, watch it fail for the right reason, minimal code, watch it pass. Frontend (no JS test harness) is verified by `npm run lint` + `npm run build` in `ui/frontend`.
- The email toolkit is READ-ONLY and never sends: scoped tools must not mark mail seen or add any send path.
- Autonomous runs use the sentinel username exactly `email-trigger` (constant `TRIGGER_USERNAME` in `ui/backend/email_trigger.py`).
- Batch size env var: `BESTTEAM_TRIGGER_BATCH_SIZE`, default `20`.
- Customer-facing strings never leak internals (env-var names, `WinError`, tracebacks).
- Interactive/`_get_workflow` behavior and the `(org,name)` workflow cache MUST NOT change.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Verified precondition (do not re-litigate)

The toolkit already operates in IMAP-UID space: `_ImapBackend.find` returns `"id": <uid>`, and `read`/`draft_reply` fetch via `conn.uid("fetch", message_id, ...)` (`src/bestteam/tools/email_client.py:344-438`). The poller's `check_mailbox` uses the same `conn.uid("search")` UIDs. So scoping is pure membership filtering on that id — no backend UID/Message-ID correction is needed.

## File structure

| File | Change | Task |
|---|---|---|
| `src/bestteam/tools/email_client.py` | `_ImapBackend.summaries_for`; `make_email_tools(backend, allowed_uids=)` scoped mode; extract `_format_summaries` | 1 |
| `ui/backend/runtime.py` | `run_in_background` reuses an existing `runs` row instead of always inserting | 2 |
| `ui/backend/email_tools.py` | extract `build_org_imap_backend(db, org_id)` | 3 |
| `ui/backend/email_trigger.py` | `build_trigger_workflow`; `batch_size()`; `poll_org`/`_start_triggered_run` rewrite; error retention; `poll_once` rollback | 3,4,6 |
| `ui/backend/main.py` | lifespan injects `build_trigger_workflow` instead of `_get_workflow` | 4 |
| `ui/backend/org_settings.py` | credential-write endpoints disable the trigger on mailbox-identity change / disconnect | 5 |
| `ui/backend/email_trigger_api.py` | server-side autonomous filter on the activity endpoint | 6 |
| `ui/backend/db/users.py` | reserve the `email-trigger` username | 6 |
| docs (`.env.example`, `deployment.md`, `ui/backend/CLAUDE.md`, `docs/STATUS.md`, the MVP spec's scope note) | 7 |

---

### Task 1: SDK — UID-scoped email tools

**Files:**
- Modify: `src/bestteam/tools/email_client.py`
- Test: `tests/test_email_scoped_tools.py` (create)

**Interfaces:**
- Consumes: existing `_find_impl`, `_read_impl`, `_draft_impl`, `email_find`/`email_read`/`email_draft_reply` docstrings, `_imap_fetch_bytes`, `_imap_logout`.
- Produces:
  - `_ImapBackend.summaries_for(uids: Iterable) -> list[dict]` — header summaries for exactly `uids` (read-only, no search), same dict shape as `find`.
  - `make_email_tools(backend, allowed_uids=None) -> dict[str, callable]` — `allowed_uids=None` is today's behavior; a set/iterable of UIDs confines `email_find` to those (ignoring its query) and makes `email_read`/`email_draft_reply` refuse any id outside the set with the string `"That message isn't part of this batch of new mail."`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_scoped_tools.py`:

```python
"""UID-scoped email tools: an autonomous run may only touch its detected batch."""

import pytest

from bestteam.tools.email_client import make_email_tools

_OUT_OF_BATCH = "That message isn't part of this batch of new mail."


class _FakeBackend:
    """Records calls; find() would return the whole inbox if not scoped."""

    def __init__(self):
        self.read_calls = []
        self.draft_calls = []

    def find(self, query):
        # The unscoped path -- returns "everything" so a scoping bug is visible.
        return [{"id": "99", "from": "x@x", "subject": "unrelated", "date": "d", "snippet": ""}]

    def summaries_for(self, uids):
        return [{"id": str(u), "from": "a@b", "subject": f"s{u}", "date": "d", "snippet": ""}
                for u in uids]

    def read(self, message_id):
        self.read_calls.append(message_id)
        return {"id": message_id, "from": "a@b", "to": "", "subject": "s", "date": "d", "body": "hi"}

    def draft_reply(self, message_id, body):
        self.draft_calls.append((message_id, body))
        return f"Draft reply saved (reply to message {message_id})."


def test_scoped_find_ignores_query_and_shows_only_the_batch():
    b = _FakeBackend()
    tools = make_email_tools(b, allowed_uids={42, 43, 45})
    out = tools["email_find"]("anything at all")
    assert "42" in out and "43" in out and "45" in out
    assert "99" not in out  # the unscoped find() result never leaks in


def test_scoped_read_refuses_out_of_batch_uid():
    b = _FakeBackend()
    tools = make_email_tools(b, allowed_uids={42, 43})
    assert tools["email_read"]("44") == _OUT_OF_BATCH
    assert b.read_calls == []  # backend never touched
    assert "hi" in tools["email_read"]("42")  # in-batch works
    assert b.read_calls == ["42"]


def test_scoped_draft_refuses_out_of_batch_uid():
    b = _FakeBackend()
    tools = make_email_tools(b, allowed_uids={42})
    assert tools["email_draft_reply"]("77", "body") == _OUT_OF_BATCH
    assert b.draft_calls == []
    tools["email_draft_reply"]("42", "body")
    assert b.draft_calls == [("42", "body")]


def test_unscoped_mode_is_unchanged():
    b = _FakeBackend()
    tools = make_email_tools(b)  # allowed_uids=None
    out = tools["email_find"]("")
    assert "99" in out  # uses backend.find(), today's behavior
    assert "hi" in tools["email_read"]("99")
```

Also create `tests/test_imap_summaries_for.py` (the backend method, with a fake IMAP connection):

```python
"""_ImapBackend.summaries_for fetches exactly the given UIDs, read-only."""

import pytest

from bestteam.tools.email_client import _ImapBackend

_HDR = b"From: a@b\r\nSubject: hello\r\nDate: today\r\n\r\n"


class _FakeConn:
    def __init__(self):
        self.selected_readonly = None
        self.fetched = []

    def select(self, mailbox, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        assert command == "fetch"
        self.fetched.append(args[0])
        return "OK", [(b"1 (UID x)", _HDR)]

    def logout(self):
        pass


def test_summaries_for_fetches_given_uids_readonly(monkeypatch):
    backend = _ImapBackend(host="h", user="u", password="p")
    conn = _FakeConn()
    monkeypatch.setattr(backend, "_connect", lambda: conn)
    out = backend.summaries_for([42, 43])
    assert conn.selected_readonly is True
    assert conn.fetched == [b"42", b"43"]  # exactly these, as UID fetches
    assert len(out) == 2 and out[0]["subject"] == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_scoped_tools.py tests/test_imap_summaries_for.py -q`
Expected: FAIL — `make_email_tools()` takes no `allowed_uids`; `_ImapBackend` has no `summaries_for`.

- [ ] **Step 3: Implement**

In `src/bestteam/tools/email_client.py`, refactor the header-fetch loop out of `_ImapBackend.find` into a shared helper and add `summaries_for`. Replace the body of `find` (lines ~344-384) so its fetch loop calls the helper, and add the two methods:

```python
    def _fetch_summaries(self, conn, uids) -> List[Dict[str, Any]]:
        messages = []
        for uid in uids:
            if isinstance(uid, int):
                uid = str(uid)
            if isinstance(uid, str):
                uid = uid.encode()
            typ, msg_data = conn.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            raw = _imap_fetch_bytes(typ, msg_data)
            if raw is None:
                continue
            headers = message_from_bytes(raw, policy=policy.default)
            messages.append(
                {
                    "id": uid.decode(),
                    "from": str(headers.get("From", "")),
                    "subject": str(headers.get("Subject", "")),
                    "date": str(headers.get("Date", "")),
                    "snippet": "",
                }
            )
        return messages

    def summaries_for(self, uids) -> List[Dict[str, Any]]:
        """Header summaries for exactly `uids` (read-only, no search) -- the
        autonomous-trigger path, where the batch is already known."""
        conn = self._connect()
        try:
            conn.select("INBOX", readonly=True)
            return self._fetch_summaries(conn, uids)
        finally:
            _imap_logout(conn)
```

Change `find`'s fetch loop (after it computes `uids = data[0].split()[-_MAX_RESULTS:]`) to `return self._fetch_summaries(conn, uids)` in place of the inline loop.

Add a module-level formatting helper and the out-of-batch constant near `_find_impl` (extract the "Found N message(s)" formatting so both paths share it):

```python
_OUT_OF_BATCH = "That message isn't part of this batch of new mail."


def _format_summaries(messages) -> str:
    lines = []
    for m in messages:
        fields = [m["id"], m["from"], m["subject"], m["date"]]
        snippet = (m.get("snippet") or "").strip()
        if snippet:
            fields.append(snippet[:_SNIPPET_CHARS])
        lines.append(" · ".join(str(field) for field in fields))
    return f"Found {len(messages)} message(s):\n" + "\n".join(lines)
```

Change `_find_impl` to reuse it:

```python
def _find_impl(backend, query: str) -> str:
    messages = backend.find(query.strip())
    if not messages:
        if query.strip():
            return f"No emails found matching '{query.strip()}'."
        return "No unread emails in the inbox."
    return _format_summaries(messages)
```

Replace `make_email_tools` with the scoped version:

```python
def make_email_tools(backend, allowed_uids=None) -> Dict[str, Any]:
    """Return the three email tools bound to `backend`.

    `allowed_uids=None` -> unchanged behavior. A set/iterable of IMAP UIDs
    confines the tools to that batch: `email_find` ignores its query and lists
    only those messages, and `email_read`/`email_draft_reply` refuse any id
    outside the set. Used by the autonomous-trigger path so a run can only ever
    touch the messages the poller detected.
    """
    allowed = None if allowed_uids is None else {str(u) for u in allowed_uids}

    @functools.wraps(email_find)
    def find(query: str = "") -> str:
        if allowed is None:
            return _find_impl(backend, query)
        messages = backend.summaries_for(sorted(allowed, key=int))
        if not messages:
            return "No new messages in this batch."
        return _format_summaries(messages)

    @functools.wraps(email_read)
    def read(message_id: str) -> str:
        if allowed is not None and message_id.strip() not in allowed:
            return _OUT_OF_BATCH
        return _read_impl(backend, message_id)

    @functools.wraps(email_draft_reply)
    def draft_reply(message_id: str, body: str) -> str:
        if allowed is not None and message_id.strip() not in allowed:
            return _OUT_OF_BATCH
        if not body.strip():
            raise ConfigurationError("email_draft_reply requires a non-empty body")
        return _draft_impl(backend, message_id, body)

    return {"email_find": find, "email_read": read, "email_draft_reply": draft_reply}
```

- [ ] **Step 4: Run tests to verify they pass, plus the existing email tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_scoped_tools.py tests/test_imap_summaries_for.py tests/test_email_tls_security.py tests/test_load_email_tools.py -q`
Expected: all pass (the last two confirm the unscoped path and per-org loader are unbroken).

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/tools/email_client.py tests/test_email_scoped_tools.py tests/test_imap_summaries_for.py
git commit -m "feat(email): UID-scoped email tools + summaries_for (autonomous-run confinement)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `run_in_background` reuses an existing runs row

**Files:**
- Modify: `ui/backend/runtime.py` (the up-front persist block, ~lines 88-103)
- Test: `tests/test_runtime_run_row.py` (create)

**Interfaces:**
- Produces: `run_in_background` inserts a `runs` row only if none exists for `run_id`; otherwise it updates the existing row's terminal status/output. Enables the trigger path (Task 4) to persist the row before dispatch (durable activity record) without a primary-key clash.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_run_row.py`:

```python
"""run_in_background must not double-insert a runs row the caller pre-persisted."""

import pytest

pytest.importorskip("sqlalchemy")

from bestteam import Workflow
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run
from ui.backend.runtime import run_in_background


def _engine():
    e = make_engine(":memory:")
    init_db(e)
    return e


def test_reuses_preexisting_run_row_and_sets_terminal_status():
    engine = _engine()
    Session = session_factory(engine)
    with Session() as s:
        s.add(Run(id="r1", workflow="w", input="in", status="running", username="email-trigger"))
        s.commit()
    wf = Workflow.from_spec({
        "name": "w",
        "agents": [{"name": "a", "role": "R", "goal": "g", "model": "fake:done"}],
        "teams": [{"name": "t", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["t"]},
    })
    run_in_background("r1", wf, "in", engine=engine, username="email-trigger")
    with Session() as s:
        rows = s.query(Run).filter_by(id="r1").all()
        assert len(rows) == 1  # not double-inserted
        assert rows[0].status in ("completed", "failed")
        assert rows[0].username == "email-trigger"
```

Note: if `Workflow.from_spec` is not the constructor in this codebase, use the same construction the existing runtime tests use — check `tests/` for a `fake:`-model Workflow builder and mirror it; the assertion targets (single row, terminal status) are what matters.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime_run_row.py -q`
Expected: FAIL — the current code always `db.add(Run(...))`, producing a duplicate-id row / IntegrityError on commit.

- [ ] **Step 3: Implement**

In `ui/backend/runtime.py`, replace the up-front insert (the block that builds `run_row = Run(...)` then `db.add(run_row); db.commit()`) with get-or-create:

```python
            run_row = db.get(Run, run_id)
            if run_row is None:
                run_row = Run(
                    id=run_id,
                    workflow=getattr(workflow, "name", ""),
                    input=input,
                    org_id=org_id,
                    username=username,
                )
                db.add(run_row)
            else:
                # A caller (the autonomous trigger) already persisted this row
                # before dispatch as a durable activity record; keep it.
                run_row.workflow = getattr(workflow, "name", "") or run_row.workflow
            db.commit()
```

Leave the rest of `run_in_background` (terminal-status update, usage, CR-003 catch-all) unchanged.

- [ ] **Step 4: Run test to verify it passes, plus existing runtime/run tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime_run_row.py -q && ./.venv/Scripts/python.exe -m pytest -q -k "runtime or runs or usage"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/runtime.py tests/test_runtime_run_row.py
git commit -m "feat(runtime): reuse a pre-persisted runs row instead of double-inserting

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Shared org-backend helper + trigger-run workflow builder

**Files:**
- Modify: `ui/backend/email_tools.py` (extract `build_org_imap_backend`)
- Modify: `ui/backend/email_trigger.py` (add `build_trigger_workflow`, `batch_size`)
- Test: `tests/test_trigger_workflow_builder.py` (create)

**Interfaces:**
- Consumes: Task 1's `make_email_tools(backend, allowed_uids=)`; `_build_workflow` (`bestteam.core.loader`), `contain_workflow_config_for_load`/`ensure_workflow_cache_paths_for_source`/`load_knowledge_base_tools` (`ui.backend.knowledge_bases`), `load_skills` (`ui.backend.skills`), `WorkflowRecord`, `get_email_credentials`, `secret_store`.
- Produces:
  - `email_tools.build_org_imap_backend(db, org_id) -> _ImapBackend | None` — the org's mailbox backend from stored credentials (decrypts), or None if no credentials; raises `secret_store.SecretsKeyError`/`InvalidToken` on a bad key (caller handles).
  - `email_trigger.batch_size() -> int` (env `BESTTEAM_TRIGGER_BATCH_SIZE`, default 20).
  - `email_trigger.build_trigger_workflow(name, db, org_id, allowed_uids) -> Workflow` — an **uncached** workflow for `(org_id, name)` whose email tools are scoped to `allowed_uids`; raises on a missing/invalid team or a missing mailbox.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trigger_workflow_builder.py`:

```python
"""build_trigger_workflow wires an uncached workflow to UID-scoped email tools."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet

from helpers import open_test_db
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import set_email_credentials
from ui.backend.db.models import WorkflowRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend import email_trigger
from ui.backend import email_tools

_TEAM = {
    "name": "triage",
    "agents": [{"name": "t", "role": "Triager", "goal": "triage",
                "model": "fake:done", "skills": ["email_triage_reply"]}],
    "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    s = Session()
    yield s
    s.close()


def _seed(db):
    from ui.backend.skills import seed_default_skills
    seed_default_skills(db)
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="imap.acme.com", username="u@acme.com", password="pw")
    db.add(WorkflowRecord(name="triage", org_id=org.id, config=_TEAM, status="deployed"))
    db.commit()
    return org.id


def test_build_trigger_workflow_scopes_email_tools(db, monkeypatch):
    org_id = _seed(db)
    captured = {}

    def fake_make(backend, allowed_uids=None):
        captured["allowed"] = allowed_uids
        return {"email_find": lambda q="": "", "email_read": lambda m: "",
                "email_draft_reply": lambda m, b: ""}

    monkeypatch.setattr(email_trigger, "make_email_tools", fake_make)
    wf = email_trigger.build_trigger_workflow("triage", db, org_id, {42, 43})
    assert wf is not None
    assert captured["allowed"] == {42, 43}  # scoped to the batch


def test_build_trigger_workflow_raises_on_missing_team(db):
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="h", username="u", password="pw")
    with pytest.raises(Exception):
        email_trigger.build_trigger_workflow("nope", db, org.id, {1})


def test_batch_size_default_and_override(monkeypatch):
    monkeypatch.delenv("BESTTEAM_TRIGGER_BATCH_SIZE", raising=False)
    assert email_trigger.batch_size() == 20
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "5")
    assert email_trigger.batch_size() == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_trigger_workflow_builder.py -q`
Expected: FAIL — `email_trigger` has no `build_trigger_workflow`/`batch_size`; `email_trigger.make_email_tools` isn't imported yet.

- [ ] **Step 3: Implement**

In `ui/backend/email_tools.py`, extract the backend-build (currently inline in `load_email_tools`) into a reusable function and call it from `load_email_tools`:

```python
def build_org_imap_backend(db: Session, org_id: int):
    """The org's IMAP backend from stored credentials, or None if unconnected.

    Decrypts the password; raises secret_store.SecretsKeyError / InvalidToken on
    a bad/rotated key (the caller decides how to surface that).
    """
    cred = get_email_credentials(db, org_id)
    if cred is None:
        return None
    password = secret_store.decrypt(cred.password_encrypted)
    return _ImapBackend(
        host=cred.host,
        user=cred.username,
        password=password,
        port=cred.port,
        drafts=cred.drafts_folder,
        restrict_to_public=True,  # customer-supplied host: validate + pin on connect
    )
```

Refactor `load_email_tools` to use it (behavior identical): where it currently decrypts and builds `_ImapBackend`, call `backend = build_org_imap_backend(db, org_id)` inside the existing `try/except (InvalidToken, SecretsKeyError)`, and keep the `if cred is None`/env-fallback structure (call `get_email_credentials` once to decide connected-vs-not, then `build_org_imap_backend` for the backend). Keep the friendly `_UNREADABLE`/`_NOT_CONNECTED` behavior exactly.

In `ui/backend/email_trigger.py`, add the imports and the two functions. Add to the import block:

```python
from pathlib import Path

from bestteam.core.loader import _build_workflow
from bestteam.tools.email_client import make_email_tools
from .email_tools import build_org_imap_backend
from .knowledge_bases import (
    contain_workflow_config_for_load,
    ensure_workflow_cache_paths_for_source,
    load_knowledge_base_tools,
)
from .skills import load_skills
from .db.models import WorkflowRecord

_WORKFLOWS_DIR = Path(__file__).resolve().parent / "workflows"

BATCH_SIZE_ENV = "BESTTEAM_TRIGGER_BATCH_SIZE"


def batch_size() -> int:
    return int(os.environ.get(BATCH_SIZE_ENV, "").strip() or 20)


def build_trigger_workflow(name: str, db: Session, org_id: int, allowed_uids):
    """An UNCACHED workflow for (org_id, name) whose email tools are confined to
    `allowed_uids`. Mirrors main._get_workflow's DB-record build but substitutes
    scoped email tools; not cached because the UID set is per-run. Raises on a
    missing/invalid team or a missing mailbox connection."""
    record = db.query(WorkflowRecord).filter_by(name=name, org_id=org_id).one_or_none()
    if record is None:
        raise ValueError(f"No team named '{name}' for org {org_id}")
    backend = build_org_imap_backend(db, org_id)
    if backend is None:
        raise ValueError("No mailbox is connected for this org")
    source = _WORKFLOWS_DIR / f"{name}.yaml"
    kb_tools = load_knowledge_base_tools(db, record.config, source, org_id=org_id)
    email_tools = make_email_tools(backend, allowed_uids=allowed_uids)
    skills = load_skills(db, org_id)
    config = contain_workflow_config_for_load(record.config)
    ensure_workflow_cache_paths_for_source(config, source)
    return _build_workflow(
        config,
        source=source,
        extra_tools={**kb_tools, **email_tools},
        extra_skills=skills,
    )
```

(If `Session` isn't already imported in `email_trigger.py`, add `from sqlalchemy.orm import Session`.)

- [ ] **Step 4: Run tests to verify they pass, plus the email-tools loader tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_trigger_workflow_builder.py tests/test_load_email_tools.py tests/test_org_settings.py -q`
Expected: all pass (the loader refactor didn't change `load_email_tools` behavior).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_tools.py ui/backend/email_trigger.py tests/test_trigger_workflow_builder.py
git commit -m "feat(trigger): uncached per-run workflow builder with UID-scoped email tools

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `poll_org` rewrite — bounded batch, build-first, durable-run-then-advance, error retention

**Files:**
- Modify: `ui/backend/email_trigger.py` (`poll_org` new-mail path, `_start_triggered_run`, remove the unconditional `last_error` clear)
- Modify: `ui/backend/main.py` (`_lifespan` injects `build_trigger_workflow`, not `_get_workflow`)
- Modify: `tests/test_email_trigger.py` (update stub signatures; add batch/build-first/durable/error-retention tests)

**Interfaces:**
- Consumes: Task 3's `build_trigger_workflow(name, db, org_id, allowed_uids)` and `batch_size()`; Task 2's row-reuse; `registry`, `_executor`, `run_in_background`, `Run`, `TRIGGER_USERNAME`.
- Produces: the corrected `poll_org` — `get_workflow` argument is now the 4-arg trigger builder `(name, db, org_id, allowed_uids) -> Workflow`.

- [ ] **Step 1: Update existing stubs and write the new failing tests**

In `tests/test_email_trigger.py`, the poller tests pass a stub workflow-getter. Change every stub and helper to the 4-arg signature. Replace `_no_workflow` and `_fake_workflow_getter`:

```python
def _no_workflow(name, db, org_id, allowed_uids):  # must NOT be called
    raise AssertionError("build_trigger_workflow should not be called in this test")


def _fake_workflow_getter(calls):
    def build(name, db, org_id, allowed_uids):
        calls.append((name, org_id, set(allowed_uids)))
        return object()
    return build
```

Update `test_poll_org_new_mail_starts_one_run` to expect the batch in the call and a persisted run row, and add the new tests:

```python
def test_poll_org_new_mail_starts_one_run(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 43, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    calls = []
    poll_org(db, trigger, _fake_workflow_getter(calls))

    assert calls == [("triage", org.id, {42, 43, 45})]  # scoped to the batch
    assert len(recorder.calls) == 1
    _, args, kwargs = recorder.calls[0]
    run_id, input_text = args[0], args[2]
    assert "42, 43, 45" in input_text
    assert kwargs["username"] == "email-trigger"
    # Durable run row exists BEFORE dispatch.
    from ui.backend.db.models import Run
    assert db.get(Run, run_id) is not None
    assert trigger.last_uid == 45 and trigger.runs_today == 1 and trigger.last_run_id == run_id


def test_poll_org_bounded_batch_carries_remainder(db, monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "2")
    org, trigger = _org_with_trigger(db, last_uid=40)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [41, 42, 43, 44, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    calls = []
    poll_org(db, trigger, _fake_workflow_getter(calls))
    assert calls[0][2] == {41, 42}          # oldest 2 only
    assert trigger.last_uid == 42           # baseline advanced only past the batch
    assert trigger.runs_today == 1


def test_poll_org_build_failure_advances_nothing(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    def _boom(name, db_, org_id, allowed_uids):
        raise ValueError("No team named 'triage'")

    poll_org(db, trigger, _boom)
    assert recorder.calls == []
    assert trigger.last_uid == 41          # NOT advanced -- no message consumed
    assert trigger.runs_today == 0         # NO cap burned
    assert trigger.last_error is not None and "triage" in trigger.last_error


def test_poll_org_workflow_error_survives_empty_poll(db, monkeypatch):
    # F5: a workflow fault must not be cleared by a later successful empty poll.
    org, trigger = _org_with_trigger(db, last_uid=41)
    trigger.last_error = "Couldn't start the team 'triage' -- it may have been removed."
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 41, []))  # no new mail
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_checked_at is not None
    assert trigger.last_error is not None   # NOT cleared by an empty successful poll
```

Keep `test_poll_org_skips_while_previous_run_still_running` and `test_poll_org_recovers_when_registry_lost_the_run` from the prior fix, updating their getter arg to `_no_workflow` (4-arg).

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: the new/updated tests FAIL (batch not bounded; build-failure still advances; `last_error` cleared on empty poll; call shape mismatch).

- [ ] **Step 3: Implement**

In `ui/backend/email_trigger.py` `poll_org`: after a successful `check_mailbox`, set health WITHOUT clearing the error:

```python
    trigger.last_checked_at = _utcnow()
    # NOTE: do NOT clear last_error here -- a workflow fault must persist across
    # empty polls (F5). last_error is cleared only on a successful dispatch
    # (below) or on (re-)enable (the API).
```

Rewrite `_start_triggered_run` (it now receives ALL new UIDs and applies the batch + build-first + durable-row + advance ordering):

```python
def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow) -> None:
    """Start ONE run over a bounded batch of the detected UIDs.

    Build the workflow FIRST (a build failure must consume no message and no
    cap), then persist a durable run row and advance state in one commit, then
    dispatch. `get_workflow` is `build_trigger_workflow(name, db, org_id,
    allowed_uids) -> Workflow`.
    """
    batch = sorted(new_uids)[:batch_size()]
    input_text = _trigger_input(batch)
    try:
        workflow = get_workflow(trigger.workflow_name, db, trigger.org_id, set(batch))
    except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since enabling
        _logger.warning("email trigger: cannot build workflow %r for org %s: %s",
                        trigger.workflow_name, trigger.org_id, exc)
        trigger.last_error = (
            f"Couldn't start the team '{trigger.workflow_name}' -- it may have "
            "been changed or removed. Re-enable automatic runs from its page."
        )
        db.commit()  # NB: last_uid / runs_today deliberately NOT advanced
        return
    run = registry.create(
        trigger.workflow_name, input_text, org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )
    # Durable activity record before dispatch (worker updates its terminal
    # status; run_in_background reuses this row rather than re-inserting).
    db.add(Run(
        id=run.id, workflow=trigger.workflow_name, input=input_text,
        status="running", org_id=trigger.org_id, username=TRIGGER_USERNAME,
    ))
    trigger.last_uid = max(batch)
    trigger.runs_today += 1
    trigger.last_run_id = run.id
    trigger.last_error = None  # a run is going out: clear any prior fault
    db.commit()
    _executor.submit(
        run_in_background, run.id, workflow, input_text,
        engine=db.get_bind(), org_id=trigger.org_id, username=TRIGGER_USERNAME,
    )
```

Update `_trigger_input` to take the batch list (unchanged body — it already renders the ids):

```python
def _trigger_input(uids) -> str:
    ids = ", ".join(str(u) for u in uids)
    return (
        f"{len(uids)} new email(s) arrived in the inbox (message ids: {ids}). "
        "Read each message by id and triage it, drafting replies where appropriate."
    )
```

In `ui/backend/main.py` `_lifespan`, inject the trigger builder instead of `_get_workflow`:

```python
    poller = asyncio.create_task(
        email_trigger.poll_forever(stop_polling, email_trigger.build_trigger_workflow)
    )
```

(Remove `_get_workflow` from the poller wiring; it is still used by the HTTP routes and stays defined.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py tests/test_auth.py -q`
Expected: all pass (`test_auth.py` covers the lifespan startup; the poller wiring change must not break it).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py ui/backend/main.py tests/test_email_trigger.py
git commit -m "fix(trigger): bounded batch, build-first, durable-run-then-advance, error retention

Runs now process only the poller-detected UID batch (scoped tools); a
workflow-build failure consumes no message or cap; the runs row is durable
before dispatch; a workflow fault survives empty polls instead of flickering
back to active.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Credential-change syncs the trigger

**Files:**
- Modify: `ui/backend/org_settings.py` (`set_email` PUT, `delete_email` DELETE)
- Test: `tests/test_org_settings.py` (append)

**Interfaces:**
- Consumes: `get_email_trigger` (`ui.backend.db.email_triggers`).
- Produces: on a mailbox-identity change (host or username differs) or disconnect, an enabled trigger is disabled; a same-host+username password rotation leaves it enabled.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_org_settings.py`:

```python
from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger


def _enable_trigger(org_name="default"):
    with open_test_db() as db:
        org = get_or_create_org(db, org_name)
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)
        return org.id


def test_password_rotation_keeps_trigger_enabled(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "old"})
    org_id = _enable_trigger()
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "new"})
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).enabled is True


def test_mailbox_host_change_disables_trigger(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "p"})
    org_id = _enable_trigger()
    client.put("/api/org/email", json={"host": "imap.other.com", "username": "u@acme.com", "password": "p"})
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).enabled is False


def test_disconnect_disables_trigger(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "p"})
    org_id = _enable_trigger()
    client.delete("/api/org/email")
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_settings.py -k "trigger" -q`
Expected: FAIL — host change / disconnect don't touch the trigger yet.

- [ ] **Step 3: Implement**

In `ui/backend/org_settings.py`, add the import:

```python
from .db.email_triggers import get_email_trigger
```

In `set_email` (the PUT handler), capture the prior mailbox identity BEFORE writing, and after a successful `set_email_credentials`, disable the trigger if the identity changed:

```python
    prior = get_email_credentials(db, org.id)
    prior_identity = (prior.host, prior.username) if prior is not None else None
    # ... existing _reject_private_host + set_email_credentials (in its try) ...
    if prior_identity is not None and prior_identity != (req.host, req.username):
        trigger = get_email_trigger(db, org.id)
        if trigger is not None and trigger.enabled:
            trigger.enabled = False
            db.commit()
```

In `delete_email` (the DELETE handler), after `clear_email_credentials`, disable an enabled trigger:

```python
    trigger = get_email_trigger(db, org.id)
    if trigger is not None and trigger.enabled:
        trigger.enabled = False
        db.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_settings.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/org_settings.py tests/test_org_settings.py
git commit -m "fix(trigger): disable the trigger on mailbox change/disconnect (keep on rotation)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Opportunistic P2 fold — session rollback, server-side activity filter, reserved username

**Files:**
- Modify: `ui/backend/email_trigger.py` (`poll_once` rollback)
- Modify: `ui/backend/email_trigger_api.py` (activity: filter autonomous server-side before the limit)
- Modify: `ui/backend/db/users.py` (`create_user` rejects the reserved `email-trigger` username)
- Modify: `tests/test_email_trigger.py`, `tests/test_email_trigger_api.py`, `tests/test_admin_cli.py` (or wherever `create_user` is tested)

**Interfaces:**
- Produces: `poll_once` rolls back the shared session after a per-org failure (#7); `GET /api/org/email-trigger/activity` filters `username == TRIGGER_USERNAME` in SQL before `.limit(50)` (#10); `create_user` raises `ValueError` for the reserved sentinel name (#9 partial).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_email_trigger.py`:

```python
def test_poll_once_rolls_back_after_org_failure(db, monkeypatch):
    a = get_or_create_org(db, "org_a")
    b = get_or_create_org(db, "org_b")
    for org in (a, b):
        set_email_credentials(db, org.id, host="h", username="u", password="p")
        upsert_email_trigger(db, org.id, workflow_name="w", enabled=True, last_uid=0, uidvalidity=1)
    seen = []

    def fake_poll_org(session, trigger, get_workflow):
        seen.append(trigger.org_id)
        if trigger.org_id == a.id:
            from sqlalchemy.exc import SQLAlchemyError
            raise SQLAlchemyError("boom")  # leaves the session needing rollback

    monkeypatch.setattr(email_trigger, "poll_org", fake_poll_org)

    class _Factory:
        def __call__(self): return self
        def __enter__(self): return db
        def __exit__(self, *exc): return False

    email_trigger.poll_once(_no_workflow, session_factory=_Factory())
    assert seen == [a.id, b.id]   # org B still ran despite org A poisoning the session
```

Add to `tests/test_email_trigger_api.py`:

```python
def test_activity_filters_autonomous_server_side(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
    # 60 manual runs (newer) then 1 autonomous (older) -- the autonomous one must
    # still appear (server-side filter), not be pushed out of a 50-row window.
    from datetime import datetime, timedelta, timezone
    from ui.backend.db.models import Run
    with open_test_db() as db:
        db.add(Run(id="auto", workflow="w", input="x", status="completed", org_id=org_id,
                   username="email-trigger",
                   created_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        for i in range(60):
            db.add(Run(id=f"m{i}", workflow="w", input="x", status="completed", org_id=org_id,
                       username="alice",
                       created_at=datetime.now(timezone.utc) - timedelta(minutes=i)))
        db.commit()
    runs = client.get("/api/org/email-trigger/activity").json()["runs"]
    assert any(r["id"] == "auto" for r in runs)
    assert all(r["autonomous"] for r in runs)
```

Add a `create_user` reserved-name test (mirror the file that tests `create_user`, e.g. `tests/test_auth.py`):

```python
def test_create_user_rejects_reserved_sentinel_name():
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.users import create_user
    engine = make_engine(":memory:"); init_db(engine)
    with session_factory(engine)() as db:
        with pytest.raises(ValueError):
            create_user(db, "email-trigger", "pw")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py::test_poll_once_rolls_back_after_org_failure tests/test_email_trigger_api.py::test_activity_filters_autonomous_server_side -q`
plus the create_user test file.
Expected: all three FAIL (no rollback → org B raises PendingRollbackError; activity filters client-side so `auto` is missing; `create_user` allows the name).

- [ ] **Step 3: Implement**

`poll_once` (in `email_trigger.py`) — roll back after a per-org failure:

```python
        for trigger in list_enabled_triggers(db):
            try:
                poll_org(db, trigger, get_workflow)
            except Exception:  # noqa: BLE001 -- one org's failure must not stop the rest
                _logger.exception("email trigger: unexpected failure for org %s", trigger.org_id)
                db.rollback()  # clear a poisoned transaction so later orgs still run
```

`trigger_activity` (in `email_trigger_api.py`) — filter before the limit:

```python
    rows = (
        db.query(Run)
        .filter(Run.org_id == org.id, Run.username == TRIGGER_USERNAME)
        .order_by(Run.created_at.desc())
        .limit(50)
        .all()
    )
```

(`autonomous` is then always `True`; keep the field for the frontend contract.)

`create_user` (in `ui/backend/db/users.py`) — reject the reserved name at the top of the function:

```python
    if username == "email-trigger":
        raise ValueError("'email-trigger' is reserved for autonomous runs.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py tests/test_email_trigger_api.py tests/test_auth.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py ui/backend/email_trigger_api.py ui/backend/db/users.py tests/test_email_trigger.py tests/test_email_trigger_api.py tests/test_auth.py
git commit -m "fix(trigger): per-org session rollback, server-side activity filter, reserve sentinel name

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs + full verification

**Files:**
- Modify: `.env.example` (add `BESTTEAM_TRIGGER_BATCH_SIZE`), `docs/deployment.md` (batch note + multi-worker operator caveat), `ui/backend/CLAUDE.md` (scoped-tools + build-first behavior), `docs/STATUS.md` (done entry), and the MVP spec's out-of-scope note if it still says batch/skill redesign is unaddressed.

- [ ] **Step 1: `.env.example`** — under the trigger block added earlier, add:

```bash
#   BESTTEAM_TRIGGER_BATCH_SIZE    max messages one automatic run handles (default 20)
BESTTEAM_TRIGGER_BATCH_SIZE=
```

- [ ] **Step 2: `docs/deployment.md`** — in the "Automatic runs" subsection, append:

```markdown
Each automatic run handles at most `BESTTEAM_TRIGGER_BATCH_SIZE` messages
(default 20) and is confined to exactly those messages; a larger burst is
processed over successive polls, nothing skipped or reprocessed. **Run the
backend as a single process/worker:** the poller and its overlap protection
are in-process, so multiple ASGI workers would each poll and could double-
process mail. Leader election is future work; until then, one worker.
```

- [ ] **Step 3: `ui/backend/CLAUDE.md`** — in the autonomous-trigger section, add one line:

```markdown
An automatic run is confined to the poller-detected UID batch: it runs an
UNCACHED workflow (`email_trigger.build_trigger_workflow`) whose email tools
are UID-scoped (`make_email_tools(backend, allowed_uids=)`), so the triage
skill's `email_find` can only see that batch. State advances (baseline, cap)
only after the workflow builds and a durable `runs` row is written; a build
failure consumes nothing. Batch size: `BESTTEAM_TRIGGER_BATCH_SIZE` (default 20).
```

- [ ] **Step 4: `docs/STATUS.md`** — append to the autonomous-email-triggered-runs Done entry (or add a follow-up bullet):

```markdown
- Autonomous email-trigger correctness fixes: runs are hard-confined to the
  poller-detected UID batch (scoped tools + uncached per-run workflow), bounded
  by `BESTTEAM_TRIGGER_BATCH_SIZE` with carry-over; state advances only through
  a durable run; workflow faults persist across empty polls; mailbox
  change/disconnect disables the trigger (rotation keeps it). Also: per-org poll
  rollback, server-side autonomous activity filter, reserved sentinel username.
  Spec: `2026-07-20-email-trigger-correctness-redesign-design.md`. Remaining P2
  hardening (env validation, shutdown thread-stop, run-source enum, RunRegistry
  eviction) tracked in Known issues.
```

Add a Known-issues bullet if not already present:

```markdown
- **Autonomous trigger residuals:** invalid `BESTTEAM_TRIGGER_*` env values
  aren't validated at startup (a bad value can stop/spin the poller);
  `asyncio.to_thread` poll cycles aren't awaited on shutdown; a process killed
  between a trigger's state commit and dispatch orphans a `runs` row (overlap
  guard self-recovers on restart; no reconciliation sweep yet); `RunRegistry`
  never evicts terminal runs, so autonomous volume grows process memory.
```

- [ ] **Step 5: Full verification**

Run: `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch7.db" ./.venv/Scripts/python.exe -m pytest -q` → expect all pass; then `rm -f .superpowers/sdd/scratch7.db`.
Run: `cd ui/frontend && npm run lint && npm run build` → clean.

- [ ] **Step 6: Commit**

```bash
git add .env.example docs/deployment.md ui/backend/CLAUDE.md docs/STATUS.md
git commit -m "docs(trigger): batch size, single-worker caveat, correctness-fix status + residuals

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** §1 scoped tools → Task 1; §2 trigger-run builder → Task 3; §3 poll rewrite (batch/build-first/durable/advance) → Tasks 2+4; §4 error retention → Task 4; §5 credential sync → Task 5; out-of-scope P2 trivial fold → Task 6; docs → Task 7. The gate-zero UID-space verification is discharged in the plan preamble (already confirmed against the code).
- **Type consistency:** the trigger workflow-getter signature is 4-arg `(name, db, org_id, allowed_uids)` everywhere it appears (Task 3 builder, Task 4 poll_org + stubs + lifespan injection). `TRIGGER_USERNAME == "email-trigger"` used in Tasks 4/6. `batch_size()`/`BESTTEAM_TRIGGER_BATCH_SIZE` consistent Tasks 3/4/7.
- **Residual (documented, not a task):** the commit→submit crash window and env-value validation are explicitly Known-issues/out-of-scope, not silently dropped.
- **Push/PR** only when the user asks.
