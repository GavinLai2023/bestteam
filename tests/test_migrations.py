"""Alembic migration regression tests.

Two properties this project relies on but never had automated coverage for:

1. `create_all -> alembic upgrade head` is idempotent. `ui/backend/db_session.py`
   runs `Base.metadata.create_all` at import, and `docs/deployment.md` tells the
   operator to start the backend (which imports that module) *before* running
   `alembic upgrade head`. So every migration must tolerate the schema already
   existing -- an unguarded `op.create_table`/`op.add_column` fails with
   "table/column already exists" and leaves a half-migrated database.
2. Dropping the vestigial `agents`/`teams` tables must not silently destroy
   data. Writable CRUD routes for those tables existed historically
   (`78c7a8a`..`036e1d6`), so a deployment could hold rows; the drop migration
   refuses rather than dropping populated tables.

These drive real Alembic `command.upgrade` runs against a throwaway on-disk
SQLite file (Alembic needs a URL, not an in-memory StaticPool engine).
"""

from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from ui.backend.db import init_db, make_engine

_ROOT = Path(__file__).resolve().parent.parent

# Revision just before the drop-vestigial-tables migration (its down_revision).
_PRE_DROP = "e85b2230b950"

# The tables the current models define (see tests/test_db.py). agents/teams are
# deliberately absent -- they are dropped at head.
_EXPECTED_HEAD_TABLES = {
    "organizations",
    "users",
    "knowledge_bases",
    "skills",
    "workflows",
    "workflow_dependencies",
    "builder_sessions",
    "email_triggers",
    "model_catalog",
    "runs",
    "trace_events",
    "usage_records",
    "org_email_credentials",
}


def _alembic_config(db_path: Path, monkeypatch) -> Config:
    # alembic/env.py builds sqlalchemy.url from BESTTEAM_DB_PATH.
    monkeypatch.setenv("BESTTEAM_DB_PATH", str(db_path))
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return cfg


def _table_names(db_path: Path) -> set[str]:
    engine = make_engine(db_path)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_create_all_then_upgrade_head_is_idempotent(tmp_path, monkeypatch):
    """The documented deploy order: create_all (backend import) THEN migrate.

    Reproduces the deterministic `table ... already exists` failure a fresh
    customer deployment hit before the migrations were made create_all-safe.
    """
    db_path = tmp_path / "create_all_then_migrate.db"
    engine = make_engine(db_path)
    init_db(engine)  # the create_all bootstrap that runs at backend import
    engine.dispose()

    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")  # must not raise

    tables = _table_names(db_path)
    assert _EXPECTED_HEAD_TABLES.issubset(tables)
    assert "agents" not in tables
    assert "teams" not in tables


def test_upgrade_head_on_empty_db_succeeds(tmp_path, monkeypatch):
    """Pure-migration path (no create_all first) still builds head cleanly."""
    db_path = tmp_path / "empty_then_migrate.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    tables = _table_names(db_path)
    assert _EXPECTED_HEAD_TABLES.issubset(tables)
    assert "agents" not in tables
    assert "teams" not in tables


def test_drop_migration_refuses_when_legacy_tables_have_rows(tmp_path, monkeypatch):
    """A populated agents/teams table must block the drop, not be destroyed."""
    db_path = tmp_path / "populated_legacy.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_DROP)  # agents/teams exist here, empty

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (name, config, created_at, updated_at) "
                "VALUES ('legacy', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    with pytest.raises(Exception, match="Refusing to drop"):
        command.upgrade(cfg, "head")

    # The table -- and its row -- survive the refused migration.
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT COUNT(*) FROM agents")).scalar() == 1
    finally:
        engine.dispose()


def test_drop_migration_drops_when_legacy_tables_empty(tmp_path, monkeypatch):
    """Empty legacy tables drop cleanly (the normal case)."""
    db_path = tmp_path / "empty_legacy.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_DROP)
    assert {"agents", "teams"}.issubset(_table_names(db_path))

    command.upgrade(cfg, "head")
    tables = _table_names(db_path)
    assert "agents" not in tables
    assert "teams" not in tables


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
            # Exactly one v1 -- the backfill did not duplicate.
            count = conn.execute(sa.text("SELECT COUNT(*) FROM workflow_versions")).scalar()
            assert count == 1
    finally:
        engine.dispose()

    # Schema parity with the ORM: created_at is NOT NULL on the migrated table
    # (matching Mapped[datetime]), not the nullable column the first draft added.
    cols = {c["name"]: c for c in sa.inspect(make_engine(db_path)).get_columns("workflow_versions")}
    assert cols["created_at"]["nullable"] is False

    # Idempotent: re-running upgrade head does not duplicate the v1 row.
    command.upgrade(cfg, "head")
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            count = conn.execute(sa.text("SELECT COUNT(*) FROM workflow_versions")).scalar()
            assert count == 1
    finally:
        engine.dispose()


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
