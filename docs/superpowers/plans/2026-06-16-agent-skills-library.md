# Agent Skills Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent, admin-managed Skills library — `SkillRecord` DB table, `/api/config/skills` CRUD, runtime skill resolution in `main.py` and `crud.py`, Solution Architect auto-assignment in `builder.py`, and a Skills tab in AdvancedPage.

**Architecture:** `SkillRecord` (name + JSON config) follows the same SQLAlchemy pattern as `AgentRecord`/`TeamRecord`. A new `ui/backend/skills.py` module provides `load_skills(db) → Dict[str, SkillSpec]` used by `crud.py`, `main.py`, and `builder.py` as `extra_skills` for every `_build_workflow` / `validate_specification` / `generate_specification` call. The Solution Architect learns about available skills via `_with_skill_catalog(db, text)` (parallel to `_with_model_catalog`). The frontend only needs one new entry in AdvancedPage's `KINDS` array — all generic CRUD machinery already works.

**Tech Stack:** SQLAlchemy 2.0 (`mapped_column`), FastAPI, Pydantic v2, React JSX, `fake:` model for all tests (zero API cost).

---

### Task 1: `SkillRecord` DB model + `SkillSpec.to_raw()`

**Files:**
- Modify: `ui/backend/db/models.py`
- Modify: `ui/backend/db/__init__.py`
- Modify: `src/bestteam/core/specification.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_specification.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_db.py`, add `SkillRecord` to the import on line 13 and update `test_init_db_creates_all_tables` to expect `"skills"` in the table set, then add a round-trip test:

```python
# line 13 — change to:
from ui.backend.db import AgentRecord, SkillRecord, WorkflowRecord, init_db, make_engine, session_factory

# In test_init_db_creates_all_tables, change the assertion to:
assert tables == {
    "users", "agents", "teams", "knowledge_bases", "skills",
    "workflows", "builder_sessions", "model_catalog",
    "runs", "trace_events", "usage_records",
}

# New test at the bottom of test_db.py:
def test_skill_record_round_trip(db_session):
    record = SkillRecord(
        name="research_skill",
        config={"name": "research_skill", "instructions": "Use web_search.", "tools": ["web_search"]},
    )
    db_session.add(record)
    db_session.commit()

    fetched = db_session.query(SkillRecord).filter_by(name="research_skill").one()
    assert fetched.config["instructions"] == "Use web_search."
    assert fetched.config["tools"] == ["web_search"]
```

In `tests/test_specification.py`, add two tests at the bottom:

```python
def test_skill_spec_to_raw_omits_empty_optional_fields():
    skill = SkillSpec(name="minimal", instructions="Do the thing.")
    raw = skill.to_raw()
    assert raw == {"name": "minimal", "instructions": "Do the thing."}
    assert "description" not in raw
    assert "tools" not in raw


def test_skill_spec_to_raw_includes_all_non_empty_fields():
    skill = SkillSpec(
        name="research_skill",
        description="Research helper",
        instructions="Use web_search.",
        tools=["web_search"],
    )
    raw = skill.to_raw()
    assert raw == {
        "name": "research_skill",
        "description": "Research helper",
        "instructions": "Use web_search.",
        "tools": ["web_search"],
    }
```

- [ ] **Step 2: Run to verify they fail**

```
.\.venv\Scripts\python.exe -m pytest tests/test_db.py::test_init_db_creates_all_tables tests/test_db.py::test_skill_record_round_trip tests/test_specification.py::test_skill_spec_to_raw_omits_empty_optional_fields tests/test_specification.py::test_skill_spec_to_raw_includes_all_non_empty_fields -v
```

Expected: all 4 FAIL (`SkillRecord` not defined, "skills" not in tables, `to_raw` not defined).

- [ ] **Step 3: Add `SkillRecord` to `ui/backend/db/models.py`**

Add after `KnowledgeBaseRecord` (before `WorkflowRecord`):

```python
class SkillRecord(Base):
    """A Skill's `raw` config (the technical fields from `SkillSpec.to_raw()`)."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
```

- [ ] **Step 4: Export `SkillRecord` from `ui/backend/db/__init__.py`**

Add `SkillRecord` to both the `from .models import (...)` block and `__all__` (in alphabetical position):

