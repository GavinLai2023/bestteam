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
    "knowledge_ingestion_jobs",
    "knowledge_documents",
    "knowledge_chunks",
    "skills",
    "skill_versions",
    "pipelines",
    "pipeline_dependencies",
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
        # create_all already produced these; the migration must not have
        # rebuilt them into something else on its way past.
        usage_columns = {c["name"]: c for c in sa.inspect(engine).get_columns("usage_records")}
        assert usage_columns["run_id"]["nullable"] is True
        assert "ingestion_job_id" in usage_columns
        assert _has_fk(engine, "skills", "current_version_id", "skill_versions")
        assert _has_fk(
            engine,
            "pipeline_dependencies",
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
            status = conn.execute(sa.text("SELECT status FROM pipelines WHERE name='legacy'")).scalar()
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
                "SELECT created_by FROM pipelines WHERE name = 'legacy'"
            )).scalar()
            shared = conn.execute(sa.text(
                "SELECT created_by FROM pipelines WHERE name = 'shared'"
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
                "SELECT created_by FROM pipelines WHERE name = 'legacy'"
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
                "SELECT created_by FROM pipelines WHERE name = 'old-bobs-team'"
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
                    "INSERT INTO pipelines (name, org_id, config, status, created_at, updated_at) "
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
                "SELECT resource_version_id FROM pipeline_dependencies WHERE id = 6"
            )).scalar() == version[0]
        assert _has_fk(engine, "skills", "current_version_id", "skill_versions")
        assert _has_fk(
            engine,
            "pipeline_dependencies",
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
            "pipeline_dependencies",
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
                "SELECT pipeline_id, version_number, config FROM pipeline_versions"
            )).all()
            assert rows == [(7, 1, '{"name": "wf"}')]
            ptr = conn.execute(sa.text(
                "SELECT current_version_id FROM pipelines WHERE id = 7"
            )).scalar()
            vid = conn.execute(sa.text(
                "SELECT id FROM pipeline_versions WHERE pipeline_id = 7"
            )).scalar()
            assert ptr == vid
            # Exactly one v1 -- the backfill did not duplicate.
            count = conn.execute(sa.text("SELECT COUNT(*) FROM pipeline_versions")).scalar()
            assert count == 1
    finally:
        engine.dispose()

    # Schema parity with the ORM: created_at is NOT NULL on the migrated table
    # (matching Mapped[datetime]), not the nullable column the first draft added.
    cols = {c["name"]: c for c in sa.inspect(make_engine(db_path)).get_columns("pipeline_versions")}
    assert cols["created_at"]["nullable"] is False

    # Idempotent: re-running upgrade head does not duplicate the v1 row.
    command.upgrade(cfg, "head")
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            count = conn.execute(sa.text("SELECT COUNT(*) FROM pipeline_versions")).scalar()
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
            "SELECT pipeline_version_id, resource_kind, resource_name, resource_id "
            "FROM pipeline_dependencies"
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
        count = conn.execute(sa.text("SELECT COUNT(*) FROM pipeline_dependencies")).scalar()
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
            "SELECT resource_kind, resource_name, resource_id FROM pipeline_dependencies"
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
            "SELECT COUNT(*) FROM pipeline_dependencies WHERE resource_kind = 'knowledge_base'"
        )).scalar()
    engine.dispose()
    assert count == 0  # inline KB shadows the standalone -> no dependency row


