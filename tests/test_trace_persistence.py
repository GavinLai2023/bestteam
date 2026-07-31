"""Trace-event persistence (run_in_background writes TraceEventRecord rows)
and the read endpoints that expose them: GET /api/runs/{id}/trace and
GET /api/runs (list, with filters)."""

import json

import pytest

pytest.importorskip("sqlalchemy")
fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from bestteam import AgentSpec, Specification, TeamSpec, WorkflowSpec, validate_specification
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


def _workflow(tmp_path, response="done"):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model=f"fake:{response}")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        workflow=WorkflowSpec(steps=["t"]),
    )
    return validate_specification(spec, source=tmp_path / "w.yaml")


def test_run_in_background_persists_trace_events_in_seq_order(tmp_path):
    engine = _engine()
    Session = session_factory(engine)
    wf = _workflow(tmp_path)
    run = registry.create("w", "in")

    run_in_background(run.id, wf, "in", engine=engine)

    with Session() as s:
        rows = (
            s.query(TraceEventRecord)
            .filter_by(run_id=run.id)
            .order_by(TraceEventRecord.seq)
            .all()
        )

    assert [r.type for r in rows] == [
        "run_queued",
        "run_started",
        "agent_started",
        "agent_completed",
        "run_completed",
    ]
    assert [r.seq for r in rows] == list(range(len(rows)))
    agent_completed_row = next(r for r in rows if r.type == "agent_completed")
    assert json.loads(agent_completed_row.data) == "done"


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


def test_get_run_trace_returns_persisted_events_in_seq_order(client, tmp_path):
    org_id = get_org_id()
    with open_test_db() as db:
        engine = db.get_bind()
    run = registry.create("w", "in", org_id=org_id, username="test")
    with open_test_db() as db:
        db.add(Run(id=run.id, workflow="w", input="in", status="running", org_id=org_id, username="test"))
        db.commit()
    wf = _workflow(tmp_path)
    run_in_background(run.id, wf, "in", engine=engine, org_id=org_id, username="test")

    resp = client.get(f"/api/runs/{run.id}/trace")

    assert resp.status_code == 200
    events = resp.json()["events"]
    assert [e["type"] for e in events] == [
        "run_queued",
        "run_started",
        "agent_started",
        "agent_completed",
        "run_completed",
    ]
    assert [e["seq"] for e in events] == list(range(len(events)))
    agent_completed = next(e for e in events if e["type"] == "agent_completed")
    assert agent_completed["data"] == "done"


def test_get_run_trace_cross_org_is_404(client):
    with open_test_db() as db:
        db.add(Run(id="other-org-run", workflow="w", input="in", status="completed", org_id=999999))
        db.commit()

    resp = client.get("/api/runs/other-org-run/trace")

    assert resp.status_code == 404


def test_get_run_trace_unknown_run_is_404(client):
    resp = client.get("/api/runs/does-not-exist/trace")

    assert resp.status_code == 404


def test_list_runs_filters_by_manual_workflow_and_status(client):
    org_id = get_org_id()
    with open_test_db() as db:
        db.add_all(
            [
                Run(id="r-manual", workflow="wf-a", input="in", status="completed", org_id=org_id, username="test"),
                Run(id="r-auto", workflow="wf-a", input="in", status="completed", org_id=org_id, username="email-trigger"),
                Run(id="r-other-wf", workflow="wf-b", input="in", status="failed", org_id=org_id, username="test"),
                Run(id="r-other-org", workflow="wf-a", input="in", status="completed", org_id=org_id + 1000, username="test"),
            ]
        )
        db.commit()

    resp = client.get("/api/runs", params={"manual": "true"})
    assert {r["id"] for r in resp.json()["runs"]} == {"r-manual", "r-other-wf"}

    resp = client.get("/api/runs", params={"workflow": "wf-a"})
    assert {r["id"] for r in resp.json()["runs"]} == {"r-manual", "r-auto"}

    resp = client.get("/api/runs", params={"status": "failed"})
    assert {r["id"] for r in resp.json()["runs"]} == {"r-other-wf"}

    resp = client.get("/api/runs")
    runs = resp.json()["runs"]
    assert {r["id"] for r in runs} == {"r-manual", "r-auto", "r-other-wf"}
    auto_row = next(r for r in runs if r["id"] == "r-auto")
    assert auto_row["autonomous"] is True
