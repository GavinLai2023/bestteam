"""Org-scoped management of ShareLinks -- lets the org's one user share a
deployed team with colleagues without giving them a real account (see
docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md).

Every route here requires `get_current_org` (a logged-in org member). A
colleague using the resulting link never authenticates this way -- see
`share_chat.py` for the separate, anonymous cookie-auth surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .auth_api import get_current_org, get_current_user
from .db.models import Organization, ShareLink, ShareSession, User, PipelineRecord, iso_utc
from .db.share_links import create_share_link, get_share_link, list_share_links, patch_share_link
from .db.share_messages import list_messages
from .db.share_sessions import list_share_sessions
from .db_session import get_db

router = APIRouter(prefix="/api", tags=["share-links"])


def _to_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize an offset-aware `expires_at` to naive UTC before it reaches
    the DB. `share_links.expires_at` is a naive column and `share_chat.py`'s
    `_is_expired` compares against naive UTC, so an offset-aware input used to
    have its offset silently dropped -- a link created with `+08:00` then
    expired up to 8 hours off (final whole-branch review M13/M23)."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class ShareLinkCreate(BaseModel):
    daily_cap: int = Field(default=30, ge=1, le=1000)
    expires_at: Optional[datetime] = None

    @field_validator("expires_at")
    @classmethod
    def _normalize_expires_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _to_naive_utc(value)


class ShareLinkPatch(BaseModel):
    active: Optional[bool] = None
    daily_cap: Optional[int] = Field(default=None, ge=1, le=1000)
    expires_at: Optional[datetime] = None
    clear_expiry: bool = False

    @field_validator("expires_at")
    @classmethod
    def _normalize_expires_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _to_naive_utc(value)


def _share_link_dict(link: ShareLink) -> dict:
    return {
        "id": link.id,
        "pipeline_id": link.pipeline_id,
        "token": link.token,
        "active": link.active,
        "daily_cap": link.daily_cap,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "created_at": link.created_at.isoformat(),
    }


def _share_session_dict(session: ShareSession) -> dict:
    return {
        "id": session.id,
        # SQLite round-trips these as tzinfo-naive datetimes -- plain
        # `.isoformat()` then omits the UTC marker, and `SharedSessionsPanel`
        # passes `last_active_at` straight into `new Date(...)`, which
        # interprets an offset-less string as browser-LOCAL time, shifting
        # activity times for any visitor outside UTC (Codex review finding).
        "created_at": iso_utc(session.created_at),
        "last_active_at": iso_utc(session.last_active_at),
        "turns_today": session.turns_today,
    }


def _get_deployed_pipeline_or_404(db: Session, pipeline_id: int, org_id: int) -> PipelineRecord:
    record = (
        db.query(PipelineRecord)
        .filter_by(id=pipeline_id, org_id=org_id, status="deployed")
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown deployed team '{pipeline_id}'")
    return record


@router.post("/pipelines/{pipeline_id}/share-links", status_code=201)
def create_share_link_endpoint(
    pipeline_id: int,
    body: ShareLinkCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> dict:
    _get_deployed_pipeline_or_404(db, pipeline_id, org.id)
    link = create_share_link(
        db,
        pipeline_id=pipeline_id,
        org_id=org.id,
        created_by=user.id,
        daily_cap=body.daily_cap,
        expires_at=body.expires_at,
    )
    return _share_link_dict(link)


@router.get("/pipelines/{pipeline_id}/share-links")
def list_share_links_endpoint(
    pipeline_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> List[dict]:
    _get_deployed_pipeline_or_404(db, pipeline_id, org.id)
    return [_share_link_dict(link) for link in list_share_links(db, pipeline_id, org.id)]


@router.patch("/share-links/{link_id}")
def patch_share_link_endpoint(
    link_id: int,
    body: ShareLinkPatch,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> dict:
    link = get_share_link(db, link_id, org.id)
    if link is None:
        raise HTTPException(status_code=404, detail=f"Unknown share link '{link_id}'")
    link = patch_share_link(
        db, link,
        active=body.active, daily_cap=body.daily_cap,
        expires_at=body.expires_at, clear_expiry=body.clear_expiry,
    )
    return _share_link_dict(link)


@router.get("/share-links/{link_id}/sessions")
def list_share_sessions_endpoint(
    link_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> List[dict]:
    link = get_share_link(db, link_id, org.id)
    if link is None:
        raise HTTPException(status_code=404, detail=f"Unknown share link '{link_id}'")
    return [_share_session_dict(s) for s in list_share_sessions(db, link_id)]


@router.get("/share-links/{link_id}/sessions/{session_id}/messages")
def get_share_session_messages_endpoint(
    link_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> List[dict]:
    link = get_share_link(db, link_id, org.id)
    if link is None:
        raise HTTPException(status_code=404, detail=f"Unknown share link '{link_id}'")
    session = db.query(ShareSession).filter_by(id=session_id, share_link_id=link_id).one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session '{session_id}'")
    return [
        {
            "role": m.role,
            "content": m.content,
            "turn_number": m.turn_number,
            "created_at": m.created_at.isoformat(),
        }
        for m in list_messages(db, session_id)
    ]
