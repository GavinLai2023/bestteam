"""Tests for the persistence layer (ui/backend/db) -- Phase 1.

Uses an in-memory SQLite database (StaticPool, see database.make_engine), so
these tests don't touch disk and run as fast as the rest of the suite.
"""

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import inspect

from ui.backend.db import AgentRecord, WorkflowRecord, init_db, make_engine, session_factory
from ui.backend.db.builder_sessions import (
    STATUSES,
    append_feedback,
    create_session,
    get_session,
    update_session,
)


@pytest.fixture
def db_session():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_init_db_creates_all_tables():
    engine = make_engine(":memory:")
    init_db(engine)

    tables = set(inspect(engine).get_table_names())
    assert tables == {
        "users",
        "agents",
        "teams",
        "knowledge_bases",
        "workflows",
        "builder_sessions",
        "model_catalog",
        "runs",
        "trace_events",
        "usage_records",
    }


def test_workflow_and_agent_records_round_trip_json_config(db_session):
    agent = AgentRecord(name="support_agent", config={"role": "Support", "goal": "Help", "model": "fake:hi"})
    workflow = WorkflowRecord(name="support_workflow", config={"agents": [], "teams": [], "workflow": {"steps": []}})
    db_session.add_all([agent, workflow])
    db_session.commit()

    fetched_agent = db_session.query(AgentRecord).filter_by(name="support_agent").one()
    fetched_workflow = db_session.query(WorkflowRecord).filter_by(name="support_workflow").one()

    assert fetched_agent.config == {"role": "Support", "goal": "Help", "model": "fake:hi"}
    assert fetched_workflow.status == "draft"
    assert fetched_workflow.config["workflow"] == {"steps": []}


def test_create_session_starts_in_intent_stage(db_session):
    session = create_session(db_session, intent_text="We need a support bot", as_is_text="Email-based today")

    assert session.status == "intent"
    assert session.intent_text == "We need a support bot"
    assert session.feedback_history == []
    assert get_session(db_session, session.id) is not None


def test_update_session_advances_status_and_stores_json(db_session):
    session = create_session(db_session, intent_text="We need a support bot")

    requirements = {"pain_points": ["slow responses"], "goals": ["faster replies"]}
    updated = update_session(db_session, session.id, status="requirements", requirements_json=requirements)

    assert updated.status == "requirements"
    assert updated.requirements_json == requirements

    reloaded = get_session(db_session, session.id)
    assert reloaded.requirements_json == requirements
    assert reloaded.status == "requirements"


def test_update_session_rejects_invalid_status(db_session):
    session = create_session(db_session, intent_text="x")

    with pytest.raises(ValueError, match="Invalid builder session status"):
        update_session(db_session, session.id, status="not_a_real_status")


def test_update_session_rejects_unknown_field(db_session):
    session = create_session(db_session, intent_text="x")

    with pytest.raises(ValueError, match="cannot be set"):
        update_session(db_session, session.id, feedback_history=[{"note": "hack"}])


def test_update_session_raises_for_unknown_session(db_session):
    with pytest.raises(LookupError):
        update_session(db_session, "does-not-exist", status="requirements")


def test_append_feedback_records_history_with_timestamp(db_session):
    session = create_session(db_session, intent_text="x")

    updated = append_feedback(db_session, session.id, {"stage": "specification", "note": "redo the researcher role"})

    assert len(updated.feedback_history) == 1
    entry = updated.feedback_history[0]
    assert entry["stage"] == "specification"
    assert entry["note"] == "redo the researcher role"
    assert "at" in entry

    # Second round of feedback appends rather than overwrites.
    updated = append_feedback(db_session, session.id, {"stage": "solution", "note": "tweak the writer"})
    assert len(updated.feedback_history) == 2


def test_all_statuses_are_settable(db_session):
    session = create_session(db_session, intent_text="x")

    for status in STATUSES:
        updated = update_session(db_session, session.id, status=status)
        assert updated.status == status