def test_the_oauth_credential_columns_upgrade_and_downgrade(tmp_path, monkeypatch):
    """Existing password mailboxes must survive the upgrade as `password`."""
    db_path = tmp_path / "oauth_credentials.db"
    cfg = _alembic_config(db_path, monkeypatch)

    command.upgrade(cfg, "h5i6j7k8l9m0")  # the revision before this one
    engine = make_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO organizations (name, display_name, active, created_at) "
                "VALUES ('acme', 'Acme', 1, CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO org_email_credentials "
                "(org_id, backend, host, port, username, password_encrypted, "
                " created_at, updated_at) "
                "VALUES (1, 'imap', 'imap.example.com', 993, 'u', 'tok', "
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))

        command.upgrade(cfg, "i6j7k8l9m0n1")
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT auth_type, oauth_tenant_id, oauth_client_id "
                "FROM org_email_credentials WHERE org_id = 1"
            )).one()
        assert row.auth_type == "password"
        assert row.oauth_tenant_id is None
        assert row.oauth_client_id is None

        command.downgrade(cfg, "h5i6j7k8l9m0")
        with engine.connect() as conn:
            columns = {c["name"] for c in sa.inspect(conn).get_columns("org_email_credentials")}
        assert "auth_type" not in columns
        assert "oauth_tenant_id" not in columns
        # The row itself survives the round trip.
        with engine.connect() as conn:
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM org_email_credentials")
            ).scalar() == 1
    finally:
        engine.dispose()


def test_the_notification_migration_upgrades_and_downgrades(tmp_path, monkeypatch):
    """Additive: an existing trigger comes through as healthy, nothing reported."""
    db_path = tmp_path / "notifications.db"
    cfg = _alembic_config(db_path, monkeypatch)

    command.upgrade(cfg, "i6j7k8l9m0n1")  # the revision before this one
    engine = make_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO organizations (name, display_name, active, created_at) "
                "VALUES ('acme', 'Acme', 1, CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO email_triggers "
                "(org_id, workflow_name, enabled, last_uid, runs_today, "
                " created_at, updated_at) "
                "VALUES (1, 'w', 1, 5, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))

        command.upgrade(cfg, "j7k8l9m0n1o2")
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT consecutive_faults, alerted_fingerprint "
                "FROM email_triggers WHERE org_id = 1"
            )).one()
            tables = set(sa.inspect(conn).get_table_names())
        # Zero faults and no fingerprint is exactly "healthy, nothing reported
        # yet", so no backfill is needed.
        assert row.consecutive_faults == 0
        assert row.alerted_fingerprint is None
        assert {"notifications", "org_notification_settings"} <= tables

        command.downgrade(cfg, "i6j7k8l9m0n1")
        with engine.connect() as conn:
            columns = {c["name"] for c in sa.inspect(conn).get_columns("email_triggers")}
            tables = set(sa.inspect(conn).get_table_names())
            surviving = conn.execute(
                sa.text("SELECT COUNT(*) FROM email_triggers")
            ).scalar()
        assert "consecutive_faults" not in columns
        assert "alerted_fingerprint" not in columns
        assert not ({"notifications", "org_notification_settings"} & tables)
        assert surviving == 1  # the trigger survives the round trip
    finally:
        engine.dispose()


def test_the_secret_expiry_column_upgrades_and_downgrades(tmp_path, monkeypatch):
    db_path = tmp_path / "secret_expiry.db"
    cfg = _alembic_config(db_path, monkeypatch)

    command.upgrade(cfg, "i6j7k8l9m0n1")
    engine = make_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO organizations (name, display_name, active, created_at) "
                "VALUES ('acme', 'Acme', 1, CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO org_email_credentials "
                "(org_id, backend, host, port, username, password_encrypted, "
                " auth_type, created_at, updated_at) "
                "VALUES (1, 'imap', 'outlook.office365.com', 993, 'u', 'tok', "
                " 'microsoft_oauth', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))

        command.upgrade(cfg, "j7k8l9m0n1o2")
        with engine.connect() as conn:
            expires = conn.execute(sa.text(
                "SELECT oauth_secret_expires_at FROM org_email_credentials WHERE org_id = 1"
            )).scalar()
        # NULL means "no expiry recorded", which switches the sweep off for
        # this credential rather than warning about a date we invented.
        assert expires is None

        command.downgrade(cfg, "i6j7k8l9m0n1")
        with engine.connect() as conn:
            columns = {c["name"] for c in sa.inspect(conn).get_columns("org_email_credentials")}
        assert "oauth_secret_expires_at" not in columns
    finally:
        engine.dispose()


