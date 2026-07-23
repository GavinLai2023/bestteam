"""Unit tests for `check_host_allowed`'s SSRF guard, in particular that its
failure messages don't leak raw OS resolver details or resolved private IPs
to callers -- e.g. the customer-facing `/api/org/email` endpoints relay
`ConfigurationError` text straight into the HTTP response (see
`ui/backend/org_settings.py`), so this is a real information-disclosure
surface, not just an internal debug string."""

import socket

import pytest

from bestteam.exceptions import ConfigurationError
from bestteam.tools.http_client import check_host_allowed


def test_resolve_failure_does_not_leak_raw_os_error(monkeypatch):
    def _raise_gaierror(host, port):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror)

    with pytest.raises(ConfigurationError) as exc_info:
        check_host_allowed("nonexistent.example")

    message = str(exc_info.value)
    assert "Name or service not known" not in message
    assert "-2" not in message


def test_private_address_rejection_does_not_leak_resolved_ip(monkeypatch):
    # Hostname and resolved IP are deliberately different values so the
    # assertion can tell "the customer-supplied hostname is echoed back"
    # (fine) apart from "the resolved internal IP is disclosed" (the leak).
    def _resolve_to_private(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to_private)

    with pytest.raises(ConfigurationError) as exc_info:
        check_host_allowed("mail.example.com")

    message = str(exc_info.value)
    assert "10.0.0.5" not in message
    assert "private" in message or "internal" in message


def test_public_address_still_resolves():
    assert check_host_allowed("93.184.216.34") == "93.184.216.34"
