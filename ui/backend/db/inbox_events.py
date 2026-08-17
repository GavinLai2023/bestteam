"""Durable per-message inbox ledger (email automation Phase 1).

Detection records a `pending` row per new message in the SAME transaction that
advances the mailbox cursor, so mail can never be consumed without a durable
record of the work. A run then claims rows; the claimed rows' `external_id`s
are the batch it processes.

None of these helpers commit. Callers own the transaction boundary -- that is
the entire point of the design, since the durability guarantee comes from
detection's insert and the cursor advance landing in one commit.

See docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import InboxEvent

EVENT_PENDING = "pending"
EVENT_CLAIMED = "claimed"
EVENT_DONE = "done"
EVENT_FAILED = "failed"

DEFAULT_CONNECTOR = "imap"

_IDENTITY_COLUMNS = [
    "org_id", "connector_type", "mailbox_identity",
    "mailbox_generation", "external_id",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mailbox_identity(host: str, username: str) -> str:
    """Stable identity for one mailbox, independent of the credential row id.

    `set_email_credentials` upserts one row per org, so the row id survives the
    customer replacing the mailbox entirely -- host/username are what actually
    change (the same reasoning `_start_triggered_run` already applies when it
    stamps `mailbox_host`/`mailbox_username` into `trigger_context`).
    """
    return f"{host}:{username}".lower()


def record_events(
    db: Session,
    *,
    org_id: int,
    mailbox_identity: str,
    mailbox_generation: str,
    external_ids: Sequence,
    connector_type: str = DEFAULT_CONNECTOR,
) -> int:
    """Record each id as a `pending` event, ignoring ones already known.

    Idempotent by the table's unique key, which is what lets the mailbox cursor
    degrade from a correctness requirement to a performance optimisation:
    losing it causes messages to be re-examined and skipped, never processed
    twice.

    `on_conflict_do_nothing` is SQLite-specific -- one of the places a future
    Postgres migration would touch (that dialect offers the same call).
    """
    if not external_ids:
        return 0
    now = _utcnow()
    rows = [
        {
            "org_id": org_id,
            "connector_type": connector_type,
            "mailbox_identity": mailbox_identity,
            "mailbox_generation": mailbox_generation,
            "external_id": str(external_id),
            "status": EVENT_PENDING,
            "attempts": 0,
            "detected_at": now,
        }
        for external_id in external_ids
    ]
    result = db.execute(
        sqlite_insert(InboxEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=_IDENTITY_COLUMNS)
    )
    return result.rowcount or 0


def claim_events(db: Session, *, org_id: int, run_id: str, limit: int) -> List[InboxEvent]:
    """Atomically claim up to `limit` of this org's oldest pending events.

    One UPDATE, so under SQLite's write lock two claimants cannot be handed the
    same message. (That removes one class of cross-process duplication; it does
    NOT make the poller multi-worker safe on its own -- the overlap guard still
    reads the in-process RunRegistry, so `_dispatch_lock` stays. See the spec's
    scope boundary.)

    Deliberately does not touch `attempts`: see `mark_dispatched`.
    """
    if limit <= 0:
        return []
    oldest_pending = (
        select(InboxEvent.id)
        .where(InboxEvent.org_id == org_id, InboxEvent.status == EVENT_PENDING)
        .order_by(InboxEvent.id)
        .limit(limit)
    )
    db.execute(
        update(InboxEvent)
        .where(InboxEvent.id.in_(oldest_pending))
        .values(status=EVENT_CLAIMED, run_id=run_id, claimed_at=_utcnow())
        .execution_options(synchronize_session=False)
    )
    return list(
        db.execute(
            select(InboxEvent)
            .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED)
            .order_by(InboxEvent.id)
        ).scalars()
    )


def claimed_events(db: Session, run_id: str) -> List[InboxEvent]:
    """This run's currently-claimed events, oldest first."""
    return list(
        db.execute(
            select(InboxEvent)
            .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED)
            .order_by(InboxEvent.id)
        ).scalars()
    )


def mark_dispatched(db: Session, run_id: str) -> None:
    """Charge one attempt against every event this run claimed.

    Charged here rather than at claim time on purpose. A workflow that fails to
    *build* (team deleted or edited into an invalid state) is not the message's
    fault, and today such mail is never consumed -- it retries until the
    customer fixes the team. Charging at claim would dead-letter a whole day of
    an org's mail because of a config mistake, so the release path that follows
    a build failure is penalty-free and only a real dispatch costs an attempt.
    """
    db.execute(
        update(InboxEvent)
        .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED)
        .values(attempts=InboxEvent.attempts + 1)
        .execution_options(synchronize_session=False)
    )


def complete_events(
    db: Session, run_id: str, *, done_external_ids, error: Optional[str] = None
) -> None:
    """Terminal outcome for a run that actually executed the model.

    `done_external_ids` is Phase 0's `already_drafted_uids` evidence: messages a
    draft demonstrably exists for. Those are `done` even on a failed run --
    reprocessing them would create a second draft, since `email_draft_reply`
    has no dedup of its own. Everything else is `failed` and waits for the
    existing human retry, which is the product behaviour today for a run whose
    model ran and failed.
    """
    now = _utcnow()
    done = {str(x) for x in done_external_ids}
    for event in claimed_events(db, run_id):
        if event.external_id in done:
            event.status = EVENT_DONE
            event.last_error = None
        else:
            event.status = EVENT_FAILED
            event.last_error = error
        event.completed_at = now


def release_events(
    db: Session, run_id: str, *, max_attempts: int, error: Optional[str] = None
) -> int:
    """Hand this run's claimed events back for reprocessing.

    For infrastructure-class failures only -- a killed process, a failed
    dispatch, a watchdog timeout -- where no model spend was incurred and the
    messages themselves are innocent. Rows that have used up `max_attempts` are
    dead-lettered instead of looping forever; the caller surfaces that on the
    trigger's health so a stuck message is not invisible.

    Returns the number dead-lettered.
    """
    now = _utcnow()
    dead_lettered = 0
    for event in claimed_events(db, run_id):
        if event.attempts >= max_attempts:
            event.status = EVENT_FAILED
            event.last_error = error
            event.completed_at = now
            dead_lettered += 1
        else:
            event.status = EVENT_PENDING
            event.run_id = None
            event.claimed_at = None
            event.last_error = error
    return dead_lettered


def reopen_events(db: Session, run_id: str) -> int:
    """Return a failed run's terminal events to the queue for a human retry.

    Attempts reset to 0: the customer has looked at the failure and asked for
    it again, so the automatic dead-letter budget starts over.
    """
    result = db.execute(
        update(InboxEvent)
        .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_FAILED)
        .values(
            status=EVENT_PENDING, run_id=None, claimed_at=None,
            completed_at=None, attempts=0, last_error=None,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0
