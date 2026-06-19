# Wire standalone knowledge bases into workflow tool resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workflow's `tools:` list reference a standalone knowledge
base (created via `/api/config/knowledge_bases`, manually or via upload) by
name, without having to also duplicate it into the workflow's own inline
`knowledge_bases:` list.

**Architecture:** A new helper, `load_knowledge_base_tools(db, raw,
source)`, scans a workflow's raw config for which tool names its agents
actually reference, builds only the standalone `KnowledgeBaseRecord`s that
match (reusing the loader's existing `_build_knowledge_base`/
`make_knowledge_base_tool`), and returns them as an `extra_tools` mapping.
Two call sites that currently hardcode `extra_tools={}` pass this instead.

**Tech Stack:** No new dependencies — reuses existing `bestteam.core.loader`
internals and SQLAlchemy session already in scope at both call sites.

## Global Constraints

- In scope: `ui/backend/main.py::_get_workflow` and `ui/backend/crud.py`'s
  workflow `PUT` route. Out of scope: `ui/backend/builder.py` (Team Builder
  wizard) — do not touch it in this plan.
- Only build standalone KBs that a workflow's agents actually reference by
  name in `tools:` — never query/build every `KnowledgeBaseRecord` in the
  database on every workflow load.
- A referenced standalone KB that fails to build raises immediately
  (`ConfigurationError`), exactly like an inline KB does today — no
  per-KB error swallowing.
- A workflow's own inline `knowledge_bases:` entry wins over a standalone
  KB of the same name (already true for free, from the existing dict-merge
  order in `core/loader.py:68-72` — no new code enforces this, a test
  proves it).
- Resolve both `local_folder` and `vector` standalone KB types — don't
  special-case by type.

---

### Task 1: `load_knowledge_base_tools` helper

**Files:**
- Create: `ui/backend/knowledge_bases.py`
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `bestteam.core.loader._build_knowledge_base(spec: Dict[str, Any], source: Path) -> KnowledgeBase` and `bestteam.core.knowledge_base.make_knowledge_base_tool(kb: KnowledgeBase) -> Callable[[str], str]` (both already exist, `src/bestteam/core/loader.py:94` and `src/bestteam/core/knowledge_base.py:173`); `ui.backend.db.models.KnowledgeBaseRecord` (existing).
- Produces: `load_knowledge_base_tools(db: Session, raw: Dict[str, Any], source: Path) -> Dict[str, Any]` — a name→tool mapping, used as `extra_tools` by Tasks 2 and 3.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_crud_api.py` (this file already has a `client` fixture
with `_KB_UPLOADS_DIR` monkeypatched to `tmp_path`, and imports
`backend_crud` — reuse both):

```python
def test_load_knowledge_base_tools_builds_only_referenced_kbs(client, tmp_path):
    from sqlalchemy.orm import Session

    from ui.backend.knowledge_bases import load_knowledge_base_tools

    docs_dir = tmp_path / "policy_docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")

    client.put(
        "/api/config/knowledge_bases/policy_kb",
        json={"path": str(docs_dir), "type": "local_folder"},
    )
    client.put(
        "/api/config/knowledge_bases/unused_kb",
        json={"path": "./does/not/exist", "type": "local_folder"},
    )

    # Use the same DB the test client's overridden get_db uses.
    from ui.backend.db_session import get_db as real_get_db

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db: Session = next(db_gen)
    try:
        raw = {"agents": [{"name": "a", "tools": ["policy_kb", "calculator"]}]}
        tools = load_knowledge_base_tools(db, raw, tmp_path / "wf.yaml")
    finally:
        db_gen.close()

    assert set(tools) == {"policy_kb"}
    assert "Refunds are processed" in tools["policy_kb"]("refund timing")
```

(If constructing a DB session this way is awkward, check how other tests
in this file already get a live `Session` from the `client` fixture's
overridden `get_db` — match that pattern exactly rather than inventing a
new one. The key behavior under test is: only `policy_kb` is built, despite
`unused_kb` existing in the database, and `policy_kb`'s tool is real and
queryable.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_load_knowledge_base_tools_builds_only_referenced_kbs -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.backend.knowledge_bases'`

