"""CRUD for `Feedback` -- defect reports and suggestions.

Write paths guarantee exactly one of submitted_by/share_session_id is set;
there is no delete (triage is a status change, the row is the record). All
feedback is read by the platform operator only -- `org_id` is provenance --
so nothing here filters by caller identity. See docs/superpowers/specs/
2026-08-26-feedback-system-design.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .models import Feedback

KINDS = ("defect", "suggestion")
STATUSES = ("new", "acknowledged", "resolved", "dismissed")


def create_feedback(
    db: Session,
    *,
    kind: str,
    body: str,
    org_id: Optional[int] = None,
    submitted_by: Optional[int] = None,
    share_session_id: Optional[int] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Feedback:
    if (submitted_by is None) == (share_session_id is None):
        raise ValueError("exactly one of submitted_by/share_session_id must be set")
    row = Feedback(
        kind=kind,
        body=body,
        org_id=org_id,
        submitted_by=submitted_by,
        share_session_id=share_session_id,
        context=context,
    )
    db.add(row)
    db.flush()
    return row


def count_session_feedback_today(db: Session, share_session_id: int) -> int:
    """Today's rows for one visitor session (UTC midnight boundary, matching
    the naive-UTC `created_at` convention). A plain count, not the
    `try_consume_turn` CAS: nothing is billed per submission, so the benign
    race (two simultaneous submits reaching cap+1) buys nothing to close."""
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    return (
        db.query(Feedback)
        .filter(Feedback.share_session_id == share_session_id, Feedback.created_at >= midnight)
        .count()
    )


def list_feedback(
    db: Session,
    *,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    org_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Feedback]:
    query = db.query(Feedback)
    if status is not None:
        query = query.filter(Feedback.status == status)
    if kind is not None:
        query = query.filter(Feedback.kind == kind)
    if org_id is not None:
        query = query.filter(Feedback.org_id == org_id)
    return (
        query.order_by(Feedback.created_at.desc(), Feedback.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def get_feedback(db: Session, feedback_id: int) -> Optional[Feedback]:
    return db.query(Feedback).filter_by(id=feedback_id).one_or_none()


def update_feedback(
    db: Session,
    row: Feedback,
    *,
    status: Optional[str] = None,
    admin_note: Optional[str] = None,
) -> Feedback:
    if status is not None:
        row.status = status
    if admin_note is not None:
        row.admin_note = admin_note
    db.flush()
    return row
