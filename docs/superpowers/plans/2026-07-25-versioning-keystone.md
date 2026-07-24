# Versioning Keystone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a deployed team a stable identity with an append-only immutable version history, make deploy publish a new version instead of overwriting config in place, and stamp each production Run with the exact version it executed.

**Architecture:** Repurpose `WorkflowRecord` as the stable team head (add `current_version_id`) and add an immutable `workflow_versions` child table. A new `ui/backend/db/workflows.py::publish_workflow_version` helper is the single deploy primitive both deploy points call. `WorkflowRecord.config` stays a mirror of the current version so every existing reader is untouched. Runs gain `workflow_version_id`, stamped at run start.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic (guarded/idempotent migrations with `op.batch_alter_table` for SQLite), pytest with `FakeListChatModel`/`fake:` specs.

## Global Constraints

- Run everything through the project venv: `./.venv/Scripts/python.exe` (Windows).
- Full suite MUST run against a scratch DB (the dev DB trips the import-time secrets guard):
  `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q`
- Single-file focused test runs may use the default DB.
- Migrations are guarded/idempotent: `db_session` runs `create_all` at import BEFORE `alembic upgrade`, so every migration step MUST inspect-guard (create table only if absent; add column only via `_has_column`; use `op.batch_alter_table` for SQLite alters). New migration `down_revision = "b1d7e4f2a9c8"` (current head).
- Preserve deploy atomicity: `deploy_session` performs NO explicit `db.commit()` — its `WorkflowRecord`/version writes and the `update_session` write share the single commit inside `update_session` (P1-14). Do NOT add a commit there. `crud.upsert_workflow_config` keeps its one explicit `db.commit()`.
- Version-number allocation runs inside the existing process-wide `component_mutation_lock` at both deploy points; the `(workflow_id, version_number)` unique constraint is the backstop.
- `WorkflowRecord.config` remains the current-version mirror — no reader changes. External addressing stays name-based. **No frontend changes.**
- OUT OF SCOPE (do not build): freezing standalone Skill/KB/model resolution (P1-04); version-history/rollback UI; rollback execution; SQLite FK enforcement (new FKs are advisory like the rest); retro-linking historical runs/sessions.

---

### Task 1: Schema — `WorkflowVersion` model, new columns, migration

**Files:**
- Modify: `ui/backend/db/models.py` (add `WorkflowVersion`; add `current_version_id` to `WorkflowRecord` ~line 130; `workflow_id` to `BuilderSession` ~line 222; `workflow_version_id` to `Run` ~line 245)
- Create: `alembic/versions/c3f5a1b8e2d4_workflow_versions.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: `WorkflowVersion` (columns `id`, `workflow_id`, `version_number`, `config`, `created_by`, `created_at`); `WorkflowRecord.current_version_id: Optional[int]`; `BuilderSession.workflow_id: Optional[int]`; `Run.workflow_version_id: Optional[int]`. Migration revision `c3f5a1b8e2d4`.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/test_migrations.py`. NOTE: `alembic/env.py` overrides
`sqlalchemy.url` from `BESTTEAM_DB_PATH`, so the test MUST use the module's
existing `_alembic_config(db_path, monkeypatch)` helper (it sets that env var)
and build state by upgrading to the prior revision — do NOT pass a url via
`cfg.set_main_option`, and do NOT use `create_all` + a raw `Config`:

