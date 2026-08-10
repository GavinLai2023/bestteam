"""Admin workflow-run analytics API (`/api/admin/analytics`).

Aggregate statistics over persisted runs/trace_events/usage_records --
success/failure rates, average duration, per-agent token/cost usage, and
common failure points -- so a platform admin can see how a workflow behaves
across many runs, not just drill into one. Admin-only (`get_current_admin`):
this reaches every org's run history, the same trust boundary as
`GET /api/runs`' cross-org admin mode and the existing `/api/config` and
`/api/memory` admin surfaces.

Reuses exactly the data already captured for the customer-facing trace view
(`TraceEventRecord`/`UsageRecord`) -- no new capture, no change to the
redaction applied at the SDK/adapter layer (see
`adapters/langgraph_adapter.py`'s `_redacted_email_tool_data`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .auth_api import get_current_admin
from .db.models import Organization, Run, TraceEventRecord, UsageRecord
from .db.orgs import get_org_by_name
from .db_session import get_db
from .run_analytics import TraceEventLite, agent_timings, common_failure_points, run_duration_seconds

router = APIRouter(
    prefix="/api/admin/analytics",
    tags=["admin-analytics"],
    dependencies=[Depends(get_current_admin)],
)

# Mirrors ui/frontend/src/lib/traceEvents.ts's TERMINAL_TYPES -- the event
# whose created_at marks when a run actually finished (Run has no
# completed_at column of its own).
_TERMINAL_TYPES = ("run_completed", "run_failed", "run_cancelled")

# Bounded sample for the failure-point tally -- same bounding philosophy as
# GET /api/runs' limit/offset (an unbounded scan would grow without limit as
# a workflow's failure history accumulates).
_FAILED_SAMPLE_LIMIT = 500


def _resolve_org_filter(db: Session, org: Optional[str]) -> Optional[int]:
    """None (omitted) = cross-org; an unknown name is a 404 -- mirrors
    `GET /api/runs`' cross-org admin mode and `crud.py`'s `_resolve_org_id`."""
    if org is None:
        return None
    org_row = get_org_by_name(db, org)
    if org_row is None:
        raise HTTPException(status_code=404, detail=f"Unknown organization '{org}'")
    return org_row.id


def _scoped_runs(
    db: Session,
    *,
    org_id: Optional[int],
    workflow: Optional[str],
    since: Optional[datetime],
    until: Optional[datetime],
) -> List[Run]:
    query = db.query(Run)
    if org_id is not None:
        query = query.filter(Run.org_id == org_id)
    if workflow is not None:
        query = query.filter(Run.workflow == workflow)
    if since is not None:
        query = query.filter(Run.created_at >= since)
    if until is not None:
        query = query.filter(Run.created_at <= until)
    return query.all()


def _terminal_event_by_run(db: Session, run_ids: List[str]) -> Dict[str, datetime]:
    if not run_ids:
        return {}
    rows = (
        db.query(TraceEventRecord.run_id, TraceEventRecord.created_at)
        .filter(TraceEventRecord.run_id.in_(run_ids), TraceEventRecord.type.in_(_TERMINAL_TYPES))
        .all()
    )
    return {row.run_id: row.created_at for row in rows}


def _events_by_run(db: Session, run_ids: List[str]) -> Dict[str, List[TraceEventLite]]:
    if not run_ids:
        return {}
    rows = (
        db.query(TraceEventRecord)
        .filter(TraceEventRecord.run_id.in_(run_ids))
        .order_by(TraceEventRecord.run_id, TraceEventRecord.seq)
        .all()
    )
    by_run: Dict[str, List[TraceEventLite]] = defaultdict(list)
    for row in rows:
        by_run[row.run_id].append(TraceEventLite(seq=row.seq, type=row.type, agent=row.agent, created_at=row.created_at))
    return by_run


@router.get("/workflows")
def list_workflow_analytics(
    org: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    db: Session = Depends(get_db),
) -> Dict[str, List[Dict[str, Any]]]:
    """One summary row per `(org, workflow)` pair. Grouped by
    `(org_id, workflow)`, never workflow name alone -- `Run.workflow` is
    only unique per org, so two orgs' same-named workflows would otherwise
    be silently conflated."""
    org_id = _resolve_org_filter(db, org)
    runs = _scoped_runs(db, org_id=org_id, workflow=None, since=since, until=until)

    org_ids = {r.org_id for r in runs if r.org_id is not None}
    org_names = (
        {o.id: o.name for o in db.query(Organization).filter(Organization.id.in_(org_ids))} if org_ids else {}
    )
    terminal_at = _terminal_event_by_run(db, [r.id for r in runs])

    groups: Dict[tuple, List[Run]] = defaultdict(list)
    for run in runs:
        groups[(run.org_id, run.workflow)].append(run)

    summaries = []
    for (group_org_id, workflow), group_runs in groups.items():
        statuses = Counter(r.status for r in group_runs)
        total = len(group_runs)
        durations = [
            d
            for r in group_runs
            if (d := run_duration_seconds(r.created_at, terminal_at.get(r.id))) is not None
        ]
        summaries.append(
            {
                "org_id": group_org_id,
                "org": org_names.get(group_org_id),
                "workflow": workflow,
                "total_runs": total,
                "completed": statuses.get("completed", 0),
                "failed": statuses.get("failed", 0),
                "cancelled": statuses.get("cancelled", 0),
                "running": statuses.get("running", 0),
                "success_rate": (statuses.get("completed", 0) / total) if total else None,
                "avg_duration_seconds": (sum(durations) / len(durations)) if durations else None,
            }
        )
    summaries.sort(key=lambda s: (s["org"] or "", s["workflow"]))
    return {"workflows": summaries}


@router.get("/workflows/{name}")
def get_workflow_analytics(
    name: str,
    org: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Per-agent token/cost/timing breakdown and common failure points for
    one `(org, workflow)`. `org` is required once more than one org
    actually has a workflow with this name -- otherwise ambiguous."""
    candidate_runs = _scoped_runs(db, org_id=None, workflow=name, since=since, until=until)
    distinct_orgs = {r.org_id for r in candidate_runs}
    if org is None:
        if len(distinct_orgs) > 1:
            raise HTTPException(
                status_code=422,
                detail=f"Multiple organizations have a workflow named '{name}'; pass ?org= to disambiguate",
            )
        org_id = next(iter(distinct_orgs), None)
        runs = candidate_runs
    else:
        org_id = _resolve_org_filter(db, org)
        runs = [r for r in candidate_runs if r.org_id == org_id]

    run_ids = [r.id for r in runs]
    usage_rows = db.query(UsageRecord).filter(UsageRecord.run_id.in_(run_ids)).all() if run_ids else []

    usage_by_agent: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"input": [], "output": [], "cost": [], "runs": set()})
    for u in usage_rows:
        if not u.agent:
            continue
        bucket = usage_by_agent[u.agent]
        bucket["input"].append(u.input_tokens)
        bucket["output"].append(u.output_tokens)
        bucket["runs"].add(u.run_id)
        if u.cost_estimate is not None:
            bucket["cost"].append(u.cost_estimate)

    events_by_run = _events_by_run(db, run_ids)
    timing_by_agent: Dict[str, List[float]] = defaultdict(list)
    for events in events_by_run.values():
        for agent, seconds in agent_timings(events).items():
            timing_by_agent[agent].append(seconds)

    def _avg(values: List[float]) -> Optional[float]:
        return (sum(values) / len(values)) if values else None

    agent_names = sorted(set(usage_by_agent) | set(timing_by_agent))
    per_agent = [
        {
            "agent": agent,
            "run_count": len(usage_by_agent[agent]["runs"]),
            "avg_input_tokens": _avg(usage_by_agent[agent]["input"]),
            "avg_output_tokens": _avg(usage_by_agent[agent]["output"]),
            "avg_cost_estimate": _avg(usage_by_agent[agent]["cost"]),
            "avg_duration_seconds": _avg(timing_by_agent.get(agent, [])),
        }
        for agent in agent_names
    ]

    failed_run_ids = [
        r.id
        for r in sorted((r for r in runs if r.status == "failed"), key=lambda r: r.created_at, reverse=True)[
            :_FAILED_SAMPLE_LIMIT
        ]
    ]
    failed_events = [events_by_run[rid] for rid in failed_run_ids if rid in events_by_run]

    return {
        "org_id": org_id,
        "workflow": name,
        "per_agent": per_agent,
        "common_failure_points": common_failure_points(failed_events),
    }
