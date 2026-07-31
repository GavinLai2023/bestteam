"""Cooperative run cancellation: RunRegistry cancel flags, run_in_background's
cancellation checkpoints, and the POST /api/runs/{id}/cancel endpoint."""

import pytest

pytest.importorskip("sqlalchemy")
fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from bestteam.core.trace import TraceEvent
from helpers import create_user_and_login, get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run, TraceEventRecord
from ui.backend.db_session import get_db
from ui.backend.runtime import registry, run_in_background


def _engine():
    e = make_engine(":memory:")
    init_db(e)
    return e


def test_request_cancel_returns_false_for_unknown_run():
    assert registry.request_cancel("does-not-exist") is False


def test_request_cancel_returns_false_for_a_terminal_run():
    run = registry.create("wf", "in")
    registry.publish(
        run.id, {"type": "run_completed", "workflow": "wf", "agent": None, "data": "done", "usage": []}
    )

    assert registry.request_cancel(run.id) is False


def test_request_cancel_succeeds_for_a_running_run():
    run = registry.create("wf", "in")

    assert registry.request_cancel(run.id) is True
    assert registry.cancel_requested(run.id) is True


def test_publish_run_cancelled_sets_status_cancelled():
    run = registry.create("wf", "in")

    registry.publish(run.id, {"type": "run_cancelled", "workflow": "wf", "agent": None, "data": None, "usage": []})

    assert registry.get(run.id).status == "cancelled"


class _NeverStreamedWorkflow:
    name = "wf"

    def stream(self, *args, **kwargs):
        raise AssertionError("workflow.stream() must not be called once cancellation was requested before dispatch")


def test_cancel_before_dispatch_skips_streaming_entirely():
    engine = _engine()
    Session = session_factory(engine)
    run = registry.create("wf", "in")
    registry.request_cancel(run.id)

    run_in_background(run.id, _NeverStreamedWorkflow(), "in", engine=engine)

    with Session() as s:
        row = s.get(Run, run.id)
        assert row.status == "cancelled"
        types = [
            r.type
            for r in s.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
        ]
    assert types == ["run_queued", "run_cancelled"]


class _MidStreamCancelWorkflow:
    """Yields event0, then -- as a side effect of the *next* fetch -- requests
    cancellation before yielding event1; event2 must never be delivered."""

    name = "wf"

    def __init__(self, run_id, events):
        self._run_id = run_id
        self._events = events

    def stream(self, *args, **kwargs):
        yield self._events[0]
        registry.request_cancel(self._run_id)
        yield self._events[1]
        yield self._events[2]


def test_cancel_mid_stream_stops_before_the_next_unfetched_event():
    engine = _engine()
    Session = session_factory(engine)
    run = registry.create("wf", "in")
    events = [
        TraceEvent(type="run_started", workflow="wf"),
        TraceEvent(type="agent_completed", workflow="wf", agent="a", data="a-output"),
        TraceEvent(type="agent_completed", workflow="wf", agent="b", data="should-not-be-delivered"),
    ]

    run_in_background(run.id, _MidStreamCancelWorkflow(run.id, events), "in", engine=engine)

    with Session() as s:
        row = s.get(Run, run.id)
        assert row.status == "cancelled"
        rows = s.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
    types = [r.type for r in rows]
    assert types == ["run_queued", "run_started", "agent_completed", "run_cancelled"]
    assert all(r.data != '"should-not-be-delivered"' for r in rows)
    assert registry.get(run.id).status == "cancelled"


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


def test_cancel_endpoint_returns_404_for_unknown_run(client):
    resp = client.post("/api/runs/does-not-exist/cancel")

    assert resp.status_code == 404


def test_cancel_endpoint_returns_404_cross_org(client):
    run = registry.create("wf", "in", org_id=999999, username="someone-else")

    resp = client.post(f"/api/runs/{run.id}/cancel")

    assert resp.status_code == 404


def test_cancel_endpoint_returns_409_for_a_finished_run(client):
    org_id = get_org_id()
    run = registry.create("wf", "in", org_id=org_id, username="test")
    registry.publish(
        run.id, {"type": "run_completed", "workflow": "wf", "agent": None, "data": "done", "usage": []}
    )

    resp = client.post(f"/api/runs/{run.id}/cancel")

    assert resp.status_code == 409


def test_cancel_endpoint_returns_202_and_requests_cancellation(client):
    org_id = get_org_id()
    run = registry.create("wf", "in", org_id=org_id, username="test")

    resp = client.post(f"/api/runs/{run.id}/cancel")

    assert resp.status_code == 202
    assert registry.cancel_requested(run.id) is True
