"""The pure engagement-stats aggregator behind the customer-facing Activity
Overview tab. No I/O, no clock, no DB -- same shape as test_trigger_health.py
and test_email_filter.py: `now` is injected so every streak/peak-hour rule is
pinned exhaustively and reproducibly, never by the wall clock.

Deliberately no model name, token count or cost anywhere in this module --
see MonitorPage.tsx's "Not Found" fix and EmailBudgetSettings.tsx for why
those stay admin-only. This dashboard is engagement data only: how often the
org's teams ran, not what they ran on or what it cost.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ui.backend.activity_overview import HEATMAP_WEEKS, compute_overview

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)  # a Thursday


def _at(days_ago: int, hour: int = 12) -> datetime:
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)


def test_no_runs_is_a_clean_zero_state():
    stats = compute_overview([], now=NOW)

    assert stats["sessions"] == 0
    assert stats["active_days"] == 0
    assert stats["current_streak"] == 0
    assert stats["longest_streak"] == 0
    assert stats["peak_hour"] is None
    assert len(stats["daily_counts"]) == HEATMAP_WEEKS * 7
    assert all(day["count"] == 0 for day in stats["daily_counts"])


def test_sessions_counts_every_run_including_same_day_repeats():
    stats = compute_overview([_at(0, 9), _at(0, 14), _at(1)], now=NOW)

    assert stats["sessions"] == 3
    assert stats["active_days"] == 2


def test_current_streak_counts_consecutive_days_up_to_today():
    stats = compute_overview([_at(0), _at(1), _at(2)], now=NOW)

    assert stats["current_streak"] == 3


def test_current_streak_is_lenient_when_todays_run_hasnt_happened_yet():
    # Today isn't over -- a run yesterday (and the day before) still counts
    # as an unbroken streak, same leniency Duolingo-style streaks use.
    stats = compute_overview([_at(1), _at(2)], now=NOW)

    assert stats["current_streak"] == 2


def test_current_streak_breaks_after_a_two_day_gap():
    stats = compute_overview([_at(2), _at(3)], now=NOW)

    assert stats["current_streak"] == 0


def test_longest_streak_is_the_all_time_best_even_if_the_current_one_is_shorter():
    # A 5-day streak two weeks ago, a gap, then today alone.
    old_streak = [_at(20), _at(19), _at(18), _at(17), _at(16)]
    stats = compute_overview(old_streak + [_at(0)], now=NOW)

    assert stats["longest_streak"] == 5
    assert stats["current_streak"] == 1


def test_peak_hour_is_the_most_frequent_hour():
    stats = compute_overview([_at(0, 9), _at(1, 9), _at(2, 14)], now=NOW)

    assert stats["peak_hour"] == 9


def test_peak_hour_ties_break_toward_the_earlier_hour():
    stats = compute_overview([_at(0, 9), _at(1, 14)], now=NOW)

    assert stats["peak_hour"] == 9


def test_daily_counts_spans_the_heatmap_window_ending_today():
    stats = compute_overview([_at(0), _at(HEATMAP_WEEKS * 7 - 1)], now=NOW)

    assert stats["daily_counts"][0]["date"] == (NOW.date() - timedelta(days=HEATMAP_WEEKS * 7 - 1)).isoformat()
    assert stats["daily_counts"][-1]["date"] == NOW.date().isoformat()
    assert stats["daily_counts"][0]["count"] == 1
    assert stats["daily_counts"][-1]["count"] == 1


def test_a_run_older_than_the_heatmap_window_still_counts_toward_sessions_and_streaks():
    # The heatmap is a recent-weeks visual; sessions/active_days/streaks are
    # all-time, same as the reference dashboard's headline numbers exceeding
    # what the visible grid could show.
    ancient = _at(HEATMAP_WEEKS * 7 + 30)
    stats = compute_overview([ancient], now=NOW)

    assert stats["sessions"] == 1
    assert stats["active_days"] == 1
    assert all(day["count"] == 0 for day in stats["daily_counts"])


def test_completed_count_and_team_counts_default_to_zero_when_omitted():
    # A caller that hasn't started passing which runs completed (or existing
    # tests above, unchanged) still gets a valid, empty accomplishment view
    # rather than a crash.
    stats = compute_overview([_at(0), _at(1)], now=NOW)

    assert stats["completed_count"] == 0
    assert stats["team_counts"] == []


def test_completed_count_counts_only_the_runs_marked_completed():
    # completed_pipelines is independent of run_timestamps -- it is exactly
    # the pipeline name of each run whose status is "completed", one entry
    # per completed run; a failed/running run simply isn't in this list.
    stats = compute_overview(
        [_at(0), _at(1), _at(2)], now=NOW, completed_pipelines=["support", "support"]
    )

    assert stats["completed_count"] == 2


def test_team_counts_breaks_completed_runs_down_by_pipeline_descending():
    stats = compute_overview(
        [], now=NOW, completed_pipelines=["a", "b", "a", "a", "b"]
    )

    assert stats["team_counts"] == [
        {"pipeline": "a", "count": 3},
        {"pipeline": "b", "count": 2},
    ]


def test_team_counts_ties_break_alphabetically_for_determinism():
    stats = compute_overview([], now=NOW, completed_pipelines=["b", "a"])

    assert stats["team_counts"] == [
        {"pipeline": "a", "count": 1},
        {"pipeline": "b", "count": 1},
    ]
