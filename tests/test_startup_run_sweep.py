"""A hard restart must not leave `runs` rows `running` forever (beta B1).

The run executor is per-process, so any row still `running` when the app
starts belongs to a worker that no longer exists. Without a sweep the
Activity page shows that run as running indefinitely, its Retry path (gated
on `failed`) never appears, and -- for an email-triggered run -- the inbox
events it had claimed stay `claimed`, so the next poll never reprocesses them.
"""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import InboxEvent, Run
from ui.backend.db.orgs import get_or_create_org
from ui.backend.runtime import INTERRUPTED_RUN_MESSAGE, fail_interrupted_runs


def _seed(Session):
    with Session() as db:
        db.add(Run(id="r-running", pipeline="w", input="in", status="running"))
        db.add(Run(id="r-done", pipeline="w", input="in", status="completed", output="ok"))
        db.add(Run(id="r-failed", pipeline="w", input="in", status="failed", output="boom"))
        db.add(Run(id="r-cancelled", pipeline="w", input="in", status="cancelled", output="stop"))
        db.commit()


def test_fail_interrupted_runs_marks_running_rows_failed_and_leaves_terminal_rows_alone():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    _seed(Session)

    assert fail_interrupted_runs(engine, max_event_attempts=3) == 1

    with Session() as db:
        swept = db.get(Run, "r-running")
        assert swept.status == "failed"
        assert swept.output == INTERRUPTED_RUN_MESSAGE
        assert db.get(Run, "r-done").output == "ok"
        assert db.get(Run, "r-failed").output == "boom"
        assert db.get(Run, "r-cancelled").output == "stop"

    # Idempotent: a second start has nothing left to sweep.
    assert fail_interrupted_runs(engine, max_event_attempts=3) == 0


def test_fail_interrupted_runs_releases_the_inbox_events_the_dead_run_had_claimed():
    # The same infrastructure-class treatment `_release_stale_run` gives a
    # hung run: the messages are innocent and nothing ever reached a terminal
    # event for them, so hand them back (or dead-letter the exhausted ones)
    # rather than leaving them `claimed` by a run that will never finish.
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        db.add(Run(id="r-triggered", pipeline="w", input="in", status="running", org_id=org.id))
        db.add(InboxEvent(
            org_id=org.id, mailbox_identity="inbox@acme.test", external_id="101",
            status="claimed", run_id="r-triggered", attempts=1,
        ))
        db.add(InboxEvent(
            org_id=org.id, mailbox_identity="inbox@acme.test", external_id="102",
            status="claimed", run_id="r-triggered", attempts=3,
        ))
        db.commit()

    assert fail_interrupted_runs(engine, max_event_attempts=3) == 1

    with Session() as db:
        fresh = db.query(InboxEvent).filter_by(external_id="101").one()
        assert fresh.status == "pending"
        assert fresh.run_id is None
        assert fresh.last_error == INTERRUPTED_RUN_MESSAGE
        exhausted = db.query(InboxEvent).filter_by(external_id="102").one()
        assert exhausted.status == "failed"  # dead-lettered, not looped forever
        assert exhausted.last_error == INTERRUPTED_RUN_MESSAGE


def test_lifespan_sweeps_orphaned_running_runs(monkeypatch):
    from ui.backend import main as backend_main

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    _seed(Session)
    monkeypatch.setattr(backend_main, "SessionLocal", Session)

    with TestClient(backend_main.app):
        pass

    with Session() as db:
        assert db.get(Run, "r-running").status == "failed"
        assert db.get(Run, "r-done").status == "completed"


def test_a_claim_whose_run_row_was_never_written_is_released():
    """The narrow window `fail_interrupted_runs`'s `running` query cannot see.

    `_start_triggered_run` commits the claim on its own -- so a build failure
    can release it penalty-free -- then builds the pipeline, and only then
    writes the `runs` row. A process killed inside the build leaves `claimed`
    rows whose `run_id` names a row that does not exist, so no `running` run
    ever points at them and nothing would ever hand them back: they would be
    `claimed` forever, invisible to both `claim_events` and
    `has_pending_events`.
    """
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        db.add(InboxEvent(
            org_id=org.id, mailbox_identity="inbox@acme.test", external_id="201",
            status="claimed", run_id="r-never-written", attempts=0,
        ))
        db.add(InboxEvent(
            org_id=org.id, mailbox_identity="inbox@acme.test", external_id="202",
            status="claimed", run_id="r-never-written", attempts=3,
        ))
        db.commit()

    fail_interrupted_runs(engine, max_event_attempts=3)

    with Session() as db:
        fresh = db.query(InboxEvent).filter_by(external_id="201").one()
        assert fresh.status == "pending"
        assert fresh.run_id is None
        exhausted = db.query(InboxEvent).filter_by(external_id="202").one()
        assert exhausted.status == "failed"  # dead-lettered like any other


def test_a_claim_left_by_a_run_that_already_reached_a_terminal_status_is_released():
    """A `completed`/`failed` run's claim is orphaned just as thoroughly.

    `complete_events` is what resolves a run's claims, and it runs on the
    worker thread. A kill between the run row's terminal commit and that call
    leaves the row terminal and its claims outstanding, which the `running`
    query also cannot see.
    """
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        db.add(Run(id="r-terminal", pipeline="w", input="in", status="failed",
                   output="boom", org_id=org.id))
        db.add(InboxEvent(
            org_id=org.id, mailbox_identity="inbox@acme.test", external_id="301",
            status="claimed", run_id="r-terminal", attempts=0,
        ))
        db.commit()

    fail_interrupted_runs(engine, max_event_attempts=3)

    with Session() as db:
        assert db.query(InboxEvent).filter_by(external_id="301").one().status == "pending"
        # The terminal run itself is untouched -- history stays immutable.
        assert db.get(Run, "r-terminal").output == "boom"


def test_the_orphan_sweep_leaves_pending_and_completed_events_alone():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        for external_id, status in (("401", "pending"), ("402", "done"),
                                    ("403", "failed"), ("404", "filtered")):
            db.add(InboxEvent(
                org_id=org.id, mailbox_identity="inbox@acme.test",
                external_id=external_id, status=status,
            ))
        db.commit()

    fail_interrupted_runs(engine, max_event_attempts=3)

    with Session() as db:
        rows = {e.external_id: e.status for e in db.query(InboxEvent).all()}
    assert rows == {"401": "pending", "402": "done", "403": "failed", "404": "filtered"}