```python
# Revision just before the workflow_versions migration.
_PRE_VERSIONS = "b1d7e4f2a9c8"


def test_workflow_versions_backfill_creates_one_v1_per_workflow(tmp_path, monkeypatch):
    """Upgrading past the versions migration gives each existing workflow
    exactly one immutable v1 with its current-version pointer set."""
    db_path = tmp_path / "wf_versions.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_VERSIONS)  # workflows exists, no workflow_versions yet

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id, name, org_id, config, status, created_at, updated_at) "
            "VALUES (7, 'wf', NULL, '{\"name\": \"wf\"}', 'deployed', '2026-01-01', '2026-01-01')"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT workflow_id, version_number, config FROM workflow_versions"
            )).all()
            assert rows == [(7, 1, '{"name": "wf"}')]
            ptr = conn.execute(sa.text(
                "SELECT current_version_id FROM workflows WHERE id = 7"
            )).scalar()
            vid = conn.execute(sa.text(
                "SELECT id FROM workflow_versions WHERE workflow_id = 7"
            )).scalar()
            assert ptr == vid
            count = conn.execute(sa.text("SELECT COUNT(*) FROM workflow_versions")).scalar()
            assert count == 1
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py::test_workflow_versions_backfill_creates_one_v1_per_workflow -v`
Expected: FAIL (`no such table: workflow_versions` — the model/migration don't exist yet).

- [ ] **Step 3: Add the models**

In `ui/backend/db/models.py`, add `current_version_id` to `WorkflowRecord` (after `updated_at`, ~line 130):

```python
    current_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflow_versions.id"), nullable=True
    )
```

Add the new class immediately after `WorkflowRecord` (before `OrgEmailCredential`):

```python
class WorkflowVersion(Base):
    """An immutable published snapshot of a WorkflowRecord's config.

    Deploy appends one row (never updates an existing one) and points the
    parent WorkflowRecord.current_version_id at it; a Run references the exact
    version it executed. This freezes the inline config blob only -- standalone
    Skills/KBs/models are still resolved by name at load (P1-04/P1-05)."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "version_number",
            name="uq_workflow_versions_workflow_id_version_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"))
    version_number: Mapped[int]
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

Add `workflow_id` to `BuilderSession` (after `feedback_history`, ~line 220):

```python
    # The stable WorkflowRecord (team head) this session deploys to. Set on
    # first deploy; a redeploy publishes a new version under the same head, so
    # two sessions that deploy the same name converge on one head (P1-02).
    workflow_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflows.id"), nullable=True
    )
```

Add `workflow_version_id` to `Run` (after `username`, ~line 245):

```python
    # The exact immutable version this run executed (P1-03/P1-15). NULL for
    # sandbox test runs (they run the session spec, not a published version)
    # and for pre-migration rows.
    workflow_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflow_versions.id"), nullable=True
    )