def test_knowledge_chunks_page_heading_migration_downgrades_and_reupgrades(tmp_path, monkeypatch):
    """m0n1o2p3q4r5: the two chunk-location columns citations are built from.

    Purely additive, so the round trip has to keep the chunk itself: an
    operator rolling back reads a database whose chunks cite their filename
    alone, exactly as they did before the columns existed. Downgrading to
    this revision's parent from head necessarily runs `n1o2p3q4r5s6`'s own
    downgrade first, so both knowledge-base migrations are dragged through
    both directions here.
    """
    db_path = tmp_path / "chunk_metadata.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        columns = {c["name"] for c in sa.inspect(engine).get_columns("knowledge_chunks")}
        assert {"page", "heading"} <= columns

        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO knowledge_chunks "
                "(id, document_id, kb_id, chunk_index, text, page, heading, created_at) "
                "VALUES (1, 1, 1, 0, 'Refunds are accepted within 14 days.', 3, "
                " 'Refunds', CURRENT_TIMESTAMP)"
            ))

        command.downgrade(cfg, "l9m0n1o2p3q4")
        with engine.connect() as conn:
            columns = {c["name"] for c in sa.inspect(conn).get_columns("knowledge_chunks")}
            surviving = conn.execute(
                sa.text("SELECT text FROM knowledge_chunks WHERE id = 1")
            ).scalar()
        assert "page" not in columns
        assert "heading" not in columns
        assert surviving == "Refunds are accepted within 14 days."

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            columns = {c["name"] for c in sa.inspect(conn).get_columns("knowledge_chunks")}
            row = conn.execute(
                sa.text("SELECT page, heading FROM knowledge_chunks WHERE id = 1")
            ).one()
        assert {"page", "heading"} <= columns
        # Re-upgrading is idempotent, and the columns come back empty: there is
        # no backfill, because a page and a heading can only be recovered by
        # re-parsing the original document, which a re-upload already does.
        assert row.page is None
        assert row.heading is None
    finally:
        engine.dispose()


def test_usage_records_nullable_run_id_downgrade_deletes_only_rows_without_a_run(tmp_path, monkeypatch):
    """n1o2p3q4r5s6: knowledge-base ingestion spend lives in the same ledger,
    so `run_id` has to be nullable and `ingestion_job_id` has to exist.

    The downgrade is lossy by design, and this pins *how far*: a row that
    belongs to a run survives, and only a row the old NOT NULL schema has no
    way to express is deleted rather than given a fabricated run id.
    """
    db_path = tmp_path / "usage_ingestion.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        inspector = sa.inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("usage_records")}
        assert "ingestion_job_id" in columns
        assert columns["run_id"]["nullable"] is True
        assert _has_fk(engine, "usage_records", "ingestion_job_id", "knowledge_ingestion_jobs")

        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO usage_records "
                "(id, run_id, agent, model, input_tokens, output_tokens, created_at) "
                "VALUES (1, 'run-1', 'writer', 'openai:gpt-4o-mini', 10, 5, "
                " CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO usage_records "
                "(id, run_id, ingestion_job_id, agent, model, input_tokens, "
                " output_tokens, created_at) "
                "VALUES (2, NULL, 7, 'kb:ingest', "
                " 'openai:text-embedding-3-small', 400, 0, CURRENT_TIMESTAMP)"
            ))

        command.downgrade(cfg, "m0n1o2p3q4r5")
        with engine.connect() as conn:
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("usage_records")}
            ids = [row[0] for row in conn.execute(
                sa.text("SELECT id FROM usage_records ORDER BY id")
            )]
        assert ids == [1]  # the run's row stays, the ingestion row goes
        assert "ingestion_job_id" not in columns
        assert columns["run_id"]["nullable"] is False

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("usage_records")}
            ids = [row[0] for row in conn.execute(
                sa.text("SELECT id FROM usage_records ORDER BY id")
            )]
        # Idempotent on the way back up, and it does not resurrect what the
        # downgrade deleted -- the spend is gone from the ledger for good.
        assert "ingestion_job_id" in columns
        assert columns["run_id"]["nullable"] is True
        assert _has_fk(engine, "usage_records", "ingestion_job_id", "knowledge_ingestion_jobs")
        assert ids == [1]
    finally:
        engine.dispose()


