"""API tests for /api/org/email-trigger (opt-in + status + activity)."""

import pytest


pytestmark = pytest.mark.integration
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
from ui.backend.db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    MICROSOFT_IMAP_HOST,
    set_email_credentials,
)
from ui.backend.db.email_triggers import get_email_trigger
from ui.backend.db.inbox_events import mailbox_identity, record_events
from ui.backend.db.models import InboxEvent, Run, PipelineRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db
from ui.backend.skills import seed_default_skills

_EMAIL_TEAM_CONFIG = {
    "name": "triage",
    "agents": [{"name": "t", "role": "Triager", "goal": "triage",
                "model": "fake:done", "skills": ["email_triage_reply"]}],
    "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
    "pipeline": {"steps": ["tm"]},
}
_PLAIN_TEAM_CONFIG = {
    "name": "plain",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "pipeline": {"steps": ["tm"]},
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("BESTTEAM_TRIGGERS_DISABLED", raising=False)
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)

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
        db.add(PipelineRecord(name=config["name"], org_id=org.id,
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
                      json={"pipeline_name": "triage", "enabled": True}).json()
    assert body["enabled"] is True and body["status"] == "active"
    with open_test_db() as db:
        t = get_email_trigger(db, org_id)
        assert (t.last_uid, t.uidvalidity) == (99, 7)  # backlog never triggers


def test_enable_uses_the_shared_factory_so_an_oauth_mailbox_can_be_turned_on(
    client, monkeypatch
):
    """The enable path built its own `_ImapBackend` with `password=`, ignoring
    `auth_type` -- the same defect the poller had. For an M365 org that column
    holds the Entra client secret, so the baseline login failed and automatic
    runs could never be turned on at all."""
    from ui.backend import email_tools

    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    with open_test_db() as db:
        set_email_credentials(
            db, org_id, host=MICROSOFT_IMAP_HOST, username="u@acme.com",
            password="client-secret", auth_type=AUTH_MICROSOFT_OAUTH,
            oauth_tenant_id="tenant-1", oauth_client_id="client-1",
        )
    built = []
    monkeypatch.setattr(email_tools, "_ImapBackend",
                        lambda **kwargs: built.append(kwargs) or "backend")
    _stub_mailbox(monkeypatch, uidvalidity=7, max_uid=99)

    resp = client.put("/api/org/email-trigger",
                      json={"pipeline_name": "triage", "enabled": True})

    assert resp.status_code == 200 and resp.json()["enabled"] is True
    assert built[0]["token_provider"] is not None
    assert built[0].get("password") is None


def test_enable_abandons_a_backlog_the_new_baseline_makes_unclaimable(
    client, monkeypatch
):
    """Enable is where the baseline is (re)established, and the only site that
    knows both the mailbox and its generation. Without this, a mailbox rebuilt
    while it was disconnected leaves rows whose generation no longer matches:
    the scoped claim query refuses them, the re-baseline branch never fires
    (enable already wrote the new UIDVALIDITY), and nothing retires them."""
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    with open_test_db() as db:
        record_events(
            db, org_id=org_id,
            mailbox_identity=mailbox_identity("imap.acme.com", "u@acme.com"),
            mailbox_generation="3", external_ids=["7"],
            decisions={"8": "bulk:list-id"},
        )
        record_events(
            db, org_id=org_id,
            mailbox_identity=mailbox_identity("imap.acme.com", "u@acme.com"),
            mailbox_generation="3", external_ids=["8"],
            decisions={"8": "bulk:list-id"},
        )
        db.commit()
    _stub_mailbox(monkeypatch, uidvalidity=7, max_uid=99)

    client.put("/api/org/email-trigger",
               json={"pipeline_name": "triage", "enabled": True})

    with open_test_db() as db:
        rows = {e.external_id: e.status for e in db.query(InboxEvent).all()}
    assert rows == {"7": "failed", "8": "failed"}
    # Customer-caused (they reconnected it themselves), and enable deliberately
    # clears the error field -- so this is logged, not reported on the trigger.
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).last_error is None


def test_enable_leaves_the_current_generations_backlog_claimable(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    with open_test_db() as db:
        record_events(
            db, org_id=org_id,
            mailbox_identity=mailbox_identity("imap.acme.com", "u@acme.com"),
            mailbox_generation="7", external_ids=["7"],
        )
        db.commit()
    _stub_mailbox(monkeypatch, uidvalidity=7, max_uid=99)

    client.put("/api/org/email-trigger",
               json={"pipeline_name": "triage", "enabled": True})

    with open_test_db() as db:
        assert db.query(InboxEvent).one().status == "pending"


def test_enable_rejects_undeployed_team(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG, status="draft")
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"pipeline_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "launch" in resp.json()["detail"].lower()


def test_enable_rejects_non_email_team(client, monkeypatch):
    org_id = _seed_team(_PLAIN_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"pipeline_name": "plain", "enabled": True})
    assert resp.status_code == 400
    assert "email" in resp.json()["detail"].lower()


def test_enable_rejects_when_no_mailbox(client, monkeypatch):
    _seed_team(_EMAIL_TEAM_CONFIG)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"pipeline_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "mailbox" in resp.json()["detail"].lower()


