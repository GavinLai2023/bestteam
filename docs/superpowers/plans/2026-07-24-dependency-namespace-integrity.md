# Dependency-namespace Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deleting a skill/KB a deployed workflow references is refused (409), and a KB whose name shadows a built-in tool is rejected at deploy (400).

**Architecture:** Two backend guards at the deploy/CRUD surface. P1-08: a pure `find_kb_tool_collisions` helper + a thin DB wrapper `kb_name_collisions`, called (name-only, before any KB build) at both deploy points. P1-07: a `_deployed_workflows_referencing` helper gating `delete_item`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, SQLite, pytest. Backend under `ui/backend/`. Spec: `docs/superpowers/specs/2026-07-24-dependency-namespace-integrity-design.md`.

## Global Constraints

- Run tests via `./.venv/Scripts/python.exe -m pytest`. TestClient tests use an in-memory DB (no `BESTTEAM_DB_PATH`). Run the **full** suite with a scratch DB: `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q` (delete the scratch db after).
- Built-in tool names come from `bestteam.tools.REGISTRY` (a name→callable dict). Both `crud.py` and `knowledge_bases.py` may import it.
- A deployed workflow references a **skill** by name in `agents[*].skills`, and a **standalone KB** by name in `agents[*].tools` (`ui/backend/knowledge_bases.py::load_knowledge_base_tools`).
- Collision detection is name-only and runs BEFORE any KB is built (so a KB path is never needed to trigger it). The per-org email-tool override is NOT a collision — only KB names are checked.
- P1-10 (KB ownership) and the typed-namespace rename are out of scope.
- Branch: `feat/dependency-namespace-integrity` (already off `main`). Commit after each task.

---

### Task 1: Pure collision helper `find_kb_tool_collisions`

**Files:**
- Modify: `ui/backend/deploy_validation.py`
- Test: `tests/test_deploy_validation.py`

**Interfaces:**
- Produces: `find_kb_tool_collisions(raw_spec: Dict[str, Any], standalone_kb_names: Iterable[str], builtin_names: Iterable[str]) -> List[str]` — sorted, de-duped KB names (inline `raw_spec["knowledge_bases"][*].name` ∪ `standalone_kb_names`) that are in `builtin_names`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_validation.py`:

```python
from ui.backend.deploy_validation import find_kb_tool_collisions


def test_inline_kb_name_shadowing_builtin_flagged():
    raw = {"knowledge_bases": [{"name": "calculator", "path": "x"}], "agents": []}
    assert find_kb_tool_collisions(raw, [], {"calculator", "web_search"}) == ["calculator"]


def test_standalone_kb_name_shadowing_builtin_flagged():
    assert find_kb_tool_collisions({}, ["calculator"], {"calculator"}) == ["calculator"]


def test_non_colliding_kb_names_pass():
    raw = {"knowledge_bases": [{"name": "product_docs", "path": "x"}], "agents": []}
    assert find_kb_tool_collisions(raw, ["returns_policy"], {"calculator", "web_search"}) == []


