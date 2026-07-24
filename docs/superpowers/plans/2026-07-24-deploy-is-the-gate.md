# Deploy is the Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Only `status='deployed'` workflows are listed/runnable (operator save = deploy), and deploy validates each agent's model against the model catalog so a bad model fails at deploy, not first run.

**Architecture:** Two small changes at the deploy/run lifecycle. P1-11 adds a pure `validate_agent_models` helper called at both deploy points (`deploy_session`, `crud.upsert_workflow_config`). P1-06 filters workflow resolution/listing to `deployed`, makes operator CRUD save as `deployed`, and adds a CHECK-constrained status column via a guarded migration.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, SQLite, pytest. Backend under `ui/backend/`. Design spec: `docs/superpowers/specs/2026-07-24-deploy-is-the-gate-design.md`.

## Global Constraints

- Run everything through the venv: `./.venv/Scripts/python.exe -m pytest`.
- The full suite must be run with a scratch DB to avoid the import-time secrets guard on the dev DB: `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q` (delete the scratch db after).
- `fake:` model specs are always exempt from model validation (deterministic, zero-cost demo/test models).
- Migrations are guarded by inspection (the app runs `create_all` at import, then `alembic upgrade head`) — see `alembic/versions/57b13700d5df_*` and `884e80106da7_*` for the pattern; the current alembic head is `57b13700d5df`.
- `/api/config/workflows` (admin CRUD list) stays unfiltered — operators see all configs. Only `/api/workflows` (the run surface) and `_get_workflow` (run resolution) filter to `deployed`.
- Branch: `feat/deploy-is-the-gate` (already created off `main`). Commit after each task.

---

### Task 1: Model-validation helper (pure function)

**Files:**
- Create: `ui/backend/deploy_validation.py`
- Test: `tests/test_deploy_validation.py`

**Interfaces:**
- Produces: `validate_agent_models(raw_spec: Dict[str, Any], catalog_specs: Iterable[str]) -> List[str]` — returns the agent model specs not offered by the platform (unknown = not in `catalog_specs` and not a `fake:` spec), first-seen order, de-duplicated.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deploy_validation.py`:

```python
from ui.backend.deploy_validation import validate_agent_models


def _spec(*models):
    return {"agents": [{"name": f"a{i}", "role": "r", "goal": "g", "model": m}
                       for i, m in enumerate(models)]}


def test_unknown_model_flagged():
    assert validate_agent_models(_spec("openai:gpt-x"), {"openai:gpt-4o"}) == ["openai:gpt-x"]


def test_catalog_model_passes():
    assert validate_agent_models(_spec("openai:gpt-4o"), {"openai:gpt-4o"}) == []


def test_fake_specs_exempt_even_with_empty_catalog():
    assert validate_agent_models(_spec("fake:hi", "fake:ok"), set()) == []


def test_multiple_unknowns_aggregated_and_deduped():
    assert validate_agent_models(_spec("m1", "m2", "m1"), set()) == ["m1", "m2"]


