"""Pausing a live team (`PATCH /api/pipelines/{id}`).

A deployed team could be neither deleted nor switched off from My Teams: the
only per-team lifecycle verb an org member had was deleting a never-deployed
draft, and the only "off" switch in the product was org-wide
(`organizations.active`). Deleting a live team is still deferred (it cascades
into runs, share sessions, KB links and trigger rows); pausing is the
reversible half, and it keeps every one of those.
"""

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, make_concurrent_safe_engine, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import PipelineRecord
from ui.backend.db_session import get_db

_CONFIG = {
    "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:done"}],
    "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
    "pipeline": {"steps": ["team"]},
}


@pytest.fixture
def rig(tmp_path, monkeypatch):
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
            "bob": {"Authorization": f"Bearer {create_user_and_login(client, username='bob', org='org_b')}"},
            "op": {"Authorization": f"Bearer {create_user_and_login(client, username='op', org=None, admin=True)}"},
        }
        yield client, headers
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _deploy(client, headers, org_name, name):
    resp = client.put(
        f"/api/config/pipelines/{name}?org={org_name}", json=_CONFIG, headers=headers["op"]
    )
    assert resp.status_code == 200, resp.text
    with open_test_db() as db:
        return db.query(PipelineRecord).filter_by(name=name).one().id


def test_pausing_takes_the_team_off_the_run_list(rig):
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")
    assert "helper" in client.get("/api/pipelines", headers=headers["alice"]).json()["pipelines"]

    paused = client.patch(
        f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"]
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["active"] is False

    assert "helper" not in client.get("/api/pipelines", headers=headers["alice"]).json()["pipelines"]


def test_a_paused_team_refuses_to_run(rig):
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")
    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"])

    resp = client.post(
        "/api/runs", json={"pipeline": "helper", "input": "hi"}, headers=headers["alice"]
    )
    # 409, not the 404 an unknown team gets: the customer paused this
    # themselves and the message has to say so, or Run a Team looks broken.
    assert resp.status_code == 409
    assert "paused" in resp.json()["detail"].lower()


def test_resuming_puts_the_team_back(rig):
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")
    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"])

    resumed = client.patch(
        f"/api/pipelines/{pipeline_id}", json={"active": True}, headers=headers["alice"]
    )
    assert resumed.status_code == 200
    assert resumed.json()["active"] is True
    assert "helper" in client.get("/api/pipelines", headers=headers["alice"]).json()["pipelines"]
    assert (
        client.post(
            "/api/runs", json={"pipeline": "helper", "input": "hi"}, headers=headers["alice"]
        ).status_code
        == 200
    )


def test_a_paused_team_still_shows_on_my_teams(rig):
    # It has to: My Teams is where the customer goes to switch it back on.
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")
    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"])

    sessions = client.get("/api/builder/sessions", headers=headers["alice"]).json()["sessions"]
    listed = [s for s in sessions if s["pipeline_id"] == pipeline_id]
    assert len(listed) == 1
    assert listed[0]["active"] is False


def test_pausing_another_orgs_team_is_a_404(rig):
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")

    resp = client.patch(
        f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["bob"]
    )
    assert resp.status_code == 404


# The org has exactly one trigger (unique `org_id`), and it names one team. A
# trigger left pointing at a paused team would keep polling the mailbox,
# claiming the messages and failing every dispatch.
def _enable_trigger(org_name, pipeline_name):
    from ui.backend.db.email_triggers import upsert_email_trigger

    with open_test_db() as db:
        from helpers import get_org_id

        upsert_email_trigger(
            db,
            get_org_id(org_name),
            pipeline_name=pipeline_name,
            enabled=True,
            last_uid=0,
            uidvalidity=None,
        )
        db.commit()


def _trigger_enabled(org_name):
    from ui.backend.db.email_triggers import get_email_trigger

    with open_test_db() as db:
        from helpers import get_org_id

        return get_email_trigger(db, get_org_id(org_name)).enabled


def test_pausing_the_automatic_team_switches_automatic_runs_off(rig):
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")
    _enable_trigger("org_a", "helper")

    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"])

    assert _trigger_enabled("org_a") is False


def test_pausing_one_team_leaves_another_teams_automatic_runs_alone(rig):
    client, headers = rig
    paused_id = _deploy(client, headers, "org_a", "helper")
    _deploy(client, headers, "org_a", "mailer")
    _enable_trigger("org_a", "mailer")

    client.patch(f"/api/pipelines/{paused_id}", json={"active": False}, headers=headers["alice"])

    assert _trigger_enabled("org_a") is True


def test_resuming_does_not_switch_automatic_runs_back_on(rig):
    # Deliberate: automatic runs are resumed by the customer, not by a side
    # effect -- the same rule `on_mailbox_saved` follows for a replaced mailbox.
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")
    _enable_trigger("org_a", "helper")
    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"])

    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": True}, headers=headers["alice"])

    assert _trigger_enabled("org_a") is False


def test_a_paused_team_cannot_be_put_back_on_automatic_runs(rig):
    # Otherwise the toggle is a way around the pause: it would poll, claim the
    # mail and fail every dispatch, with nothing on screen saying why.
    client, headers = rig
    pipeline_id = _deploy(client, headers, "org_a", "helper")
    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"])

    resp = client.put(
        "/api/org/email-trigger",
        json={"enabled": True, "pipeline_name": "helper"},
        headers=headers["alice"],
    )
    assert resp.status_code == 400
    assert "paused" in resp.json()["detail"].lower()


def test_a_paused_team_cannot_be_built_for_an_automatic_run(rig):
    """The last entry point: `build_trigger_pipeline` is what both the poller
    and a retry of a failed automatic run go through.

    Pausing already disables the trigger, so the poller should never get here
    -- but a pause can land between the trigger check and the build, and a
    retry has no trigger check of its own at all.
    """
    from ui.backend.email_trigger import build_trigger_pipeline
    from helpers import get_org_id

    client, headers = rig
    mail_config = {
        "agents": [
            {"name": "a", "role": "r", "goal": "g", "model": "fake:done", "tools": ["email_find"]}
        ],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "pipeline": {"steps": ["team"]},
    }
    resp = client.put(
        "/api/config/pipelines/mailer?org=org_a", json=mail_config, headers=headers["op"]
    )
    assert resp.status_code == 200, resp.text
    with open_test_db() as db:
        pipeline_id = db.query(PipelineRecord).filter_by(name="mailer").one().id
    client.patch(f"/api/pipelines/{pipeline_id}", json={"active": False}, headers=headers["alice"])

    with open_test_db() as db:
        with pytest.raises(ValueError, match="paused"):
            build_trigger_pipeline("mailer", db, get_org_id("org_a"), [1], backend=None)
