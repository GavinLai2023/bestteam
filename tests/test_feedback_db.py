"""DB helpers for the feedback table (db/feedback.py)."""

import pytest

pytest.importorskip("sqlalchemy")

pytestmark = pytest.mark.unit

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.feedback import (
    count_session_feedback_today,
    create_feedback,
    get_feedback,
    list_feedback,
    update_feedback,
)
from ui.backend.db.models import PipelineRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.share_links import create_share_link
from ui.backend.db.share_sessions import create_share_session
from ui.backend.db.users import create_user


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    SessionLocal = session_factory(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org_user(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "alice", "pw", org_id=org.id)
    return org, user


def test_create_and_list_newest_first(db):
    org, user = _org_user(db)
    first = create_feedback(db, kind="defect", body="b1", org_id=org.id, submitted_by=user.id)
    second = create_feedback(db, kind="suggestion", body="b2", org_id=org.id, submitted_by=user.id)
    db.commit()
    rows = list_feedback(db)
    assert [r.id for r in rows] == [second.id, first.id]
    assert rows[0].status == "new"


def test_exactly_one_author_is_enforced(db):
    org, user = _org_user(db)
    with pytest.raises(ValueError):
        create_feedback(db, kind="defect", body="x", org_id=org.id)
    with pytest.raises(ValueError):
        create_feedback(
            db, kind="defect", body="x", org_id=org.id, submitted_by=user.id, share_session_id=1
        )


def test_filters(db):
    org, user = _org_user(db)
    kept = create_feedback(db, kind="defect", body="x", org_id=org.id, submitted_by=user.id)
    create_feedback(db, kind="suggestion", body="y", org_id=org.id, submitted_by=user.id)
    db.commit()
    assert [r.id for r in list_feedback(db, kind="defect")] == [kept.id]
    assert list_feedback(db, status="resolved") == []
    assert [r.id for r in list_feedback(db, status="new", org_id=org.id, kind="defect")] == [kept.id]


def test_update_status_and_note(db):
    org, user = _org_user(db)
    row = create_feedback(db, kind="defect", body="x", org_id=org.id, submitted_by=user.id)
    db.commit()
    update_feedback(db, row, status="acknowledged", admin_note="looking")
    db.commit()
    got = get_feedback(db, row.id)
    assert got.status == "acknowledged"
    assert got.admin_note == "looking"


def test_session_daily_count(db):
    org, user = _org_user(db)
    team = PipelineRecord(name="t", org_id=org.id, config={}, status="deployed")
    db.add(team)
    db.commit()
    link = create_share_link(db, pipeline_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)
    assert count_session_feedback_today(db, session.id) == 0
    create_feedback(db, kind="defect", body="x", org_id=org.id, share_session_id=session.id)
    db.commit()
    assert count_session_feedback_today(db, session.id) == 1
