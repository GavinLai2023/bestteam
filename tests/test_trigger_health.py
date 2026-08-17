"""The pure health evaluator: when does a fault become an alert?

Being pure, the whole noise-control policy is testable by folding a sequence
of outcomes and asserting which notifications come out.
"""

import pytest

from ui.backend.trigger_health import (
    OUTCOME_HEALTHY,
    OUTCOME_MAILBOX,
    OUTCOME_TIMEOUT,
    OUTCOME_WORKFLOW,
    alert_threshold,
    evaluate,
)

pytestmark = pytest.mark.unit


def _fold(outcomes, threshold=3):
    """Run a sequence through the evaluator, returning the notifications."""
    faults, fingerprint, emitted = 0, None, []
    for outcome in outcomes:
        decision = evaluate(
            outcome=outcome,
            consecutive_faults=faults,
            alerted_fingerprint=fingerprint,
            threshold=threshold,
        )
        faults = decision.consecutive_faults
        fingerprint = decision.alerted_fingerprint
        if decision.notification is not None:
            emitted.append(decision.notification)
    return emitted


def test_faults_below_the_threshold_stay_quiet():
    assert _fold([OUTCOME_WORKFLOW, OUTCOME_WORKFLOW]) == []


def test_reaching_the_threshold_alerts_once_and_then_stays_quiet():
    emitted = _fold([OUTCOME_WORKFLOW] * 6)
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "workflow"
    assert emitted[0].severity == "error"


def test_a_recovery_is_announced_and_resets_the_streak():
    emitted = _fold([OUTCOME_WORKFLOW] * 3 + [OUTCOME_HEALTHY])
    assert [n.fingerprint for n in emitted] == ["workflow", "recovered"]
    assert emitted[1].severity == "info"


def test_success_without_a_prior_alert_says_nothing():
    assert _fold([OUTCOME_HEALTHY, OUTCOME_HEALTHY]) == []


def test_a_brief_wobble_below_the_threshold_never_alerts():
    # Two failures then a success: the streak resets, nobody is told.
    assert _fold([OUTCOME_WORKFLOW, OUTCOME_WORKFLOW, OUTCOME_HEALTHY,
                  OUTCOME_WORKFLOW, OUTCOME_WORKFLOW]) == []


def test_a_timeout_alerts_immediately_regardless_of_the_threshold():
    emitted = _fold([OUTCOME_TIMEOUT], threshold=10)
    assert len(emitted) == 1
    assert emitted[0].fingerprint == "run_timeout"


def test_repeated_timeouts_alert_once():
    assert len(_fold([OUTCOME_TIMEOUT] * 4)) == 1


def test_a_changed_fault_kind_alerts_again():
    # Mailbox trouble after workflow trouble is a different problem with a
    # different fix, so suppressing it would hide the thing that matters.
    emitted = _fold([OUTCOME_WORKFLOW] * 3 + [OUTCOME_MAILBOX] * 3)
    assert [n.fingerprint for n in emitted] == ["workflow", "mailbox"]


def test_the_mailbox_alert_points_at_the_connection_settings():
    emitted = _fold([OUTCOME_MAILBOX] * 3)
    assert "email connection" in emitted[0].body
    assert emitted[0].title == "Your mailbox can't be reached"


def test_a_threshold_of_one_alerts_on_the_first_fault():
    assert len(_fold([OUTCOME_WORKFLOW], threshold=1)) == 1


def test_an_unmodelled_outcome_changes_nothing():
    decision = evaluate(
        outcome="something_new", consecutive_faults=2,
        alerted_fingerprint="workflow", threshold=3,
    )
    assert decision.consecutive_faults == 2
    assert decision.alerted_fingerprint == "workflow"
    assert decision.notification is None


def test_a_negative_stored_count_is_treated_as_zero():
    decision = evaluate(
        outcome=OUTCOME_WORKFLOW, consecutive_faults=-5,
        alerted_fingerprint=None, threshold=3,
    )
    assert decision.consecutive_faults == 1


@pytest.mark.parametrize("raw,expected", [
    ("", 3), ("5", 5), ("1", 1), ("0", 1), ("-2", 1), ("nonsense", 3),
])
def test_the_threshold_env_var_is_read_and_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("BESTTEAM_TRIGGER_ALERT_THRESHOLD", raw)
    assert alert_threshold() == expected


def test_the_threshold_defaults_to_three_when_unset(monkeypatch):
    monkeypatch.delenv("BESTTEAM_TRIGGER_ALERT_THRESHOLD", raising=False)
    assert alert_threshold() == 3
