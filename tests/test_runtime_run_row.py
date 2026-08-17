"""run_in_background must not double-insert a runs row the caller pre-persisted."""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from bestteam import AgentSpec, Specification, TeamSpec, WorkflowSpec, validate_specification
from helpers import make_concurrent_safe_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run
from ui.backend.runtime import registry, run_in_background


def _engine(tmp_path):
    e = make_concurrent_safe_engine(tmp_path)
    init_db(e)
    return e


def _workflow(tmp_path):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        workflow=WorkflowSpec(steps=["t"]),
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
        s.add(Run(id=run.id, workflow="w", input="in", status="running", username="email-trigger"))
        s.commit()
    wf = _workflow(tmp_path)
    run_in_background(run.id, wf, "in", engine=engine, username="email-trigger")
    with Session() as s:
        rows = s.query(Run).filter_by(id=run.id).all()
        assert len(rows) == 1  # not double-inserted
        assert rows[0].status in ("completed", "failed")
        assert rows[0].username == "email-trigger"


def test_run_in_background_stamps_workflow_version_id(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    wf = _workflow(tmp_path)
    run = registry.create("w", "in")

    run_in_background(run.id, wf, "in", engine=engine, workflow_version_id=42)

    with Session() as s:
        assert s.get(Run, run.id).workflow_version_id == 42


def test_run_in_background_leaves_version_null_when_absent(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    wf = _workflow(tmp_path)
    run = registry.create("w", "in")

    run_in_background(run.id, wf, "in", engine=engine)

    with Session() as s:
        assert s.get(Run, run.id).workflow_version_id is None


# ---------------------------------------------------------------------------
# Phase 0 (0.5): a triggered run's outcome must reach the trigger's health.
#
# runtime.py never referenced EmailTrigger at all: _start_triggered_run cleared
# last_error on dispatch and nothing ever wrote it back, so a mailbox whose
# workflow failed on every single run kept reporting "Active" with no error
# indefinitely. Only the mailbox connectivity path could ever set a fault.
# ---------------------------------------------------------------------------


def _failing_workflow(tmp_path):
    """A workflow whose only agent raises, so the run reaches `failed`."""
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        workflow=WorkflowSpec(steps=["t"]),
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
        session, org.id, workflow_name="w", enabled=True, last_uid=1, uidvalidity=3
    )
    session.commit()
    return org, trigger


def _triggered_run_row(run_id, org_id):
    return Run(
        id=run_id, workflow="w", input="in", status="running", org_id=org_id,
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
        run.id, _failing_workflow(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error_kind == "workflow"
        assert trigger.last_error  # a customer-readable message, not empty
        assert s.get(Run, run.id).status == "failed"


def test_a_successful_triggered_run_clears_a_workflow_fault(tmp_path):
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
        run.id, _workflow(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error is None
        assert trigger.last_error_kind is None


def test_a_successful_triggered_run_leaves_a_mailbox_fault_alone(tmp_path):
    # A mailbox-kind fault is owned by the poller's own connectivity check and
    # auto-clears there; a workflow outcome says nothing about it.
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
        run.id, _workflow(tmp_path), "in",
        engine=engine, org_id=org_id, username="email-trigger",
    )

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error == "mailbox unreachable"
        assert trigger.last_error_kind == "mailbox"


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
        s.add(Run(id=run.id, workflow="w", input="in", status="running",
                  org_id=org_id, username="alice"))
        s.commit()

    run_in_background(run.id, _workflow(tmp_path), "in", engine=engine, org_id=org_id)

    with Session() as s:
        trigger = get_email_trigger(s, org_id)
        assert trigger.last_error == "an earlier failure"
