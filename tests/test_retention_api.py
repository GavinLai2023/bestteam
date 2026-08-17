"""Phase 3b: the retention/export HTTP surface."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import AutomationItemResult, Run, TraceEventRecord, UsageRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db


@pytest.fixture
def client(monkeypatch, tmp_path):
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
        c = TestClient(backend_main.app)
        token = create_user_and_login(c)  # plain org member of 'default'
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _seed_run(db, org_id, *, run_id, status="completed", age_days=0):
    """One run with all three kinds of content a purge clears."""
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    db.add(Run(
        id=run_id, workflow="support", input="From alice@example.com: my boiler leaks",
        output="Drafted a reply to alice@example.com", status=status,
        org_id=org_id, created_at=created,
    ))
    db.add(TraceEventRecord(run_id=run_id, seq=1, type="agent_completed",
                            agent="writer", data='{"text": "alice@example.com"}'))
    db.add(UsageRecord(run_id=run_id, agent="writer", model="fake:",
                       input_tokens=10, output_tokens=5, org_id=org_id))
    db.add(AutomationItemResult(
        org_id=org_id, run_id=run_id, source_key=f"mbx:{run_id}", result_type="email",
        status="processed", needs_attention=False,
        payload={"sender": "alice@example.com", "summary": "boiler"},
    ))
    return run_id


@pytest.fixture
def seeded_runs(client):
    """One 40-day-old completed run and one fresh one, for the client's org."""
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        ids = {
            "old": _seed_run(db, org_id, run_id="old", age_days=40),
            "new": _seed_run(db, org_id, run_id="new", age_days=2),
        }
        db.commit()
    return ids


@pytest.fixture
def other_org_run(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "beta").id
        run_id = _seed_run(db, org_id, run_id="beta-1", age_days=40)
        db.commit()
    return run_id


@pytest.fixture
def running_run(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        run_id = _seed_run(db, org_id, run_id="live", status="running")
        db.commit()
    return run_id


# --- /api/org/retention -------------------------------------------------------


def test_get_retention_defaults_to_off(client):
    body = client.get("/api/org/retention").json()
    assert body["run_retention_days"] is None
    assert body["last_swept_at"] is None
    assert body["purgeable_now"] == 0


def test_put_retention_round_trips(client):
    assert client.put("/api/org/retention", json={"run_retention_days": 30}).status_code == 200
    assert client.get("/api/org/retention").json()["run_retention_days"] == 30


def test_put_retention_rejects_out_of_range(client):
    assert client.put("/api/org/retention", json={"run_retention_days": 0}).status_code == 422
    assert client.put("/api/org/retention", json={"run_retention_days": 99999}).status_code == 422


def test_put_retention_null_turns_it_off(client):
    client.put("/api/org/retention", json={"run_retention_days": 30})
    assert client.put("/api/org/retention", json={"run_retention_days": None}).status_code == 200
    assert client.get("/api/org/retention").json()["run_retention_days"] is None


def test_purgeable_now_counts_what_the_policy_would_take(client, seeded_runs):
    client.put("/api/org/retention", json={"run_retention_days": 30})
    assert client.get("/api/org/retention").json()["purgeable_now"] == 1


def test_purge_requires_an_explicit_window(client):
    # No body at all: this is a destructive button and must say what it does.
    assert client.post("/api/org/retention/purge", json={}).status_code == 422


def test_purge_removes_and_reports(client, seeded_runs):
    body = client.post("/api/org/retention/purge", json={"older_than_days": 30}).json()
    assert body["purged"] == 1


# --- /api/org/export ----------------------------------------------------------


def test_export_returns_an_attachment(client, seeded_runs):
    resp = client.get("/api/org/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.json()["truncated"] is False
    assert len(resp.json()["runs"]) >= 1


def test_export_respects_the_cap(client, seeded_runs, monkeypatch):
    monkeypatch.setenv("BESTTEAM_EXPORT_MAX_RUNS", "1")
    body = client.get("/api/org/export").json()
    assert body["truncated"] is True
    assert len(body["runs"]) == 1


# --- /api/runs/{run_id}/purge -------------------------------------------------


def test_purge_one_run(client, seeded_runs):
    assert client.post(f"/api/runs/{seeded_runs['old']}/purge").json() == {"purged": True}


def test_purging_twice_is_not_an_error(client, seeded_runs):
    client.post(f"/api/runs/{seeded_runs['old']}/purge")
    assert client.post(f"/api/runs/{seeded_runs['old']}/purge").json() == {"purged": False}


def test_purge_another_orgs_run_is_404(client, other_org_run):
    assert client.post(f"/api/runs/{other_org_run}/purge").status_code == 404


def test_purge_a_running_run_is_409(client, running_run):
    assert client.post(f"/api/runs/{running_run}/purge").status_code == 409
