"""Tests for org-scoped self-service settings (`/api/org/email`) + the wizard
mailbox trigger (`spec_uses_email`, deploy gate, `uses_email` flag)."""

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from bestteam.exceptions import ConfigurationError
from helpers import create_user_and_login, open_test_db
from ui.backend import email_tools
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
    "pipeline": {"steps": ["tm"]},
}
_PLAIN_SPEC = {
    "name": "plain_team",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "pipeline": {"steps": ["tm"]},
}


class _FakeConn:
    def logout(self):
        pass


class _FakeBackend:
    ok = True
    drafts_ok = True

    def __init__(self, **kw):
        self.kw = kw

    def _connect(self):
        if not _FakeBackend.ok:
            raise ConfigurationError("IMAP login failed")
        return _FakeConn()

    def check_drafts_writable(self):
        if not _FakeBackend.drafts_ok:
            raise ConfigurationError(
                "The drafts folder 'Drafts' does not exist on this mailbox."
            )
        return "Drafts"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()
    _FakeBackend.ok = True
    _FakeBackend.drafts_ok = True
    # PUT /api/org/email validates the mailbox before storing it, so the
    # default for this module is a mailbox that works; individual tests flip
    # `_FakeBackend.ok`/`drafts_ok` or substitute their own double.
    # Patched on `email_tools`, which is now the single place an _ImapBackend
    # is constructed -- `org_settings` builds its pre-save backend through the
    # same primitive the stored credential will use at run time.
    monkeypatch.setattr(email_tools, "_ImapBackend", _FakeBackend)

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


