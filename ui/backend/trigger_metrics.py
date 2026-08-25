"""Operator-facing health metrics for the email trigger, from rows it writes.

Everything here is computed from what the poller already persists
(`EmailTrigger.last_checked_at`, `inbox_events` timestamps) -- no new
instrumentation, no in-process counters that a restart would zero.

Two consumers. The poll cycle reads `oldest_pending_seconds` to drive the
in-app backlog alert (the transition itself lives in `trigger_health`, with
the rest of the noise policy). The `check-health` CLI reads `collect` +
`evaluate` for the FAIL/WARN/OK checklist -- and that CLI is deliberately the
ONLY watcher for a stalled poller: notifications are delivered *by* the poll
loop (`run_maintenance`), so an in-process alert about the poller being
wedged could never leave the process. Cron `check-health` from outside; its
exit code is the pager.
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from .db.models import EmailTrigger, InboxEvent, Organization
from .env_check import Finding

BACKLOG_ALERT_MINUTES_ENV = "BESTTEAM_BACKLOG_ALERT_MINUTES"
_DEFAULT_BACKLOG_MINUTES = 30
# A stalled poller is lag beyond this many poll intervals...
_STALL_INTERVALS = 3
# ...but never less than this, so a short dev interval doesn't page.
_MIN_STALL_SECONDS = 300.0

_WINDOW = timedelta(hours=24)


def backlog_alert_seconds() -> float:
    """How long the oldest waiting email may sit before the backlog alert.

    Clamped to at least one minute: below that, ordinary dispatch latency
    (one poll interval) would alert on every cycle.
    """
    raw = os.environ.get(BACKLOG_ALERT_MINUTES_ENV, "").strip()
    try:
        minutes = int(raw) if raw else _DEFAULT_BACKLOG_MINUTES
    except ValueError:
        minutes = _DEFAULT_BACKLOG_MINUTES
    return float(max(minutes, 1)) * 60


@dataclass(frozen=True)
class OrgTriggerMetrics:
    """One enabled trigger's health numbers, all in seconds."""

    org_id: int
    org_name: str
    poll_lag_seconds: Optional[float]  # None until the first completed check
    oldest_pending_seconds: Optional[float]  # None when nothing is waiting
    pending_count: int
    done_24h: int
    failed_24h: int
    latency_p50_seconds: Optional[float]  # detected -> completed, done only
    latency_max_seconds: Optional[float]


def _as_utc(dt: datetime) -> datetime:
    # SQLite round-trips the poller's aware writes as naive UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _age(now: datetime, dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return (now - _as_utc(dt)).total_seconds()


def oldest_pending_seconds(db: Session, org_id: int, now: datetime) -> Optional[float]:
    """Age of the oldest `pending` inbox event, or None when nothing waits.

    `pending` only: `claimed` mail is in-flight and covered by the
    run-timeout alert, and counting it would cry "backing up" while a
    recovered outage is actively draining.
    """
    oldest = (
        db.query(InboxEvent.detected_at)
        .filter(InboxEvent.org_id == org_id, InboxEvent.status == "pending")
        .order_by(InboxEvent.detected_at)
        .first()
    )
    return _age(now, oldest[0] if oldest else None)


def collect(db: Session, now: Optional[datetime] = None) -> List[OrgTriggerMetrics]:
    """One metrics row per org with an enabled trigger."""
    now = now or datetime.now(timezone.utc)
    since = now - _WINDOW
    out: List[OrgTriggerMetrics] = []
    rows = (
        db.query(EmailTrigger, Organization.name)
        .join(Organization, Organization.id == EmailTrigger.org_id)
        .filter(EmailTrigger.enabled.is_(True))
        .order_by(Organization.name)
        .all()
    )
    for trigger, org_name in rows:
        pending = (
            db.query(InboxEvent.detected_at)
            .filter(InboxEvent.org_id == trigger.org_id, InboxEvent.status == "pending")
            .all()
        )
        window = (
            db.query(InboxEvent.status, InboxEvent.detected_at, InboxEvent.completed_at)
            .filter(
                InboxEvent.org_id == trigger.org_id,
                InboxEvent.status.in_(("done", "failed")),
                InboxEvent.completed_at.isnot(None),
            )
            .all()
        )
        recent = [row for row in window if _as_utc(row[2]) >= since]
        latencies = sorted(
            (_as_utc(completed) - _as_utc(detected)).total_seconds()
            for status, detected, completed in recent
            if status == "done"
        )
        pending_ages = [_age(now, row[0]) for row in pending]
        out.append(
            OrgTriggerMetrics(
                org_id=trigger.org_id,
                org_name=org_name,
                poll_lag_seconds=_age(now, trigger.last_checked_at),
                oldest_pending_seconds=max(pending_ages) if pending_ages else None,
                pending_count=len(pending),
                done_24h=len(latencies),
                failed_24h=sum(1 for row in recent if row[0] == "failed"),
                latency_p50_seconds=statistics.median(latencies) if latencies else None,
                latency_max_seconds=latencies[-1] if latencies else None,
            )
        )
    return out


def evaluate(
    metrics: List[OrgTriggerMetrics],
    *,
    poll_interval_seconds: float,
    backlog_threshold_seconds: float,
) -> List[Finding]:
    """The FAIL/WARN/OK checklist over collected metrics. Pure."""
    if not metrics:
        return [Finding("OK", "triggers", "no org has automatic email runs enabled")]

    stall = max(_STALL_INTERVALS * poll_interval_seconds, _MIN_STALL_SECONDS)
    out: List[Finding] = []
    for m in metrics:
        name = f"poll[{m.org_name}]"
        if m.poll_lag_seconds is None:
            out.append(Finding("WARN", name, "enabled but has not completed a "
                               "mailbox check yet"))
        elif m.poll_lag_seconds > stall:
            out.append(Finding("FAIL", name, f"last mailbox check was "
                               f"{m.poll_lag_seconds:.0f}s ago (interval "
                               f"{poll_interval_seconds:.0f}s) -- the poller "
                               "looks stalled or the process is down"))
        else:
            out.append(Finding("OK", name, f"checked {m.poll_lag_seconds:.0f}s ago"))

        name = f"backlog[{m.org_name}]"
        if (
            m.oldest_pending_seconds is not None
            and m.oldest_pending_seconds > backlog_threshold_seconds
        ):
            out.append(Finding("WARN", name, f"{m.pending_count} message(s) "
                               f"waiting; the oldest for "
                               f"{m.oldest_pending_seconds / 60:.0f} minute(s). "
                               "Dispatch may be paused by a cap or budget"))
        elif m.pending_count:
            out.append(Finding("OK", name, f"{m.pending_count} message(s) waiting, "
                               f"oldest {m.oldest_pending_seconds:.0f}s"))
        else:
            out.append(Finding("OK", name, "no messages waiting"))

        name = f"runs[{m.org_name}]"
        if m.failed_24h:
            out.append(Finding("WARN", name, f"{m.failed_24h} message(s) failed, "
                               f"{m.done_24h} completed in the last 24h -- see "
                               "the run history"))
        else:
            out.append(Finding("OK", name, f"{m.done_24h} message(s) completed, "
                               "none failed in the last 24h"))

        name = f"latency[{m.org_name}]"
        if m.latency_p50_seconds is not None:
            out.append(Finding("OK", name, f"detection to draft: p50 "
                               f"{m.latency_p50_seconds:.0f}s, max "
                               f"{m.latency_max_seconds:.0f}s over the last 24h"))
        else:
            out.append(Finding("OK", name, "no messages completed in the last 24h"))
    return out
