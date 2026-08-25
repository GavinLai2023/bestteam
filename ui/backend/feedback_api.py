"""Feedback: defect reports and suggestions.

Submission is open to every authenticated principal (org members and platform
admins); reading and triage are platform-admin only -- all feedback belongs to
the operator, and `org_id` on a row is provenance rather than ownership, so
none of the org-scoping machinery applies here. The anonymous share-link
counterpart lives in `share_chat.py` (it needs that module's link/session/
cookie helpers). See docs/superpowers/specs/
2026-08-26-feedback-system-design.md.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth_api import get_current_admin, get_current_user
from .db.feedback import (
    KINDS,
    STATUSES,
    create_feedback,
    get_feedback,
    list_feedback,
    update_feedback,
)
from .db.models import Organization, User, iso_utc
from .db_session import get_db

router = APIRouter(prefix="/api", tags=["feedback"])

MAX_BODY_CHARS = 4000
_CONTEXT_VALUE_CHARS = 200

_AUTHED_CONTEXT_KEYS: FrozenSet[str] = frozenset({"page", "locale"})


def sanitize_context(
    raw: Optional[Dict[str, Any]], allowed: FrozenSet[str]
) -> Optional[Dict[str, str]]:
    """Keep only whitelisted keys, coerced to bounded strings -- the client
    dict is attacker-shaped on the share surface and merely untrusted here."""
    if not raw:
        return None
    kept = {
        key: str(value)[:_CONTEXT_VALUE_CHARS]
        for key, value in raw.items()
        if key in allowed and value is not None
    }
    return kept or None


class FeedbackCreate(BaseModel):
    kind: Literal["defect", "suggestion"]
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    context: Optional[Dict[str, Any]] = None


class FeedbackPatch(BaseModel):
    status: Optional[Literal["new", "acknowledged", "resolved", "dismissed"]] = None
    admin_note: Optional[str] = Field(default=None, max_length=MAX_BODY_CHARS)


@router.post("/feedback", status_code=201)
def submit_feedback(
    payload: FeedbackCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Feedback body is empty")
    row = create_feedback(
        db,
        kind=payload.kind,
        body=body,
        org_id=user.org_id,
        submitted_by=user.id,
        context=sanitize_context(payload.context, _AUTHED_CONTEXT_KEYS),
    )
    db.commit()
    return {"id": row.id}


def _serialise(row, db: Session) -> Dict[str, Any]:
    org_name = None
    if row.org_id is not None:
        org = db.query(Organization).filter_by(id=row.org_id).one_or_none()
        org_name = org.name if org else None
    username = None
    if row.submitted_by is not None:
        submitter = db.query(User).filter_by(id=row.submitted_by).one_or_none()
        username = submitter.username if submitter else None
    return {
        "id": row.id,
        "kind": row.kind,
        "body": row.body,
        "status": row.status,
        "admin_note": row.admin_note,
        "org_name": org_name,
        "username": username,
        "source": "user" if row.submitted_by is not None else "visitor",
        "context": row.context,
        "created_at": iso_utc(row.created_at) if row.created_at else None,
    }


@router.get("/admin/feedback")
def admin_list_feedback(
    status: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    org_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail="Unknown status")
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=422, detail="Unknown kind")
    rows = list_feedback(db, status=status, kind=kind, org_id=org_id, limit=limit, offset=offset)
    return {"feedback": [_serialise(row, db) for row in rows]}


@router.patch("/admin/feedback/{feedback_id}")
def admin_patch_feedback(
    feedback_id: int,
    payload: FeedbackPatch,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    row = get_feedback(db, feedback_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    update_feedback(db, row, status=payload.status, admin_note=payload.admin_note)
    db.commit()
    return {"ok": True}
