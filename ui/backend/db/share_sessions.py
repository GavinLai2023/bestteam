"""CRUD for `ShareSession` -- one anonymous visitor's browser against one
ShareLink, plus its daily turn-rate CAS.

Mirrors `db/email_triggers.py`'s `runs_today`/`runs_date` shape, simplified:
no per-org lock is needed here (unlike the mailbox-side-effect stakes of the
email trigger) since a rare CAS-race overshoot of a few extra turns is a
minor cost concern, not a correctness one -- YAGNI per the design's approved
scope.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .models import ShareSession


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def create_share_session(db: Session, share_link_id: int) -> ShareSession:
    session = ShareSession(share_link_id=share_link_id, session_token=secrets.token_urlsafe(24))
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_share_session_by_token(db: Session, session_token: str) -> Optional[ShareSession]:
    return db.query(ShareSession).filter_by(session_token=session_token).one_or_none()


def list_share_sessions(db: Session, share_link_id: int) -> List[ShareSession]:
    return (
        db.query(ShareSession)
        .filter_by(share_link_id=share_link_id)
        .order_by(ShareSession.last_active_at.desc())
        .all()
    )


def try_consume_turn(db: Session, session: ShareSession, daily_cap: int) -> bool:
    """Atomically claim one turn against today's cap. Returns True if granted.

    Resets the counter first if the stored date has rolled over using a guarded
    UPDATE (no Python-level read-check-then-write race), then does a single
    conditional UPDATE so two near-simultaneous sends from the same session
    can't both slip through under the cap.
    """
    today = _today()
    db.execute(
        update(ShareSession)
        .where(
            ShareSession.id == session.id,
            or_(ShareSession.turns_date.is_(None), ShareSession.turns_date != today),
        )
        .values(turns_today=0, turns_date=today)
    )
    db.commit()
    db.refresh(session)
    advanced = db.execute(
        update(ShareSession)
        .where(ShareSession.id == session.id, ShareSession.turns_today < daily_cap)
        # Naive UTC, matching what every other writer here round-trips
        # through SQLite (final whole-branch review M23).
        .values(
            turns_today=ShareSession.turns_today + 1,
            last_active_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    ).rowcount
    db.commit()
    db.refresh(session)
    return bool(advanced)
