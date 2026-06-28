"""CRUD for `BuilderSession` -- the wizard's session state machine (Phase 1).

The six methodology stages (docs/team_builder_methodology.md) map onto
`BuilderSession.status`. Phase 2's builder-session API endpoints are thin
wrappers over these functions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import BuilderSession

# intent -> requirements -> spec -> solution -> testing -> deployed.
# Earlier stages are reachable again via feedback (see append_feedback).
STATUSES = ("intent", "requirements", "spec", "solution", "testing", "deployed")

# Fields a caller may update directly via update_session().
_UPDATABLE_FIELDS = frozenset(
    {
        "intent_text",
        "as_is_text",
        "requirements_json",
        "specification_json",
        "status",
    }
)


def create_session(db: Session, *, intent_text: str = "", as_is_text: str = "") -> BuilderSession:
    """Start a new wizard session in the 'intent' stage."""
    session = BuilderSession(
        id=uuid.uuid4().hex[:12],
        intent_text=intent_text,
        as_is_text=as_is_text,
        status="intent",
        feedback_history=[],
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_session(db: Session, session_id: str) -> Optional[BuilderSession]:
    return db.get(BuilderSession, session_id)


def list_sessions(db: Session, *, limit: int = 50) -> list[BuilderSession]:
    """All builder sessions, most-recently-updated first, for an "AI teams
    I've built" list. No pagination -- per-deployment scale is small (see
    ui/backend/CLAUDE.md's auth section)."""
    return db.query(BuilderSession).order_by(BuilderSession.updated_at.desc()).limit(limit).all()


def update_session(db: Session, session_id: str, **fields: Any) -> BuilderSession:
    """Update one or more fields of a builder session.

    Only `_UPDATABLE_FIELDS` may be set this way; `feedback_history` is
    append-only via `append_feedback`. Raises `LookupError` for an unknown
    session id, `ValueError` for an unknown field or invalid `status`.
    """
    session = get_session(db, session_id)
    if session is None:
        raise LookupError(f"Unknown builder session '{session_id}'")

    for key, value in fields.items():
        if key not in _UPDATABLE_FIELDS:
            raise ValueError(f"BuilderSession field '{key}' cannot be set via update_session")
        if key == "status" and value not in STATUSES:
            raise ValueError(f"Invalid builder session status '{value}'. Valid statuses: {', '.join(STATUSES)}")
        setattr(session, key, value)

    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)
    return session


def append_feedback(db: Session, session_id: str, feedback: dict[str, Any]) -> BuilderSession:
    """Record one round of customer feedback (e.g. a request to redesign a stage)."""
    session = get_session(db, session_id)
    if session is None:
        raise LookupError(f"Unknown builder session '{session_id}'")

    entry = {**feedback, "at": datetime.now(timezone.utc).isoformat()}
    session.feedback_history = [*session.feedback_history, entry]
    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)
    return session
