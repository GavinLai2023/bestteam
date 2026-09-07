"""`admin migrate-db` (spec §11): SQLite → SQLite everywhere, SQLite → Postgres
on the lane. The source is seeded through the plain `make_engine` (no
foreign-key enforcement, like the production file) so orphans can be planted."""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]  # stamps through Alembic
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import _postgres
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.database import readonly_engine, sqlite_url_for
from ui.backend.db.inbox_events import record_events
from ui.backend.db.migrate import (
    MigrateError,
    head_revision,
    orphan_report,
    run_migration,
    stamped_revision,
)
from ui.backend.db.models import (
    Organization,
    PipelineRecord,
    PipelineVersion,
    Run,
    TraceEventRecord,
    UsageRecord,
)
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.pipelines import publish_pipeline_version
from ui.backend.db.users import create_user

_ROOT = Path(__file__).resolve().parent.parent


def _stamp_head(url: str) -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.stamp(cfg, "head")


def _seed_source(path: Path) -> str:
    """Every kind of row the copy has to get right, plus the two orphan kinds
    the development database showed on 2026-09-07."""
    url = sqlite_url_for(path)
    engine = make_engine(url)
    init_db(engine)
    with session_factory(engine)() as db:
        org = get_or_create_org(db, "acme", "Acme")
        create_user(db, "alice", "pw", org_id=org.id)
        db.add(PipelineRecord(name="team", org_id=org.id, config={"agents": [{"name": "a"}]}, status="deployed"))
        db.add(Run(id="run-1", pipeline="team", input="hello", org_id=org.id, username="alice", status="completed"))
        db.add(Run(id="run-2", pipeline="team", input="again", org_id=org.id, username="alice",
                   status="failed", retry_of_run_id="run-1"))
        db.add(TraceEventRecord(run_id="run-1", seq=0, type="run_queued"))
        db.add(TraceEventRecord(run_id="run-2", seq=0, type="run_failed", data='"x"'))
        db.add(UsageRecord(run_id="run-1", org_id=org.id, agent="a", model="fake:x", input_tokens=1, output_tokens=2))
        db.add(UsageRecord(run_id="ghost", org_id=org.id, agent="a", model="fake:x"))        # nullable FK dangles
        db.add(TraceEventRecord(run_id="ghost-2", seq=0, type="run_failed"))                  # NOT NULL FK dangles
        record_events(db, org_id=org.id, mailbox_identity="m", mailbox_generation="g", external_ids=["1", "2"])
        db.commit()
    engine.dispose()
    _stamp_head(url)
    return url


def test_orphan_report_names_both_kinds(tmp_path):
    url = _seed_source(tmp_path / "src.db")
    engine = make_engine(url)
    try:
        found = {(o.table, o.column, o.parent, o.rows, o.nullable) for o in orphan_report(engine)}
        assert stamped_revision(engine) == head_revision()
    finally:
        engine.dispose()
    assert found == {
        ("usage_records", "run_id", "runs", 1, True),
        ("trace_events", "run_id", "runs", 1, False),
    }


def test_orphan_report_handles_self_referential_keys(tmp_path):
    # `runs.retry_of_run_id` points at `runs.id`; without an alias on the parent
    # table the subquery keeps its own `FROM runs` and both sides resolve to
    # it, so the EXISTS is an uncorrelated, table-global check that is false on
    # ordinary data and every retry run counts as an orphan. `run-2` (a real
    # parent) must stay silent while a retry of a missing run is reported.
    url = _seed_source(tmp_path / "src.db")
    engine = make_engine(url)
    try:
        with session_factory(engine)() as db:
            org_id = db.query(Organization).one().id
            db.add(Run(id="run-3", pipeline="team", input="x", org_id=org_id, username="alice",
                       status="failed", retry_of_run_id="ghost-3"))
            db.commit()
        found = {(o.table, o.column, o.rows) for o in orphan_report(engine)}
    finally:
        engine.dispose()
    assert ("runs", "retry_of_run_id", 1) in found
    assert ("usage_records", "run_id", 1) in found
    assert ("trace_events", "run_id", 1) in found
    assert len(found) == 3


def test_an_unstamped_source_has_no_revision(tmp_path):
    engine = make_engine(tmp_path / "plain.db")
    init_db(engine)
    try:
        assert stamped_revision(engine) is None
    finally:
        engine.dispose()


