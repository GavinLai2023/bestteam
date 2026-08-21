"""Pure aggregation for the customer-facing Activity Overview tab.

No I/O, no clock, no DB -- same shape as trigger_health.py/email_filter.py.
Takes the org's own run timestamps (already queried, already org-scoped) plus
`now` (injected, never `datetime.now()`, so results are reproducible in tests
and never drift because a caller forgot to pass real time).

Deliberately no model name, token count or cost anywhere here: this is an
engagement/achievement view for any org member, not the admin-only spend
surfaces (EmailBudgetSettings.tsx, run_analytics_api.py) -- how often the
org's teams ran, never what they ran on or what it cost.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, TypedDict

# A recent-weeks visual window for the calendar heatmap. The headline stats
# (sessions/active_days/streaks/peak_hour) are all-time and can exceed what
# this window shows, same as the reference dashboard's "Active days: 50"
# sitting above a heatmap that only has room for a few months.
HEATMAP_WEEKS = 12


class DailyCount(TypedDict):
    date: str
    count: int


class TeamCount(TypedDict):
    pipeline: str
    count: int


class OverviewStats(TypedDict):
    sessions: int
    active_days: int
    current_streak: int
    longest_streak: int
    peak_hour: Optional[int]
    daily_counts: List[DailyCount]
    completed_count: int
    team_counts: List[TeamCount]


def compute_overview(
    run_timestamps: List[datetime],
    *,
    now: datetime,
    heatmap_weeks: int = HEATMAP_WEEKS,
    # The pipeline name of each run whose status is "completed" -- one entry
    # per completed run, independent of run_timestamps (not index-parallel to
    # it): a failed/running run simply isn't in this list. This is the
    # accomplishment half of the tab ("your teams did N things for you"),
    # separate from sessions/streaks/heatmap above, which count every run
    # regardless of outcome (engagement, not accomplishment).
    completed_pipelines: Optional[List[str]] = None,
) -> OverviewStats:
    active_days: Set[date] = {ts.date() for ts in run_timestamps}
    today = now.date()

    hour_counts = Counter(ts.hour for ts in run_timestamps)
    # Ties break toward the earlier hour -- deterministic, and an early-morning
    # habit is as much a "peak" as a late one when counts are equal.
    peak_hour = max(hour_counts, key=lambda h: (hour_counts[h], -h)) if hour_counts else None

    window_start = today - timedelta(days=heatmap_weeks * 7 - 1)
    day_counts = Counter(ts.date() for ts in run_timestamps if ts.date() >= window_start)
    daily_counts: List[DailyCount] = [
        {
            "date": (window_start + timedelta(days=i)).isoformat(),
            "count": day_counts.get(window_start + timedelta(days=i), 0),
        }
        for i in range(heatmap_weeks * 7)
    ]

    completed_pipelines = completed_pipelines or []
    team_counts: List[TeamCount] = [
        {"pipeline": name, "count": count}
        for name, count in sorted(Counter(completed_pipelines).items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    return {
        "sessions": len(run_timestamps),
        "active_days": len(active_days),
        "current_streak": _current_streak(active_days, today),
        "longest_streak": _longest_streak(active_days),
        "peak_hour": peak_hour,
        "daily_counts": daily_counts,
        "completed_count": len(completed_pipelines),
        "team_counts": team_counts,
    }


def _current_streak(active_days: Set[date], today: date) -> int:
    # Lenient like a Duolingo-style streak: today not having a run yet
    # doesn't break yesterday's streak, since today isn't over.
    anchor = today if today in active_days else today - timedelta(days=1)
    if anchor not in active_days:
        return 0
    streak = 0
    day = anchor
    while day in active_days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def _longest_streak(active_days: Set[date]) -> int:
    longest = 0
    current = 0
    prev: Optional[date] = None
    for day in sorted(active_days):
        current = current + 1 if prev is not None and day == prev + timedelta(days=1) else 1
        longest = max(longest, current)
        prev = day
    return longest
