"""Cross-org isolation matrix: the consolidated leak-prevention net.

Two customer orgs (alice@org_a, bob@org_b) and a platform operator (op,
org-NULL admin) share one backend. Complements the per-surface tests
(admin /api/config targeting in test_crud_api, run/WS ownership in
test_ws_stream, builder scoping in test_builder_api) with the org-user-level
workflow surface and the platform-operator boundaries.
"""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db

_WORKFLOW_CONFIG = {
    "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:done"}],
    "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["team"]},
}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """client + bearer headers for alice (org_a), bob (org_b), op (platform admin)."""
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
        client = TestClient(backend_main.app)
        headers = {
            "alice": {"Authorization": f"Bearer {create_user_and_login(client, username='alice', org='org_a')}"},
            "bob": {"Authorization": f"Bearer {create_user_and_login(client, username='bob', org='org_b')}"},
            "op": {"Authorization": f"Bearer {create_user_and_login(client, username='op', org=None, admin=True)}"},
        }
        yield client, headers, tmp_path
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _deploy_workflow(client, headers, org_name, workflow_name, config=_WORKFLOW_CONFIG):
    resp = client.put(
        f"/api/config/workflows/{workflow_name}?org={org_name}", json=config, headers=headers["op"]
    )
    assert resp.status_code == 200, resp.text


def test_workflow_list_and_graph_are_org_scoped(rig):
    client, headers, workflows_dir = rig
    _deploy_workflow(client, headers, "org_a", "secret_wf")
    (workflows_dir / "demo.yaml").write_text(
        "name: demo\n"
        "agents:\n"
        "  - name: a\n"
        "    role: r\n"
        "    goal: g\n"
        '    model: "fake:hi"\n'
        "teams:\n"
        "  - name: t\n"
        "    agents: [a]\n"
        "    mode: sequential\n"
        "workflow:\n"
        "  steps: [t]\n",
        encoding="utf-8",
    )

    alice_list = client.get("/api/workflows", headers=headers["alice"]).json()["workflows"]
    bob_list = client.get("/api/workflows", headers=headers["bob"]).json()["workflows"]
    assert "secret_wf" in alice_list
    assert "secret_wf" not in bob_list
    # Global YAML demos are visible to every org.
    assert "demo" in alice_list and "demo" in bob_list

    assert client.get("/api/workflows/secret_wf/graph", headers=headers["alice"]).status_code == 200
    assert client.get("/api/workflows/secret_wf/graph", headers=headers["bob"]).status_code == 404


def test_running_another_orgs_workflow_is_404(rig):
    client, headers, _ = rig
    _deploy_workflow(client, headers, "org_a", "secret_wf")

    run_req = {"workflow": "secret_wf", "input": "hi"}
    assert client.post("/api/runs", json=run_req, headers=headers["bob"]).status_code == 404

    run_resp = client.post("/api/runs", json=run_req, headers=headers["alice"])
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]
    # The run itself is org-guarded too.
    assert client.get(f"/api/runs/{run_id}", headers=headers["bob"]).status_code == 404
    assert client.get(f"/api/runs/{run_id}", headers=headers["alice"]).status_code == 200
    assert client.get(f"/api/runs/{run_id}", headers=headers["op"]).status_code == 200


def test_same_named_workflow_runs_independently_per_org(rig):
    client, headers, _ = rig
    config_a = {**_WORKFLOW_CONFIG, "agents": [{**_WORKFLOW_CONFIG["agents"][0], "model": "fake:AAA"}]}
    config_b = {**_WORKFLOW_CONFIG, "agents": [{**_WORKFLOW_CONFIG["agents"][0], "model": "fake:BBB"}]}
    _deploy_workflow(client, headers, "org_a", "wf", config_a)
    _deploy_workflow(client, headers, "org_b", "wf", config_b)

    # Each org's user runs THEIR "wf" -- the (org_id, name) cache key keeps
    # the two builds separate even when org A's build is cached first.
    a_run = client.post("/api/runs", json={"workflow": "wf", "input": "x"}, headers=headers["alice"])
    b_run = client.post("/api/runs", json={"workflow": "wf", "input": "x"}, headers=headers["bob"])
    assert a_run.status_code == 200 and b_run.status_code == 200

    a_events = client.get(f"/api/runs/{a_run.json()['run_id']}", headers=headers["alice"])
    b_events = client.get(f"/api/runs/{b_run.json()['run_id']}", headers=headers["bob"])
    assert a_events.status_code == 200 and b_events.status_code == 200


def test_workflow_cannot_reference_another_orgs_knowledge_base(rig, tmp_path):
    client, headers, _ = rig
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "a.txt").write_text("org A's private docs", encoding="utf-8")
    resp = client.put(
        "/api/config/knowledge_bases/private_kb?org=org_a",
        json={"path": str(docs), "type": "local_folder"},
        headers=headers["op"],
    )
    assert resp.status_code == 200

    leaky_config = {
        **_WORKFLOW_CONFIG,
        "agents": [{**_WORKFLOW_CONFIG["agents"][0], "tools": ["private_kb"]}],
    }
    # org B's workflow referencing org A's KB name must not resolve it.
    resp = client.put(
        "/api/config/workflows/leaky_wf?org=org_b", json=leaky_config, headers=headers["op"]
    )
    assert resp.status_code == 400
    assert "private_kb" in resp.json()["detail"]
    # The same reference from org A itself is fine.
    resp = client.put(
        "/api/config/workflows/ok_wf?org=org_a", json=leaky_config, headers=headers["op"]
    )
    assert resp.status_code == 200


def test_org_skill_shadows_builtin_and_stays_private(rig):
    client, headers, _ = rig
    # Platform built-in (org omitted = built-in tier) visible to both orgs.
    assert client.put(
        "/api/config/skills/playbook",
        json={"instructions": "Platform default playbook.", "tools": []},
        headers=headers["op"],
    ).status_code == 200
    # org_a's own version shadows the built-in for org_a only.
    assert client.put(
        "/api/config/skills/playbook?org=org_a",
        json={"instructions": "Org A's custom playbook.", "tools": []},
        headers=headers["op"],
    ).status_code == 200

    from helpers import get_org_id, open_test_db
    from ui.backend.skills import load_skills

    with open_test_db() as db:
        a_skills = load_skills(db, get_org_id("org_a"))
        b_skills = load_skills(db, get_org_id("org_b"))
    assert a_skills["playbook"].instructions == "Org A's custom playbook."
    assert b_skills["playbook"].instructions == "Platform default playbook."


def test_platform_operator_gets_403_on_org_user_surfaces(rig):
    client, headers, _ = rig
    assert client.get("/api/workflows", headers=headers["op"]).status_code == 403
    assert client.post("/api/runs", json={"workflow": "x", "input": "y"}, headers=headers["op"]).status_code == 403
    assert client.get("/api/builder/sessions", headers=headers["op"]).status_code == 403
    assert (
        client.post("/api/builder/sessions", json={"intent_text": "x"}, headers=headers["op"]).status_code
        == 403
    )


def test_org_users_cannot_reach_admin_surfaces(rig):
    client, headers, _ = rig
    assert client.get("/api/config/agents", headers=headers["alice"]).status_code == 403
    assert client.get("/api/memory/users", headers=headers["alice"]).status_code == 403
