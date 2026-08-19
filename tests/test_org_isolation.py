"""Cross-org isolation matrix: the consolidated leak-prevention net.

Two customer orgs (alice@org_a, bob@org_b) and a platform operator (op,
org-NULL admin) share one backend. Complements the per-surface tests
(admin /api/config targeting in test_crud_api, run/WS ownership in
test_ws_stream, builder scoping in test_builder_api) with the org-user-level
pipeline surface and the platform-operator boundaries.
"""

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, get_org_id, make_concurrent_safe_engine, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run
from ui.backend.db_session import get_db

_PIPELINE_CONFIG = {
    "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:done"}],
    "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
    "pipeline": {"steps": ["team"]},
}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """client + bearer headers for alice (org_a), bob (org_b), op (platform admin)."""
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()

    # Several tests here dispatch real runs via POST /api/runs, so a worker
    # thread's Session overlaps the request's -- see the helper's docstring.
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
        client = TestClient(backend_main.app)
        headers = {
            "alice": {"Authorization": f"Bearer {create_user_and_login(client, username='alice', org='org_a')}"},
            "bob": {"Authorization": f"Bearer {create_user_and_login(client, username='bob', org='org_b')}"},
            "op": {"Authorization": f"Bearer {create_user_and_login(client, username='op', org=None, admin=True)}"},
        }
        yield client, headers, tmp_path
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _deploy_pipeline(client, headers, org_name, pipeline_name, config=_PIPELINE_CONFIG):
    resp = client.put(
        f"/api/config/pipelines/{pipeline_name}?org={org_name}", json=config, headers=headers["op"]
    )
    assert resp.status_code == 200, resp.text


def test_pipeline_list_and_graph_are_org_scoped(rig):
    client, headers, pipelines_dir = rig
    _deploy_pipeline(client, headers, "org_a", "secret_wf")
    (pipelines_dir / "demo.yaml").write_text(
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
        "pipeline:\n"
        "  steps: [t]\n",
        encoding="utf-8",
    )

    alice_list = client.get("/api/pipelines", headers=headers["alice"]).json()["pipelines"]
    bob_list = client.get("/api/pipelines", headers=headers["bob"]).json()["pipelines"]
    assert "secret_wf" in alice_list
    assert "secret_wf" not in bob_list
    # Shipped YAML demos are off by default -- they're our fixtures, not any
    # customer's teams, and they're global (every org would see them).
    assert "demo" not in alice_list and "demo" not in bob_list

    assert client.get("/api/pipelines/secret_wf/graph", headers=headers["alice"]).status_code == 200
    assert client.get("/api/pipelines/secret_wf/graph", headers=headers["bob"]).status_code == 404


def _write_demo_yaml(pipelines_dir):
    (pipelines_dir / "demo.yaml").write_text(
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
        "pipeline:\n"
        "  steps: [t]\n",
        encoding="utf-8",
    )


def test_disabled_demo_pipelines_are_unreachable_not_just_hidden(rig, monkeypatch):
    # Hiding a demo from the list isn't enough: it must not be runnable by
    # name either. email_triage_demo_live reaches the configured mailbox, so a
    # listed-but-runnable demo would let any org user read that inbox.
    client, headers, pipelines_dir = rig
    _write_demo_yaml(pipelines_dir)
    monkeypatch.delenv("BESTTEAM_DEMO_PIPELINES", raising=False)

    assert "demo" not in client.get("/api/pipelines", headers=headers["alice"]).json()["pipelines"]
    assert client.get("/api/pipelines/demo/graph", headers=headers["alice"]).status_code == 404
    run = client.post("/api/runs", json={"pipeline": "demo", "input": "hi"}, headers=headers["alice"])
    assert run.status_code == 404


def test_demo_pipelines_are_available_when_enabled(rig, monkeypatch):
    client, headers, pipelines_dir = rig
    _write_demo_yaml(pipelines_dir)
    monkeypatch.setenv("BESTTEAM_DEMO_PIPELINES", "1")

    assert "demo" in client.get("/api/pipelines", headers=headers["alice"]).json()["pipelines"]
    assert client.get("/api/pipelines/demo/graph", headers=headers["alice"]).status_code == 200
    run = client.post("/api/runs", json={"pipeline": "demo", "input": "hi"}, headers=headers["alice"])
    assert run.status_code == 200


def test_running_another_orgs_pipeline_is_404(rig):
    client, headers, _ = rig
    _deploy_pipeline(client, headers, "org_a", "secret_wf")

    run_req = {"pipeline": "secret_wf", "input": "hi"}
    assert client.post("/api/runs", json=run_req, headers=headers["bob"]).status_code == 404

    run_resp = client.post("/api/runs", json=run_req, headers=headers["alice"])
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]
    # The run itself is org-guarded too.
    assert client.get(f"/api/runs/{run_id}", headers=headers["bob"]).status_code == 404
    assert client.get(f"/api/runs/{run_id}", headers=headers["alice"]).status_code == 200
    assert client.get(f"/api/runs/{run_id}", headers=headers["op"]).status_code == 200


