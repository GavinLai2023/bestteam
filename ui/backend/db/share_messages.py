"""CRUD for `ShareMessage` -- the human-readable transcript for a
ShareSession (see `ui/backend/share_chat.py` for how a turn is produced)."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .models import ShareMessage


def append_message(
    db: Session,
    share_session_id: int,
    *,
    turn_number: int,
    role: str,
    content: str,
    run_id: Optional[str] = None,
) -> ShareMessage:
    message = ShareMessage(
        share_session_id=share_session_id,
        turn_number=turn_number,
        role=role,
        content=content,
        run_id=run_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def list_messages(db: Session, share_session_id: int) -> List[ShareMessage]:
    return (
        db.query(ShareMessage)
        .filter_by(share_session_id=share_session_id)
        .order_by(ShareMessage.turn_number)
        .all()
    )


def next_turn_number(db: Session, share_session_id: int) -> int:
    last = (
        db.query(ShareMessage.turn_number)
        .filter_by(share_session_id=share_session_id)
        .order_by(ShareMessage.turn_number.desc())
        .first()
    )
    return (last[0] + 1) if last else 1
