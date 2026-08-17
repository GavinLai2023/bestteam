"""Per-org automation budget settings and the spend query (Phase 4a).

Monthly spend is queried, never counted into a column: `usage_records.org_id`
is already denormalised for exactly this, and a stored counter would need its
own reset, its own backfill and its own drift bug.

Nothing here commits: callers own the transaction boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..email_budget import BudgetCaps
from .models import OrgEmailBudgetSetting, UsageRecord


def _month_start(now: datetime) -> datetime:
    """First instant of `now`'s UTC month, tzinfo-naive to match how SQLite
    stores and returns `usage_records.created_at`."""
    moment = now.astimezone(timezone.utc) if now.tzinfo else now
    return datetime(moment.year, moment.month, 1)


def get_budget_row(db: Session, org_id: int) -> Optional[OrgEmailBudgetSetting]:
    return (
        db.query(OrgEmailBudgetSetting)
        .filter(OrgEmailBudgetSetting.org_id == org_id)
        .one_or_none()
    )


def get_budget_caps(db: Session, org_id: int) -> BudgetCaps:
    row = get_budget_row(db, org_id)
    if row is None:
        return BudgetCaps()
    return BudgetCaps(
        daily_message_cap=row.daily_message_cap,
        monthly_cost_cap=row.monthly_cost_cap,
    )


def set_budget_caps(
    db: Session,
    org_id: int,
    *,
    daily_message_cap: Optional[int],
    monthly_cost_cap: Optional[float],
) -> OrgEmailBudgetSetting:
    """Set or clear the caps. The row is kept either way, like
    `set_retention_days`."""
    row = get_budget_row(db, org_id)
    if row is None:
        row = OrgEmailBudgetSetting(org_id=org_id)
        db.add(row)
    row.daily_message_cap = daily_message_cap
    row.monthly_cost_cap = monthly_cost_cap
    db.flush()
    return row


def spent_this_month(db: Session, org_id: int, now: datetime) -> Optional[float]:
    """Estimated spend so far this UTC month, or `None` if nothing is priced.

    `None` is not zero and not "over budget": it means every usage record this
    month came from a model with no `model_catalog` entry, which the budget
    surfaces rather than hides.
    """
    return db.execute(
        select(func.sum(UsageRecord.cost_estimate)).where(
            UsageRecord.org_id == org_id,
            UsageRecord.created_at >= _month_start(now),
        )
    ).scalar()


def unpriced_run_count(db: Session, org_id: int, now: datetime) -> int:
    """Distinct runs this month whose usage carried no price.

    Reported in the UI so an admin can tell "we spent almost nothing" from "the
    cap does not cover what we ran".
    """
    return int(
        db.execute(
            select(func.count(func.distinct(UsageRecord.run_id))).where(
                UsageRecord.org_id == org_id,
                UsageRecord.created_at >= _month_start(now),
                UsageRecord.cost_estimate.is_(None),
            )
        ).scalar()
        or 0
    )
