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

pytestmark = [pytest.mark.integration, pytest.mark.slow]

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
    "skill_versions",
    "workflows",
    "workflow_dependencies",
    "builder_sessions",
    "email_triggers",
    "model_catalog",
    "runs",
    "trace_events",
    "usage_records",
    "org_email_credentials",
    "share_links",
    "share_sessions",
    "share_messages",
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


def _has_fk(engine, table: str, column: str, referred_table: str) -> bool:
    return any(
        fk.get("constrained_columns") == [column]
        and fk.get("referred_table") == referred_table
        for fk in sa.inspect(engine).get_foreign_keys(table)
    )


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
    engine = make_engine(db_path)
    try:
        assert _has_fk(engine, "skills", "current_version_id", "skill_versions")
        assert _has_fk(
            engine,
            "workflow_dependencies",
            "resource_version_id",
            "skill_versions",
        )
    finally:
        engine.dispose()


# Revision just before principal_id, and the principal_id revision itself.
_PRE_PRINCIPAL = "a7b8c9d0e1f2"
_PRINCIPAL = "b8c9d0e1f2a3"


def test_interrupted_principal_migration_backfills_on_retry(tmp_path, monkeypatch):
    """Finding 2: if the principal_id column was added but the process died before
    the backfill (and the revision wasn't recorded), re-running to head must
    backfill the NULL principal -- not skip it because the column already exists.
    """
    db_path = tmp_path / "interrupted.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_PRINCIPAL)  # users has no principal_id yet

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (username, password_hash, is_admin, created_at) "
                "VALUES ('alice', 'x', 0, '2026-01-01T00:00:00+00:00')"
            )
        )
        # Simulate the crash: the column-add committed, the backfill did not, and
        # the revision was never stamped (still at _PRE_PRINCIPAL).
        conn.execute(sa.text("ALTER TABLE users ADD COLUMN principal_id VARCHAR"))
    engine.dispose()

    command.upgrade(cfg, "head")  # re-run must backfill the NULL principal

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            principal = conn.execute(
                sa.text("SELECT principal_id FROM users WHERE username = 'alice'")
            ).scalar()
    finally:
        engine.dispose()
    assert principal is not None and principal != ""


def test_upgrade_head_on_empty_db_succeeds(tmp_path, monkeypatch):
    """Pure-migration path (no create_all first) still builds head cleanly."""
    db_path = tmp_path / "empty_then_migrate.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    tables = _table_names(db_path)
    assert _EXPECTED_HEAD_TABLES.issubset(tables)
    assert "agents" not in tables
    assert "teams" not in tables


def test_automation_item_results_migration_creates_table_and_run_columns(tmp_path, monkeypatch):
    """c1d2e3f4a5b6: automation_item_results table + runs.trigger_context/
    retry_of_run_id, both from a pure-migration path (no create_all first)."""
    db_path = tmp_path / "automation_results.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        inspector = sa.inspect(engine)
        assert "automation_item_results" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("automation_item_results")}
        assert {
            "id", "org_id", "run_id", "source_type", "source_key",
            "result_type", "status", "needs_attention", "payload", "created_at",
        } <= columns
        run_columns = {c["name"] for c in inspector.get_columns("runs")}
        assert {"trigger_context", "retry_of_run_id"} <= run_columns
    finally:
        engine.dispose()


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


# Revision just before the workflows.created_by -> principal_id backfill.
_PRE_CREATED_BY_BACKFILL = "g4h5i6j7k8l9"


