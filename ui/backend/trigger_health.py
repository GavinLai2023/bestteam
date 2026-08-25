"""Decide when a trigger's health change is worth telling someone about.

Phase 0 made a failing trigger *visible* (`EmailTrigger.last_error`); nothing
ever *told* anyone, so a customer whose automation stopped a week ago found out
by opening the dashboard. This module holds the whole noise-control policy as
one pure function, so it can be tested by feeding it a sequence of outcomes and
asserting which notifications come out -- no database, no clock, no mailbox.

The policy in one line: **alerts fire on state transitions, not on
occurrences.** The most common way an alerting system fails is not missing an
event, it is generating enough noise that people mute it. A trigger remembers
which alerts are outstanding; a condition already alerted for stays quiet
until it clears.

*Which alerts*, plural: two domains can be broken at once. A single remembered
fingerprint meant a mailbox fault overwrote an outstanding pipeline one, and
the next successful mailbox check then cleared it and announced a recovery
that had not happened (Codex review finding). The outstanding set is stored in
the one `alerted_fingerprint` column as a sorted comma-separated string --
a single value parses as a one-element set, so nothing already stored needs
migrating.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Identifiers renamed Workflow -> Pipeline; the stored VALUES stay unchanged
# ("workflow_fault"/"workflow_ok"/"workflow") on purpose -- they're compared
# against EmailTrigger.alerted_fingerprint/Notification.fingerprint rows an
# older app version may have written, and changing them would need a backfill
# purely for cosmetic consistency. See db/models.py's matching note.
OUTCOME_PIPELINE = "workflow_fault"
OUTCOME_MAILBOX = "mailbox_fault"
OUTCOME_TIMEOUT = "run_timeout"
# Recovery is domain-specific on purpose. A successful mailbox check is direct
# proof that connectivity and credentials are fine; it says nothing about
# whether the team still builds and runs, so it must not clear a pipeline
# fault. That asymmetry is the existing `last_error_kind` convention (F5) and
# flattening it here would silently re-introduce the "healthy trigger, every
# run failing" state Phase 0 fixed.
OUTCOME_MAILBOX_OK = "mailbox_ok"
OUTCOME_PIPELINE_OK = "workflow_ok"

FINGERPRINT_PIPELINE = "workflow"
FINGERPRINT_MAILBOX = "mailbox"
FINGERPRINT_TIMEOUT = "run_timeout"
FINGERPRINT_BACKLOG = "backlog"

KIND_TRIGGER_HEALTH = "trigger_health"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_DEFAULT_THRESHOLD = 3
_THRESHOLD_ENV = "BESTTEAM_TRIGGER_ALERT_THRESHOLD"

_FAULT_FINGERPRINTS = {
    OUTCOME_PIPELINE: FINGERPRINT_PIPELINE,
    OUTCOME_MAILBOX: FINGERPRINT_MAILBOX,
    OUTCOME_TIMEOUT: FINGERPRINT_TIMEOUT,
}

# Which alerts each kind of success is entitled to clear. A completed run also
# clears a timeout alert: the wedge it reported is demonstrably gone.
_OK_CLEARS = {
    OUTCOME_MAILBOX_OK: frozenset({FINGERPRINT_MAILBOX}),
    OUTCOME_PIPELINE_OK: frozenset({FINGERPRINT_PIPELINE, FINGERPRINT_TIMEOUT}),
}

_RECOVERY_TITLES = {
    OUTCOME_MAILBOX_OK: "Your mailbox is reachable again",
    OUTCOME_PIPELINE_OK: "Automatic email replies are working again",
}


@dataclass(frozen=True)
class NotificationDraft:
    """What to tell the customer. Storage-agnostic: the caller persists it."""

    kind: str
    severity: str
    title: str
    body: str
    fingerprint: str


@dataclass(frozen=True)
class HealthDecision:
    """The trigger's new health state, plus a notification if one is due.

    `alerted_fingerprint` is the whole outstanding set, encoded for the
    column: sorted, comma-separated, or None when nothing is outstanding.
    """

    consecutive_faults: int
    alerted_fingerprint: Optional[str]
    notification: Optional[NotificationDraft]


def _parse(alerted_fingerprint: Optional[str]) -> set:
    """The outstanding set from the stored column. A pre-existing single
    value ("mailbox") parses as a one-element set, so old rows just work."""
    return {part for part in (alerted_fingerprint or "").split(",") if part}


def _encode(outstanding: set) -> Optional[str]:
    """Back to the column. Sorted, so the same set is always the same string
    and an unchanged state never looks like an update."""
    return ",".join(sorted(outstanding)) or None


def alert_threshold() -> int:
    """Consecutive faults before a pipeline/mailbox alert fires.

    Clamped to at least 1: a threshold of 0 would alert before anything had
    gone wrong, and a negative one would alert forever.
    """
    raw = os.environ.get(_THRESHOLD_ENV, "").strip()
    if not raw:
        return _DEFAULT_THRESHOLD
    try:
        return max(int(raw), 1)
    except ValueError:
        return _DEFAULT_THRESHOLD


def _draft(outcome: str, faults: int) -> NotificationDraft:
    if outcome == OUTCOME_TIMEOUT:
        return NotificationDraft(
            kind=KIND_TRIGGER_HEALTH,
            severity=SEVERITY_WARNING,
            title="An automatic run was stopped after taking too long",
            body=(
                "One automatic email run didn't finish in time, so it was "
                "stopped and automatic runs have resumed. If this keeps "
                "happening, the team may be waiting on a model or mailbox "
                "that isn't responding."
            ),
            fingerprint=FINGERPRINT_TIMEOUT,
        )
    if outcome == OUTCOME_MAILBOX:
        return NotificationDraft(
            kind=KIND_TRIGGER_HEALTH,
            severity=SEVERITY_ERROR,
            title="Your mailbox can't be reached",
            body=(
                f"The last {faults} attempts to check the mailbox failed, so "
                "no new mail is being processed. Check the email connection "
                "in your organisation's settings -- the most common causes "
                "are a changed password, an expired credential, or a revoked "
                "permission."
            ),
            fingerprint=FINGERPRINT_MAILBOX,
        )
    return NotificationDraft(
        kind=KIND_TRIGGER_HEALTH,
        severity=SEVERITY_ERROR,
        title="Automatic email replies are failing",
        body=(
            f"The last {faults} automatic runs failed, so no replies are "
            "being drafted. Open the run history to see what each run "
            "reported."
        ),
        fingerprint=FINGERPRINT_PIPELINE,
    )


def _recovery_draft(outcome: str) -> NotificationDraft:
    return NotificationDraft(
        kind=KIND_TRIGGER_HEALTH,
        severity=SEVERITY_INFO,
        title=_RECOVERY_TITLES[outcome],
        body="The problem reported earlier has cleared and automatic runs are succeeding.",
        fingerprint="recovered",
    )


def evaluate(
    *,
    outcome: str,
    consecutive_faults: int,
    alerted_fingerprint: Optional[str],
    threshold: int,
) -> HealthDecision:
    """Fold one outcome into a trigger's health state.

    `outcome` is one of the `OUTCOME_*` constants. The caller passes the
    trigger's current `consecutive_faults` / `alerted_fingerprint` and persists
    whatever comes back, appending `notification` when it is not `None`.

    A timeout alerts immediately rather than waiting for the threshold: by the
    time the watchdog sees it, the run has already been stuck for the full run
    timeout (30 minutes by default), so requiring three of them would mean an
    hour and a half of silence about a system that is plainly wedged.
    """
    faults = max(int(consecutive_faults or 0), 0)
    outstanding = _parse(alerted_fingerprint)

    clears = _OK_CLEARS.get(outcome)
    if clears is not None:
        cleared = outstanding & clears
        # The backlog alert is a *level*, not a fault (see `evaluate_backlog`):
        # it is invisible here, so it neither keeps a streak alive past a
        # success nor gets cleared by one -- only its own drain clears it.
        fault_outstanding = outstanding & set(_FAULT_FINGERPRINTS.values())
        if not fault_outstanding:
            # Nothing was reported, so there is nothing to announce -- but the
            # streak did end.
            return HealthDecision(0, _encode(outstanding), None)
        if cleared:
            # Only this domain's alerts clear. Anything else outstanding stays
            # outstanding, so it is neither announced as recovered nor
            # re-alerted from scratch later.
            return HealthDecision(0, _encode(outstanding - cleared), _recovery_draft(outcome))
        # An alert from the other domain is still outstanding: this success
        # does not speak to it, so leave both the alert and the streak alone.
        return HealthDecision(faults, _encode(outstanding), None)

    fingerprint = _FAULT_FINGERPRINTS.get(outcome)
    if fingerprint is None:
        # An outcome this module doesn't model changes nothing rather than
        # silently resetting a fault streak.
        return HealthDecision(faults, _encode(outstanding), None)

    faults += 1

    if outcome != OUTCOME_TIMEOUT and faults < max(int(threshold), 1):
        return HealthDecision(faults, _encode(outstanding), None)
    if fingerprint in outstanding:
        return HealthDecision(faults, _encode(outstanding), None)
    return HealthDecision(
        faults, _encode(outstanding | {fingerprint}), _draft(outcome, faults)
    )


@dataclass(frozen=True)
class BacklogDecision:
    """The trigger's new outstanding-alert set, plus a notification if due."""

    alerted_fingerprint: Optional[str]
    notification: Optional[NotificationDraft]


