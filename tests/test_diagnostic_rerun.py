"""`POST /api/runs/{id}/diagnose` -- an admin's diagnostic re-run of a poor run.

The new run re-executes the original input against the team as currently
deployed with `Pipeline.stream(diagnostic=True)`, so its trace carries the
prompts / model turns / tool args+results a normal trace leaves out. It is
admin-only, refuses autonomous/shared-chat runs and purged runs, never engages
per-user memory, and is hidden from the customer's own run list. See
docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md.
"""

import time

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, get_org_id, make_concurrent_safe_engine, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import PipelineRecord, Run, ShareMessage, User
from ui.backend.db.share_links import create_share_link
from ui.backend.db.share_messages import append_message, list_messages
from ui.backend.db.share_sessions import create_share_session
from ui.backend.db_session import get_db

_PIPELINE_CONFIG = {
    "agents": [{"name": "a", "role": "r", "goal": "g", "backstory": "b", "model": "fake:done"}],
    "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
    "pipeline": {"steps": ["team"]},
}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """client + bearer headers for alice (org_a member) and op (platform admin)."""
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()
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
            "op": {"Authorization": f"Bearer {create_user_and_login(client, username='op', org=None, admin=True)}"},
        }
        yield client, headers
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _deploy(client, headers, config=_PIPELINE_CONFIG, name="wf"):
    resp = client.put(f"/api/config/pipelines/{name}?org=org_a", json=config, headers=headers["op"])
    assert resp.status_code == 200, resp.text


def _wait_finished(client, headers, run_id, deadline_seconds=10):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}", headers=headers["op"])
        assert resp.status_code == 200, resp.text
        if resp.json()["status"] != "running":
            return resp.json()
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} still running after {deadline_seconds}s")


def _run_as_alice(client, headers, text="hello team"):
    resp = client.post("/api/runs", json={"pipeline": "wf", "input": text}, headers=headers["alice"])
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    _wait_finished(client, headers, run_id)
    return run_id


