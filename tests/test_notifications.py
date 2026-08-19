"""Webhook delivery for notifications (ui/backend/notifications.py)."""

import hashlib
import hmac
import json

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet

from ui.backend import notifications, secret_store
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.notifications import (
    create_notification,
    set_notification_settings,
)
from ui.backend.db.orgs import get_or_create_org
from ui.backend.notifications import (
    MAX_DELIVERY_ATTEMPTS,
    WebhookUrlError,
    _payload,
    _sign,
    dispatch_pending,
    validate_webhook_url,
)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def _org(db, name="acme"):
    return get_or_create_org(db, name).id


def _notify(db, org_id):
    row = create_notification(
        db, org_id=org_id, kind="trigger_health", severity="error",
        title="Automatic email replies are failing", body="b", fingerprint="pipeline",
    )
    db.commit()
    return row


class _FakeConnection:
    """Stands in for `_PinnedHTTPSConnection`, recording what was dialled."""

    def __init__(self, host, port, *, pinned_ip, timeout, log, status):
        self._log = log
        self._status = status
        self._entry = {"host": host, "port": port, "pinned_ip": pinned_ip}

    @classmethod
    def record(cls, monkeypatch, status=200):
        """Patch the connection class in and return the list it appends to."""
        log = []
        monkeypatch.setattr(
            notifications, "_PinnedHTTPSConnection",
            lambda host, port, *, pinned_ip, timeout: cls(
                host, port, pinned_ip=pinned_ip, timeout=timeout, log=log, status=status,
            ),
        )
        return log

    def request(self, method, path, body=None, headers=None):
        self._entry["path"] = path
        self._log.append(self._entry)

    def getresponse(self):
        return self

    @property
    def status(self):
        return self._status

    def read(self):
        return b""

    def close(self):
        pass


class _Recorder:
    """Stands in for the network: records posts, returns a scripted status."""

    def __init__(self, status=200, boom=None):
        self.status = status
        self.boom = boom
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if self.boom is not None:
            raise self.boom
        return self.status


# --- no webhook configured ---------------------------------------------------


def test_without_a_webhook_the_notification_is_skipped_not_failed(db):
    # In-app only is a legitimate configuration, not a delivery failure.
    org_id = _org(db)
    row = _notify(db, org_id)
    assert dispatch_pending(db) == 1
    db.refresh(row)
    assert row.delivery_state == "skipped"
    assert row.delivery_attempts == 0


