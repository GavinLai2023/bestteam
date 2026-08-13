"""Security regressions for the IMAP backend's transport handling.

Covers the connect-time hardening added after the PR #18 review:
- TLS is verified against the hostname (an explicit default context; imaplib's
  fallback does not verify) and every socket op is bounded by a timeout.
- The customer-supplied path (`restrict_to_public=True`) re-validates and pins
  to the checked IP on every connect, closing a DNS-rebinding window, while the
  operator-trusted env path (`False`) does not restrict a private host.

All $0: imaplib.IMAP4_SSL / the pinned subclass / DNS resolution are patched.
"""
import ssl
from unittest.mock import MagicMock, patch

import pytest

from bestteam.exceptions import ConfigurationError
from bestteam.tools.email_client import _ImapBackend

pytestmark = pytest.mark.unit

def _logged_in_conn():
    conn = MagicMock()
    conn.login.return_value = ("OK", [b"ok"])
    return conn


def test_connect_verifies_tls_and_sets_timeout():
    captured = {}

    def fake_ssl(host, port, *, ssl_context, timeout):
        captured["ssl_context"] = ssl_context
        captured["timeout"] = timeout
        return _logged_in_conn()

    backend = _ImapBackend(host="mail.example.com", user="u", password="p")
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", side_effect=fake_ssl):
        backend._connect()

    ctx = captured["ssl_context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert isinstance(captured["timeout"], (int, float)) and captured["timeout"] > 0


def test_restrict_to_public_pins_to_validated_ip(monkeypatch):
    monkeypatch.setattr(
        "bestteam.tools.email_client.check_host_allowed", lambda h: "93.184.216.34"
    )
    captured = {}

    class _FakePinned:
        def __init__(self, host, port, *, pinned_ip, ssl_context, timeout):
            captured["host"] = host
            captured["pinned_ip"] = pinned_ip
            captured["timeout"] = timeout

        def login(self, user, password):
            return ("OK", [b"ok"])

    monkeypatch.setattr("bestteam.tools.email_client._PinnedIMAP4_SSL", _FakePinned)
    backend = _ImapBackend(
        host="imap.acme.com", user="u", password="p", restrict_to_public=True
    )
    backend._connect()

    # TLS still validates against the hostname; the socket dials the checked IP.
    assert captured["host"] == "imap.acme.com"
    assert captured["pinned_ip"] == "93.184.216.34"
    assert captured["timeout"] > 0


def test_restrict_to_public_refuses_private_rebind(monkeypatch):
    def _rebind_private(host):
        raise ConfigurationError(
            f"host '{host}' resolves to a private/internal address (10.0.0.5)"
        )

    monkeypatch.setattr(
        "bestteam.tools.email_client.check_host_allowed", _rebind_private
    )
    backend = _ImapBackend(
        host="imap.acme.com", user="u", password="p", restrict_to_public=True
    )
    with pytest.raises(ConfigurationError, match="private/internal"):
        backend._connect()


def test_pinned_create_socket_dials_ip_keeps_hostname_for_tls(monkeypatch):
    # Exercises the real _PinnedIMAP4_SSL override (bypassing the full imaplib
    # constructor): dial the validated IP, but hand the hostname to wrap_socket
    # so TLS SNI + certificate verification still target the hostname.
    from bestteam.tools import email_client

    obj = email_client._PinnedIMAP4_SSL.__new__(email_client._PinnedIMAP4_SSL)
    obj._pinned_ip = "93.184.216.34"
    obj.host = "imap.acme.com"
    obj.port = 993

    captured = {}

    class _FakeCtx:
        def wrap_socket(self, sock, server_hostname=None):
            captured["server_hostname"] = server_hostname
            return "wrapped-sock"

    obj.ssl_context = _FakeCtx()
    raw_sock = object()
    monkeypatch.setattr(
        email_client.socket,
        "create_connection",
        lambda address, timeout: captured.setdefault("address", address) or raw_sock,
    )

    result = obj._create_socket(30)
    assert captured["address"] == ("93.184.216.34", 993)
    assert captured["server_hostname"] == "imap.acme.com"
    assert result == "wrapped-sock"


def test_env_path_does_not_restrict_private_host(monkeypatch):
    # The operator-trusted env path may legitimately point at an internal IMAP
    # server, so it must NOT run the SSRF check.
    calls = {"n": 0}

    def _spy(host):
        calls["n"] += 1
        return "10.0.0.5"

    monkeypatch.setattr("bestteam.tools.email_client.check_host_allowed", _spy)
    backend = _ImapBackend(host="imap.internal.corp", user="u", password="p")
    with patch(
        "bestteam.tools.email_client.imaplib.IMAP4_SSL",
        return_value=_logged_in_conn(),
    ):
        backend._connect()
    assert calls["n"] == 0