def test_set_email_private_host_rejection_does_not_leak_resolved_ip(client, monkeypatch):
    # Exercises the real (non-monkeypatched) check_host_allowed(), not a
    # stand-in exception -- hostname and resolved IP are distinct values so
    # "the customer-supplied hostname is echoed back" (fine) can be told
    # apart from "the resolved internal IP is disclosed" (the leak).
    import socket

    def _resolve_to_private(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr("bestteam.tools.http_client.socket.getaddrinfo", _resolve_to_private)
    resp = client.put("/api/org/email", json={
        "host": "internal.acme.com", "username": "u", "password": "p",
    })
    assert resp.status_code == 400
    assert "10.0.0.5" not in resp.json()["detail"]


def test_set_email_resolve_failure_does_not_leak_raw_os_error(client, monkeypatch):
    # Same real-path concern for DNS resolution failure (vs. private-address
    # rejection above): the raw OS resolver exception must not reach the
    # customer-facing response.
    import socket

    def _raise_gaierror(host, port):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("bestteam.tools.http_client.socket.getaddrinfo", _raise_gaierror)
    resp = client.put("/api/org/email", json={
        "host": "nonexistent.example", "username": "u", "password": "p",
    })
    assert resp.status_code == 400
    assert "Name or service not known" not in resp.json()["detail"]


def test_test_connection_success_does_not_save(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    # Patched on `email_tools`, which is now the single place an _ImapBackend
    # is constructed -- `org_settings` builds its pre-save backend through the
    # same primitive the stored credential will use at run time.
    monkeypatch.setattr(email_tools, "_ImapBackend", _FakeBackend)
    resp = client.post("/api/org/email/test", json={
        "host": "imap.acme.com", "username": "u", "password": "p",
    })
    assert resp.json() == {"ok": True}
    assert client.get("/api/org/email").json() == {"connected": False}  # not saved


def test_test_connection_reports_failure(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    # Patched on `email_tools`, which is now the single place an _ImapBackend
    # is constructed -- `org_settings` builds its pre-save backend through the
    # same primitive the stored credential will use at run time.
    monkeypatch.setattr(email_tools, "_ImapBackend", _FakeBackend)
    _FakeBackend.ok = False
    body = client.post("/api/org/email/test", json={
        "host": "imap.acme.com", "username": "u", "password": "wrong",
    }).json()
    # A login rejection now surfaces the friendly app-password guidance, not the
    # raw "IMAP login failed" string (see _friendly_connect_error).
    assert body["ok"] is False and "app password" in body["error"].lower()


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


def test_session_list_carries_uses_email(client):
    # The list endpoint must resolve uses_email too -- it needs db + org context.
    # Regressing to _session_to_dict(s) without them silently reports False for
    # every listed session (a future "needs mailbox" badge would read wrong).
    email_sid = _make_session(_EMAIL_SPEC)
    plain_sid = _make_session(_PLAIN_SPEC)
    sessions = {s["id"]: s for s in client.get("/api/builder/sessions").json()["sessions"]}
    assert sessions[email_sid]["uses_email"] is True
    assert sessions[plain_sid]["uses_email"] is False


def test_mutation_response_carries_uses_email(client):
    # The flag must be computed on mutation responses too, not just GET -- else
    # a refine/save drops the connector while deploy still requires a mailbox.
    with open_test_db() as db:
        seed_default_skills(db)
    sid = client.post("/api/builder/sessions", json={"intent_text": "triage my inbox"}).json()["id"]
    resp = client.post(f"/api/builder/sessions/{sid}/specification", json={"specification": _EMAIL_SPEC})
    assert resp.status_code == 200
    assert resp.json()["uses_email"] is True


# --- friendly connection-error messages (no raw OS codes to non-technical users)

import socket  # noqa: E402

from ui.backend.org_settings import _friendly_connect_error  # noqa: E402


def test_friendly_error_timeout_points_at_993_without_os_code():
    # The exact failure the customer hit: a wrong port times out. The message
    # must name the port they used, suggest 993, and never leak "WinError 10060".
    msg = _friendly_connect_error(TimeoutError("timed out"), "imap.gmail.com", 994)
    assert "993" in msg
    assert "994" in msg
    assert "WinError" not in msg


def test_friendly_error_oserror_winerror_10060_treated_as_timeout():
    exc = OSError("[WinError 10060] A connection attempt failed ...")
    exc.winerror = 10060
    msg = _friendly_connect_error(exc, "imap.gmail.com", 994)
    assert "993" in msg
    assert "WinError" not in msg


def test_friendly_error_connection_refused_names_port():
    msg = _friendly_connect_error(ConnectionRefusedError("refused"), "mail.acme.com", 143)
    assert "refused" in msg.lower()
    assert "143" in msg


def test_friendly_error_dns_failure_flags_server_address():
    msg = _friendly_connect_error(socket.gaierror("name resolution failed"), "imap.typo.com", 993)
    assert "imap.typo.com" in msg
    assert "WinError" not in msg


def test_friendly_error_login_rejection_suggests_app_password():
    exc = ConfigurationError("IMAP login to 'h' as 'u@x' failed: [AUTHENTICATIONFAILED]")
    msg = _friendly_connect_error(exc, "h", 993)
    assert "app password" in msg.lower()


def test_friendly_error_generic_does_not_leak_raw_reason():
    msg = _friendly_connect_error(OSError("weird low-level thing"), "mail.acme.com", 993)
    assert "weird low-level thing" not in msg
    assert "mail.acme.com" in msg


class _TimeoutBackend:
    def __init__(self, **kw):
        pass

    def _connect(self):
        raise TimeoutError("timed out")

    def check_drafts_writable(self):
        raise TimeoutError("timed out")


def test_test_connection_returns_friendly_timeout(client, monkeypatch):
    # End-to-end: a connect timeout via /test surfaces the friendly message,
    # not the raw OS string.
    _bypass_ssrf(monkeypatch)
    monkeypatch.setattr(email_tools, "_ImapBackend", _TimeoutBackend)
    body = client.post("/api/org/email/test", json={
        "host": "imap.gmail.com", "username": "u", "password": "p", "port": 994,
    }).json()
    assert body["ok"] is False
    assert "993" in body["error"]
    assert "WinError" not in body["error"]


def test_connect_mailbox_without_secrets_key_is_operator_friendly(client, monkeypatch):
    # A missing server-side encryption key is the operator's problem, not the
    # customer's -- the store path must NOT leak "BESTTEAM_SECRETS_KEY is not set"
    # to a non-technical end user, who can only be told to contact an admin.
    _bypass_ssrf(monkeypatch)
    monkeypatch.delenv("BESTTEAM_SECRETS_KEY", raising=False)
    resp = client.put("/api/org/email", json={
        "host": "imap.gmail.com", "username": "u@x", "password": "app-pw",
    })
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "BESTTEAM_SECRETS_KEY" not in detail
    assert "administrator" in detail.lower()


def test_connect_mailbox_unexpected_error_does_not_leak_internals(client, monkeypatch):
    # Any other failure while saving must also surface an actionable message,
    # never a raw internal string.
    _bypass_ssrf(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("psycopg2.OperationalError: connection reset 0xdeadbeef")

    monkeypatch.setattr(org_settings, "set_email_credentials", _boom)
    resp = client.put("/api/org/email", json={
        "host": "imap.gmail.com", "username": "u@x", "password": "p",
    })
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "0xdeadbeef" not in detail
    assert "administrator" in detail.lower()


# --- autonomous-trigger identity-change interactions ------------------------

from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger


def _enable_trigger(org_name="default"):
    with open_test_db() as db:
        org = get_or_create_org(db, org_name)
        upsert_email_trigger(db, org.id, pipeline_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)
        return org.id


def test_password_rotation_keeps_trigger_enabled(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "old"})
    org_id = _enable_trigger()
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "new"})
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).enabled is True


def test_mailbox_host_change_disables_trigger(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "p"})
    org_id = _enable_trigger()
    client.put("/api/org/email", json={"host": "imap.other.com", "username": "u@acme.com", "password": "p"})
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).enabled is False


