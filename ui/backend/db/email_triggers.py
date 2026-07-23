"""CRUD for `EmailTrigger` -- one org's autonomous new-mail trigger state.

Mirrors `db/email_credentials.py`: small helpers over the one-row-per-org
table. Poll-state mutations (cap counters, baselines, errors) are done by
`ui/backend/email_trigger.py` directly on the row inside its own session.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .models import EmailTrigger


def get_email_trigger(db: Session, org_id: int) -> Optional[EmailTrigger]:
    return db.query(EmailTrigger).filter_by(org_id=org_id).one_or_none()


def upsert_email_trigger(
    db: Session,
    org_id: int,
    *,
    workflow_name: str,
    enabled: bool,
    last_uid: int,
    uidvalidity: Optional[int],
) -> EmailTrigger:
    """Create or replace an org's trigger config (upsert on `org_id`).

    Resets neither the daily-cap counters nor `last_run_id` -- re-enabling on
    the same day keeps counting against the same cap.
    """
    row = get_email_trigger(db, org_id)
    if row is None:
        row = EmailTrigger(org_id=org_id)
        db.add(row)
    row.workflow_name = workflow_name
    row.enabled = enabled
    row.last_uid = last_uid
    row.uidvalidity = uidvalidity
    db.commit()
    db.refresh(row)
    return row


def list_enabled_triggers(db: Session) -> List[EmailTrigger]:
    return db.query(EmailTrigger).filter_by(enabled=True).order_by(EmailTrigger.org_id).all()
