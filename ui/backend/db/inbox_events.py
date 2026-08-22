"""Durable per-message inbox ledger (email automation Phase 1).

Detection records a `pending` row per new message in the SAME transaction that
advances the mailbox cursor, so mail can never be consumed without a durable
record of the work. A run then claims rows; the claimed rows' `external_id`s
are the batch it processes. Status is `pending | claimed | done | failed |
filtered` -- `filtered` (Phase 4a's pre-LLM filter) still records the row,
just with a status the claim query never selects.

None of these helpers commit. Callers own the transaction boundary -- that is
the entire point of the design, since the durability guarantee comes from
detection's insert and the cursor advance landing in one commit.

See docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Mapping, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import InboxEvent

EVENT_PENDING = "pending"
EVENT_CLAIMED = "claimed"
EVENT_DONE = "done"
EVENT_FAILED = "failed"
EVENT_FILTERED = "filtered"

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
    decisions: Optional[Mapping[str, str]] = None,
) -> int:
    """Record each id, ignoring ones already known.

    `decisions` maps an `external_id` to a pre-LLM filter decision (Phase 4a);
    those ids are recorded `filtered` with the reason, the rest `pending`.
    Filtering changes a row's status, never whether the row exists -- Phase 1's
    durability guarantee is that the commit consuming the mail is the commit
    recording the work, and a filter that skipped the insert would become a
    second way to consume mail with no record of it.

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
            "status": (
                EVENT_FILTERED if decisions and str(external_id) in decisions
                else EVENT_PENDING
            ),
            "decision": (decisions or {}).get(str(external_id)),
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


def claim_events(
    db: Session,
    *,
    org_id: int,
    run_id: str,
    limit: int,
    mailbox_identity: str,
    mailbox_generation: str,
) -> List[InboxEvent]:
    """Atomically claim up to `limit` of this mailbox's oldest pending events.

    One UPDATE, so under SQLite's write lock two claimants cannot be handed the
    same message. (That removes one class of cross-process duplication; it does
    NOT make the poller multi-worker safe on its own -- the overlap guard still
    reads the in-process RunRegistry, so `_dispatch_lock` stays. See the spec's
    scope boundary.)

    `mailbox_identity`/`mailbox_generation` are **required**, not optional with
    an org-wide default: the defect they close is that a caller could omit the
    mailbox entirely, so the signature is what should refuse. An org that
    replaces or rebuilds its mailbox keeps `pending` rows from the previous
    one, and an IMAP UID means nothing outside the generation that issued it --
    claiming such a row hands a run bound to the new mailbox a UID that, after
    a rebuild, now names a completely different message.

    Deliberately does not touch `attempts`: see `mark_dispatched`.
    """
    if limit <= 0:
        return []
    oldest_pending = (
        select(InboxEvent.id)
        .where(
            InboxEvent.org_id == org_id,
            InboxEvent.status == EVENT_PENDING,
            InboxEvent.mailbox_identity == mailbox_identity,
            InboxEvent.mailbox_generation == mailbox_generation,
        )
        .order_by(InboxEvent.id)
        .limit(limit)
    )
    db.execute(
        update(InboxEvent)
        .where(InboxEvent.id.in_(oldest_pending))
        .values(status=EVENT_CLAIMED, run_id=run_id, claimed_at=_utcnow())
        .execution_options(synchronize_session="fetch")
    )
    return list(
        db.execute(
            select(InboxEvent)
            .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED)
            .order_by(InboxEvent.id)
        ).scalars()
    )


def has_pending_events(
    db: Session, *, org_id: int, mailbox_identity: str, mailbox_generation: str
) -> bool:
    """Whether this mailbox has anything `claim_events` could claim right now.

    A `pending` row can exist for reasons other than "mail just arrived": an
    admin released a filtered false positive, or a budget cap left a backlog
    behind. `poll_org` uses this to decide whether a cycle that found no new
    UIDs should still go on to dispatch -- without it, both of those wait for
    unrelated mail to land before anything claims them, on a quiet mailbox
    possibly for days.

    Deliberately an existence check (`LIMIT 1`) scoped exactly as
    `claim_events` scopes its own selection, rather than a speculative claim:
    an idle mailbox is the common case and the empty poll must stay cheap.
    """
    return (
        db.execute(
            select(InboxEvent.id)
            .where(
                InboxEvent.org_id == org_id,
                InboxEvent.status == EVENT_PENDING,
                InboxEvent.mailbox_identity == mailbox_identity,
                InboxEvent.mailbox_generation == mailbox_generation,
            )
            .limit(1)
        ).first()
        is not None
    )


SUPERSEDED_MESSAGE = (
    "The mailbox was replaced or rebuilt before this message was processed."
)