```

- [ ] **Step 4: Write the migration**

Create `alembic/versions/c3f5a1b8e2d4_workflow_versions.py`:

```python
"""workflow_versions table + current/version pointers + backfill v1

Revision ID: c3f5a1b8e2d4
Revises: b1d7e4f2a9c8
Create Date: 2026-07-25 00:00:00.000000

Introduces immutable workflow versions (P1-01/02/03). Guarded/idempotent:
db_session runs create_all at import before upgrade, so a fresh DB already has
the table/columns -- create/add only when absent. Backfill gives every existing
workflow exactly one v1 (config copied verbatim) and sets the pointer; the
`current_version_id IS NULL` filter makes a re-run a no-op. No JSON-parsing
backfill for builder_sessions.workflow_id / runs.workflow_version_id -- those
forward-populate on the next deploy / run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f5a1b8e2d4"
down_revision: Union[str, Sequence[str], None] = "b1d7e4f2a9c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "workflow_versions" not in tables:
        op.create_table(
            "workflow_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workflow_id", sa.Integer(), sa.ForeignKey("workflows.id"), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "workflow_id", "version_number",
                name="uq_workflow_versions_workflow_id_version_number",
            ),
        )

    if not _has_column(bind, "workflows", "current_version_id"):
        with op.batch_alter_table("workflows") as batch:
            batch.add_column(sa.Column("current_version_id", sa.Integer(), nullable=True))
    if not _has_column(bind, "builder_sessions", "workflow_id"):
        with op.batch_alter_table("builder_sessions") as batch:
            batch.add_column(sa.Column("workflow_id", sa.Integer(), nullable=True))
    if not _has_column(bind, "runs", "workflow_version_id"):
        with op.batch_alter_table("runs") as batch:
            batch.add_column(sa.Column("workflow_version_id", sa.Integer(), nullable=True))

    # Backfill: one v1 per workflow lacking a pointer, then set the pointer.
    op.execute(
        "INSERT INTO workflow_versions (workflow_id, version_number, config, created_by, created_at) "
        "SELECT id, 1, config, NULL, created_at FROM workflows WHERE current_version_id IS NULL"
    )
    op.execute(
        "UPDATE workflows SET current_version_id = ("
        "  SELECT wv.id FROM workflow_versions wv "
        "  WHERE wv.workflow_id = workflows.id AND wv.version_number = 1"
        ") WHERE current_version_id IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "runs", "workflow_version_id"):
        with op.batch_alter_table("runs") as batch:
            batch.drop_column("workflow_version_id")
    if _has_column(bind, "builder_sessions", "workflow_id"):
        with op.batch_alter_table("builder_sessions") as batch:
            batch.drop_column("workflow_id")
    if _has_column(bind, "workflows", "current_version_id"):
        with op.batch_alter_table("workflows") as batch:
            batch.drop_column("current_version_id")
    if "workflow_versions" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("workflow_versions")
```

- [ ] **Step 5: Run the test to confirm it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py::test_workflow_versions_backfill_creates_one_v1_per_workflow -v`
Expected: PASS.

- [ ] **Step 6: Run the full migrations suite (guard against regressions)**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py -q`
Expected: all pass (existing `create_all → upgrade head` idempotency test still green).

- [ ] **Step 7: Commit**

```bash
git add ui/backend/db/models.py alembic/versions/c3f5a1b8e2d4_workflow_versions.py tests/test_migrations.py
git commit -m "feat(db): workflow_versions table + version/team pointers + backfill"
```

---

### Task 2: `publish_workflow_version` + `current_version_id` helpers

**Files:**
- Create: `ui/backend/db/workflows.py`
- Test: `tests/test_workflow_versions.py`

**Interfaces:**
- Consumes: `WorkflowRecord`, `WorkflowVersion` from Task 1.
- Produces:
  - `publish_workflow_version(db, *, org_id, name, config, workflow_id=None, created_by=None) -> tuple[WorkflowRecord, WorkflowVersion]` — flushes (no commit); caller commits.
  - `current_version_id(db, org_id, name) -> Optional[int]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_versions.py`:

```python
from ui.backend.db.database import make_engine, init_db, session_factory
from ui.backend.db.models import WorkflowRecord, WorkflowVersion
from ui.backend.db.workflows import publish_workflow_version, current_version_id


def _db():
    engine = make_engine(":memory:")
    init_db(engine)
    return session_factory(engine)()


def test_first_deploy_creates_v1_and_sets_pointer():
    db = _db()
    record, version = publish_workflow_version(db, org_id=1, name="wf", config={"name": "wf", "v": 1})
    db.commit()
    assert version.version_number == 1
    assert record.current_version_id == version.id
    assert record.config == {"name": "wf", "v": 1}


def test_redeploy_appends_v2_moves_pointer_and_keeps_v1_immutable():
    db = _db()
    record, v1 = publish_workflow_version(db, org_id=1, name="wf", config={"v": 1})
    db.commit()
    v1_id = v1.id
    record2, v2 = publish_workflow_version(db, org_id=1, name="wf", config={"v": 2})
    db.commit()
    assert record2.id == record.id                 # same team head
    assert v2.version_number == 2
    assert record2.current_version_id == v2.id     # pointer moved
    frozen = db.get(WorkflowVersion, v1_id)
    assert frozen.config == {"v": 1}               # v1 untouched
    assert record2.config == {"v": 2}              # mirror is current


def test_redeploy_by_workflow_id_renames_head_in_place():
    db = _db()
    record, _ = publish_workflow_version(db, org_id=1, name="old", config={"v": 1})
    db.commit()
    head_id = record.id
    record2, v2 = publish_workflow_version(
        db, org_id=1, name="new", config={"v": 2}, workflow_id=head_id
    )
    db.commit()
    assert record2.id == head_id
    assert record2.name == "new"
    assert v2.version_number == 2


def test_stale_workflow_id_falls_back_to_resolve_or_create_by_name():
    db = _db()
    record, v2 = publish_workflow_version(
        db, org_id=1, name="wf", config={"v": 1}, workflow_id=999  # nonexistent
    )
    db.commit()
    assert record.id is not None
    assert record.name == "wf"
    assert v2.version_number == 1


def test_current_version_id_returns_pointer_for_deployed_only():
    db = _db()
    record, version = publish_workflow_version(db, org_id=1, name="wf", config={"v": 1})
    db.commit()
    assert current_version_id(db, 1, "wf") == version.id
    assert current_version_id(db, 1, "absent") is None
```

- [ ] **Step 2: Run to confirm failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_workflow_versions.py -v`
Expected: FAIL (`No module named 'ui.backend.db.workflows'`).

- [ ] **Step 3: Implement the helpers**

Create `ui/backend/db/workflows.py`:

```python
"""Deploy primitive: publish a WorkflowRecord's config as an immutable version.

