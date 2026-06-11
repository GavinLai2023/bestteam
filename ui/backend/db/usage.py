"""CRUD for `UsageRecord` (Phase 3).

`record_usage` is called from `ui/backend/runtime.py` for every per-model-call
usage entry on an `agent_completed` `TraceEvent` (see
`adapters/langgraph_adapter.py::_record_usage`), converting token counts into
a `cost_estimate` via the `model_catalog` (when a matching `spec` is found).
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .model_catalog import get_entry
from .models import UsageRecord


def record_usage(
    db: Session,
    *,
    run_id: str,
    agent: Optional[str],
    model: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> UsageRecord:
    cost_estimate = None
    if model:
        entry = get_entry(db, model)
        if entry is not None:
            cost_estimate = (input_tokens / 1000) * entry.input_price_per_1k + (
                output_tokens / 1000
            ) * entry.output_price_per_1k

    record = UsageRecord(
        run_id=run_id,
        agent=agent,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def list_usage_for_run(db: Session, run_id: str) -> List[UsageRecord]:
    return db.query(UsageRecord).filter_by(run_id=run_id).order_by(UsageRecord.id).all()