def test_workflow_created_by_backfilled_from_username_to_principal_id(tmp_path, monkeypatch):
    """A workflow deployed before the ownership-key switch has `created_by` set
    to the owner's username; the backfill must re-key it to that user's
    principal_id so My Teams / run ownership still resolves it post-upgrade."""
    db_path = tmp_path / "created_by_backfill.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_CREATED_BY_BACKFILL)

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (username, password_hash, is_admin, created_at, principal_id) "
            "VALUES ('alice', 'x', 0, '2026-01-01T00:00:00+00:00', 'principal-abc')"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflows (name, org_id, config, status, created_at, updated_at, created_by) "
            "VALUES ('legacy', NULL, '{}', 'deployed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'alice')"
        ))
        # Admin-shared (NULL created_by) must stay NULL.
        conn.execute(sa.text(
            "INSERT INTO workflows (name, org_id, config, status, created_at, updated_at, created_by) "
            "VALUES ('shared', NULL, '{}', 'deployed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            legacy = conn.execute(sa.text(
                "SELECT created_by FROM workflows WHERE name = 'legacy'"
            )).scalar()
            shared = conn.execute(sa.text(
                "SELECT created_by FROM workflows WHERE name = 'shared'"
            )).scalar()
    finally:
        engine.dispose()
    assert legacy == "principal-abc"
    assert shared is None

    # Idempotent: re-running upgrade head leaves the already-backfilled row alone.
    command.upgrade(cfg, "head")
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            legacy = conn.execute(sa.text(
                "SELECT created_by FROM workflows WHERE name = 'legacy'"
            )).scalar()
    finally:
        engine.dispose()
    assert legacy == "principal-abc"


