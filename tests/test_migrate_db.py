"""`admin migrate-db` (spec §11): SQLite → SQLite everywhere, SQLite → Postgres
on the lane. The source is seeded through the plain `make_engine` (no
foreign-key enforcement, like the production file) so orphans can be planted."""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]  # stamps through Alembic
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

from alembic import command
from alembic.config import Config

import _postgres
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.database import sqlite_url_for
from ui.backend.db.inbox_events import record_events
from ui.backend.db.migrate import MigrateError, head_revision, orphan_report, stamped_revision
from ui.backend.db.models import Organization, PipelineRecord, Run, TraceEventRecord, UsageRecord
from ui.backend.db.orgs import get_or_create_org
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
    # table the EXISTS compiles as a self-comparison and every retry run
    # counts as an orphan. `run-2` (a real parent) must stay silent while a
    # retry of a missing run is reported.
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