def _count(url: str, model) -> int:
    engine = make_engine(url)
    try:
        with session_factory(engine)() as db:
            return db.query(model).count()
    finally:
        engine.dispose()


def test_refuses_orphans_without_the_flag_and_writes_no_rows(tmp_path):
    src = _seed_source(tmp_path / "src.db")
    target = sqlite_url_for(tmp_path / "dst.db")
    with pytest.raises(MigrateError, match="fix-orphans"):
        run_migration(src, target, log=lambda _line: None)
    if (tmp_path / "dst.db").exists():
        engine = readonly_engine(target)
        try:
            assert sa.inspect(engine).get_table_names() == []
        finally:
            engine.dispose()


def test_copies_everything_with_orphans_fixed_and_the_source_untouched(tmp_path):
    src_path = tmp_path / "src.db"
    src = _seed_source(src_path)
    before = src_path.read_bytes()
    target = sqlite_url_for(tmp_path / "dst.db")
    lines = []

    assert run_migration(src, target, fix_orphans=True, log=lines.append) == 0

    assert src_path.read_bytes() == before
    assert _count(target, Organization) == 1
    assert _count(target, Run) == 2
    assert _count(target, TraceEventRecord) == 2   # ghost-2 skipped (NOT NULL key)
    assert _count(target, UsageRecord) == 2        # ghost kept, run_id set to NULL
    engine = make_engine(target)
    try:
        with session_factory(engine)() as db:
            assert db.get(Run, "run-2").retry_of_run_id == "run-1"
            assert db.query(UsageRecord).filter_by(run_id=None).count() == 1
            assert db.get(Organization, 1).name == "acme"  # primary keys preserved
    finally:
        engine.dispose()
    assert any("verified" in line for line in lines)
    assert any("trace_events" in line and "1 skipped" in line for line in lines)


def test_refuses_a_non_empty_target_and_the_same_url(tmp_path):
    src = _seed_source(tmp_path / "src.db")
    other = _seed_source(tmp_path / "other.db")
    with pytest.raises(MigrateError, match="already holds rows"):
        run_migration(src, other, fix_orphans=True, log=lambda _line: None)
    with pytest.raises(MigrateError, match="same database"):
        run_migration(src, src, fix_orphans=True, log=lambda _line: None)


def test_refuses_a_source_behind_head(tmp_path):
    src_path = tmp_path / "old.db"
    engine = make_engine(src_path)
    init_db(engine)
    engine.dispose()  # tables, but no Alembic stamp
    with pytest.raises(MigrateError, match="alembic upgrade head"):
        run_migration(sqlite_url_for(src_path), sqlite_url_for(tmp_path / "dst.db"), log=lambda _line: None)


def test_refuses_an_in_memory_database_on_either_side(tmp_path):
    src = _seed_source(tmp_path / "src.db")
    with pytest.raises(MigrateError, match="in-memory"):
        run_migration("sqlite:///:memory:", sqlite_url_for(tmp_path / "dst.db"), log=lambda _line: None)
    with pytest.raises(MigrateError, match="in-memory"):
        run_migration(src, "sqlite:///:memory:", log=lambda _line: None)
    assert not (tmp_path / "dst.db").exists()


def test_copies_the_head_pointers_across_the_version_cycle(tmp_path):
    # `pipelines.current_version_id` points at `pipeline_versions`, whose
    # `pipeline_id` points back: no table order satisfies both, so the copy
    # writes the head pointer as NULL first and patches it once the versions
    # are in. A lost pointer here would silently un-deploy every team.
    src_path = tmp_path / "src.db"
    src = _seed_source(src_path)
    engine = make_engine(src)
    try:
        with session_factory(engine)() as db:
            org_id = db.query(Organization).one().id
            record, version = publish_pipeline_version(
                db, org_id=org_id, name="team", config={"agents": [{"name": "a"}]}
            )
            db.commit()
            expected = (record.id, version.id)
            assert record.current_version_id == version.id
    finally:
        engine.dispose()
    target = sqlite_url_for(tmp_path / "dst.db")
    lines = []
    assert run_migration(src, target, fix_orphans=True, log=lines.append) == 0
    engine = make_engine(target)
    try:
        with session_factory(engine)() as db:
            assert db.get(PipelineRecord, expected[0]).current_version_id == expected[1]
            assert db.get(PipelineVersion, expected[1]).pipeline_id == expected[0]
    finally:
        engine.dispose()
    assert not any("pipelines:" in line and "nulled" in line for line in lines)