- [ ] **Step 3: Implement the helper**

Create `ui/backend/knowledge_bases.py`:

```python
"""Build extra_tools for workflow loading from standalone KnowledgeBaseRecords
(created via /api/config/knowledge_bases, manually or via file upload)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from sqlalchemy.orm import Session

from bestteam.core.knowledge_base import make_knowledge_base_tool
from bestteam.core.loader import _build_knowledge_base

from .db.models import KnowledgeBaseRecord


def load_knowledge_base_tools(db: Session, raw: Dict[str, Any], source: Path) -> Dict[str, Any]:
    """Return a name -> tool mapping for only the standalone knowledge bases
    `raw`'s agents actually reference by name in their `tools:` lists.

    Building a knowledge base means re-reading and re-chunking every file
    (and, for type: vector, calling an embedding model) -- this only pays
    that cost for knowledge bases the workflow being loaded actually uses,
    not every standalone knowledge base in the database.
    """
    referenced = {
        tool_name
        for agent in raw.get("agents", [])
        for tool_name in agent.get("tools", [])
    }
    if not referenced:
        return {}

    records = db.query(KnowledgeBaseRecord).filter(KnowledgeBaseRecord.name.in_(referenced)).all()
    tools: Dict[str, Any] = {}
    for record in records:
        kb = _build_knowledge_base(record.config, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_load_knowledge_base_tools_builds_only_referenced_kbs -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/knowledge_bases.py tests/test_crud_api.py
git commit -m "feat: add load_knowledge_base_tools helper for standalone KB tool resolution"
```

---

### Task 2: Wire into `crud.py`'s workflow PUT validation

**Files:**
- Modify: `ui/backend/crud.py:206`
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `load_knowledge_base_tools` (Task 1).
- Produces: no new public interface — `PUT /api/config/workflows/{name}` now accepts a workflow referencing a standalone KB by name where it previously raised "Unknown tool".

- [ ] **Step 1: Write the failing test**

```python
def test_workflow_put_resolves_standalone_knowledge_base_by_name(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")
    client.put("/api/config/knowledge_bases/policy_kb", json={"path": str(docs_dir), "type": "local_folder"})

    workflow_config = {
        "agents": [
            {
                "name": "support_agent",
                "role": "Support",
                "goal": "Answer policy questions",
                "model": "fake:hi",
                "tools": ["policy_kb"],
            }
        ],
        "teams": [{"name": "team", "agents": ["support_agent"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/policy_wf", json=workflow_config)

    assert resp.status_code == 200
```

(Before Task 2, this fails with 400 "Unknown tool 'policy_kb'".)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_workflow_put_resolves_standalone_knowledge_base_by_name -v`
Expected: FAIL with 400, body containing "Unknown tool 'policy_kb'"

- [ ] **Step 3: Wire in the helper**

In `ui/backend/crud.py`, add the import near the other local imports
(after `from .skills import load_skills`):

```python
from .knowledge_bases import load_knowledge_base_tools
```

Change line 206 from:

```python
        _build_workflow(raw, source=_WORKFLOWS_DIR / f"{item_name}.yaml", extra_tools={}, extra_skills=load_skills(db))
```

to:

```python
        source = _WORKFLOWS_DIR / f"{item_name}.yaml"
        kb_tools = load_knowledge_base_tools(db, raw, source)
        _build_workflow(raw, source=source, extra_tools=kb_tools, extra_skills=load_skills(db))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_workflow_put_resolves_standalone_knowledge_base_by_name -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/crud.py tests/test_crud_api.py