```python
from .models import (
    AgentRecord,
    Base,
    BuilderSession,
    KnowledgeBaseRecord,
    ModelCatalogEntry,
    Run,
    SkillRecord,        # ← new
    TeamRecord,
    TraceEventRecord,
    UsageRecord,
    User,
    WorkflowRecord,
)

__all__ = [
    "Base",
    "make_engine",
    "init_db",
    "session_factory",
    "User",
    "AgentRecord",
    "TeamRecord",
    "KnowledgeBaseRecord",
    "SkillRecord",      # ← new
    "WorkflowRecord",
    "BuilderSession",
    "Run",
    "TraceEventRecord",
    "UsageRecord",
    "ModelCatalogEntry",
]
```

- [ ] **Step 5: Add `SkillSpec.to_raw()` to `src/bestteam/core/specification.py`**

Add the method to the `SkillSpec` class, after the field declarations:

```python
def to_raw(self) -> Dict[str, Any]:
    raw: Dict[str, Any] = {"name": self.name, "instructions": self.instructions}
    if self.description:
        raw["description"] = self.description
    if self.tools:
        raw["tools"] = list(self.tools)
    return raw
```

- [ ] **Step 6: Run tests to verify they pass**

```
.\.venv\Scripts\python.exe -m pytest tests/test_db.py::test_init_db_creates_all_tables tests/test_db.py::test_skill_record_round_trip tests/test_specification.py::test_skill_spec_to_raw_omits_empty_optional_fields tests/test_specification.py::test_skill_spec_to_raw_includes_all_non_empty_fields -v
```

Expected: all 4 PASS.

- [ ] **Step 7: Run full suite**

```
.\.venv\Scripts\python.exe -m pytest -v
```

Expected: all existing tests still PASS plus 4 new.

- [ ] **Step 8: Commit**

```
git add ui/backend/db/models.py ui/backend/db/__init__.py src/bestteam/core/specification.py tests/test_db.py tests/test_specification.py
git commit -m "feat: add SkillRecord model and SkillSpec.to_raw()"
```

---

### Task 2: `load_skills()` helper + `/api/config/skills` CRUD

**Files:**
- Create: `ui/backend/skills.py`
- Modify: `ui/backend/crud.py`
- Modify: `tests/test_crud_api.py`

- [ ] **Step 1: Write the failing tests**

Add to the bottom of `tests/test_crud_api.py`:

```python
def test_skill_crud_round_trip(client):
    config = {"instructions": "Use web_search for research.", "tools": []}

    create = client.put("/api/config/skills/research_skill", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["instructions"] == "Use web_search for research."

    listed = client.get("/api/config/skills")
    assert [item["name"] for item in listed.json()] == ["research_skill"]

    fetched = client.get("/api/config/skills/research_skill")
    assert fetched.status_code == 200
    assert fetched.json()["config"]["tools"] == []

    deleted = client.delete("/api/config/skills/research_skill")
    assert deleted.status_code == 204
    assert client.get("/api/config/skills/research_skill").status_code == 404


def test_skill_put_rejects_missing_instructions(client):
    resp = client.put("/api/config/skills/bad_skill", json={"description": "no instructions"})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to verify failure**

```
.\.venv\Scripts\python.exe -m pytest tests/test_crud_api.py::test_skill_crud_round_trip tests/test_crud_api.py::test_skill_put_rejects_missing_instructions -v
```

Expected: both FAIL with 404 (endpoint doesn't exist yet).

- [ ] **Step 3: Create `ui/backend/skills.py`**

```python
"""Shared helper for loading SkillRecords from the database as SkillSpec instances."""

from __future__ import annotations

from typing import Dict

from sqlalchemy.orm import Session

from bestteam import SkillSpec

from .db.models import SkillRecord


def load_skills(db: Session) -> Dict[str, SkillSpec]:
    """Return all SkillRecords as a name→SkillSpec mapping for use as `extra_skills`."""
    return {
        r.name: SkillSpec.model_validate({**r.config, "name": r.name})
        for r in db.query(SkillRecord).all()
    }
