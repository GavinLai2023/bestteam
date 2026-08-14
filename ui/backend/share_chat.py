"""Public, anonymous chat surface for a ShareLink -- no login (see
docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md).

Visitor identity is a signed session cookie (`share_auth.py`), never a
`users` row. Every route re-validates the link's active/expiry/org-active
state fresh from the DB (no push-invalidation needed -- mirrors the run-
stream WebSocket's own re-authorize-per-event philosophy, see
`share_stream_api.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import Organization, Run, ShareLink, ShareSession, WorkflowRecord
from .db.share_links import get_share_link_by_token
from .db.share_messages import append_message, list_messages, next_turn_number
from .db.share_sessions import create_share_session, get_share_session_by_token, try_consume_turn
from .db_session import get_db
from .runtime import _executor, registry, run_in_background
from .share_auth import COOKIE_NAME, sign_session_token, verify_cookie_value

router = APIRouter(prefix="/api/share", tags=["share-chat"])

MAX_MESSAGE_LENGTH = 4000
MAX_HISTORY_TURNS = 20
_UNAVAILABLE = "This share link is no longer available."
_PENDING_TURN_MESSAGE = "Please wait for the previous reply to finish."


class ShareMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


def _is_expired(link: ShareLink) -> bool:
    if link.expires_at is None:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return link.expires_at.replace(tzinfo=None) < now


def _resolve_active_link(db: Session, token: str) -> ShareLink:
    link = get_share_link_by_token(db, token)
    if link is None or not link.active or _is_expired(link):
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)
    org = db.get(Organization, link.org_id)
    if org is None or not org.active:
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)
    return link


def _resolve_session_from_cookie(request: Request, db: Session, link: ShareLink) -> Optional[ShareSession]:
    cookie_value = request.cookies.get(COOKIE_NAME)
    session_token = verify_cookie_value(cookie_value) if cookie_value else None
    if session_token is None:
        return None
    session = get_share_session_by_token(db, session_token)
    if session is None or session.share_link_id != link.id:
        return None
    return session


def _get_or_create_session(request: Request, response: Response, db: Session, link: ShareLink) -> ShareSession:
    session = _resolve_session_from_cookie(request, db, link)
    if session is not None:
        return session
    session = create_share_session(db, link.id)
    response.set_cookie(
        COOKIE_NAME,
        sign_session_token(session.session_token),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
    )
    return session


def _has_pending_turn(db: Session, session: ShareSession) -> bool:
    """True if the session's last message is an unanswered user turn --
    either a run still in flight, or one whose terminal event never made it
    to record_share_reply (should not happen, but this still blocks sending
    into an inconsistent state rather than silently overwriting it)."""
    messages = list_messages(db, session.id)
    return bool(messages) and messages[-1].role == "user"


@router.post("/{token}/messages", status_code=202)
def send_share_message(
    token: str,
    body: ShareMessageCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    link = _resolve_active_link(db, token)
    session = _get_or_create_session(request, response, db, link)

    if _has_pending_turn(db, session):
        raise HTTPException(status_code=409, detail=_PENDING_TURN_MESSAGE)

    # Resolved before try_consume_turn (below) deliberately: it's a
    # read-only lookup with no side effects, so checking the team is
    # actually usable first means a 404 here never burns a daily-cap turn
    # for a message that was never persisted and no run that was ever
    # created (controller-ruled fix, Task 8 review finding 1a).
    workflow_record = (
        db.query(WorkflowRecord)
        .filter_by(id=link.workflow_id, org_id=link.org_id, status="deployed")
        .one_or_none()
    )
    if workflow_record is None:
        # Same detail text as _resolve_active_link's failures -- a
        # distinguishable message here would let a prober tell "real,
        # active link whose team just isn't deployed" apart from
        # "fake/revoked/expired/org-deactivated", breaking the project's
        # single-404-message convention for invalid/unusable access
        # (controller-ruled fix, Task 8 review finding 2).
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)

    if not try_consume_turn(db, session, link.daily_cap):
        raise HTTPException(
            status_code=429, detail="Today's message limit has been reached -- try again tomorrow."
        )

    from .main import _resolve_workflow_and_version  # local import: main.py imports this router

    workflow, version_id, workflow_id = _resolve_workflow_and_version(
        workflow_record.name, db, link.org_id
    )

    turn_number = next_turn_number(db, session.id)
    try:
        append_message(db, session.id, turn_number=turn_number, role="user", content=body.content)
    except IntegrityError:
        # A UniqueConstraint(share_session_id, turn_number) collision means
        # another near-simultaneous request already won the race for this
        # turn (e.g. a visitor double-clicking Send) -- same user-facing
        # meaning as the coarse _has_pending_turn guard above, just catching
        # the narrower race between next_turn_number's read and this insert
        # that guard doesn't fully close.
        db.rollback()
        # Refund the cap turn try_consume_turn already claimed above: the
        # losing request's message was never persisted and no run was ever
        # created for it, so it must not cost the visitor a turn against
        # their daily cap (controller-ruled fix, Task 8 review finding 1b).
        db.execute(
            update(ShareSession)
            .where(ShareSession.id == session.id, ShareSession.turns_today > 0)
            .values(turns_today=ShareSession.turns_today - 1)
        )
        db.commit()
        raise HTTPException(status_code=409, detail=_PENDING_TURN_MESSAGE)

    history = list_messages(db, session.id)[-(MAX_HISTORY_TURNS * 2):]
    transcript = "\n".join(
        f"{'User' if m.role == 'user' else 'Team'}: {m.content}" for m in history
    )

    run = registry.create(workflow_record.name, transcript, org_id=link.org_id, username="share-link")
    db.add(
        Run(
            id=run.id,
            workflow=workflow_record.name,
            input=transcript,
            org_id=link.org_id,
            username="share-link",
            workflow_version_id=version_id,
            trigger_context={
                "share_link_id": link.id,
                "share_session_id": session.id,
                "turn_number": turn_number,
            },
        )
    )
    db.commit()

    _executor.submit(
        run_in_background,
        run.id,
        workflow,
        transcript,
        engine=db.get_bind(),
        org_id=link.org_id,
        username="share-link",
        workflow_version_id=version_id,
        workflow_id=workflow_id,
    )
    return {"run_id": run.id, "turn_number": turn_number}


@router.get("/{token}/messages")
def get_share_messages(token: str, request: Request, db: Session = Depends(get_db)) -> dict:
    link = _resolve_active_link(db, token)
    session = _resolve_session_from_cookie(request, db, link)
    if session is None:
        return {"messages": []}
    return {
        "messages": [
            {"role": m.role, "content": m.content, "turn_number": m.turn_number}
            for m in list_messages(db, session.id)
        ]
    }
