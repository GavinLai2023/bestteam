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

from .db.models import Organization, Run, ShareLink, ShareSession, PipelineRecord
from .db.feedback import count_session_feedback_today, create_feedback
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
# MAX_HISTORY_TURNS alone bounds message COUNT, not size: at MAX_MESSAGE_LENGTH
# per message, 40 messages could reach ~160,000 characters -- enough to exceed
# a smaller model's context window or spend unexpectedly high per-turn cost
# (Codex review finding; the design's own "Approach" section calls for
# bounding replay by "the most recent N turns / a character cap", but only
# the turn cap was implemented). Chosen to comfortably fit even a modest
# context window alongside the system prompt, the new question, and room for
# a reply.
MAX_HISTORY_CHARS = 24000
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


def _trim_history_to_budget(messages, max_chars: int):
    """Keep the most recent messages whose combined content fits within
    `max_chars`, dropping the oldest first -- preserves the newest user
    message and as many complete recent exchanges as fit. Always keeps at
    least the single most recent message, even if it alone exceeds the
    budget, so a reply is never sent with an empty transcript."""
    kept = []
    total = 0
    for message in reversed(messages):
        length = len(message.content)
        if kept and total + length > max_chars:
            break
        kept.append(message)
        total += length
    kept.reverse()
    return kept


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


def _refund_link_turn(db: Session, link: ShareLink) -> None:
    """Undo one already-claimed link-wide turn and commit immediately (the
    request's `db` session has no auto-commit-on-return, so an uncommitted
    `UPDATE` here would just roll back with the rest of the transaction) --
    used when a later check in the same request rejects the send anyway, so
    a blocked send never costs the link's aggregate daily allowance."""
    db.execute(
        update(ShareLink)
        .where(ShareLink.id == link.id, ShareLink.turns_today > 0)
        .values(turns_today=ShareLink.turns_today - 1)
    )
    db.commit()


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
    # Order matters (final whole-branch review C1/M12; Codex review finding
    # on the link-cap check specifically). Every pure read that can reject
    # the request, AND the link-wide aggregate cap check, run BEFORE
    # _get_or_create_session, which both INSERTs a share_sessions row and
    # sets a cookie: an undeployed team's link, or one already at its daily
    # cap, hammered in a loop used to grow share_sessions (and, via
    # `_session_lock`, process memory) unboundedly with orphaned rows/locks.
    # The per-session cap is then consumed after the session exists but
    # before anything is persisted for this turn, so a rejected request never
    # leaves a message or a run behind.
    link = _resolve_active_link(db, token)

    # Resolved before either try_consume_* call deliberately: it's a
    # read-only lookup with no side effects, so checking the team is
    # actually usable first means a 404 here never burns a daily-cap turn
    # for a message that was never persisted and no run that was ever
    # created (controller-ruled fix, Task 8 review finding 1a).
    pipeline_record = (
        db.query(PipelineRecord)
        .filter_by(id=link.pipeline_id, org_id=link.org_id, status="deployed")
        .one_or_none()
    )
    if pipeline_record is None:
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
    from .main import _resolve_pipeline_and_version  # local import: main.py imports this router

    pipeline, version_id, pipeline_id = _resolve_pipeline_and_version(
        pipeline_record.name, db, link.org_id
    )

    # Link-level aggregate cap claimed BEFORE any session is created (a new
    # session also means a new, never-evicted lock in `_session_locks`) --
    # otherwise a cookie-less client hammering an already-exhausted link
    # grows both the database and process memory for free, forever (Codex
    # review finding). Rejections further down in this same request that
    # would otherwise leave a claimed-but-unused turn (an existing session
    # with a pending turn already in flight, or its own per-session cap)
    # refund it via `_refund_link_turn`, so an already-blocked send still
    # never actually costs the link a turn -- matching the cap-neutral
    # behavior every other rejection path here already has.
    if not try_consume_link_turn(db, link, link.daily_cap):
        raise HTTPException(status_code=429, detail=_RATE_LIMITED_MESSAGE)

    session = _get_or_create_session(request, response, db, link)

    # Serializes pending-turn-check through the user-message insert for this
    # one session -- mirrors email_trigger.py's per-org `_dispatch_lock` for
    # the identical class of race (see `_session_lock`'s own docstring for
    # exactly what it closes; Codex review finding).
    with _session_lock(session.id):
        if _has_pending_turn(db, session):
            _refund_link_turn(db, link)
            raise HTTPException(status_code=409, detail=_PENDING_TURN_MESSAGE)

        if not try_consume_turn(db, session, link.daily_cap):
            _refund_link_turn(db, link)
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
    history = _trim_history_to_budget(history, MAX_HISTORY_CHARS)
    transcript = format_transcript(history)

    run = registry.create(pipeline_record.name, transcript, org_id=link.org_id, username="share-link")
    run_row = Run(
        id=run.id,
        pipeline=pipeline_record.name,
        input=transcript,
        org_id=link.org_id,
        username="share-link",
        pipeline_version_id=version_id,
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
            pipeline,
            transcript,
            engine=db.get_bind(),
            org_id=link.org_id,
            username="share-link",
            pipeline_version_id=version_id,
            pipeline_id=pipeline_id,
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


@router.get("/{token}/team")
def get_share_team(token: str, db: Session = Depends(get_db)) -> dict:
    """The team's name, and how many steps a visitor will see it take.

    Deliberately a pure read: a first-time visitor must be able to render the
    page header before sending anything, so this neither requires nor creates
    a session cookie.

    `steps` is the number of `agent_completed` events the visitor will
    observe. A HIERARCHICAL team emits exactly one however many subordinates
    its manager delegates to (subordinates emit `subagent_completed`, which
    `visitor_safe_event` renders indistinguishable), so no honest denominator
    exists and this is None -- the page shows a pulse instead of a count.

    What is NOT disclosed: agent names, roles, models, or the collaboration
    mode itself -- only its consequence. The org member generating a link is
    deliberately telling a colleague "talk to this team", so the team's name
    is shared; everything else stays behind `visitor_safe_event`.
    """
    link = _resolve_active_link(db, token)
    pipeline_record = (
        db.query(PipelineRecord)
        .filter_by(id=link.pipeline_id, org_id=link.org_id, status="deployed")
        .one_or_none()
    )
    if pipeline_record is None:
        # Same detail as every other failure here -- see send_share_message.
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)

    return {
        "name": _display_name(pipeline_record),
        "steps": _visible_step_count(pipeline_record.config),
    }