`WorkflowRecord` is the stable team head (unique `(org_id, name)`);
`workflow_versions` is its append-only history. Deploy appends a version, moves
`current_version_id`, and keeps `config` as a mirror of the current version so
every reader stays name-based and unchanged (P1-01/02/03). Callers hold
`component_mutation_lock` and own the commit."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import WorkflowRecord, WorkflowVersion


def publish_workflow_version(
    db: Session,
    *,
    org_id: Optional[int],
    name: str,
    config: dict[str, Any],
    workflow_id: Optional[int] = None,
    created_by: Optional[str] = None,
) -> tuple[WorkflowRecord, WorkflowVersion]:
    """Publish `config` as the next immutable version of a team head, moving its
    current-version pointer. Returns `(record, version)`; does NOT commit.

    `workflow_id` given and found -> that existing head (rename-safe:
    `record.name = name`). Otherwise resolve-or-create the head by
    `(org_id, name)` -- so a stale session pointer (deleted team) recreates
    cleanly, and two sessions deploying the same name converge on one head."""
    record: Optional[WorkflowRecord] = None
    if workflow_id is not None:
        # Org-scoped: a workflow_id naming another org's record (or a stale one)
        # falls through to resolve-or-create in the caller's own org.
        record = db.query(WorkflowRecord).filter_by(id=workflow_id, org_id=org_id).one_or_none()
    if record is not None:
        record.name = name
        record.config = config
        record.status = "deployed"
    else:
        record = (
            db.query(WorkflowRecord).filter_by(name=name, org_id=org_id).one_or_none()
        )
        if record is None:
            record = WorkflowRecord(name=name, config=config, status="deployed", org_id=org_id)
            db.add(record)
        else:
            record.config = config
            record.status = "deployed"

    db.flush()  # need record.id
    next_number = (
        db.query(func.max(WorkflowVersion.version_number))
        .filter_by(workflow_id=record.id)
        .scalar()
        or 0
    ) + 1
    version = WorkflowVersion(
        workflow_id=record.id,
        version_number=next_number,
        config=config,
        created_by=created_by,
    )
    db.add(version)
    db.flush()  # need version.id
    record.current_version_id = version.id
    return record, version


def current_version_id(db: Session, org_id: Optional[int], name: str) -> Optional[int]:
    """The `current_version_id` of a deployed team by `(org_id, name)`, else None."""
    record = (
        db.query(WorkflowRecord)
        .filter_by(org_id=org_id, name=name, status="deployed")
        .one_or_none()
    )
    return record.current_version_id if record else None
```

- [ ] **Step 4: Run to confirm pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_workflow_versions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/workflows.py tests/test_workflow_versions.py
git commit -m "feat(db): publish_workflow_version + current_version_id helpers"
```

---

### Task 3: Wire the wizard deploy path (P1-02 fix)

**Files:**
- Modify: `ui/backend/db/builder_sessions.py:23-31` (add `workflow_id` to `_UPDATABLE_FIELDS`)
- Modify: `ui/backend/builder.py:467-536` (`deploy_session`: publish + link session; add `user` dep)
- Test: `tests/test_builder_api.py`

**Interfaces:**
- Consumes: `publish_workflow_version` (Task 2); `BuilderSession.workflow_id`, `WorkflowVersion` (Task 1).
- Produces: after a wizard deploy, `session.workflow_id` = the deployed head's id; a `WorkflowVersion` row exists per deploy.

- [ ] **Step 1: Write the failing P1-02 tests**

