"""run_in_background must not double-insert a runs row the caller pre-persisted."""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from bestteam import AgentSpec, Specification, TeamSpec, PipelineSpec, validate_specification
from helpers import make_concurrent_safe_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run
from ui.backend.runtime import registry, run_in_background


def _engine(tmp_path):
    e = make_concurrent_safe_engine(tmp_path)
    init_db(e)
    return e


def _pipeline(tmp_path):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    return validate_specification(spec, source=tmp_path / "w.yaml")


def test_reuses_preexisting_run_row_and_sets_terminal_status(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    # registry.create() is what publishes the in-memory Run the worker thread
    # publishes trace events against (see test_usage_metering.py); its id is
    # what a real caller would also use as the durable `runs` row's primary
    # key, so reuse it here rather than a disconnected literal.
    run = registry.create("w", "in", username="email-trigger")
    with Session() as s:
        s.add(Run(id=run.id, pipeline="w", input="in", status="running", username="email-trigger"))
        s.commit()
    wf = _pipeline(tmp_path)
    run_in_background(run.id, wf, "in", engine=engine, username="email-trigger")
    with Session() as s:
        rows = s.query(Run).filter_by(id=run.id).all()
        assert len(rows) == 1  # not double-inserted
        assert rows[0].status in ("completed", "failed")
        assert rows[0].username == "email-trigger"


def test_run_in_background_stamps_pipeline_version_id(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    wf = _pipeline(tmp_path)
    run = registry.create("w", "in")

    run_in_background(run.id, wf, "in", engine=engine, pipeline_version_id=42)

    with Session() as s:
        assert s.get(Run, run.id).pipeline_version_id == 42


def test_run_in_background_leaves_version_null_when_absent(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    wf = _pipeline(tmp_path)
    run = registry.create("w", "in")

    run_in_background(run.id, wf, "in", engine=engine)

    with Session() as s:
        assert s.get(Run, run.id).pipeline_version_id is None


# ---------------------------------------------------------------------------
# Phase 0 (0.5): a triggered run's outcome must reach the trigger's health.
#
# runtime.py never referenced EmailTrigger at all: _start_triggered_run cleared
# last_error on dispatch and nothing ever wrote it back, so a mailbox whose
# pipeline failed on every single run kept reporting "Active" with no error
# indefinitely. Only the mailbox connectivity path could ever set a fault.
# ---------------------------------------------------------------------------


def _failing_pipeline(tmp_path):
    """A pipeline whose only agent raises, so the run reaches `failed`."""
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    wf = validate_specification(spec, source=tmp_path / "w.yaml")

    def _boom(*a, **k):
        raise RuntimeError("model exploded")

    wf.stream = _boom
    return wf


def _org_with_trigger(session):
    from ui.backend.db.email_triggers import upsert_email_trigger
    from ui.backend.db.orgs import get_or_create_org

    org = get_or_create_org(session, "acme")
    trigger = upsert_email_trigger(
        session, org.id, pipeline_name="w", enabled=True, last_uid=1, uidvalidity=3
    )
    session.commit()
    return org, trigger


def _triggered_run_row(run_id, org_id):
    return Run(
        id=run_id, pipeline="w", input="in", status="running", org_id=org_id,
        username="email-trigger",
        trigger_context={"trigger_type": "email", "uids": [42], "uidvalidity": 3},
    )


def test_a_failed_triggered_run_records_a_fault_on_the_trigger(tmp_path):
    from ui.backend.db.email_triggers import get_email_trigger

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()

    run_in_background(
        run.id, _failing_pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error_kind == "workflow"
        assert trigger.last_error  # a customer-readable message, not empty
        assert s.get(Run, run.id).status == "failed"


def test_a_successful_triggered_run_clears_a_pipeline_fault(tmp_path):
    from ui.backend.db.email_triggers import get_email_trigger

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, trigger = _org_with_trigger(s)
        org_id = org.id
        trigger.last_error = "an earlier failure"
        trigger.last_error_kind = "workflow"
        s.commit()
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()

    run_in_background(
        run.id, _pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error is None
        assert trigger.last_error_kind is None


def test_a_successful_triggered_run_leaves_a_mailbox_fault_alone(tmp_path):
    # A mailbox-kind fault is owned by the poller's own connectivity check and
    # auto-clears there; a pipeline outcome says nothing about it.
    from ui.backend.db.email_triggers import get_email_trigger

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, trigger = _org_with_trigger(s)
        org_id = org.id
        trigger.last_error = "mailbox unreachable"
        trigger.last_error_kind = "mailbox"
        s.commit()
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()

    run_in_background(
        run.id, _pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error == "mailbox unreachable"
        assert trigger.last_error_kind == "mailbox"


def test_a_superseded_run_never_touches_trigger_health(tmp_path):
    # The stale-run watchdog can release a wedged run's overlap guard and let a
    # new run start while the old one is still executing. When the old one
    # finally finishes, its outcome is stale: applying it would let a failure
    # from the abandoned run overwrite health the new run just established.
    from ui.backend.db.email_triggers import get_email_trigger

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, trigger = _org_with_trigger(s)
        org_id = org.id
        trigger.last_run_id = "the-run-we-are-waiting-on"
        s.commit()
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()

    run_in_background(
        run.id, _failing_pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error is None
        assert trigger.last_error_kind is None


def test_the_run_the_trigger_is_waiting_on_still_updates_health(tmp_path):
    from ui.backend.db.email_triggers import get_email_trigger

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, trigger = _org_with_trigger(s)
        org_id = org.id
        s.commit()
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        trigger.last_run_id = run.id
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()

    run_in_background(
        run.id, _failing_pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error_kind == "workflow"


def test_a_non_triggered_run_never_touches_trigger_health(tmp_path):
    from ui.backend.db.email_triggers import get_email_trigger

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, trigger = _org_with_trigger(s)
        org_id = org.id
        trigger.last_error = "an earlier failure"
        trigger.last_error_kind = "workflow"
        s.commit()
    run = registry.create("w", "in", org_id=org_id, username="alice")
    with Session() as s:
        # No trigger_context: an ordinary human-started run.
        s.add(Run(id=run.id, pipeline="w", input="in", status="running",
                  org_id=org_id, username="alice"))
        s.commit()

    run_in_background(run.id, _pipeline(tmp_path), "in", engine=engine, org_id=org_id)

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error == "an earlier failure"


# --- durable inbox events: terminal outcomes ---------------------------------


def _claimed_events(session, org_id, run_id, uids):
    from ui.backend.db import inbox_events as store

    store.record_events(session, org_id=org_id, mailbox_identity="m",
                        mailbox_generation="3", external_ids=uids)
    session.commit()
    store.claim_events(session, org_id=org_id, run_id=run_id, limit=len(uids),
                       mailbox_identity="m", mailbox_generation="3")
    session.commit()


def test_a_completed_run_marks_its_events_done(tmp_path):
    from ui.backend.db.models import InboxEvent
    from ui.backend import runtime

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id
        row = _triggered_run_row("run-done", org_id)
        row.trigger_context = {"trigger_type": "email", "uids": [11, 12], "uidvalidity": 3}
        row.status = "completed"
        s.add(row)
        s.commit()
        _claimed_events(s, org_id, "run-done", ["11", "12"])

        runtime._safe_complete_inbox_events(s, row)
        assert {e.status for e in s.query(InboxEvent)} == {"done"}


def test_a_failed_run_keeps_drafted_messages_done_and_fails_the_rest(tmp_path):
    from ui.backend.db.models import InboxEvent, TraceEventRecord
    from ui.backend import runtime
    import json

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id
        row = _triggered_run_row("run-part", org_id)
        row.trigger_context = {"trigger_type": "email", "uids": [11, 12], "uidvalidity": 3}
        row.status = "failed"
        s.add(row)
        s.commit()
        _claimed_events(s, org_id, "run-part", ["11", "12"])
        # Phase 0's evidence layer is what decides: a draft demonstrably exists
        # for 11, so reprocessing it would create a second draft.
        s.add(TraceEventRecord(
            run_id="run-part", seq=1, type="tool_completed",
            data=json.dumps({"tool": "email_draft_reply", "outcome": "draft_created",
                             "message_id": "11"}),
        ))
        s.commit()

        runtime._safe_complete_inbox_events(s, row)
        rows = {e.external_id: e.status for e in s.query(InboxEvent)}
        assert rows == {"11": "done", "12": "failed"}


def test_a_completion_failure_never_breaks_the_run(tmp_path, monkeypatch):
    from ui.backend import runtime

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        row = _triggered_run_row("run-boom", org.id)
        row.status = "completed"
        s.add(row)
        s.commit()

        def _explode(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(runtime, "complete_events", _explode)
        runtime._safe_complete_inbox_events(s, row)  # must not raise


def test_a_real_triggered_run_completes_its_events_end_to_end(tmp_path):
    from ui.backend.db.models import InboxEvent

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()
        _claimed_events(s, org_id, run.id, ["42"])

    run_in_background(
        run.id, _pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        assert s.query(InboxEvent).one().status == "done"


def test_repeated_run_failures_notify_once_at_the_threshold(tmp_path, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger
    from ui.backend.db.notifications import list_notifications

    monkeypatch.setenv("BESTTEAM_TRIGGER_ALERT_THRESHOLD", "2")
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id

    for index in range(3):
        run = registry.create("w", "in", org_id=org_id, username="email-trigger")
        with Session() as s:
            trigger = get_email_trigger(s, org_id)
            trigger.last_run_id = run.id
            s.add(_triggered_run_row(run.id, org_id))
            s.commit()
        run_in_background(
            run.id, _failing_pipeline(tmp_path), "in",
            engine=engine, org_id=org_id, username="email-trigger",
        )

    with Session() as s:
        emitted = list_notifications(s, org_id)
        trigger = get_email_trigger(s, org_id)
    # Three failures, one alert: the fingerprint suppresses the rest until the
    # condition clears.
    assert [n.fingerprint for n in emitted] == ["workflow"]
    assert trigger.consecutive_faults == 3


def test_a_successful_run_announces_the_recovery(tmp_path, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger
    from ui.backend.db.notifications import list_notifications

    monkeypatch.setenv("BESTTEAM_TRIGGER_ALERT_THRESHOLD", "1")
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id

    failing = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        get_email_trigger(s, org_id).last_run_id = failing.id
        s.add(_triggered_run_row(failing.id, org_id))
        s.commit()
    run_in_background(
        failing.id, _failing_pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    good = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        get_email_trigger(s, org_id).last_run_id = good.id
        s.add(_triggered_run_row(good.id, org_id))
        s.commit()
    run_in_background(
        good.id, _pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        emitted = list_notifications(s, org_id)
        trigger = get_email_trigger(s, org_id)
    assert [n.fingerprint for n in emitted] == ["recovered", "workflow"]
    assert trigger.alerted_fingerprint is None
    assert trigger.consecutive_faults == 0


# --- draft outcomes: recorded at finalization --------------------------------


def test_a_terminal_triggered_run_records_draft_outcomes(tmp_path, monkeypatch):
    from ui.backend import runtime

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()

    recorded = []
    monkeypatch.setattr(
        runtime, "record_outcomes_for_run", lambda db, row: recorded.append(row.id)
    )
    run_in_background(
        run.id, _pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )
    assert recorded == [run.id]


def test_a_draft_outcome_failure_never_breaks_the_run(tmp_path, monkeypatch):
    from ui.backend import runtime

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    with Session() as s:
        org, _ = _org_with_trigger(s)
        org_id = org.id
    run = registry.create("w", "in", org_id=org_id, username="email-trigger")
    with Session() as s:
        s.add(_triggered_run_row(run.id, org_id))
        s.commit()

    def _explode(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(runtime, "record_outcomes_for_run", _explode)
    run_in_background(
        run.id, _pipeline(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )
    with Session() as s:
        assert s.get(Run, run.id).status == "completed"
