"""Trace-event persistence (run_in_background writes TraceEventRecord rows)
and the read endpoints that expose them: GET /api/runs/{id}/trace and
GET /api/runs (list, with filters)."""

import json

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")
fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from bestteam import AgentSpec, Specification, TeamSpec, PipelineSpec, validate_specification
from helpers import create_user_and_login, get_org_id, make_concurrent_safe_engine, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run, TraceEventRecord, UsageRecord, PipelineRecord, PipelineVersion
from ui.backend.db_session import get_db
from ui.backend.runtime import registry, run_in_background


def _engine(tmp_path):
    e = make_concurrent_safe_engine(tmp_path)
    init_db(e)
    return e


def _pipeline(tmp_path, response="done"):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model=f"fake:{response}")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    return validate_specification(spec, source=tmp_path / "w.yaml")


def test_run_in_background_publishes_run_queued_to_the_live_registry_log(tmp_path):
    # The DB-persisted trace_events includes a synthesized run_queued bookend
    # (see test below); the live registry log a WS subscriber replays from
    # must carry the same event, or a live view starts at run_started while
    # the historical view starts at run_queued.
    engine = _engine(tmp_path)
    wf = _pipeline(tmp_path)
    run = registry.create("w", "in")

    run_in_background(run.id, wf, "in", engine=engine)

    types = [e["type"] for e in registry.get(run.id).events]
    assert types[0] == "run_queued"


def test_run_in_background_persists_trace_events_in_seq_order(tmp_path):
    engine = _engine(tmp_path)
    Session = session_factory(engine)
    wf = _pipeline(tmp_path)
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


def test_get_run_trace_returns_persisted_events_in_seq_order(client, tmp_path):
    org_id = get_org_id()
    with open_test_db() as db:
        engine = db.get_bind()
    run = registry.create("w", "in", org_id=org_id, username="test")
    with open_test_db() as db:
        db.add(Run(id=run.id, pipeline="w", input="in", status="running", org_id=org_id, username="test"))
        db.commit()
    wf = _pipeline(tmp_path)
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


def test_get_run_trace_includes_per_agent_usage(client):
    # Additive field for the admin trace view -- the existing customer
    # RunDetail.tsx only reads `events` and ignores this.
    org_id = get_org_id()
    with open_test_db() as db:
        db.add(Run(id="r-1", pipeline="wf-a", input="in", status="completed", org_id=org_id, username="test"))
        db.add(
            UsageRecord(
                run_id="r-1", agent="agent-a", model="fake:x", input_tokens=10, output_tokens=5,
                cost_estimate=0.01, org_id=org_id,
            )
        )
        db.commit()

    resp = client.get("/api/runs/r-1/trace")

    assert resp.status_code == 200
    usage = resp.json()["usage"]
    assert usage == [
        {"agent": "agent-a", "model": "fake:x", "input_tokens": 10, "output_tokens": 5, "cost_estimate": 0.01}
    ]


def test_get_run_trace_cross_org_is_404(client):
    with open_test_db() as db:
        db.add(Run(id="other-org-run", pipeline="w", input="in", status="completed", org_id=999999))
        db.commit()

    resp = client.get("/api/runs/other-org-run/trace")

    assert resp.status_code == 404


def test_get_run_trace_unknown_run_is_404(client):
    resp = client.get("/api/runs/does-not-exist/trace")

    assert resp.status_code == 404


def test_list_runs_by_run_id_cross_org_is_404(client):
    """An explicit run_id lookup on GET /api/runs is a targeted probe, not a
    passive filter -- a run belonging to another org must 404 like
    GET /api/runs/{id}, not silently return an empty `runs` list (Codex
    review finding: that would let a caller distinguish "not yours" from
    "doesn't exist" by diffing it against a real 404 elsewhere)."""
    with open_test_db() as db:
        db.add(Run(id="other-org-run", pipeline="w", input="in", status="completed", org_id=999999))
        db.commit()

    resp = client.get("/api/runs", params={"run_id": "other-org-run"})

    assert resp.status_code == 404


def test_list_runs_by_run_id_unknown_is_404(client):
    resp = client.get("/api/runs", params={"run_id": "does-not-exist"})

    assert resp.status_code == 404


