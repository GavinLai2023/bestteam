"""Tests for org-scoped self-service settings (`/api/org/email`) + the wizard
mailbox trigger (`spec_uses_email`, deploy gate, `uses_email` flag)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from bestteam.exceptions import ConfigurationError
from helpers import create_user_and_login, open_test_db
from ui.backend import main as backend_main
from ui.backend import org_settings
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.builder_sessions import create_session, update_session
from ui.backend.db.email_credentials import get_email_credentials, set_email_credentials
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db
from ui.backend.email_tools import spec_uses_email
from ui.backend.skills import seed_default_skills

_EMAIL_SPEC = {
    "name": "email_team",
    "agents": [{"name": "t", "role": "Triager", "goal": "triage",
                "model": "fake:done", "skills": ["email_triage_reply"]}],
    "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}
_PLAIN_SPEC = {
    "name": "plain_team",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


class _FakeConn:
    def logout(self):
        pass


class _FakeBackend:
    ok = True

    def __init__(self, **kw):
        self.kw = kw

    def _connect(self):
        if not _FakeBackend.ok:
            raise ConfigurationError("IMAP login failed")
        return _FakeConn()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()
    _FakeBackend.ok = True

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


def _bypass_ssrf(monkeypatch):
    # For non-SSRF paths, skip real DNS resolution.
    monkeypatch.setattr(org_settings, "check_host_allowed", lambda host: "1.2.3.4")


# --- /api/org/email endpoints ------------------------------------------------

def test_get_email_when_not_connected(client):
    assert client.get("/api/org/email").json() == {"connected": False}


def test_set_then_get_never_returns_password(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    resp = client.put("/api/org/email", json={
        "host": "imap.acme.com", "username": "support@acme.com", "password": "app-pw",
    })
    assert resp.status_code == 200
    got = client.get("/api/org/email").json()
    assert got["connected"] is True
    assert got["host"] == "imap.acme.com" and got["username"] == "support@acme.com"
    assert "password" not in got and "app-pw" not in str(got)
    # Stored encrypted, not plaintext.
    with open_test_db() as db:
        cred = get_email_credentials(db, get_or_create_org(db, "default").id)
        assert cred.password_encrypted != "app-pw"


def test_set_email_rejects_private_host(client):
    resp = client.put("/api/org/email", json={
        "host": "127.0.0.1", "username": "u", "password": "p",
    })
    assert resp.status_code == 400
    assert "private/internal" in resp.json()["detail"]


def test_test_connection_success_does_not_save(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    monkeypatch.setattr(org_settings, "_ImapBackend", _FakeBackend)
    resp = client.post("/api/org/email/test", json={
        "host": "imap.acme.com", "username": "u", "password": "p",
    })
    assert resp.json() == {"ok": True}
    assert client.get("/api/org/email").json() == {"connected": False}  # not saved


def test_test_connection_reports_failure(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    monkeypatch.setattr(org_settings, "_ImapBackend", _FakeBackend)
    _FakeBackend.ok = False
    body = client.post("/api/org/email/test", json={
        "host": "imap.acme.com", "username": "u", "password": "wrong",
    }).json()
    assert body["ok"] is False and "login failed" in body["error"].lower()


def test_test_connection_rejects_private_host(client):
    resp = client.post("/api/org/email/test", json={
        "host": "10.0.0.5", "username": "u", "password": "p",
    })
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_port", [0, -1, 70000])
def test_set_email_rejects_out_of_range_port(client, bad_port):
    # An out-of-range port must not be stored as a "connected" config -- 422 at
    # validation, before it can be persisted.
    resp = client.put("/api/org/email", json={
        "host": "imap.acme.com", "username": "u", "password": "p", "port": bad_port,
    })
    assert resp.status_code == 422
    assert client.get("/api/org/email").json() == {"connected": False}


def test_test_email_rejects_out_of_range_port(client):
    resp = client.post("/api/org/email/test", json={
        "host": "imap.acme.com", "username": "u", "password": "p", "port": 70000,
    })
    assert resp.status_code == 422


def test_delete_email(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "h", "username": "u", "password": "p"})
    assert client.delete("/api/org/email").status_code == 204
    assert client.get("/api/org/email").json() == {"connected": False}


def test_platform_operator_gets_403(client):
    op = create_user_and_login(client, username="op", org=None, admin=True)
    resp = client.get("/api/org/email", headers={"Authorization": f"Bearer {op}"})
    assert resp.status_code == 403


def test_unauthenticated_401(client):
    resp = client.get("/api/org/email", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_cross_org_isolation(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "ha", "username": "a@x", "password": "pa"})
    other = create_user_and_login(client, username="bob", org="org_b")
    bob = {"Authorization": f"Bearer {other}"}
    assert client.get("/api/org/email", headers=bob).json() == {"connected": False}
    client.put("/api/org/email", headers=bob,
               json={"host": "hb", "username": "b@x", "password": "pb"})
    assert client.get("/api/org/email").json()["username"] == "a@x"
    assert client.get("/api/org/email", headers=bob).json()["username"] == "b@x"


# --- wizard trigger: spec_uses_email, deploy gate, uses_email flag -----------

def test_spec_uses_email_detection(client):
    with open_test_db() as db:
        seed_default_skills(db)
        org_id = get_or_create_org(db, "default").id
        assert spec_uses_email(db, _EMAIL_SPEC, org_id) is True
        assert spec_uses_email(db, _PLAIN_SPEC, org_id) is False
        # Direct tool reference is detected too (no skill).
        direct = {"agents": [{"name": "a", "tools": ["email_find"]}]}
        assert spec_uses_email(db, direct, org_id) is True


def _make_session(spec):
    with open_test_db() as db:
        seed_default_skills(db)
        org_id = get_or_create_org(db, "default").id
        s = create_session(db, intent_text="x", org_id=org_id)
        update_session(db, s.id, specification_json=spec, status="solution")
        return s.id


def test_deploy_gate_blocks_email_team_without_mailbox(client):
    sid = _make_session(_EMAIL_SPEC)
    resp = client.post(f"/api/builder/sessions/{sid}/deploy")
    assert resp.status_code == 400
    assert "mailbox" in resp.json()["detail"].lower()


def test_deploy_succeeds_once_mailbox_connected(client):
    sid = _make_session(_EMAIL_SPEC)
    with open_test_db() as db:
        set_email_credentials(db, get_or_create_org(db, "default").id,
                              host="h", username="u", password="p")
    assert client.post(f"/api/builder/sessions/{sid}/deploy").status_code == 200


def test_deploy_plain_team_needs_no_mailbox(client):
    sid = _make_session(_PLAIN_SPEC)
    assert client.post(f"/api/builder/sessions/{sid}/deploy").status_code == 200


def test_session_response_carries_uses_email(client):
    sid = _make_session(_EMAIL_SPEC)
    assert client.get(f"/api/builder/sessions/{sid}").json()["uses_email"] is True
    sid2 = _make_session(_PLAIN_SPEC)
    assert client.get(f"/api/builder/sessions/{sid2}").json()["uses_email"] is False


def test_mutation_response_carries_uses_email(client):
    # The flag must be computed on mutation responses too, not just GET -- else
    # a refine/save drops the connector while deploy still requires a mailbox.
    with open_test_db() as db:
        seed_default_skills(db)
    sid = client.post("/api/builder/sessions", json={"intent_text": "triage my inbox"}).json()["id"]
    resp = client.post(f"/api/builder/sessions/{sid}/specification", json={"specification": _EMAIL_SPEC})
    assert resp.status_code == 200
    assert resp.json()["uses_email"] is True
