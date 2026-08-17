"""Org-scoped notification list (`/api/notifications`).

Read and mark-as-read only. Notifications are raised by the system, never by a
customer, so there is no create or delete verb here: a customer deleting the
record of a fault they never fixed would defeat the point.

Every route is guarded by `get_current_org`, so an org sees only its own.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .auth_api import get_current_org
from .db.models import Organization, iso_utc
from .db.notifications import list_notifications, mark_read, unread_count
from .db_session import get_db

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_MAX_LIMIT = 200


def _serialise(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "severity": row.severity,
        "title": row.title,
        "body": row.body,
        "fingerprint": row.fingerprint,
        # `iso_utc`, not bare `isoformat()`: SQLite hands the column back
        # tzinfo-naive, and the frontend's `formatDateTime` then reads a bare
        # timestamp as browser-local, shifting every alert time.
        "created_at": iso_utc(row.created_at) if row.created_at else None,
        "read": row.read_at is not None,
        # Exposed so an admin can tell "nobody was paged" from "nothing
        # happened" -- a webhook that has quietly been failing looks exactly
        # like a quiet system otherwise.
        "delivery_state": row.delivery_state,
    }


@router.get("")
def list_route(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    rows = list_notifications(db, org.id, unread_only=unread_only, limit=limit)
    return {
        "notifications": [_serialise(row) for row in rows],
        "unread": unread_count(db, org.id),
    }


@router.post("/{notification_id}/read")
def mark_read_route(
    notification_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    # 404 rather than 403 for another org's row: the two are indistinguishable
    # to a caller, so cross-org probing learns nothing.
    if not mark_read(db, org.id, notification_id):
        raise HTTPException(status_code=404, detail="No such notification.")
    db.commit()
    return {"ok": True, "unread": unread_count(db, org.id)}
