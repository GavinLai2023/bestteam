# Typed Dependency Records (P1-04) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Materialize each skill/standalone-KB dependency of a published workflow version into a typed DB row with a stable `resource_id`, and rewire the skill/KB delete guard to query those rows instead of scanning JSON.

**Architecture:** A new `workflow_dependencies` table, one row per (version, skill|KB). Rows are written once at deploy inside `db/workflows.py::publish_workflow_version` (the single funnel both deploy points already use). The skill/KB delete guard in `crud.delete_item` switches from a raw JSON scan to a precise `resource_id` query joined through each workflow's *current* version. A guarded Alembic migration creates the table and backfills existing deployed workflows' current versions.

**Tech Stack:** Python 3, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic (guarded/idempotent migrations, `op.batch_alter_table` for SQLite), FastAPI, pytest. Run via the project venv: `./.venv/Scripts/python.exe`.

## Global Constraints

- **Kinds recorded: skills + standalone KBs only.** Built-in tools and model specs are NOT recorded (schema has a general `resource_kind` column so they can be added later without a migration, but no rows/tests for them now — YAGNI).
- **Delete-block scope: current deployed config only** — behaviorally identical to today's guard, sourced from typed rows. Non-regressing.
- **Backend / data-model only.** No frontend changes.
- **No SQLite FK-enforcement toggle** (P1-13 deferred). The new FK is advisory like every other FK.
- **Migrations are guarded/idempotent:** `ui/backend/db_session.py` runs `Base.metadata.create_all` at import *before* `alembic upgrade`, so every `create_table`/`add_column` must be inspect-guarded and every backfill must be a no-op on re-run. Use `op.batch_alter_table` for any column op (SQLite can't `ALTER` in place).
- **Constraint naming (explicit, by hand — there is no `naming_convention` on the metadata):** unique constraint is `uq_workflow_dependencies_version_kind_name`.
- **Both deploy points and both component-delete paths already run inside the process-wide `component_mutation_lock`** — the new recording/guard/cascade inherit it; do not add new locks.
- **`resource_id` is nullable** on purpose: a backfilled row for a name that no longer resolves stays NULL (harmless — it can't block a delete, and such a ref can't build). Forward deploys always resolve it.
- Full-suite command (from root `CLAUDE.md`): `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q` (a dev DB trips an import-time secrets guard; the scratch DB avoids it).

## File Structure

- `ui/backend/db/models.py` — add `WorkflowDependency` model (after `WorkflowVersion`).
- `ui/backend/db/dependencies.py` (**new**) — `record_version_dependencies` (deploy-time populate) and `workflows_referencing` (reverse guard query). One responsibility: the typed-dependency read/write helpers.
- `ui/backend/db/workflows.py` — `publish_workflow_version` calls `record_version_dependencies`.
- `ui/backend/crud.py` — `delete_item` uses `workflows_referencing`; delete the now-orphaned `_deployed_workflows_referencing`; `delete_workflow_config` cascades dep rows.
- `alembic/versions/d4e6b2c9f1a7_workflow_dependencies.py` (**new**) — create table + Python backfill.
- `tests/test_dependencies.py` (**new**), `tests/test_db.py`, `tests/test_migrations.py`, `tests/test_crud_api.py` — coverage.
- Docs: `ui/backend/db/CLAUDE.md`, `ui/backend/CLAUDE.md`, `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`, `docs/STATUS.md`.

---

### Task 1: Schema + migration + table/backfill tests

**Files:**
- Modify: `ui/backend/db/models.py` (add `WorkflowDependency` after `WorkflowVersion`, ~line 158)
- Create: `alembic/versions/d4e6b2c9f1a7_workflow_dependencies.py`
- Modify: `tests/test_db.py` (add table to the strict set)
- Modify: `tests/test_migrations.py` (add table to `_EXPECTED_HEAD_TABLES`; new backfill test)

**Interfaces:**
- Produces: `WorkflowDependency` ORM model — columns `id`, `workflow_version_id: int` (FK→`workflow_versions.id`), `resource_kind: str`, `resource_name: str`, `resource_id: Optional[int]`; unique `(workflow_version_id, resource_kind, resource_name)`. Table name `workflow_dependencies`. Migration head becomes `d4e6b2c9f1a7` (down_revision `c3f5a1b8e2d4`).

- [ ] **Step 1: Add the `WorkflowDependency` model**

In `ui/backend/db/models.py`, immediately after the `WorkflowVersion` class (which ends at ~line 158, before `class OrgEmailCredential`), add:

```python
class WorkflowDependency(Base):
    """A typed record of one skill/KB a published workflow version depends on.

    Materialized at deploy from the version's inline config (agents[*].skills and
    the standalone KBs named in agents[*].tools) so the DB can answer "what depends
    on this resource?" and the skill/KB delete guard can RESTRICT by a precise
    resource_id instead of re-scanning every deployed workflow's JSON (P1-04).
    Written once per version (a version is immutable); resource_id is the resolved
    SkillRecord/KnowledgeBaseRecord id (NULL only for a backfilled name that no
    longer resolves -- harmless, such a ref can't build)."""

    __tablename__ = "workflow_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "workflow_version_id", "resource_kind", "resource_name",
            name="uq_workflow_dependencies_version_kind_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_version_id: Mapped[int] = mapped_column(ForeignKey("workflow_versions.id"))
    resource_kind: Mapped[str]
    resource_name: Mapped[str]
    resource_id: Mapped[Optional[int]] = mapped_column(nullable=True)
```

(`UniqueConstraint`, `ForeignKey`, `Mapped`, `mapped_column`, `Optional` are already imported at the top of the file.)

- [ ] **Step 2: Add the table to the strict table-set test and run it (RED)**

In `tests/test_db.py`, find `test_init_db_creates_all_tables` (~line 32) and add `"workflow_dependencies"` to the asserted set (keep alphabetical-ish grouping near `workflows`/`workflow_versions`).

Run: `./.venv/Scripts/python.exe -m pytest tests/test_db.py::test_init_db_creates_all_tables -q`
Expected: PASS (the model registers the table with `create_all`; this confirms the model is wired). If it FAILS with the table missing, the model wasn't picked up — fix the model.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/d4e6b2c9f1a7_workflow_dependencies.py`:

```python
"""workflow_dependencies table + backfill from current versions

Revision ID: d4e6b2c9f1a7
Revises: c3f5a1b8e2d4
Create Date: 2026-07-25 00:00:00.000000

Typed skill/KB dependency records (P1-04). Guarded/idempotent: db_session runs
create_all at import before upgrade, so create the table only when absent. The
Python backfill materializes dep rows for each workflow's CURRENT version by
parsing its config and resolving skill/KB names to record ids exactly as the
runtime does (an org skill shadows a same-named platform built-in; KBs are
org-scoped). It skips any version that already has rows, so a re-run is a no-op.
Resolving the id (not just the name) keeps the rewired delete guard non-regressing
for pre-migration deployed workflows.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e6b2c9f1a7"
down_revision: Union[str, Sequence[str], None] = "c3f5a1b8e2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _resolve_skill_id(bind, name, org_id):
    if org_id is not None:
        row = bind.execute(
            sa.text("SELECT id FROM skills WHERE name = :n AND org_id = :o"),
            {"n": name, "o": org_id},
        ).first()
        if row is not None:
            return row[0]
    row = bind.execute(
        sa.text("SELECT id FROM skills WHERE name = :n AND org_id IS NULL"),
        {"n": name},
    ).first()
    return row[0] if row is not None else None


def _resolve_kb_id(bind, name, org_id):
    if org_id is None:
        row = bind.execute(
            sa.text("SELECT id FROM knowledge_bases WHERE name = :n AND org_id IS NULL"),
            {"n": name},
        ).first()
    else:
        row = bind.execute(
            sa.text("SELECT id FROM knowledge_bases WHERE name = :n AND org_id = :o"),
            {"n": name, "o": org_id},
        ).first()
    return row[0] if row is not None else None


def _names(config):
    """(skill names, tool names) from a config dict, defensively."""
    skills, tools = set(), set()
    if not isinstance(config, dict):
        return skills, tools
    agents = config.get("agents")
    if not isinstance(agents, list):
        return skills, tools
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for field, sink in (("skills", skills), ("tools", tools)):
            try:
                sink.update(agent.get(field) or [])
            except TypeError:
                continue
    return skills, tools


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "workflow_dependencies" not in tables:
        op.create_table(
            "workflow_dependencies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workflow_version_id", sa.Integer(),
                      sa.ForeignKey("workflow_versions.id"), nullable=False),
            sa.Column("resource_kind", sa.String(), nullable=False),
            sa.Column("resource_name", sa.String(), nullable=False),
            sa.Column("resource_id", sa.Integer(), nullable=True),
            sa.UniqueConstraint(
                "workflow_version_id", "resource_kind", "resource_name",
                name="uq_workflow_dependencies_version_kind_name",
            ),
        )

    # Backfill dep rows for each workflow's current version. Idempotent: skip any
    # version that already has rows.
    have = {
        r[0] for r in bind.execute(
            sa.text("SELECT DISTINCT workflow_version_id FROM workflow_dependencies")
        )
    }
    rows = bind.execute(sa.text(
        "SELECT id, org_id, config, current_version_id FROM workflows "
        "WHERE current_version_id IS NOT NULL"
    )).fetchall()
    insert = sa.text(
        "INSERT INTO workflow_dependencies "
        "(workflow_version_id, resource_kind, resource_name, resource_id) "
        "VALUES (:v, :k, :n, :rid)"
    )
    for _wf_id, org_id, config, ver_id in rows:
        if ver_id in have:
            continue
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                continue
        skills, tools = _names(config)
        for name in sorted(skills):
            bind.execute(insert, {"v": ver_id, "k": "skill", "n": name,
                                  "rid": _resolve_skill_id(bind, name, org_id)})
        for name in sorted(tools):
            kid = _resolve_kb_id(bind, name, org_id)
            if kid is not None:
                bind.execute(insert, {"v": ver_id, "k": "knowledge_base", "n": name,
                                      "rid": kid})


def downgrade() -> None:
    bind = op.get_bind()
    if "workflow_dependencies" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("workflow_dependencies")
```

Note: SQLite JSON columns come back from raw SQL as TEXT strings, hence the `json.loads` guard.

- [ ] **Step 4: Add the table to `_EXPECTED_HEAD_TABLES` and write the backfill test (RED)**

In `tests/test_migrations.py`: add `"workflow_dependencies"` to the `_EXPECTED_HEAD_TABLES` set (~line 40). Add a prior-revision constant and a test near the other backfill tests:

```python
_PRE_DEPS = "c3f5a1b8e2d4"  # revision before workflow_dependencies


def test_workflow_dependencies_backfill_resolves_current_version(tmp_path, monkeypatch):
    db_path = tmp_path / "wf_deps.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_DEPS)  # workflows + workflow_versions exist; no deps table

    engine = make_engine(db_path)
    config_json = (
        '{"name": "wf", "agents": [{"name": "a", '
        '"skills": ["greet"], "tools": ["kb1", "http_get"]}]}'
    )
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO skills (id, name, org_id, config, created_at, updated_at) "
            "VALUES (3, 'greet', 5, '{}', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(sa.text(
            "INSERT INTO knowledge_bases (id, name, org_id, config, created_at, updated_at) "
            "VALUES (4, 'kb1', 5, '{}', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflows (id, name, org_id, config, status, created_at, updated_at, current_version_id) "
            "VALUES (7, 'wf', 5, :cfg, 'deployed', '2026-01-01', '2026-01-01', 11)"
        ), {"cfg": config_json})
        conn.execute(sa.text(
            "INSERT INTO workflow_versions (id, workflow_id, version_number, config, created_by, created_at) "
            "VALUES (11, 7, 1, :cfg, NULL, '2026-01-01')"
        ), {"cfg": config_json})
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    with engine.begin() as conn:
        deps = set(conn.execute(sa.text(
            "SELECT workflow_version_id, resource_kind, resource_name, resource_id "
            "FROM workflow_dependencies"
        )).fetchall())
    engine.dispose()
    assert deps == {
        (11, "skill", "greet", 3),
        (11, "knowledge_base", "kb1", 4),
    }  # http_get is a built-in tool, not a standalone KB -> no row

    # Idempotent: re-running upgrade head adds nothing.
    command.upgrade(cfg, "head")
    engine = make_engine(db_path)
    with engine.begin() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM workflow_dependencies")).scalar()
    engine.dispose()
    assert count == 2
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py::test_workflow_dependencies_backfill_resolves_current_version -q`
Expected: PASS (table created, backfill resolves ids, idempotent). If the migration head isn't discovered, confirm the revision id and `down_revision` chain.

- [ ] **Step 5: Run the migration + db test files**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py tests/test_db.py -q`
Expected: PASS (including the existing `create_all -> upgrade head` idempotency tests, now covering the new table).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/db/models.py alembic/versions/d4e6b2c9f1a7_workflow_dependencies.py tests/test_db.py tests/test_migrations.py
git commit -m "feat(db): workflow_dependencies table + guarded backfill (P1-04)"
```

---

### Task 2: `record_version_dependencies` + wire into publish + unit tests

**Files:**
- Create: `ui/backend/db/dependencies.py`
- Modify: `ui/backend/db/workflows.py` (call the recorder in `publish_workflow_version`)
- Create: `tests/test_dependencies.py`

**Interfaces:**
- Consumes: `WorkflowDependency` (Task 1); `publish_workflow_version(db, *, org_id, name, config, workflow_id=None, created_by=None) -> tuple[WorkflowRecord, WorkflowVersion]` (existing, `ui/backend/db/workflows.py`).
- Produces:
  - `record_version_dependencies(db, *, version_id: int, org_id: Optional[int], raw: dict) -> None` — inserts dep rows; does NOT commit.
  - `workflows_referencing(db, *, kind: str, resource_id: int) -> list[str]` — sorted names of deployed workflows whose current version depends on that resource.

- [ ] **Step 1: Write the new module**

Create `ui/backend/db/dependencies.py`:

```python
"""Typed skill/KB dependency records for published workflow versions (P1-04).

`record_version_dependencies` materializes, at deploy, one row per skill/standalone
KB a version depends on; `workflows_referencing` answers the reverse "which deployed
teams' current version depend on this resource?" that the skill/KB delete guard uses.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import (
    KnowledgeBaseRecord,
    SkillRecord,
    WorkflowDependency,
    WorkflowRecord,
    WorkflowVersion,
)


def _referenced_names(raw: Any) -> tuple[set[str], set[str]]:
    """(skill names, tool names) referenced by raw["agents"], defensively --
    mirrors the loader's `list(refs or [])` normalization; skips malformed rows."""
    skills: set[str] = set()
    tools: set[str] = set()
    agents = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(agents, list):
        return skills, tools
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for field, sink in (("skills", skills), ("tools", tools)):
            try:
                sink.update(agent.get(field) or [])
            except TypeError:
                continue
    return skills, tools


def record_version_dependencies(
    db: Session, *, version_id: int, org_id: Optional[int], raw: dict[str, Any]
) -> None:
    """Insert one WorkflowDependency row per skill and per standalone KB `raw`
    references. Resolves resource_id the way the loader resolves names: an org
    skill shadows a same-named platform built-in (org_id IS NULL); KBs are
    org-scoped. A tool name that is not a standalone KB (built-in tool, email
    tool, or inline KB) is not a KB dependency and is skipped. Does NOT commit --
    the caller owns the transaction."""
    skill_names, tool_names = _referenced_names(raw)

    skill_ids: dict[str, int] = {}
    if skill_names:
        rows = (
            db.query(SkillRecord.name, SkillRecord.id, SkillRecord.org_id)
            .filter(
                SkillRecord.name.in_(skill_names),
                or_(SkillRecord.org_id == org_id, SkillRecord.org_id.is_(None)),
            )
            .all()
        )
        # Platform built-ins (org_id IS NULL, sort key False) first so an org's
        # own row (sort key True) overwrites on a name clash.
        for name, sid, _row_org in sorted(rows, key=lambda r: r[2] is not None):
            skill_ids[name] = sid

    kb_ids: dict[str, int] = {}
    if tool_names:
        for name, kid in db.query(KnowledgeBaseRecord.name, KnowledgeBaseRecord.id).filter(
            KnowledgeBaseRecord.name.in_(tool_names),
            KnowledgeBaseRecord.org_id == org_id,
        ):
            kb_ids[name] = kid

    for name in sorted(skill_names):
        db.add(WorkflowDependency(
            workflow_version_id=version_id, resource_kind="skill",
            resource_name=name, resource_id=skill_ids.get(name),
        ))
    for name in sorted(tool_names):
        if name in kb_ids:
            db.add(WorkflowDependency(
                workflow_version_id=version_id, resource_kind="knowledge_base",
                resource_name=name, resource_id=kb_ids[name],
            ))


def workflows_referencing(db: Session, *, kind: str, resource_id: int) -> list[str]:
    """Names of `deployed` workflows whose CURRENT version depends on the resource
    with id `resource_id` (kind = "skill" | "knowledge_base"). Matches by stable
    id, so a platform built-in skill's referencers across every org are found
    without an all-orgs name scan; the current_version_id join reproduces
    "current deployed config only"."""
    q = (
        db.query(WorkflowRecord.name)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRecord.current_version_id)
        .join(WorkflowDependency, WorkflowDependency.workflow_version_id == WorkflowVersion.id)
        .filter(
            WorkflowRecord.status == "deployed",
            WorkflowDependency.resource_kind == kind,
            WorkflowDependency.resource_id == resource_id,
        )
    )
    return sorted({name for (name,) in q})
```

- [ ] **Step 2: Wire the recorder into `publish_workflow_version`**

In `ui/backend/db/workflows.py`: add the import near the existing model import (top of file):

```python
from .dependencies import record_version_dependencies
```

Then in `publish_workflow_version`, the tail currently reads:

```python
    db.add(version)
    db.flush()  # need version.id
    record.current_version_id = version.id
    return record, version
```

Change it to:

```python
    db.add(version)
    db.flush()  # need version.id
    record.current_version_id = version.id
    record_version_dependencies(db, version_id=version.id, org_id=org_id, raw=config)
    return record, version
```

- [ ] **Step 3: Write the unit tests (RED first)**

Create `tests/test_dependencies.py`:

```python
import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine
from ui.backend.db.database import session_factory
from ui.backend.db.dependencies import record_version_dependencies, workflows_referencing
from ui.backend.db.models import KnowledgeBaseRecord, SkillRecord, WorkflowDependency
from ui.backend.db.workflows import publish_workflow_version


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    session = session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _config(agents):
    return {"name": "wf", "agents": agents}


def test_records_skill_and_standalone_kb_deps(db):
    db.add(SkillRecord(id=1, name="greet", org_id=7, config={}))
    db.add(KnowledgeBaseRecord(id=2, name="returns_policy", org_id=7, config={}))
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["greet"],
                         "tools": ["returns_policy", "http_get"]}]),
    )
    db.commit()
    deps = {
        (d.resource_kind, d.resource_name, d.resource_id)
        for d in db.query(WorkflowDependency).filter_by(workflow_version_id=version.id)
    }
    # http_get is a built-in tool, not a standalone KB -> no row.
    assert deps == {("skill", "greet", 1), ("knowledge_base", "returns_policy", 2)}


def test_org_skill_shadows_platform_builtin(db):
    db.add(SkillRecord(id=1, name="triage", org_id=None, config={}))  # platform built-in
    db.add(SkillRecord(id=2, name="triage", org_id=7, config={}))     # org override
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).one()
    assert (row.resource_kind, row.resource_id) == ("skill", 2)  # org row wins


def test_platform_builtin_skill_resolves_for_org_workflow(db):
    db.add(SkillRecord(id=5, name="triage", org_id=None, config={}))
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).one()
    assert row.resource_id == 5


def test_inline_kb_is_not_a_standalone_dependency(db):
    config = {
        "name": "wf",
        "knowledge_bases": [{"name": "faq", "path": "./faq"}],
        "agents": [{"name": "a", "skills": [], "tools": ["faq"]}],
    }
    _rec, version = publish_workflow_version(db, org_id=7, name="wf", config=config)
    db.commit()
    assert db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).count() == 0


def test_workflows_referencing_matches_current_version_only(db):
    db.add(SkillRecord(id=1, name="greet", org_id=7, config={}))
    db.commit()
    publish_workflow_version(
        db, org_id=7, name="team-a",
        config=_config([{"name": "a", "skills": ["greet"], "tools": []}]),
    )
    db.commit()
    assert workflows_referencing(db, kind="skill", resource_id=1) == ["team-a"]
    assert workflows_referencing(db, kind="skill", resource_id=999) == []
```

If `session_factory` isn't exported from `ui.backend.db.database`, check `ui/backend/db/database.py` for the exact factory name and use it (the module provides `make_engine`, `init_db`, `session_factory` per `ui/backend/db/CLAUDE.md`).

- [ ] **Step 4: Run the unit tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dependencies.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the versioning tests to confirm no regression at the publish site**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_workflow_versions.py tests/test_builder_api.py -q`
Expected: PASS (publish now also records deps; existing publish/version tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/db/dependencies.py ui/backend/db/workflows.py tests/test_dependencies.py
git commit -m "feat(db): record typed skill/KB deps at deploy (P1-04)"
```

---

### Task 3: Rewire delete guard + head-delete cascade + remove dead scanner

**Files:**
- Modify: `ui/backend/crud.py` (imports; `delete_item` ~line 362-364; delete `_deployed_workflows_referencing` ~line 226-258; `delete_workflow_config` ~line 668)
- Modify: `tests/test_crud_api.py` (existing P1-07 tests stay green; add cross-org + drop-then-deletable + cascade tests)

**Interfaces:**
- Consumes: `workflows_referencing(db, *, kind, resource_id)` and `WorkflowDependency` (Task 2/1).

- [ ] **Step 1: Update imports in `crud.py`**

Add `WorkflowDependency` to the `.db.models` import block (currently imports `BuilderSession, KnowledgeBaseRecord, Organization, Run, SkillRecord, WorkflowRecord, WorkflowVersion` at ~line 46-54):

```python
from .db.models import (
    BuilderSession,
    KnowledgeBaseRecord,
    Organization,
    Run,
    SkillRecord,
    WorkflowDependency,
    WorkflowRecord,
    WorkflowVersion,
)
```

Add, next to `from .db.workflows import publish_workflow_version` (~line 56):

```python
from .db.dependencies import workflows_referencing
```

- [ ] **Step 2: Rewire the skill/KB delete guard**

In `delete_item` (~line 362-373), the block currently reads:

```python
            if name in ("skills", "knowledge_bases"):
                kind = "skill" if name == "skills" else "knowledge_base"
                used_by = _deployed_workflows_referencing(db, org_id, kind, item_name)
                if used_by:
```

Change the `used_by` line to query typed rows by the item's stable id:

```python
            if name in ("skills", "knowledge_bases"):
                kind = "skill" if name == "skills" else "knowledge_base"
                used_by = workflows_referencing(db, kind=kind, resource_id=item.id)
                if used_by:
```

(`item` is the resolved record fetched at ~line 352; `item.id` is its stable PK. The 409 message and surrounding lock are unchanged.)

- [ ] **Step 3: Delete the now-orphaned scanner**

Remove the entire `_deployed_workflows_referencing` function (~lines 226-258). Confirm no other caller:

Run: `./.venv/Scripts/python.exe -c "import ui.backend.crud"` (imports cleanly — no NameError)
Then grep: `grep -rn _deployed_workflows_referencing ui tests` — expect **no** remaining references in `ui/`. If a test references it directly, that test is rewritten in Step 5.

- [ ] **Step 4: Cascade dep rows on workflow-head hard-delete**

In `delete_workflow_config` (~line 668), the cleanup currently reads:

```python
        db.query(BuilderSession).filter_by(workflow_id=item.id).update(
            {BuilderSession.workflow_id: None}
        )
        db.query(WorkflowVersion).filter_by(workflow_id=item.id).delete()
        db.delete(item)
        db.commit()
```

Insert a dep-row cleanup before the `WorkflowVersion` delete (FK enforcement is off, so no DB cascade removes them):

```python
        db.query(BuilderSession).filter_by(workflow_id=item.id).update(
            {BuilderSession.workflow_id: None}
        )
        version_ids = [
            v for (v,) in db.query(WorkflowVersion.id).filter_by(workflow_id=item.id)
        ]
        if version_ids:
            db.query(WorkflowDependency).filter(
                WorkflowDependency.workflow_version_id.in_(version_ids)
            ).delete(synchronize_session=False)
        db.query(WorkflowVersion).filter_by(workflow_id=item.id).delete()
        db.delete(item)
        db.commit()
```

- [ ] **Step 5: Confirm existing P1-07 delete tests still pass, then add new tests**

First run the existing guard tests to prove non-regression:

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k "delete and (skill or kb or knowledge)" -q`
Expected: PASS (the existing `test_delete_skill_referenced_by_deployed_workflow_is_409`, `test_delete_kb_referenced_by_deployed_workflow_is_409`, `test_delete_unreferenced_skill_still_204` now flow through typed rows). If any references the removed `_deployed_workflows_referencing` directly, update it to assert behavior via the API instead.

Then add these tests to `tests/test_crud_api.py`, **modeled on the existing `test_delete_skill_referenced_by_deployed_workflow_is_409`** (reuse its exact setup helpers for creating an org/user, deploying a workflow via `PUT /api/config/workflows/{name}`, and creating a skill/KB — do not invent new harness):

1. `test_delete_platform_builtin_skill_referenced_by_org_workflow_is_409` — deploy an org workflow whose agent references a **platform built-in** skill (org omitted at skill creation, so `org_id IS NULL`), then `DELETE /api/config/skills/{name}` with no `?org=` (platform tier). Assert `409` and the org's team name in the detail. (Proves cross-org matching via `resource_id`.)
2. `test_skill_dropped_from_current_version_becomes_deletable` — deploy a workflow referencing skill `s`, then redeploy the same workflow (same name) with an agent config that **no longer** references `s`, then `DELETE` skill `s`. Assert `204`. (Proves current-config semantics: the superseded version still has a dep row, but the guard only reads the current version.)
3. `test_delete_workflow_head_removes_dependency_rows` — deploy a never-run workflow referencing a skill, capture its workflow id, `DELETE /api/config/workflows/{name}`, then assert (via a DB session in the test, as sibling tests do) that no `WorkflowDependency` rows remain for that workflow's versions. (Proves the cascade.)

- [ ] **Step 6: Run the crud test file**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ui/backend/crud.py tests/test_crud_api.py
git commit -m "feat(crud): typed-row skill/KB delete guard + head-delete dep cascade (P1-04)"
```

---

### Task 4: Docs + full-suite verification

**Files:**
- Modify: `ui/backend/db/CLAUDE.md`, `ui/backend/CLAUDE.md`, `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`, `docs/STATUS.md`

**Interfaces:** none (documentation + verification only).

- [ ] **Step 1: Update `ui/backend/db/CLAUDE.md`**

Add a `workflow_dependencies` bullet after the `workflow_versions` bullet:
- `workflow_dependencies` — one typed row per (published version, skill|standalone-KB) it depends on (`WorkflowDependency`; `workflow_version_id` FK, `resource_kind`, `resource_name`, `resource_id` = the resolved `skills`/`knowledge_bases` id, nullable; `(workflow_version_id, resource_kind, resource_name)` unique). Written once at deploy in `db/workflows.py::publish_workflow_version` via `db/dependencies.py::record_version_dependencies` (resolves names exactly as the loader: org skill shadows platform built-in; KBs org-scoped; a built-in tool / email tool / inline KB is not a KB dep). The skill/KB `DELETE` guard now queries these rows by `resource_id` for the **current** version (`workflows_referencing`) instead of scanning JSON — non-regressing, and the stable id makes the platform-built-in-skill cross-org case fall out without an all-orgs scan. Migration `d4e6b2c9f1a7` creates the table and backfills each workflow's current version. Model/tool deps and content/version pinning are still deferred (P1-04 recorded only skills+KBs; P1-05 for content pinning).

Also update the `workflow_versions` bullet's tail sentence "a fully-resolved dependency snapshot is deferred to P1-04" → note that skill/KB dependency records now exist (`workflow_dependencies`), with model resolution and content pinning still deferred (P1-04 partial / P1-05).

- [ ] **Step 2: Update `ui/backend/CLAUDE.md`**

In the `crud.py` section, update the P1-07/P1-08 paragraph: the skill/KB delete guard no longer scans deployed workflows' JSON — it queries `db/dependencies.py::workflows_referencing(db, kind=, resource_id=item.id)` against typed `workflow_dependencies` rows for each workflow's current version (populated at deploy by `record_version_dependencies`). Replace the "Known limitations deferred to P1-04 (typed dependency records): ... raw-name matching that can over-block" sentence: raw-name matching is now resolved (typed rows keyed by stable `resource_id`); still deferred — the delete/deploy TOCTOU is serialized via `component_mutation_lock` (not DB-enforced), model/built-in-tool dependency rows aren't recorded (no consumer), and skill/KB **content** pinning to freeze behavior is P1-05.

- [ ] **Step 3: Update `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`**

Move P1-04 into the "Implemented this pass" narrative: add a paragraph (after the versioning-keystone one) describing the `workflow_dependencies` table, deploy-time population, the rewired resource_id delete guard, current-config scope, skills+KBs-only scope, and the deferred pieces (model/tool deps, content pinning = P1-05). Update the "Everything else" residual list: remove P1-04, change "The remaining 21 findings" → "The remaining 20 findings", and update the Phase-1 list `P1-04, P1-05, P1-10, P1-12, P1-15, P1-18` → `P1-05, P1-10, P1-12, P1-15, P1-18`.

- [ ] **Step 4: Update `docs/STATUS.md`**

Add P1-04 (typed dependency records: skills + KBs) to the done/implemented section, mirroring how the versioning keystone was recorded.

- [ ] **Step 5: Full-suite verification**

Run: `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (all tests). Record the pass count.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/db/CLAUDE.md ui/backend/CLAUDE.md docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md docs/STATUS.md
git commit -m "docs: record P1-04 typed dependency records"
```

---

## Self-Review

**Spec coverage:**
- New table `workflow_dependencies` → Task 1. ✓
- Populate at deploy via `record_version_dependencies` in `publish_workflow_version` → Task 2. ✓
- Rewire delete guard to `workflows_referencing` by `resource_id`; remove dead scanner → Task 3. ✓
- Head-delete dep cascade → Task 3 Step 4. ✓
- Guarded/idempotent migration + Python backfill resolving ids → Task 1 Step 3. ✓
- Skills+KBs only; inline KB / built-in tool / email tool excluded → Task 2 tests. ✓
- Current-config guard scope; platform-skill cross-org via id; drop-then-deletable → Task 3 Step 5. ✓
- Docs (db/CLAUDE, CLAUDE, TRIAGE, STATUS) → Task 4. ✓
- Verification (unit, guard, cascade, migration, full suite) → covered across tasks. ✓

**Type consistency:** `record_version_dependencies(db, *, version_id, org_id, raw)` and `workflows_referencing(db, *, kind, resource_id)` used identically in Tasks 2 and 3. `WorkflowDependency` column names (`workflow_version_id`, `resource_kind`, `resource_name`, `resource_id`) identical in model, migration, recorder, guard, and tests. Migration head `d4e6b2c9f1a7` / down_revision `c3f5a1b8e2d4` consistent.

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step carries complete code.
