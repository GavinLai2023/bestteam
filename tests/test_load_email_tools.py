"""Tests for per-org email tool resolution (ui/backend/email_tools.py).

Uses a recording fake backend (patched in place of `_ImapBackend`) so no real
IMAP connection is made -- the point is which mailbox each org's tools bind to.
"""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet

from ui.backend import email_tools, secret_store
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import set_email_credentials
from ui.backend.db.orgs import get_or_create_org


class _FakeBackend:
    """Records how it was constructed; returns its own user in find() output."""

    made = []

    def __init__(self, *, host, user, password=None, port=993, drafts=None,
                 restrict_to_public=False, token_provider=None):
        self.host, self.user, self.password, self.port, self.drafts = (
            host, user, password, port, drafts,
        )
        self.restrict_to_public = restrict_to_public
        self.token_provider = token_provider
        _FakeBackend.made.append(self)

    def find(self, query):
        return [{"id": "1", "from": self.user, "subject": "s", "date": "d"}]

    def read(self, message_id):
        return {"from": self.user, "body": "body"}

    def draft_reply(self, message_id, body):
        return f"drafted via {self.user}"


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    _FakeBackend.made = []
    monkeypatch.setattr(email_tools, "_ImapBackend", _FakeBackend)
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, Fernet.generate_key().decode())


@pytest.fixture
def db_session():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def _org(db, name):
    return get_or_create_org(db, name).id


def test_configured_org_binds_tools_to_its_mailbox(db_session):
    org_id = _org(db_session, "acme")
    set_email_credentials(
        db_session, org_id, host="imap.acme.com", username="support@acme.com", password="pw"
    )
    tools = email_tools.load_email_tools(db_session, org_id)

    assert set(tools) == {
        "email_find", "email_read", "email_read_attachment", "email_draft_reply",
    }
    # The backend was built with the decrypted password and the org's mailbox.
    backend = _FakeBackend.made[-1]
    assert backend.host == "imap.acme.com"
    assert backend.user == "support@acme.com"
    assert backend.password == "pw"
    # Customer-supplied host: the per-org path validates + pins on connect.
    assert backend.restrict_to_public is True
    # And the tool actually routes to that mailbox.
    assert "support@acme.com" in tools["email_find"]("")


def test_tools_keep_the_public_names_and_docs(db_session):
    org_id = _org(db_session, "acme")
    set_email_credentials(db_session, org_id, host="h", username="u", password="pw")
    tools = email_tools.load_email_tools(db_session, org_id)
    assert tools["email_find"].__name__ == "email_find"
    assert "mailbox" in tools["email_find"].__doc__


def test_cross_org_isolation(db_session):
    a = _org(db_session, "org_a")
    b = _org(db_session, "org_b")
    set_email_credentials(db_session, a, host="ha", username="a@x", password="pa")
    set_email_credentials(db_session, b, host="hb", username="b@x", password="pb")

    a_tools = email_tools.load_email_tools(db_session, a)
    b_tools = email_tools.load_email_tools(db_session, b)
    assert "a@x" in a_tools["email_find"]("")
    assert "b@x" in b_tools["email_find"]("")
    # Neither org's tools ever reach the other's mailbox.
    assert "b@x" not in a_tools["email_find"]("")
    assert "a@x" not in b_tools["email_find"]("")


def test_no_credentials_with_env_yields_empty(db_session, monkeypatch):
    # Single-org / SDK env path: don't shadow the env-based registry tools.
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "imap")
    assert email_tools.load_email_tools(db_session, _org(db_session, "acme")) == {}


def test_no_credentials_no_env_yields_friendly_tools(db_session, monkeypatch):
    monkeypatch.delenv("BESTTEAM_EMAIL_BACKEND", raising=False)
    tools = email_tools.load_email_tools(db_session, _org(db_session, "acme"))
    assert set(tools) == {"email_find", "email_read", "email_draft_reply"}
    msg = tools["email_find"]("anything")
    assert "no mailbox" in msg.lower()
    # Names/docs preserved so the model still recognizes them as the email tools.
    assert tools["email_read"].__name__ == "email_read"