git commit -m "feat: resolve standalone knowledge bases by name in workflow PUT validation"
```

---

### Task 3: Wire into `main.py`'s `_get_workflow`

**Files:**
- Modify: `ui/backend/main.py:83-120`
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `load_knowledge_base_tools` (Task 1).
- Produces: no new public interface — `POST /api/runs` (and
  `GET /api/workflows/{name}/graph`) now resolve standalone KBs referenced
  by a DB-backed workflow's `tools:` lists.

**Why both branches:** `_get_workflow` has a `db is not None` branch (the
request-scoped session, stays open) and a `db is None` branch (opens its
own short-lived `SessionLocal()` session that closes before
`_build_workflow` runs). `skill_lookup` is already computed inside both
branches, before the session in the `else` branch closes — `kb_tools` must
follow the exact same pattern, computed while the session is still live.

- [ ] **Step 1: Write the failing test**

```python
def test_run_resolves_standalone_knowledge_base_by_name(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")
    client.put("/api/config/knowledge_bases/policy_kb", json={"path": str(docs_dir), "type": "local_folder"})

    workflow_config = {
        "agents": [
            {
                "name": "support_agent",
                "role": "Support",
                "goal": "Answer policy questions",
                "model": "fake:hi",
                "tools": ["policy_kb"],
            }
        ],
        "teams": [{"name": "team", "agents": ["support_agent"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    put_resp = client.put("/api/config/workflows/policy_wf", json=workflow_config)
    assert put_resp.status_code == 200

    run_resp = client.post("/api/runs", json={"workflow": "policy_wf", "input": "How long do refunds take?"})
    assert run_resp.status_code == 200
```

(Before Task 2 this would already fail at the PUT step; Task 2 makes PUT
pass but `/api/runs` calls `_get_workflow`, a separate code path that still
hardcodes `extra_tools={}` until this task — without this task, the PUT
above succeeds since Task 2 is already done, but re-running `_get_workflow`
fresh, e.g. after restarting the process / cache eviction, would still hit
"Unknown tool". To make this test meaningfully fail before Task 3's fix,
clear `backend_main._workflow_cache` between the PUT and the run, since the
`client` fixture in this file already does this on test setup but the PUT
itself doesn't repopulate-then-invalidate within the same test — check
current cache behavior with a quick manual run before assuming; if the
cache already makes this test pass without Task 3's fix due to reusing a
cached compiled workflow, add `backend_main._workflow_cache.clear()` right
before the `run_resp = ...` line to force `_get_workflow` to rebuild.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_run_resolves_standalone_knowledge_base_by_name -v`
Expected: FAIL (400 "Unknown tool 'policy_kb'", possibly needing the cache-clear noted above to actually exercise `_get_workflow`'s rebuild path)

- [ ] **Step 3: Wire in the helper**

In `ui/backend/main.py`, add the import near `from .skills import load_skills`:

```python
from .knowledge_bases import load_knowledge_base_tools
```

Change the body of `_get_workflow` (currently lines 98-120) from:

```python
    if db is not None:
        record = db.query(WorkflowRecord).filter_by(name=name).one_or_none()
        skill_lookup = load_skills(db) if record is not None else {}
    else:
        with SessionLocal() as session:
            record = session.query(WorkflowRecord).filter_by(name=name).one_or_none()
            skill_lookup = load_skills(session) if record is not None else {}

    if record is not None:
        cache_key: Any = ("db", record.updated_at)
        cached = _workflow_cache.get(name)
        if cached is None or cached[1] != cache_key:
            try:
                workflow = _build_workflow(
                    record.config,
                    source=WORKFLOWS_DIR / f"{name}.yaml",
                    extra_tools={},
                    extra_skills=skill_lookup,
                )
            except (KeyError, TypeError, BestTeamError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _workflow_cache[name] = (workflow, cache_key)
        return _workflow_cache[name][0]
```

to:

```python
    source = WORKFLOWS_DIR / f"{name}.yaml"
    if db is not None:
        record = db.query(WorkflowRecord).filter_by(name=name).one_or_none()
        skill_lookup = load_skills(db) if record is not None else {}
        kb_tools = load_knowledge_base_tools(db, record.config, source) if record is not None else {}
    else:
        with SessionLocal() as session:
            record = session.query(WorkflowRecord).filter_by(name=name).one_or_none()
            skill_lookup = load_skills(session) if record is not None else {}
            kb_tools = load_knowledge_base_tools(session, record.config, source) if record is not None else {}

    if record is not None:
        cache_key: Any = ("db", record.updated_at)
        cached = _workflow_cache.get(name)
        if cached is None or cached[1] != cache_key:
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

(The `path = WORKFLOWS_DIR / f"{name}.yaml"` line further down, for the
file-based fallback, stays untouched — only the DB-backed branch changes;
introducing the `source` variable above must not shadow or conflict with
that later `path` variable, which it doesn't since they're different
names.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_run_resolves_standalone_knowledge_base_by_name -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass (210+ from before, plus the new ones)

- [ ] **Step 6: Commit**

```bash
git add ui/backend/main.py tests/test_crud_api.py
git commit -m "feat: resolve standalone knowledge bases by name when loading a workflow to run"
```

---

### Task 4: Resolution priority and failure-mode tests

**Files:**
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3. No new production code in this
  task — it only adds tests proving two behaviors that already fall out of
  the existing implementation, per the design's Global Constraints.

- [ ] **Step 1: Write and pass the inline-wins-over-standalone test**

```python
def test_inline_knowledge_base_wins_over_standalone_of_same_name(client, tmp_path):
    standalone_dir = tmp_path / "standalone_docs"
    standalone_dir.mkdir()
    (standalone_dir / "doc.txt").write_text("STANDALONE: refunds take 5 days.")
    client.put("/api/config/knowledge_bases/shared_name", json={"path": str(standalone_dir), "type": "local_folder"})

    inline_dir = tmp_path / "inline_docs"
    inline_dir.mkdir()
    (inline_dir / "doc.txt").write_text("INLINE: refunds take 10 days.")

    workflow_config = {
        "knowledge_bases": [{"name": "shared_name", "path": str(inline_dir), "type": "local_folder"}],
        "agents": [
            {
                "name": "a",
                "role": "Support",
                "goal": "Answer",
                "model": "fake:hi",
                "tools": ["shared_name"],
            }
        ],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/priority_wf", json=workflow_config)
    assert resp.status_code == 200

    from ui.backend.main import _get_workflow
    from ui.backend.db_session import get_db as real_get_db

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        workflow = _get_workflow("priority_wf", db)
    finally:
        db_gen.close()

    team = workflow.steps[0]
    agent = team.agents[0]
    tool = next(t for t in agent.tools if t.__name__ == "shared_name")
    assert "INLINE" in tool("refund timing")
```

(Match this `_get_workflow`/db-session-retrieval pattern to whatever Task 1
or Task 3's tests already settled on — keep it consistent across the file
rather than inventing a third way to grab a live session.)

- [ ] **Step 2: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_inline_knowledge_base_wins_over_standalone_of_same_name -v`
Expected: PASS (no production code change needed — this proves existing
dict-merge order behavior)

- [ ] **Step 3: Write and pass the contained-blast-radius test**

```python
def test_broken_standalone_kb_only_breaks_workflows_that_reference_it(client):
    client.put("/api/config/knowledge_bases/broken_kb", json={"path": "/no/such/path", "type": "local_folder"})

    broken_workflow = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi", "tools": ["broken_kb"]}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/broken_wf", json=broken_workflow)
    assert resp.status_code == 400
    assert "broken_kb" in resp.json()["detail"]

    unrelated_workflow = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi"}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp2 = client.put("/api/config/workflows/unrelated_wf", json=unrelated_workflow)
    assert resp2.status_code == 200
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k test_broken_standalone_kb_only_breaks_workflows_that_reference_it -v`
Expected: PASS

- [ ] **Step 5: Run the full suite one final time**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add tests/test_crud_api.py
git commit -m "test: verify standalone-KB resolution priority and contained failure blast radius"
```

---

## Verification (end-to-end, after all 4 tasks)

1. `./.venv/Scripts/python.exe -m pytest -q` — full suite passes, including all new tests from Tasks 1-4.
2. Manual check: in the running dev stack, create a standalone KB via the Advanced page's "Upload files" mode, then create a workflow (via `/api/config/workflows` PUT, e.g. through the Advanced page's "Workflows" tab raw JSON) referencing that KB by name in an agent's `tools:` list — confirm it saves without "Unknown tool" and a run against it streams a real response.
3. Confirm the existing `test_uploaded_kb_is_queryable_by_a_workflow` test (which embeds the KB inline as a workaround for this exact gap) still passes unchanged — this plan doesn't remove that fallback, it adds a more direct path alongside it.