def test_disconnect_disables_trigger(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    client.put("/api/org/email", json={"host": "imap.acme.com", "username": "u@acme.com", "password": "p"})
    org_id = _enable_trigger()
    client.delete("/api/org/email")
    with open_test_db() as db:
        assert get_email_trigger(db, org_id).enabled is False


def test_deploy_rejects_kb_named_after_builtin(client):
    bad_spec = {
        "name": "collide_team",
        "knowledge_bases": [{"name": "web_search", "path": "docs"}],
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi",
                    "tools": ["web_search"]}],
        "teams": [], "pipeline": {"steps": []},
    }
    sid = _make_session(bad_spec)
    resp = client.post(f"/api/builder/sessions/{sid}/deploy")
    assert resp.status_code == 400
    assert "web_search" in resp.json()["detail"]
    assert "built-in tool name" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Phase 0 (0.7): a mailbox is only "connected" if it is genuinely usable.
#
# Saving used to store whatever was posted, and the connection test only
# attempted a login. Every reply this platform produces is an APPEND to the
# drafts folder, so a mailbox whose drafts folder doesn't exist under the
# configured name passed both and then failed on the first real draft -- long
# after the customer had left the wizard.
# ---------------------------------------------------------------------------


def _creds(**over):
    base = {"host": "imap.acme.com", "username": "u@acme.com", "password": "pw"}
    base.update(over)
    return base


