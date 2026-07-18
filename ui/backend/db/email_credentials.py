"""CRUD for `OrgEmailCredential` -- one org's encrypted mailbox connection.

Passwords are encrypted here (via `secret_store`) before they touch the DB, so
callers pass and receive plaintext and the stored column is always a Fernet
token. Reading a credential does NOT decrypt the password; call
`secret_store.decrypt(row.password_encrypted)` at the point of use (see
`ui/backend/email_tools.py`).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .. import secret_store
from .models import OrgEmailCredential


def get_email_credentials(db: Session, org_id: int) -> Optional[OrgEmailCredential]:
    return db.query(OrgEmailCredential).filter_by(org_id=org_id).one_or_none()


def set_email_credentials(
    db: Session,
    org_id: int,
    *,
    host: str,
    username: str,
    password: str,
    port: int = 993,
    drafts_folder: Optional[str] = None,
    backend: str = "imap",
) -> OrgEmailCredential:
    """Create or replace an org's mailbox credentials (upsert on `org_id`).

    `password` is plaintext in; it is encrypted before storage. Raises
    `secret_store.SecretsKeyError` if `BESTTEAM_SECRETS_KEY` is unset/invalid,
    or if it collides with the JWT signing key.
    """
    secret_store.ensure_key_separation()
    token = secret_store.encrypt(password)
    row = get_email_credentials(db, org_id)
    if row is None:
        row = OrgEmailCredential(org_id=org_id)
        db.add(row)
    row.backend = backend
    row.host = host
    row.port = port
    row.username = username
    row.password_encrypted = token
    row.drafts_folder = drafts_folder
    db.commit()
    db.refresh(row)
    return row


def clear_email_credentials(db: Session, org_id: int) -> bool:
    """Remove an org's mailbox credentials. Returns True if a row was deleted."""
    row = get_email_credentials(db, org_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def ensure_secrets_key_for_stored_credentials(db: Session) -> None:
    """Refuse to boot if credentials are stored but the secrets key can't read them.

    A missing/rotated `BESTTEAM_SECRETS_KEY` would otherwise surface only when a
    run first tries to use email -- much worse than failing at startup. Checks
    **every** stored row (a partial rotation could leave some rows readable and
    others not) and names the affected orgs. No-op when no org has stored
    credentials (the key isn't needed then).
    """
    rows = db.query(OrgEmailCredential).all()
    if not rows:
        return
    secret_store.ensure_key_separation()
    if not secret_store.secrets_key_configured():
        raise RuntimeError(
            f"Stored per-org email credentials exist, but {secret_store.SECRETS_KEY_ENV} "
            "is not set. Set it to the Fernet key those credentials were encrypted with."
        )
    undecryptable = [r.org_id for r in rows if not secret_store.can_decrypt(r.password_encrypted)]
    if undecryptable:
        raise RuntimeError(
            f"{secret_store.SECRETS_KEY_ENV} cannot decrypt the stored email credentials for "
            f"org id(s) {sorted(undecryptable)} (wrong or rotated key). Restore the original "
            "key, or clear and re-enter the affected mailboxes "
            "(`admin clear-email <org>` / `set-email <org>`). The operator CLI runs even when "
            "the app refuses to start."
        )
