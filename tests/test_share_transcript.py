"""Tests for share_transcript.record_share_reply -- the hook that appends a
share-chat run's assistant reply (or a friendly fallback) to share_messages
on every terminal path."""

import pytest

pytest.importorskip("sqlalchemy")

pytestmark = pytest.mark.integration

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.share_links import create_share_link
from ui.backend.db.share_messages import append_message, list_messages
from ui.backend.db.share_sessions import create_share_session
from ui.backend.db.users import create_user
from ui.backend.db.models import WorkflowRecord
from ui.backend.share_transcript import record_share_reply


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _share_session(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = WorkflowRecord(
        name="t", org_id=org.id,
        config={"name": "t", "agents": [], "teams": [], "workflow": {"steps": []}},
        status="deployed",
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    return create_share_session(db, link.id)


def test_records_the_reply_for_a_share_run(db):
    session = _share_session(db)
    append_message(db, session.id, turn_number=1, role="user", content="hi")
    run_row = Run(
        id="run-1", workflow="t", input="hi", org_id=1,
        trigger_context={"share_link_id": 1, "share_session_id": session.id, "turn_number": 1},
    )
    db.add(run_row)
    db.commit()

    record_share_reply(db, run_row, "hello there")

    messages = list_messages(db, session.id)
    assert [(m.turn_number, m.role, m.content) for m in messages] == [
        (1, "user", "hi"),
        (2, "assistant", "hello there"),
    ]


def test_falls_back_to_a_friendly_message_when_output_is_none(db):
    session = _share_session(db)
    append_message(db, session.id, turn_number=1, role="user", content="hi")
    run_row = Run(
        id="run-2", workflow="t", input="hi", org_id=1,
        trigger_context={"share_link_id": 1, "share_session_id": session.id, "turn_number": 1},
    )
    db.add(run_row)
    db.commit()

    record_share_reply(db, run_row, None)

    messages = list_messages(db, session.id)
    assert messages[-1].role == "assistant"
    assert messages[-1].content  # non-empty friendly fallback


def test_calling_twice_for_the_same_turn_is_a_no_op(db):
    """`record_share_reply` is reachable twice for one run: if the streaming
    loop's terminal branch raises partway through, `terminal_seen` stays
    False and the outer crash handler records a reply for the same run again
    (final whole-branch review I5). The second call must not raise on the
    (share_session_id, turn_number) unique constraint, and must not overwrite
    the real reply with the failure fallback -- first reply wins.
    """
    from ui.backend.db.models import ShareMessage

    session = _share_session(db)
    append_message(db, session.id, turn_number=1, role="user", content="hi")
    run_row = Run(
        id="run-6", workflow="t", input="hi", org_id=1,
        trigger_context={"share_link_id": 1, "share_session_id": session.id, "turn_number": 1},
    )
    db.add(run_row)
    db.commit()

    record_share_reply(db, run_row, "the real reply")
    record_share_reply(db, run_row, "a second, later reply")

    assistant_rows = (
        db.query(ShareMessage).filter_by(share_session_id=session.id, role="assistant").all()
    )
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == "the real reply"


def test_no_op_for_a_run_without_trigger_context(db):
    from ui.backend.db.models import ShareMessage

    _share_session(db)  # a real share session exists, to prove nothing touches it
    run_row = Run(id="run-3", workflow="t", input="hi", org_id=1, trigger_context=None)
    # No share_session_id anywhere -- must not raise, must not write anything.
    record_share_reply(None, run_row, "irrelevant")

    assert db.query(ShareMessage).count() == 0


def test_no_op_for_a_run_with_unrelated_trigger_context(db):
    from ui.backend.db.models import ShareMessage

    # e.g. an email-triggered run's trigger_context (no share_session_id key).
    session = _share_session(db)  # a real share session exists, to prove nothing touches it
    run_row = Run(
        id="run-4", workflow="t", input="hi", org_id=1,
        trigger_context={"mailbox_credential_id": 1},
    )
    record_share_reply(db, run_row, "irrelevant")
    # No exception, and no share_messages row was created for anyone.
    assert db.query(ShareMessage).count() == 0
    assert list_messages(db, session.id) == []


def test_run_in_background_records_share_reply_on_success(db, monkeypatch):
    # `_build_workflow` (core/loader.py) requires `source`/`extra_tools`
    # keyword-only args that the plan brief's snippet omitted (signature
    # drift since the brief was written) -- build the same Workflow directly
    # via the SDK dataclasses instead, following the established pattern in
    # tests/test_memory_backend.py::_workflow().
    from bestteam import Agent, CollaborationMode, Team, Workflow
    from ui.backend.runtime import run_in_background

    session = _share_session(db)
    append_message(db, session.id, turn_number=1, role="user", content="hi")

    agent = Agent(name="a", role="Asst", goal="help", model="fake:hello!")
    workflow = Workflow(name="t", steps=[Team(name="tm", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    db.add(Run(
        id="run-5", workflow="t", input="hi", org_id=1,
        trigger_context={"share_link_id": 1, "share_session_id": session.id, "turn_number": 1},
    ))
    db.commit()

    run_in_background("run-5", workflow, "hi", engine=db.get_bind())

    messages = list_messages(db, session.id)
    assert messages[-1].role == "assistant"
    assert messages[-1].content  # the fake model's output
