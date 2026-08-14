"""Tests for the share-link/session/message CRUD layer (db/share_links.py,
db/share_sessions.py, db/share_messages.py)."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.models import WorkflowRecord
from ui.backend.db.share_links import (
    count_active_share_links,
    create_share_link,
    get_share_link,
    get_share_link_by_token,
    list_share_links,
    patch_share_link,
)
from ui.backend.db.users import create_user


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


def _deployed_team(db, org_id, name="team1"):
    record = WorkflowRecord(
        name=name, org_id=org_id,
        config={"name": name, "agents": [], "teams": [], "workflow": {"steps": []}},
        status="deployed",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def test_create_and_get_share_link(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)

    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    assert link.active is True
    assert link.daily_cap == 30
    assert len(link.token) > 20
    fetched = get_share_link_by_token(db, link.token)
    assert fetched.id == link.id


def test_get_share_link_scoped_to_org(db):
    org_a = get_or_create_org(db, "org-a")
    org_b = get_or_create_org(db, "org-b")
    user = create_user(db, "owner", "pw", org_id=org_a.id)
    team = _deployed_team(db, org_a.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org_a.id, created_by=user.id)

    assert get_share_link(db, link.id, org_a.id) is not None
    assert get_share_link(db, link.id, org_b.id) is None


def test_list_share_links_newest_first(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    first = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    second = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    links = list_share_links(db, team.id, org.id)
    assert [l.id for l in links] == [second.id, first.id]


def test_patch_share_link_revoke(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    patched = patch_share_link(db, link, active=False)
    assert patched.active is False
    assert get_share_link_by_token(db, link.token).active is False


def test_patch_share_link_daily_cap_and_expiry(db):
    from datetime import datetime, timezone

    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    expiry = datetime(2027, 1, 1, tzinfo=timezone.utc)

    patched = patch_share_link(db, link, daily_cap=5, expires_at=expiry)
    assert patched.daily_cap == 5
    assert patched.expires_at is not None

    cleared = patch_share_link(db, link, clear_expiry=True)
    assert cleared.expires_at is None


def test_count_active_share_links(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    a = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    assert count_active_share_links(db, team.id) == 2
    patch_share_link(db, a, active=False)
    assert count_active_share_links(db, team.id) == 1


from ui.backend.db.share_sessions import (
    create_share_session,
    get_share_session_by_token,
    list_share_sessions,
    try_consume_turn,
)


def test_create_and_get_share_session(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    session = create_share_session(db, link.id)
    assert session.turns_today == 0
    fetched = get_share_session_by_token(db, session.session_token)
    assert fetched.id == session.id


def test_list_share_sessions_newest_active_first(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    first = create_share_session(db, link.id)
    second = create_share_session(db, link.id)

    sessions = list_share_sessions(db, link.id)
    assert [s.id for s in sessions] == [second.id, first.id]


def test_try_consume_turn_respects_daily_cap(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)

    assert try_consume_turn(db, session, daily_cap=2) is True
    assert try_consume_turn(db, session, daily_cap=2) is True
    assert try_consume_turn(db, session, daily_cap=2) is False
    db.refresh(session)
    assert session.turns_today == 2


def test_try_consume_turn_resets_on_new_day(db, monkeypatch):
    import ui.backend.db.share_sessions as share_sessions_module

    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)

    monkeypatch.setattr(share_sessions_module, "_today", lambda: "2026-08-14")
    assert try_consume_turn(db, session, daily_cap=1) is True
    assert try_consume_turn(db, session, daily_cap=1) is False

    monkeypatch.setattr(share_sessions_module, "_today", lambda: "2026-08-15")
    assert try_consume_turn(db, session, daily_cap=1) is True
