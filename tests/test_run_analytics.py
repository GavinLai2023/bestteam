"""Unit tests for the pure aggregation helpers in ui/backend/run_analytics.py
-- no DB, no FastAPI, just plain data."""

from datetime import datetime, timedelta, timezone

from ui.backend.run_analytics import TraceEventLite, agent_timings, common_failure_points, run_duration_seconds


def _t(seconds: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_agent_timings_uses_consecutive_checkpoint_gaps_not_buffered_subevents():
    # A node's buffered sub-events (agent_started/tool_started/tool_completed)
    # all land at nearly the same timestamp, right before agent_completed --
    # the real signal is the gap between consecutive checkpoints.
    events = [
        TraceEventLite(seq=0, type="run_started", agent=None, created_at=_t(0)),
        TraceEventLite(seq=1, type="agent_started", agent="agent-a", created_at=_t(10)),
        TraceEventLite(seq=2, type="tool_started", agent="agent-a", created_at=_t(10)),
        TraceEventLite(seq=3, type="tool_completed", agent="agent-a", created_at=_t(10)),
        TraceEventLite(seq=4, type="agent_completed", agent="agent-a", created_at=_t(10)),
        TraceEventLite(seq=5, type="agent_started", agent="agent-b", created_at=_t(15)),
        TraceEventLite(seq=6, type="agent_completed", agent="agent-b", created_at=_t(25)),
    ]

    assert agent_timings(events) == {"agent-a": 10.0, "agent-b": 15.0}


def test_agent_timings_averages_repeated_agent_turns():
    events = [
        TraceEventLite(seq=0, type="run_started", agent=None, created_at=_t(0)),
        TraceEventLite(seq=1, type="agent_completed", agent="a", created_at=_t(4)),
        TraceEventLite(seq=2, type="agent_completed", agent="a", created_at=_t(10)),
    ]

    assert agent_timings(events) == {"a": (4.0 + 6.0) / 2}


def test_agent_timings_ignores_events_with_no_agent():
    events = [
        TraceEventLite(seq=0, type="run_started", agent=None, created_at=_t(0)),
        TraceEventLite(seq=1, type="run_completed", agent=None, created_at=_t(5)),
    ]

    assert agent_timings(events) == {}


def test_common_failure_points_takes_second_to_last_event_per_failed_run():
    run_1 = [
        TraceEventLite(seq=0, type="run_started", agent=None, created_at=_t(0)),
        TraceEventLite(seq=1, type="tool_completed", agent="a", created_at=_t(1)),
        TraceEventLite(seq=2, type="run_failed", agent=None, created_at=_t(2)),
    ]
    run_2 = [
        TraceEventLite(seq=0, type="run_started", agent=None, created_at=_t(0)),
        TraceEventLite(seq=1, type="tool_completed", agent="a", created_at=_t(1)),
        TraceEventLite(seq=2, type="run_failed", agent=None, created_at=_t(2)),
    ]
    run_3 = [
        TraceEventLite(seq=0, type="run_started", agent=None, created_at=_t(0)),
        TraceEventLite(seq=1, type="agent_started", agent="b", created_at=_t(1)),
        TraceEventLite(seq=2, type="run_failed", agent=None, created_at=_t(2)),
    ]

    points = common_failure_points([run_1, run_2, run_3])

    assert points[0] == {"agent": "a", "event_type": "tool_completed", "count": 2, "pct_of_failures": 2 / 3}
    assert points[1] == {"agent": "b", "event_type": "agent_started", "count": 1, "pct_of_failures": 1 / 3}


def test_common_failure_points_skips_runs_with_fewer_than_two_events():
    lone_event = [TraceEventLite(seq=0, type="run_failed", agent=None, created_at=_t(0))]

    assert common_failure_points([lone_event]) == []


def test_run_duration_seconds_none_without_terminal_event():
    assert run_duration_seconds(_t(0), None) is None


def test_run_duration_seconds():
    assert run_duration_seconds(_t(0), _t(30)) == 30.0