```

- [ ] **Step 4: Register the skills router in `ui/backend/crud.py`**

Change the `from bestteam import ...` line (line 24) to add `SkillSpec`:

```python
from bestteam import AgentSpec, KnowledgeBaseSpec, SkillSpec, TeamSpec
```

Change the `from .db.models import ...` line (line 31) to add `SkillRecord`:

```python
from .db.models import AgentRecord, KnowledgeBaseRecord, SkillRecord, TeamRecord, WorkflowRecord
```

Add one line after line 86 (after the `knowledge_bases` include_router call):

```python
router.include_router(_make_component_router("skills", SkillRecord, SkillSpec))
```

- [ ] **Step 5: Run tests to verify they pass**

```
.\.venv\Scripts\python.exe -m pytest tests/test_crud_api.py::test_skill_crud_round_trip tests/test_crud_api.py::test_skill_put_rejects_missing_instructions -v
```

Expected: both PASS.

- [ ] **Step 6: Run full suite**

```
.\.venv\Scripts\python.exe -m pytest -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```
git add ui/backend/skills.py ui/backend/crud.py tests/test_crud_api.py
git commit -m "feat: add /api/config/skills CRUD and load_skills() helper"
```

---

### Task 3: Runtime skill resolution in `crud.py` and `main.py`

`upsert_workflow_config` currently validates workflows via `_build_workflow(raw, extra_tools={})` with no `extra_skills`, so saving a workflow that references a skill will always fail even if that skill exists in the DB. `_get_workflow` in `main.py` has the same gap at run time.

**Files:**
- Modify: `ui/backend/crud.py`
- Modify: `ui/backend/main.py`
- Modify: `tests/test_crud_api.py`

- [ ] **Step 1: Write the failing test**

Add to the bottom of `tests/test_crud_api.py`:

```python
def test_workflow_put_accepts_skill_reference_when_skill_exists(client):
    client.put("/api/config/skills/research_skill", json={
        "instructions": "Research topics thoroughly.",
        "tools": [],
    })
    config = {
        "knowledge_bases": [],
        "agents": [{
            "name": "agent1",
            "role": "Researcher",
            "goal": "Research topics",
            "model": "fake:hello",
            "tools": [],
            "skills": ["research_skill"],
        }],
        "teams": [{"name": "team1", "agents": ["agent1"], "mode": "sequential"}],
        "workflow": {"steps": ["team1"]},
    }
    resp = client.put("/api/config/workflows/my_workflow", json=config)
    assert resp.status_code == 200


def test_workflow_put_rejects_unknown_skill_reference(client):
    config = {
        "knowledge_bases": [],
        "agents": [{
            "name": "agent1",
            "role": "Researcher",
            "goal": "Research topics",
            "model": "fake:hello",
            "tools": [],
            "skills": ["nonexistent_skill"],
        }],
        "teams": [{"name": "team1", "agents": ["agent1"], "mode": "sequential"}],
        "workflow": {"steps": ["team1"]},
    }
    resp = client.put("/api/config/workflows/my_workflow", json=config)
    assert resp.status_code == 400
    assert "Unknown skill" in resp.json()["detail"]
```

- [ ] **Step 2: Run to see first test fail**

```
.\.venv\Scripts\python.exe -m pytest tests/test_crud_api.py::test_workflow_put_accepts_skill_reference_when_skill_exists tests/test_crud_api.py::test_workflow_put_rejects_unknown_skill_reference -v
```

