"""GET /api/org/activity-overview -- the customer-facing Activity Overview
tab's engagement stats. Org-scoped, no model/cost anywhere in the response
(see ui/backend/activity_overview.py)."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db


@pytest.fixture
def client(monkeypatch, tmp_path):
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
        c = TestClient(backend_main.app)
        token = create_user_and_login(c)  # plain org member of 'default'
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _seed_run(db, org_id, *, run_id, age_days=0, pipeline="support", status="completed"):
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    db.add(Run(
        id=run_id, pipeline=pipeline, input="do the thing", output="done",
        status=status, org_id=org_id, created_at=created,
    ))


def test_no_runs_yet_is_a_clean_zero_state(client):
    body = client.get("/api/org/activity-overview").json()

    assert body["sessions"] == 0
    assert body["active_days"] == 0
    assert body["current_streak"] == 0
    assert body["longest_streak"] == 0
    assert body["peak_hour"] is None
    assert len(body["daily_counts"]) == 12 * 7


def test_counts_this_orgs_runs(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        _seed_run(db, org_id, run_id="r1", age_days=0)
        _seed_run(db, org_id, run_id="r2", age_days=1)
        db.commit()

    body = client.get("/api/org/activity-overview").json()

    assert body["sessions"] == 2
    assert body["active_days"] == 2


def test_never_leaks_another_orgs_runs(client):
    with open_test_db() as db:
        other_org_id = get_or_create_org(db, "beta").id
        _seed_run(db, other_org_id, run_id="beta-1", age_days=0)
        db.commit()

    body = client.get("/api/org/activity-overview").json()

    assert body["sessions"] == 0


def test_completed_count_and_team_breakdown_reflect_only_completed_runs(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        _seed_run(db, org_id, run_id="r1", pipeline="support", status="completed")
        _seed_run(db, org_id, run_id="r2", pipeline="support", status="completed")
        _seed_run(db, org_id, run_id="r3", pipeline="sales", status="completed")
        _seed_run(db, org_id, run_id="r4", pipeline="sales", status="failed")
        db.commit()

    body = client.get("/api/org/activity-overview").json()

    assert body["sessions"] == 4
    assert body["completed_count"] == 3
    assert body["team_counts"] == [
        {"pipeline": "support", "count": 2},
        {"pipeline": "sales", "count": 1},
    ]


def test_response_never_names_a_model_or_a_cost(client):
    # This tab is reachable by any org member -- see EmailBudgetSettings.tsx
    # and the MonitorPage "Not Found" fix for why model/spend stay admin-only.
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        _seed_run(db, org_id, run_id="r1")
        db.commit()

    body = client.get("/api/org/activity-overview").json()

    forbidden = {"model", "cost", "cost_estimate", "spent", "spend", "tokens", "input_tokens", "output_tokens"}
    assert forbidden.isdisjoint(body.keys())
