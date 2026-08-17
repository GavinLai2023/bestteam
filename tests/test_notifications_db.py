"""Tests for notification storage (db/notifications.py)."""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet

from ui.backend import secret_store
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.notifications import (
    STATE_PENDING,
    create_notification,
    get_notification_settings,
    has_fingerprint,
    list_notifications,
    mark_read,
    pending_notifications,
    set_notification_settings,
    unread_count,
)
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


def _notify(db, org_id, *, fingerprint="workflow", severity="error"):
    return create_notification(
        db, org_id=org_id, kind="trigger_health", severity=severity,
        title="Automatic email replies are failing", body="b",
        fingerprint=fingerprint,
    )


def test_a_new_notification_is_pending_and_unread(db_session):
    org_id = _org(db_session)
    row = _notify(db_session, org_id)
    assert row.delivery_state == STATE_PENDING
    assert row.read_at is None
    assert row.delivery_attempts == 0


def test_notifications_are_org_scoped(db_session):
    acme, other = _org(db_session, "acme"), _org(db_session, "other")
    _notify(db_session, acme)
    assert len(list_notifications(db_session, acme)) == 1
    assert list_notifications(db_session, other) == []


def test_marking_another_orgs_notification_read_reports_failure(db_session):
    acme, other = _org(db_session, "acme"), _org(db_session, "other")
    row = _notify(db_session, acme)
    # False, not an exception: the caller turns it into a 404 so cross-org
    # probing can't tell "not yours" from "doesn't exist".
    assert mark_read(db_session, other, row.id) is False
    assert mark_read(db_session, acme, row.id) is True
    assert unread_count(db_session, acme) == 0


def test_unread_only_filters_read_rows(db_session):
    org_id = _org(db_session)
    first = _notify(db_session, org_id)
    _notify(db_session, org_id, fingerprint="mailbox")
    mark_read(db_session, org_id, first.id)
    assert len(list_notifications(db_session, org_id, unread_only=True)) == 1
    assert len(list_notifications(db_session, org_id)) == 2


def test_has_fingerprint_is_org_scoped(db_session):
    acme, other = _org(db_session, "acme"), _org(db_session, "other")
    _notify(db_session, acme, fingerprint="secret_expiry_30")
    assert has_fingerprint(db_session, acme, "secret_expiry_30") is True
    assert has_fingerprint(db_session, other, "secret_expiry_30") is False


def test_pending_notifications_are_returned_oldest_first(db_session):
    org_id = _org(db_session)
    first = _notify(db_session, org_id)
    second = _notify(db_session, org_id, fingerprint="mailbox")
    assert [n.id for n in pending_notifications(db_session)] == [first.id, second.id]


def test_the_webhook_secret_is_stored_encrypted(db_session):
    org_id = _org(db_session)
    set_notification_settings(
        db_session, org_id, webhook_url="https://example.com/hook",
        webhook_secret="s3cret", enabled=True,
    )
    row = get_notification_settings(db_session, org_id)
    assert row.webhook_secret_encrypted != "s3cret"
    assert secret_store.decrypt(row.webhook_secret_encrypted) == "s3cret"


def test_settings_upsert_replaces_rather_than_duplicating(db_session):
    org_id = _org(db_session)
    set_notification_settings(db_session, org_id, webhook_url="https://a.example/h")
    set_notification_settings(db_session, org_id, webhook_url="https://b.example/h")
    assert get_notification_settings(db_session, org_id).webhook_url == "https://b.example/h"


def test_keep_existing_secret_leaves_the_stored_secret_alone(db_session):
    # The UI never receives the secret back, so an update that doesn't resend
    # it must not wipe it.
    org_id = _org(db_session)
    set_notification_settings(
        db_session, org_id, webhook_url="https://a.example/h", webhook_secret="keepme",
    )
    set_notification_settings(
        db_session, org_id, webhook_url="https://b.example/h", keep_existing_secret=True,
    )
    row = get_notification_settings(db_session, org_id)
    assert secret_store.decrypt(row.webhook_secret_encrypted) == "keepme"


def test_clearing_the_secret_is_possible_without_keep_existing(db_session):
    org_id = _org(db_session)
    set_notification_settings(
        db_session, org_id, webhook_url="https://a.example/h", webhook_secret="gone",
    )
    set_notification_settings(db_session, org_id, webhook_url="https://a.example/h")
    assert get_notification_settings(db_session, org_id).webhook_secret_encrypted is None


def test_an_empty_webhook_url_is_stored_as_none(db_session):
    org_id = _org(db_session)
    set_notification_settings(db_session, org_id, webhook_url="   ")
    assert get_notification_settings(db_session, org_id).webhook_url is None