def test_workflow_created_by_backfill_skips_workflow_older_than_username_holder(tmp_path, monkeypatch):
    """Codex review finding: a deleted-then-recreated account can reuse a
    username. If the workflow predates the *current* holder of that username's
    own account, the current account can't be the one that created it -- an
    unconditional backfill would hand it a stranger's (the deleted account's)
    workflows. The backfill must leave such a row alone."""
    db_path = tmp_path / "created_by_backfill_reuse.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_CREATED_BY_BACKFILL)

    engine = make_engine(db_path)
    with engine.begin() as conn:
        # 'bob' the workflow belongs to (created_at 2026-01-01) is deleted and
        # a new, unrelated account reuses the username 'bob' later.
        conn.execute(sa.text(
            "INSERT INTO workflows (name, org_id, config, status, created_at, updated_at, created_by) "
            "VALUES ('old-bobs-team', NULL, '{}', 'deployed', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00', 'bob')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (username, password_hash, is_admin, created_at, principal_id) "
            "VALUES ('bob', 'x', 0, '2026-06-01T00:00:00+00:00', 'principal-new-bob')"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            created_by = conn.execute(sa.text(
                "SELECT created_by FROM workflows WHERE name = 'old-bobs-team'"
            )).scalar()
    finally:
        engine.dispose()
    # Left as the stale username, NOT rewritten to the new account's
    # principal_id -- an orphaned reference is safer than a takeover.
    assert created_by == "bob"


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


# Revision immediately before immutable skill versions/pins.
_PRE_SKILL_VERSIONS = "b8c9d0e1f2a3"


def test_skill_version_migration_backfills_heads_and_workflow_pins(tmp_path, monkeypatch):
    db_path = tmp_path / "skill_versions.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_SKILL_VERSIONS)

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO skills (id, name, org_id, config, created_at, updated_at) "
            "VALUES (3, 'greet', NULL, '{\"name\": \"greet\", "
            "\"instructions\": \"legacy\"}', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflows "
            "(id, name, org_id, config, status, created_at, updated_at, current_version_id) "
            "VALUES (4, 'team', NULL, '{}', 'deployed', '2026-01-01', '2026-01-01', NULL)"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_versions "
            "(id, workflow_id, version_number, config, created_by, created_at) "
            "VALUES (5, 4, 1, '{}', NULL, '2026-01-01')"
        ))
        conn.execute(sa.text(
            "UPDATE workflows SET current_version_id = 5 WHERE id = 4"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_dependencies "
            "(id, workflow_version_id, resource_kind, resource_name, resource_id) "
            "VALUES (6, 5, 'skill', 'greet', 3)"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text(
                "SELECT id, skill_id, version_number, config FROM skill_versions"
            )).one()
            assert version[1:3] == (3, 1)
            assert "legacy" in version[3]
            assert conn.execute(sa.text(
                "SELECT current_version_id FROM skills WHERE id = 3"
            )).scalar() == version[0]
            assert conn.execute(sa.text(
                "SELECT resource_version_id FROM workflow_dependencies WHERE id = 6"
            )).scalar() == version[0]
        assert _has_fk(engine, "skills", "current_version_id", "skill_versions")
        assert _has_fk(
            engine,
            "workflow_dependencies",
            "resource_version_id",
            "skill_versions",
        )
    finally:
        engine.dispose()


def test_skill_version_migration_repairs_existing_columns_missing_fks(tmp_path, monkeypatch):
    """Retry must repair schema parity when columns exist without constraints."""
    db_path = tmp_path / "skill_versions_missing_fks.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_SKILL_VERSIONS)

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE skill_versions ("
            "id INTEGER PRIMARY KEY, skill_id INTEGER, version_number INTEGER NOT NULL, "
            "config JSON NOT NULL, created_by VARCHAR, created_at DATETIME NOT NULL, "
            "CONSTRAINT uq_skill_versions_skill_id_version_number "
            "UNIQUE (skill_id, version_number), "
            "FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE SET NULL)"
        ))
        conn.execute(sa.text("ALTER TABLE skills ADD COLUMN current_version_id INTEGER"))
        conn.execute(sa.text(
            "ALTER TABLE workflow_dependencies ADD COLUMN resource_version_id INTEGER"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        assert _has_fk(engine, "skills", "current_version_id", "skill_versions")
        assert _has_fk(
            engine,
            "workflow_dependencies",
            "resource_version_id",
            "skill_versions",
        )
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


def test_workflow_dependencies_backfill_tolerates_mixed_type_refs(tmp_path, monkeypatch):
    # A legacy config with a non-string skill/tool ref (["greet", 1]) must not
    # abort the whole upgrade with a str/int sorted() TypeError; the valid string
    # refs are still recorded.
    db_path = tmp_path / "wf_deps_mixed.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_DEPS)

    engine = make_engine(db_path)
    config_json = (
        '{"name": "wf", "agents": [{"name": "a", '
        '"skills": ["greet", 1], "tools": ["http_get", 2]}]}'
    )
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO skills (id, name, org_id, config, created_at, updated_at) "
            "VALUES (3, 'greet', 5, '{}', '2026-01-01', '2026-01-01')"
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

    command.upgrade(cfg, "head")  # must not raise

    engine = make_engine(db_path)
    with engine.begin() as conn:
        deps = set(conn.execute(sa.text(
            "SELECT resource_kind, resource_name, resource_id FROM workflow_dependencies"
        )).fetchall())
    engine.dispose()
    # int refs dropped; http_get is a built-in tool, not a standalone KB.
    assert deps == {("skill", "greet", 3)}


def test_workflow_dependencies_backfill_inline_kb_shadows_standalone(tmp_path, monkeypatch):
    # A workflow with an inline KB "faq" and a same-named standalone KB. The
    # inline KB wins at runtime, so the backfill must NOT record the standalone
    # KB as a dependency (else the standalone would be wrongly delete-blocked).
    db_path = tmp_path / "wf_deps_inline.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_DEPS)

    engine = make_engine(db_path)
    config_json = (
        '{"name": "wf", "knowledge_bases": [{"name": "faq", "path": "./faq"}], '
        '"agents": [{"name": "a", "skills": [], "tools": ["faq"]}]}'
    )
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO knowledge_bases (id, name, org_id, config, created_at, updated_at) "
            "VALUES (4, 'faq', 5, '{}', '2026-01-01', '2026-01-01')"
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
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM workflow_dependencies WHERE resource_kind = 'knowledge_base'"
        )).scalar()
    engine.dispose()
    assert count == 0  # inline KB shadows the standalone -> no dependency row