# Revision just before the Workflow -> Pipeline rename (the previous head).
_PRE_PIPELINE_RENAME = "n1o2p3q4r5s6"


def test_workflow_to_pipeline_rename_preserves_data_and_rewrites_config(tmp_path, monkeypatch):
    """o2p3q4r5s6t7: tables/columns renamed, FKs follow, and the `"workflow"`
    key inside `config` JSON becomes `"pipeline"` -- all without losing rows."""
    db_path = tmp_path / "rename.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_PIPELINE_RENAME)  # old workflow* names, no rename yet

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id, name, org_id, config, status, created_at, updated_at) "
            "VALUES (9, 'wf', NULL, "
            "'{\"name\": \"wf\", \"agents\": [], \"teams\": [], \"workflow\": {\"steps\": []}}', "
            "'deployed', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_versions (id, workflow_id, version_number, config, created_by, created_at) "
            "VALUES (10, 9, 1, "
            "'{\"name\": \"wf\", \"agents\": [], \"teams\": [], \"workflow\": {\"steps\": []}}', "
            "NULL, '2026-01-01')"
        ))
        conn.execute(sa.text("UPDATE workflows SET current_version_id = 10 WHERE id = 9"))
        conn.execute(sa.text(
            "INSERT INTO workflow_dependencies "
            "(id, workflow_version_id, resource_kind, resource_name, resource_id) "
            "VALUES (11, 10, 'skill', 'greet', NULL)"
        ))
        conn.execute(sa.text(
            "INSERT INTO runs (id, workflow, input, status, workflow_version_id, created_at) "
            "VALUES ('run-9', 'wf', 'hi', 'completed', 10, CURRENT_TIMESTAMP)"
        ))
        conn.execute(sa.text(
            "INSERT INTO organizations (name, active) VALUES ('acme', 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO email_triggers (org_id, workflow_name, enabled, last_uid, runs_today, "
            "messages_today, created_at, updated_at) "
            "VALUES (1, 'wf', 1, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        # A wizard session's in-progress draft (builder_sessions.specification_json)
        # is the same Specification.to_raw() shape as pipelines/pipeline_versions.config
        # and must be rewritten too, or a pre-upgrade session silently loses its steps
        # the next time it's opened (Codex review finding).
        conn.execute(sa.text(
            "INSERT INTO builder_sessions (id, intent_text, as_is_text, specification_json, "
            "status, feedback_history, created_at, updated_at) "
            "VALUES ('sess-1', 'hi', '', "
            "'{\"name\": \"wf\", \"agents\": [], \"teams\": [], \"workflow\": {\"steps\": []}}', "
            "'spec', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert {"pipelines", "pipeline_versions", "pipeline_dependencies"} <= tables
        assert not ({"workflows", "workflow_versions", "workflow_dependencies"} & tables)

        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT config, current_version_id FROM pipelines WHERE id = 9"
            )).one()
            assert '"pipeline"' in row.config and '"workflow"' not in row.config

            version = conn.execute(sa.text(
                "SELECT pipeline_id, config FROM pipeline_versions WHERE id = 10"
            )).one()
            assert version.pipeline_id == 9
            assert '"pipeline"' in version.config and '"workflow"' not in version.config
            assert row.current_version_id == 10

            dep = conn.execute(sa.text(
                "SELECT pipeline_version_id, resource_kind FROM pipeline_dependencies WHERE id = 11"
            )).one()
            assert dep.pipeline_version_id == 10 and dep.resource_kind == "skill"

            run = conn.execute(sa.text(
                "SELECT pipeline, pipeline_version_id FROM runs WHERE id = 'run-9'"
            )).one()
            assert run.pipeline == "wf" and run.pipeline_version_id == 10

            trigger = conn.execute(sa.text(
                "SELECT pipeline_name FROM email_triggers WHERE org_id = 1"
            )).one()
            assert trigger.pipeline_name == "wf"

            session = conn.execute(sa.text(
                "SELECT specification_json FROM builder_sessions WHERE id = 'sess-1'"
            )).one()
            assert '"pipeline"' in session.specification_json
            assert '"workflow"' not in session.specification_json
    finally:
        engine.dispose()

    # Idempotent: re-running upgrade head is a no-op, not a second rename attempt.
    command.upgrade(cfg, "head")


def test_workflow_to_pipeline_rename_absorbs_a_create_all_race(tmp_path, monkeypatch):
    """If `create_all()` (the `db_session.py` safety net) runs before this
    migration and creates the new tables empty, the migration must absorb
    them rather than fail with 'table already exists'."""
    from ui.backend.db.database import init_db as _init_db

    db_path = tmp_path / "race.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_PIPELINE_RENAME)

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id, name, org_id, config, status, created_at, updated_at) "
            "VALUES (1, 'wf', NULL, '{}', 'deployed', '2026-01-01', '2026-01-01')"
        ))
    # Simulate a process booting (and running create_all) before this migration --
    # the renamed models declare empty `pipelines`/`pipeline_versions`/
    # `pipeline_dependencies` tables that create_all() will happily create
    # alongside the still-populated old ones.
    _init_db(engine)
    engine.dispose()

    command.upgrade(cfg, "head")  # must absorb the empty shells, not raise

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            name = conn.execute(sa.text("SELECT name FROM pipelines WHERE id = 1")).scalar()
        assert name == "wf"
    finally:
        engine.dispose()