def test_list_runs_by_run_id_returns_that_run_for_its_own_org(client):
    org_id = get_org_id()
    with open_test_db() as db:
        db.add(Run(id="r-1", pipeline="wf-a", input="in", status="completed", org_id=org_id, username="test"))
        db.add(Run(id="r-2", pipeline="wf-b", input="in", status="failed", org_id=org_id, username="test"))
        db.commit()

    resp = client.get("/api/runs", params={"run_id": "r-1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [r["id"] for r in body["runs"]] == ["r-1"]


def test_list_runs_started_at_is_utc_qualified(client):
    org_id = get_org_id()
    with open_test_db() as db:
        db.add(Run(id="r-1", pipeline="wf-a", input="in", status="completed", org_id=org_id, username="test"))
        db.commit()

    resp = client.get("/api/runs")

    started_at = resp.json()["runs"][0]["started_at"]
    assert started_at.endswith("+00:00") or started_at.endswith("Z"), (
        f"started_at {started_at!r} must carry a UTC offset, or new Date() on the "
        "frontend misreads it as browser-local time"
    )


def test_list_runs_includes_the_team_display_name_from_the_pinned_version(client):
    # customer_support_team is the internal technical name (used for the
    # `pipeline` filter/API identity); the Activity page's run cards should
    # show the customer-facing team name instead, same as My Teams.
    org_id = get_org_id()
    with open_test_db() as db:
        record = PipelineRecord(name="customer_support_team", org_id=org_id, config={}, status="deployed")
        db.add(record)
        db.flush()
        version = PipelineVersion(
            pipeline_id=record.id,
            version_number=1,
            config={"name": "customer_support_team", "teams": [{"name": "t", "display_name": "Customer Support Team"}]},
        )
        db.add(version)
        db.flush()
        db.add(
            Run(
                id="r-1",
                pipeline="customer_support_team",
                input="in",
                status="completed",
                org_id=org_id,
                username="test",
                pipeline_version_id=version.id,
            )
        )
        db.commit()

    resp = client.get("/api/runs")

    row = resp.json()["runs"][0]
    assert row["team_display_name"] == "Customer Support Team"


def test_list_runs_team_display_name_is_null_without_a_pinned_version(client):
    # A sandbox test-run (or a pre-migration row) has no pipeline_version_id --
    # the frontend falls back to the raw pipeline name in that case.
    org_id = get_org_id()
    with open_test_db() as db:
        db.add(Run(id="r-1", pipeline="wf-a", input="in", status="completed", org_id=org_id, username="test"))
        db.commit()

    resp = client.get("/api/runs")

    assert resp.json()["runs"][0]["team_display_name"] is None


def test_list_runs_manual_filter_includes_null_username_rows(client):
    # A legacy/pre-migration run with no recorded username is classified
    # autonomous: False in the unfiltered response (it isn't the email
    # trigger), so manual=true must include it too -- SQL's
    # `username != 'email-trigger'` is UNKNOWN (excluded) for a NULL username.
    org_id = get_org_id()
    with open_test_db() as db:
        db.add(Run(id="r-null-username", pipeline="wf-a", input="in", status="completed", org_id=org_id, username=None))
        db.commit()

    resp = client.get("/api/runs")
    row = next(r for r in resp.json()["runs"] if r["id"] == "r-null-username")
    assert row["autonomous"] is False

    resp = client.get("/api/runs", params={"manual": "true"})
    assert {r["id"] for r in resp.json()["runs"]} == {"r-null-username"}

    resp = client.get("/api/runs", params={"manual": "false"})
    assert resp.json()["runs"] == []


def test_list_runs_is_paginated(client):
    org_id = get_org_id()
    with open_test_db() as db:
        db.add_all(
            [
                Run(id=f"r-{i}", pipeline="wf-a", input="in", status="completed", org_id=org_id, username="test")
                for i in range(3)
            ]
        )
        db.commit()

    resp = client.get("/api/runs", params={"limit": 2})
    body = resp.json()
    assert len(body["runs"]) == 2
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0

    resp = client.get("/api/runs", params={"limit": 2, "offset": 2})
    body = resp.json()
    assert len(body["runs"]) == 1
    assert body["total"] == 3


def test_list_runs_defaults_to_a_bounded_page(client):
    resp = client.get("/api/runs")

    assert resp.json()["limit"] == 50


def test_list_runs_filters_by_manual_pipeline_and_status(client):
    org_id = get_org_id()
    with open_test_db() as db:
        db.add_all(
            [
                Run(id="r-manual", pipeline="wf-a", input="in", status="completed", org_id=org_id, username="test"),
                Run(id="r-auto", pipeline="wf-a", input="in", status="completed", org_id=org_id, username="email-trigger"),
                Run(id="r-other-wf", pipeline="wf-b", input="in", status="failed", org_id=org_id, username="test"),
                Run(id="r-other-org", pipeline="wf-a", input="in", status="completed", org_id=org_id + 1000, username="test"),
            ]
        )
        db.commit()

    resp = client.get("/api/runs", params={"manual": "true"})
    assert {r["id"] for r in resp.json()["runs"]} == {"r-manual", "r-other-wf"}

    resp = client.get("/api/runs", params={"pipeline": "wf-a"})
    assert {r["id"] for r in resp.json()["runs"]} == {"r-manual", "r-auto"}

    resp = client.get("/api/runs", params={"status": "failed"})
    assert {r["id"] for r in resp.json()["runs"]} == {"r-other-wf"}

    resp = client.get("/api/runs")
    runs = resp.json()["runs"]
    assert {r["id"] for r in runs} == {"r-manual", "r-auto", "r-other-wf"}
    auto_row = next(r for r in runs if r["id"] == "r-auto")
    assert auto_row["autonomous"] is True


def test_only_a_platform_admin_sees_a_runs_internal_error(client):
    """`runs.internal_error` is the operator's copy of why a run failed -- a
    provider's own text, which the customer's `output` deliberately no longer
    carries (see runtime.py). The endpoint is shared by RunDetail.tsx and
    AdminRunDetail.tsx, so the gate has to be here, not in the component."""
    org_id = get_org_id()
    with open_test_db() as db:
        db.add(
            Run(
                id="r-boom", pipeline="wf-a", input="in",
                output="The run failed due to an internal error.",
                internal_error="Error calling model 'gemini-3.7-flash' (RESOURCE_EXHAUSTED)",
                status="failed", org_id=org_id, username="test",
            )
        )
        db.commit()

    member = client.get("/api/runs/r-boom/trace")
    assert member.status_code == 200
    assert "internal_error" not in member.json()

    admin_token = create_user_and_login(client, username="op", org=None, admin=True)
    admin = client.get(
        "/api/runs/r-boom/trace", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert admin.status_code == 200
    assert admin.json()["internal_error"] == (
        "Error calling model 'gemini-3.7-flash' (RESOURCE_EXHAUSTED)"
    )
