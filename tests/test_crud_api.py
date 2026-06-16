"""Tests for the `/api/config` CRUD API (Phase 2) -- the "advanced view" for
fine-tuning agents/teams/knowledge_bases/workflows directly."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from ui.backend import main as backend_main
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
        token = test_client.post("/api/auth/register", json={"username": "test", "password": "test"}).json()["access_token"]
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_agent_crud_round_trip(client):
    config = {"role": "Support", "goal": "Help customers", "model": "fake:hi", "tools": []}

    create = client.put("/api/config/agents/support_agent", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["role"] == "Support"

    listed = client.get("/api/config/agents")
    assert [item["name"] for item in listed.json()] == ["support_agent"]

    fetched = client.get("/api/config/agents/support_agent")
    assert fetched.status_code == 200
    assert fetched.json()["config"]["goal"] == "Help customers"

    deleted = client.delete("/api/config/agents/support_agent")
    assert deleted.status_code == 204
    assert client.get("/api/config/agents/support_agent").status_code == 404


def test_agent_put_rejects_invalid_shape(client):
    resp = client.put("/api/config/agents/support_agent", json={"role": "Support"})
    assert resp.status_code == 400


def test_team_crud_round_trip(client):
    config = {"agents": ["support_agent"], "mode": "sequential"}

    create = client.put("/api/config/teams/support_team", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["agents"] == ["support_agent"]

    assert client.get("/api/config/teams/support_team").status_code == 200
    assert client.delete("/api/config/teams/support_team").status_code == 204


def test_knowledge_base_put_omits_vector_only_fields_for_local_folder(client):
    config = {"path": "./docs", "type": "local_folder", "embedding_model": "fake:8"}

    resp = client.put("/api/config/knowledge_bases/docs", json=config)

    assert resp.status_code == 200
    assert "embedding_model" not in resp.json()["config"]


def test_unknown_agent_returns_404(client):
    assert client.get("/api/config/agents/does-not-exist").status_code == 404
    assert client.delete("/api/config/agents/does-not-exist").status_code == 404


_VALID_WORKFLOW_CONFIG = {
    "knowledge_bases": [],
    "agents": [
        {
            "name": "support_agent",
            "role": "Customer Support Specialist",
            "goal": "Answer customer questions",
            "model": "fake:hello",
        }
    ],
    "teams": [{"name": "support_team", "agents": ["support_agent"], "mode": "sequential"}],
    "workflow": {"steps": ["support_team"]},
}


def test_workflow_crud_round_trip_and_validation(client):
    create = client.put("/api/config/workflows/support_workflow", json=_VALID_WORKFLOW_CONFIG)
    assert create.status_code == 200
    body = create.json()
    assert body["status"] == "draft"
    assert body["config"]["name"] == "support_workflow"

    listed = client.get("/api/config/workflows")
    assert [item["name"] for item in listed.json()] == ["support_workflow"]

    fetched = client.get("/api/config/workflows/support_workflow")
    assert fetched.status_code == 200

    assert client.delete("/api/config/workflows/support_workflow").status_code == 204
    assert client.get("/api/config/workflows/support_workflow").status_code == 404


def test_workflow_put_rejects_invalid_config(client):
    bad_config = {**_VALID_WORKFLOW_CONFIG, "teams": [{"name": "support_team", "agents": ["does_not_exist"], "mode": "sequential"}]}

    resp = client.put("/api/config/workflows/support_workflow", json=bad_config)

    assert resp.status_code == 400
    assert "unknown agent" in resp.json()["detail"]


def test_workflow_config_is_runnable_via_get_workflow(client):
    client.put("/api/config/workflows/support_workflow", json=_VALID_WORKFLOW_CONFIG)

    resp = client.post("/api/runs", json={"workflow": "support_workflow", "input": "hi"})

    assert resp.status_code == 200


def test_skill_crud_round_trip(client):
    config = {"instructions": "Use web_search for research.", "tools": []}

    create = client.put("/api/config/skills/research_skill", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["instructions"] == "Use web_search for research."

    listed = client.get("/api/config/skills")
    assert [item["name"] for item in listed.json()] == ["research_skill"]

    fetched = client.get("/api/config/skills/research_skill")
    assert fetched.status_code == 200
    assert fetched.json()["config"].get("tools", []) == []

    deleted = client.delete("/api/config/skills/research_skill")
    assert deleted.status_code == 204
    assert client.get("/api/config/skills/research_skill").status_code == 404


def test_skill_put_rejects_missing_instructions(client):
    resp = client.put("/api/config/skills/bad_skill", json={"description": "no instructions"})
    assert resp.status_code == 400


def test_workflow_put_accepts_skill_reference_when_skill_exists(client):
    client.put("/api/config/skills/research_skill", json={
        "instructions": "Research topics thoroughly.",
        "tools": [],
    })
    config = {
        "knowledge_bases": [],
        "agents": [{
            "name": "agent1",
            "role": "Researcher",
            "goal": "Research topics",
            "model": "fake:hello",
            "tools": [],
            "skills": ["research_skill"],
        }],
        "teams": [{"name": "team1", "agents": ["agent1"], "mode": "sequential"}],
        "workflow": {"steps": ["team1"]},
    }
    resp = client.put("/api/config/workflows/my_workflow", json=config)
    assert resp.status_code == 200


def test_workflow_put_rejects_unknown_skill_reference(client):
    config = {
        "knowledge_bases": [],
        "agents": [{
            "name": "agent1",
            "role": "Researcher",
            "goal": "Research topics",
            "model": "fake:hello",
            "tools": [],
            "skills": ["nonexistent_skill"],
        }],
        "teams": [{"name": "team1", "agents": ["agent1"], "mode": "sequential"}],
        "workflow": {"steps": ["team1"]},
    }
    resp = client.put("/api/config/workflows/my_workflow", json=config)
    assert resp.status_code == 400
    assert "Unknown skill" in resp.json()["detail"]
