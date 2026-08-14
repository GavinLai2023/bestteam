"""Appends a share-chat run's assistant reply to `share_messages` once it
reaches a terminal state -- called from `runtime.py::run_in_background` on
every terminal path a run with `trigger_context["share_session_id"]` can
take, mirroring `automation_results.py::normalize_run_result`'s placement
for the email-trigger vertical.

No-op for any run without that key (a regular manual run, or an
email-triggered run's `trigger_context`, which has different keys) -- this
never touches an unrelated execution.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .db.models import Run
from .db.share_messages import append_message

_FALLBACK_REPLY = "Sorry, something went wrong producing a reply."


def record_share_reply(db: Optional[Session], run_row: Run, output: Optional[str]) -> None:
    """`output` is the real final text for a completed run, or `None`/a
    friendly string for a failed/cancelled/crashed one. This must be called
    on every terminal path so a share session's "last message is still
    unanswered" guard (`share_chat.py::_has_pending_turn`) never wedges a
    visitor's chat shut after a failure.
    """
    if db is None or run_row.trigger_context is None:
        return
    share_session_id = run_row.trigger_context.get("share_session_id")
    turn_number = run_row.trigger_context.get("turn_number")
    if share_session_id is None or turn_number is None:
        return
    append_message(
        db,
        share_session_id,
        turn_number=turn_number + 1,
        role="assistant",
        content=output or _FALLBACK_REPLY,
        run_id=run_row.id,
    )
