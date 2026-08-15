"""API tests for the anonymous share-chat surface (/api/share/{token}/messages)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Organization, WorkflowRecord
from ui.backend.db.share_links import create_share_link, patch_share_link
from ui.backend.db.users import create_user
from ui.backend.db_session import get_db

_TEAM_CONFIG = {
    "name": "greeter",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hello!"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()

    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(backend_main.app)
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _make_link(**overrides):
    with open_test_db() as db:
        org_id = get_org_id()
        user = create_user(db, "owner", "pw", org_id=org_id)
        team = WorkflowRecord(name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status="deployed")
        db.add(team)
        db.commit()
        db.refresh(team)
        link = create_share_link(db, workflow_id=team.id, org_id=org_id, created_by=user.id, **overrides)
        return link.token, link.id


def test_unknown_token_is_404(client):
    resp = client.post("/api/share/not-a-real-token/messages", json={"content": "hi"})
    assert resp.status_code == 404


def test_send_message_dispatches_a_run_and_sets_cookie(client):
    token, _ = _make_link()
    resp = client.post(f"/api/share/{token}/messages", json={"content": "hi there"})
    assert resp.status_code == 202
    assert resp.json()["run_id"]
    assert client.cookies.get("bestteam_share_session")


def test_revoked_link_is_404(client):
    token, link_id = _make_link()
    with open_test_db() as db:
        from ui.backend.db.share_links import get_share_link_by_token
        link = get_share_link_by_token(db, token)
        patch_share_link(db, link, active=False)

    resp = client.post(f"/api/share/{token}/messages", json={"content": "hi"})
    assert resp.status_code == 404


def test_second_visitor_gets_an_isolated_session(client):
    token, _ = _make_link()
    client.post(f"/api/share/{token}/messages", json={"content": "from visitor A"})

    other = TestClient(backend_main.app)
    other.post(f"/api/share/{token}/messages", json={"content": "from visitor B"})

    a_history = client.get(f"/api/share/{token}/messages").json()["messages"]
    b_history = other.get(f"/api/share/{token}/messages").json()["messages"]
    assert a_history[0]["content"] == "from visitor A"
    assert b_history[0]["content"] == "from visitor B"


def test_get_messages_with_no_cookie_returns_empty(client):
    token, _ = _make_link()
    resp = client.get(f"/api/share/{token}/messages")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_daily_cap_is_enforced(client):
    token, _ = _make_link(daily_cap=1)
    first = client.post(f"/api/share/{token}/messages", json={"content": "one"})
    assert first.status_code == 202
    second = client.post(f"/api/share/{token}/messages", json={"content": "two"})
    assert second.status_code == 429


def test_message_over_length_cap_is_rejected(client):
    token, _ = _make_link()
    resp = client.post(f"/api/share/{token}/messages", json={"content": "x" * 5000})
    assert resp.status_code == 422


def test_deactivated_org_makes_link_unavailable(client):
    token, _ = _make_link()
    with open_test_db() as db:
        org = db.query(Organization).filter_by(id=get_org_id()).one()
        org.active = False
        db.commit()

    resp = client.post(f"/api/share/{token}/messages", json={"content": "hi"})
    assert resp.status_code == 404


def test_append_message_same_turn_number_raises_integrity_error(client):
    """Proves the narrow race `send_share_message` guards against is real and
    reachable: `next_turn_number` (a plain SELECT) and `append_message` (a
    plain INSERT) are two separate, unlocked calls, and `ShareMessage` carries
    UniqueConstraint(share_session_id, turn_number). Two near-simultaneous
    sends for the same session can both read the same next turn number, and
    the second `append_message` call raises `IntegrityError` -- this is
    exactly the exception `send_share_message` catches and turns into a 409,
    exercised directly at the CRUD layer (below the HTTP layer) since that is
    the cleanest way to force the exact race deterministically.
    """
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    from ui.backend.db.share_messages import append_message
    from ui.backend.db.share_sessions import create_share_session

    token, _ = _make_link()
    with open_test_db() as db:
        from ui.backend.db.share_links import get_share_link_by_token
        link = get_share_link_by_token(db, token)
        session = create_share_session(db, link.id)
        append_message(db, session.id, turn_number=1, role="user", content="first")
        with _pytest.raises(IntegrityError):
            append_message(db, session.id, turn_number=1, role="user", content="second")


def test_send_message_returns_409_when_turn_number_collides(client, monkeypatch):
    """End-to-end proof through the actual HTTP layer that
    `send_share_message` converts the UniqueConstraint collision into a
    friendly 409 rather than letting the raw `IntegrityError` become an
    uncaught 500: `next_turn_number` is patched to always return the same
    value it already returned for a message that's already been appended for
    this session, simulating what a second near-simultaneous request would
    observe if it read the "next" turn number before the first request's
    insert had landed.
    """
    import ui.backend.share_chat as share_chat_module

    token, _ = _make_link()

    with open_test_db() as db:
        from ui.backend.db.share_links import get_share_link_by_token
        from ui.backend.db.share_messages import append_message
        from ui.backend.db.share_sessions import create_share_session

        link = get_share_link_by_token(db, token)
        session = create_share_session(db, link.id)
        append_message(db, session.id, turn_number=1, role="user", content="already here")
        session_token = session.session_token

    monkeypatch.setattr(share_chat_module, "next_turn_number", lambda db, session_id: 1)
    # Bypass the coarse _has_pending_turn guard so the request reaches the
    # IntegrityError-handling code path this test targets, not the earlier
    # coarse 409 (which is exercised separately elsewhere).
    monkeypatch.setattr(share_chat_module, "_has_pending_turn", lambda db, session: False)

    client.cookies.set(
        "bestteam_share_session",
        share_chat_module.sign_session_token(session_token),
    )

    resp = client.post(f"/api/share/{token}/messages", json={"content": "colliding turn"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == share_chat_module._PENDING_TURN_MESSAGE


def test_turn_collision_refunds_the_daily_cap_turn(client, monkeypatch):
    """The losing request in the IntegrityError race must not cost the
    visitor a turn against their daily cap: its message was never persisted
    and no run was ever created for it (Task 8 review finding 1b). Forces the
    same collision as `test_send_message_returns_409_when_turn_number_collides`
    and asserts `turns_today` afterward equals what it was immediately before
    the colliding request -- not just that a 409 came back.
    """
    import ui.backend.share_chat as share_chat_module
    from ui.backend.db.models import ShareSession
    from ui.backend.db.share_links import get_share_link_by_token
    from ui.backend.db.share_messages import append_message
    from ui.backend.db.share_sessions import create_share_session

    token, _ = _make_link()

    with open_test_db() as db:
        link = get_share_link_by_token(db, token)
        session = create_share_session(db, link.id)
        append_message(db, session.id, turn_number=1, role="user", content="already here")
        session_token = session.session_token
        session_id = session.id

    monkeypatch.setattr(share_chat_module, "next_turn_number", lambda db, session_id: 1)
    monkeypatch.setattr(share_chat_module, "_has_pending_turn", lambda db, session: False)

    client.cookies.set(
        "bestteam_share_session",
        share_chat_module.sign_session_token(session_token),
    )

    with open_test_db() as db:
        turns_before = db.query(ShareSession).filter_by(id=session_id).one().turns_today

    resp = client.post(f"/api/share/{token}/messages", json={"content": "colliding turn"})
    assert resp.status_code == 409

    with open_test_db() as db:
        turns_after = db.query(ShareSession).filter_by(id=session_id).one().turns_today

    assert turns_after == turns_before


def test_undeployed_team_404_matches_revoked_link_404(client):
    """A workflow-not-deployed 404 must be indistinguishable from every other
    "can't use this link" 404 (Task 8 review finding 2) -- a differing detail
    string would let a prober tell "real, active link whose team just isn't
    deployed" apart from "fake/revoked/expired/org-deactivated".
    """
    from ui.backend.db.share_links import create_share_link, get_share_link_by_token

    undeployed_token, _ = _make_link()
    with open_test_db() as db:
        link = get_share_link_by_token(db, undeployed_token)
        team = db.query(WorkflowRecord).filter_by(id=link.workflow_id).one()
        team.status = "draft"
        db.commit()

    undeployed_resp = client.post(f"/api/share/{undeployed_token}/messages", json={"content": "hi"})
    assert undeployed_resp.status_code == 404

    # Reuse the same org/user/team to mint a second, independent link that we
    # revoke -- _make_link can't be called twice in one test (it provisions a
    # fixed "owner" username, globally unique).
    with open_test_db() as db:
        link = get_share_link_by_token(db, undeployed_token)
        revoked_link = create_share_link(
            db, workflow_id=link.workflow_id, org_id=link.org_id, created_by=link.created_by
        )
        revoked_token = revoked_link.token
        patch_share_link(db, revoked_link, active=False)

    other_client = TestClient(backend_main.app)
    revoked_resp = other_client.post(f"/api/share/{revoked_token}/messages", json={"content": "hi"})
    assert revoked_resp.status_code == 404

    assert undeployed_resp.json() == revoked_resp.json()


def test_link_level_cap_stops_a_burst_of_cookieless_requests(client):
    """The per-session cap alone caps nobody: a client that never stores the
    session cookie gets a brand-new, free ShareSession -- and so a fresh
    allowance -- on every request. `daily_cap` is therefore also the
    aggregate ceiling across every session on one link (final whole-branch
    review C1). Each request below uses its own cookie-less TestClient, so
    every one of them is a "new visitor" as far as the session cap is
    concerned.
    """
    token, _ = _make_link(daily_cap=2)

    statuses = []
    for _ in range(4):
        fresh = TestClient(backend_main.app)  # no cookies carried over
        statuses.append(fresh.post(f"/api/share/{token}/messages", json={"content": "hi"}).status_code)

    assert statuses == [202, 202, 429, 429]


def test_undeployed_team_link_never_creates_a_share_session_row(client):
    """A valid-but-undeployed link, hammered in a loop, used to grow
    `share_sessions` unboundedly with orphaned rows, because the session was
    created (and committed) before the deployed check ran (final whole-branch
    review M12).
    """
    from ui.backend.db.models import ShareSession
    from ui.backend.db.share_links import get_share_link_by_token

    token, _ = _make_link()
    with open_test_db() as db:
        link = get_share_link_by_token(db, token)
        team = db.query(WorkflowRecord).filter_by(id=link.workflow_id).one()
        team.status = "draft"
        db.commit()

    with open_test_db() as db:
        before = db.query(ShareSession).count()

    for _ in range(3):
        assert client.post(f"/api/share/{token}/messages", json={"content": "hi"}).status_code == 404

    with open_test_db() as db:
        assert db.query(ShareSession).count() == before


def test_cors_allows_credentials():
    from ui.backend.main import app

    cors_middleware = next(
        m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"
    )
    assert cors_middleware.kwargs.get("allow_credentials") is True
