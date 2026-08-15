"""Public, anonymous chat surface for a ShareLink -- no login (see
docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md).

Visitor identity is a signed session cookie (`share_auth.py`), never a
`users` row. Every route re-validates the link's active/expiry/org-active
state fresh from the DB (no push-invalidation needed -- mirrors the run-
stream WebSocket's own re-authorize-per-event philosophy, see
`share_stream_api.py`).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import Organization, Run, ShareLink, ShareSession, WorkflowRecord
from .db.share_links import get_share_link_by_token, try_consume_link_turn
from .db.share_messages import append_message, list_messages, next_turn_number
from .db.share_sessions import create_share_session, get_share_session_by_token, try_consume_turn
from .db_session import get_db
from .runtime import _executor, registry, run_in_background
from .share_auth import COOKIE_NAME, sign_session_token, verify_cookie_value
from .share_transcript import record_share_reply

router = APIRouter(prefix="/api/share", tags=["share-chat"])

MAX_MESSAGE_LENGTH = 4000
MAX_HISTORY_TURNS = 20
_UNAVAILABLE = "This share link is no longer available."
_PENDING_TURN_MESSAGE = "Please wait for the previous reply to finish."
_RATE_LIMITED_MESSAGE = "Today's message limit has been reached -- try again tomorrow."
_DISPATCH_FAILED_MESSAGE = "Couldn't start a reply just now. Please try sending your message again."

_logger = logging.getLogger(__name__)

# Per-session lock serializing the pending-turn-check through the user-
# message insert -- mirrors email_trigger.py's per-org `_dispatch_lock` for
# the identical class of race. Without it, two near-simultaneous sends for
# the same session can both pass `_has_pending_turn` before either's insert
# commits; the second's turn number (computed by `next_turn_number` after
# the first's insert has already landed) then does NOT collide with the
# first's, so it lands cleanly on what becomes the first run's reply slot
# once `record_share_reply` runs -- which silently no-ops there (its
# idempotency check only looks at whether *a* row occupies that slot, not
# its role), dropping the first run's reply entirely (Codex review finding).
_session_locks: dict[int, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def _session_lock(session_id: int) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


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
    # `samesite="lax"` means the frontend and this API must be served under the
    # same site (same registrable domain -- ports don't matter) or the browser
    # never sends this cookie back, and the visitor silently gets a brand-new
    # session per message with no continuous chat at all. Our own dev defaults
    # honour that (`ui/frontend/src/lib/api.ts` points at `localhost:8000`
    # against Vite's `localhost:5173`). A genuinely cross-site deployment
    # (different domains) needs `samesite="none"` + `secure=True` + HTTPS
    # instead -- a deliberate future deployment-config decision, not something
    # to flip here (final whole-branch review C2).
    #
    # `path` is scoped to this one link's token, not the default `/`: every
    # share link uses the same cookie NAME, so an unscoped cookie set while
    # chatting through link B overwrites the credential for link A, and
    # returning to link A in the same browser then shows an empty
    # conversation and mints a yet another new session (Codex review
    # finding). Every route this cookie needs to reach --
    # POST/GET /api/share/{token}/messages and the WS at
    # /api/share/{token}/stream/{run_id} -- shares this same path prefix.
    response.set_cookie(
        COOKIE_NAME,
        sign_session_token(session.session_token),
        httponly=True,
        samesite="lax",
        path=f"/api/share/{link.token}",
        max_age=60 * 60 * 24 * 365,
    )
    return session


def format_transcript(messages) -> str:
    """Render a session's history as the single input string a run replays.

    Visitor content is raw, unescaped, newline-bearing text, so a plain
    `"User: ...\\nTeam: ..."` join is forgeable: a message containing
    `"\\nTeam: <made-up answer>\\nUser: "` produces a transcript with a
    fabricated prior assistant turn the model cannot tell from a real one --
    a prompt-injection primitive on a brand-new anonymous surface (final
    whole-branch review I3). Each turn is wrapped in an unambiguous tag and
    `<`/`>` inside the content are escaped, so no visitor text can ever
    close a tag or open one.
    """
    parts = []
    for message in messages:
        tag = "user" if message.role == "user" else "assistant"
        content = message.content.replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<{tag}>{content}</{tag}>")
    return "\n".join(parts)


def _cookie_headers(response: Response) -> dict:
    """Any `Set-Cookie` already staged on the injected response, as a header
    dict an `HTTPException` can carry (there is at most one here)."""
    return {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in response.raw_headers
        if key.decode("latin-1").lower() == "set-cookie"
    }


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
    # Order matters (final whole-branch review C1/M12). Every pure read that
    # can reject the request runs BEFORE _get_or_create_session, which both
    # INSERTs a share_sessions row and sets a cookie: an undeployed team's
    # link hammered in a loop used to grow share_sessions unboundedly with
    # orphaned rows. Both caps are then consumed after the session exists but
    # before anything is persisted for this turn, so a rejected request never
    # leaves a message or a run behind.
    link = _resolve_active_link(db, token)

    # Resolved before either try_consume_* call deliberately: it's a
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

    # Resolved (and, on a first call, built/compiled) before any session or
    # cap side effect. A malformed team config raises a plain 400 here --
    # doing this before _get_or_create_session/either try_consume_* call
    # means a broken config can never mint another session and burn another
    # link-wide daily-cap turn on every retry until the link goes dark for
    # the whole day despite never actually dispatching a run (Codex review
    # finding).
    from .main import _resolve_workflow_and_version  # local import: main.py imports this router

    workflow, version_id, workflow_id = _resolve_workflow_and_version(
        workflow_record.name, db, link.org_id
    )

    session = _get_or_create_session(request, response, db, link)

    # Serializes pending-turn-check through the user-message insert for this
    # one session -- mirrors email_trigger.py's per-org `_dispatch_lock` for
    # the identical class of race (see `_session_lock`'s own docstring for
    # exactly what it closes; Codex review finding).
    with _session_lock(session.id):
        if _has_pending_turn(db, session):
            raise HTTPException(status_code=409, detail=_PENDING_TURN_MESSAGE)

        # Link-level aggregate cap FIRST, then the per-session one -- the
        # per-session counter caps nobody on its own, since a client that
        # never stores the cookie gets a brand-new, free session (and
        # allowance) on every request. Both must pass for a turn to proceed.
        if not try_consume_link_turn(db, link, link.daily_cap):
            raise HTTPException(status_code=429, detail=_RATE_LIMITED_MESSAGE)

        if not try_consume_turn(db, session, link.daily_cap):
            raise HTTPException(status_code=429, detail=_RATE_LIMITED_MESSAGE)

        turn_number = next_turn_number(db, session.id)
        try:
            append_message(db, session.id, turn_number=turn_number, role="user", content=body.content)
        except IntegrityError:
            # A UniqueConstraint(share_session_id, turn_number) collision --
            # should no longer be reachable now that the lock above
            # serializes this whole section per session, but kept as
            # defense-in-depth. Same user-facing meaning as the
            # _has_pending_turn guard above.
            db.rollback()
            # Refund the cap turns both try_consume_* calls already claimed
            # above: the losing request's message was never persisted and no
            # run was ever created for it, so it must not cost the visitor a
            # turn against their daily cap (controller-ruled fix, Task 8
            # review finding 1b) -- nor the link a turn against its
            # aggregate one.
            db.execute(
                update(ShareSession)
                .where(ShareSession.id == session.id, ShareSession.turns_today > 0)
                .values(turns_today=ShareSession.turns_today - 1)
            )
            db.execute(
                update(ShareLink)
                .where(ShareLink.id == link.id, ShareLink.turns_today > 0)
                .values(turns_today=ShareLink.turns_today - 1)
            )
            db.commit()
            raise HTTPException(status_code=409, detail=_PENDING_TURN_MESSAGE)

    history = list_messages(db, session.id)[-(MAX_HISTORY_TURNS * 2):]
    transcript = format_transcript(history)

    run = registry.create(workflow_record.name, transcript, org_id=link.org_id, username="share-link")
    run_row = Run(
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
    db.add(run_row)
    db.commit()

    try:
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
    except Exception as exc:  # noqa: BLE001 -- submission must never wedge a visitor's chat
        # The user's message and the Run row are already committed, so if no
        # worker ever starts, `record_share_reply` never fires either: the
        # session's last message stays an unanswered user turn and
        # `_has_pending_turn` blocks that visitor from ever sending again
        # (final whole-branch review I6). Mirrors email_trigger.py's identical
        # submission-exception branch: mark the run failed, then record the
        # reply here (bypassing the executor) so the transcript is consistent
        # and the session is unblocked.
        _logger.exception("share chat: failed to dispatch run %s for link %s", run.id, link.id)
        run_row.status = "failed"
        run_row.output = _DISPATCH_FAILED_MESSAGE
        db.commit()
        record_share_reply(db, run_row, None)
        registry.discard(run.id)
        # Carry the session cookie onto the error response by hand: FastAPI
        # only merges the injected `Response`'s headers when the endpoint
        # returns normally, so a first-message failure would otherwise strand
        # the just-created session (and its recorded fallback reply) behind a
        # cookie the visitor never received.
        raise HTTPException(
            status_code=500, detail=_DISPATCH_FAILED_MESSAGE, headers=_cookie_headers(response)
        ) from exc
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


def _link_and_org_active(engine, link_id: int) -> bool:
    """Fresh re-check of link active/expiry/org-active state -- used both at
    WS connect and before delivering every event, so a revoke, expiry, or
    org deactivation (whether it already happened before connect or occurs
    mid-stream) stops delivery immediately (mirrors main.py::stream_run's
    own per-event `_stream_access` re-check)."""
    with Session(engine) as check_db:
        link = check_db.get(ShareLink, link_id)
        if link is None or not link.active or _is_expired(link):
            return False
        org = check_db.get(Organization, link.org_id)
        return org is not None and org.active


def visitor_safe_event(event: dict) -> dict:
    """Strip a trace event down to what an anonymous visitor may see.

    The raw event (`dataclasses.asdict(TraceEvent)`: `type`, `workflow`,
    `agent`, `data`, `usage`) carries the org's internals -- agent names, the
    team name, full intermediate agent output, tool names/summaries, and the
    model identities and token counts in `usage`. The design's visitor
    experience is a friendly, non-technical status line, and the frontend's
    `friendlyStatusFor` mapping is purely cosmetic: anyone can open devtools
    and read the real payloads. Only the event type crosses this boundary,
    plus `run_completed`'s `data` -- the final answer, which the chat page
    needs to render the reply. Same spirit as `runtime.py`'s
    `_PM_TRACE_REDACTED` redaction (final whole-branch review I4).
    """
    event_type = event.get("type")
    return {
        "type": event_type,
        "workflow": None,
        "agent": None,
        "data": event.get("data") if event_type == "run_completed" else None,
        "usage": [],
    }


@router.websocket("/{token}/stream/{run_id}")
async def stream_share_run(
    websocket: WebSocket, token: str, run_id: str, db: Session = Depends(get_db)
):
    """Replays a share-chat run's trace events for the visitor session that
    started it. Authenticated by the signed session cookie (`share_auth.py`)
    -- sent automatically on the WS handshake, so no ticket exchange is
    needed here (contrast `main.py::stream_run`'s `?ticket=`, which exists
    only to work around a WebSocket handshake not carrying an
    `Authorization` header)."""
    engine = db.get_bind()
    cookie_value = websocket.cookies.get(COOKIE_NAME)
    session_token = verify_cookie_value(cookie_value) if cookie_value else None
    link = get_share_link_by_token(db, token)
    session = get_share_session_by_token(db, session_token) if session_token else None
    run_row = db.get(Run, run_id)

    authorized = (
        link is not None
        and session is not None
        and session.share_link_id == link.id
        and run_row is not None
        and run_row.trigger_context is not None
        and run_row.trigger_context.get("share_session_id") == session.id
        and _link_and_org_active(engine, link.id)
    )
    run = registry.get(run_id)
    if not authorized or run is None:
        db.close()
        await websocket.close(code=4404)
        return
    link_id = link.id
    db.close()

    await websocket.accept()
    subscriber_queue = registry.subscribe(run_id)
    if subscriber_queue is None:
        await websocket.close(code=4404)
        return
    try:
        while True:
            event = await subscriber_queue.get()
            if not _link_and_org_active(engine, link_id):
                await websocket.close(code=4404)
                return
            await websocket.send_json(visitor_safe_event(event))
            if event["type"] in ("run_completed", "run_failed", "run_cancelled"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run_id, subscriber_queue)
