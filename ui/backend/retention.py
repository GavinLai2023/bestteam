"""Run-history retention: the purge engine, the sweep, and export (Phase 3b).

A purge clears CONTENT and keeps ACCOUNTING. Content is `runs.input`/`output`,
every `trace_events` row, the run's `run_knowledge_generations` references (derived from those events),
and `automation_item_results.payload`. Accounting is
the `runs` row itself, `usage_records`, `trigger_context`, and an item result's
`status`/`source_key` -- see the design spec's invariants I1-I5. Deleting the
run row instead would take the org's token/cost history with it, and clearing
an item's status/source_key would make a sweep cause duplicate drafts on retry.
A purge also scrubs the run's in-memory copy in `RunRegistry`, which serves
`GET /api/runs/{id}` and the WebSocket replay from its own retained history.

See docs/superpowers/specs/2026-08-17-email-phase-3b-retention-export-design.md.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db.models import AutomationItemResult, Run, TraceEventRecord, iso_utc
from .db.retention import orgs_with_retention, record_sweep
from .db.run_knowledge_generations import delete_for_run as _release_knowledge_generations

_logger = logging.getLogger(__name__)

# The purge surface, declared once. `export_org_runs` must emit every one of
# these, and tests/test_retention.py::test_export_covers_everything_purge_clears
# is what enforces it -- an export that stopped covering a purged field would
# make deletion quietly unsafe.
PURGED_FIELDS: dict[str, tuple[str, ...]] = {
    "runs": ("input", "output"),
    "trace_events": ("*",),
    "automation_item_results": ("payload",),
}

# Purged like the fields above, and deliberately NOT exported -- which is why
# it cannot live in `PURGED_FIELDS`, whose whole contract is the opposite.
# `runs.internal_error` is the operator's copy of why a run failed: a provider's
# own exception text, which can name the model, the provider and the account's
# billing state, and which the customer was deliberately never shown (see
# runtime.py). The export is the CUSTOMER's way out of their own data; this is
# not theirs. It is still purged, because a provider's message can quote the
# prompt or the model's output.
PURGED_OPERATOR_FIELDS: dict[str, tuple[str, ...]] = {
    "runs": ("internal_error",),
}

_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def purge_run(db: Session, run: Run) -> bool:
    """Clear one run's content. Does NOT commit.

    Returns False without touching anything when the run is still running (its
    worker is mid-write) or was already purged -- both are ordinary, not
    errors, so callers can loop over a batch without special cases.
    """
    if run.status not in _TERMINAL_STATUSES or run.content_purged_at is not None:
        return False

    db.query(TraceEventRecord).filter(TraceEventRecord.run_id == run.id).delete(
        synchronize_session=False
    )
    # The trace is what named a knowledge-base generation's chunk ids, so the
    # reference keeping that generation's rows alive goes with it (see
    # db/run_knowledge_generations.py). Not a purged field: it is an index
    # over content the export already carries.
    _release_knowledge_generations(db, run.id)
    for item in db.query(AutomationItemResult).filter(
        AutomationItemResult.run_id == run.id
    ):
        item.payload = {}

    run.input = ""
    run.output = None
    run.internal_error = None  # PURGED_OPERATOR_FIELDS -- purged, never exported
    run.content_purged_at = _utcnow()
    db.flush()

    # The DB is not the only copy. `RunRegistry` keeps the last 1,000 runs in
    # memory with their full input and event history, and that is what
    # `GET /api/runs/{id}` and the WebSocket replay serve -- so clearing only
    # the rows would leave deleted content readable until the entry was
    # evicted or the process restarted. Late import: `runtime` imports this
    # package's siblings, and keeping it out of module scope keeps the purge
    # engine importable on its own.
    from .runtime import registry

    registry.purge_content(run.id)
    return True


def _purgeable_query(
    db: Session, *, org_id: int, older_than_days: int, now: Optional[datetime] = None
):
    """The one definition of "what a purge would take".

    Both the purge and its preview count go through here, so the number the
    customer is shown before pressing the button can never disagree with what
    the button then removes.
    """
    cutoff = (now or _utcnow()) - timedelta(days=older_than_days)
    return db.query(Run).filter(
        Run.org_id == org_id,
        Run.created_at < cutoff,
        Run.content_purged_at.is_(None),
        Run.status.in_(_TERMINAL_STATUSES),
    )


def purge_org_runs(
    db: Session, *, org_id: int, older_than_days: int, now: Optional[datetime] = None
) -> int:
    """Purge every terminal, unpurged run of this org older than the cutoff.

    `older_than_days=0` means everything terminal, right now. Does NOT commit.
    """
    runs = _purgeable_query(
        db, org_id=org_id, older_than_days=older_than_days, now=now
    ).all()
    return sum(1 for run in runs if purge_run(db, run))


def purgeable_run_count(
    db: Session, *, org_id: int, older_than_days: int, now: Optional[datetime] = None
) -> int:
    """How many runs `purge_org_runs` would take with these arguments."""
    return _purgeable_query(
        db, org_id=org_id, older_than_days=older_than_days, now=now
    ).count()


def _int_env(name: str, default: Optional[int], *, minimum: int) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        _logger.warning("%s is not an integer (%r); using %r", name, raw, default)
        return default


def retention_default_days() -> Optional[int]:
    """The policy a NEWLY created org starts with. Never applied to an existing
    org: an upgrade must not delete anybody's history (I5)."""
    return _int_env("BESTTEAM_RUN_RETENTION_DAYS", None, minimum=1)


