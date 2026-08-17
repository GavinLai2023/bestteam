"""Decide when a trigger's health change is worth telling someone about.

Phase 0 made a failing trigger *visible* (`EmailTrigger.last_error`); nothing
ever *told* anyone, so a customer whose automation stopped a week ago found out
by opening the dashboard. This module holds the whole noise-control policy as
one pure function, so it can be tested by feeding it a sequence of outcomes and
asserting which notifications come out -- no database, no clock, no mailbox.

The policy in one line: **alerts fire on state transitions, not on
occurrences.** The most common way an alerting system fails is not missing an
event, it is generating enough noise that people mute it. A trigger remembers
the fingerprint of the alert most recently sent; a condition already alerted
for stays quiet until it clears.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

OUTCOME_WORKFLOW = "workflow_fault"
OUTCOME_MAILBOX = "mailbox_fault"
OUTCOME_TIMEOUT = "run_timeout"
# Recovery is domain-specific on purpose. A successful mailbox check is direct
# proof that connectivity and credentials are fine; it says nothing about
# whether the team still builds and runs, so it must not clear a workflow
# fault. That asymmetry is the existing `last_error_kind` convention (F5) and
# flattening it here would silently re-introduce the "healthy trigger, every
# run failing" state Phase 0 fixed.
OUTCOME_MAILBOX_OK = "mailbox_ok"
OUTCOME_WORKFLOW_OK = "workflow_ok"

FINGERPRINT_WORKFLOW = "workflow"
FINGERPRINT_MAILBOX = "mailbox"
FINGERPRINT_TIMEOUT = "run_timeout"

KIND_TRIGGER_HEALTH = "trigger_health"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_DEFAULT_THRESHOLD = 3
_THRESHOLD_ENV = "BESTTEAM_TRIGGER_ALERT_THRESHOLD"

_FAULT_FINGERPRINTS = {
    OUTCOME_WORKFLOW: FINGERPRINT_WORKFLOW,
    OUTCOME_MAILBOX: FINGERPRINT_MAILBOX,
    OUTCOME_TIMEOUT: FINGERPRINT_TIMEOUT,
}

# Which alerts each kind of success is entitled to clear. A completed run also
# clears a timeout alert: the wedge it reported is demonstrably gone.
_OK_CLEARS = {
    OUTCOME_MAILBOX_OK: frozenset({FINGERPRINT_MAILBOX}),
    OUTCOME_WORKFLOW_OK: frozenset({FINGERPRINT_WORKFLOW, FINGERPRINT_TIMEOUT}),
}

_RECOVERY_TITLES = {
    OUTCOME_MAILBOX_OK: "Your mailbox is reachable again",
    OUTCOME_WORKFLOW_OK: "Automatic email replies are working again",
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
    """The trigger's new health state, plus a notification if one is due."""

    consecutive_faults: int
    alerted_fingerprint: Optional[str]
    notification: Optional[NotificationDraft]


def alert_threshold() -> int:
    """Consecutive faults before a workflow/mailbox alert fires.

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
        fingerprint=FINGERPRINT_WORKFLOW,
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

    clears = _OK_CLEARS.get(outcome)
    if clears is not None:
        if alerted_fingerprint is None:
            # Nothing was reported, so there is nothing to announce -- but the
            # streak did end.
            return HealthDecision(0, None, None)
        if alerted_fingerprint in clears:
            return HealthDecision(0, None, _recovery_draft(outcome))
        # An alert from the other domain is still outstanding: this success
        # does not speak to it, so leave both the alert and the streak alone.
        return HealthDecision(faults, alerted_fingerprint, None)

    fingerprint = _FAULT_FINGERPRINTS.get(outcome)
    if fingerprint is None:
        # An outcome this module doesn't model changes nothing rather than
        # silently resetting a fault streak.
        return HealthDecision(faults, alerted_fingerprint, None)

    faults += 1

    if outcome == OUTCOME_TIMEOUT:
        if alerted_fingerprint == FINGERPRINT_TIMEOUT:
            return HealthDecision(faults, alerted_fingerprint, None)
        return HealthDecision(faults, FINGERPRINT_TIMEOUT, _draft(outcome, faults))

    if faults < max(int(threshold), 1):
        return HealthDecision(faults, alerted_fingerprint, None)
    if alerted_fingerprint == fingerprint:
        return HealthDecision(faults, alerted_fingerprint, None)
    return HealthDecision(faults, fingerprint, _draft(outcome, faults))
