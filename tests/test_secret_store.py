"""Tests for at-rest secret encryption (ui/backend/secret_store.py)."""

import pytest

pytest.importorskip("cryptography")

from cryptography.fernet import Fernet, InvalidToken

from ui.backend import secret_store


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, k)
    return k


def test_encrypt_decrypt_round_trip(key):
    token = secret_store.encrypt("hunter2")
    assert token != "hunter2"  # not stored in the clear
    assert secret_store.decrypt(token) == "hunter2"


def test_ciphertext_differs_each_time(key):
    # Fernet embeds a random IV, so the same plaintext encrypts differently.
    assert secret_store.encrypt("x") != secret_store.encrypt("x")


def test_tampered_token_fails_to_decrypt(key):
    token = secret_store.encrypt("secret")
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    with pytest.raises(InvalidToken):
        secret_store.decrypt(tampered)


def test_token_from_a_different_key_fails(monkeypatch):
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, Fernet.generate_key().decode())
    token = secret_store.encrypt("secret")
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, Fernet.generate_key().decode())
    assert secret_store.can_decrypt(token) is False
    with pytest.raises(InvalidToken):
        secret_store.decrypt(token)


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv(secret_store.SECRETS_KEY_ENV, raising=False)
    assert secret_store.secrets_key_configured() is False
    with pytest.raises(secret_store.SecretsKeyError):
        secret_store.encrypt("x")


def test_invalid_key_raises(monkeypatch):
    monkeypatch.setenv(secret_store.SECRETS_KEY_ENV, "not-a-valid-fernet-key")
    with pytest.raises(secret_store.SecretsKeyError):
        secret_store.encrypt("x")
