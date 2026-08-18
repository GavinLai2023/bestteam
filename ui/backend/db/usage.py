"""CRUD for `UsageRecord` (Phase 3).

`record_usage` is called from `ui/backend/runtime.py` for every per-model-call
usage entry on an `agent_completed` `TraceEvent` (see
`adapters/langgraph_adapter.py::_record_usage`), converting token counts into
a `cost_estimate` via the `model_catalog` (when a matching `spec` is found).
It is also called from `ui/backend/ingestion.py` for a knowledge base's
document-embedding spend, which belongs to an upload rather than a run and so
carries a NULL `run_id` and an `ingestion_job_id` instead.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .model_catalog import get_entry
from .models import UsageRecord


def record_usage(
    db: Session,
    *,
    run_id: Optional[str],
    agent: Optional[str],
    model: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
    org_id: Optional[int] = None,
    ingestion_job_id: Optional[int] = None,
) -> UsageRecord:
    """Persist one metered call. `run_id` is None only for spend that belongs
    to no run -- today, a knowledge base's ingestion embeddings, which pass
    `ingestion_job_id` instead (see `ui/backend/ingestion.py`)."""
    cost_estimate = None
    if model:
        entry = get_entry(db, model)
        if entry is not None:
            cost_estimate = (input_tokens / 1000) * entry.input_price_per_1k + (
                output_tokens / 1000
            ) * entry.output_price_per_1k

    record = UsageRecord(
        run_id=run_id,
        ingestion_job_id=ingestion_job_id,
        agent=agent,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
        # Denormalized from the run's org for per-customer aggregation.
        org_id=org_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def list_usage_for_run(db: Session, run_id: str) -> List[UsageRecord]:
    return db.query(UsageRecord).filter_by(run_id=run_id).order_by(UsageRecord.id).all()