def test_undecryptable_credentials_yield_unreadable_tools_not_a_crash(db_session, monkeypatch):
    # A wrong/rotated key must not crash the workflow build with InvalidToken;
    # the tools return a clear message instead.
    org_id = _org(db_session, "acme")
    set_email_credentials(db_session, org_id, host="h", username="u", password="p")
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, Fernet.generate_key().decode())  # wrong key
    tools = email_tools.load_email_tools(db_session, org_id)
    assert set(tools) == {"email_find", "email_read", "email_draft_reply"}
    assert "can't be read" in tools["email_find"]("").lower()


def test_yaml_demo_resolves_per_org_mailbox(db_session, monkeypatch, tmp_path):
    # Finding 2: a global YAML demo that uses the built-in email skill must
    # resolve the RUNNING org's mailbox, not the env registry -- and org A's
    # build must never carry org B's mailbox (per-org cache key).
    from ui.backend import main as backend_main
    from ui.backend.skills import seed_default_skills

    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setenv("BESTTEAM_DEMO_WORKFLOWS", "1")
    monkeypatch.delenv("BESTTEAM_EMAIL_BACKEND", raising=False)
    backend_main._workflow_cache.clear()
    seed_default_skills(db_session)
    (tmp_path / "emaildemo.yaml").write_text(
        "name: emaildemo\n"
        "agents:\n"
        "  - name: triager\n"
        "    role: Triager\n"
        "    goal: Triage the mailbox\n"
        '    model: "fake:done"\n'
        "    skills: [email_triage_reply]\n"
        "teams:\n"
        "  - name: t\n"
        "    agents: [triager]\n"
        "    mode: sequential\n"
        "workflow:\n"
        "  steps: [t]\n",
        encoding="utf-8",
    )
    a = _org(db_session, "org_a")
    b = _org(db_session, "org_b")
    set_email_credentials(db_session, a, host="ha", username="a@x", password="pa")
    set_email_credentials(db_session, b, host="hb", username="b@x", password="pb")

    def _email_find(wf):
        agent = wf.steps[0].agents[0]
        return next(t for t in agent.tools if t.__name__ == "email_find")

    wf_a = backend_main._get_workflow("emaildemo", db_session, a)
    wf_b = backend_main._get_workflow("emaildemo", db_session, b)
    assert "a@x" in _email_find(wf_a)("")
    assert "b@x" in _email_find(wf_b)("")
    assert "b@x" not in _email_find(wf_a)("")  # no cross-org leak


def test_connecting_mailbox_invalidates_workflow_cache_key(db_session):
    # Per-org email tools are baked into the compiled workflow, so connecting a
    # mailbox must change the freshness key that keys the workflow cache --
    # otherwise a "no mailbox connected" workflow would keep being served.
    from ui.backend import main as backend_main

    org_id = _org(db_session, "acme")
    before = backend_main._dependency_freshness(db_session)
    set_email_credentials(db_session, org_id, host="h", username="u", password="p")
    assert backend_main._dependency_freshness(db_session) != before


# ---------------------------------------------------------------------------
# Microsoft 365 (OAuth) mailboxes
# ---------------------------------------------------------------------------

def test_an_oauth_mailbox_builds_a_backend_with_a_token_provider(db_session):
    from bestteam.tools._oauth import MicrosoftClientCredentialsToken
    from ui.backend.db.email_credentials import AUTH_MICROSOFT_OAUTH, MICROSOFT_IMAP_HOST

    org_id = _org(db_session, "acme")
    set_email_credentials(
        db_session, org_id, host=MICROSOFT_IMAP_HOST, username="support@acme.com",
        password="the-client-secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )

    backend = email_tools.build_org_imap_backend(db_session, org_id)

    assert backend.password is None
    provider = backend.token_provider
    assert isinstance(provider, MicrosoftClientCredentialsToken)
    # The provider must carry the *decrypted* secret and address the right
    # tenant -- getting either wrong would fail only at first use, in production.
    assert provider.url.endswith("/tenant-1/oauth2/v2.0/token")
    assert provider._client_id == "client-1"
    assert provider._client_secret == "the-client-secret"
    assert backend.restrict_to_public is True


def test_a_password_mailbox_still_builds_a_password_backend(db_session):
    org_id = _org(db_session, "acme")
    set_email_credentials(
        db_session, org_id, host="imap.example.com", username="u", password="p"
    )

    backend = email_tools.build_org_imap_backend(db_session, org_id)

    assert backend.password == "p"
    assert backend.token_provider is None
