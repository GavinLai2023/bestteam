"""Symmetric encryption for at-rest secrets (per-org mailbox credentials).

Stored credentials (e.g. an org's IMAP password) are encrypted with
``cryptography.fernet.Fernet`` (AES-128-CBC + HMAC-SHA256) under a key from
``BESTTEAM_SECRETS_KEY`` -- deliberately separate from the JWT signing key
(``BESTTEAM_SECRET_KEY``); a signing key must never double as an encryption key.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

The key is read from the environment on each use (not cached at import) so a
process that has no secrets configured never needs it, and tests can set it
per-case. ``MultiFernet`` (key rotation) is a future extension; only a single
key is supported today.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

SECRETS_KEY_ENV = "BESTTEAM_SECRETS_KEY"


class SecretsKeyError(RuntimeError):
    """The secrets key is missing or malformed."""


def secrets_key_configured() -> bool:
    """True if a non-empty ``BESTTEAM_SECRETS_KEY`` is set (not that it's valid)."""
    return bool(os.environ.get(SECRETS_KEY_ENV, "").strip())


def _fernet() -> Fernet:
    key = os.environ.get(SECRETS_KEY_ENV, "").strip()
    if not key:
        raise SecretsKeyError(
            f"{SECRETS_KEY_ENV} is not set. It is required to encrypt/decrypt stored "
            "credentials. Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise SecretsKeyError(
            f"{SECRETS_KEY_ENV} is not a valid Fernet key (expected 32 url-safe "
            "base64-encoded bytes)."
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a string; returns a url-safe token safe to store as text."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a token from :func:`encrypt`. Raises ``SecretsKeyError`` on a bad
    key and ``cryptography.fernet.InvalidToken`` on a tampered/foreign token."""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


def can_decrypt(token: str) -> bool:
    """True if `token` decrypts under the current key -- used by startup checks."""
    try:
        decrypt(token)
        return True
    except (SecretsKeyError, InvalidToken):
        return False
