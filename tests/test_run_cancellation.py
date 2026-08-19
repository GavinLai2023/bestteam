"""Cooperative run cancellation: RunRegistry cancel flags, run_in_background's
cancellation checkpoints, and the POST /api/runs/{id}/cancel endpoint."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")
fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from bestteam.core.trace import TraceEvent
from helpers import create_user_and_login, get_org_id, make_concurrent_safe_engine, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run, TraceEventRecord
from ui.backend.db_session import get_db
from ui.backend.runtime import registry, run_in_background


def _engine(tmp_path):
    e = make_concurrent_safe_engine(tmp_path)
    init_db(e)
    return e


def test_request_cancel_returns_false_for_unknown_run():
    assert registry.request_cancel("does-not-exist") is False


def test_request_cancel_returns_false_for_a_terminal_run():
    run = registry.create("wf", "in")
    registry.publish(
        run.id, {"type": "run_completed", "pipeline": "wf", "agent": None, "data": "done", "usage": []}
    )

    assert registry.request_cancel(run.id) is False


def test_request_cancel_succeeds_for_a_running_run():
    run = registry.create("wf", "in")

    assert registry.request_cancel(run.id) is True
    assert registry.cancel_requested(run.id) is True


def test_publish_run_cancelled_sets_status_cancelled():
    run = registry.create("wf", "in")

    registry.publish(run.id, {"type": "run_cancelled", "pipeline": "wf", "agent": None, "data": None, "usage": []})

    assert registry.get(run.id).status == "cancelled"


class _NeverStreamedPipeline:
    name = "wf"

    def stream(self, *args, **kwargs):
        raise AssertionError("pipeline.stream() must not be called once cancellation was requested before dispatch")


def test_cancel_before_dispatch_skips_streaming_entirely(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    run = registry.create("wf", "in")
    registry.request_cancel(run.id)

    run_in_background(run.id, _NeverStreamedPipeline(), "in", engine=engine)

    with Session() as s:
        row = s.get(Run, run.id)
        assert row.status == "cancelled"
        types = [
            r.type
            for r in s.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
        ]
    assert types == ["run_queued", "run_cancelled"]


class _MidStreamCancelPipeline:
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


def test_cancel_mid_stream_stops_before_the_next_unfetched_event(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    run = registry.create("wf", "in")
    events = [
        TraceEvent(type="run_started", pipeline="wf"),
        TraceEvent(type="agent_completed", pipeline="wf", agent="a", data="a-output"),
        TraceEvent(type="agent_completed", pipeline="wf", agent="b", data="should-not-be-delivered"),
    ]

    run_in_background(run.id, _MidStreamCancelPipeline(run.id, events), "in", engine=engine)

    with Session() as s:
        row = s.get(Run, run.id)
        assert row.status == "cancelled"
        rows = s.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
    types = [r.type for r in rows]
    assert types == ["run_queued", "run_started", "agent_completed", "run_cancelled"]
    assert all(r.data != '"should-not-be-delivered"' for r in rows)
    assert registry.get(run.id).status == "cancelled"


class _CancelBetweenBufferedEventAndAgentCompleted:
    """Reproduces the P1 usage-loss bug: cancellation lands between a node's
    buffered granular event (e.g. tool_completed) and the agent_completed
    that follows it and carries that node's usage. The paid call already
    happened by the time either event is yielded, so usage must survive."""

    name = "wf"

    def __init__(self, run_id, events):
        self._run_id = run_id
        self._events = events

    def stream(self, *args, **kwargs):
        yield self._events[0]  # run_started
        registry.request_cancel(self._run_id)
        yield self._events[1]  # tool_completed (buffered, same node)
        yield self._events[2]  # agent_completed carrying usage for that node
        yield self._events[3]  # next node's event -- must never be delivered


def test_cancel_between_buffered_event_and_its_agent_completed_preserves_usage(tmp_path, db_session_factory=None):
    from ui.backend.db.usage import list_usage_for_run

    engine = _engine(tmp_path)
    Session = session_factory(engine)
    run = registry.create("wf", "in")
    events = [
        TraceEvent(type="run_started", pipeline="wf"),
        TraceEvent(type="tool_completed", pipeline="wf", agent="a", data={"tool": "t", "success": True}),
        TraceEvent(
            type="agent_completed",
            pipeline="wf",
            agent="a",
            data="a-output",
            usage=[{"model": "m", "input_tokens": 10, "output_tokens": 5}],
        ),
        TraceEvent(type="agent_completed", pipeline="wf", agent="b", data="should-not-be-delivered"),
    ]

    run_in_background(run.id, _CancelBetweenBufferedEventAndAgentCompleted(run.id, events), "in", engine=engine)

    with Session() as s:
        row = s.get(Run, run.id)
        assert row.status == "cancelled"
        rows = s.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
        usage_records = list_usage_for_run(s, run.id)

    types = [r.type for r in rows]
    assert types == ["run_queued", "run_started", "tool_completed", "agent_completed", "run_cancelled"]
    assert len(usage_records) == 1, "usage for the already-completed node must not be dropped by cancellation"
    assert usage_records[0].input_tokens == 10
    assert usage_records[0].output_tokens == 5


class _CancelBetweenRunStartedAndMemoryRecalled:
    """Reproduces the memory_recalled usage-loss bug: cancellation is
    discovered right after run_started, before the memory_recalled event --
    the sole carrier of query-expansion usage -- is ever pulled from the
    generator. Usage must survive: the paid expansion call already happened
    before run_started was even yielded."""

    name = "wf"

    def __init__(self, run_id, events):
        self._run_id = run_id
        self._events = events

    def stream(self, *args, **kwargs):
        yield self._events[0]  # run_started
        registry.request_cancel(self._run_id)
        yield self._events[1]  # memory_recalled, carries expansion usage
        yield self._events[2]  # next event -- must never be delivered


def test_cancel_right_after_run_started_preserves_memory_recalled_usage(monkeypatch, tmp_path):
    from ui.backend.db.usage import list_usage_for_run

    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    run = registry.create("wf", "in")
    events = [
        TraceEvent(type="run_started", pipeline="wf", data="in"),
        TraceEvent(
            type="memory_recalled",
            pipeline="wf",
            data=0,
            usage=[{"model": None, "input_tokens": 9, "output_tokens": 3}],
        ),
        TraceEvent(type="agent_started", pipeline="wf", agent="a", data="should-not-be-delivered"),
    ]

    run_in_background(
        run.id,
        _CancelBetweenRunStartedAndMemoryRecalled(run.id, events),
        "in",
        engine=engine,
        user_id="alice",
    )

    with Session() as s:
        row = s.get(Run, run.id)
        assert row.status == "cancelled"
        rows = s.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
        usage_records = list_usage_for_run(s, run.id)

    types = [r.type for r in rows]
    assert types == ["run_queued", "run_started", "memory_recalled", "run_cancelled"]
    assert (
        len(usage_records) == 1
    ), "query-expansion usage must not be dropped by cancellation racing run_started"
    assert usage_records[0].agent == "memory:query_expansion"
    assert usage_records[0].input_tokens == 9
    assert usage_records[0].output_tokens == 3


class _CancelDuringPreStreamSetup:
    """Cancellation lands during pipeline setup (compile/memory-recall) --
    before the generator ever yields its first event. Must be honored right
    after run_started, before entering the first agent's paid work -- not
    deferred until that agent's own agent_completed (round-2 review P1)."""

    name = "wf"

    def __init__(self, run_id, events):
        self._run_id = run_id
        self._events = events

    def stream(self, *args, **kwargs):
        registry.request_cancel(self._run_id)
        yield self._events[0]  # run_started
        yield self._events[1]  # agent_started (buffered, node1) -- must never be reached
        yield self._events[2]  # agent_completed (node1) -- must never be reached (a paid call)