def _display_name(pipeline_record: PipelineRecord) -> str:
    """The team's customer-facing name.

    `PipelineRecord.name` is the technical identifier the YAML, the API and
    the admin surfaces use (`contract_review_v2`); a builder-created team also
    carries a friendly `teams[0].display_name`, which is what every other
    customer-facing surface shows (`main.py::_team_display_name`). A visitor
    on a shared link is the most customer-facing audience there is, so it must
    not be the one place an internal identifier leaks out (Codex review
    finding). Falls back to the technical name when there is no display name,
    exactly as those surfaces do.
    """
    config = pipeline_record.config
    if isinstance(config, dict):
        teams = config.get("teams") or []
        if teams and isinstance(teams[0], dict):
            display_name = teams[0].get("display_name")
            if display_name:
                return str(display_name)
    return pipeline_record.name


def _visible_step_count(config: Optional[dict]) -> Optional[int]:
    """How many `agent_completed` events a visitor will see, from the stored
    spec alone.

    Deliberately reads `PipelineRecord.config` rather than building the
    pipeline: `_resolve_pipeline_and_version`'s cache-miss path loads every
    skill, knowledge base and email tool, and a path-constructed vector
    knowledge base embeds at load time -- so building here would let an
    anonymous, uncapped GET incur build latency and real embedding spend
    before the visitor has sent a single capped message (Codex review
    finding).

    Counts the teams the pipeline actually steps through, in order, not every
    team defined in the spec -- a team can be declared and never used. None
    when any of them is HIERARCHICAL: that manager emits one completion
    however many subordinates it delegates to, so no honest denominator
    exists and the page shows a pulse instead. None too for a spec this
    cannot read, for the same reason -- a wrong count is worse than none.
    """
    if not isinstance(config, dict):
        return None
    teams_by_name = {
        team.get("name"): team for team in config.get("teams") or [] if isinstance(team, dict)
    }
    step_names = (config.get("pipeline") or {}).get("steps") or []
    total = 0
    for name in step_names:
        team = teams_by_name.get(name)
        if team is None:
            return None
        if str(team.get("mode") or "sequential").lower() == "hierarchical":
            return None
        total += len(team.get("agents") or [])
    return total or None


