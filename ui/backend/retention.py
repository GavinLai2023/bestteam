"""Run-history retention: the purge engine, the sweep, and export (Phase 3b).

A purge clears CONTENT and keeps ACCOUNTING. Content is `runs.input`/`output`,
every `trace_events` row, and `automation_item_results.payload`. Accounting is
the `runs` row itself, `usage_records`, `trigger_context`, and an item result's
`status`/`source_key` -- see the design spec's invariants I1-I5. Deleting the
run row instead would take the org's token/cost history with it, and clearing
an item's status/source_key would make a sweep cause duplicate drafts on retry.

See docs/superpowers/specs/2026-08-17-email-phase-3b-retention-export-design.md.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db.models import AutomationItemResult, Run, TraceEventRecord
from .db.retention import orgs_with_retention, record_sweep

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
    for item in db.query(AutomationItemResult).filter(
        AutomationItemResult.run_id == run.id
    ):
        item.payload = {}

    run.input = ""
    run.output = None
    run.content_purged_at = _utcnow()
    db.flush()
    return True


def purge_org_runs(
    db: Session, *, org_id: int, older_than_days: int, now: Optional[datetime] = None
) -> int:
    """Purge every terminal, unpurged run of this org older than the cutoff.

    `older_than_days=0` means everything terminal, right now. Does NOT commit.
    """
    cutoff = (now or _utcnow()) - timedelta(days=older_than_days)
    runs = (
        db.query(Run)
        .filter(
            Run.org_id == org_id,
            Run.created_at < cutoff,
            Run.content_purged_at.is_(None),
            Run.status.in_(_TERMINAL_STATUSES),
        )
        .all()
    )
    return sum(1 for run in runs if purge_run(db, run))


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
            "workflow": run.workflow,
            "status": run.status,
            "input": run.input,
            "output": run.output,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "content_purged_at": (
                run.content_purged_at.isoformat() if run.content_purged_at else None
            ),
            "trigger_context": run.trigger_context,
            "trace_events": [
                {"seq": e.seq, "type": e.type, "agent": e.agent, "data": e.data,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in events
            ],
            "automation_item_results": [
                {"source_key": i.source_key, "status": i.status,
                 "needs_attention": i.needs_attention, "payload": i.payload,
                 "created_at": i.created_at.isoformat() if i.created_at else None}
                for i in items
            ],
        })

    return {
        "org_id": org_id,
        "exported_at": _utcnow().isoformat(),
        "truncated": truncated,
        "oldest_included": runs[-1]["created_at"] if runs else None,
        "runs": runs,
    }
