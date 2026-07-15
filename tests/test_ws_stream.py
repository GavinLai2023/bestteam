"""Tests for the WebSocket stream auth via short-lived single-use tickets (CR-013)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import create_user_and_login
from ui.backend import main as backend_main
from ui.backend import runtime, ws_tickets
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db


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
        test_client = TestClient(backend_main.app)
        token = create_user_and_login(test_client)
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _completed_run(org_id=None):
    # Run ownership is org-level: stream tests must stamp the fixture user's
    # org (default = first org, id 1) or the stream is refused with 4404.
    from helpers import get_org_id

    run = runtime.registry.create("wf", "in", org_id=org_id if org_id is not None else get_org_id())
    runtime.registry.publish(
        run.id,
        {"type": "run_completed", "workflow": "wf", "agent": None, "data": "done", "usage": []},
    )
    return run


def test_ws_ticket_endpoint_requires_auth(client):
    resp = client.post("/api/runs/ws-ticket", headers={"Authorization": ""})
    assert resp.status_code == 401


def test_ws_ticket_endpoint_returns_ticket(client):
    resp = client.post("/api/runs/ws-ticket")
    assert resp.status_code == 200
    assert resp.json()["ticket"]


def test_ws_stream_rejects_missing_ticket(client):
    run = _completed_run()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/runs/{run.id}/stream") as ws:
            ws.receive_json()


def test_ws_stream_rejects_bogus_ticket(client):
    run = _completed_run()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/runs/{run.id}/stream?ticket=not-a-real-ticket") as ws:
            ws.receive_json()


def test_ws_stream_accepts_valid_ticket_and_replays_events(client):
    run = _completed_run()
    ticket = client.post("/api/runs/ws-ticket").json()["ticket"]
    with client.websocket_connect(f"/api/runs/{run.id}/stream?ticket={ticket}") as ws:
        event = ws.receive_json()
    assert event["type"] == "run_completed"


def test_ws_stream_refuses_cross_org_run_with_unknown_run_code(client):
    # An org-B user with a perfectly valid ticket must not stream an org-A
    # run -- and the close code equals the unknown-run code (4404), so a
    # probing client can't tell "exists in another org" from "doesn't exist".
    run = _completed_run()  # owned by the fixture user's 'default' org
    create_user_and_login(client, username="bob", org="orgb")
    ticket = ws_tickets.issue_ticket("bob")

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/runs/{run.id}/stream?ticket={ticket}") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4404


def test_ws_stream_allows_platform_admin_passthrough(client):
    run = _completed_run()
    create_user_and_login(client, username="op", org=None, admin=True)
    ticket = ws_tickets.issue_ticket("op")

    with client.websocket_connect(f"/api/runs/{run.id}/stream?ticket={ticket}") as ws:
        event = ws.receive_json()
    assert event["type"] == "run_completed"


def test_get_run_is_org_scoped(client):
    # Fixes the pre-existing hole where any authenticated user could read any
    # run by id: cross-org access is now a 404; platform admins pass through.
    run = _completed_run()

    assert client.get(f"/api/runs/{run.id}").status_code == 200  # owner org

    bob_token = create_user_and_login(client, username="bob", org="orgb")
    bob_headers = {"Authorization": f"Bearer {bob_token}"}
    assert client.get(f"/api/runs/{run.id}", headers=bob_headers).status_code == 404

    op_token = create_user_and_login(client, username="op", org=None, admin=True)
    op_headers = {"Authorization": f"Bearer {op_token}"}
    assert client.get(f"/api/runs/{run.id}", headers=op_headers).status_code == 200


def test_ws_ticket_is_single_use():
    ticket = ws_tickets.issue_ticket("alice")
    assert ws_tickets.consume_ticket(ticket) == "alice"
    assert ws_tickets.consume_ticket(ticket) is None


def test_ticket_operations_are_thread_safe():
    # CR-013: concurrent issue/consume/purge must not corrupt the ticket dict
    # (e.g. "dictionary changed size during iteration" from _purge_expired).
    import threading

    errors = []

    def worker():
        try:
            for _ in range(300):
                ticket = ws_tickets.issue_ticket("u")
                ws_tickets.consume_ticket(ticket)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