def export_max_runs() -> int:
    return _int_env("BESTTEAM_EXPORT_MAX_RUNS", 5000, minimum=1)


def sweep_retention(db: Session, *, now: Optional[datetime] = None) -> int:
    """Apply every org's configured policy. Commits; returns the total purged.

    Orgs with no policy (the default) are not touched at all.
    """
    at = now or _utcnow()
    total = 0
    for org_id, days in orgs_with_retention(db):
        purged = purge_org_runs(db, org_id=org_id, older_than_days=days, now=at)
        record_sweep(db, org_id, purged=purged, at=at)
        total += purged
    db.commit()
    if total:
        _logger.info("retention sweep purged %d run(s)", total)
    return total


def export_org_runs(
    db: Session, *, org_id: int, days: Optional[int] = None, limit: Optional[int] = None
) -> dict:
    """Everything a purge would remove, plus the context needed to read it.

    Newest first, so a truncated export is the part a customer most likely
    wants. `truncated` is explicit: a partial export that looked complete would
    be worse than no export at all.
    """
    cap = limit or export_max_runs()
    query = db.query(Run).filter(Run.org_id == org_id)
    if days is not None:
        query = query.filter(Run.created_at >= _utcnow() - timedelta(days=days))
    rows = query.order_by(Run.created_at.desc(), Run.id).limit(cap + 1).all()
    truncated = len(rows) > cap
    rows = rows[:cap]

    runs = []
    for run in rows:
        events = (
            db.query(TraceEventRecord)
            .filter(TraceEventRecord.run_id == run.id)
            .order_by(TraceEventRecord.seq)
            .all()
        )
        items = (
            db.query(AutomationItemResult)
            .filter(AutomationItemResult.run_id == run.id)
            .order_by(AutomationItemResult.id)
            .all()
        )
        runs.append({
            "id": run.id,
            "pipeline": run.pipeline,
            "status": run.status,
            "input": run.input,
            "output": run.output,
            # `iso_utc` throughout: these columns come back from SQLite
            # tzinfo-naive, and an archive a customer keeps must not carry
            # timestamps that read as local time wherever it is opened.
            "created_at": iso_utc(run.created_at) if run.created_at else None,
            "content_purged_at": (
                iso_utc(run.content_purged_at) if run.content_purged_at else None
            ),
            "trigger_context": run.trigger_context,
            "trace_events": [
                {"seq": e.seq, "type": e.type, "agent": e.agent, "data": e.data,
                 "created_at": iso_utc(e.created_at) if e.created_at else None}
                for e in events
            ],
            "automation_item_results": [
                {"source_key": i.source_key, "status": i.status,
                 "needs_attention": i.needs_attention, "payload": i.payload,
                 "created_at": iso_utc(i.created_at) if i.created_at else None}
                for i in items
            ],
        })

    return {
        "org_id": org_id,
        "exported_at": iso_utc(_utcnow()),
        "truncated": truncated,
        "oldest_included": runs[-1]["created_at"] if runs else None,
        "runs": runs,
    }
