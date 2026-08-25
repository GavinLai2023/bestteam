"""Trigger health metrics: collection, threshold findings, and the backlog
alert transition.

Two homes on purpose. The backlog *transition* (when does a growing backlog
become a notification, when does draining it announce recovery) lives in
`trigger_health` with the rest of the noise-control policy and is tested
pure. Metric *collection* and the operator-facing FAIL/WARN/OK findings live
in `trigger_metrics` and are tested against a real in-memory database, the
same rows the poller writes.
"""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from ui.backend import admin, email_trigger, trigger_metrics
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_triggers import upsert_email_trigger
from ui.backend.db.models import InboxEvent, Notification
from ui.backend.db.orgs import get_or_create_org
from ui.backend.trigger_health import FINGERPRINT_BACKLOG, evaluate_backlog

pytestmark = pytest.mark.unit


# --- the pure backlog transition (trigger_health.evaluate_backlog) -----------


def test_backlog_over_the_threshold_alerts_once():
    first = evaluate_backlog(
        oldest_pending_seconds=3600, threshold_seconds=1800, alerted_fingerprint=None
    )
    assert first.alerted_fingerprint == FINGERPRINT_BACKLOG
    assert first.notification is not None
    assert first.notification.severity == "warning"
    assert first.notification.fingerprint == FINGERPRINT_BACKLOG

    second = evaluate_backlog(
        oldest_pending_seconds=7200,
        threshold_seconds=1800,
        alerted_fingerprint=first.alerted_fingerprint,
    )
    assert second.alerted_fingerprint == FINGERPRINT_BACKLOG
    assert second.notification is None


def test_backlog_draining_announces_recovery():
    drained = evaluate_backlog(
        oldest_pending_seconds=None,
        threshold_seconds=1800,
        alerted_fingerprint=FINGERPRINT_BACKLOG,
    )
    assert drained.alerted_fingerprint is None
    assert drained.notification is not None
    assert drained.notification.severity == "info"
    assert drained.notification.fingerprint == "recovered"


def test_backlog_below_the_threshold_also_clears():
    decision = evaluate_backlog(
        oldest_pending_seconds=10,
        threshold_seconds=1800,
        alerted_fingerprint=FINGERPRINT_BACKLOG,
    )
    assert decision.alerted_fingerprint is None
    assert decision.notification is not None


def test_backlog_leaves_other_outstanding_alerts_alone():
    over = evaluate_backlog(
        oldest_pending_seconds=3600, threshold_seconds=1800, alerted_fingerprint="mailbox"
    )
    assert over.alerted_fingerprint == "backlog,mailbox"

    drained = evaluate_backlog(
        oldest_pending_seconds=None,
        threshold_seconds=1800,
        alerted_fingerprint=over.alerted_fingerprint,
    )
    assert drained.alerted_fingerprint == "mailbox"
    assert drained.notification is not None
    assert drained.notification.fingerprint == "recovered"


def test_no_backlog_and_nothing_outstanding_changes_nothing():
    decision = evaluate_backlog(
        oldest_pending_seconds=None, threshold_seconds=1800, alerted_fingerprint=None
    )
    assert decision.alerted_fingerprint is None
    assert decision.notification is None


def test_outstanding_backlog_does_not_block_a_success_from_resetting_the_streak():
    # A backlog alert is a level, not a fault: with only "backlog" outstanding,
    # a success must still reset the fault streak (and keep the backlog alert,
    # which only its own drain may clear).
    from ui.backend.trigger_health import OUTCOME_MAILBOX_OK, evaluate

    decision = evaluate(
        outcome=OUTCOME_MAILBOX_OK,
        consecutive_faults=2,
        alerted_fingerprint=FINGERPRINT_BACKLOG,
        threshold=3,
    )
    assert decision.consecutive_faults == 0
    assert decision.alerted_fingerprint == FINGERPRINT_BACKLOG
    assert decision.notification is None


