"""Phase 3b: retention settings, the purge engine, and export."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.orgs import create_org
from ui.backend.db.retention import (
    get_retention_settings,
    orgs_with_retention,
    record_sweep,
    set_retention_days,
)


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_retention_is_unset_until_configured(db):
    org = create_org(db, "acme")
    assert get_retention_settings(db, org.id) is None
    assert orgs_with_retention(db) == []


def test_set_and_clear_retention_days(db):
    org = create_org(db, "acme")

    row = set_retention_days(db, org.id, 30)
    db.commit()
    assert row.run_retention_days == 30
    assert orgs_with_retention(db) == [(org.id, 30)]

    set_retention_days(db, org.id, None)
    db.commit()
    # The row survives (it carries sweep history); the policy is off.
    assert get_retention_settings(db, org.id).run_retention_days is None
    assert orgs_with_retention(db) == []


def test_record_sweep_stamps_history(db):
    from datetime import datetime, timezone

    org = create_org(db, "acme")
    set_retention_days(db, org.id, 7)
    at = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

    record_sweep(db, org.id, purged=4, at=at)
    db.commit()

    row = get_retention_settings(db, org.id)
    assert row.last_purged_count == 4
    assert row.last_swept_at.replace(tzinfo=timezone.utc) == at


from datetime import datetime, timedelta, timezone

from ui.backend.db.models import (
    AutomationItemResult,
    Run,
    TraceEventRecord,
    UsageRecord,
)
from ui.backend.retention import purge_org_runs, purge_run


def _run(db, org_id, *, run_id="r1", status="completed", age_days=0):
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    run = Run(
        id=run_id, workflow="support", input="From alice@example.com: my boiler leaks",
        output="Drafted a reply to alice@example.com", status=status,
        org_id=org_id, created_at=created,
    )
    db.add(run)
    db.add(TraceEventRecord(run_id=run_id, seq=1, type="agent_completed",
                            agent="writer", data='{"text": "alice@example.com"}'))
    db.add(UsageRecord(run_id=run_id, agent="writer", model="fake:",
                       input_tokens=10, output_tokens=5, org_id=org_id))
    db.add(AutomationItemResult(
        org_id=org_id, run_id=run_id, source_key="mbx:7", result_type="email",
        status="processed", needs_attention=False,
        payload={"sender": "alice@example.com", "summary": "boiler"},
    ))
    db.flush()
    return run


def test_purge_clears_content(db):
    org = create_org(db, "acme")
    run = _run(db, org.id)

    assert purge_run(db, run) is True
    db.commit()

    assert run.input == ""
    assert run.output is None
    assert run.content_purged_at is not None
    assert db.query(TraceEventRecord).filter_by(run_id=run.id).count() == 0
    assert db.query(AutomationItemResult).filter_by(run_id=run.id).one().payload == {}


def test_purge_keeps_the_accounting(db):
    """I2: usage rows and the run row itself survive -- they are the org's
    cost history, not email content."""
    org = create_org(db, "acme")
    run = _run(db, org.id)

    purge_run(db, run)
    db.commit()

    assert db.get(Run, run.id) is not None
    usage = db.query(UsageRecord).filter_by(run_id=run.id).one()
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)


def test_purge_keeps_item_status_and_source_key(db):
    """I1: clearing these would make a sweep cause duplicate drafts, because
    automation_results.py excludes already-drafted UIDs by exactly these two
    fields."""
    org = create_org(db, "acme")
    run = _run(db, org.id)

    purge_run(db, run)
    db.commit()

    item = db.query(AutomationItemResult).filter_by(run_id=run.id).one()
    assert item.source_key == "mbx:7"
    assert item.status == "processed"


def test_purge_refuses_a_running_run(db):
    """I3: the worker is still writing trace events."""
    org = create_org(db, "acme")
    run = _run(db, org.id, status="running")

    assert purge_run(db, run) is False
    assert run.input != ""


def test_purge_is_idempotent(db):
    """I4: the sweep re-selects rows on overlapping cycles."""
    org = create_org(db, "acme")
    run = _run(db, org.id)

    assert purge_run(db, run) is True
    db.commit()
    first = run.content_purged_at

    assert purge_run(db, run) is False
    db.commit()
    assert run.content_purged_at == first


def test_purge_org_runs_respects_the_cutoff(db):
    org = create_org(db, "acme")
    _run(db, org.id, run_id="old", age_days=40)
    _run(db, org.id, run_id="new", age_days=2)

    assert purge_org_runs(db, org_id=org.id, older_than_days=30) == 1
    db.commit()

    assert db.get(Run, "old").content_purged_at is not None
    assert db.get(Run, "new").content_purged_at is None


def test_purge_org_runs_is_scoped_to_one_org(db):
    a = create_org(db, "acme")
    b = create_org(db, "beta")
    _run(db, a.id, run_id="a1", age_days=40)
    _run(db, b.id, run_id="b1", age_days=40)

    assert purge_org_runs(db, org_id=a.id, older_than_days=30) == 1
    db.commit()

    assert db.get(Run, "b1").content_purged_at is None


def test_purge_org_runs_zero_days_takes_everything_terminal(db):
    org = create_org(db, "acme")
    _run(db, org.id, run_id="done", age_days=0)
    _run(db, org.id, run_id="live", age_days=0, status="running")

    assert purge_org_runs(db, org_id=org.id, older_than_days=0) == 1
    db.commit()

    assert db.get(Run, "done").content_purged_at is not None
    assert db.get(Run, "live").content_purged_at is None


def test_sweep_applies_each_orgs_own_policy(db):
    from ui.backend.db.retention import set_retention_days
    from ui.backend.retention import sweep_retention

    a = create_org(db, "acme")
    b = create_org(db, "beta")
    set_retention_days(db, a.id, 30)
    _run(db, a.id, run_id="a-old", age_days=40)
    _run(db, b.id, run_id="b-old", age_days=40)  # no policy at all
    db.commit()

    assert sweep_retention(db) == 1

    assert db.get(Run, "a-old").content_purged_at is not None
    assert db.get(Run, "b-old").content_purged_at is None  # I5: NULL keeps forever


def test_sweep_records_that_it_ran(db):
    from ui.backend.db.retention import get_retention_settings, set_retention_days
    from ui.backend.retention import sweep_retention

    org = create_org(db, "acme")
    set_retention_days(db, org.id, 30)
    _run(db, org.id, run_id="old", age_days=40)
    db.commit()

    sweep_retention(db)

    row = get_retention_settings(db, org.id)
    assert row.last_swept_at is not None
    assert row.last_purged_count == 1


def test_export_carries_the_content(db):
    from ui.backend.retention import export_org_runs

    org = create_org(db, "acme")
    _run(db, org.id, run_id="r1")
    db.commit()

    bundle = export_org_runs(db, org_id=org.id)

    assert bundle["truncated"] is False
    run = bundle["runs"][0]
    assert run["id"] == "r1"
    assert "alice@example.com" in run["output"]
    assert run["trace_events"][0]["data"] == '{"text": "alice@example.com"}'
    assert run["automation_item_results"][0]["payload"]["sender"] == "alice@example.com"


def test_export_is_scoped_to_one_org(db):
    from ui.backend.retention import export_org_runs

    a = create_org(db, "acme")
    b = create_org(db, "beta")
    _run(db, a.id, run_id="a1")
    _run(db, b.id, run_id="b1")
    db.commit()

    assert [r["id"] for r in export_org_runs(db, org_id=a.id)["runs"]] == ["a1"]


def test_export_flags_truncation(db):
    from ui.backend.retention import export_org_runs

    org = create_org(db, "acme")
    for i in range(3):
        _run(db, org.id, run_id=f"r{i}", age_days=i)
    db.commit()

    bundle = export_org_runs(db, org_id=org.id, limit=2)

    assert bundle["truncated"] is True
    assert len(bundle["runs"]) == 2
    assert bundle["oldest_included"] is not None


def test_export_covers_everything_purge_clears(db):
    """The coupling that makes deletion safe: if a field is added to the purge
    and not to the export, the export silently stops being a way out."""
    from ui.backend.retention import PURGED_FIELDS, export_org_runs

    org = create_org(db, "acme")
    _run(db, org.id, run_id="r1")
    db.commit()

    run = export_org_runs(db, org_id=org.id)["runs"][0]

    for field in PURGED_FIELDS["runs"]:
        assert field in run
    assert "trace_events" in run
    for field in PURGED_FIELDS["automation_item_results"]:
        assert field in run["automation_item_results"][0]
