"""Tests for the anonymous share-session signed cookie (share_auth.py) --
deliberately separate from auth.py's JWT bearer tokens."""

import pytest

from ui.backend.share_auth import sign_session_token, verify_cookie_value

pytestmark = pytest.mark.unit


def test_sign_then_verify_round_trips():
    signed = sign_session_token("abc123")
    assert verify_cookie_value(signed) == "abc123"


def test_verify_rejects_tampered_token():
    signed = sign_session_token("abc123")
    tampered = signed.replace("abc123", "xyz999")
    assert verify_cookie_value(tampered) is None


def test_verify_rejects_tampered_signature():
    signed = sign_session_token("abc123")
    token_part, _sig = signed.rsplit(".", 1)
    assert verify_cookie_value(f"{token_part}.notarealsignature") is None


def test_verify_rejects_malformed_value():
    assert verify_cookie_value("no-dot-separator") is None
    assert verify_cookie_value("") is None


def test_verify_rejects_non_ascii_token():
    """Non-ASCII token causes UnicodeEncodeError in _signature_for; should return None."""
    assert verify_cookie_value("café.somehash") is None


def test_verify_rejects_non_ascii_signature():
    """Non-ASCII signature causes UnicodeError in hmac.compare_digest; should return None."""
    assert verify_cookie_value("abc123.café") is None