def test_missing_or_malformed_agents_ignored():
    assert validate_agent_models({}, set()) == []
    assert validate_agent_models({"agents": [42, {"name": "a"}]}, set()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_deploy_validation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ui.backend.deploy_validation'`.

- [ ] **Step 3: Write the helper**

Create `ui/backend/deploy_validation.py`:

```python
"""Deploy-time validation that complements the SDK's structural validation.

`validate_specification` (SDK) resolves an agent's tools/skills/KB references,
but not that its model is one the platform actually offers. A bad model spec
would otherwise pass deploy and fail only at first run. This checks agent model
specs against the model catalog so the failure surfaces at deploy.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List


def validate_agent_models(raw_spec: Dict[str, Any], catalog_specs: Iterable[str]) -> List[str]:
    """Return the agent model specs in `raw_spec` that the platform doesn't offer.

    A spec is offered if it is in `catalog_specs`. `fake:` specs (deterministic,
    zero-cost demo/test models) are always allowed and never reported. The result
    keeps first-seen order and is de-duplicated so the caller can name every
    rejected model at once.
    """
    allowed = set(catalog_specs)
    unknown: List[str] = []
    seen = set()
    for agent in raw_spec.get("agents", []) or []:
        if not isinstance(agent, dict):
            continue
        model = agent.get("model")
        if not model or model.startswith("fake:"):
            continue
        if model not in allowed and model not in seen:
            unknown.append(model)
        seen.add(model)
    return unknown
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_deploy_validation.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/deploy_validation.py tests/test_deploy_validation.py
git commit -m "feat(deploy): validate_agent_models helper (P1-11)"
```

---

### Task 2: Wire model validation into both deploy points

**Files:**
- Modify: `ui/backend/builder.py` (`deploy_session`, after `raw = spec.to_raw()`)
- Modify: `ui/backend/crud.py` (`upsert_workflow_config`, after `_build_workflow` succeeds)
- Test: `tests/test_builder_api.py`, `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `validate_agent_models` (Task 1); `list_entries(db) -> List[ModelCatalogEntry]` (already imported in both files, from `ui/backend/db/model_catalog.py`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_builder_api.py` (near the existing deploy tests; `_VALID_SPEC` already defined there):

```python
def test_deploy_rejects_agent_model_not_in_catalog(client):
    bad_spec = {**_VALID_SPEC, "agents": [{**_VALID_SPEC["agents"][0], "model": "openai:gpt-nope"}]}
    sid = client.post("/api/builder/sessions", json={"intent_text": "bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{sid}/specification", json={"specification": bad_spec})
    resp = client.post(f"/api/builder/sessions/{sid}/deploy")
    assert resp.status_code == 400
    assert "openai:gpt-nope" in resp.json()["detail"]
```

Append to `tests/test_crud_api.py` (after the workflow tests; `_VALID_WORKFLOW_CONFIG` already defined):

```python
def test_workflow_put_rejects_agent_model_not_in_catalog(client):
    bad_config = {**_VALID_WORKFLOW_CONFIG,
                  "agents": [{**_VALID_WORKFLOW_CONFIG["agents"][0], "model": "openai:gpt-nope"}]}
    resp = client.put("/api/config/workflows/support_workflow?org=default", json=bad_config)
    assert resp.status_code == 400
    assert "openai:gpt-nope" in resp.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py::test_deploy_rejects_agent_model_not_in_catalog tests/test_crud_api.py::test_workflow_put_rejects_agent_model_not_in_catalog -q`
Expected: FAIL — both return 200 (no model validation yet).

- [ ] **Step 3a: Wire into `deploy_session`**

In `ui/backend/builder.py`, add the import near the other `.` imports (beside `from .db.model_catalog import list_entries, to_prompt_text`):

```python
from .deploy_validation import validate_agent_models
```

Then in `deploy_session`, immediately after `raw = spec.to_raw()` (currently line 499) and before the `record = db.query(...)` upsert, insert:

```python
    unknown_models = validate_agent_models(raw, {e.spec for e in list_entries(db)})
    if unknown_models:
        raise HTTPException(
            status_code=400,
            detail=(
                "These models aren't available on this platform: "
                + ", ".join(unknown_models)
                + ". Pick a model from the catalog."
            ),
        )
```

- [ ] **Step 3b: Wire into `upsert_workflow_config`**

In `ui/backend/crud.py`, add the import near the other `.` imports (beside `from .db.model_catalog import ...`):

```python
from .deploy_validation import validate_agent_models
```

Then in `upsert_workflow_config`, after the `_build_workflow(...)` call inside the `try` succeeds — i.e. right after the `except (KeyError, TypeError, BestTeamError) ...` block (currently ends line 487) and before `item = db.query(WorkflowRecord)...` (line 489) — insert:

```python
    unknown_models = validate_agent_models(raw, {e.spec for e in list_entries(db)})
    if unknown_models:
        raise HTTPException(
            status_code=400,
            detail=(
                "These models aren't available on this platform: "
                + ", ".join(unknown_models)
                + ". Pick a model from the catalog."
            ),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py::test_deploy_rejects_agent_model_not_in_catalog tests/test_crud_api.py::test_workflow_put_rejects_agent_model_not_in_catalog -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run both touched test files to confirm no regression**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py tests/test_crud_api.py -q`
Expected: PASS (existing `fake:`-spec deploy/crud tests still pass — the `fake:` exemption covers them).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/builder.py ui/backend/crud.py tests/test_builder_api.py tests/test_crud_api.py
git commit -m "feat(deploy): reject agent models not in the catalog at deploy (P1-11)"
```

---

### Task 3: Migration — CHECK-constrain `workflows.status` + backfill

**Files:**
- Modify: `ui/backend/db/models.py` (`WorkflowRecord.__table_args__`, import `CheckConstraint`)
- Create: `alembic/versions/b1d7e4f2a9c8_workflows_status_check.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: schema constraint `ck_workflows_status` on `workflows.status IN ('draft','ready_for_testing','deployed')`; existing non-`deployed` rows backfilled to `deployed`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrations.py` (it already imports `sqlalchemy as sa`, `command`, `make_engine`, and has `_alembic_config` / `_PRE_DROP`):

```python
# Revision just before the workflows.status CHECK migration.
_PRE_STATUS = "57b13700d5df"


def test_existing_non_deployed_workflow_backfilled_to_deployed(tmp_path, monkeypatch):
    db_path = tmp_path / "status_backfill.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_STATUS)  # workflows table exists, no status CHECK yet

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (name, org_id, config, status, created_at, updated_at) "
            "VALUES ('legacy', NULL, '{}', 'draft', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            status = conn.execute(sa.text("SELECT status FROM workflows WHERE name='legacy'")).scalar()
            assert status == "deployed"
    finally:
        engine.dispose()


def test_status_check_rejects_invalid_value(tmp_path, monkeypatch):
    db_path = tmp_path / "status_check.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.begin() as conn:
            with pytest.raises(Exception):  # IntegrityError/OperationalError on CHECK
                conn.execute(sa.text(
                    "INSERT INTO workflows (name, org_id, config, status, created_at, updated_at) "
                    "VALUES ('bad', NULL, '{}', 'bogus', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ))
    finally:
        engine.dispose()
```

(`tests/test_migrations.py` already imports `pytest`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py -q`
Expected: FAIL — `test_status_check_rejects_invalid_value` fails (no CHECK yet; the insert succeeds) and/or the head revision doesn't exist.

- [ ] **Step 3a: Add the CHECK to the model**

In `ui/backend/db/models.py`, extend the import (line 15) to include `CheckConstraint`:

```python
from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, UniqueConstraint, text
```

Change `WorkflowRecord.__table_args__` (currently line 114) to:

```python
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_workflows_org_id_name"),
        CheckConstraint(
            "status IN ('draft', 'ready_for_testing', 'deployed')",
            name="ck_workflows_status",
        ),
    )
```

- [ ] **Step 3b: Write the migration**

Create `alembic/versions/b1d7e4f2a9c8_workflows_status_check.py`:

```python
"""constrain workflows.status + backfill legacy rows to deployed

Revision ID: b1d7e4f2a9c8
Revises: 57b13700d5df
Create Date: 2026-07-24 00:00:00.000000

Only `status='deployed'` workflows are runnable/listed now (P1-06). Existing
rows were runnable regardless of status, so backfill non-deployed -> deployed to
preserve behavior on upgrade. Then add a CHECK bounding the column.

Guarded (create_all-at-import idempotency): the model now declares the CHECK, so
a fresh create_all database already has `ck_workflows_status`; add it only when
absent, matching the other migrations' inspection guards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1d7e4f2a9c8'
down_revision: Union[str, Sequence[str], None] = '57b13700d5df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALLOWED = "status IN ('draft', 'ready_for_testing', 'deployed')"


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("UPDATE workflows SET status = 'deployed' WHERE status != 'deployed'")
    existing = {c["name"] for c in sa.inspect(bind).get_check_constraints("workflows")}
    if "ck_workflows_status" not in existing:
        with op.batch_alter_table("workflows") as batch:
            batch.create_check_constraint("ck_workflows_status", _ALLOWED)


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints("workflows")}
    if "ck_workflows_status" in existing:
        with op.batch_alter_table("workflows") as batch:
            batch.drop_constraint("ck_workflows_status", type_="check")
```

- [ ] **Step 4: Run the migration tests + the existing idempotency test**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py -q`
Expected: PASS (all — including the existing `test_create_all_then_upgrade_head_is_idempotent`, since the migration skips the CHECK when create_all already added it).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/models.py alembic/versions/b1d7e4f2a9c8_workflows_status_check.py tests/test_migrations.py
git commit -m "feat(db): CHECK-constrain workflows.status + backfill legacy rows to deployed (P1-06)"
```

---

### Task 4: Enforce deployed-only run/list; operator save = deploy

**Files:**
- Modify: `ui/backend/main.py` (`_get_workflow` both query branches; `list_workflows`)
- Modify: `ui/backend/crud.py` (`upsert_workflow_config` — write `status="deployed"` on insert and update)
- Test: `tests/test_crud_api.py` (fix the `status` assertion; add the enforcement test)

**Interfaces:**
- Consumes: the CHECK-constrained `status` column (Task 3).

- [ ] **Step 1: Write the failing test + fix the stale assertion**

In `tests/test_crud_api.py`, change the assertion in `test_workflow_crud_round_trip_and_validation` (currently line 722) from:

```python
    assert body["status"] == "draft"
```

to:

```python
    assert body["status"] == "deployed"
```

Then append a new enforcement test (imports `open_test_db`, `get_org_id` from `helpers`, and `WorkflowRecord` — add `from ui.backend.db.models import WorkflowRecord` at the top if not present):

```python
def test_only_deployed_workflows_are_listed_and_runnable(client):
    from helpers import open_test_db, get_org_id
    from ui.backend.db.models import WorkflowRecord

    # crud save = deploy -> runnable immediately
    client.put("/api/config/workflows/live_wf?org=default", json=_VALID_WORKFLOW_CONFIG)
    # a draft can only exist as a legacy/direct row now
    with open_test_db() as db:
        org_id = get_org_id(db, "default")
        db.add(WorkflowRecord(name="draft_wf",
                              config={**_VALID_WORKFLOW_CONFIG, "name": "draft_wf"},
                              status="draft", org_id=org_id))
        db.commit()

    headers = _org_user_headers(client)
    workflows = client.get("/api/workflows", headers=headers).json()["workflows"]
    assert "live_wf" in workflows
    assert "draft_wf" not in workflows
    assert client.post("/api/runs", json={"workflow": "live_wf", "input": "hi"},
                       headers=headers).status_code == 200
    assert client.post("/api/runs", json={"workflow": "draft_wf", "input": "hi"},
                       headers=headers).status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest "tests/test_crud_api.py::test_workflow_crud_round_trip_and_validation" "tests/test_crud_api.py::test_only_deployed_workflows_are_listed_and_runnable" -q`
Expected: FAIL — round-trip test fails on `deployed != draft`; enforcement test fails because `draft_wf` is currently listed and runnable.

- [ ] **Step 3a: Filter `_get_workflow` to deployed**

In `ui/backend/main.py::_get_workflow`, both `WorkflowRecord` lookups (currently lines 360 and 364) change from:

```python
        record = db.query(WorkflowRecord).filter_by(name=name, org_id=org_id).one_or_none()
```

to:

```python
        record = db.query(WorkflowRecord).filter_by(name=name, org_id=org_id, status="deployed").one_or_none()
```

(Apply to both the `if db is not None:` branch and the `else:` SessionLocal branch.)

- [ ] **Step 3b: Filter `list_workflows` to deployed**

In `ui/backend/main.py::list_workflows` (currently line 457-459), change:

```python
    db_names = {
        row.name for row in db.query(WorkflowRecord.name).filter(WorkflowRecord.org_id == org.id)
    }
```

to:

```python
    db_names = {
        row.name
        for row in db.query(WorkflowRecord.name).filter(
            WorkflowRecord.org_id == org.id, WorkflowRecord.status == "deployed"
        )
    }
```

- [ ] **Step 3c: Operator save = deploy**

In `ui/backend/crud.py::upsert_workflow_config` (currently lines 489-494), change:

```python
    item = db.query(WorkflowRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        item = WorkflowRecord(name=item_name, config=raw, status="draft", org_id=org_id)
        db.add(item)
    else:
        item.config = raw
```

to:

```python
    item = db.query(WorkflowRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        item = WorkflowRecord(name=item_name, config=raw, status="deployed", org_id=org_id)
        db.add(item)
    else:
        item.config = raw
        item.status = "deployed"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest "tests/test_crud_api.py::test_workflow_crud_round_trip_and_validation" "tests/test_crud_api.py::test_only_deployed_workflows_are_listed_and_runnable" -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the touched test files to confirm no regression**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py tests/test_builder_api.py tests/test_main.py -q`
Expected: PASS (existing crud→run tests still pass because crud now writes `deployed`).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/main.py ui/backend/crud.py tests/test_crud_api.py
git commit -m "feat(deploy): only deployed workflows run/list; operator save = deploy (P1-06)"
```

---

### Task 5: Docs + full-suite verification

**Files:**
- Modify: `ui/backend/CLAUDE.md`, `ui/backend/db/CLAUDE.md`, `docs/STATUS.md`, `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`

- [ ] **Step 1: Update `ui/backend/CLAUDE.md`**

In the `_get_workflow` bullet and the `crud.py` section, note: only `status='deployed'` `WorkflowRecord`s are resolved by `_get_workflow` and listed by `GET /api/workflows`; `/api/config/workflows` (admin) still lists all. An operator save via `/api/config/workflows` writes `status='deployed'` (save = deploy). Deploy (`deploy_session`) and operator save both validate agent models against the model catalog (`deploy_validation.validate_agent_models`, `fake:` exempt) and 400 on any model not offered.

- [ ] **Step 2: Update `ui/backend/db/CLAUDE.md`**

In the `workflows` bullet, note `status` is CHECK-constrained to `('draft','ready_for_testing','deployed')` (migration `b1d7e4f2a9c8`), only `deployed` is runnable/listed, and legacy rows were backfilled to `deployed`.

- [ ] **Step 3: Update `docs/STATUS.md`**

Add a Done entry: P1-06 (only deployed workflows run/list; operator save = deploy; status CHECK + backfill) and P1-11 (deploy-time model-catalog validation, `fake:` exempt) from the data-architecture review, with TDD regressions.

- [ ] **Step 4: Update `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`**

Move P1-06 and P1-11 out of the "validated-accurate, out of scope" set into an "Implemented" entry (mirroring the P1-09/P1-14 rows), citing this spec/plan and the tests.

- [ ] **Step 5: Full-suite verification**

Run:
```bash
rm -f .superpowers/sdd/scratch.db
BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q
rm -f .superpowers/sdd/scratch.db
```
Expected: PASS (previous count + the new tests; no failures introduced). Then frontend (unchanged, but confirm nothing broke): `cd ui/frontend && npm run lint && npm run build`.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/CLAUDE.md ui/backend/db/CLAUDE.md docs/STATUS.md docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md
git commit -m "docs: record deploy-is-the-gate (P1-06 lifecycle enforcement + P1-11 model validation)"
```

---

## Self-Review

**Spec coverage:**
- P1-06 "only deployed runs/lists" → Task 4 (`_get_workflow`, `list_workflows`). ✓
- P1-06 "save = deploy" → Task 4 (`crud.upsert_workflow_config`). ✓
- P1-06 "bounded status enum + backfill" → Task 3 (model CHECK + guarded migration). ✓
- P1-11 "validate agent models against catalog at deploy" → Task 1 (helper) + Task 2 (both deploy points). ✓
- Preview/production already separate → no change needed (design note); Task 4 touches only production paths. ✓
- Docs/triage update → Task 5. ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `validate_agent_models(raw_spec, catalog_specs) -> List[str]` defined in Task 1 and called identically in Task 2 (both pass `{e.spec for e in list_entries(db)}`). `status="deployed"` string used consistently across Tasks 3-4. Migration id `b1d7e4f2a9c8` / down_revision `57b13700d5df` consistent between the migration and the test's `_PRE_STATUS`.
