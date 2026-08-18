"""Per-org automation budget settings and the spend query (Phase 4a).

Monthly spend is queried, never counted into a column: `usage_records.org_id`
is already denormalised for exactly this, and a stored counter would need its
own reset, its own backfill and its own drift bug.

Nothing here commits: callers own the transaction boundary.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bestteam.core.embeddings import billable_spec

from ..email_budget import BudgetCaps
from .email_triggers import get_email_trigger
from .model_catalog import list_entries
from .models import (
    KnowledgeBaseRecord,
    OrgEmailBudgetSetting,
    UsageRecord,
    WorkflowRecord,
)

# The operator-wide default a self-service "smart search" knowledge base is
# created on (`ui/backend/org_knowledge_bases.py`). Named here rather than
# imported to avoid a db -> API-layer import.
_ENV_DEFAULT_EMBEDDING_MODEL = "BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL"

# The two knowledge-base model fields that cost money. `rerank_model` is
# deliberately absent: reranking is a local cross-encoder, $0, and is never
# metered (see `ui/backend/CLAUDE.md`).
_KB_BILLABLE_FIELDS = ("embedding_model", "query_expansion_model")

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


def _kb_config_specs(config: Any) -> Set[str]:
    """The billable model specs one knowledge base's `config` names.

    `billable_spec` is the same definition of "costs money" the metering
    itself uses (`core/embeddings.py`), so a `fake:` spec -- $0 by
    construction -- is never reported as a blind spot.
    """
    if not isinstance(config, dict):
        return set()
    specs = {billable_spec(config.get(field)) for field in _KB_BILLABLE_FIELDS}
    return {spec for spec in specs if spec}


def _knowledge_base_specs(db: Session, org_id: int, raw: Dict[str, Any]) -> Set[str]:
    """The billable specs of the knowledge bases this deployed team searches.

    A knowledge base spends on its own account, which is why the agent walk
    above is not enough: a query embedding rides the searching agent's usage,
    and an *ingestion* embedding is written with no `run_id` at all -- so an
    unpriced embedding model is invisible to `unpriced_run_count` as well as
    to `spent_this_month`.

    An inline `knowledge_bases:` entry shadows a same-named standalone record,
    matching how `db/dependencies.py::record_version_dependencies` resolves
    the same two sources.
    """
    inline = {
        kb.get("name"): kb
        for kb in raw.get("knowledge_bases") or []
        if isinstance(kb, dict)
    }
    specs: Set[str] = set()
    for kb in inline.values():
        specs |= _kb_config_specs(kb)

    referenced = {
        tool
        for agent in raw.get("agents") or []
        if isinstance(agent, dict)
        for tool in agent.get("tools") or []
        if isinstance(tool, str) and tool not in inline
    }
    if referenced:
        records = (
            db.query(KnowledgeBaseRecord)
            .filter(
                KnowledgeBaseRecord.name.in_(referenced),
                KnowledgeBaseRecord.org_id == org_id,
            )
            .all()
        )
        for record in records:
            specs |= _kb_config_specs(record.config)

    # The operator's smart-search default is what the next knowledge base this
    # org uploads will embed under, whether or not one exists today, so an
    # unpriced one is worth naming while the admin is looking at the cap.
    default = billable_spec(os.environ.get(_ENV_DEFAULT_EMBEDDING_MODEL) or None)
    if default:
        specs.add(default)
    return specs


def unpriced_models_for_org(db: Session, org_id: int) -> List[str]:
    """The models this org's automation runs that have no `model_catalog` row.

    Such a model contributes nothing to `spent_this_month`, so a monthly cap is
    a floor on reality rather than a ceiling. Naming the models is what turns
    that from a silent inaccuracy into something an admin can act on -- by
    pricing the model, or by knowing the cap does not cover it.

    Resolved from the org's trigger: its `workflow_name` against the same
    deployed `WorkflowRecord` the poller itself builds from, then every agent's
    non-empty string `model` that has no `model_catalog` entry, plus the
    billable embedding/query-expansion specs of the knowledge bases that team
    searches (`_knowledge_base_specs`). Only a deployed record can run
    automatically, so only its models can cost anything.

    The agent step is a **narrower** rule than
    `deploy_validation.validate_agent_models` applies to the same field: that
    function exempts `fake:`/`fake-architect:` specs, and this one does not.
    So a demo team on a `fake:` model is reported here as unpriced. It is: it
    has no catalogue row and so contributes nothing to `spent_this_month`,
    which is exactly what this list is for. Pinned by
    `tests/test_email_filter_api.py::test_saving_a_spend_cap_names_the_models_it_cannot_cover`.

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
        raw = record.config or {}
        specs = {
            agent.get("model")
            for agent in raw.get("agents") or []
            if isinstance(agent, dict) and isinstance(agent.get("model"), str)
            and agent.get("model")
        }
        specs |= _knowledge_base_specs(db, org_id, raw)
        if not specs:
            return []
        # `list_entries`, not `list_chat_entries`: an embedding model's row is
        # exactly what prices a knowledge base's spend.
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
