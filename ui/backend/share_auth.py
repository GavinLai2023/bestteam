"""Signed session-cookie primitives for anonymous share-link visitors.

Deliberately separate from `auth.py`'s JWT bearer tokens: a share session has
no username, no password-reset-driven revocation, and no expiry tied to
`User.security_stamp` -- reusing those primitives would borrow semantics that
don't apply to an anonymous visitor. Uses the same HMAC building block and
`SECRET_KEY` as `auth.py`, in its own function pair.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from .auth import SECRET_KEY

COOKIE_NAME = "bestteam_share_session"


def _signature_for(session_token: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), session_token.encode("ascii"), hashlib.sha256).hexdigest()


def sign_session_token(session_token: str) -> str:
    """Wrap `session_token` (the ShareSession.session_token column value) in
    an HMAC signature suitable for a cookie value."""
    return f"{session_token}.{_signature_for(session_token)}"


def verify_cookie_value(value: str) -> Optional[str]:
    """Return the embedded session_token if `value`'s signature is valid,
    else None (malformed, empty, or tampered)."""
    try:
        session_token, signature = value.rsplit(".", 1)
    except ValueError:
        return None
    if not session_token:
        return None
    expected = _signature_for(session_token)
    if not hmac.compare_digest(expected, signature):
        return None
    return session_token
