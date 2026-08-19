"""CRUD for `ShareLink` -- an anonymous, revocable entry point to one
deployed team. Mirrors the shape of `db/email_triggers.py`: small helpers
over one table, no business logic beyond straightforward reads/writes.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .models import ShareLink


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def create_share_link(
    db: Session,
    *,
    pipeline_id: int,
    org_id: int,
    created_by: int,
    daily_cap: int = 30,
    expires_at: Optional[datetime] = None,
) -> ShareLink:
    link = ShareLink(
        pipeline_id=pipeline_id,
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


def list_share_links(db: Session, pipeline_id: int, org_id: int) -> List[ShareLink]:
    return (
        db.query(ShareLink)
        .filter_by(pipeline_id=pipeline_id, org_id=org_id)
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


def try_consume_link_turn(db: Session, link: ShareLink, daily_cap: int) -> bool:
    """Atomically claim one turn against this LINK's aggregate daily cap.

    Same shape as `db/share_sessions.py::try_consume_turn` (reset-if-stale
    guarded by a WHERE clause, then one conditional UPDATE matched by
    rowcount), but counted per ShareLink rather than per ShareSession -- the
    per-session counter alone caps nobody, since a client that never stores
    the session cookie gets a brand-new, free session on every request
    (final whole-branch review C1). `daily_cap` is deliberately reused as
    both ceilings: no one session may exceed it, and neither may everyone
    on the link put together.
    """
    today = _today()
    db.execute(
        update(ShareLink)
        .where(
            ShareLink.id == link.id,
            or_(ShareLink.turns_date.is_(None), ShareLink.turns_date != today),
        )
        .values(turns_today=0, turns_date=today)
    )
    db.commit()
    db.refresh(link)
    advanced = db.execute(
        update(ShareLink)
        .where(ShareLink.id == link.id, ShareLink.turns_today < daily_cap)
        .values(turns_today=ShareLink.turns_today + 1)
    ).rowcount
    db.commit()
    db.refresh(link)
    return bool(advanced)


def count_active_share_links(db: Session, pipeline_id: int) -> int:
    """Used by the pipeline-delete guard (crud.py) -- an active link blocks
    deletion of the team it points at."""
    return db.query(ShareLink).filter_by(pipeline_id=pipeline_id, active=True).count()