def test_test_connection_fails_when_the_drafts_folder_is_unusable(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _FakeBackend.drafts_ok = False
    body = client.post("/api/org/email/test", json=_creds()).json()
    assert body["ok"] is False
    assert "drafts folder" in body["error"].lower()


def test_saving_is_refused_when_the_drafts_folder_is_unusable(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _FakeBackend.drafts_ok = False
    resp = client.put("/api/org/email", json=_creds())
    assert resp.status_code == 400
    assert "drafts folder" in resp.json()["detail"].lower()
    # Nothing was stored -- the mailbox must not look connected afterwards.
    assert client.get("/api/org/email").json() == {"connected": False}


def test_saving_is_refused_when_the_login_fails(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _FakeBackend.ok = False
    resp = client.put("/api/org/email", json=_creds())
    assert resp.status_code == 400
    assert client.get("/api/org/email").json() == {"connected": False}


def test_a_working_mailbox_still_saves(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    resp = client.put("/api/org/email", json=_creds())
    assert resp.status_code == 200
    assert client.get("/api/org/email").json()["connected"] is True


# --- Microsoft 365 mailboxes -------------------------------------------------

_OAUTH_BODY = {
    "auth_type": "microsoft_oauth",
    "username": "support@acme.com",
    "client_secret": "shh",
    "oauth_tenant_id": "tenant-1",
    "oauth_client_id": "client-1",
}


def _working_token(monkeypatch):
    """Patch the token provider so it issues a token without any network."""
    provider = type("_P", (), {"token": lambda self: "tok-1"})()
    monkeypatch.setattr(email_tools, "MicrosoftClientCredentialsToken", lambda **kw: provider)
    return provider


def _failing_token(monkeypatch, message):
    def _make(**kw):
        raise_exc = ConfigurationError(message)

        class _P:
            def token(self):
                raise raise_exc

        return _P()

    monkeypatch.setattr(email_tools, "MicrosoftClientCredentialsToken", _make)


def test_connecting_a_microsoft_mailbox_stores_the_oauth_identifiers(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _working_token(monkeypatch)

    resp = client.put("/api/org/email", json=_OAUTH_BODY)
    assert resp.status_code == 200

    got = client.get("/api/org/email").json()
    assert got["auth_type"] == "microsoft_oauth"
    assert got["oauth_tenant_id"] == "tenant-1"
    assert got["oauth_client_id"] == "client-1"
    # Fixed server-side: the OAuth scope is bound to this host.
    assert got["host"] == "outlook.office365.com"
    # The secret is never echoed back, in any field.
    assert "shh" not in str(got) and "shh" not in resp.text
    with open_test_db() as db:
        cred = get_email_credentials(db, get_or_create_org(db, "default").id)
        assert cred.password_encrypted != "shh"


def test_a_client_supplied_host_is_discarded_for_a_microsoft_mailbox(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _working_token(monkeypatch)

    client.put("/api/org/email", json={**_OAUTH_BODY, "host": "evil.example.com"})
    assert client.get("/api/org/email").json()["host"] == "outlook.office365.com"


@pytest.mark.parametrize("missing", ["client_secret", "oauth_tenant_id", "oauth_client_id"])
def test_a_microsoft_mailbox_needs_every_oauth_field(client, missing):
    body = {k: v for k, v in _OAUTH_BODY.items() if k != missing}
    assert client.put("/api/org/email", json=body).status_code == 422


def test_a_microsoft_mailbox_rejects_a_password(client):
    assert client.put("/api/org/email", json={**_OAUTH_BODY, "password": "p"}).status_code == 422


def test_a_password_mailbox_rejects_stray_oauth_fields(client):
    assert client.put("/api/org/email", json={
        "host": "imap.example.com", "username": "u", "password": "p",
        "oauth_tenant_id": "tenant-1",
    }).status_code == 422


def test_the_existing_password_body_still_works_unchanged(client, monkeypatch):
    """An older client posts no auth_type at all; it must keep working."""
    _bypass_ssrf(monkeypatch)
    resp = client.put("/api/org/email", json={
        "host": "imap.example.com", "username": "u", "password": "p",
        "port": 993, "drafts": None,
    })
    assert resp.status_code == 200
    assert client.get("/api/org/email").json()["auth_type"] == "password"


def test_a_bad_client_secret_is_reported_as_an_application_problem(client, monkeypatch):
    """A token failure and a mailbox-access failure have completely different
    fixes, and by Microsoft's message alone they are not tellable apart -- so
    they are told apart by which step failed."""
    _bypass_ssrf(monkeypatch)
    _failing_token(monkeypatch, "Microsoft rejected the application's sign-in (401): "
                                "AADSTS7000215: Invalid client secret provided.")

    body = client.post("/api/org/email/test", json=_OAUTH_BODY).json()
    assert body["ok"] is False
    assert "client secret" in body["error"].lower()
    assert "app password" not in body["error"].lower(), "password advice is wrong here"


def test_an_unknown_tenant_is_reported_as_a_tenant_problem(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _failing_token(monkeypatch, "Microsoft rejected the application's sign-in (400): "
                                "AADSTS90002: Tenant 'nope' not found.")

    assert "Directory (tenant) ID" in client.post(
        "/api/org/email/test", json=_OAUTH_BODY
    ).json()["error"]


def test_a_working_token_with_a_refused_mailbox_names_the_exchange_setup(client, monkeypatch):
    """The most likely outcome of a half-finished Azure setup, and the one that
    is useless without a specific message."""
    _bypass_ssrf(monkeypatch)
    _working_token(monkeypatch)
    _FakeBackend.ok = False  # _connect() raises: the token was fine, access was not

    error = client.post("/api/org/email/test", json=_OAUTH_BODY).json()["error"]
    assert "Add-MailboxPermission" in error
    assert "IMAP.AccessAsApp" in error
    assert "support@acme.com" in error


def test_a_microsoft_mailbox_is_not_stored_when_its_drafts_folder_is_unusable(
    client, monkeypatch
):
    """The Phase 0 guarantee holds for both auth types: a mailbox that can't
    hold a draft must not look connected."""
    _bypass_ssrf(monkeypatch)
    _working_token(monkeypatch)
    _FakeBackend.drafts_ok = False

    assert client.put("/api/org/email", json=_OAUTH_BODY).status_code == 400
    assert client.get("/api/org/email").json() == {"connected": False}


def test_a_microsoft_mailbox_accepts_and_returns_a_secret_expiry(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _working_token(monkeypatch)

    resp = client.put("/api/org/email", json={
        **_OAUTH_BODY, "oauth_secret_expires_at": "2027-01-31",
    })
    assert resp.status_code == 200, resp.text
    body = client.get("/api/org/email").json()
    assert body["oauth_secret_expires_at"] == "2027-01-31"
    # The secret itself is never echoed back, expiry or not.
    assert "client_secret" not in body and "password" not in body


def test_a_password_mailbox_rejects_a_secret_expiry(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    resp = client.put("/api/org/email", json={
        **_creds(), "oauth_secret_expires_at": "2027-01-31",
    })
    assert resp.status_code == 422, resp.text


def test_the_secret_expiry_is_optional_for_microsoft(client, monkeypatch):
    _bypass_ssrf(monkeypatch)
    _working_token(monkeypatch)
    resp = client.put("/api/org/email", json=_OAUTH_BODY)
    assert resp.status_code == 200, resp.text
    assert client.get("/api/org/email").json()["oauth_secret_expires_at"] is None


def test_switching_back_to_a_password_mailbox_clears_the_stored_expiry(client, monkeypatch):
    # Otherwise a stale expiry would keep the sweep warning about a secret the
    # mailbox no longer uses.
    _bypass_ssrf(monkeypatch)
    _working_token(monkeypatch)
    client.put("/api/org/email", json={**_OAUTH_BODY, "oauth_secret_expires_at": "2027-01-31"})
    resp = client.put("/api/org/email", json=_creds())
    assert resp.status_code == 200, resp.text
    assert client.get("/api/org/email").json().get("oauth_secret_expires_at") is None