def evaluate_backlog(
    *,
    oldest_pending_seconds: Optional[float],
    threshold_seconds: float,
    alerted_fingerprint: Optional[str],
) -> BacklogDecision:
    """The backlog transition: a *level*, not a fault streak.

    Unlike `evaluate`'s outcomes, a backlog is measured, not counted -- the
    oldest `pending` inbox event's age. Mail waiting longer than the
    threshold alerts once; the backlog draining (or merely dropping back
    under the threshold) announces recovery once. `consecutive_faults` is
    untouched: nothing failed, dispatch is just not keeping up -- typically a
    cap or budget pause. In-flight (`claimed`) mail is deliberately not
    counted; a wedged run is the run-timeout alert's job.
    """
    outstanding = _parse(alerted_fingerprint)
    over = (
        oldest_pending_seconds is not None
        and oldest_pending_seconds > threshold_seconds
    )

    if over and FINGERPRINT_BACKLOG not in outstanding:
        minutes = int(oldest_pending_seconds // 60)
        return BacklogDecision(
            _encode(outstanding | {FINGERPRINT_BACKLOG}),
            NotificationDraft(
                kind=KIND_TRIGGER_HEALTH,
                severity=SEVERITY_WARNING,
                title="Incoming email is backing up",
                body=(
                    f"The oldest unprocessed email has been waiting about "
                    f"{max(minutes, 1)} minute(s). Automatic runs may be paused "
                    "by a daily or budget limit, or falling behind -- check the "
                    "automation settings and run history."
                ),
                fingerprint=FINGERPRINT_BACKLOG,
            ),
        )
    if not over and FINGERPRINT_BACKLOG in outstanding:
        return BacklogDecision(
            _encode(outstanding - {FINGERPRINT_BACKLOG}),
            NotificationDraft(
                kind=KIND_TRIGGER_HEALTH,
                severity=SEVERITY_INFO,
                title="The email backlog has cleared",
                body="Waiting email reported earlier is being processed again.",
                fingerprint="recovered",
            ),
        )
    return BacklogDecision(_encode(outstanding), None)
