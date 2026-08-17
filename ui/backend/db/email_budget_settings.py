"""Per-org automation budget settings and the spend query (Phase 4a).

Monthly spend is queried, never counted into a column: `usage_records.org_id`
is already denormalised for exactly this, and a stored counter would need its
own reset, its own backfill and its own drift bug.

Nothing here commits: callers own the transaction boundary.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..email_budget import BudgetCaps
from .email_triggers import get_email_trigger
from .model_catalog import list_entries
from .models import OrgEmailBudgetSetting, UsageRecord, WorkflowRecord

_logger = logging.getLogger(__name__)


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


def unpriced_models_for_org(db: Session, org_id: int) -> List[str]:
    """The models this org's automation runs that have no `model_catalog` row.

    Such a model contributes nothing to `spent_this_month`, so a monthly cap is
    a floor on reality rather than a ceiling. Naming the models is what turns
    that from a silent inaccuracy into something an admin can act on -- by
    pricing the model, or by knowing the cap does not cover it.

    Resolved from the org's trigger: its `workflow_name` against the same
    deployed `WorkflowRecord` the poller itself builds from, then each agent's
    `model` exactly as `deploy_validation.validate_agent_models` reads it. Only
    a deployed record can run automatically, so only its models can cost
    anything.

    Deliberately total: no trigger, no deployed team, no models, or any failure
    at all yields `[]`. This is advisory copy on a settings page and must never
    be able to stop an admin saving a cap.
    """
    try:
        trigger = get_email_trigger(db, org_id)
        if trigger is None or not trigger.workflow_name:
            return []
        # `status="deployed"` is intended, not an oversight: a trigger pointing
        # at a draft cannot run, so it cannot spend, so it has no models the cap
        # fails to cover. Do not widen this to scan drafts.
        record = (
            db.query(WorkflowRecord)
            .filter_by(name=trigger.workflow_name, org_id=org_id, status="deployed")
            .one_or_none()
        )
        if record is None:
            return []
        specs = {
            agent.get("model")
            for agent in (record.config or {}).get("agents") or []
            if isinstance(agent, dict) and isinstance(agent.get("model"), str)
            and agent.get("model")
        }
        if not specs:
            return []
        priced = {entry.spec for entry in list_entries(db)}
        # Sorted and de-duplicated so the response -- and the test pinning it --
        # cannot depend on dict ordering.
        return sorted(specs - priced)
    except Exception:  # noqa: BLE001 -- advisory only, never a failed save
        _logger.warning(
            "Could not resolve unpriced models for org %s", org_id, exc_info=True
        )
        return []


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
