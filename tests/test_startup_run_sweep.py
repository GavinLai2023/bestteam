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
        db.add(Run(id="r-running", workflow="w", input="in", status="running"))
        db.add(Run(id="r-done", workflow="w", input="in", status="completed", output="ok"))
        db.add(Run(id="r-failed", workflow="w", input="in", status="failed", output="boom"))
        db.add(Run(id="r-cancelled", workflow="w", input="in", status="cancelled", output="stop"))
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
        db.add(Run(id="r-triggered", workflow="w", input="in", status="running", org_id=org.id))
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
