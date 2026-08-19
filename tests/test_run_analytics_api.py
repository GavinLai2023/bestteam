"""Admin pipeline-run analytics API (`/api/admin/analytics`) -- aggregate
stats over runs/trace_events/usage_records for the admin Trace page."""

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run, TraceEventRecord, UsageRecord
from ui.backend.db_session import get_db


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """client + bearer headers for alice (org_a) and op (platform admin)."""
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()

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
        client = TestClient(backend_main.app)
        headers = {
            "alice": {"Authorization": f"Bearer {create_user_and_login(client, username='alice', org='org_a')}"},
            "op": {"Authorization": f"Bearer {create_user_and_login(client, username='op', org=None, admin=True)}"},
        }
        yield client, headers
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _add_run_with_events(
    db, *, run_id, org_id, pipeline, status, agent="a", input_tokens=100, output_tokens=50, cost=0.01
):
    """A minimal but realistic run: run_started -> agent_completed (usage-
    bearing) -> [tool_completed for a failed run] -> terminal event."""
    db.add(Run(id=run_id, pipeline=pipeline, input="in", status=status, org_id=org_id, username="test"))
    db.add(TraceEventRecord(run_id=run_id, seq=0, type="run_started", agent=None, data=None))
    db.add(TraceEventRecord(run_id=run_id, seq=1, type="agent_completed", agent=agent, data=None))
    terminal_type = "run_completed"
    if status == "failed":
        db.add(TraceEventRecord(run_id=run_id, seq=2, type="tool_completed", agent=agent, data=None))
        terminal_type = "run_failed"
    db.add(TraceEventRecord(run_id=run_id, seq=3, type=terminal_type, agent=None, data=None))
    db.add(
        UsageRecord(
            run_id=run_id, agent=agent, model="fake:x", input_tokens=input_tokens, output_tokens=output_tokens,
            cost_estimate=cost, org_id=org_id,
        )
    )
    db.commit()


