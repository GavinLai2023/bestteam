"""Tests for per-org email credential storage (db/email_credentials.py)."""

import os

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet

from ui.backend import secret_store
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    AUTH_PASSWORD,
    MICROSOFT_IMAP_HOST,
    clear_email_credentials,
    ensure_secrets_key_for_stored_credentials,
    get_email_credentials,
    set_email_credentials,
)
from ui.backend.db.models import OrgEmailCredential
from ui.backend.db.orgs import get_or_create_org


@pytest.fixture
def db_session(monkeypatch):
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def _org(db, name="acme"):
    return get_or_create_org(db, name).id


def test_set_stores_encrypted_not_plaintext(db_session):
    org_id = _org(db_session)
    set_email_credentials(
        db_session, org_id, host="imap.example.com", username="me@example.com", password="s3cret"
    )
    row = db_session.query(OrgEmailCredential).one()
    assert row.password_encrypted != "s3cret"
    assert secret_store.decrypt(row.password_encrypted) == "s3cret"
    assert row.host == "imap.example.com"
    assert row.port == 993
    assert row.backend == "imap"


def test_set_is_upsert_on_org(db_session):
    org_id = _org(db_session)
    set_email_credentials(db_session, org_id, host="h1", username="u1", password="p1")
    set_email_credentials(db_session, org_id, host="h2", username="u2", password="p2", port=1993)
    rows = db_session.query(OrgEmailCredential).all()
    assert len(rows) == 1  # one mailbox per org
    assert rows[0].host == "h2"
    assert rows[0].port == 1993
    assert secret_store.decrypt(rows[0].password_encrypted) == "p2"


def test_get_and_clear(db_session):
    org_id = _org(db_session)
    assert get_email_credentials(db_session, org_id) is None
    set_email_credentials(db_session, org_id, host="h", username="u", password="p")
    assert get_email_credentials(db_session, org_id) is not None
    assert clear_email_credentials(db_session, org_id) is True
    assert get_email_credentials(db_session, org_id) is None
    assert clear_email_credentials(db_session, org_id) is False  # already gone


def test_two_orgs_have_independent_credentials(db_session):
    a = _org(db_session, "org_a")
    b = _org(db_session, "org_b")
    set_email_credentials(db_session, a, host="ha", username="a@x", password="pa")
    set_email_credentials(db_session, b, host="hb", username="b@x", password="pb")
    assert get_email_credentials(db_session, a).username == "a@x"
    assert get_email_credentials(db_session, b).username == "b@x"


def test_startup_guard_noop_without_credentials(db_session):
    ensure_secrets_key_for_stored_credentials(db_session)  # no rows -> no error


def test_startup_guard_rejects_missing_key(db_session, monkeypatch):
    set_email_credentials(db_session, _org(db_session), host="h", username="u", password="p")
    monkeypatch.delenv(secret_store.SECRETS_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError, match="BESTTEAM_SECRETS_KEY"):
        ensure_secrets_key_for_stored_credentials(db_session)


def test_startup_guard_rejects_wrong_key(db_session, monkeypatch):
    set_email_credentials(db_session, _org(db_session), host="h", username="u", password="p")
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="cannot decrypt"):
        ensure_secrets_key_for_stored_credentials(db_session)


def test_startup_guard_checks_every_row_and_names_affected_orgs(db_session):
    # A partial rotation can leave one org readable and another not. The guard
    # must check ALL rows (not just the first) and report which org failed.
    good = _org(db_session, "org_good")
    bad = _org(db_session, "org_bad")
    set_email_credentials(db_session, good, host="h", username="u", password="p")
    # A row whose token doesn't decrypt under the current key (e.g. old key).
    db_session.add(OrgEmailCredential(
        org_id=bad, host="h", port=993, username="u", password_encrypted="not-a-valid-token"
    ))
    db_session.commit()
    with pytest.raises(RuntimeError, match=str(bad)):
        ensure_secrets_key_for_stored_credentials(db_session)


def test_startup_guard_rejects_key_collision(db_session, monkeypatch):
    set_email_credentials(db_session, _org(db_session), host="h", username="u", password="p")
    same = os.environ[secret_store.SECRETS_KEY_ENV]
    monkeypatch.setenv("BESTTEAM_SECRET_KEY", same)
    with pytest.raises(secret_store.SecretsKeyError, match="BESTTEAM_SECRET_KEY"):
        ensure_secrets_key_for_stored_credentials(db_session)


def test_set_credentials_rejects_key_collision(db_session, monkeypatch):
    monkeypatch.setenv("BESTTEAM_SECRET_KEY", os.environ[secret_store.SECRETS_KEY_ENV])
    with pytest.raises(secret_store.SecretsKeyError):
        set_email_credentials(db_session, _org(db_session), host="h", username="u", password="p")


# ---------------------------------------------------------------------------
# Microsoft 365 (OAuth) mailboxes -- Exchange Online has no basic auth
# ---------------------------------------------------------------------------

def test_an_oauth_credential_round_trips_with_the_secret_encrypted(db_session):
    org_id = _org(db_session)
    row = set_email_credentials(
        db_session, org_id,
        host=MICROSOFT_IMAP_HOST, username="support@acme.com",
        password="the-client-secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )

    assert row.auth_type == AUTH_MICROSOFT_OAUTH
    assert row.oauth_tenant_id == "tenant-1"
    assert row.oauth_client_id == "client-1"
    # The client secret goes through exactly the same encrypted column the
    # mailbox password does -- one place a secret is written, one place the
    # boot-time key check has to look.
    assert "the-client-secret" not in row.password_encrypted
    assert secret_store.decrypt(row.password_encrypted) == "the-client-secret"


def test_credentials_default_to_password_auth(db_session):
    org_id = _org(db_session)
    row = set_email_credentials(
        db_session, org_id, host="imap.example.com", username="u", password="p"
    )
    assert row.auth_type == AUTH_PASSWORD
    assert row.oauth_tenant_id is None
    assert row.oauth_client_id is None


def test_switching_an_oauth_mailbox_back_to_a_password_clears_the_oauth_fields(db_session):
    org_id = _org(db_session)
    set_email_credentials(
        db_session, org_id, host=MICROSOFT_IMAP_HOST, username="support@acme.com",
        password="secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )
    row = set_email_credentials(
        db_session, org_id, host="imap.example.com", username="u", password="p"
    )

    assert row.auth_type == AUTH_PASSWORD
    assert row.oauth_tenant_id is None
    assert row.oauth_client_id is None


def test_the_boot_key_check_still_catches_an_unreadable_oauth_credential(
    db_session, monkeypatch
):
    org_id = _org(db_session)
    set_email_credentials(
        db_session, org_id, host=MICROSOFT_IMAP_HOST, username="support@acme.com",
        password="secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )
    monkeypatch.setattr(secret_store, "can_decrypt", lambda token: False)

    with pytest.raises(RuntimeError, match="cannot decrypt"):
        ensure_secrets_key_for_stored_credentials(db_session)
