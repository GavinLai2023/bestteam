"""CRUD for `Notification` and `OrgNotificationSetting`.

The webhook secret is encrypted here (via `secret_store`) before it touches the
DB, exactly as `email_credentials.py` handles mailbox passwords: callers pass
and receive plaintext, and the stored column is always a Fernet token. Reading
the settings does NOT decrypt the secret; call
`secret_store.decrypt(row.webhook_secret_encrypted)` at the point of use.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from .. import secret_store
from .models import Notification, OrgNotificationSetting

STATE_PENDING = "pending"
STATE_DELIVERED = "delivered"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_notification(
    db: Session,
    *,
    org_id: int,
    kind: str,
    severity: str,
    title: str,
    body: str,
    fingerprint: str,
) -> Notification:
    """Append a notification. Starts `pending`; dispatch decides its fate."""
    row = Notification(
        org_id=org_id,
        kind=kind,
        severity=severity,
        title=title,
        body=body,
        fingerprint=fingerprint,
    )
    db.add(row)
    db.flush()
    return row


def list_notifications(
    db: Session, org_id: int, *, unread_only: bool = False, limit: int = 50
) -> List[Notification]:
    query = db.query(Notification).filter_by(org_id=org_id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    return query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit).all()


def unread_count(db: Session, org_id: int) -> int:
    return (
        db.query(Notification)
        .filter_by(org_id=org_id)
        .filter(Notification.read_at.is_(None))
        .count()
    )


def mark_read(db: Session, org_id: int, notification_id: int) -> bool:
    """True if it was marked read. False when it belongs to another org --
    the caller turns that into a 404, so cross-org probing can't distinguish
    "not yours" from "doesn't exist"."""
    row = (
        db.query(Notification)
        .filter_by(id=notification_id, org_id=org_id)
        .one_or_none()
    )
    if row is None:
        return False
    if row.read_at is None:
        row.read_at = _utcnow()
    return True


def has_fingerprint(db: Session, org_id: int, fingerprint: str) -> bool:
    """Whether this org already has a notification with this fingerprint.

    Used by the secret-expiry sweep, whose state belongs to the credential
    rather than to a trigger row, so it has nowhere else to remember that it
    already warned.
    """
    return (
        db.query(Notification)
        .filter_by(org_id=org_id, fingerprint=fingerprint)
        .first()
        is not None
    )


def pending_notifications(db: Session, limit: int = 20) -> List[Notification]:
    return (
        db.query(Notification)
        .filter_by(delivery_state=STATE_PENDING)
        .order_by(Notification.id.asc())
        .limit(limit)
        .all()
    )


def get_notification_settings(db: Session, org_id: int) -> Optional[OrgNotificationSetting]:
    return db.query(OrgNotificationSetting).filter_by(org_id=org_id).one_or_none()


def set_notification_settings(
    db: Session,
    org_id: int,
    *,
    webhook_url: Optional[str],
    webhook_secret: Optional[str] = None,
    enabled: bool = True,
    keep_existing_secret: bool = False,
) -> OrgNotificationSetting:
    """Create or replace an org's notification settings (upsert on `org_id`).

    `webhook_secret` is plaintext in and encrypted before storage.
    `keep_existing_secret` lets the API accept an update that doesn't resend
    the secret -- the UI never receives it back, so it cannot echo it.
    """
    row = get_notification_settings(db, org_id)
    if row is None:
        row = OrgNotificationSetting(org_id=org_id)
        db.add(row)
    row.webhook_url = (webhook_url or "").strip() or None
    row.enabled = bool(enabled)
    if not keep_existing_secret:
        row.webhook_secret_encrypted = (
            secret_store.encrypt(webhook_secret) if webhook_secret else None
        )
    db.flush()
    return row