@router.post("/{token}/runs/{run_id}/cancel", status_code=202)
def cancel_share_run(token: str, run_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Let a visitor stop a turn they no longer want.

    Authorised exactly like the stream WebSocket below: the signed session
    cookie must resolve to a session on this link, and that session must own
    the run. Any failure is the standard 404, preserving the single-message
    convention that stops a prober telling "not your run" apart from
    "revoked link".

    The turn is NOT refunded against either daily cap: the tokens were spent,
    and a free retry after a stop would hand an abusive visitor unlimited
    work against the org's budget. `registry.request_cancel` already no-ops
    for an unknown or already-terminal run, so a Stop racing the reply is
    harmless.
    """
    link = _resolve_active_link(db, token)
    session = _resolve_session_from_cookie(request, db, link)
    run_row = db.get(Run, run_id)
    owns_run = (
        session is not None
        and run_row is not None
        and run_row.trigger_context is not None
        and run_row.trigger_context.get("share_session_id") == session.id
    )
    if not owns_run:
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)
    return {"cancelled": registry.request_cancel(run_id)}


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

    The raw event (`dataclasses.asdict(TraceEvent)`: `type`, `pipeline`,
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
    # `reply_delta` (see runtime.py's `_TokenSink`) carries text by exactly the
    # argument that already admits `run_completed.data`: it is the final
    # agent's own reply, which the visitor is about to be given in full. Only
    # one node in the graph is ever wired to stream (the `streams` flag in
    # adapters/langgraph_adapter.py), so no other agent's text can reach this
    # event. `reply_reset` carries nothing at all.
    carries_text = event_type in ("run_completed", "reply_delta")
    return {
        "type": event_type,
        "pipeline": None,
        "agent": None,
        "data": event.get("data") if carries_text else None,
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


# --- visitor feedback --------------------------------------------------------

# Local import target, not a circular one: feedback_api imports nothing from
# this module.
from .feedback_api import FeedbackCreate, sanitize_context  # noqa: E402

FEEDBACK_DAILY_CAP = 5
_SHARE_CONTEXT_KEYS = frozenset({"page", "locale", "run_id"})
_NO_SESSION_MESSAGE = "Open the chat before sending feedback"


@router.post("/{token}/feedback", status_code=201)
def submit_share_feedback(
    token: str,
    payload: FeedbackCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Visitor feedback (defect/suggestion), routed to the platform operator.

    Deliberately does NOT mint a session (unlike a message send): a visitor
    with no cookie has never opened the chat, and feedback alone must not
    grow share_sessions unboundedly -- 403 instead. The cap is a plain
    count-per-UTC-day (see db/feedback.py) because nothing is billed per
    submission.
    """
    link = _resolve_active_link(db, token)
    session = _resolve_session_from_cookie(request, db, link)
    if session is None:
        raise HTTPException(status_code=403, detail=_NO_SESSION_MESSAGE)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Feedback body is empty")
    if count_session_feedback_today(db, session.id) >= FEEDBACK_DAILY_CAP:
        raise HTTPException(status_code=429, detail=_RATE_LIMITED_MESSAGE)
    context = sanitize_context(payload.context, _SHARE_CONTEXT_KEYS) or {}
    context["share_link_id"] = link.id
    row = create_feedback(
        db,
        kind=payload.kind,
        body=body,
        org_id=link.org_id,
        share_session_id=session.id,
        context=context,
    )
    db.commit()
    return {"id": row.id}
