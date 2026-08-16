"""Tests for the share-chat WebSocket stream (/api/share/{token}/stream/{run_id})
-- cookie-authenticated, no ticket (contrast tests/test_ws_stream.py)."""

from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

pytestmark = pytest.mark.integration

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import get_org_id, make_concurrent_safe_engine, open_test_db
from ui.backend import main as backend_main
from ui.backend import runtime
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run, WorkflowRecord
from ui.backend.db.orgs import set_org_active
from ui.backend.db.share_links import create_share_link, get_share_link_by_token, patch_share_link
from ui.backend.db.share_sessions import create_share_session
from ui.backend.db.users import create_user
from ui.backend.db_session import get_db
from ui.backend.share_auth import COOKIE_NAME, sign_session_token

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

    # POST /api/share/{token}/messages dispatches a real run, so a worker
    # thread's Session overlaps the request's -- see the helper's docstring.
    engine = make_concurrent_safe_engine(tmp_path)
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


def _session_and_run(client, token):
    """Create a ShareSession directly (not via the real POST /messages flow,
    so the run's events can be driven manually/deterministically) and a
    matching Run/registry entry, and set the session cookie on `client`.
    Returns (link, run_id)."""
    with open_test_db() as db:
        link = get_share_link_by_token(db, token)
        session = create_share_session(db, link.id)
        run = runtime.registry.create("greeter", "hi", org_id=link.org_id, username="share-link")
        db.add(
            Run(
                id=run.id,
                workflow="greeter",
                input="hi",
                org_id=link.org_id,
                username="share-link",
                trigger_context={
                    "share_link_id": link.id,
                    "share_session_id": session.id,
                    "turn_number": 1,
                },
            )
        )
        db.commit()
    client.cookies.set(COOKIE_NAME, sign_session_token(session.session_token))
    return link, run.id


def test_stream_closes_when_org_deactivated_midstream(client):
    token = _make_link()
    link, run_id = _session_and_run(client, token)

    runtime.registry.publish(
        run_id, {"type": "agent_completed", "workflow": "greeter", "agent": "a", "data": "x", "usage": []}
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/share/{token}/stream/{run_id}") as ws:
            assert ws.receive_json()["type"] == "agent_completed"
            with open_test_db() as db:
                set_org_active(db, "default", False)
            runtime.registry.publish(
                run_id, {"type": "run_completed", "workflow": "greeter", "agent": None, "data": "done", "usage": []}
            )
            ws.receive_json()  # revalidation must close before delivering this
    assert exc.value.code == 4404


def test_stream_closes_when_link_revoked_midstream(client):
    token = _make_link()
    link, run_id = _session_and_run(client, token)

    runtime.registry.publish(
        run_id, {"type": "agent_completed", "workflow": "greeter", "agent": "a", "data": "x", "usage": []}
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/share/{token}/stream/{run_id}") as ws:
            assert ws.receive_json()["type"] == "agent_completed"
            with open_test_db() as db:
                patch_share_link(db, get_share_link_by_token(db, token), active=False)
            runtime.registry.publish(
                run_id, {"type": "run_completed", "workflow": "greeter", "agent": None, "data": "done", "usage": []}
            )
            ws.receive_json()  # revalidation must close before delivering this
    assert exc.value.code == 4404


def test_stream_redacts_internals_from_a_non_terminal_event(client):
    """A visitor must never receive the org's internals over this socket:
    agent names, intermediate agent output, tool names/summaries, the team
    name, or the model identities/token counts in `usage` (final whole-branch
    review I4). Only the event type crosses the boundary for a non-terminal
    event -- the frontend's friendly status mapping keys off nothing else.
    """
    token = _make_link()
    link, run_id = _session_and_run(client, token)

    runtime.registry.publish(
        run_id,
        {
            "type": "agent_completed",
            "workflow": "greeter",
            "agent": "Secret Internal Analyst",
            "data": "raw intermediate output nobody outside the org may read",
            "usage": [{"model": "openai:gpt-4o-mini", "input_tokens": 123, "output_tokens": 45}],
        },
    )

    with client.websocket_connect(f"/api/share/{token}/stream/{run_id}") as ws:
        event = ws.receive_json()

    assert event["type"] == "agent_completed"
    assert event["agent"] is None
    assert event["data"] is None
    assert event["usage"] == []
    assert event["workflow"] is None
    serialized = str(event)
    assert "Secret Internal Analyst" not in serialized
    assert "raw intermediate output" not in serialized
    assert "gpt-4o-mini" not in serialized
    assert "greeter" not in serialized


def test_stream_still_delivers_the_final_answer_on_run_completed(client):
    """The one field a visitor DOES need: `run_completed.data` is the reply
    the chat page renders, so the I4 redaction must not strip it."""
    token = _make_link()
    link, run_id = _session_and_run(client, token)

    runtime.registry.publish(
        run_id,
        {
            "type": "run_completed",
            "workflow": "greeter",
            "agent": "a",
            "data": "here is your answer",
            "usage": [{"model": "openai:gpt-4o-mini", "input_tokens": 1, "output_tokens": 2}],
        },
    )

    with client.websocket_connect(f"/api/share/{token}/stream/{run_id}") as ws:
        event = ws.receive_json()

    assert event["type"] == "run_completed"
    assert event["data"] == "here is your answer"
    assert event["agent"] is None
    assert event["usage"] == []


def test_stream_rejects_connect_to_an_already_expired_link(client):
    token = _make_link()
    link, run_id = _session_and_run(client, token)
    with open_test_db() as db:
        patch_share_link(
            db, get_share_link_by_token(db, token), expires_at=datetime.now(timezone.utc) - timedelta(days=1)
        )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/share/{token}/stream/{run_id}") as ws:
            ws.receive_json()
    assert exc.value.code == 4404