def test_cancel_during_pre_stream_setup_stops_before_the_first_agent(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    run = registry.create("wf", "in")
    events = [
        TraceEvent(type="run_started", pipeline="wf"),
        TraceEvent(type="agent_started", pipeline="wf", agent="a", data={"role": "R", "goal": "G"}),
        TraceEvent(type="agent_completed", pipeline="wf", agent="a", data="should-not-run"),
    ]

    run_in_background(run.id, _CancelDuringPreStreamSetup(run.id, events), "in", engine=engine)

    with Session() as s:
        rows = s.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
    types = [r.type for r in rows]
    assert types == ["run_queued", "run_started", "run_cancelled"], (
        "cancellation known before the first agent started must stop there, "
        "not run a whole avoidable extra paid agent turn first"
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()

    # File-backed, not `:memory:` -- this fixture drives run_in_background,
    # which opens its own Session on a worker thread (see
    # make_concurrent_safe_engine's docstring in helpers.py).
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
        run.id, {"type": "run_completed", "pipeline": "wf", "agent": None, "data": "done", "usage": []}
    )

    resp = client.post(f"/api/runs/{run.id}/cancel")

    assert resp.status_code == 409


def test_cancel_endpoint_returns_202_and_requests_cancellation(client):
    org_id = get_org_id()
    run = registry.create("wf", "in", org_id=org_id, username="test")

    resp = client.post(f"/api/runs/{run.id}/cancel")

    assert resp.status_code == 202
    assert registry.cancel_requested(run.id) is True