Expected: `test_workflow_put_accepts_skill_reference_when_skill_exists` FAILS (the existing skill can't be found because `extra_skills={}` is hard-coded). `test_workflow_put_rejects_unknown_skill_reference` PASSES already — `_build_workflow` raises `ConfigurationError("Unknown skill...")` which is caught as `BestTeamError` and returned as 400.

- [ ] **Step 3: Update `upsert_workflow_config` in `ui/backend/crud.py`**

Add import after line 26:

```python
from .skills import load_skills
```

Change the `_build_workflow` call in `upsert_workflow_config` (around line 110):

```python
# Before:
_build_workflow(raw, source=_WORKFLOWS_DIR / f"{item_name}.yaml", extra_tools={})
# After:
_build_workflow(raw, source=_WORKFLOWS_DIR / f"{item_name}.yaml", extra_tools={}, extra_skills=load_skills(db))
```

- [ ] **Step 4: Update `_get_workflow` in `ui/backend/main.py`**

Add import after the existing `.` imports:

```python
from .skills import load_skills
```

In `_get_workflow`, extend both branches to compute `skill_lookup` alongside the record lookup, then pass it to `_build_workflow`. Replace lines 77–92:

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

The YAML-file fallback path (lines 94–105) is unchanged — demo YAML workflows do not reference DB-library skills.

- [ ] **Step 5: Run tests to verify both pass**

```
.\.venv\Scripts\python.exe -m pytest tests/test_crud_api.py::test_workflow_put_accepts_skill_reference_when_skill_exists tests/test_crud_api.py::test_workflow_put_rejects_unknown_skill_reference -v
```

Expected: both PASS.

- [ ] **Step 6: Run full suite**

```
.\.venv\Scripts\python.exe -m pytest -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```
git add ui/backend/crud.py ui/backend/main.py tests/test_crud_api.py
git commit -m "feat: wire extra_skills into workflow validation and runtime resolution"
```

---

### Task 4: Solution Architect integration

Add `_with_skill_catalog` to `builder.py`, update `_ARCHITECT_SYSTEM_PROMPT` with a skills paragraph, and pass `extra_skills=load_skills(db)` through every `validate_specification` / `generate_specification` call site in `builder.py`.

**Files:**
- Modify: `src/bestteam/core/specification.py`
- Modify: `ui/backend/builder.py`
- Modify: `tests/test_builder_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_builder_api.py` (after the existing imports and fixtures):

```python
from ui.backend.builder import _with_skill_catalog
from ui.backend.db import SkillRecord


@pytest.fixture
def db_session():
    from ui.backend.db import init_db, make_engine, session_factory
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_with_skill_catalog_unchanged_when_no_skills(db_session):
    text = "Some requirements."
    assert _with_skill_catalog(db_session, text) == text


def test_with_skill_catalog_appends_skill_list(db_session):
    db_session.add(SkillRecord(
        name="research_skill",
        config={
            "name": "research_skill",
            "description": "Deep research assistant",
            "instructions": "Use web_search to research topics.",
            "tools": ["web_search"],
        },
    ))
    db_session.commit()

    result = _with_skill_catalog(db_session, "Requirements here.")
    assert "research_skill" in result
    assert "Deep research assistant" in result
    assert "web_search" in result
```

- [ ] **Step 2: Run to verify failure**

```
.\.venv\Scripts\python.exe -m pytest tests/test_builder_api.py::test_with_skill_catalog_unchanged_when_no_skills tests/test_builder_api.py::test_with_skill_catalog_appends_skill_list -v
```

Expected: both FAIL with `ImportError` (`_with_skill_catalog` does not exist yet).

- [ ] **Step 3: Update `_ARCHITECT_SYSTEM_PROMPT` in `src/bestteam/core/specification.py`**

Insert a new paragraph after the "Choose tools..." paragraph and before the "Group agents into teams..." paragraph. The full updated prompt:

```python
_ARCHITECT_SYSTEM_PROMPT = """You are the Solution Architect for bestteam, a multi-agent \
team-building platform for non-technical customers.

Given a customer's confirmed Requirements (their goals, pain points, success \
criteria, and constraints), design a Specification: a small team of AI \
"employees" (agents), organized into one or more teams, that can address \
those requirements.

For each agent, give it a clear name, role, goal, and (optionally) a \
backstory, plus a friendly display_name and a one-sentence \
friendly_description that a non-technical person would understand. Choose \
tools from the ones available to the workflow, and pick a model spec string \
appropriate to the task.

If skills are listed in the input, assign them to agents via each agent's \
`skills` field (a list of skill names from the provided list). A Skill is a \
reusable instruction document for a repeatable task -- prefer assigning one \
over re-describing the same task in `backstory`. Only use skill names from \
the provided list; never invent names.

Group agents into teams. Use 'sequential' mode when agents hand work to each \
other in order, 'parallel' mode when they work independently on the same \
input, and 'hierarchical' mode (with a 'manager' agent) when one agent \
should coordinate and delegate to the others -- the metaphor customers will \
see is "hiring a team with a manager". Give each team a friendly \
display_name and friendly_description describing how the team works \
together in plain language.

Finally, list the teams in the order they should run as the workflow's steps.

If you receive feedback that a previous design was invalid, fix exactly the \
issue described and resubmit a complete, corrected Specification."""
```

- [ ] **Step 4: Add `_with_skill_catalog` to `ui/backend/builder.py`**

Add import (after the existing `.db.*` imports, around line 27):

```python
from .skills import load_skills
```

Add `_with_skill_catalog` immediately after `_with_model_catalog` (around line 99):

```python
def _with_skill_catalog(db: Session, text: str) -> str:
    """Append available skills (if any) so the Solution Architect can assign
    them to agents by name, parallel to `_with_model_catalog`."""
    skills = load_skills(db)
    if not skills:
        return text
    lines = ["", "", "Available skills (from the platform's skill library):"]
    for spec in skills.values():
        tools_note = f" (tools: {', '.join(spec.tools)})" if spec.tools else ""
        desc = spec.description if spec.description else (
            spec.instructions[:80] + "..." if len(spec.instructions) > 80 else spec.instructions
        )
        lines.append(f"- {spec.name}: {desc}{tools_note}")
    return text + "\n".join(lines)
```

- [ ] **Step 5: Update `_validate_spec_payload` to forward `extra_skills`**

Change the signature and body of `_validate_spec_payload` (around line 101):

```python
def _validate_spec_payload(
    payload: Dict[str, Any], source: Path, extra_skills: Optional[Dict[str, Any]] = None
) -> Specification:
    try:
        spec = Specification.model_validate(payload)
        validate_specification(spec, source=source, extra_skills=extra_skills or {})
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return spec
```

- [ ] **Step 6: Wire `extra_skills` through all call sites in `builder.py`**

`submit_specification` — `req.specification` branch (around line 185):
```python
spec = _validate_spec_payload(req.specification, source, extra_skills=load_skills(db))
```

`submit_specification` — `req.model` branch (around lines 190–192):
```python
requirements_text = _with_model_catalog(db, requirements_text)
requirements_text = _with_skill_catalog(db, requirements_text)
chat_model = _call_model(_resolve_model, req.model)
spec = _call_model(generate_specification, chat_model, requirements_text, source=source, extra_skills=load_skills(db))
```

`submit_solution_feedback` — `req.specification` branch (around line 211):
```python
spec = _validate_spec_payload(req.specification, source, extra_skills=load_skills(db))
```

`submit_solution_feedback` — `req.model` branch (around lines 222–224):
```python
requirements_text = _with_model_catalog(db, requirements_text)
requirements_text = _with_skill_catalog(db, requirements_text)
chat_model = _call_model(_resolve_model, req.model)
spec = _call_model(generate_specification, chat_model, requirements_text, source=source, extra_skills=load_skills(db))
```

`create_test_run` (around line 244):
```python
workflow = validate_specification(spec, source=source, extra_skills=load_skills(db))
```

`deploy_session` (around line 267):
```python
validate_specification(spec, source=source, extra_skills=load_skills(db))
```

- [ ] **Step 7: Run new tests to verify they pass**

```
.\.venv\Scripts\python.exe -m pytest tests/test_builder_api.py::test_with_skill_catalog_unchanged_when_no_skills tests/test_builder_api.py::test_with_skill_catalog_appends_skill_list -v
```

Expected: both PASS.

- [ ] **Step 8: Run full suite**

```
.\.venv\Scripts\python.exe -m pytest -v
```

Expected: all PASS.

- [ ] **Step 9: Commit**

```
git add src/bestteam/core/specification.py ui/backend/builder.py tests/test_builder_api.py
git commit -m "feat: Solution Architect skill awareness and extra_skills through builder"
```

---

### Task 5: Frontend — Skills tab in AdvancedPage

The generic `KINDS` array in `AdvancedPage.jsx` drives all CRUD tabs. `api.js` already has `listConfig(kind)` / `putConfigItem(kind, name, payload)` / `deleteConfigItem(kind, name)` which work for any `/api/config/<kind>` endpoint. One line is all that's needed.

**Files:**
- Modify: `ui/frontend/src/pages/AdvancedPage.jsx`

- [ ] **Step 1: Add `skills` entry to `KINDS` in `AdvancedPage.jsx`**

The current array (lines 6–12):

```js
const KINDS = [
  { key: 'agents', label: 'Agents', idField: 'name', editableField: 'config' },
  { key: 'teams', label: 'Teams', idField: 'name', editableField: 'config' },
  { key: 'knowledge_bases', label: 'Knowledge bases', idField: 'name', editableField: 'config' },
  { key: 'workflows', label: 'Workflows', idField: 'name', editableField: 'config' },
  { key: 'model-catalog', label: 'Model catalog', idField: 'spec', editableField: null },
]
```

Replace with:

```js
const KINDS = [
  { key: 'agents', label: 'Agents', idField: 'name', editableField: 'config' },
  { key: 'teams', label: 'Teams', idField: 'name', editableField: 'config' },
  { key: 'knowledge_bases', label: 'Knowledge bases', idField: 'name', editableField: 'config' },
  { key: 'workflows', label: 'Workflows', idField: 'name', editableField: 'config' },
  { key: 'skills', label: 'Skills', idField: 'name', editableField: 'config' },
  { key: 'model-catalog', label: 'Model catalog', idField: 'spec', editableField: null },
]
```

- [ ] **Step 2: Verify in the browser**

Start both servers in separate terminals:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1

# Terminal 2
cd ui\frontend
npm run dev
```

Open `http://localhost:5173/advanced`. Confirm a "Skills" tab appears between Workflows and Model catalog. Click it — list should be empty. Enter name `test_skill`, click "New", paste `{"instructions": "Test.", "tools": []}` in the editor, click Save — should succeed and appear in the list.

- [ ] **Step 3: Commit**

```
git add ui/frontend/src/pages/AdvancedPage.jsx
git commit -m "feat: add Skills tab to AdvancedPage"
```

---

### Task 6: Docs + STATUS.md

**Files:**
- Modify: `ui/backend/db/CLAUDE.md`
- Modify: `ui/backend/CLAUDE.md`
- Modify: `docs/STATUS.md`

- [ ] **Step 1: Update `ui/backend/db/CLAUDE.md`**

In the schema list, change the first bullet from:

```
- `agents` / `teams` / `knowledge_bases` / `workflows` — each row's `config`
```

to:

```
- `agents` / `teams` / `knowledge_bases` / `skills` / `workflows` — each row's `config`
```

Add a new `skills` bullet after `knowledge_bases`:

```
- `skills` — platform-wide Skill library; one row per Skill. `config` is a
  `SkillSpec.to_raw()` dict (`name` / `instructions` / `description` / `tools`).
  Consumed by `load_skills(db)` (`ui/backend/skills.py`) which is passed as
  `extra_skills` to every `_build_workflow` / `validate_specification` /
  `generate_specification` call in `main.py`, `crud.py`, and `builder.py`.
```

- [ ] **Step 2: Update `ui/backend/CLAUDE.md`**

In the "Model catalog" paragraph under "Auth, model catalog, and usage metering (Phase 3)", add after the existing `_with_model_catalog` sentence:

```
- **`ui/backend/skills.py`** — `load_skills(db: Session) → Dict[str, SkillSpec]`
  returns all `SkillRecord` rows as a name→SkillSpec mapping; used as `extra_skills`
  in `main.py` (`_get_workflow`), `crud.py` (`upsert_workflow_config`), and
  `builder.py` (all `validate_specification`/`generate_specification` calls).
  Returns an empty dict when the `skills` table is empty — fully backward compatible.
- **`_with_skill_catalog(db, text)`** (`builder.py`) — appends the available Skill
  names, descriptions, and required tools to the requirements text before
  `generate_specification()`, parallel to `_with_model_catalog`. Returns `text`
  unchanged when no skills are registered.
```

- [ ] **Step 3: Update `docs/STATUS.md`**

Add to the **Done** section:

```
- Agent Skills Library (sub-project 2): `SkillRecord` table, `/api/config/skills`
  CRUD, runtime skill resolution in `main.py`/`crud.py`, Solution Architect
  auto-assignment via `_with_skill_catalog`, Skills tab in AdvancedPage.
```

Remove the sub-project 2 bullet from **Next steps / roadmap**.

- [ ] **Step 4: Commit**

```
git add ui/backend/db/CLAUDE.md ui/backend/CLAUDE.md docs/STATUS.md
git commit -m "docs: update docs for Agent Skills Library (sub-project 2)"
```
