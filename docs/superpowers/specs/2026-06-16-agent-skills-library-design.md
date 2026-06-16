# Agent Skills Library (sub-project 2) — design

## Context

Sub-project 1 (`docs/superpowers/specs/2026-06-15-agent-skills-design.md`) added
the SDK primitive: `SkillSpec`, `AgentSpec.skills: List[str]`, and
`_build_workflow(..., extra_skills=Dict[str, SkillSpec])`. Skills are resolved at
build time — a skill's `instructions` are appended to the agent's `backstory` and
its `tools` are merged into the agent's tool list.

Sub-project 2 builds the **persistent library** on top of that primitive: a DB
table, a CRUD API so admins can author skills, Solution Architect awareness so the
wizard auto-assigns skills, and a Skills section in the existing AdvancedPage.

Philosophy: 把复杂留给自己，把简单留给客户 — the admin writes Skills once
(complexity); the Solution Architect picks appropriate ones automatically when
designing a team (simplicity for the customer). The "live resolution" semantics
from sub-project 1 mean updating a Skill propagates to every workflow that
references it, without touching individual agent configs.

## Scope

This spec covers sub-project 2 only. Sub-project 1's SDK primitives
(`SkillSpec`, `AgentSpec.skills`, `extra_skills` parameter) are already
implemented and are not re-specified here.

## Design

### 1. Persistence — `SkillRecord` + `SkillSpec.to_raw()`

**`ui/backend/db/models.py`** — new `SkillRecord`, same shape as
`AgentRecord`/`TeamRecord`/`KnowledgeBaseRecord`:

```python
class SkillRecord(Base):
    __tablename__ = "skills"
    name = Column(String, primary_key=True)
    config = Column(JSON, nullable=False)
```

`init_db()` (`Base.metadata.create_all`) creates the table automatically — no
migration script needed.

**`src/bestteam/core/specification.py`** — add `SkillSpec.to_raw()`:

```python
def to_raw(self) -> Dict[str, Any]:
    raw: Dict[str, Any] = {"name": self.name, "instructions": self.instructions}
    if self.description:
        raw["description"] = self.description
    if self.tools:
        raw["tools"] = list(self.tools)
    return raw
```

Required fields always output; empty optional fields omitted — consistent with
`AgentSpec.to_raw()`.

**`src/bestteam/__init__.py`** — export `SkillSpec` so `crud.py` can
`from bestteam import ..., SkillSpec`.

### 2. CRUD API — `/api/config/skills`

**`ui/backend/crud.py`** — three lines:

```python
from bestteam import AgentSpec, KnowledgeBaseSpec, SkillSpec, TeamSpec
from .db.models import AgentRecord, KnowledgeBaseRecord, SkillRecord, TeamRecord, WorkflowRecord

router.include_router(_make_component_router("skills", SkillRecord, SkillSpec))
```

`_make_component_router` is the existing generic factory (lines 40–81 of `crud.py`).
It provides GET list, GET single, PUT upsert (with `SkillSpec` field validation),
DELETE — no bespoke endpoint code needed.

### 3. Shared helper — `load_skills(db)`

New file **`ui/backend/skills.py`** (used by `main.py`, `crud.py`, `builder.py`):

```python
from typing import Dict
from sqlalchemy.orm import Session
from bestteam import SkillSpec
from .db.models import SkillRecord

def load_skills(db: Session) -> Dict[str, SkillSpec]:
    return {r.name: SkillSpec.model_validate({**r.config, "name": r.name})
            for r in db.query(SkillRecord).all()}
```

Returns an empty dict when the skills table is empty — fully backward compatible
(existing workflows with no `skills:` references behave exactly as before).

### 4. Runtime resolution

Two `_build_workflow` call sites need `extra_skills`:

**`ui/backend/main.py`** (DB-stored workflow path, line 88):
```python
# before
_build_workflow(record.config, source=..., extra_tools={})
# after
_build_workflow(record.config, source=..., extra_tools={}, extra_skills=load_skills(db))
```

**`ui/backend/crud.py`** (`upsert_workflow_config`, line 110):
```python
# before
_build_workflow(raw, source=_WORKFLOWS_DIR / f"{item_name}.yaml", extra_tools={})
# after
_build_workflow(raw, source=_WORKFLOWS_DIR / f"{item_name}.yaml", extra_tools={}, extra_skills=load_skills(db))
```