# --- collection from real rows ----------------------------------------------


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    session = session_factory(engine)()
    yield session
    session.close()


def _now():
    return datetime.now(timezone.utc)


def _org_with_trigger(db, name="acme", *, enabled=True, checked_ago=None):
    org = get_or_create_org(db, name)
    trigger = upsert_email_trigger(
        db, org.id, pipeline_name="triage", enabled=enabled, last_uid=0, uidvalidity=1
    )
    if checked_ago is not None:
        trigger.last_checked_at = _now() - timedelta(seconds=checked_ago)
        db.commit()
    return org, trigger


def _event(db, org_id, uid, *, status="pending", detected_ago=0.0, completed_ago=None):
    completed = None
    if completed_ago is not None:
        completed = _now() - timedelta(seconds=completed_ago)
    db.add(
        InboxEvent(
            org_id=org_id,
            mailbox_identity="m",
            external_id=str(uid),
            status=status,
            detected_at=_now() - timedelta(seconds=detected_ago),
            completed_at=completed,
        )
    )
    db.commit()


def test_collect_reports_lag_backlog_and_window_stats(db):
    org, _ = _org_with_trigger(db, checked_ago=300)
    _event(db, org.id, 1, detected_ago=3600)
    _event(db, org.id, 2, detected_ago=60)
    _event(db, org.id, 3, status="done", detected_ago=500, completed_ago=100)
    _event(db, org.id, 4, status="failed", detected_ago=400, completed_ago=200)
    # Outside the 24h window: must not count.
    _event(db, org.id, 5, status="done", detected_ago=90000, completed_ago=89000)
    # Claimed is in-flight, not waiting: run-timeout alerts cover it instead.
    _event(db, org.id, 6, status="claimed", detected_ago=7200)

    (m,) = trigger_metrics.collect(db)
    assert m.org_name == "acme"
    assert 295 <= m.poll_lag_seconds <= 320
    assert m.pending_count == 2
    assert 3595 <= m.oldest_pending_seconds <= 3620
    assert m.done_24h == 1
    assert m.failed_24h == 1
    assert 395 <= m.latency_p50_seconds <= 405
    assert 395 <= m.latency_max_seconds <= 405


def test_collect_skips_disabled_triggers(db):
    _org_with_trigger(db, "off", enabled=False)
    assert trigger_metrics.collect(db) == []


def test_collect_skips_deactivated_orgs(db):
    # The poller excludes inactive orgs (full-suspend enforcement), so their
    # last_checked_at freezes; reporting on them would page "poller stalled"
    # for a customer an operator deliberately suspended.
    org, _ = _org_with_trigger(db, "suspended", checked_ago=10)
    org.active = False
    db.commit()
    assert trigger_metrics.collect(db) == []


def test_collect_before_the_first_check_has_no_lag(db):
    _org_with_trigger(db)
    (m,) = trigger_metrics.collect(db)
    assert m.poll_lag_seconds is None
    assert m.pending_count == 0
    assert m.oldest_pending_seconds is None


# --- threshold findings ------------------------------------------------------


def _metrics(**overrides):
    base = dict(
        org_id=1,
        org_name="acme",
        poll_lag_seconds=30.0,
        oldest_pending_seconds=None,
        pending_count=0,
        done_24h=3,
        failed_24h=0,
        latency_p50_seconds=40.0,
        latency_max_seconds=90.0,
    )
    base.update(overrides)
    return trigger_metrics.OrgTriggerMetrics(**base)


def _evaluate(metrics):
    return trigger_metrics.evaluate(
        metrics, poll_interval_seconds=120, backlog_threshold_seconds=1800
    )


def test_evaluate_healthy_org_is_all_ok():
    findings = _evaluate([_metrics()])
    assert findings and all(f.level == "OK" for f in findings)


def test_evaluate_stalled_poller_fails():
    # Stall threshold is 3x the interval with a 5-minute floor: 360s here.
    (finding,) = [f for f in _evaluate([_metrics(poll_lag_seconds=1000.0)])
                  if f.level == "FAIL"]
    assert finding.name == "poll[acme]"


