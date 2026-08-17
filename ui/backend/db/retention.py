"""Per-org run-history retention settings (Phase 3b).

Row CRUD only. The purge itself lives in `ui/backend/retention.py` so it can be
tested without any notion of policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from .models import OrgRetentionSetting


def get_retention_settings(db: Session, org_id: int) -> Optional[OrgRetentionSetting]:
    return (
        db.query(OrgRetentionSetting)
        .filter(OrgRetentionSetting.org_id == org_id)
        .one_or_none()
    )


def set_retention_days(db: Session, org_id: int, days: Optional[int]) -> OrgRetentionSetting:
    """Set (or clear, with None) the policy. The row is kept either way -- it
    carries the sweep history, which outlives any one policy value."""
    row = get_retention_settings(db, org_id)
    if row is None:
        row = OrgRetentionSetting(org_id=org_id)
        db.add(row)
    row.run_retention_days = days
    db.flush()
    return row


def record_sweep(db: Session, org_id: int, *, purged: int, at: datetime) -> None:
    row = get_retention_settings(db, org_id)
    if row is None:
        return
    row.last_swept_at = at
    row.last_purged_count = purged
    db.flush()


def orgs_with_retention(db: Session) -> List[Tuple[int, int]]:
    """`(org_id, days)` for every org with a policy actually set."""
    rows = (
        db.query(OrgRetentionSetting)
        .filter(OrgRetentionSetting.run_retention_days.isnot(None))
        .order_by(OrgRetentionSetting.org_id)
        .all()
    )
    return [(r.org_id, int(r.run_retention_days)) for r in rows]
