"""API tests for org-side share-link management (/api/workflows/{id}/share-links,
/api/share-links/{id}, .../sessions)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import WorkflowRecord
from ui.backend.db_session import get_db

_TEAM_CONFIG = {
    "name": "greeter",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def client():
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
        c = TestClient(backend_main.app)
        token = create_user_and_login(c)
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _deploy_team(status="deployed", org_name="default"):
    with open_test_db() as db:
        org_id = get_org_id(org_name)
        record = WorkflowRecord(name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status=status)
        db.add(record)
        db.commit()
        db.refresh(record)
        return record.id


def test_create_share_link_requires_deployed_team(client):
    workflow_id = _deploy_team(status="draft")
    resp = client.post(f"/api/workflows/{workflow_id}/share-links", json={})
    assert resp.status_code == 404


def test_create_and_list_share_links(client):
    workflow_id = _deploy_team()
    created = client.post(f"/api/workflows/{workflow_id}/share-links", json={"daily_cap": 10})
    assert created.status_code == 201
    body = created.json()
    assert body["active"] is True
    assert body["daily_cap"] == 10
    assert body["token"]

    listed = client.get(f"/api/workflows/{workflow_id}/share-links")
    assert listed.status_code == 200
    assert [l["id"] for l in listed.json()] == [body["id"]]


def test_patch_share_link_revokes(client):
    workflow_id = _deploy_team()
    link = client.post(f"/api/workflows/{workflow_id}/share-links", json={}).json()

    patched = client.patch(f"/api/share-links/{link['id']}", json={"active": False})
    assert patched.status_code == 200
    assert patched.json()["active"] is False


def test_patch_unknown_share_link_is_404(client):
    resp = client.patch("/api/share-links/999999", json={"active": False})
    assert resp.status_code == 404


def test_share_links_are_org_scoped(client):
    workflow_id = _deploy_team()
    link = client.post(f"/api/workflows/{workflow_id}/share-links", json={}).json()

    other_client = TestClient(backend_main.app)
    other_token = create_user_and_login(other_client, username="other", org="other-org")
    other_client.headers["Authorization"] = f"Bearer {other_token}"

    resp = other_client.patch(f"/api/share-links/{link['id']}", json={"active": False})
    assert resp.status_code == 404
    resp = other_client.get(f"/api/workflows/{workflow_id}/share-links")
    assert resp.status_code == 404  # not this org's deployed team either


def test_list_sessions_and_messages_for_a_link(client):
    from ui.backend.db.share_messages import append_message
    from ui.backend.db.share_sessions import create_share_session

    workflow_id = _deploy_team()
    link = client.post(f"/api/workflows/{workflow_id}/share-links", json={}).json()

    with open_test_db() as db:
        session = create_share_session(db, link["id"])
        append_message(db, session.id, turn_number=1, role="user", content="hi there")
        append_message(db, session.id, turn_number=2, role="assistant", content="hello!")
        session_id = session.id

    sessions = client.get(f"/api/share-links/{link['id']}/sessions")
    assert sessions.status_code == 200
    assert [s["id"] for s in sessions.json()] == [session_id]

    messages = client.get(f"/api/share-links/{link['id']}/sessions/{session_id}/messages")
    assert messages.status_code == 200
    assert [(m["role"], m["content"]) for m in messages.json()] == [
        ("user", "hi there"),
        ("assistant", "hello!"),
    ]