Validating at save time (crud.py) means an unknown skill reference is caught
immediately with an HTTP 400, not discovered at run time.

The `load_workflow(path)` YAML-file path in `main.py` (line 102) is unchanged —
demo YAML workflows are self-contained and do not reference DB-library skills.

**Skill deletion**: no cascade check in v1. Deleting a skill while a deployed
workflow references it causes `ConfigurationError("Unknown skill '...'")` at run
time (same shape as an unknown tool reference). Acceptable for a small admin-curated
library; a guard can be added in a follow-up if needed.

### 5. Solution Architect integration

**`ui/backend/builder.py`** — three changes:

**① `_with_skill_catalog(db, text)`** — new helper, parallel to `_with_model_catalog`.
Uses `load_skills(db)` so it doesn't need a direct `SkillRecord` import in `builder.py`:

```python
def _with_skill_catalog(db: Session, text: str) -> str:
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

If no skills are registered, the text is unchanged — backward compatible.

**② Call-site update** in `submit_specification` and `submit_solution_feedback`
(both `model=` branches):

```python
requirements_text = _with_model_catalog(db, requirements_text)
requirements_text = _with_skill_catalog(db, requirements_text)   # new
spec = _call_model(
    generate_specification, chat_model, requirements_text,
    source=source,
    extra_skills=load_skills(db),                                 # new
)
```

**③ `_ARCHITECT_SYSTEM_PROMPT` addition** — one short paragraph appended to the
existing prompt:

> "If skills are listed in the input, assign them to agents via each agent's
> `skills` field (a list of skill names). A Skill is a reusable instruction
> document for a repeatable task — prefer assigning one over re-describing
> the same task in `backstory`. Only use skill names from the provided list;
> never invent names."

### 6. Frontend

**`ui/frontend/src/lib/api.js`** — three new methods parallel to existing
`listAgents`/`upsertAgent`/`deleteAgent`:

```js
listSkills: () => apiFetch("/api/config/skills"),
upsertSkill: (name, config) => apiFetch(`/api/config/skills/${name}`, {
    method: "PUT", body: JSON.stringify(config),
}),
deleteSkill: (name) => apiFetch(`/api/config/skills/${name}`, { method: "DELETE" }),
```

**`ui/frontend/src/pages/AdvancedPage.jsx`** — add a Skills section using the
same "list + raw-JSON editor + save/delete" component pattern as the existing
agents / teams / knowledge\_bases sections. No new components needed.

### 7. Testing

All tests use `fake:` models — zero API cost, deterministic.

| File | New test(s) |
|---|---|
| `tests/test_db.py` | `SkillRecord` insert + read (2 assertions) |
| `tests/test_crud_api.py` | PUT upsert, GET list/single, DELETE, 400 on invalid spec |
| `tests/test_builder_api.py` | `_with_skill_catalog` output with/without records; `generate_specification` call receives `extra_skills` |
| `tests/test_specification.py` | `SkillSpec.to_raw()` omits empty fields; includes non-empty fields |
| Existing suite | Unchanged and passing — empty skill table = same behavior as today |

### 8. Documentation

- `ui/backend/db/CLAUDE.md` — add `skills` to the schema table (one line).
- `ui/backend/CLAUDE.md` — document `load_skills(db)` and `_with_skill_catalog`
  alongside the existing `_with_model_catalog` description.
- `docs/STATUS.md` — move sub-project 2 from "Next steps" to "Done" once
  implemented.

## Out of scope / deferred

- Dedicated `/skills` frontend route with search/categories — deferred until
  the library grows large enough to need it (sub-project 3 if ever needed).
- Skill versioning / snapshot-at-deploy — out of scope; live resolution
  (change a Skill, all referencing workflows benefit) is the desired semantic
  per Method B decision.
- Cascade delete guard (prevent deleting a skill still referenced by a
  deployed workflow) — deferred; current behavior (runtime `ConfigurationError`)
  is acceptable for a small admin-curated library.
- Solution Architect UI for skill assignment review — the existing `RefinePage`
  lets admins edit the full specification JSON (including `skills` fields)
  before deploying; no dedicated review step needed.
