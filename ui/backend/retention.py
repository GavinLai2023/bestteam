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
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db.models import AutomationItemResult, Run, TraceEventRecord

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
