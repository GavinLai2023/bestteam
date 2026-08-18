"""Tests for the persistence layer (ui/backend/db) -- Phase 1.

Uses an in-memory SQLite database (StaticPool, see database.make_engine), so
these tests don't touch disk and run as fast as the rest of the suite.
"""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")

from sqlalchemy import inspect

from ui.backend.db import SkillRecord, WorkflowRecord, init_db, make_engine, session_factory
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
        "organizations",
        "users",
        "knowledge_bases",
        "knowledge_ingestion_jobs",
        "knowledge_documents",
        "knowledge_chunks",
        "skills",
        "skill_versions",
        "workflows",
        "workflow_versions",
        "workflow_dependencies",
        "builder_sessions",
        "email_triggers",
        "inbox_events",
        "model_catalog",
        "runs",
        "trace_events",
        "usage_records",
        "org_email_credentials",
        "automation_item_results",
        "share_links",
        "share_sessions",
        "share_messages",
        "notifications",
        "org_notification_settings",
        "org_retention_settings",
        "org_email_filter_settings",
        "org_email_budget_settings",
    }


def test_workflow_record_round_trips_json_config(db_session):
    workflow = WorkflowRecord(name="support_workflow", config={"agents": [], "teams": [], "workflow": {"steps": []}})
    db_session.add(workflow)
    db_session.commit()

    fetched_workflow = db_session.query(WorkflowRecord).filter_by(name="support_workflow").one()

    assert fetched_workflow.status == "draft"
    assert fetched_workflow.config["workflow"] == {"steps": []}


def test_same_name_allowed_in_two_orgs_but_not_within_one(db_session):
    from sqlalchemy.exc import IntegrityError

    from ui.backend.db.orgs import create_org

    org_a = create_org(db_session, "org_a")
    org_b = create_org(db_session, "org_b")

    db_session.add(SkillRecord(name="helper", config={}, org_id=org_a.id))
    db_session.add(SkillRecord(name="helper", config={}, org_id=org_b.id))
    db_session.commit()  # same name in two orgs: fine

    db_session.add(SkillRecord(name="helper", config={}, org_id=org_a.id))
    with pytest.raises(IntegrityError):
        db_session.commit()  # duplicate within one org: rejected
    db_session.rollback()


def test_organization_round_trip(db_session):
    from ui.backend.db.orgs import create_org, get_org_by_name, seed_default_org

    create_org(db_session, "acme", display_name="Acme Corp")
    fetched = get_org_by_name(db_session, "acme")
    assert fetched.display_name == "Acme Corp"

    seed_default_org(db_session)
    seed_default_org(db_session)  # idempotent
    assert get_org_by_name(db_session, "default") is not None


def test_email_guard_rejects_multi_org_with_email_configured(db_session, monkeypatch):
    # CR-031: BESTTEAM_EMAIL_* configures ONE process-wide mailbox and the
    # built-in email skill is visible to every org, so a second org would let
    # another customer read the configured mailbox. Until per-org credentials
    # exist, that combination must refuse to run.
    from ui.backend.db.orgs import create_org, ensure_email_single_org, seed_default_org

    seed_default_org(db_session)
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "imap")

    ensure_email_single_org(db_session)  # one org: fine

    create_org(db_session, "acme")
    with pytest.raises(RuntimeError, match="BESTTEAM_EMAIL_BACKEND"):
        ensure_email_single_org(db_session)


def test_email_guard_allows_multi_org_without_email(db_session, monkeypatch):
    from ui.backend.db.orgs import create_org, ensure_email_single_org, seed_default_org

    monkeypatch.delenv("BESTTEAM_EMAIL_BACKEND", raising=False)
    seed_default_org(db_session)
    create_org(db_session, "acme")

    ensure_email_single_org(db_session)  # no email config: multi-org is fine


def test_email_guard_counts_an_org_about_to_be_created(db_session, monkeypatch):
    # The create-org CLI checks BEFORE inserting, passing creating=1.
    from ui.backend.db.orgs import ensure_email_single_org, seed_default_org

    seed_default_org(db_session)
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "graph")

    with pytest.raises(RuntimeError, match="BESTTEAM_EMAIL_BACKEND"):
        ensure_email_single_org(db_session, creating=1)


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


def test_skill_record_round_trip(db_session):
    record = SkillRecord(
        name="research_skill",
        config={"name": "research_skill", "instructions": "Use web_search.", "tools": ["web_search"]},
    )
    db_session.add(record)
    db_session.commit()

    fetched = db_session.query(SkillRecord).filter_by(name="research_skill").one()
    assert fetched.config["instructions"] == "Use web_search."
    assert fetched.config["tools"] == ["web_search"]


def test_share_tables_exist():
    from sqlalchemy import inspect

    from ui.backend.db import init_db, make_engine

    engine = make_engine(":memory:")
    init_db(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"share_links", "share_sessions", "share_messages"} <= tables
