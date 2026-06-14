"""Password hashing + bearer-token primitives for the per-deployment login (Phase 3).

Stdlib-only (no `passlib`/`python-jose` dependency): passwords are hashed with
PBKDF2-HMAC-SHA256 (`hash_password`/`verify_password`); access tokens are
JWT-shaped (header.payload.signature, base64url) and HMAC-SHA256 signed
(`create_access_token`/`decode_access_token`). `BESTTEAM_SECRET_KEY` should be
set to a long random value in any real deployment -- the default is only for
local dev/tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

_DEFAULT_SECRET_KEY = "bestteam-dev-secret-change-me"
SECRET_KEY = os.environ.get("BESTTEAM_SECRET_KEY", _DEFAULT_SECRET_KEY)
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("BESTTEAM_ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

_PBKDF2_ALGORITHM = "sha256"
_PBKDF2_ITERATIONS = 260_000


class AuthError(Exception):
    """Raised for an invalid, malformed, or expired access token."""


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGORITHM, password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected_hex = password_hash.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGORITHM, password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(digest.hex(), expected_hex)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_access_token(username: str, *, expires_minutes: Optional[int] = None) -> str:
    minutes = ACCESS_TOKEN_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    header = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    payload = _b64encode(json.dumps({"sub": username, "exp": int(time.time()) + minutes * 60}).encode("utf-8"))
    signing_input = f"{header}.{payload}"
    signature = hmac.new(SECRET_KEY.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64encode(signature)}"


def decode_access_token(token: str) -> str:
    """Return the username (`sub` claim) encoded in `token`.

    Raises `AuthError` if the token is malformed, has an invalid signature,
    or has expired.
    """
    try:
        header, payload, signature = token.split(".")
    except ValueError:
        raise AuthError("Malformed token")

    signing_input = f"{header}.{payload}"
    expected_signature = hmac.new(SECRET_KEY.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    try:
        actual_signature = _b64decode(signature)
    except Exception:
        raise AuthError("Malformed token")
    if not hmac.compare_digest(expected_signature, actual_signature):
        raise AuthError("Invalid token signature")

    try:
        claims = json.loads(_b64decode(payload))
    except Exception:
        raise AuthError("Malformed token")

    if claims.get("exp", 0) < time.time():
        raise AuthError("Token has expired")

    username = claims.get("sub")
    if not username:
        raise AuthError("Token missing subject")
    return username