def test_a_disabled_webhook_is_skipped(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/h", enabled=False)
    row = _notify(db, org_id)
    recorder = _Recorder()
    monkeypatch.setattr(notifications, "_post", recorder)
    dispatch_pending(db)
    db.refresh(row)
    assert row.delivery_state == "skipped"
    assert recorder.calls == []


# --- successful delivery -----------------------------------------------------


def test_a_2xx_marks_the_notification_delivered(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/hook")
    row = _notify(db, org_id)
    monkeypatch.setattr(notifications, "_post", _Recorder(status=204))
    dispatch_pending(db)
    db.refresh(row)
    assert row.delivery_state == "delivered"
    assert row.delivered_at is not None


def test_the_signature_is_hmac_sha256_over_the_exact_body(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(
        db, org_id, webhook_url="https://example.com/hook", webhook_secret="k",
    )
    _notify(db, org_id)
    recorder = _Recorder()
    monkeypatch.setattr(notifications, "_post", recorder)
    dispatch_pending(db)

    _, body, headers = recorder.calls[0]
    expected = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert headers[notifications.SIGNATURE_HEADER] == expected


def test_no_secret_means_no_signature_header(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/hook")
    _notify(db, org_id)
    recorder = _Recorder()
    monkeypatch.setattr(notifications, "_post", recorder)
    dispatch_pending(db)
    assert notifications.SIGNATURE_HEADER not in recorder.calls[0][2]


def test_sign_matches_a_receivers_own_computation():
    assert _sign("k", b'{"a":1}') == (
        "sha256=" + hmac.new(b"k", b'{"a":1}', hashlib.sha256).hexdigest()
    )


# --- the payload carries no email content ------------------------------------


def test_the_payload_carries_only_health_information(db):
    org_id = _org(db)
    row = _notify(db, org_id)
    payload = _payload(row)
    assert set(payload) == {
        "id", "org_id", "kind", "severity", "title", "body", "fingerprint",
        "created_at",
    }


def test_the_posted_body_is_json_and_matches_the_payload(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/hook")
    row = _notify(db, org_id)
    recorder = _Recorder()
    monkeypatch.setattr(notifications, "_post", recorder)
    dispatch_pending(db)
    assert json.loads(recorder.calls[0][1].decode()) == _payload(row)


# --- retries -----------------------------------------------------------------


def test_a_non_2xx_retries_and_eventually_fails(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/hook")
    row = _notify(db, org_id)
    monkeypatch.setattr(notifications, "_post", _Recorder(status=500))

    for _ in range(MAX_DELIVERY_ATTEMPTS - 1):
        dispatch_pending(db)
        db.refresh(row)
        assert row.delivery_state == "pending"  # still retriable

    dispatch_pending(db)
    db.refresh(row)
    assert row.delivery_state == "failed"
    assert row.delivery_attempts == MAX_DELIVERY_ATTEMPTS
    assert row.last_delivery_error


def test_a_failed_delivery_still_leaves_the_notification_readable_in_app(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/hook")
    row = _notify(db, org_id)
    monkeypatch.setattr(notifications, "_post", _Recorder(boom=OSError("no route")))
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        dispatch_pending(db)
    db.refresh(row)
    assert row.delivery_state == "failed"
    # The customer still learns -- just not through the webhook.
    assert row.read_at is None
    assert row.title


def test_a_transport_error_never_escapes(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/hook")
    _notify(db, org_id)
    monkeypatch.setattr(notifications, "_post", _Recorder(boom=RuntimeError("kaboom")))
    dispatch_pending(db)  # must not raise


def test_an_error_message_is_truncated(db, monkeypatch):
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://example.com/hook")
    row = _notify(db, org_id)
    monkeypatch.setattr(notifications, "_post", _Recorder(boom=OSError("x" * 5000)))
    dispatch_pending(db)
    db.refresh(row)
    assert len(row.last_delivery_error) <= 500


# --- URL validation ----------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://example.com/hook",       # not HTTPS
    "https://127.0.0.1/hook",        # loopback
    "https://192.168.1.5/hook",      # private range
    "https://localhost/hook",
    "",                              # empty
    "https://",                      # no host
])
def test_unusable_webhook_urls_are_refused(url):
    with pytest.raises(WebhookUrlError):
        validate_webhook_url(url)


def test_delivery_dials_the_validated_ip_not_the_hostname(monkeypatch):
    # `check_host_allowed` approves one resolution; the connection must go to
    # THAT address. Re-resolving (what `urlopen` did) lets a rebinding
    # hostname point at an internal service after passing the check.
    monkeypatch.setattr(notifications, "check_host_allowed", lambda h: "203.0.113.5")
    dialled = _FakeConnection.record(monkeypatch)

    assert notifications._post(
        "https://hooks.example.com/services/abc?x=1", b"{}", {"Content-Type": "application/json"}
    ) == 200
    assert dialled == [{
        "host": "hooks.example.com",       # kept for TLS SNI + certificate
        "pinned_ip": "203.0.113.5",        # but this is what is connected to
        "port": 443,
        "path": "/services/abc?x=1",
    }]


def test_a_host_that_rebinds_to_private_is_refused_at_delivery_time(db, monkeypatch):
    # Validation happens again at connect time, so a URL that was public when
    # the admin saved it cannot be delivered to once it points inward.
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://rebind.example.com/h")
    row = _notify(db, org_id)
    _FakeConnection.record(monkeypatch)
    monkeypatch.setattr(
        notifications, "check_host_allowed",
        lambda h: (_ for _ in ()).throw(ValueError("resolves to a private/internal address")),
    )

    dispatch_pending(db)
    db.refresh(row)
    assert row.delivery_state == "pending"  # attempted and failed, not delivered
    assert "private/internal" in row.last_delivery_error


def test_a_redirect_is_not_followed(db, monkeypatch):
    # Following one would take the POST to an address nothing validated.
    org_id = _org(db)
    set_notification_settings(db, org_id, webhook_url="https://hooks.example.com/h")
    row = _notify(db, org_id)
    monkeypatch.setattr(notifications, "check_host_allowed", lambda h: "203.0.113.5")
    _FakeConnection.record(monkeypatch, status=302)

    dispatch_pending(db)
    db.refresh(row)
    assert row.delivery_state == "pending"
    assert row.delivery_attempts == 1


def test_a_public_https_url_is_accepted(monkeypatch):
    # `check_host_allowed` resolves DNS for real, so stub it: this test is
    # about the validator's own rules and the trimming, not about whether a
    # particular domain exists from wherever the suite is running.
    checked = []
    monkeypatch.setattr(notifications, "check_host_allowed", lambda h: checked.append(h))
    assert validate_webhook_url("  https://hooks.example.com/services/abc  ") == (
        "https://hooks.example.com/services/abc"
    )
    # The hostname alone reaches the SSRF check -- not the path, not the scheme.
    assert checked == ["hooks.example.com"]
