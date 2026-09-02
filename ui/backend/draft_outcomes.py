"""Draft outcome tracking: what the mailbox eventually did with each
platform-written draft (B1).

One `draft_outcomes` row per confirmed draft, created at run finalization
from `automation_results.already_drafted_uids` -- the DB-only union of trace
evidence and result rows, never the model's claim. The poll cycle then walks
`pending` rows down a decision ladder against the mailbox: still in Drafts →
pending; found in Sent by the `X-BestTeam-Source-Key` header → sent; found in
Sent by `In-Reply-To` (a client that rebuilt the MIME on send, dropping the
custom header) → sent; in neither folder for `MISS_THRESHOLD` consecutive
cycles → handled. The `evidence` column doubles as the live answer to the
"does the header survive a client send?" spike the design could not run
against a real mailbox. See docs/superpowers/specs/
2026-09-03-draft-outcome-tracking-design.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from sqlalchemy.orm import Session

from .automation_results import already_drafted_uids
from .db.models import DraftOutcome, Run

_logger = logging.getLogger(__name__)

# Reconciliation only ever looks at rows this young; a draft still pending
# after this is marked `unknown` (untouched for a month is not "awaiting
# action" in any useful sense, and the bound keeps IMAP work finite).
WINDOW_DAYS = 30
# Rows examined per org per poll cycle -- each key can cost an IMAP SEARCH.
RECONCILE_BATCH = 25
# Consecutive cycles a draft must be missing from both Drafts and Sent before
# it is `handled`: a customer who pressed Send seconds ago has a message in
# neither folder until the client finishes uploading to Sent.
MISS_THRESHOLD = 2

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_HANDLED = "handled"
STATUS_UNKNOWN = "unknown"

EVIDENCE_SOURCE_KEY_HEADER = "source_key_header"
EVIDENCE_IN_REPLY_TO = "in_reply_to"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_outcomes_for_run(db: Session, run_row: Run) -> int:
    """One `pending` row per draft this run is proven to have written.

    Returns the number of rows added. `source_key` is globally unique (it
    encodes credential id + uidvalidity + uid), so a retry family collapses
    to one row: keys that already have a row are skipped, whichever family
    member recorded them first.
    """
    trigger_context = getattr(run_row, "trigger_context", None) or {}
    if trigger_context.get("trigger_type") != "email" or run_row.org_id is None:
        return 0
    drafted = already_drafted_uids(db, run_row)
    if not drafted:
        return 0
    prefix = _marker_prefix(
        trigger_context.get("mailbox_credential_id"), trigger_context.get("uidvalidity")
    )
    keys = {f"{prefix}{uid}": uid for uid in drafted}
    existing = {
        row.source_key
        for row in db.query(DraftOutcome.source_key)
        .filter(DraftOutcome.source_key.in_(keys))
        .all()
    }
    rows = [
        DraftOutcome(org_id=run_row.org_id, run_id=run_row.id, source_key=key)
        for key in keys
        if key not in existing
    ]
    if not rows:
        return 0
    db.add_all(rows)
    db.commit()
    return len(rows)


def _marker_prefix(mailbox_credential_id, uidvalidity) -> str:
    # Same shape as email_trigger.draft_marker_prefix / automation_results.
    # _source_key; repeated here (not imported) because email_trigger imports
    # this module, and the format is pinned by both of those modules' tests.
    return f"mailbox:{mailbox_credential_id}:uidvalidity:{uidvalidity}:uid:"


def reconcile(db: Session, *, org_id: int, backend, marker_prefix: str,
              now: datetime | None = None) -> None:
    """One org's reconcile pass over up to RECONCILE_BATCH pending rows.

    May raise (an IMAP search can fail mid-pass); `reconcile_org` is the
    isolation boundary. Committed once at the end: a mid-pass failure rolls
    the whole pass back, and the next cycle simply re-derives the same
    verdicts -- every rung reads current mailbox state, so nothing is lost.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(days=WINDOW_DAYS)
    rows: List[DraftOutcome] = (
        db.query(DraftOutcome)
        .filter(DraftOutcome.org_id == org_id, DraftOutcome.status == STATUS_PENDING)
        .order_by(DraftOutcome.checked_at.asc().nullsfirst(), DraftOutcome.id.asc())
        .limit(RECONCILE_BATCH)
        .all()
    )

    active: List[DraftOutcome] = []
    for row in rows:
        # A UID means nothing outside the mailbox generation that issued it
        # (same rule as the poller's uidvalidity re-baseline), and a row past
        # the window stops costing IMAP searches. Both verdicts are reached
        # without touching the mailbox.
        if not row.source_key.startswith(marker_prefix) or row.created_at < cutoff:
            _resolve(row, STATUS_UNKNOWN, now=now)
        else:
            active.append(row)
    if not active:
        db.commit()
        return

    by_key = {row.source_key: row for row in active}
    still_drafts = backend.drafts_with_source_keys(list(by_key))
    gone = []
    for key, row in by_key.items():
        if key in still_drafts:
            row.checked_at = now
            row.miss_count = 0
        else:
            gone.append(row)

    if gone:
        in_sent = backend.sent_with_source_keys([row.source_key for row in gone])
        remaining = []
        for row in gone:
            if row.source_key in in_sent:
                _resolve(row, STATUS_SENT, evidence=EVIDENCE_SOURCE_KEY_HEADER, now=now)
            else:
                remaining.append(row)

        if remaining:
            need_msgid = [r for r in remaining if not r.origin_message_id]
            if need_msgid:
                uid_to_row = {r.source_key[len(marker_prefix):]: r for r in need_msgid}
                fetched = backend.message_ids_for_uids(list(uid_to_row))
                for uid, message_id in (fetched or {}).items():
                    if uid in uid_to_row and message_id:
                        uid_to_row[uid].origin_message_id = message_id

            with_msgid = [r for r in remaining if r.origin_message_id]
            replied = (
                backend.sent_replies_to([r.origin_message_id for r in with_msgid])
                if with_msgid
                else set()
            )
            for row in remaining:
                if row.origin_message_id and row.origin_message_id in replied:
                    _resolve(row, STATUS_SENT, evidence=EVIDENCE_IN_REPLY_TO, now=now)
                else:
                    row.checked_at = now
                    row.miss_count = (row.miss_count or 0) + 1
                    if row.miss_count >= MISS_THRESHOLD:
                        _resolve(row, STATUS_HANDLED, now=now)

    db.commit()