def test_collisions_sorted_and_deduped():
    raw = {"knowledge_bases": [{"name": "web_search"}], "agents": []}
    assert find_kb_tool_collisions(raw, ["calculator", "web_search"], {"calculator", "web_search"}) == ["calculator", "web_search"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_deploy_validation.py -q`
Expected: FAIL — `ImportError: cannot import name 'find_kb_tool_collisions'`.

- [ ] **Step 3: Add the helper**

Append to `ui/backend/deploy_validation.py`:

```python
def find_kb_tool_collisions(
    raw_spec: Dict[str, Any],
    standalone_kb_names: Iterable[str],
    builtin_names: Iterable[str],
) -> List[str]:
    """Return the KB names that would shadow a built-in tool.

    All tools resolve through one flat name->tool lookup, so a knowledge base
    named after a built-in silently shadows it. This returns the sorted,
    de-duplicated KB names -- inline (`raw_spec["knowledge_bases"][*].name`) plus
    the referenced standalone KB names -- that collide with `builtin_names`.
    """
    builtin = set(builtin_names)
    inline = {
        kb.get("name")
        for kb in raw_spec.get("knowledge_bases", []) or []
        if isinstance(kb, dict) and kb.get("name")
    }
    names = inline | set(standalone_kb_names)
    return sorted(n for n in names if n in builtin)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_deploy_validation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/deploy_validation.py tests/test_deploy_validation.py
git commit -m "feat(deploy): find_kb_tool_collisions helper (P1-08)"
```

---

### Task 2: Wire collision detection into both deploy points

**Files:**
- Modify: `ui/backend/knowledge_bases.py` (new `kb_name_collisions` wrapper)
- Modify: `ui/backend/builder.py` (`deploy_session`)
- Modify: `ui/backend/crud.py` (`upsert_workflow_config`)
- Test: `tests/test_org_settings.py`, `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `find_kb_tool_collisions` (Task 1); `bestteam.tools.REGISTRY`; `ui/backend/db/models.py::KnowledgeBaseRecord`.
- Produces: `kb_name_collisions(db: Session, org_id: Optional[int], raw_spec: Dict[str, Any]) -> List[str]` in `ui/backend/knowledge_bases.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crud_api.py` (uses the existing `_VALID_WORKFLOW_CONFIG`):

```python
def test_workflow_put_rejects_kb_named_after_builtin(client):
    # An inline KB named after a built-in tool would silently shadow it.
    bad = {**_VALID_WORKFLOW_CONFIG,
           "knowledge_bases": [{"name": "calculator", "path": "docs"}],
           "agents": [{**_VALID_WORKFLOW_CONFIG["agents"][0], "tools": ["calculator"]}]}
    resp = client.put("/api/config/workflows/collide_wf?org=default", json=bad)
    assert resp.status_code == 400
    assert "calculator" in resp.json()["detail"]
    assert "collide_wf" not in client.get(
        "/api/workflows", headers=_org_user_headers(client)
    ).json()["workflows"]
```

Append to `tests/test_org_settings.py` (it already defines `_make_session(spec)`, which inserts `specification_json` directly and whose `client` is a default-org user — the `POST /specification` route validates/builds, so we bypass it to isolate the deploy-time collision check):

```python
def test_deploy_rejects_kb_named_after_builtin(client):
    bad_spec = {
        "name": "collide_team",
        "knowledge_bases": [{"name": "web_search", "path": "docs"}],
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi",
                    "tools": ["web_search"]}],
        "teams": [], "workflow": {"steps": []},
    }
    sid = _make_session(bad_spec)
    resp = client.post(f"/api/builder/sessions/{sid}/deploy")
    assert resp.status_code == 400
    assert "web_search" in resp.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py::test_workflow_put_rejects_kb_named_after_builtin tests/test_org_settings.py::test_deploy_rejects_kb_named_after_builtin -q`
Expected: FAIL — currently deploy/upsert either 200s or fails for an unrelated reason (no collision check).

- [ ] **Step 3: Add the `kb_name_collisions` wrapper**

Append to `ui/backend/knowledge_bases.py` (it already imports `Session`, `KnowledgeBaseRecord`; add the two imports at the top if absent — `from typing import ... List`, `from .deploy_validation import find_kb_tool_collisions`, `from bestteam.tools import REGISTRY`):

```python
def kb_name_collisions(db: Session, org_id: Optional[int], raw_spec: Dict[str, Any]) -> List[str]:
    """KB names in `raw_spec` (inline + referenced standalone) that shadow a built-in tool.

    Name-only (no KB is built), so it can run before path validation / build.
    """
    referenced = {
        tool
        for agent in raw_spec.get("agents", []) or []
        for tool in (agent.get("tools") or [])
    }
    standalone: set = set()
    if referenced:
        standalone = {
            row.name
            for row in db.query(KnowledgeBaseRecord.name).filter(
                KnowledgeBaseRecord.org_id == org_id,
                KnowledgeBaseRecord.name.in_(referenced),
            )
        }
    return find_kb_tool_collisions(raw_spec, standalone, REGISTRY)
```

- [ ] **Step 4: Wire into `crud.upsert_workflow_config`**

In `ui/backend/crud.py`, add `kb_name_collisions` to the existing `from .knowledge_bases import ...` line. Then in `upsert_workflow_config`, immediately after `raw = {**config, "name": item_name}` and BEFORE the `try:` block, insert:

```python
    kb_collisions = kb_name_collisions(db, org_id, raw)
    if kb_collisions:
        raise HTTPException(
            status_code=400,
            detail=(
                "A knowledge base can't reuse a built-in tool name: "
                + ", ".join(kb_collisions)
                + ". Rename the knowledge base."
            ),
        )
```

- [ ] **Step 5: Wire into `builder.deploy_session`**

In `ui/backend/builder.py`, add `kb_name_collisions` to the existing `from .knowledge_bases import ...` line. Then in `deploy_session`, immediately after `_reject_unsafe_kb_paths(spec)`, insert:

```python
    kb_collisions = kb_name_collisions(db, org.id, spec.to_raw())
    if kb_collisions:
        raise HTTPException(
            status_code=400,
            detail=(
                "A knowledge base can't reuse a built-in tool name: "
                + ", ".join(kb_collisions)
                + ". Rename the knowledge base."
            ),
        )
```

- [ ] **Step 6: Run the new tests + touched files**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py tests/test_org_settings.py -q`
Expected: PASS (the two new tests pass; existing tests unaffected — no existing fixture names a KB after a built-in).

- [ ] **Step 7: Commit**

```bash
git add ui/backend/knowledge_bases.py ui/backend/crud.py ui/backend/builder.py tests/test_crud_api.py tests/test_org_settings.py
git commit -m "feat(deploy): reject KB names that shadow built-in tools (P1-08)"
```

---

### Task 3: Refuse deleting a skill/KB referenced by a deployed workflow

**Files:**
- Modify: `ui/backend/crud.py` (`_deployed_workflows_referencing` + `delete_item` guard)
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Produces: `_deployed_workflows_referencing(db: Session, org_id: Optional[int], kind: str, name: str) -> list[str]` — sorted names of `status="deployed"` `WorkflowRecord`s referencing `name` (`kind="skill"` → `agents[*].skills`; `kind="knowledge_base"` → `agents[*].tools`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crud_api.py` (add `from ui.backend.db.models import SkillRecord, KnowledgeBaseRecord, WorkflowRecord` and `from helpers import open_test_db, get_org_id` at the top if not already imported):

```python
def _deployed_wf(config_agents):
    return {"agents": config_agents, "teams": [], "workflow": {"steps": []}}


def test_delete_skill_referenced_by_deployed_workflow_is_409(client):
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(SkillRecord(name="greeting", org_id=org_id,
                           config={"name": "greeting", "instructions": "hi", "tools": []}))
        db.add(WorkflowRecord(name="greeter_team", org_id=org_id, status="deployed",
                              config=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                                                    "model": "fake:hi", "skills": ["greeting"]}])))
        db.commit()
    resp = client.delete("/api/config/skills/greeting?org=default")
    assert resp.status_code == 409
    assert "greeter_team" in resp.json()["detail"]
    assert client.get("/api/config/skills/greeting?org=default").status_code == 200  # not deleted


def test_delete_kb_referenced_by_deployed_workflow_is_409(client):
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(KnowledgeBaseRecord(name="mykb", org_id=org_id,
                                   config={"name": "mykb", "path": "docs"}))
        db.add(WorkflowRecord(name="kb_team", org_id=org_id, status="deployed",
                              config=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                                                    "model": "fake:hi", "tools": ["mykb"]}])))
        db.commit()
    resp = client.delete("/api/config/knowledge_bases/mykb?org=default")
    assert resp.status_code == 409
    assert "kb_team" in resp.json()["detail"]


def test_delete_unreferenced_skill_still_204(client):
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(SkillRecord(name="unused", org_id=org_id,
                           config={"name": "unused", "instructions": "x", "tools": []}))
        db.commit()
    assert client.delete("/api/config/skills/unused?org=default").status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py::test_delete_skill_referenced_by_deployed_workflow_is_409 tests/test_crud_api.py::test_delete_kb_referenced_by_deployed_workflow_is_409 tests/test_crud_api.py::test_delete_unreferenced_skill_still_204 -q`
Expected: FAIL — the two 409 tests get 204 (delete succeeds unconditionally); the unreferenced test passes.

- [ ] **Step 3: Add the reference helper**

In `ui/backend/crud.py`, add near the other module helpers (e.g. after `_invalidate_workflow_cache`):

```python
def _deployed_workflows_referencing(db: Session, org_id, kind: str, name: str) -> list[str]:
    """Names of deployed workflows whose config references `name`.

    `kind="skill"` matches an agent's `skills`; `kind="knowledge_base"` matches an
    agent's `tools` (a standalone KB is referenced by name there).
    """
    field = "skills" if kind == "skill" else "tools"
    query = db.query(WorkflowRecord).filter(WorkflowRecord.status == "deployed")
    if org_id is not None:
        query = query.filter(WorkflowRecord.org_id == org_id)
    hits = []
    for row in query:
        for agent in (row.config or {}).get("agents", []) or []:
            if isinstance(agent, dict) and name in (agent.get(field) or []):
                hits.append(row.name)
                break
    return sorted(hits)
```

- [ ] **Step 4: Guard `delete_item`**

In `ui/backend/crud.py`, inside `_make_component_router`'s `delete_item`, immediately after the `if item is None: raise HTTPException(404, ...)` check and BEFORE the `if name == "knowledge_bases":` rmtree block, insert:

```python
        if name in ("skills", "knowledge_bases"):
            kind = "skill" if name == "skills" else "knowledge_base"
            used_by = _deployed_workflows_referencing(db, org_id, kind, item_name)
            if used_by:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Can't delete '{item_name}': it's used by deployed team(s): "
                        + ", ".join(used_by)
                        + ". Update or remove those teams first."
                    ),
                )
```

- [ ] **Step 5: Run the new tests + the delete/CRUD suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -q`
Expected: PASS (the three new tests pass; existing delete tests still pass — their skills/KBs aren't referenced by any deployed workflow).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/crud.py tests/test_crud_api.py
git commit -m "feat(config): refuse deleting a skill/KB used by a deployed workflow (P1-07)"
```

---

### Task 4: Docs + full verification

**Files:**
- Modify: `ui/backend/CLAUDE.md`, `ui/backend/db/CLAUDE.md`, `docs/STATUS.md`, `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`

- [ ] **Step 1: Update `ui/backend/CLAUDE.md`**

In the `crud.py` section, note: deleting a skill/KB via `/api/config/{skills,knowledge_bases}` is refused with 409 if a `status="deployed"` workflow references it (skill → `agents[*].skills`, KB → `agents[*].tools`; `_deployed_workflows_referencing`). Deploy and operator save reject (400) a workflow whose inline/standalone KB name shadows a built-in tool (`knowledge_bases.kb_name_collisions` → `deploy_validation.find_kb_tool_collisions`, name-only, before build); the per-org email-tool override is unaffected.

- [ ] **Step 2: Update `ui/backend/db/CLAUDE.md`**

In the `knowledge_bases` / `skills` bullet, note that a deployed workflow's references (KB in agent `tools`, skill in agent `skills`) now block deletion of the referenced record (409), and a KB may not be named after a built-in tool (rejected at deploy).

- [ ] **Step 3: Update `docs/STATUS.md`**

Add a Done entry: P1-07 (delete of a skill/KB used by a deployed workflow → 409) and P1-08 (KB name shadowing a built-in tool → 400 at deploy; collision detection, not the typed-namespace rename) from the data-architecture review, with the test names and spec path.

- [ ] **Step 4: Update `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`**

Move P1-07 and P1-08 into an "Implemented" entry (mirroring the P1-06/P1-11 rows), citing the spec, helpers, and tests; note P1-08 was scoped to collision detection (typed-namespace rename deferred) and P1-10 split out. Decrement the "remaining findings" count accordingly (26 → 24) and remove P1-07/P1-08 from the remaining ID list.

- [ ] **Step 5: Full-suite + frontend verification**

Run:
```bash
rm -f .superpowers/sdd/scratch.db
BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q
rm -f .superpowers/sdd/scratch.db
```
Expected: PASS (previous count + the new tests). Then `cd ui/frontend && npm run lint && npm run build` (expect clean; frontend untouched).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/CLAUDE.md ui/backend/db/CLAUDE.md docs/STATUS.md docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md
git commit -m "docs: record dependency-namespace integrity (P1-07 deletion safety + P1-08 collision detection)"
```

---

## Self-Review

**Spec coverage:**
- P1-07 "refuse deleting a referenced skill/KB (409), deployed-only, name the teams" → Task 3. ✓
- P1-08 "reject KB name shadowing a built-in at both deploy points (400)" → Task 1 (pure helper) + Task 2 (wrapper + wiring). ✓
- Email-override not a false positive (only KB names checked) → Task 2 `kb_name_collisions` (checks KB names only). ✓
- Docs/triage update → Task 4. ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `find_kb_tool_collisions(raw_spec, standalone_kb_names, builtin_names) -> List[str]` (Task 1) is called by `kb_name_collisions(db, org_id, raw_spec) -> List[str]` (Task 2) with `REGISTRY` as `builtin_names`; both deploy points call `kb_name_collisions(db, org(.)id, raw)` and raise the identical 400. `_deployed_workflows_referencing(db, org_id, kind, name) -> list[str]` (Task 3) is called from `delete_item` with `kind in ("skill","knowledge_base")`. Consistent.
