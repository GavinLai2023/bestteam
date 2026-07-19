"""API tests for /api/org/email-trigger (opt-in + status + activity)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import email_trigger_api
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import set_email_credentials
from ui.backend.db.email_triggers import get_email_trigger
from ui.backend.db.models import Run, WorkflowRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db
from ui.backend.skills import seed_default_skills

_EMAIL_TEAM_CONFIG = {
    "name": "triage",
    "agents": [{"name": "t", "role": "Triager", "goal": "triage",
                "model": "fake:done", "skills": ["email_triage_reply"]}],
    "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}
_PLAIN_TEAM_CONFIG = {
    "name": "plain",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("BESTTEAM_TRIGGERS_DISABLED", raising=False)
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)

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
        token = create_user_and_login(c)  # plain member of 'default'
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _seed_team(config, status="deployed", org_name="default"):
    with open_test_db() as db:
        seed_default_skills(db)
        org = get_or_create_org(db, org_name)
        db.add(WorkflowRecord(name=config["name"], org_id=org.id,
                              config=config, status=status))
        db.commit()
        return org.id


def _connect_mailbox(org_id):
    with open_test_db() as db:
        set_email_credentials(db, org_id, host="imap.acme.com",
                              username="u@acme.com", password="pw")


def _stub_mailbox(monkeypatch, uidvalidity=3, max_uid=45):
    monkeypatch.setattr(email_trigger_api, "mailbox_state",
                        lambda backend: (uidvalidity, max_uid))


def test_get_status_off_by_default(client):
    body = client.get("/api/org/email-trigger").json()
    assert body["enabled"] is False
    assert body["status"] == "off"
    assert body["daily_cap"] > 0


def test_enable_happy_path_sets_baseline(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch, uidvalidity=7, max_uid=99)
    body = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True}).json()
    assert body["enabled"] is True and body["status"] == "active"
    with open_test_db() as db:
        t = get_email_trigger(db, org_id)
        assert (t.last_uid, t.uidvalidity) == (99, 7)  # backlog never triggers


def test_enable_rejects_undeployed_team(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG, status="draft")
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "launch" in resp.json()["detail"].lower()


def test_enable_rejects_non_email_team(client, monkeypatch):
    org_id = _seed_team(_PLAIN_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "plain", "enabled": True})
    assert resp.status_code == 400
    assert "email" in resp.json()["detail"].lower()


def test_enable_rejects_when_no_mailbox(client, monkeypatch):
    _seed_team(_EMAIL_TEAM_CONFIG)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "mailbox" in resp.json()["detail"].lower()


def test_enable_unreachable_mailbox_is_friendly_400(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)

    def _fail(backend):
        raise OSError("[WinError 10060] timed out")

    monkeypatch.setattr(email_trigger_api, "mailbox_state", _fail)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "WinError" not in resp.json()["detail"]


def test_disable_turns_off(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    client.put("/api/org/email-trigger", json={"workflow_name": "triage", "enabled": True})
    body = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": False}).json()
    assert body["enabled"] is False and body["status"] == "off"


def test_enable_clears_stale_error(client, monkeypatch):
    # A successful (re-)enable just proved the mailbox reachable -- an old
    # poll failure must not keep the status stuck on "error" until the next
    # poll cycle.
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    client.put("/api/org/email-trigger", json={"workflow_name": "triage", "enabled": True})
    with open_test_db() as db:
        t = get_email_trigger(db, org_id)
        t.last_error = "Couldn't check the mailbox. We'll keep retrying automatically."
        db.commit()
    body = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True}).json()
    assert body["status"] == "active"
    assert body["last_error"] is None
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).last_error is None


def test_platform_operator_gets_403(client):
    op = create_user_and_login(client, username="op", org=None, admin=True)
    resp = client.get("/api/org/email-trigger",
                      headers={"Authorization": f"Bearer {op}"})
    assert resp.status_code == 403


def test_cross_org_cannot_enable_other_orgs_team(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG, org_name="default")
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    other = create_user_and_login(client, username="bob", org="org_b")
    resp = client.put("/api/org/email-trigger",
                      headers={"Authorization": f"Bearer {other}"},
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400  # 'triage' is invisible from org_b


# --- activity list ------------------------------------------------------------


def _add_run(org_id, run_id, username, minutes_ago, status="completed"):
    with open_test_db() as db:
        db.add(Run(id=run_id, workflow="triage", input="x", status=status,
                   org_id=org_id, username=username,
                   created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
        db.commit()


def test_activity_lists_org_runs_newest_first_with_autonomous_flag(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
    _add_run(org_id, "r-old", "email-trigger", minutes_ago=10)
    _add_run(org_id, "r-new", "demo", minutes_ago=1)
    body = client.get("/api/org/email-trigger/activity").json()
    assert [r["id"] for r in body["runs"]] == ["r-new", "r-old"]
    assert body["runs"][0]["autonomous"] is False
    assert body["runs"][1]["autonomous"] is True
    # SQLite drops tzinfo -- the explicit UTC offset stops the browser from
    # parsing this timestamp as local time.
    assert body["runs"][0]["started_at"].endswith("+00:00")


def test_activity_is_org_scoped(client):
    with open_test_db() as db:
        mine = get_or_create_org(db, "default").id
        theirs = get_or_create_org(db, "org_b").id
    _add_run(mine, "r-mine", "email-trigger", minutes_ago=1)
    _add_run(theirs, "r-theirs", "email-trigger", minutes_ago=1)
    ids = [r["id"] for r in client.get("/api/org/email-trigger/activity").json()["runs"]]
    assert ids == ["r-mine"]