def test_enable_unreachable_mailbox_is_friendly_400(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)

    def _fail(backend):
        raise OSError("[WinError 10060] timed out")

    monkeypatch.setattr(email_trigger_api, "mailbox_state", _fail)
    resp = client.put("/api/org/email-trigger",
                      json={"pipeline_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "WinError" not in resp.json()["detail"]


def test_disable_turns_off(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    client.put("/api/org/email-trigger", json={"pipeline_name": "triage", "enabled": True})
    body = client.put("/api/org/email-trigger",
                      json={"pipeline_name": "triage", "enabled": False}).json()
    assert body["enabled"] is False and body["status"] == "off"


def test_enable_clears_stale_error(client, monkeypatch):
    # A successful (re-)enable just proved the mailbox reachable -- an old
    # poll failure must not keep the status stuck on "error" until the next
    # poll cycle.
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    client.put("/api/org/email-trigger", json={"pipeline_name": "triage", "enabled": True})
    with open_test_db() as db:
        t = get_email_trigger(db, org_id)
        t.last_error = "Couldn't check the mailbox. We'll keep retrying automatically."
        db.commit()
    body = client.put("/api/org/email-trigger",
                      json={"pipeline_name": "triage", "enabled": True}).json()
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
                      json={"pipeline_name": "triage", "enabled": True})
    assert resp.status_code == 400  # 'triage' is invisible from org_b


# --- activity list ------------------------------------------------------------


def _add_run(org_id, run_id, username, minutes_ago, status="completed"):
    with open_test_db() as db:
        db.add(Run(id=run_id, pipeline="triage", input="x", status=status,
                   org_id=org_id, username=username,
                   created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
        db.commit()


def test_activity_lists_org_runs_newest_first_with_autonomous_flag(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
    _add_run(org_id, "r-old", "email-trigger", minutes_ago=10)
    _add_run(org_id, "r-new", "email-trigger", minutes_ago=1)
    _add_run(org_id, "r-manual", "demo", minutes_ago=0)  # not autonomous -- excluded
    body = client.get("/api/org/email-trigger/activity").json()
    assert [r["id"] for r in body["runs"]] == ["r-new", "r-old"]
    assert body["runs"][0]["autonomous"] is True
    assert body["runs"][1]["autonomous"] is True
    # SQLite drops tzinfo -- the explicit UTC offset stops the browser from
    # parsing this timestamp as local time.
    assert body["runs"][0]["started_at"].endswith("+00:00")


def test_activity_filters_autonomous_server_side(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
    # 60 manual runs (newer) then 1 autonomous (older) -- the autonomous one must
    # still appear (server-side filter), not be pushed out of a 50-row window.
    with open_test_db() as db:
        db.add(Run(id="auto", pipeline="w", input="x", status="completed", org_id=org_id,
                   username="email-trigger",
                   created_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        for i in range(60):
            db.add(Run(id=f"m{i}", pipeline="w", input="x", status="completed", org_id=org_id,
                       username="alice",
                       created_at=datetime.now(timezone.utc) - timedelta(minutes=i)))
        db.commit()
    runs = client.get("/api/org/email-trigger/activity").json()["runs"]
    assert any(r["id"] == "auto" for r in runs)
    assert all(r["autonomous"] for r in runs)


def test_activity_is_org_scoped(client):
    with open_test_db() as db:
        mine = get_or_create_org(db, "default").id
        theirs = get_or_create_org(db, "org_b").id
    _add_run(mine, "r-mine", "email-trigger", minutes_ago=1)
    _add_run(theirs, "r-theirs", "email-trigger", minutes_ago=1)
    ids = [r["id"] for r in client.get("/api/org/email-trigger/activity").json()["runs"]]
    assert ids == ["r-mine"]


# --- filtered messages --------------------------------------------------------


def _seed_filtered(org_id, external_id="101", decision="bulk:list-id"):
    """One message the pre-LLM filter skipped, recorded the way the poller
    records it."""
    with open_test_db() as db:
        record_events(
            db, org_id=org_id,
            mailbox_identity=mailbox_identity("imap.acme.com", "u@acme.com"),
            mailbox_generation="3", external_ids=[external_id],
            decisions={external_id: decision},
        )
        db.commit()
        return (
            db.query(InboxEvent)
            .filter_by(org_id=org_id, external_id=external_id)
            .one()
            .id
        )


@pytest.fixture
def filtered_event_id(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
    return _seed_filtered(org_id)


@pytest.fixture
def other_org_event_id(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "org_b").id
    return _seed_filtered(org_id, external_id="202")


def test_filtered_messages_are_listed_with_a_readable_reason(client, filtered_event_id):
    body = client.get("/api/org/email-trigger/filtered").json()
    assert body["filtered"][0]["reason"].startswith("Skipped:")
    assert body["filtered"][0]["decision"] == "bulk:list-id"
    assert body["filtered"][0]["external_id"] == "101"


def test_filtered_list_is_org_scoped(client, filtered_event_id, other_org_event_id):
    ids = [row["id"] for row in client.get("/api/org/email-trigger/filtered").json()["filtered"]]
    assert ids == [filtered_event_id]


def test_releasing_a_filtered_message_makes_it_pending(client, filtered_event_id):
    assert client.post(
        f"/api/org/email-trigger/filtered/{filtered_event_id}/release"
    ).status_code == 200
    with open_test_db() as db:
        assert db.get(InboxEvent, filtered_event_id).status == "pending"


def test_releasing_the_same_message_twice_is_404(client, filtered_event_id):
    # Indistinguishable from another org's row on purpose -- see below.
    client.post(f"/api/org/email-trigger/filtered/{filtered_event_id}/release")
    assert client.post(
        f"/api/org/email-trigger/filtered/{filtered_event_id}/release"
    ).status_code == 404


def test_releasing_an_unknown_id_is_404(client):
    assert client.post("/api/org/email-trigger/filtered/999999/release").status_code == 404


def test_releasing_another_orgs_message_is_404_not_403(client, other_org_event_id):
    # 403 would confirm the row exists; 404 tells a cross-org prober nothing.
    assert client.post(
        f"/api/org/email-trigger/filtered/{other_org_event_id}/release"
    ).status_code == 404