def test_the_ingestion_chunk_parameter_columns_upgrade_and_downgrade(tmp_path, monkeypatch):
    """r5s6t7u8v9w0: what incremental ingestion reads to decide reuse.

    A job written before the columns existed reads back NULL, and
    `ingestion._carryable` treats NULL as "not reusable" -- so the round trip
    has to leave an existing job intact and unreused, not backfilled with a
    guess at what it was actually chunked with.
    """
    db_path = tmp_path / "chunk_params.db"
    cfg = _alembic_config(db_path, monkeypatch)

    command.upgrade(cfg, "q4r5s6t7u8v9")
    engine = make_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO organizations (name, display_name, active, created_at) "
                "VALUES ('acme', 'Acme', 1, CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO knowledge_bases (name, config, org_id, created_at, updated_at) "
                "VALUES ('policies', '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "INSERT INTO knowledge_ingestion_jobs "
                "(kb_id, org_id, version, kb_type, status, file_count, "
                " documents_succeeded, documents_failed, created_at) "
                "VALUES (1, 1, 'v_old', 'local_folder', 'completed', 1, 1, 0, "
                " CURRENT_TIMESTAMP)"
            ))

        command.upgrade(cfg, "r5s6t7u8v9w0")
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT chunk_size, chunk_overlap, version FROM knowledge_ingestion_jobs"
            )).one()
        assert row.version == "v_old"
        assert (row.chunk_size, row.chunk_overlap) == (None, None)

        command.downgrade(cfg, "q4r5s6t7u8v9")
        with engine.connect() as conn:
            columns = {c["name"] for c in sa.inspect(conn).get_columns("knowledge_ingestion_jobs")}
            assert conn.execute(sa.text(
                "SELECT count(*) FROM knowledge_ingestion_jobs"
            )).scalar() == 1
        assert "chunk_size" not in columns
        assert "chunk_overlap" not in columns
    finally:
        engine.dispose()


# Revision just before the built-in skill rename (the previous head).
_PRE_SKILL_RENAME = "v9w0x1y2z3a4"


