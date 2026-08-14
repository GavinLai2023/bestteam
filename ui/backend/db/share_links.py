"""CRUD for `ShareLink` -- an anonymous, revocable entry point to one
deployed team. Mirrors the shape of `db/email_triggers.py`: small helpers
over one table, no business logic beyond straightforward reads/writes.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from .models import ShareLink


def create_share_link(
    db: Session,
    *,
    workflow_id: int,
    org_id: int,
    created_by: int,
    daily_cap: int = 30,
    expires_at: Optional[datetime] = None,
) -> ShareLink:
    link = ShareLink(
        workflow_id=workflow_id,
        org_id=org_id,
        created_by=created_by,
        token=secrets.token_urlsafe(32),
        daily_cap=daily_cap,
        expires_at=expires_at,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def get_share_link_by_token(db: Session, token: str) -> Optional[ShareLink]:
    return db.query(ShareLink).filter_by(token=token).one_or_none()


def get_share_link(db: Session, link_id: int, org_id: int) -> Optional[ShareLink]:
    """Org-scoped lookup -- another org's link id returns None (404 upstream),
    never revealing whether the id exists at all."""
    return db.query(ShareLink).filter_by(id=link_id, org_id=org_id).one_or_none()


def list_share_links(db: Session, workflow_id: int, org_id: int) -> List[ShareLink]:
    return (
        db.query(ShareLink)
        .filter_by(workflow_id=workflow_id, org_id=org_id)
        .order_by(ShareLink.created_at.desc())
        .all()
    )


def patch_share_link(
    db: Session,
    link: ShareLink,
    *,
    active: Optional[bool] = None,
    daily_cap: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    clear_expiry: bool = False,
) -> ShareLink:
    if active is not None:
        link.active = active
    if daily_cap is not None:
        link.daily_cap = daily_cap
    if clear_expiry:
        link.expires_at = None
    elif expires_at is not None:
        link.expires_at = expires_at
    db.commit()
    db.refresh(link)
    return link


def count_active_share_links(db: Session, workflow_id: int) -> int:
    """Used by the workflow-delete guard (crud.py) -- an active link blocks
    deletion of the team it points at."""
    return db.query(ShareLink).filter_by(workflow_id=workflow_id, active=True).count()
