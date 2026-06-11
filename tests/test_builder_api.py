"""Tests for the builder session state-machine API (Phase 2)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from ui.backend import main as backend_main
from ui.backend.builder import _with_model_catalog
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.model_catalog import upsert_entry
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
        yield TestClient(backend_main.app)
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


_VALID_SPEC = {
    "name": "support_workflow",
    "knowledge_bases": [],
    "agents": [
        {
            "name": "support_agent",
            "role": "Customer Support Specialist",
            "goal": "Answer customer questions",
            "backstory": "",
            "model": "fake:hello",
            "tools": [],
            "display_name": "Support Specialist",
            "friendly_description": "Answers customer questions.",
        }
    ],
    "teams": [
        {
            "name": "support_team",
            "agents": ["support_agent"],
            "mode": "sequential",
            "manager": None,
            "display_name": "Support Team",
            "friendly_description": "The support specialist handles every request.",
        }
    ],
    "workflow": {"steps": ["support_team"]},
}

_INVALID_SPEC = {**_VALID_SPEC, "agents": [{**_VALID_SPEC["agents"][0], "tools": ["does_not_exist"]}]}


def test_create_session_starts_in_intent_stage(client):
    resp = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot", "as_is_text": "Email today"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "intent"
    assert body["intent_text"] == "We need a support bot"
    assert body["feedback_history"] == []


def test_get_session_returns_404_for_unknown_id(client):
    resp = client.get("/api/builder/sessions/does-not-exist")
    assert resp.status_code == 404


def test_submit_requirements_with_confirmed_payload(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    requirements = {"summary": "Faster support", "pain_points": ["slow replies"], "goals": ["reply within an hour"]}
    resp = client.post(f"/api/builder/sessions/{session_id}/requirements", json={"requirements": requirements})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "requirements"
    assert body["requirements_json"]["summary"] == "Faster support"


def test_submit_requirements_requires_payload_or_model(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "x"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/requirements", json={})

    assert resp.status_code == 400


def test_submit_specification_with_valid_payload(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "spec"
    assert body["specification_json"]["name"] == "support_workflow"


def test_submit_specification_rejects_invalid_payload(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _INVALID_SPEC})

    assert resp.status_code == 400
    assert "Unknown tool" in resp.json()["detail"]


def test_solution_feedback_appends_history_and_accepts_revised_spec(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(
        f"/api/builder/sessions/{session_id}/solution",
        json={"feedback": "Looks good", "specification": _VALID_SPEC},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "solution"
    assert len(body["feedback_history"]) == 1
    assert body["feedback_history"][0]["note"] == "Looks good"


def test_test_run_requires_specification(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/test-runs", json={"input": "hello"})

    assert resp.status_code == 400


def test_test_run_executes_validated_specification(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(f"/api/builder/sessions/{session_id}/test-runs", json={"input": "hi"})

    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    session = client.get(f"/api/builder/sessions/{session_id}").json()
    assert session["status"] == "testing"

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["workflow"] == "support_workflow"


def test_deploy_requires_specification(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")

    assert resp.status_code == 400


def test_deploy_persists_workflow_record_and_marks_session_deployed(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deployed"

    workflows = client.get("/api/workflows").json()["workflows"]
    assert "support_workflow" in workflows

    config = client.get("/api/config/workflows/support_workflow").json()
    assert config["status"] == "deployed"
    assert config["config"]["name"] == "support_workflow"


def test_deployed_workflow_can_be_run_via_get_workflow(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    client.post(f"/api/builder/sessions/{session_id}/deploy")

    resp = client.post("/api/runs", json={"workflow": "support_workflow", "input": "hi"})

    assert resp.status_code == 200


def test_with_model_catalog_appends_catalog_text_when_present():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)

    with Session() as db:
        assert _with_model_catalog(db, "Requirements text") == "Requirements text"

        upsert_entry(db, "openai:gpt-4o-mini", display_name="Quick Assistant", tier="fast")

        with_catalog = _with_model_catalog(db, "Requirements text")

    assert with_catalog.startswith("Requirements text\n\n")
    assert "openai:gpt-4o-mini" in with_catalog
    assert "Quick Assistant" in with_catalog
