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
    # Only the breakdown itself is this test's subject; the `deleted` flag has
    # its own test below.
    assert [(tc["pipeline"], tc["count"]) for tc in body["team_counts"]] == [
        ("support", 2),
        ("sales", 1),
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


def test_a_deleted_team_is_counted_but_flagged_and_a_live_one_is_not(client):
    # `pipelines` rows are hard-deleted (crud.py:576), so "no row with that
    # name in this org" is exactly what deleted means. The run keeps the name
    # it ran under, which is how the history survives the deletion at all.
    from ui.backend.db.models import PipelineRecord

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        db.add(PipelineRecord(name="support", org_id=org_id, config={}, status="deployed"))
        _seed_run(db, org_id, run_id="r1", pipeline="support")
        _seed_run(db, org_id, run_id="r2", pipeline="support")
        _seed_run(db, org_id, run_id="r3", pipeline="e2e_gone_team")
        db.commit()

    body = client.get("/api/org/activity-overview").json()

    assert body["completed_count"] == 3, "a deleted team's work still happened"
    assert body["team_counts"] == [
        {"pipeline": "support", "count": 2, "deleted": False},
        {"pipeline": "e2e_gone_team", "count": 1, "deleted": True},
    ]


def test_another_orgs_live_team_does_not_unmark_this_orgs_deleted_one(client):
    # The live-name lookup has to be org-scoped like everything else here:
    # two orgs may well both have run a team called "support".
    from ui.backend.db.models import PipelineRecord

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        other_id = get_or_create_org(db, "other-org").id
        db.add(PipelineRecord(name="support", org_id=other_id, config={}, status="deployed"))
        _seed_run(db, org_id, run_id="r1", pipeline="support")
        db.commit()

    body = client.get("/api/org/activity-overview").json()

    assert body["team_counts"] == [{"pipeline": "support", "count": 1, "deleted": True}]