Add to `tests/test_builder_api.py` (reuse the file's existing helpers for creating an org user + a session with a valid `fake:` specification; follow an existing deploy test for the setup shape):

```python
def test_redeploy_same_session_keeps_head_and_bumps_version(client):
    """One session deployed twice -> same workflow_id, versions 1 then 2."""
    from ui.backend.db.models import WorkflowVersion
    headers = _org_user_headers(client)
    session_id = _make_deployable_session(client, headers, name="Acme")

    client.post(f"/api/builder/sessions/{session_id}/deploy", headers=headers).raise_for_status()
    db = _db_session()
    sess = db.get(BuilderSession, session_id)
    head_id = sess.workflow_id
    assert head_id is not None
    v1 = db.query(WorkflowVersion).filter_by(workflow_id=head_id).count()
    assert v1 == 1

    client.post(f"/api/builder/sessions/{session_id}/deploy", headers=headers).raise_for_status()
    db2 = _db_session()
    sess2 = db2.get(BuilderSession, session_id)
    assert sess2.workflow_id == head_id  # same head
    assert db2.query(WorkflowVersion).filter_by(workflow_id=head_id).count() == 2


def test_two_sessions_same_name_converge_on_one_head_v1_preserved(client):
    """P1-02: two sessions with the same team name deploy to the SAME head;
    the first config survives as v1 (no silent clobber)."""
    from ui.backend.db.models import WorkflowVersion
    headers = _org_user_headers(client)
    s_a = _make_deployable_session(client, headers, name="Dup", marker="A")
    s_b = _make_deployable_session(client, headers, name="Dup", marker="B")

    client.post(f"/api/builder/sessions/{s_a}/deploy", headers=headers).raise_for_status()
    client.post(f"/api/builder/sessions/{s_b}/deploy", headers=headers).raise_for_status()

    db = _db_session()
    head_a = db.get(BuilderSession, s_a).workflow_id
    head_b = db.get(BuilderSession, s_b).workflow_id
    assert head_a == head_b and head_a is not None          # one shared head
    versions = (db.query(WorkflowVersion)
                  .filter_by(workflow_id=head_a)
                  .order_by(WorkflowVersion.version_number).all())
    assert [v.version_number for v in versions] == [1, 2]   # both preserved
```

Notes for the implementer: `_make_deployable_session(client, headers, name=..., marker=...)` should create a session and store a minimal valid `specification_json` whose `name` is `name` (use a `fake:` model so no API quota is needed; the `marker` just makes A/B configs distinguishable). If the test file already has a helper that produces a deployable session, extend it to accept `name`/`marker` rather than duplicating it. `_db_session()` / `_org_user_headers` follow the existing patterns in this test module.

- [ ] **Step 2: Run to confirm failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py::test_two_sessions_same_name_converge_on_one_head_v1_preserved -v`
Expected: FAIL (`session.workflow_id` is None — deploy doesn't link it yet).

- [ ] **Step 3: Make `workflow_id` updatable on a session**

In `ui/backend/db/builder_sessions.py`, add `"workflow_id"` to `_UPDATABLE_FIELDS`:

```python
_UPDATABLE_FIELDS = frozenset(
    {
        "intent_text",
        "as_is_text",
        "requirements_json",
        "specification_json",
        "status",
        "workflow_id",
    }
)
```

- [ ] **Step 4: Rewire `deploy_session` to publish + link**

In `ui/backend/builder.py`, add the import near the other db imports:

```python
from .db.workflows import publish_workflow_version
```

Add a `user` dependency to `deploy_session` so a version can record who deployed it (mirrors `create_test_run`, which already depends on `get_current_user`):

```python
@router.post("/{session_id}/deploy")
def deploy_session(
    session_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
```

Replace the in-place upsert block (currently lines ~527-535) with a publish call that links the session in the SAME `update_session` commit (preserving P1-14):

```python
        record, _version = publish_workflow_version(
            db,
            org_id=org.id,
            name=spec.name,
            config=raw,
            workflow_id=session.workflow_id,
            created_by=user.username,
        )
        session = update_session(
            db, session_id, status="deployed", workflow_id=record.id
        )
    return _session_to_dict(session, db, org.id)
```

(The `WorkflowRecord` query/insert/overwrite that was here is now inside `publish_workflow_version`. Do not add a `db.commit()` — `update_session` commits both writes together.)

- [ ] **Step 5: Run the P1-02 tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py::test_redeploy_same_session_keeps_head_and_bumps_version tests/test_builder_api.py::test_two_sessions_same_name_converge_on_one_head_v1_preserved -v`
Expected: PASS.

- [ ] **Step 6: Run the full builder-api suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py -q`
Expected: all pass (existing deploy/atomicity tests still green — including `test_deploy_is_atomic_across_workflow_and_session_updates`).

- [ ] **Step 7: Commit**

```bash
git add ui/backend/db/builder_sessions.py ui/backend/builder.py tests/test_builder_api.py
git commit -m "feat(builder): deploy publishes a version and links session to its team head (P1-02)"
```

---

### Task 4: Wire the admin CRUD deploy path

**Files:**
- Modify: `ui/backend/crud.py:617-624` (`upsert_workflow_config`)
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `publish_workflow_version` (Task 2).
- Produces: `PUT /api/config/workflows/{name}` appends a version per save.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_crud_api.py` (reuse the module's existing helpers for an admin client + a minimal valid workflow config body with a `fake:` model, following an existing `PUT /api/config/workflows` test):

```python
def test_workflow_put_appends_immutable_versions(client):
    """Two PUTs of the same workflow -> versions 1 then 2; v1 config frozen."""
    from ui.backend.db.models import WorkflowRecord, WorkflowVersion
    _admin_headers(client)  # ensure admin + org 'default' exist per existing helpers

    body_v1 = _minimal_workflow_config(marker="one")
    client.put("/api/config/workflows/wf?org=default", json=body_v1).raise_for_status()
    body_v2 = _minimal_workflow_config(marker="two")
    client.put("/api/config/workflows/wf?org=default", json=body_v2).raise_for_status()

    db = _db_session()
    head = db.query(WorkflowRecord).filter_by(name="wf").one()
    versions = (db.query(WorkflowVersion)
                  .filter_by(workflow_id=head.id)
                  .order_by(WorkflowVersion.version_number).all())
    assert [v.version_number for v in versions] == [1, 2]
    assert versions[0].config != versions[1].config      # v1 preserved distinctly
    assert head.current_version_id == versions[1].id      # pointer at latest
```

`_minimal_workflow_config(marker=...)` returns a valid `Specification.to_raw()`-shaped dict (one agent, a `fake:` model) with `marker` embedded somewhere in the config (e.g. an agent goal) so v1 and v2 differ; reuse any existing config-builder helper in the module.

- [ ] **Step 2: Run to confirm failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py::test_workflow_put_appends_immutable_versions -v`
Expected: FAIL (`no such table: workflow_versions` is already resolved by Task 1, so it fails on `versions == []` — the CRUD path doesn't write versions yet).

- [ ] **Step 3: Rewire `upsert_workflow_config`**

In `ui/backend/crud.py`, add the import near the other db imports:

```python
from .db.workflows import publish_workflow_version
```

Replace the in-place upsert block (lines ~617-625) with:

```python
        item, _version = publish_workflow_version(
            db, org_id=org_id, name=item_name, config=raw
        )
        db.commit()
        status = item.status
    return {"name": item_name, "org": org, "status": status, "config": raw}
```

(No `workflow_id` — the CRUD path has no session; `publish_workflow_version` resolve-or-creates by `(org_id, item_name)`. Keep the single explicit `db.commit()`.)

- [ ] **Step 4: Run to confirm pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py::test_workflow_put_appends_immutable_versions -v`
Expected: PASS.

- [ ] **Step 5: Run the full crud-api suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -q`
Expected: all pass (existing collision/deletion/status tests still green).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/crud.py tests/test_crud_api.py
git commit -m "feat(crud): workflow PUT appends an immutable version"
```

---

### Task 5: Stamp runs with the executed version

**Files:**
- Modify: `ui/backend/runtime.py:56-109` (`run_in_background`: accept + write `workflow_version_id`)
- Modify: `ui/backend/main.py:481-502` (`create_run`: resolve + pass version id)
- Modify: `ui/backend/email_trigger.py:374-383` (stamp the durable run row)
- Test: `tests/test_runtime.py` (or the existing runtime test module) + one email-trigger assertion

**Interfaces:**
- Consumes: `current_version_id` (Task 2); `Run.workflow_version_id` (Task 1).
- Produces: `run_in_background(..., workflow_version_id: Optional[int] = None)` writes the column on the row it creates.

- [ ] **Step 1: Write the failing unit tests**

Add to the runtime test module (`tests/test_runtime.py`; if it does not exist, create it — follow an existing test that calls `run_in_background` directly with a `fake:`-backed `Workflow` and an in-memory engine):

```python
def test_run_in_background_stamps_workflow_version_id(fake_workflow, engine):
    from ui.backend.runtime import run_in_background
    from ui.backend.db.models import Run
    from sqlalchemy.orm import Session

    run_in_background("run-v", fake_workflow, "hi", engine=engine, workflow_version_id=42)
    with Session(engine) as db:
        row = db.get(Run, "run-v")
        assert row.workflow_version_id == 42


def test_run_in_background_leaves_version_null_when_absent(fake_workflow, engine):
    from ui.backend.runtime import run_in_background
    from ui.backend.db.models import Run
    from sqlalchemy.orm import Session

    run_in_background("run-none", fake_workflow, "hi", engine=engine)
    with Session(engine) as db:
        assert db.get(Run, "run-none").workflow_version_id is None
```

`fake_workflow` is a `bestteam` `Workflow` built from a `fake:` spec (reuse the loader/fixtures the existing runtime/usage tests use); `engine` is an in-memory engine with `init_db` applied. Follow the existing runtime test setup exactly — do not invent a new fixture style if the module already has one.

- [ ] **Step 2: Run to confirm failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime.py::test_run_in_background_stamps_workflow_version_id -v`
Expected: FAIL (`run_in_background() got an unexpected keyword argument 'workflow_version_id'`).

- [ ] **Step 3: Add the parameter and write the column**

In `ui/backend/runtime.py`, add the parameter to `run_in_background` (after `username`):

```python
def run_in_background(
    run_id: str,
    workflow: Workflow,
    input: str,
    engine: Optional[Engine] = None,
    user_id: Optional[str] = None,
    org_id: Optional[int] = None,
    username: Optional[str] = None,
    workflow_version_id: Optional[int] = None,
) -> None:
```

Set it when creating the row (the `if run_row is None:` branch, ~line 96):

```python
            run_row = db.get(Run, run_id)
            if run_row is None:
                run_row = Run(
                    id=run_id,
                    workflow=getattr(workflow, "name", ""),
                    input=input,
                    org_id=org_id,
                    username=username,
                    workflow_version_id=workflow_version_id,
                )
                db.add(run_row)
```

(Leave the `else` reuse branch untouched — a caller that pre-inserted the row, i.e. the email trigger, stamps the version itself in Step 5.)

- [ ] **Step 4: Wire `create_run` to resolve and pass the version**

In `ui/backend/main.py`, add the import near the other db imports:

```python
from .db.workflows import current_version_id
```

In `create_run`, resolve the version and pass it through:

```python
    workflow = _get_workflow(req.workflow, db, org.id)
    version_id = current_version_id(db, org.id, req.workflow)
    run = registry.create(req.workflow, req.input, org_id=org.id, username=user.username)

    _executor.submit(
        run_in_background,
        run.id,
        workflow,
        req.input,
        engine=db.get_bind(),
        user_id=user.username,
        org_id=org.id,
        username=user.username,
        workflow_version_id=version_id,
    )
```

(`create_test_run` in `builder.py` is left unchanged — it never passes `workflow_version_id`, so sandbox runs stay NULL, which is correct: they run the session spec, not a published version.)

- [ ] **Step 5: Stamp the email-trigger durable run row**

In `ui/backend/email_trigger.py`, add the import near the top with the other db imports:

```python
from .db.workflows import current_version_id
```

Set the version on the durable `Run` row it builds before dispatch (~line 380):

```python
    run_row = Run(
        id=run.id, workflow=trigger.workflow_name, input=input_text,
        status="running", org_id=trigger.org_id, username=TRIGGER_USERNAME,
        workflow_version_id=current_version_id(db, trigger.org_id, trigger.workflow_name),
    )
```

- [ ] **Step 6: Run the unit tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime.py -v`
Expected: PASS (both new tests).

- [ ] **Step 7: Run the email-trigger suite (no regressions from the new column/import)**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: all pass. If the module has a test that inspects the persisted trigger `Run` row, extend it to assert `workflow_version_id == current_version_id(...)`; otherwise the two runtime unit tests plus the Task-2 helper test cover the stamping logic.

- [ ] **Step 8: Commit**

```bash
git add ui/backend/runtime.py ui/backend/main.py ui/backend/email_trigger.py tests/test_runtime.py
git commit -m "feat(runtime): stamp runs with the executed workflow version"
```

---

### Task 6: Documentation

**Files:**
- Modify: `ui/backend/db/CLAUDE.md`, `ui/backend/CLAUDE.md`, `docs/STATUS.md`, `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`

**Interfaces:**
- Consumes: the finished behavior from Tasks 1-5. No code.

- [ ] **Step 1: Update the persistence-layer notes**

In `ui/backend/db/CLAUDE.md`, in the `knowledge_bases / skills / workflows` bullet, add that a `workflows` row is now the stable team head: it carries `current_version_id` pointing at the latest immutable `workflow_versions` snapshot; deploy appends a version (never overwrites history); `config` remains a mirror of the current version. Add a `workflow_versions` bullet (immutable snapshots; `(workflow_id, version_number)` unique; `created_by`). Note `builder_sessions.workflow_id` (the team head a session deploys to — P1-02) and `runs.workflow_version_id` (the exact version a run executed; NULL for sandbox/pre-migration). State the P1-04/P1-05 boundary explicitly: a version freezes the inline config blob only; standalone Skills/KBs/models are still resolved by name at load.

- [ ] **Step 2: Update the API-layer notes**

In `ui/backend/CLAUDE.md`, update the `deploy_session` and `crud.py` `PUT /workflows/{name}` descriptions: both now call `db/workflows.py::publish_workflow_version` (append a version + move the pointer) instead of overwriting `config`; `deploy_session` links `session.workflow_id` in the same commit (P1-02, rename-safe). Note runs are stamped with `workflow_version_id` at start (sandbox test-runs stay NULL).

- [ ] **Step 3: Update STATUS.md**

In `docs/STATUS.md`, record P1-01/02/03 as implemented (immutable workflow versions + stable team identity + run→version linkage; backend/data-model only). Note the deferred follow-ups: version-history/rollback UI, P1-01 UI-terminology cleanup, and the P1-04 standalone-dependency freeze.

- [ ] **Step 4: Update the triage register**

In `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`, move P1-01/P1-02/P1-03 into the "Implemented this pass" section with a row each (validation → fix → verification), and reference `docs/superpowers/specs/2026-07-25-versioning-keystone-design.md`. Update the "Everything else" list to drop P1-01/02/03.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/CLAUDE.md ui/backend/CLAUDE.md docs/STATUS.md docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md
git commit -m "docs: record versioning keystone (P1-01/02/03)"
```

- [ ] **Step 6: Full suite (scratch DB)**

Run: `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q`
Expected: all pass.

---

## Verification (whole plan)

- Unit: `publish_workflow_version` — v1 on first deploy; v2 on redeploy with pointer moved and v1 immutable; rename under `workflow_id`; stale-pointer fallback; `current_version_id`.
- P1-02: two distinct sessions with the same spec name converge on one head, both configs preserved as v1/v2; one session redeployed twice keeps its head, versions 1→2.
- CRUD: `PUT /api/config/workflows/{name}` appends versions, v1 frozen, pointer at latest.
- Run linkage: `run_in_background` stamps `workflow_version_id` when given, NULL otherwise; `create_run` resolves it; the email trigger stamps the durable row; sandbox test-runs stay NULL.
- Migration: `create_all → upgrade head` idempotent; backfill creates exactly one v1 per existing workflow and sets the pointer; re-run adds nothing.
- Full suite green with the scratch DB; frontend unaffected (no FE changes).