def abandon_superseded_events(
    db: Session,
    *,
    org_id: int,
    mailbox_identity: str,
    mailbox_generation: Optional[str] = None,
) -> int:
    """Mark this org's `pending` rows from any OTHER mailbox terminal.

    `claim_events`'s scoping already makes such a row unclaimable; this is what
    stops it sitting `pending` forever with nothing reporting it. Called at the
    two moments a generation changes: the mailbox identity changing
    (`email_trigger.disable_trigger_on_identity_change`, which passes no
    generation because the new mailbox's UIDVALIDITY is not known yet) and the
    UIDVALIDITY re-baseline in `poll_org`.

    Expressed as "everything that is not the current mailbox" rather than
    "everything that was the old one", so it needs no memory of the previous
    identity and self-corrects if a change was ever missed.

    `claimed` rows are deliberately untouched: one belongs to a run that will
    complete, be released by the stale-run watchdog, or be released by
    `runtime.fail_interrupted_runs` at startup, and racing that from here would
    give one batch two owners.

    Returns how many were abandoned. Does not commit.
    """
    mismatched = InboxEvent.mailbox_identity != mailbox_identity
    if mailbox_generation is not None:
        mismatched = mismatched | (InboxEvent.mailbox_generation != mailbox_generation)
    return db.execute(
        update(InboxEvent)
        .where(
            InboxEvent.org_id == org_id,
            InboxEvent.status == EVENT_PENDING,
            mismatched,
        )
        .values(
            status=EVENT_FAILED,
            last_error=SUPERSEDED_MESSAGE,
            completed_at=_utcnow(),
        )
        .execution_options(synchronize_session="fetch")
    ).rowcount or 0


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

    Charged here rather than at claim time on purpose. A pipeline that fails to
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
        .execution_options(synchronize_session="fetch")
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


def resolve_retry_events(
    db: Session, *, from_run_id: str, to_run_id: str, retry_external_ids, done_external_ids
) -> int:
    """Hand a failed run's events to the retry that will redo them.

    The retry path decides what is actually redone -- it re-derives the batch
    from `trigger_context` and subtracts everything Phase 0's evidence (result
    rows, trace events, and the mailbox scan) shows a draft for. This mirrors
    that decision onto the ledger rather than second-guessing it:

    - `done_external_ids` become `done` on the ORIGINAL run. These are messages
      the retry refuses to touch because a draft already exists; the mailbox
      scan can discover some of them only now, at retry time.
    - `retry_external_ids` move to the new run as `claimed`, which makes that
      run responsible for completing them.

    Attempts reset to 0: a human has looked at the failure and asked for it
    again, so the automatic dead-letter budget starts over.

    Returns how many events moved. Zero is normal and expected for a run that
    predates the ledger -- the retry path falls back to `trigger_context` alone.
    """
    now = _utcnow()
    retry = {str(x) for x in retry_external_ids}
    done = {str(x) for x in done_external_ids}
    moved = 0
    rows = db.execute(
        select(InboxEvent).where(
            InboxEvent.run_id == from_run_id, InboxEvent.status == EVENT_FAILED
        )
    ).scalars()
    for event in rows:
        if event.external_id in done:
            event.status = EVENT_DONE
            event.last_error = None
            event.completed_at = now
        elif event.external_id in retry:
            event.run_id = to_run_id
            event.status = EVENT_CLAIMED
            event.claimed_at = now
            event.completed_at = None
            event.attempts = 0
            event.last_error = None
            moved += 1
    return moved


def list_filtered_events(db: Session, *, org_id: int, limit: int) -> List[InboxEvent]:
    """This org's filtered messages, newest first -- what the activity view
    shows so a false positive is discoverable rather than silently lost."""
    return list(
        db.execute(
            select(InboxEvent)
            .where(InboxEvent.org_id == org_id, InboxEvent.status == EVENT_FILTERED)
            .order_by(InboxEvent.id.desc())
            .limit(limit)
        ).scalars()
    )


def release_filtered_event(db: Session, *, org_id: int, event_id: int) -> bool:
    """Hand one filtered message back for normal processing.

    A single status flip is the whole feature: the next poll cycle claims it
    like any other pending row, so there is no second dispatch path to keep
    correct. Scoped by `org_id` and by `status == filtered`, so it can neither
    touch another org's row nor resurrect one that already ran.
    """
    updated = db.execute(
        update(InboxEvent)
        .where(
            InboxEvent.id == event_id,
            InboxEvent.org_id == org_id,
            InboxEvent.status == EVENT_FILTERED,
        )
        .values(status=EVENT_PENDING, decision=None, run_id=None)
        .execution_options(synchronize_session="fetch")
    ).rowcount
    return bool(updated)