def test_admin_diagnoses_a_run_and_the_new_trace_carries_the_prompt_and_turns(rig):
    client, headers = rig
    _deploy(client, headers)
    original = _run_as_alice(client, headers, "why is the sky blue")

    resp = client.post(f"/api/runs/{original}/diagnose", headers=headers["op"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    new_id = body["run_id"]
    assert new_id != original
    assert body["diagnostic_of_run_id"] == original
    assert body["version_changed"] is False
    _wait_finished(client, headers, new_id)

    trace = client.get(f"/api/runs/{new_id}/trace", headers=headers["op"]).json()
    types = [e["type"] for e in trace["events"]]
    assert "agent_prompt" in types and "model_turn" in types
    prompt = next(e for e in trace["events"] if e["type"] == "agent_prompt")
    assert prompt["agent"] == "a"
    assert prompt["data"]["input"] == "why is the sky blue"
    assert "Background: b" in prompt["data"]["system_prompt"]
    turn = next(e for e in trace["events"] if e["type"] == "model_turn")
    assert turn["data"] == {"turn": 1, "content": "done", "tool_calls": []}

    # The original run's own trace is untouched: still no diagnostic events.
    original_types = [e["type"] for e in client.get(f"/api/runs/{original}/trace", headers=headers["op"]).json()["events"]]
    assert "agent_prompt" not in original_types

    with open_test_db() as db:
        new_row = db.get(Run, new_id)
        assert new_row.diagnostic_of_run_id == original
        assert new_row.org_id == get_org_id("org_a")
        assert new_row.username == "op"
        assert new_row.input == "why is the sky blue"
        assert new_row.status == "completed"
        assert new_row.pipeline_version_id == db.get(Run, original).pipeline_version_id


def test_diagnostic_run_is_hidden_from_the_customer_list_but_listed_for_the_admin(rig):
    client, headers = rig
    _deploy(client, headers)
    original = _run_as_alice(client, headers)
    new_id = client.post(f"/api/runs/{original}/diagnose", headers=headers["op"]).json()["run_id"]
    _wait_finished(client, headers, new_id)

    alice_ids = [r["id"] for r in client.get("/api/runs", headers=headers["alice"]).json()["runs"]]
    assert original in alice_ids
    assert new_id not in alice_ids

    admin_runs = {r["id"]: r for r in client.get("/api/runs", headers=headers["op"]).json()["runs"]}
    assert admin_runs[new_id]["diagnostic_of_run_id"] == original
    assert admin_runs[new_id]["version_changed"] is False
    assert admin_runs[original]["diagnostic_of_run_id"] is None
    assert admin_runs[original]["version_changed"] is None


def test_org_member_cannot_diagnose(rig):
    client, headers = rig
    _deploy(client, headers)
    original = _run_as_alice(client, headers)

    assert client.post(f"/api/runs/{original}/diagnose", headers=headers["alice"]).status_code == 403


def test_unknown_run_is_404(rig):
    client, headers = rig
    assert client.post("/api/runs/nope/diagnose", headers=headers["op"]).status_code == 404


def test_autonomous_email_runs_are_refused(rig):
    client, headers = rig
    org_id = get_org_id("org_a")
    with open_test_db() as db:
        db.add(Run(id="auto-1", pipeline="wf", input="in", status="failed", org_id=org_id,
                   username="email-trigger", trigger_context={"uids": [1]}))
        db.commit()

    resp = client.post("/api/runs/auto-1/diagnose", headers=headers["op"])

    assert resp.status_code == 400
    assert "mailbox" in resp.json()["detail"].lower()


def test_a_shared_chat_turn_can_be_diagnosed_without_touching_the_visitor_session(rig):
    """A share-link turn is a regular run stamped with
    trigger_context["share_session_id"]. Its diagnostic re-run is a NEW row
    with no trigger_context, so share_transcript.record_share_reply is a
    no-op and nothing reaches the visitor's transcript -- the blanket
    refusal was protecting nothing (spec 2026-08-22)."""
    client, headers = rig
    _deploy(client, headers)
    org_id = get_org_id("org_a")
    # A real link, session and transcript -- so "untouched" is checked against
    # an existing visitor conversation, not an empty table (Codex review).
    with open_test_db() as db:
        alice = db.query(User).filter_by(username="alice").one()
        team = PipelineRecord(name="wf-share", org_id=org_id, config=_PIPELINE_CONFIG, status="deployed")
        db.add(team)
        db.commit()
        db.refresh(team)
        link = create_share_link(db, pipeline_id=team.id, org_id=org_id, created_by=alice.id)
        session = create_share_session(db, link.id)
        append_message(db, session.id, turn_number=1, role="user", content="hi")
        append_message(db, session.id, turn_number=2, role="assistant", content="hello!", run_id=None)
        db.add(Run(id="share-1", pipeline="wf", input="<user>hi</user>", status="completed", org_id=org_id,
                   username="share-link",
                   trigger_context={"share_link_id": link.id, "share_session_id": session.id, "turn_number": 1}))
        db.commit()
        session_id = session.id
        before = [(m.turn_number, m.role, m.content) for m in list_messages(db, session_id)]

    resp = client.post("/api/runs/share-1/diagnose", headers=headers["op"])

    assert resp.status_code == 200, resp.text
    new_id = resp.json()["run_id"]
    assert _wait_finished(client, headers, new_id)["status"] == "completed"
    with open_test_db() as db:
        new_row = db.get(Run, new_id)
        assert new_row.diagnostic_of_run_id == "share-1"
        assert new_row.trigger_context is None
        after = [(m.turn_number, m.role, m.content) for m in list_messages(db, session_id)]
        assert after == before
        assert db.query(ShareMessage).filter(ShareMessage.run_id == new_id).count() == 0


def test_a_malformed_share_context_is_refused_like_any_other_trigger(rig):
    """The guard must classify a share turn the way the share code does -- a
    real session id -- not by key presence, or a context the share paths
    would ignore is admitted on a false premise (Codex review)."""
    client, headers = rig
    org_id = get_org_id("org_a")
    with open_test_db() as db:
        db.add(Run(id="bad-1", pipeline="wf", input="in", status="completed", org_id=org_id,
                   username="share-link", trigger_context={"share_session_id": None}))
        db.add(Run(id="bad-2", pipeline="wf", input="in", status="completed", org_id=org_id,
                   username="share-link", trigger_context={"share_session_id": "1"}))
        db.commit()

    assert client.post("/api/runs/bad-1/diagnose", headers=headers["op"]).status_code == 400
    assert client.post("/api/runs/bad-2/diagnose", headers=headers["op"]).status_code == 400


def test_a_diagnostic_run_cannot_itself_be_diagnosed(rig):
    client, headers = rig
    _deploy(client, headers)
    original = _run_as_alice(client, headers)
    new_id = client.post(f"/api/runs/{original}/diagnose", headers=headers["op"]).json()["run_id"]
    _wait_finished(client, headers, new_id)

    resp = client.post(f"/api/runs/{new_id}/diagnose", headers=headers["op"])

    assert resp.status_code == 400
    assert original in resp.json()["detail"]


def test_a_purged_run_is_refused(rig):
    client, headers = rig
    _deploy(client, headers)
    original = _run_as_alice(client, headers)
    assert client.post(f"/api/runs/{original}/purge", headers=headers["alice"]).status_code == 200

    assert client.post(f"/api/runs/{original}/diagnose", headers=headers["op"]).status_code == 409


def test_version_changed_is_reported_after_a_redeploy(rig):
    client, headers = rig
    _deploy(client, headers)
    original = _run_as_alice(client, headers)
    redeployed = {**_PIPELINE_CONFIG, "agents": [{**_PIPELINE_CONFIG["agents"][0], "model": "fake:v2"}]}
    _deploy(client, headers, redeployed)

    resp = client.post(f"/api/runs/{original}/diagnose", headers=headers["op"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["version_changed"] is True
    new_id = resp.json()["run_id"]
    assert _wait_finished(client, headers, new_id)["status"] == "completed"
    # Derived again on the list, so the Trace page keeps the warning on revisit.
    listed = client.get("/api/runs", headers=headers["op"], params={"run_id": new_id}).json()["runs"][0]
    assert listed["version_changed"] is True
    trace = client.get(f"/api/runs/{new_id}/trace", headers=headers["op"]).json()
    turn = next(e for e in trace["events"] if e["type"] == "model_turn")
    assert turn["data"]["content"] == "v2"
