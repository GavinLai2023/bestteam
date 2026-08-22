"""One credential -> IMAP connector implementation, shared by every path.

The defect these pin: `email_trigger` had its own `_make_backend` that ignored
`OrgEmailCredential.auth_type` and always passed `password=`. For a
`microsoft_oauth` credential that column holds the Entra *client secret*, so
the poller tried to LOGIN with a client secret -- an M365 org could connect,
test and run the manual tools, while every automatic poll and retry failed.

`tests/test_email_trigger.py` could not catch it: its autouse fixture replaces
the poller's factory for the whole module. These tests deliberately exercise
the real one.

See docs/superpowers/specs/2026-08-22-email-poller-oauth-and-claim-scoping-design.md.
"""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from cryptography.fernet import Fernet

from ui.backend import email_tools, email_trigger, org_settings
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    MICROSOFT_IMAP_HOST,
    get_email_credentials,
    set_email_credentials,
)
from ui.backend.db.email_triggers import upsert_email_trigger
from ui.backend.db.orgs import get_or_create_org
from ui.backend.email_trigger import poll_org


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    session = session_factory(engine)()
    yield session
    session.close()



@pytest.fixture
def captured_backends(monkeypatch):
    """Capture every `_ImapBackend(**kwargs)` the shared factory builds.

    Patched on `email_tools`, which after this change is the ONLY module that
    constructs one from stored credentials -- that is the property under test.
    """
    built = []

    def _fake(**kwargs):
        built.append(kwargs)
        return f"backend-{len(built)}"

    monkeypatch.setattr(email_tools, "_ImapBackend", _fake)
    return built


def _password_org(db, name="pw-org"):
    org = get_or_create_org(db, name)
    set_email_credentials(db, org.id, host="imap.acme.com", username="u@acme.com",
                          password="pw")
    return org


def _oauth_org(db, name="oauth-org"):
    org = get_or_create_org(db, name)
    set_email_credentials(
        db, org.id, host=MICROSOFT_IMAP_HOST, username="u@acme.com",
        password="client-secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )
    return org


# --- the factory itself -------------------------------------------------

def test_a_password_credential_builds_a_password_backend(db, captured_backends):
    org = _password_org(db)
    cred = get_email_credentials(db, org.id)

    email_tools.build_backend_for_credential(cred, "pw")

    assert captured_backends == [{
        "host": "imap.acme.com",
        "user": "u@acme.com",
        "password": "pw",
        "port": cred.port,
        "drafts": cred.drafts_folder,
        "restrict_to_public": True,  # customer-supplied host: validate and pin
    }]


def test_an_oauth_credential_builds_a_token_provider_backend(db, captured_backends):
    org = _oauth_org(db)
    cred = get_email_credentials(db, org.id)

    email_tools.build_backend_for_credential(cred, "client-secret")

    assert len(captured_backends) == 1
    kwargs = captured_backends[0]
    # The client secret must never be offered as an IMAP password.
    assert kwargs.get("password") is None
    assert "client-secret" not in repr(kwargs.get("password"))
    provider = kwargs["token_provider"]
    assert provider is not None
    # `url` is the provider's only public view of its identifiers; the client
    # id has no public accessor, so the private one is read deliberately -- the
    # property under test is that THIS org's Entra app is what will be used.
    assert "tenant-1" in provider.url
    assert provider._client_id == "client-1"
    assert kwargs["restrict_to_public"] is True


def test_build_org_imap_backend_goes_through_the_same_factory(db, captured_backends):
    org = _oauth_org(db)

    email_tools.build_org_imap_backend(db, org.id)

    assert captured_backends[0]["token_provider"] is not None
    assert captured_backends[0].get("password") is None


# --- the poller, which had its own factory ------------------------------

def test_poll_org_authenticates_an_oauth_mailbox_with_a_token_provider(
    db, monkeypatch, captured_backends
):
    """The regression itself: the automatic path must honour `auth_type`."""
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda backend, last: (3, 45, []))
    org = _oauth_org(db)
    trigger = upsert_email_trigger(db, org.id, pipeline_name="triage", enabled=True,
                                   last_uid=45, uidvalidity=3)

    poll_org(db, trigger, _unreachable_pipeline)

    assert len(captured_backends) == 1
    assert captured_backends[0]["token_provider"] is not None
    assert captured_backends[0].get("password") is None
    assert trigger.last_error is None  # the mailbox check succeeded


def test_poll_org_still_uses_a_password_for_a_password_mailbox(
    db, monkeypatch, captured_backends
):
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda backend, last: (3, 45, []))
    org = _password_org(db)
    trigger = upsert_email_trigger(db, org.id, pipeline_name="triage", enabled=True,
                                   last_uid=45, uidvalidity=3)

    poll_org(db, trigger, _unreachable_pipeline)

    assert captured_backends[0]["password"] == "pw"
    assert captured_backends[0].get("token_provider") is None


def test_the_poller_has_no_factory_of_its_own():
    """A second factory is how this drifted; there must not be one again."""
    assert not hasattr(email_trigger, "_make_backend")


# --- the third copy: the pre-save connection check ----------------------

def test_the_connect_check_builds_the_same_backend_shape(db, captured_backends):
    """`org_settings` validates an UNSAVED request, so it cannot call the
    stored-credential factory -- but it must share the primitives, or it
    validates a differently-built backend than the one that will run."""
    req = org_settings.EmailConnectRequest(
        host=MICROSOFT_IMAP_HOST, username="u@acme.com",
        auth_type=AUTH_MICROSOFT_OAUTH, oauth_tenant_id="tenant-1",
        oauth_client_id="client-1", client_secret="client-secret",
    )

    provider = org_settings._token_provider_for(req)
    org_settings._backend_for(req, provider)

    assert captured_backends[0]["token_provider"] is provider
    assert captured_backends[0].get("password") is None
    assert captured_backends[0]["restrict_to_public"] is True


def _unreachable_pipeline(name, db, org_id, allowed_uids, backend):
    raise AssertionError("no pipeline should be built in these tests")
