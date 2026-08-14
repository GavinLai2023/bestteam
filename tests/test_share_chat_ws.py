"""Tests for the share-chat WebSocket stream (/api/share/{token}/stream/{run_id})
-- cookie-authenticated, no ticket (contrast tests/test_ws_stream.py)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend import runtime
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run, WorkflowRecord
from ui.backend.db.share_links import create_share_link
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


def _make_link():
    with open_test_db() as db:
        org_id = get_org_id()
        user = create_user(db, "owner", "pw", org_id=org_id)
        team = WorkflowRecord(name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status="deployed")
        db.add(team)
        db.commit()
        db.refresh(team)
        link = create_share_link(db, workflow_id=team.id, org_id=org_id, created_by=user.id)
        return link.token


def test_stream_rejects_missing_cookie(client):
    token = _make_link()
    run = runtime.registry.create("greeter", "hi", org_id=get_org_id(), username="share-link")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/share/{token}/stream/{run.id}") as ws:
            ws.receive_json()


def test_stream_delivers_events_for_the_sending_sessions_own_run(client):
    token = _make_link()
    sent = client.post(f"/api/share/{token}/messages", json={"content": "hi"})
    run_id = sent.json()["run_id"]

    with client.websocket_connect(f"/api/share/{token}/stream/{run_id}") as ws:
        event = ws.receive_json()
        assert event["type"] in ("run_queued", "run_started", "agent_started", "agent_completed", "run_completed")


def test_stream_rejects_a_run_id_belonging_to_another_session(client):
    token = _make_link()
    client.post(f"/api/share/{token}/messages", json={"content": "from A"})

    # `other` shares the same TestClient app (and thus the same dependency-
    # overridden in-memory DB) as `client`, but has its own cookie jar -- so
    # its POST creates a second, independent share session.
    other = TestClient(backend_main.app)
    sent_b = other.post(f"/api/share/{token}/messages", json={"content": "from B"})
    run_id_b = sent_b.json()["run_id"]

    # Client A's cookie jar doesn't carry B's session -- must be rejected.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/share/{token}/stream/{run_id_b}") as ws:
            ws.receive_json()
