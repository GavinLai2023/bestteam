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