def test_evaluate_lag_within_the_stall_threshold_is_ok():
    findings = _evaluate([_metrics(poll_lag_seconds=350.0)])
    assert all(f.level == "OK" for f in findings)


def test_evaluate_never_checked_warns():
    (finding,) = [f for f in _evaluate([_metrics(poll_lag_seconds=None)])
                  if f.level == "WARN"]
    assert finding.name == "poll[acme]"


def test_evaluate_old_backlog_warns():
    (finding,) = [
        f
        for f in _evaluate(
            [_metrics(oldest_pending_seconds=3600.0, pending_count=4)]
        )
        if f.level == "WARN"
    ]
    assert finding.name == "backlog[acme]"
    assert "4" in finding.message


def test_evaluate_failed_runs_warn():
    (finding,) = [f for f in _evaluate([_metrics(failed_24h=2)]) if f.level == "WARN"]
    assert finding.name == "runs[acme]"
    assert "2" in finding.message


def test_evaluate_no_enabled_triggers_is_a_single_ok():
    (finding,) = _evaluate([])
    assert finding.level == "OK"


# --- the poller wiring -------------------------------------------------------


def test_apply_backlog_health_alerts_once_then_recovers(db, monkeypatch):
    monkeypatch.setenv("BESTTEAM_BACKLOG_ALERT_MINUTES", "30")
    org, trigger = _org_with_trigger(db)
    _event(db, org.id, 1, detected_ago=3600)

    email_trigger._apply_backlog_health(db, trigger)
    email_trigger._apply_backlog_health(db, trigger)
    alerts = db.query(Notification).filter_by(fingerprint="backlog").all()
    assert len(alerts) == 1
    assert trigger.alerted_fingerprint == "backlog"

    event = db.query(InboxEvent).one()
    event.status = "done"
    db.commit()
    email_trigger._apply_backlog_health(db, trigger)
    assert trigger.alerted_fingerprint is None
    recoveries = db.query(Notification).filter_by(fingerprint="recovered").all()
    assert len(recoveries) == 1


def test_backlog_threshold_env_default_and_clamp(monkeypatch):
    monkeypatch.delenv("BESTTEAM_BACKLOG_ALERT_MINUTES", raising=False)
    assert trigger_metrics.backlog_alert_seconds() == 1800
    monkeypatch.setenv("BESTTEAM_BACKLOG_ALERT_MINUTES", "5")
    assert trigger_metrics.backlog_alert_seconds() == 300
    monkeypatch.setenv("BESTTEAM_BACKLOG_ALERT_MINUTES", "0")
    assert trigger_metrics.backlog_alert_seconds() == 60
    monkeypatch.setenv("BESTTEAM_BACKLOG_ALERT_MINUTES", "nonsense")
    assert trigger_metrics.backlog_alert_seconds() == 1800


# --- the check-health CLI ----------------------------------------------------


class _Factory:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False


def test_check_health_cli_prints_findings_and_exits_on_fail(db, monkeypatch, capsys):
    org, _ = _org_with_trigger(db, checked_ago=5000)
    _event(db, org.id, 1, detected_ago=3600)
    monkeypatch.setattr(admin, "_open_session", _Factory(db))

    rc = admin.main(["check-health"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL]" in out and "poll[acme]" in out
    assert "[WARN]" in out and "backlog[acme]" in out


def test_check_health_cli_all_ok_exits_zero(db, monkeypatch, capsys):
    _org_with_trigger(db, checked_ago=10)
    monkeypatch.setattr(admin, "_open_session", _Factory(db))

    rc = admin.main(["check-health"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[FAIL]" not in out


def test_check_health_cli_without_a_database_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BESTTEAM_DB_PATH", str(tmp_path / "absent.db"))
    rc = admin.main(["check-health"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no database" in out
    assert not (tmp_path / "absent.db").exists()