def test_same_named_pipeline_runs_independently_per_org(rig):
    client, headers, _ = rig
    config_a = {**_PIPELINE_CONFIG, "agents": [{**_PIPELINE_CONFIG["agents"][0], "model": "fake:AAA"}]}
    config_b = {**_PIPELINE_CONFIG, "agents": [{**_PIPELINE_CONFIG["agents"][0], "model": "fake:BBB"}]}
    _deploy_pipeline(client, headers, "org_a", "wf", config_a)
    _deploy_pipeline(client, headers, "org_b", "wf", config_b)

    # Each org's user runs THEIR "wf" -- the (org_id, name) cache key keeps
    # the two builds separate even when org A's build is cached first.
    a_run = client.post("/api/runs", json={"pipeline": "wf", "input": "x"}, headers=headers["alice"])
    b_run = client.post("/api/runs", json={"pipeline": "wf", "input": "x"}, headers=headers["bob"])
    assert a_run.status_code == 200 and b_run.status_code == 200

    a_events = client.get(f"/api/runs/{a_run.json()['run_id']}", headers=headers["alice"])
    b_events = client.get(f"/api/runs/{b_run.json()['run_id']}", headers=headers["bob"])
    assert a_events.status_code == 200 and b_events.status_code == 200


# --- GET /api/runs admin cross-org passthrough (get_current_org_or_admin) ---


def test_list_runs_admin_sees_across_orgs_by_default(rig):
    """An org member is always forced to their own org (get_current_org,
    unchanged); a platform admin sees every org's runs when ?org= is
    omitted, and can narrow to one with ?org=<name>."""
    client, headers, _ = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        db.add(Run(id="run-a", pipeline="wf", input="x", status="completed", org_id=org_a, username="alice"))
        db.add(Run(id="run-b", pipeline="wf", input="x", status="completed", org_id=org_b, username="bob"))
        db.commit()

    alice_ids = {r["id"] for r in client.get("/api/runs", headers=headers["alice"]).json()["runs"]}
    assert alice_ids == {"run-a"}

    op_all = client.get("/api/runs", headers=headers["op"]).json()["runs"]
    assert {r["id"] for r in op_all} == {"run-a", "run-b"}
    assert {r["org"] for r in op_all} == {"org_a", "org_b"}

    op_scoped = client.get("/api/runs", params={"org": "org_a"}, headers=headers["op"]).json()["runs"]
    assert {r["id"] for r in op_scoped} == {"run-a"}
    assert op_scoped[0]["org"] == "org_a"


def test_list_runs_org_member_org_param_has_no_effect(rig):
    """A regular org member can't use ?org= to peek at another org -- the
    param is silently ignored for them (never a leak, never an error), same
    as today's behavior with no ?org= at all."""
    client, headers, _ = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        db.add(Run(id="run-a", pipeline="wf", input="x", status="completed", org_id=org_a, username="alice"))
        db.add(Run(id="run-b", pipeline="wf", input="x", status="completed", org_id=org_b, username="bob"))
        db.commit()

    resp = client.get("/api/runs", params={"org": "org_b"}, headers=headers["alice"])
    assert resp.status_code == 200
    assert {r["id"] for r in resp.json()["runs"]} == {"run-a"}


def test_list_runs_admin_unknown_org_is_404(rig):
    client, headers, _ = rig
    resp = client.get("/api/runs", params={"org": "does-not-exist"}, headers=headers["op"])
    assert resp.status_code == 404


def test_list_runs_by_run_id_admin_cross_org_passthrough(rig):
    """The explicit run_id probe mirrors GET /api/runs/{id}'s own admin
    passthrough: an admin without ?org= can look up any run; scoped to a
    different org via ?org=, it's a 404 same as an org member's."""
    client, headers, _ = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        db.add(Run(id="run-a", pipeline="wf", input="x", status="completed", org_id=org_a, username="alice"))
        db.commit()

    assert client.get("/api/runs", params={"run_id": "run-a"}, headers=headers["op"]).status_code == 200
    resp = client.get("/api/runs", params={"run_id": "run-a", "org": "org_b"}, headers=headers["op"])
    assert resp.status_code == 404


def test_pipeline_cannot_reference_another_orgs_knowledge_base(rig, tmp_path):
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
        **_PIPELINE_CONFIG,
        "agents": [{**_PIPELINE_CONFIG["agents"][0], "tools": ["private_kb"]}],
    }
    # org B's pipeline referencing org A's KB name must not resolve it.
    resp = client.put(
        "/api/config/pipelines/leaky_wf?org=org_b", json=leaky_config, headers=headers["op"]
    )
    assert resp.status_code == 400
    assert "private_kb" in resp.json()["detail"]
    # The same reference from org A itself is fine.
    resp = client.put(
        "/api/config/pipelines/ok_wf?org=org_a", json=leaky_config, headers=headers["op"]
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
    assert client.get("/api/pipelines", headers=headers["op"]).status_code == 403
    assert client.post("/api/runs", json={"pipeline": "x", "input": "y"}, headers=headers["op"]).status_code == 403
    assert client.get("/api/builder/sessions", headers=headers["op"]).status_code == 403
    assert (
        client.post("/api/builder/sessions", json={"intent_text": "x"}, headers=headers["op"]).status_code
        == 403
    )


def test_org_users_cannot_reach_admin_surfaces(rig):
    client, headers, _ = rig
    assert client.get("/api/config/knowledge_bases", headers=headers["alice"]).status_code == 403
    assert client.get("/api/memory/users", headers=headers["alice"]).status_code == 403
