"""Pure aggregation helpers for the admin pipeline-run analytics API
(`run_analytics_api.py`). Kept free of DB/FastAPI dependencies so the
tricky bits -- checkpoint-based per-agent timing, failure-point tallying --
are unit-testable against plain data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class TraceEventLite:
    """The subset of a persisted `TraceEventRecord` these helpers need."""

    seq: int
    type: str
    agent: Optional[str]
    created_at: datetime


def agent_timings(events: Sequence[TraceEventLite]) -> Dict[str, float]:
    """Average per-agent wall-clock seconds for one run's events (seq-ordered),
    derived from consecutive checkpoint events (`run_started` -> first
    `agent_completed`, each `agent_completed` -> the next).

    A node's *buffered* sub-events (`agent_started`/`tool_started`/
    `tool_completed`/`agent_progress`) are flushed together immediately
    before that node's `agent_completed` (see `adapters/langgraph_adapter.py`),
    so their own `created_at` deltas do NOT reflect real per-node compute
    time -- only the gap between consecutive checkpoints, each yielded
    individually right when a node actually finishes, is a trustworthy
    timing signal.
    """
    checkpoints = [e for e in events if e.type in ("run_started", "agent_completed")]
    per_agent: Dict[str, List[float]] = {}
    for prev, cur in zip(checkpoints, checkpoints[1:]):
        if cur.type != "agent_completed" or not cur.agent:
            continue
        delta = (cur.created_at - prev.created_at).total_seconds()
        per_agent.setdefault(cur.agent, []).append(delta)
    return {agent: sum(vals) / len(vals) for agent, vals in per_agent.items()}


def common_failure_points(failed_run_events: Sequence[Sequence[TraceEventLite]]) -> List[Dict[str, object]]:
    """Tally each failed run's second-to-last event (by `seq` -- the one
    immediately preceding its `run_failed` terminal event) by
    `(agent, type)`, across a bounded sample of recent failed runs. Cheaper
    and more testable than a SQLite JSON1 query over the opaque `data`
    column."""
    tally: Counter[Tuple[Optional[str], str]] = Counter()
    total = 0
    for events in failed_run_events:
        ordered = sorted(events, key=lambda e: e.seq)
        if len(ordered) < 2:
            continue
        culprit = ordered[-2]
        tally[(culprit.agent, culprit.type)] += 1
        total += 1
    return [
        {
            "agent": agent,
            "event_type": event_type,
            "count": count,
            "pct_of_failures": (count / total) if total else 0.0,
        }
        for (agent, event_type), count in tally.most_common()
    ]


def run_duration_seconds(run_created_at: datetime, terminal_event_created_at: Optional[datetime]) -> Optional[float]:
    """Seconds from a run's `created_at` to its terminal trace event's
    `created_at`. `Run` has no `completed_at` column of its own."""
    if terminal_event_created_at is None:
        return None
    return (terminal_event_created_at - run_created_at).total_seconds()