def test_pipelines_summary_requires_admin(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/pipelines", headers=headers["alice"])
    assert resp.status_code == 403


def test_pipeline_detail_requires_admin(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/pipelines/wf", headers=headers["alice"])
    assert resp.status_code == 403


def test_pipelines_summary_groups_by_org_and_pipeline_not_name_alone(rig):
    """Two orgs with a same-named pipeline must not be conflated into one
    summary row -- Run.pipeline is only unique per (org_id, name)."""
    client, headers = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        _add_run_with_events(db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed")
        _add_run_with_events(db, run_id="a-2", org_id=org_a, pipeline="wf", status="failed")
        _add_run_with_events(db, run_id="b-1", org_id=org_b, pipeline="wf", status="completed")

    resp = client.get("/api/admin/analytics/pipelines", headers=headers["op"])
    assert resp.status_code == 200
    rows = {(r["org"], r["pipeline"]): r for r in resp.json()["pipelines"]}

    assert rows[("org_a", "wf")]["total_runs"] == 2
    assert rows[("org_a", "wf")]["completed"] == 1
    assert rows[("org_a", "wf")]["failed"] == 1
    assert rows[("org_a", "wf")]["success_rate"] == 0.5
    assert rows[("org_b", "wf")]["total_runs"] == 1
    assert rows[("org_b", "wf")]["completed"] == 1
    assert rows[("org_b", "wf")]["success_rate"] == 1.0


def test_pipelines_summary_org_filter_scopes_to_one_org(rig):
    client, headers = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        _add_run_with_events(db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed")
        _add_run_with_events(db, run_id="b-1", org_id=org_b, pipeline="wf", status="completed")

    resp = client.get("/api/admin/analytics/pipelines", params={"org": "org_a"}, headers=headers["op"])
    assert {(w["org"], w["pipeline"]) for w in resp.json()["pipelines"]} == {("org_a", "wf")}


def test_pipelines_summary_unknown_org_is_404(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/pipelines", params={"org": "nope"}, headers=headers["op"])
    assert resp.status_code == 404


def test_pipelines_summary_token_and_cost_totals(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed",
            input_tokens=100, output_tokens=20, cost=0.1,
        )
        _add_run_with_events(
            db, run_id="a-2", org_id=org_a, pipeline="wf", status="completed",
            input_tokens=200, output_tokens=40, cost=0.3,
        )

    resp = client.get("/api/admin/analytics/pipelines", params={"org": "org_a"}, headers=headers["op"])
    assert resp.status_code == 200
    row = next(r for r in resp.json()["pipelines"] if r["pipeline"] == "wf")
    assert row["total_input_tokens"] == 300
    assert row["total_output_tokens"] == 60
    assert row["total_cost_estimate"] == pytest.approx(0.4)


def test_pipelines_summary_null_cost_when_no_usage_has_cost(rig):
    """A pipeline whose usage rows all have cost_estimate=None (e.g. a model
    not in model_catalog) reports total_cost_estimate=None, not 0."""
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed", cost=None,
        )

    resp = client.get("/api/admin/analytics/pipelines", params={"org": "org_a"}, headers=headers["op"])
    row = next(r for r in resp.json()["pipelines"] if r["pipeline"] == "wf")
    assert row["total_cost_estimate"] is None


def test_pipeline_detail_requires_org_when_ambiguous(rig):
    client, headers = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        _add_run_with_events(db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed")
        _add_run_with_events(db, run_id="b-1", org_id=org_b, pipeline="wf", status="completed")

    resp = client.get("/api/admin/analytics/pipelines/wf", headers=headers["op"])
    assert resp.status_code == 422


def test_pipeline_detail_unambiguous_without_org_when_only_one_org_has_it(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed")

    resp = client.get("/api/admin/analytics/pipelines/wf", headers=headers["op"])
    assert resp.status_code == 200
    assert resp.json()["org_id"] == org_a


def test_pipeline_detail_per_agent_usage(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed",
            agent="agent-x", input_tokens=100, output_tokens=20, cost=0.1,
        )
        _add_run_with_events(
            db, run_id="a-2", org_id=org_a, pipeline="wf", status="completed",
            agent="agent-x", input_tokens=200, output_tokens=40, cost=0.3,
        )

    resp = client.get("/api/admin/analytics/pipelines/wf", params={"org": "org_a"}, headers=headers["op"])
    assert resp.status_code == 200
    agent_row = next(a for a in resp.json()["per_agent"] if a["agent"] == "agent-x")
    assert agent_row["run_count"] == 2
    assert agent_row["avg_input_tokens"] == 150
    assert agent_row["avg_output_tokens"] == 30
    assert agent_row["avg_cost_estimate"] == pytest.approx(0.2)


def test_pipeline_detail_per_model_usage(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed",
            agent="agent-x", input_tokens=100, output_tokens=20, cost=0.1,
        )
        _add_run_with_events(
            db, run_id="a-2", org_id=org_a, pipeline="wf", status="completed",
            agent="agent-y", input_tokens=200, output_tokens=40, cost=0.3,
        )
        # Both runs use model="fake:x" via _add_run_with_events' default.

    resp = client.get("/api/admin/analytics/pipelines/wf", params={"org": "org_a"}, headers=headers["op"])
    assert resp.status_code == 200
    model_row = next(m for m in resp.json()["per_model"] if m["model"] == "fake:x")
    assert model_row["run_count"] == 2
    assert model_row["avg_input_tokens"] == 150
    assert model_row["avg_output_tokens"] == 30
    assert model_row["avg_cost_estimate"] == pytest.approx(0.2)


def test_pipeline_detail_per_model_buckets_null_model_as_unknown(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        db.add(Run(id="a-1", pipeline="wf", input="in", status="completed", org_id=org_a, username="test"))
        db.add(TraceEventRecord(run_id="a-1", seq=0, type="run_started", agent=None, data=None))
        db.add(TraceEventRecord(run_id="a-1", seq=1, type="run_completed", agent=None, data=None))
        db.add(
            UsageRecord(
                run_id="a-1", agent="agent-x", model=None, input_tokens=50, output_tokens=10,
                cost_estimate=None, org_id=org_a,
            )
        )
        db.commit()

    resp = client.get("/api/admin/analytics/pipelines/wf", params={"org": "org_a"}, headers=headers["op"])
    model_row = next(m for m in resp.json()["per_model"] if m["model"] == "(unknown model)")
    assert model_row["run_count"] == 1
    assert model_row["avg_input_tokens"] == 50


def test_pipeline_detail_common_failure_points(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(db, run_id="a-1", org_id=org_a, pipeline="wf", status="failed", agent="agent-x")
        _add_run_with_events(db, run_id="a-2", org_id=org_a, pipeline="wf", status="failed", agent="agent-x")

    resp = client.get("/api/admin/analytics/pipelines/wf", params={"org": "org_a"}, headers=headers["op"])
    points = resp.json()["common_failure_points"]
    assert points[0]["agent"] == "agent-x"
    assert points[0]["event_type"] == "tool_completed"
    assert points[0]["count"] == 2


def test_models_summary_requires_admin(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/models", headers=headers["alice"])
    assert resp.status_code == 403


def test_models_summary_aggregates_across_pipelines_and_orgs(rig):
    client, headers = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, pipeline="wf1", status="completed",
            input_tokens=100, output_tokens=20, cost=0.1,
        )
        _add_run_with_events(
            db, run_id="a-2", org_id=org_a, pipeline="wf2", status="completed",
            input_tokens=200, output_tokens=40, cost=0.3,
        )
        _add_run_with_events(
            db, run_id="b-1", org_id=org_b, pipeline="wf1", status="completed",
            input_tokens=50, output_tokens=10, cost=0.05,
        )
        # All three use model="fake:x" via _add_run_with_events' default.

    resp = client.get("/api/admin/analytics/models", headers=headers["op"])
    assert resp.status_code == 200
    row = next(m for m in resp.json()["models"] if m["model"] == "fake:x")
    assert row["run_count"] == 3
    assert row["total_input_tokens"] == 350
    assert row["total_output_tokens"] == 70
    assert row["total_cost_estimate"] == pytest.approx(0.45)


def test_models_summary_org_filter_scopes_to_one_org(rig):
    client, headers = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        _add_run_with_events(db, run_id="a-1", org_id=org_a, pipeline="wf", status="completed")
        _add_run_with_events(db, run_id="b-1", org_id=org_b, pipeline="wf", status="completed")

    resp = client.get("/api/admin/analytics/models", params={"org": "org_a"}, headers=headers["op"])
    assert resp.json()["models"][0]["run_count"] == 1


def test_models_summary_unknown_org_is_404(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/models", params={"org": "nope"}, headers=headers["op"])
    assert resp.status_code == 404


def test_models_summary_empty_scope_returns_empty_list(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/models", headers=headers["op"])
    assert resp.status_code == 200
    assert resp.json() == {"models": []}