def test_builtin_skill_suffix_rename_merges_and_rewrites(tmp_path, monkeypatch):
    """w0x1y2z3a4b5: platform skills lose their _vN suffix, intake _v1+_v2
    merge into one skill (snapshot ids untouched, renumbered by created_at),
    every stored config/dependency reference is rewritten -- except inside an
    org that shadows an old name with its own skill."""
    db_path = tmp_path / "skills_rename.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_SKILL_RENAME)

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, display_name, created_at, active) "
            "VALUES (7, 'acme', '', CURRENT_TIMESTAMP, 1), (8, 'shadow_co', '', CURRENT_TIMESTAMP, 1)"
        ))
        # Platform intake _v1 + _v2 with one snapshot each; shadow_co owns a
        # skill under a built-in's old name (must survive untouched).
        conn.execute(sa.text(
            "INSERT INTO skills (id, name, org_id, config, created_at, updated_at, current_version_id) VALUES "
            "(1, 'property_maintenance_intake_v1', NULL, "
            " '{\"name\": \"property_maintenance_intake_v1\", \"instructions\": \"old\"}', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL), "
            "(2, 'property_maintenance_intake_v2', NULL, "
            " '{\"name\": \"property_maintenance_intake_v2\", \"instructions\": \"new\"}', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL), "
            "(3, 'contractor_sourcing_v1', NULL, "
            " '{\"name\": \"contractor_sourcing_v1\", \"instructions\": \"c\"}', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL), "
            "(4, 'property_maintenance_response_v1', 8, "
            " '{\"name\": \"property_maintenance_response_v1\", \"instructions\": \"org own\"}', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)"
        ))
        conn.execute(sa.text(
            "INSERT INTO skill_versions (id, skill_id, version_number, config, created_by, created_at) VALUES "
            "(11, 1, 1, '{\"name\": \"property_maintenance_intake_v1\", \"instructions\": \"old\"}', NULL, '2026-01-01'), "
            "(12, 2, 1, '{\"name\": \"property_maintenance_intake_v2\", \"instructions\": \"new\"}', NULL, '2026-01-02'), "
            "(13, 3, 1, '{\"name\": \"contractor_sourcing_v1\", \"instructions\": \"c\"}', NULL, '2026-01-01'), "
            "(14, 4, 1, '{\"name\": \"property_maintenance_response_v1\", \"instructions\": \"org own\"}', NULL, '2026-01-03')"
        ))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 11 WHERE id = 1"))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 12 WHERE id = 2"))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 13 WHERE id = 3"))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 14 WHERE id = 4"))
        # acme: deployed team pinned to intake _v2's snapshot; config names old skills.
        acme_cfg = (
            '{"name": "team", "agents": [{"name": "a", "role": "r", "goal": "g", '
            '"model": "fake:ok", "skills": ["property_maintenance_intake_v2", "contractor_sourcing_v1"]}], '
            '"teams": [], "pipeline": {"steps": []}}'
        )
        conn.execute(
            sa.text(
                "INSERT INTO pipelines (id, name, org_id, config, status, created_at, updated_at) "
                "VALUES (21, 'team', 7, :cfg, 'deployed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"cfg": acme_cfg},
        )
        conn.execute(
            sa.text(
                "INSERT INTO pipeline_versions (id, pipeline_id, version_number, config, created_by, created_at) "
                "VALUES (31, 21, 1, :cfg, NULL, CURRENT_TIMESTAMP)"
            ),
            {"cfg": acme_cfg},
        )
        conn.execute(sa.text("UPDATE pipelines SET current_version_id = 31 WHERE id = 21"))
        conn.execute(sa.text(
            "INSERT INTO pipeline_dependencies "
            "(id, pipeline_version_id, resource_kind, resource_name, resource_id, resource_version_id) VALUES "
            "(41, 31, 'skill', 'property_maintenance_intake_v2', 2, 12), "
            "(42, 31, 'skill', 'contractor_sourcing_v1', 3, 13)"
        ))
        # shadow_co: its OWN skill uses a built-in old name; its team keeps the old name.
        shadow_cfg = (
            '{"name": "steam", "agents": [{"name": "a", "role": "r", "goal": "g", '
            '"model": "fake:ok", "skills": ["property_maintenance_response_v1"]}], '
            '"teams": [], "pipeline": {"steps": []}}'
        )
        conn.execute(
            sa.text(
                "INSERT INTO pipelines (id, name, org_id, config, status, created_at, updated_at) "
                "VALUES (22, 'steam', 8, :cfg, 'deployed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"cfg": shadow_cfg},
        )
        conn.execute(
            sa.text(
                "INSERT INTO pipeline_versions (id, pipeline_id, version_number, config, created_by, created_at) "
                "VALUES (32, 22, 1, :cfg, NULL, CURRENT_TIMESTAMP)"
            ),
            {"cfg": shadow_cfg},
        )
        conn.execute(sa.text("UPDATE pipelines SET current_version_id = 32 WHERE id = 22"))
        conn.execute(sa.text(
            "INSERT INTO pipeline_dependencies "
            "(id, pipeline_version_id, resource_kind, resource_name, resource_id, resource_version_id) "
            "VALUES (43, 32, 'skill', 'property_maintenance_response_v1', 4, 14)"
        ))
        # A wizard draft referencing old names must be rewritten like the head config.
        conn.execute(
            sa.text(
                "INSERT INTO builder_sessions (id, intent_text, as_is_text, specification_json, "
                "status, org_id, feedback_history, created_at, updated_at) "
                "VALUES ('sess-1', 'hi', '', :cfg, 'spec', 7, '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"cfg": acme_cfg},
        )
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            names = {r[0] for r in conn.execute(sa.text("SELECT name FROM skills WHERE org_id IS NULL"))}
            assert "property_maintenance_intake" in names
            assert "contractor_sourcing" in names
            assert not any(n.endswith("_v1") or n.endswith("_v2") for n in names)
            # Merge: one intake row; _v1's snapshot re-pointed with its id kept,
            # renumbered 1; _v2's snapshot is version 2 and remains the head.
            merged = conn.execute(sa.text(
                "SELECT id, current_version_id FROM skills "
                "WHERE name = 'property_maintenance_intake' AND org_id IS NULL"
            )).one()
            versions = conn.execute(
                sa.text(
                    "SELECT id, version_number, config FROM skill_versions "
                    "WHERE skill_id = :sid ORDER BY version_number"
                ),
                {"sid": merged.id},
            ).fetchall()
            assert [(v.id, v.version_number) for v in versions] == [(11, 1), (12, 2)]
            assert merged.current_version_id == 12
            # Snapshot-internal names tidied to the new skill name.
            assert all('"property_maintenance_intake"' in v.config for v in versions)
            # The org's own same-named skill is untouched.
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM skills WHERE org_id = 8 AND name = 'property_maintenance_response_v1'"
            )).scalar() == 1
            # acme config + dependencies rewritten; pins (resource_version_id) untouched.
            for query in (
                "SELECT config FROM pipelines WHERE id = 21",
                "SELECT config FROM pipeline_versions WHERE id = 31",
                "SELECT specification_json FROM builder_sessions WHERE id = 'sess-1'",
            ):
                blob = conn.execute(sa.text(query)).scalar()
                assert "property_maintenance_intake_v2" not in blob
                assert '"property_maintenance_intake"' in blob
                assert "contractor_sourcing_v1" not in blob
                assert '"contractor_sourcing"' in blob
            dep = conn.execute(sa.text(
                "SELECT resource_name, resource_id, resource_version_id "
                "FROM pipeline_dependencies WHERE id = 41"
            )).one()
            assert dep.resource_name == "property_maintenance_intake"
            assert dep.resource_id == merged.id and dep.resource_version_id == 12
            # shadow_co keeps the old name everywhere (config + dependency row).
            shadow = conn.execute(sa.text("SELECT config FROM pipelines WHERE id = 22")).scalar()
            assert "property_maintenance_response_v1" in shadow
            assert conn.execute(sa.text(
                "SELECT resource_name FROM pipeline_dependencies WHERE id = 43"
            )).scalar() == "property_maintenance_response_v1"
    finally:
        engine.dispose()