def _resolve(row: DraftOutcome, status: str, *, evidence: str | None = None,
             now: datetime) -> None:
    row.status = status
    row.evidence = evidence
    row.checked_at = now
    row.resolved_at = now


def reconcile_org(db: Session, trigger) -> None:
    """Glue for the poll cycle: build the org's mailbox backend and run one
    reconcile pass. Isolated like `_apply_backlog_health` -- outcome
    bookkeeping must never break polling -- and free in the steady state: no
    recent pending rows means no credential decrypt and no IMAP connection.
    """
    try:
        now = _utcnow()
        has_pending = (
            db.query(DraftOutcome.id)
            .filter(
                DraftOutcome.org_id == trigger.org_id,
                DraftOutcome.status == STATUS_PENDING,
            )
            .first()
        )
        if has_pending is None or trigger.uidvalidity is None:
            return
        # Late imports: email_trigger (which imports this module) sits on the
        # path from email_tools, and none of this is needed on the free path.
        from . import secret_store
        from .db.email_credentials import get_email_credentials
        from .email_tools import build_backend_for_credential

        cred = get_email_credentials(db, trigger.org_id)
        if cred is None:
            return
        password = secret_store.decrypt(cred.password_encrypted)
        backend = build_backend_for_credential(cred, password)
        reconcile(
            db,
            org_id=trigger.org_id,
            backend=backend,
            marker_prefix=_marker_prefix(cred.id, trigger.uidvalidity),
            now=now,
        )
    except Exception:  # noqa: BLE001 -- bookkeeping must never break the poll loop
        db.rollback()
        _logger.warning(
            "draft outcomes: reconcile failed for org %s", trigger.org_id, exc_info=True
        )


def summary(db: Session, org_id: int, *, now: datetime | None = None) -> Dict[str, int]:
    """Counts for the Automations tab: rows created inside the window, by
    status. `unknown` is deliberately excluded -- it means "we can no longer
    tell", which is not a number worth a customer's attention."""
    now = now or _utcnow()
    cutoff = now - timedelta(days=WINDOW_DAYS)
    counts = {STATUS_SENT: 0, STATUS_HANDLED: 0, STATUS_PENDING: 0}
    rows = (
        db.query(DraftOutcome.status)
        .filter(
            DraftOutcome.org_id == org_id,
            DraftOutcome.created_at >= cutoff,
            DraftOutcome.status.in_(list(counts)),
        )
        .all()
    )
    for (status,) in rows:
        counts[status] += 1
    counts["window_days"] = WINDOW_DAYS
    return counts
