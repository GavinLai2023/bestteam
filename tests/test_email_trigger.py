"""Unit tests for the autonomous email trigger (poller logic, no real IMAP)."""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from ui.backend import email_trigger
from ui.backend.email_trigger import check_mailbox, mailbox_state


class FakeConn:
    """Duck-types the slice of imaplib.IMAP4_SSL the trigger uses."""

    def __init__(self, uidvalidity=3, uidnext=46, search_uids=b"42 43 45"):
        self._status_line = (
            f'"INBOX" (UIDVALIDITY {uidvalidity} UIDNEXT {uidnext})'.encode()
        )
        self._search_uids = search_uids
        self.selected_readonly = None
        self.logged_out = False

    def status(self, mailbox, items):
        return "OK", [self._status_line]

    def select(self, mailbox, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"3"]

    def uid(self, command, *args):
        assert command == "search"
        return "OK", [self._search_uids]

    def logout(self):
        self.logged_out = True


class FakeBackend:
    def __init__(self, conn):
        self._conn = conn

    def _connect(self):
        return self._conn


def test_mailbox_state_parses_status():
    conn = FakeConn(uidvalidity=7, uidnext=100)
    assert mailbox_state(FakeBackend(conn)) == (7, 99)
    assert conn.logged_out is True


def test_check_mailbox_returns_new_uids_above_baseline():
    conn = FakeConn(uidvalidity=3, uidnext=46, search_uids=b"42 43 45")
    uidvalidity, max_uid, new = check_mailbox(FakeBackend(conn), last_uid=41)
    assert (uidvalidity, max_uid, new) == (3, 45, [42, 43, 45])
    assert conn.selected_readonly is True  # never marks mail seen


def test_check_mailbox_filters_the_imap_star_quirk():
    # IMAP "N:*" returns the highest-UID message even when N > max; results at
    # or below the baseline must be filtered out client-side.
    conn = FakeConn(uidvalidity=3, uidnext=46, search_uids=b"45")
    _, _, new = check_mailbox(FakeBackend(conn), last_uid=45)
    assert new == []


def test_check_mailbox_short_circuits_when_no_new_possible():
    conn = FakeConn(uidvalidity=3, uidnext=46)
    _, max_uid, new = check_mailbox(FakeBackend(conn), last_uid=45)
    assert (max_uid, new) == (45, [])
    assert conn.selected_readonly is None  # STATUS said nothing new: no SELECT


def test_mailbox_state_raises_oserror_on_garbage():
    conn = FakeConn()
    conn._status_line = b"unexpected"
    with pytest.raises(OSError):
        mailbox_state(FakeBackend(conn))


def test_mailbox_state_parses_regardless_of_field_order():
    # RFC 3501 doesn't guarantee UIDVALIDITY precedes UIDNEXT in the response.
    conn = FakeConn()
    conn._status_line = b'"INBOX" (UIDNEXT 46 UIDVALIDITY 3)'
    assert mailbox_state(FakeBackend(conn)) == (3, 45)


# --- poll_org: cap / baseline / bookkeeping / errors -------------------------

from datetime import datetime, timezone

from cryptography.fernet import Fernet

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import set_email_credentials
from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
from ui.backend.db.orgs import get_or_create_org
from ui.backend.email_trigger import daily_cap, poll_org


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    # `_declares_property_maintenance_contract` resolves `property_maintenance_
    # response_v1` against the actual platform SkillRecord (Codex review
    # finding) -- several tests below declare that name in a workflow config
    # and expect the contract to be recognized, which now requires the row
    # to actually exist.
    from ui.backend.skills import seed_default_skills
    seed_default_skills(session)
    yield session
    session.close()


def _org_with_trigger(db, *, last_uid=45, uidvalidity=3, enabled=True):
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="imap.acme.com", username="u@acme.com",
                          password="pw")
    trigger = upsert_email_trigger(db, org.id, workflow_name="triage",
                                   enabled=enabled, last_uid=last_uid,
                                   uidvalidity=uidvalidity)
    return org, trigger


def _no_workflow(name, db, org_id, allowed_uids, backend):  # must NOT be called
    raise AssertionError("build_trigger_workflow should not be called in this test")


def test_poll_org_no_new_mail_updates_health_only(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_checked_at is not None
    assert trigger.last_error is None
    assert trigger.last_uid == 45 and trigger.runs_today == 0


def test_poll_org_uidvalidity_change_rebaselines_without_running(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    # New validity 9, and the "new" mailbox reports max_uid 200 with new uids --
    # they must be skipped and the baseline reset instead.
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (9, 200, [199, 200]))
    poll_org(db, trigger, _no_workflow)
    assert trigger.uidvalidity == 9
    assert trigger.last_uid == 200
    assert trigger.runs_today == 0


def test_poll_org_cap_reached_skips_mailbox_entirely(db, monkeypatch):
    org, trigger = _org_with_trigger(db)
    trigger.runs_today = daily_cap()
    trigger.runs_date = datetime.now(timezone.utc).date().isoformat()
    db.commit()

    def _boom(backend, last_uid):
        raise AssertionError("must not touch the mailbox when capped")

    monkeypatch.setattr(email_trigger, "check_mailbox", _boom)
    poll_org(db, trigger, _no_workflow)  # must not raise


def test_poll_org_cap_resets_on_new_day(db, monkeypatch):
    org, trigger = _org_with_trigger(db)
    trigger.runs_today = daily_cap()
    trigger.runs_date = "2020-01-01"  # stale date -> counter must reset
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.runs_today == 0
    assert trigger.runs_date == datetime.now(timezone.utc).date().isoformat()


def test_poll_org_mailbox_failure_stores_friendly_error(db, monkeypatch):
    org, trigger = _org_with_trigger(db)

    def _fail(backend, last_uid):
        raise OSError("[WinError 10060] connection attempt failed")

    monkeypatch.setattr(email_trigger, "check_mailbox", _fail)
    poll_org(db, trigger, _no_workflow)  # must not raise
    assert trigger.last_error is not None
    assert "WinError" not in trigger.last_error  # no internals to customers
    assert trigger.last_checked_at is not None


def test_poll_org_mailbox_error_clears_on_next_successful_check(db, monkeypatch):
    # A resolved mailbox outage must not keep showing "error" forever -- only
    # a *workflow*-kind error must survive an empty poll (F5, unchanged).
    org, trigger = _org_with_trigger(db, last_uid=41)

    def _fail(backend, last_uid):
        raise OSError("[WinError 10060] connection attempt failed")

    monkeypatch.setattr(email_trigger, "check_mailbox", _fail)
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is not None
    assert trigger.last_error_kind == "mailbox"

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 41, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is None
    assert trigger.last_error_kind is None


def test_poll_org_workflow_error_kind_survives_empty_poll(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    trigger.last_error = "Couldn't start the team 'triage' -- it may have been removed."
    trigger.last_error_kind = "workflow"
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 41, []))  # no new mail
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is not None
    assert trigger.last_error_kind == "workflow"


# --- poll_org: the new-mail path ---------------------------------------------


class _SubmitRecorder:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


def _fake_workflow_getter(calls, version_id=None):
    def build(name, db, org_id, allowed_uids, backend):
        calls.append((name, org_id, set(allowed_uids)))
        return object(), version_id
    return build


def test_poll_org_new_mail_starts_one_run(db, monkeypatch):
    from ui.backend.db.workflows import current_version_id, publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41)
    _, version = publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    db.commit()

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 43, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    calls = []
    poll_org(db, trigger, _fake_workflow_getter(calls, version_id=version.id))

    assert calls == [("triage", org.id, {42, 43, 45})]  # scoped to the batch
    assert len(recorder.calls) == 1
    _, args, kwargs = recorder.calls[0]
    run_id, input_text = args[0], args[2]
    assert "42, 43, 45" in input_text
    assert kwargs["username"] == "email-trigger"
    # Durable run row exists BEFORE dispatch, stamped with the executed version.
    from ui.backend.db.models import Run
    run_row = db.get(Run, run_id)
    assert run_row is not None
    assert run_row.workflow_version_id == version.id == current_version_id(db, org.id, "triage")
    assert trigger.last_uid == 45 and trigger.runs_today == 1 and trigger.last_run_id == run_id


def test_poll_org_stamps_trigger_context_for_normalization(db, monkeypatch):
    """Run.trigger_context is the server's own record of exactly which
    mailbox/UIDVALIDITY/UID batch a triggered run covers -- automation result
    normalization and manual retry both depend on it (spec section 11.1)."""
    from ui.backend.db.email_credentials import get_email_credentials
    from ui.backend.db.models import Run

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    cred = get_email_credentials(db, org.id)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    poll_org(db, trigger, _fake_workflow_getter([]))

    run_id = recorder.calls[0][1][0]
    run_row = db.get(Run, run_id)
    assert run_row.trigger_context == {
        "trigger_type": "email",
        "mailbox_credential_id": cred.id,
        "mailbox_host": cred.host,
        "mailbox_username": cred.username,
        "uidvalidity": 3,
        "uids": [42, 45],
        "folder": "INBOX",
        "triggered_at": run_row.trigger_context["triggered_at"],  # timestamp, checked for presence only
    }
    assert run_row.trigger_context["triggered_at"]  # non-empty


def test_poll_org_stamps_result_contract_when_workflow_declares_the_maintenance_skill(db, monkeypatch):
    """automation_results._normalize needs a positive, persisted signal that a
    run belongs to the Property Maintenance Inbox vertical for the case where
    it crashes before producing any JSON output (indistinguishable, from the
    output alone, from any other org's unrelated email-trigger workflow) --
    trigger_context['result_contract'] is that signal, stamped from the
    deployed workflow's own config at dispatch time (Codex review finding)."""
    from ui.backend.db.models import Run
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["property_maintenance_response_v1"]}]},
    )
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    poll_org(db, trigger, _fake_workflow_getter([]))

    run_id = recorder.calls[0][1][0]
    run_row = db.get(Run, run_id)
    assert run_row.trigger_context["result_contract"] == "property_maintenance_email_batch"


def test_poll_org_does_not_stamp_result_contract_when_org_skill_shadows_the_platform_one(db, monkeypatch):
    """`load_skills` intentionally lets an org's own skill shadow a same-named
    platform built-in. A name-only check can't tell the two apart, so an org
    that names its own, unrelated skill `property_maintenance_response_v1`
    would otherwise get this run wrongly redacted and stamped with synthetic
    maintenance error rows (Codex review finding) -- the org-shadowed skill
    must NOT count as the platform contract."""
    from ui.backend.db.models import Run, SkillRecord
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    db.add(SkillRecord(
        name="property_maintenance_response_v1", org_id=org.id,
        config={"description": "unrelated org skill", "instructions": "do something else"},
    ))
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["property_maintenance_response_v1"]}]},
    )
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    poll_org(db, trigger, _fake_workflow_getter([]))

    run_id = recorder.calls[0][1][0]
    run_row = db.get(Run, run_id)
    assert "result_contract" not in run_row.trigger_context


def test_poll_org_does_not_stamp_result_contract_for_a_workflow_without_the_skill(db, monkeypatch):
    from ui.backend.db.models import Run
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["email_triage_reply"]}]},
    )
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    poll_org(db, trigger, _fake_workflow_getter([]))

    run_id = recorder.calls[0][1][0]
    run_row = db.get(Run, run_id)
    assert "result_contract" not in run_row.trigger_context


def test_triggered_run_stamps_builder_returned_version_not_a_requery(db, monkeypatch):
    """_start_triggered_run records the version the builder returned (bound to the
    config it built), NOT a fresh current_version_id re-query -- so a redeploy
    committing between build and stamp can't mislabel the run's version."""
    from ui.backend.db.workflows import current_version_id, publish_workflow_version
    from ui.backend.db.models import Run

    org, trigger = _org_with_trigger(db, last_uid=41)
    _, version = publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    db.commit()
    assert current_version_id(db, org.id, "triage") == version.id

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    # The builder reports a DIFFERENT version than the current pointer -- as if a
    # redeploy landed after the build read. The run must record the built one.
    stale = version.id + 999
    poll_org(db, trigger, _fake_workflow_getter([], version_id=stale))

    run_id = recorder.calls[0][1][0]
    run_row = db.get(Run, run_id)
    assert run_row.workflow_version_id == stale  # builder's value, not the re-query


def test_start_triggered_run_marks_run_failed_when_submit_raises(db, monkeypatch):
    from ui.backend.db.models import Run as RunRow
    from ui.backend.runtime import registry

    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))

    class _BoomExecutor:
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(email_trigger, "_executor", _BoomExecutor())

    poll_org(db, trigger, _fake_workflow_getter([]))

    run_id = trigger.last_run_id
    assert run_id is not None
    assert registry.get(run_id).status == "failed"
    assert db.get(RunRow, run_id).status == "failed"

    # The overlap guard must not wedge on this run afterward.
    calls = []

    def _track(b, u):
        calls.append(1)
        return (3, 45, [])

    monkeypatch.setattr(email_trigger, "check_mailbox", _track)
    poll_org(db, trigger, _no_workflow)
    assert calls == [1]  # mailbox was actually checked -- guard didn't wedge


def test_start_triggered_run_normalizes_a_declared_batch_when_submit_raises(db, monkeypatch):
    """The worker never starts when submission itself raises, so
    run_in_background's own normalization never runs either -- without an
    explicit normalize_run_result call here, a declared property-maintenance
    batch would be marked failed with no automation_item_results rows at all
    and silently vanish from Needs-attention (Codex review finding)."""
    from ui.backend.automation_results import AutomationItemResult
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["property_maintenance_response_v1"]}]},
    )
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))

    class _BoomExecutor:
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(email_trigger, "_executor", _BoomExecutor())

    poll_org(db, trigger, _fake_workflow_getter([]))

    run_id = trigger.last_run_id
    rows = db.query(AutomationItemResult).filter_by(run_id=run_id).all()
    assert len(rows) == 2  # one per UID in the batch (42, 45)
    assert all(r.status == "error" and r.needs_attention for r in rows)


def test_start_triggered_run_normalizes_before_publishing_run_failed_when_submit_raises(db, monkeypatch):
    """Same ordering guarantee as the normal terminal path (runtime.py): a
    live Run Detail view can react to run_failed the instant it's published,
    so normalize_run_result must commit BEFORE that publish, not after --
    otherwise it can fetch zero automation rows with no later terminal
    transition to prompt a re-fetch (Codex review finding)."""
    from ui.backend.automation_results import AutomationItemResult
    from ui.backend.db.workflows import publish_workflow_version
    from ui.backend.runtime import registry

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["property_maintenance_response_v1"]}]},
    )
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))

    class _BoomExecutor:
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(email_trigger, "_executor", _BoomExecutor())

    rows_seen_at_publish_time = []
    orig_publish = registry.publish

    def spy_publish(run_id, event):
        if event.get("type") == "run_failed":
            rows_seen_at_publish_time.append(
                db.query(AutomationItemResult).filter_by(run_id=run_id).count()
            )
        orig_publish(run_id, event)

    monkeypatch.setattr(registry, "publish", spy_publish)

    poll_org(db, trigger, _fake_workflow_getter([]))

    assert rows_seen_at_publish_time == [2]  # already committed before run_failed was published


def test_start_triggered_run_discards_if_disabled_mid_build(db, monkeypatch):
    # If the customer disconnects/replaces the mailbox WHILE this cycle's
    # workflow is being built, org_settings.py/admin.py disable the trigger
    # in a separate commit. A concurrent session is used here (StaticPool
    # shares one in-memory DB) to simulate that race precisely.
    from ui.backend.db import session_factory
    from ui.backend.db.models import EmailTrigger

    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    Session2 = session_factory(db.get_bind())

    def build(name, db_, org_id, allowed_uids, backend):
        with Session2() as db2:
            other = db2.query(EmailTrigger).filter_by(org_id=org_id).one()
            other.enabled = False
            db2.commit()
        return object(), None

    poll_org(db, trigger, build)

    assert recorder.calls == []      # never dispatched against the old mailbox
    assert trigger.runs_today == 0   # NO cap burned
    assert trigger.last_run_id is None
    # Released, not consumed -- the cursor advances at detection now, so the
    # ledger is what carries the "nothing was lost" guarantee.
    from ui.backend.db.models import InboxEvent
    assert all(e.status == "pending" and e.attempts == 0 for e in db.query(InboxEvent))


def test_start_triggered_run_discards_if_disabled_after_enabled_check(db, monkeypatch):
    # The mid-build test above disables BEFORE the poller's enabled-check. This
    # covers the narrower window the check-then-commit split left open: a disable
    # landing AFTER the check but before the run is committed. We inject it at
    # registry.create -- called inside that exact window -- by disabling from a
    # concurrent session there. The atomic compare-and-swap on `enabled` must
    # still refuse to advance the batch/cap or dispatch, and must leak no run.
    from ui.backend.db import session_factory
    from ui.backend.db.models import EmailTrigger

    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    Session2 = session_factory(db.get_bind())
    real_create = email_trigger.registry.create
    created_ids = []

    def create_then_disable(*a, **k):
        run = real_create(*a, **k)
        created_ids.append(run.id)
        with Session2() as db2:
            other = db2.query(EmailTrigger).filter_by(org_id=org.id).one()
            other.enabled = False
            db2.commit()
        return run

    monkeypatch.setattr(email_trigger.registry, "create", create_then_disable)

    poll_org(db, trigger, _fake_workflow_getter([]))

    assert recorder.calls == []       # never dispatched
    assert trigger.runs_today == 0    # cap NOT burned
    assert trigger.last_run_id is None
    assert email_trigger.registry.get(created_ids[0]) is None  # no leaked run
    # The messages are released rather than consumed: they stay queued for
    # whenever the trigger is re-enabled, with no attempt charged.
    from ui.backend.db.models import InboxEvent
    rows = db.query(InboxEvent).all()
    assert all(e.status == "pending" and e.attempts == 0 for e in rows)


def test_poll_org_reuses_the_same_backend_for_workflow_build(db, monkeypatch):
    # Fix 1: poll_org must not let workflow-building re-fetch credentials --
    # a credential change mid-cycle must not produce a run that detects mail
    # on one mailbox and builds tools against another.
    org, trigger = _org_with_trigger(db, last_uid=41)
    seen_check_backends = []

    def fake_check_mailbox(backend, last_uid):
        seen_check_backends.append(backend)
        return (3, 45, [42, 45])

    monkeypatch.setattr(email_trigger, "check_mailbox", fake_check_mailbox)
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    seen_workflow_backends = []

    def build(name, db_, org_id, allowed_uids, backend):
        seen_workflow_backends.append(backend)
        return object(), None

    poll_org(db, trigger, build)

    assert len(seen_check_backends) == 1
    assert len(seen_workflow_backends) == 1
    assert seen_workflow_backends[0] is seen_check_backends[0]


def test_poll_org_dispatch_clears_prior_error(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    trigger.last_error = "some prior fault"
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    poll_org(db, trigger, _fake_workflow_getter([]))
    assert len(recorder.calls) == 1          # a run was dispatched
    assert trigger.last_error is None        # prior fault cleared on dispatch


def test_poll_org_bounded_batch_carries_remainder(db, monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "2")
    org, trigger = _org_with_trigger(db, last_uid=40)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [41, 42, 43, 44, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    calls = []
    poll_org(db, trigger, _fake_workflow_getter(calls))
    assert calls[0][2] == {41, 42}          # oldest 2 only
    assert trigger.runs_today == 1
    # The remainder is now carried by the durable ledger rather than by leaving
    # the cursor behind: detection records every message it saw and advances
    # past all of them, and the un-claimed ones stay pending for the next cycle.
    from ui.backend.db.models import InboxEvent
    pending = {
        e.external_id for e in db.query(InboxEvent).filter(InboxEvent.status == "pending")
    }
    assert pending == {"43", "44", "45"}
    assert trigger.last_uid == 45


def test_poll_org_build_failure_advances_nothing(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    def _boom(name, db_, org_id, allowed_uids, backend):
        raise ValueError("No team named 'triage'")

    poll_org(db, trigger, _boom)
    assert recorder.calls == []
    assert trigger.runs_today == 0         # NO cap burned
    assert trigger.last_error is not None and "triage" in trigger.last_error
    # "No message consumed" is now enforced by the ledger, not by holding the
    # cursor back: both messages are released to pending with no attempt
    # charged, so a broken team retries them until it is fixed.
    from ui.backend.db.models import InboxEvent
    rows = db.query(InboxEvent).all()
    assert {e.external_id for e in rows} == {"42", "45"}
    assert all(e.status == "pending" and e.attempts == 0 for e in rows)


def test_poll_org_workflow_error_survives_empty_poll(db, monkeypatch):
    # F5: a workflow fault must not be cleared by a later successful empty poll.
    org, trigger = _org_with_trigger(db, last_uid=41)
    trigger.last_error = "Couldn't start the team 'triage' -- it may have been removed."
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 41, []))  # no new mail
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_checked_at is not None
    assert trigger.last_error is not None   # NOT cleared by an empty successful poll


def test_poll_org_skips_while_previous_run_still_running(db, monkeypatch):
    from ui.backend.runtime import registry

    org, trigger = _org_with_trigger(db, last_uid=41)
    run = registry.create("triage", "x", org_id=org.id, username="email-trigger")
    trigger.last_run_id = run.id
    db.commit()

    calls = []

    def _track(backend, last_uid):
        calls.append(last_uid)
        return (3, 41, [])

    monkeypatch.setattr(email_trigger, "check_mailbox", _track)
    poll_org(db, trigger, _no_workflow)
    assert calls == []  # mailbox never touched
    assert trigger.last_uid == 41  # untouched


def test_poll_org_recovers_when_registry_lost_the_run(db, monkeypatch):
    # A hard restart can leave a `runs` row stuck "running" forever (the
    # registry is never rehydrated). The overlap guard must trust the
    # in-process registry, not that stale DB row, or the trigger wedges.
    from ui.backend.db.models import Run as RunRow

    org, trigger = _org_with_trigger(db, last_uid=41)
    db.add(RunRow(id="r-prev", workflow="triage", input="x", status="running",
                  org_id=org.id, username="email-trigger"))
    trigger.last_run_id = "r-prev"
    db.commit()

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_checked_at is not None  # polling proceeded -- no wedge


def test_poll_org_workflow_load_failure_recorded_not_raised(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    def _missing(name, db_, org_id, allowed_uids, backend):
        raise Exception("Unknown workflow 'triage'")

    poll_org(db, trigger, _missing)
    assert recorder.calls == []
    assert trigger.last_error is not None
    assert "triage" in trigger.last_error


# --- poll_once / poll_forever -------------------------------------------------

import asyncio

from ui.backend.email_trigger import poll_forever, poll_once


def test_poll_once_covers_enabled_orgs_and_survives_failures(db, monkeypatch):
    a = get_or_create_org(db, "org_a")
    b = get_or_create_org(db, "org_b")
    for org in (a, b):
        set_email_credentials(db, org.id, host="h", username="u", password="p")
        upsert_email_trigger(db, org.id, workflow_name="w", enabled=True,
                             last_uid=0, uidvalidity=1)
    polled = []

    def fake_poll_org(session, trigger, get_workflow):
        polled.append(trigger.org_id)
        if trigger.org_id == a.id:
            raise RuntimeError("org A explodes")  # must not stop org B

    monkeypatch.setattr(email_trigger, "poll_org", fake_poll_org)

    class _Factory:  # context-manager session factory over the test db
        def __call__(self):
            return self

        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    poll_once(_no_workflow, session_factory=_Factory())
    assert polled == [a.id, b.id]


def test_poll_once_rolls_back_after_org_failure(db, monkeypatch):
    a = get_or_create_org(db, "org_a")
    b = get_or_create_org(db, "org_b")
    for org in (a, b):
        set_email_credentials(db, org.id, host="h", username="u", password="p")
        upsert_email_trigger(db, org.id, workflow_name="w", enabled=True, last_uid=0, uidvalidity=1)
    seen = []

    def fake_poll_org(session, trigger, get_workflow):
        seen.append(trigger.org_id)
        if trigger.org_id == a.id:
            from ui.backend.db.models import EmailTrigger
            # org_id is unique -- a second row for org A raises IntegrityError
            # from the session itself on flush, leaving the shared session in
            # a pending-rollback state until poll_once rolls it back.
            session.add(EmailTrigger(org_id=a.id, workflow_name="dup", enabled=True,
                                     last_uid=0, uidvalidity=1))
            session.flush()

    monkeypatch.setattr(email_trigger, "poll_org", fake_poll_org)

    class _Factory:
        def __call__(self): return self
        def __enter__(self): return db
        def __exit__(self, *exc): return False

    email_trigger.poll_once(_no_workflow, session_factory=_Factory())
    # Org A really dirtied the shared session (IntegrityError on flush);
    # poll_once must roll back before org B's list_enabled_triggers query,
    # or that query raises PendingRollbackError and org B never runs.
    assert seen == [a.id, b.id]


def test_poll_forever_sleeps_first_and_respects_kill_switch(monkeypatch):
    calls = []
    monkeypatch.setattr(email_trigger, "poll_once", lambda gw, session_factory=None: calls.append(1))
    monkeypatch.setattr(email_trigger, "poll_seconds", lambda: 0.01)

    async def run_briefly(disabled):
        monkeypatch.setenv("BESTTEAM_TRIGGERS_DISABLED", "1" if disabled else "")
        stop = asyncio.Event()
        task = asyncio.ensure_future(poll_forever(stop, _no_workflow))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly(disabled=True))
    assert calls == []  # kill switch: loop alive, no polling

    asyncio.run(run_briefly(disabled=False))
    assert len(calls) >= 1


# --- validate_trigger_env -----------------------------------------------------


def test_validate_trigger_env_accepts_unset_and_valid_values(monkeypatch):
    monkeypatch.delenv("BESTTEAM_TRIGGER_POLL_SECONDS", raising=False)
    monkeypatch.delenv("BESTTEAM_TRIGGER_DAILY_CAP", raising=False)
    monkeypatch.delenv("BESTTEAM_TRIGGER_BATCH_SIZE", raising=False)
    email_trigger.validate_trigger_env()  # must not raise

    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "60")
    monkeypatch.setenv("BESTTEAM_TRIGGER_DAILY_CAP", "10")
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "5")
    email_trigger.validate_trigger_env()  # must not raise


def test_validate_trigger_env_rejects_non_numeric_poll_seconds(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "soon")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_POLL_SECONDS"):
        email_trigger.validate_trigger_env()


def test_validate_trigger_env_rejects_zero_batch_size(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "0")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_BATCH_SIZE"):
        email_trigger.validate_trigger_env()


def test_validate_trigger_env_rejects_negative_daily_cap(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_DAILY_CAP", "-5")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_DAILY_CAP"):
        email_trigger.validate_trigger_env()


def test_validate_trigger_env_rejects_nan_poll_seconds(monkeypatch):
    # float("nan") parses without raising, and nan <= 0 is False -- a naive
    # bounds check alone would let this through.
    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "nan")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_POLL_SECONDS"):
        email_trigger.validate_trigger_env()


def test_validate_trigger_env_rejects_infinite_poll_seconds(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "inf")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_POLL_SECONDS"):
        email_trigger.validate_trigger_env()


def test_validate_trigger_env_rejects_poll_seconds_below_minimum(monkeypatch):
    # A sub-minimum positive interval would hammer the IMAP server in a
    # practical tight loop -- positive alone isn't a sufficient bound.
    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "1")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_POLL_SECONDS"):
        email_trigger.validate_trigger_env()


# --- r-ext2 F2: a deactivation racing the dispatch CAS must not dispatch ---

def test_poll_org_does_not_dispatch_when_org_deactivated_before_cas(db, monkeypatch):
    from ui.backend.db.orgs import set_org_active
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41)
    publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    db.commit()
    # deactivation landed after trigger enumeration (poll_org already holds it)
    set_org_active(db, "acme", False)

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 43, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    poll_org(db, trigger, _fake_workflow_getter([]))

    assert recorder.calls == []          # no run dispatched
    assert trigger.runs_today == 0
    # A deactivated org's mail is released, not consumed: full suspend must be
    # reversible, so re-activating picks these up rather than losing them.
    from ui.backend.db.models import InboxEvent
    assert all(e.status == "pending" and e.attempts == 0 for e in db.query(InboxEvent))


# --- retry_triggered_run: safe manual retry of a triggered run (spec 11.2) --

from ui.backend.db.models import Run as _RunRow
from ui.backend.email_trigger import RetryError, retry_triggered_run


def _completed_triggered_run(db, org, *, uids=(42,), uidvalidity=3, run_id="orig-1",
                              status="failed", mailbox_credential_id=None,
                              mailbox_host="imap.acme.com", mailbox_username="u@acme.com"):
    from ui.backend.db.email_credentials import get_email_credentials

    if mailbox_credential_id is None:
        mailbox_credential_id = get_email_credentials(db, org.id).id
    row = _RunRow(
        id=run_id, workflow="triage", input="triage this batch",
        status=status, org_id=org.id, username="email-trigger",
        trigger_context={
            "trigger_type": "email",
            "mailbox_credential_id": mailbox_credential_id,
            "mailbox_host": mailbox_host,
            "mailbox_username": mailbox_username,
            "uidvalidity": uidvalidity,
            "uids": list(uids),
            "folder": "INBOX",
            "triggered_at": "2026-08-01T00:00:00+00:00",
        },
    )
    db.add(row)
    db.commit()
    return row


def test_retry_dispatches_a_new_run_and_preserves_history(db, monkeypatch):
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    run_row = _completed_triggered_run(db, org, uids=(42, 43), uidvalidity=3)

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    new_run_id = retry_triggered_run(db, run_row)

    assert new_run_id != run_row.id
    new_row = db.get(_RunRow, new_run_id)
    assert new_row.retry_of_run_id == run_row.id
    assert new_row.trigger_context["uids"] == [42, 43]
    assert new_row.status == "running"
    # Original row is untouched -- history stays immutable.
    assert db.get(_RunRow, run_row.id).status == "failed"
    assert db.get(_RunRow, run_row.id).retry_of_run_id is None
    assert len(recorder.calls) == 1


def test_retry_recomputes_result_contract_from_the_currently_deployed_workflow(db, monkeypatch):
    """retry_triggered_run must NOT blindly spread the original run's
    result_contract into the new one -- it has to re-derive it from the
    workflow's CURRENT deployed config, the same way _start_triggered_run
    does for a fresh dispatch, or a retry can carry a stale signal (Codex
    review finding: see the next two tests for the drift directions this
    guards against). This test is the steady-state case: still declared."""
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["property_maintenance_response_v1"]}]},
    )
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    run_row.trigger_context = {**run_row.trigger_context, "result_contract": "property_maintenance_email_batch"}
    db.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)
    new_row = db.get(_RunRow, new_run_id)
    assert new_row.trigger_context["result_contract"] == "property_maintenance_email_batch"


def test_retry_picks_up_a_newly_added_maintenance_contract_on_the_retried_workflow(db, monkeypatch):
    """The original run dispatched BEFORE the workflow declared the platform
    maintenance skill (no result_contract stamped). By retry time the
    workflow was redeployed to add that skill -- the retry must pick up the
    contract, not silently keep the original run's unstamped state, or its
    output would go unredacted despite now being a real maintenance run
    (Codex review finding)."""
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    assert "result_contract" not in run_row.trigger_context
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["property_maintenance_response_v1"]}]},
    )
    db.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)
    new_row = db.get(_RunRow, new_run_id)
    assert new_row.trigger_context["result_contract"] == "property_maintenance_email_batch"


def test_retry_drops_a_stale_maintenance_contract_when_the_workflow_no_longer_declares_it(db, monkeypatch):
    """The original run was stamped result_contract, but by retry time the
    workflow was redeployed to a config that no longer declares the platform
    maintenance skill -- the retry must NOT carry the stale contract
    forward, or an unrelated run's output would be wrongly redacted and
    normalized as a maintenance batch (Codex review finding)."""
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    run_row.trigger_context = {**run_row.trigger_context, "result_contract": "property_maintenance_email_batch"}
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["some_other_skill"]}]},
    )
    db.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)
    new_row = db.get(_RunRow, new_run_id)
    assert "result_contract" not in new_row.trigger_context


def test_retry_normalizes_before_publishing_run_failed_when_submit_raises(db, monkeypatch):
    """Same ordering fix as _start_triggered_run's analogous branch (Codex
    review finding): normalize_run_result must commit before run_failed is
    published when the retry's own dispatch submission fails."""
    from ui.backend.automation_results import AutomationItemResult
    from ui.backend.db.workflows import publish_workflow_version
    from ui.backend.runtime import registry

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "responder", "skills": ["property_maintenance_response_v1"]}]},
    )
    run_row = _completed_triggered_run(db, org, uids=(42, 43), uidvalidity=3)
    run_row.trigger_context = {**run_row.trigger_context, "result_contract": "property_maintenance_email_batch"}
    db.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))

    class _BoomExecutor:
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(email_trigger, "_executor", _BoomExecutor())

    rows_seen_at_publish_time = []
    orig_publish = registry.publish

    def spy_publish(run_id, event):
        if event.get("type") == "run_failed":
            rows_seen_at_publish_time.append(
                db.query(AutomationItemResult).filter_by(run_id=run_id).count()
            )
        orig_publish(run_id, event)

    monkeypatch.setattr(registry, "publish", spy_publish)

    new_run_id = retry_triggered_run(db, run_row)

    assert rows_seen_at_publish_time == [2]  # already committed before run_failed was published
    assert db.get(_RunRow, new_run_id).status == "failed"


def test_retry_rejects_uidvalidity_mismatch(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    # Mailbox was rebuilt/migrated since the original run -- current validity differs.
    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (9, 45))

    with pytest.raises(RetryError, match="UIDVALIDITY"):
        retry_triggered_run(db, run_row)


def test_retry_rejects_a_completed_run(db):
    # A completed run may already have real mailbox side effects (drafts) for
    # this batch -- only a failed run is safe to redo (Codex review finding).
    org, trigger = _org_with_trigger(db, last_uid=45)
    run_row = _completed_triggered_run(db, org, status="completed")
    with pytest.raises(RetryError, match="Only a failed run"):
        retry_triggered_run(db, run_row)


def test_retry_rejects_when_the_mailbox_identity_has_changed(db, monkeypatch):
    # A replaced mailbox could coincidentally share the original UIDVALIDITY --
    # host/username must also still match (Codex review finding).
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(
        db, org, uidvalidity=3, mailbox_host="imap.old-provider.com", mailbox_username="u@acme.com",
    )
    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))

    with pytest.raises(RetryError, match="mailbox has changed"):
        retry_triggered_run(db, run_row)


def test_retry_registers_itself_with_the_overlap_guard(db, monkeypatch):
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    run_row = _completed_triggered_run(db, org, uidvalidity=3)

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)

    assert trigger.last_run_id == new_run_id


def test_retry_rejects_while_a_run_is_still_registered_against_this_mailbox(db, monkeypatch):
    # Same overlap guard poll_org enforces: if trigger.last_run_id already
    # points at a running run (e.g. a concurrent automatic poll), a retry
    # must not silently displace that registration by overwriting
    # last_run_id -- or a later automatic poll would think the mailbox is
    # free while that original run is still in flight (Codex review finding).
    from ui.backend.runtime import registry

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    in_flight = registry.create("triage", "x", org_id=org.id, username="email-trigger")
    trigger.last_run_id = in_flight.id
    db.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    with pytest.raises(RetryError, match="already in progress"):
        retry_triggered_run(db, run_row)

    assert recorder.calls == []
    assert trigger.last_run_id == in_flight.id  # guard registration untouched


def test_retry_proceeds_once_the_registered_run_is_no_longer_running(db, monkeypatch):
    from ui.backend.runtime import registry

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    finished = registry.create("triage", "x", org_id=org.id, username="email-trigger")
    registry.publish(finished.id, {"type": "run_completed", "workflow": "triage", "data": "done"})
    trigger.last_run_id = finished.id
    db.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)  # must not raise
    assert trigger.last_run_id == new_run_id


def test_retry_clears_a_sticky_trigger_error_on_successful_dispatch(db, monkeypatch):
    # _start_triggered_run clears last_error/last_error_kind on a successful
    # dispatch ("a run is going out: clear any prior fault") -- a successful
    # retry dispatch must do the same, or a resolved workflow-kind error keeps
    # showing indefinitely despite the successful retry (Codex review finding).
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    trigger.last_error = "Couldn't start the team 'triage' -- it may have been removed."
    trigger.last_error_kind = "workflow"
    db.commit()
    run_row = _completed_triggered_run(db, org, uidvalidity=3)

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    retry_triggered_run(db, run_row)

    assert trigger.last_error is None
    assert trigger.last_error_kind is None


def test_retry_rejects_a_run_with_no_trigger_context(db):
    org, trigger = _org_with_trigger(db, last_uid=45)
    row = _RunRow(id="manual-1", workflow="triage", input="hi", status="failed", org_id=org.id)
    db.add(row)
    db.commit()
    with pytest.raises(RetryError, match="no recorded email batch"):
        retry_triggered_run(db, row)


def test_retry_rejects_a_still_running_run(db):
    org, trigger = _org_with_trigger(db, last_uid=45)
    run_row = _completed_triggered_run(db, org, status="running")
    with pytest.raises(RetryError, match="still in progress"):
        retry_triggered_run(db, run_row)


def test_retry_rejects_when_a_retry_is_already_running(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45)
    run_row = _completed_triggered_run(db, org)
    db.add(_RunRow(
        id="retry-in-flight", workflow="triage", input="x", status="running",
        org_id=org.id, retry_of_run_id=run_row.id,
    ))
    db.commit()
    with pytest.raises(RetryError, match="already in progress"):
        retry_triggered_run(db, run_row)


def test_retry_respects_daily_cap(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    trigger.runs_today = daily_cap()
    trigger.runs_date = datetime.now(timezone.utc).date().isoformat()
    db.commit()
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))

    with pytest.raises(RetryError, match="limit"):
        retry_triggered_run(db, run_row)


def test_retry_reports_unbuildable_workflow(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))

    def _boom(name, db_, org_id, allowed_uids, backend):
        raise ValueError("team not found")

    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _boom)
    with pytest.raises(RetryError, match="triage"):
        retry_triggered_run(db, run_row)
    assert trigger.last_run_id is None


def _confirm_draft(db, run_row, message_id):
    """Normalize `run_row` with a claimed-and-confirmed draft for `message_id`,
    as if the original run had actually called email_draft_reply for it before
    later failing -- the state already_drafted_uids reads."""
    import json

    from ui.backend.automation_results import normalize_run_result

    run_row.output = json.dumps({
        "schema_version": 1, "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": message_id, "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed", "summary": "s",
            "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": True, "draft_type": "ack"},
            "needs_human": False, "human_reason": "",
        }],
    })
    db.commit()
    normalize_run_result(db, run_row, confirmed_draft_message_ids=frozenset({message_id}))


def test_retry_excludes_uids_with_a_confirmed_draft_from_the_previous_attempt(db, monkeypatch):
    # email_draft_reply has no dedup -- resubmitting a UID the original run
    # already drafted a reply for would create a second draft in the mailbox
    # (Codex review finding).
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42, 43), uidvalidity=3)
    _confirm_draft(db, run_row, "42")

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    calls = []
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter(calls))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)

    new_row = db.get(_RunRow, new_run_id)
    assert new_row.trigger_context["uids"] == [43]
    assert calls[0][2] == {43}  # allowed_uids passed to build_trigger_workflow


def test_retry_rejects_when_every_uid_already_has_a_confirmed_draft(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42,), uidvalidity=3)
    _confirm_draft(db, run_row, "42")

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    with pytest.raises(RetryError, match="already has a confirmed draft"):
        retry_triggered_run(db, run_row)
    assert recorder.calls == []


def test_retry_input_text_reflects_the_narrowed_batch_not_the_original(db, monkeypatch):
    # run_row.input names the full original batch, including any UID just
    # excluded by the already-drafted check -- passing that stale text would
    # instruct the retrying agent to process a message its own scoped tools
    # then reject as out-of-batch, wasting the retry instead of covering the
    # UID that actually still needs it (Codex review finding).
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42, 43), uidvalidity=3)
    _confirm_draft(db, run_row, "42")

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    new_run_id = retry_triggered_run(db, run_row)

    _, args, _kwargs = recorder.calls[0]
    submitted_input_text = args[2]
    assert "42" not in submitted_input_text
    assert "43" in submitted_input_text
    new_row = db.get(_RunRow, new_run_id)
    assert "42" not in new_row.input
    assert "43" in new_row.input


def test_retry_daily_cap_recheck_inside_lock_catches_a_stale_early_pass(db, monkeypatch):
    """The early cap check (before mailbox/workflow work, a fast-path
    optimization only) reads whatever `trigger.runs_today` this call's own
    ORM object was loaded with -- it can pass on a stale snapshot. Only the
    fresh, uncached recheck taken inside the dispatch lock right before
    dispatch actually prevents exceeding the cap when another dispatch's
    increment (a concurrent session, simulated here) lands in between
    (Codex review finding)."""
    from ui.backend.db.models import EmailTrigger

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)
    today = datetime.now(timezone.utc).date().isoformat()
    trigger.runs_today = daily_cap() - 1
    trigger.runs_date = today
    db.commit()

    # A concurrent dispatch (a different session/request) consumes the last
    # remaining slot. `trigger`'s already-loaded runs_today stays stale in
    # memory -- only a fresh SELECT would see this.
    Session2 = session_factory(db.get_bind())
    with Session2() as db2:
        other = db2.query(EmailTrigger).filter_by(org_id=org.id).one()
        other.runs_today = daily_cap()
        db2.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    with pytest.raises(RetryError, match="limit"):
        retry_triggered_run(db, run_row)
    assert recorder.calls == []


def test_poll_org_daily_cap_recheck_inside_lock_catches_a_stale_early_pass(db, monkeypatch):
    """Symmetric to the retry-side test above, for poll_org's own early
    cap check vs. its fresh recheck inside the dispatch lock."""
    from ui.backend.db.models import EmailTrigger
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    today = datetime.now(timezone.utc).date().isoformat()
    trigger.runs_today = daily_cap() - 1
    trigger.runs_date = today
    db.commit()

    Session2 = session_factory(db.get_bind())
    with Session2() as db2:
        other = db2.query(EmailTrigger).filter_by(org_id=org.id).one()
        other.runs_today = daily_cap()
        db2.commit()

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    poll_org(db, trigger, _fake_workflow_getter([]))

    assert recorder.calls == []
    assert trigger.last_run_id is None


def test_retry_dispatch_blocks_on_the_same_per_org_lock_poll_org_uses(db, monkeypatch):
    """The overlap-guard check and the last_run_id write aren't atomic on
    their own -- a per-org lock (_dispatch_lock) must span both, in both
    poll_org and retry_triggered_run, or the two could race for the same org
    and both dispatch against the same mailbox (Codex review finding).
    Externally holding the org's dispatch lock (the same one poll_org uses)
    must block a concurrent retry until it's released."""
    import threading

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42,), uidvalidity=3)

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    lock = email_trigger._dispatch_lock(org.id)
    lock.acquire()
    result = {}

    def do_retry():
        result["run_id"] = retry_triggered_run(db, run_row)

    t = threading.Thread(target=do_retry)
    t.start()
    try:
        t.join(timeout=0.3)
        assert result == {}, "retry must not dispatch while the org's dispatch lock is held"
    finally:
        lock.release()

    t.join(timeout=2)
    assert result.get("run_id") is not None


def test_poll_org_blocks_on_the_per_org_dispatch_lock(db, monkeypatch):
    """Symmetric to the retry-side test above: poll_org must also acquire
    _dispatch_lock around its overlap-check-through-dispatch section, or a
    manual retry could win the DB write while a poll cycle's own dispatch is
    mid-flight, unobserved by the guard (Codex review finding)."""
    import threading

    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 43, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    lock = email_trigger._dispatch_lock(org.id)
    lock.acquire()

    def do_poll():
        poll_org(db, trigger, _fake_workflow_getter([]))

    t = threading.Thread(target=do_poll)
    t.start()
    try:
        t.join(timeout=0.3)
        assert recorder.calls == [], "poll_org must not dispatch while the org's dispatch lock is held"
    finally:
        lock.release()

    t.join(timeout=2)
    assert len(recorder.calls) == 1


def test_retry_discards_if_the_trigger_is_disabled_before_the_atomic_advance(db, monkeypatch):
    """Unlike _start_triggered_run, retry_triggered_run's dispatch update
    previously had no enabled/active guard at all -- a customer
    disconnecting/replacing the mailbox (or an operator deactivating the
    org) between this call's own pre-lock credential check and the atomic
    dispatch advance went undetected, and the retry would dispatch a real
    email_draft_reply against a mailbox the customer already disconnected
    (Codex review finding). Injected via a concurrent session disabling the
    trigger right as registry.create() is called, inside the dispatch lock,
    immediately before the CAS."""
    from ui.backend.db import session_factory
    from ui.backend.db.models import EmailTrigger

    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uidvalidity=3)

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter([]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    Session2 = session_factory(db.get_bind())
    real_create = email_trigger.registry.create

    def create_then_disable(*a, **k):
        run = real_create(*a, **k)
        with Session2() as db2:
            other = db2.query(EmailTrigger).filter_by(org_id=org.id).one()
            other.enabled = False
            db2.commit()
        return run

    monkeypatch.setattr(email_trigger.registry, "create", create_then_disable)

    with pytest.raises(RetryError, match="turned off"):
        retry_triggered_run(db, run_row)

    assert recorder.calls == []  # never dispatched
    assert db.query(_RunRow).filter_by(retry_of_run_id=run_row.id).first() is None  # no leaked run row


def test_retry_rechecks_already_drafted_freshly_inside_the_lock(db, monkeypatch):
    """The already-drafted check further up (before mailbox I/O) is only a
    fast-path -- a concurrent retry of the SAME run that dispatches and
    confirms a draft in the window between that check and the dispatch lock
    must still be caught by the fresh recheck inside the lock, or this call
    would resubmit a UID the concurrent retry just drafted (Codex review
    finding)."""
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42,), uidvalidity=3)

    def mailbox_state_then_confirm_draft(backend):
        # Simulates a concurrent retry (a different request) dispatching and
        # confirming a draft for the only UID in this batch, in the window
        # between this call's own fast-path check and its mailbox check.
        _confirm_draft(db, run_row, "42")
        return (3, 45)

    monkeypatch.setattr(email_trigger, "mailbox_state", mailbox_state_then_confirm_draft)
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    with pytest.raises(RetryError, match="already has a confirmed draft"):
        retry_triggered_run(db, run_row)
    assert recorder.calls == []


def test_retry_rechecks_retry_already_running_freshly_inside_the_lock(db, monkeypatch):
    """Same fast-path-vs-lock staleness as the already-drafted recheck above,
    for the 'a retry of this run is already in progress' guard: a concurrent
    retry of the SAME run dispatched in the window between this call's own
    fast-path check and the dispatch lock must still be caught (Codex review
    finding)."""
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42,), uidvalidity=3)

    def mailbox_state_then_start_concurrent_retry(backend):
        db.add(_RunRow(
            id="concurrent-retry", workflow="triage", input="x", status="running",
            org_id=org.id, retry_of_run_id=run_row.id,
        ))
        db.commit()
        return (3, 45)

    monkeypatch.setattr(email_trigger, "mailbox_state", mailbox_state_then_start_concurrent_retry)
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    with pytest.raises(RetryError, match="already in progress"):
        retry_triggered_run(db, run_row)
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Phase 0 (0.1 / 0.2 / 0.4): draft idempotency and the stuck-run watchdog.
# ---------------------------------------------------------------------------


def _trace_draft(db, run_id, message_id, seq=0):
    """A persisted `tool_completed` trace row proving a real draft call -- the
    evidence every run records regardless of workflow template."""
    import json

    from ui.backend.db.models import TraceEventRecord

    db.add(TraceEventRecord(
        run_id=run_id, seq=seq, type="tool_completed", agent="Triage",
        data=json.dumps({
            "tool": "email_draft_reply", "success": True, "duration_ms": 3,
            "summary": "Draft reply saved.",
            "message_id": message_id, "outcome": "draft_created",
        }),
    ))
    db.commit()


def test_retry_of_a_generic_email_run_excludes_trace_confirmed_drafts(db, monkeypatch):
    # The gap this closes: a generic (non-property-maintenance) email team
    # never gets automation_item_results rows, so the retry guard used to see
    # no evidence at all and resubmitted the whole partially-drafted batch,
    # creating a duplicate draft for every message already replied to. No
    # crash needed -- an ordinary mid-run failure was enough.
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42, 43), uidvalidity=3)
    _trace_draft(db, run_row.id, "42")

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    calls = []
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter(calls))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)

    assert db.get(_RunRow, new_run_id).trigger_context["uids"] == [43]
    assert calls[0][2] == {43}


def test_retry_excludes_uids_the_mailbox_itself_still_has_a_draft_for(db, monkeypatch):
    # Defence in depth behind the trace evidence: a draft that was really
    # APPENDed but whose confirming trace event never got persisted (process
    # killed between the two) is only visible in the mailbox.
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42, 43), uidvalidity=3)

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(
        email_trigger, "_mailbox_drafted_uids",
        lambda backend, trigger_context, uids: {"42"},
    )
    calls = []
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter(calls))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)

    assert db.get(_RunRow, new_run_id).trigger_context["uids"] == [43]
    assert calls[0][2] == {43}


def test_a_failing_mailbox_scan_never_blocks_a_legitimate_retry(db, monkeypatch):
    # The scan is best-effort: an IMAP server that cannot search custom
    # headers must degrade to trace evidence alone, not break retry entirely.
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_row = _completed_triggered_run(db, org, uids=(42,), uidvalidity=3)

    class _ScanFails:
        def drafts_with_source_keys(self, keys):
            raise OSError("SEARCH not supported")

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))
    monkeypatch.setattr(email_trigger, "_ImapBackend", lambda **kw: _ScanFails())
    calls = []
    monkeypatch.setattr(email_trigger, "build_trigger_workflow", _fake_workflow_getter(calls))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    new_run_id = retry_triggered_run(db, run_row)
    assert db.get(_RunRow, new_run_id).trigger_context["uids"] == [42]


def test_build_trigger_workflow_stamps_a_deterministic_draft_marker(db, monkeypatch):
    from ui.backend.db.email_credentials import get_email_credentials
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(
        db, org_id=org.id, name="triage",
        config={"agents": [{"name": "a", "model": "fake:x", "tools": ["email_read"]}]},
    )
    db.commit()
    captured = {}

    def _fake_make_email_tools(backend, allowed_uids=None, draft_marker_prefix=None):
        captured["prefix"] = draft_marker_prefix
        return {}

    monkeypatch.setattr(email_trigger, "make_email_tools", _fake_make_email_tools)
    monkeypatch.setattr(email_trigger, "_build_workflow", lambda *a, **k: object())
    monkeypatch.setattr(email_trigger, "load_knowledge_base_tools", lambda *a, **k: {})
    monkeypatch.setattr(email_trigger, "load_skills", lambda *a, **k: {})
    monkeypatch.setattr(email_trigger, "spec_uses_email", lambda *a, **k: True)

    cred_id = get_email_credentials(db, org.id).id
    email_trigger.build_trigger_workflow("triage", db, org.id, {42}, object())

    # Same shape automation_results._source_key generates, so the mailbox
    # marker and the stored source keys agree by construction.
    assert captured["prefix"] == "mailbox:%s:uidvalidity:3:uid:" % cred_id


def test_a_stale_running_run_stops_blocking_the_trigger(db, monkeypatch):
    # Without a watchdog, a hung run leaves the overlap guard permanently
    # closed: the org's automation silently stops forever, with last_error
    # empty so nothing in the UI reports a fault.
    from datetime import datetime, timedelta, timezone

    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    stale = _RunRow(
        id="stuck-1", workflow="triage", input="x", status="running",
        org_id=org.id, username="email-trigger",
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
        trigger_context={"trigger_type": "email", "uids": [40]},
    )
    db.add(stale)
    trigger.last_run_id = "stuck-1"
    db.commit()

    class _LiveRun:
        status = "running"

    monkeypatch.setattr(email_trigger.registry, "get", lambda rid: _LiveRun())
    cancelled = []
    monkeypatch.setattr(
        email_trigger.registry, "request_cancel", lambda rid: cancelled.append(rid)
    )
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda backend, last_uid: (3, 45, [42]))
    monkeypatch.setattr(email_trigger, "_ImapBackend", lambda **kw: object())
    calls = []
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    email_trigger.poll_org(db, trigger, _fake_workflow_getter(calls))

    assert cancelled == ["stuck-1"]
    assert db.get(_RunRow, "stuck-1").status == "failed"
    assert len(recorder.calls) == 1          # the new run went out
    assert trigger.last_uid == 42


def test_a_fresh_running_run_still_blocks_the_trigger(db, monkeypatch):
    from datetime import datetime, timezone

    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    fresh = _RunRow(
        id="live-1", workflow="triage", input="x", status="running",
        org_id=org.id, username="email-trigger",
        created_at=datetime.now(timezone.utc),
    )
    db.add(fresh)
    trigger.last_run_id = "live-1"
    db.commit()

    class _LiveRun:
        status = "running"

    monkeypatch.setattr(email_trigger.registry, "get", lambda rid: _LiveRun())
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    email_trigger.poll_org(db, trigger, _no_workflow)

    assert recorder.calls == []
    assert db.get(_RunRow, "live-1").status == "running"


def test_run_timeout_env_is_validated_at_startup(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS", "nonsense")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS"):
        email_trigger.validate_trigger_env()
    monkeypatch.setenv("BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS", "5")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS"):
        email_trigger.validate_trigger_env()


# --- inbox event dead-letter budget -------------------------------------------


def test_max_event_attempts_defaults_to_three(monkeypatch):
    monkeypatch.delenv(email_trigger.MAX_EVENT_ATTEMPTS_ENV, raising=False)
    assert email_trigger.max_event_attempts() == 3


def test_validate_trigger_env_rejects_zero_max_event_attempts(monkeypatch):
    monkeypatch.setenv(email_trigger.MAX_EVENT_ATTEMPTS_ENV, "0")
    with pytest.raises(RuntimeError, match="MAX_EVENT_ATTEMPTS"):
        email_trigger.validate_trigger_env()


# --- durable inbox events: detection, claim, release --------------------------


def _events(db):
    from ui.backend.db.models import InboxEvent

    return db.query(InboxEvent).order_by(InboxEvent.id).all()


def test_detection_records_events_and_advances_the_cursor_together(db, monkeypatch):
    # A poll that detects mail must leave a durable row per message in the same
    # commit that moves last_uid past it. Before this, the cursor advanced and
    # the work only existed inside a thread-pool submission, so a process kill
    # in that window consumed mail nothing ever ran.
    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 43, 45]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())
    poll_org(db, trigger, _fake_workflow_getter([]))

    rows = _events(db)
    assert [r.external_id for r in rows] == ["42", "43", "45"]
    assert all(r.mailbox_generation == "3" for r in rows)
    assert all(r.mailbox_identity == "imap.acme.com:u@acme.com" for r in rows)
    assert trigger.last_uid == 45


def test_detection_is_idempotent_across_polls(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 42, [42]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())
    poll_org(db, trigger, _fake_workflow_getter([]))

    # The same message seen again (e.g. the cursor write was lost): the unique
    # key must make this a no-op rather than a second event.
    trigger.last_uid = 41
    db.commit()
    poll_org(db, trigger, _fake_workflow_getter([]))
    assert len(_events(db)) == 1


def test_detection_is_bounded_per_cycle(db, monkeypatch):
    # A long outage can leave a large backlog; one cycle must not open an
    # unbounded transaction. The cursor advances only as far as it recorded.
    monkeypatch.setenv(email_trigger.BATCH_SIZE_ENV, "2")
    org, trigger = _org_with_trigger(db, last_uid=0, uidvalidity=3)
    monkeypatch.setattr(
        email_trigger, "check_mailbox", lambda b, u: (3, 100, list(range(1, 101)))
    )
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())
    poll_org(db, trigger, _fake_workflow_getter([]))

    rows = _events(db)
    assert len(rows) == 2 * email_trigger._DETECT_MULTIPLIER
    assert trigger.last_uid == 2 * email_trigger._DETECT_MULTIPLIER


def test_dispatch_claims_events_and_charges_one_attempt(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 43, [42, 43]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())
    poll_org(db, trigger, _fake_workflow_getter([]))

    rows = _events(db)
    assert {r.status for r in rows} == {"claimed"}
    assert all(r.attempts == 1 for r in rows)
    assert all(r.run_id == trigger.last_run_id for r in rows)


def test_a_workflow_build_failure_releases_the_events_without_penalty(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 42, [42]))
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())

    def _broken(name, db_, org_id, allowed_uids, backend):
        raise RuntimeError("team deleted")

    poll_org(db, trigger, _broken)

    row = _events(db)[0]
    # Back to pending with no attempt charged: a broken team config must not
    # dead-letter the org's mail, and today such mail is never consumed.
    assert (row.status, row.attempts, row.run_id) == ("pending", 0, None)
    assert trigger.last_error_kind == "workflow"


class _DispatchBoom:
    def submit(self, *a, **kw):
        raise RuntimeError("cannot schedule new futures after shutdown")


def test_a_dispatch_failure_releases_the_events(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 42, [42]))
    monkeypatch.setattr(email_trigger, "_executor", _DispatchBoom())
    poll_org(db, trigger, _fake_workflow_getter([]))

    row = _events(db)[0]
    # An attempt WAS charged (the run really was dispatched), but the message
    # returns for reprocessing rather than being silently consumed.
    assert (row.status, row.attempts) == ("pending", 1)


def test_a_dispatch_failure_dead_letters_at_the_attempt_limit(db, monkeypatch):
    monkeypatch.setenv(email_trigger.MAX_EVENT_ATTEMPTS_ENV, "1")
    org, trigger = _org_with_trigger(db, last_uid=41, uidvalidity=3)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 42, [42]))
    monkeypatch.setattr(email_trigger, "_executor", _DispatchBoom())
    poll_org(db, trigger, _fake_workflow_getter([]))

    assert _events(db)[0].status == "failed"
    # A dead-lettered message must not be invisible to the customer.
    assert trigger.last_error is not None


def _hung_run(db, org, trigger, *, uids, age_seconds=4000):
    """A triggered run stuck `running`, with its inbox events claimed."""
    from datetime import timedelta

    from ui.backend.db import inbox_events as store
    from ui.backend.db.models import Run as _R

    run_id = "hung-run"
    db.add(_R(
        id=run_id, workflow="triage", input="x", status="running", org_id=org.id,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        trigger_context={"trigger_type": "email", "uids": [int(u) for u in uids]},
    ))
    store.record_events(db, org_id=org.id, mailbox_identity="m",
                        mailbox_generation="3", external_ids=uids)
    db.commit()
    store.claim_events(db, org_id=org.id, run_id=run_id, limit=len(uids))
    store.mark_dispatched(db, run_id)
    trigger.last_run_id = run_id
    db.commit()
    return run_id


def test_the_watchdog_releases_a_hung_runs_events(db):
    # Phase 0's watchdog frees the trigger from a hung run; its messages must
    # come back too, or they are consumed with nothing having processed them.
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_id = _hung_run(db, org, trigger, uids=["11", "12"])
    assert email_trigger._release_stale_run(db, trigger, run_id) is True
    rows = _events(db)
    assert {r.status for r in rows} == {"pending"}
    assert all(r.run_id is None for r in rows)


def test_the_watchdog_dead_letters_at_the_attempt_limit(db, monkeypatch):
    monkeypatch.setenv(email_trigger.MAX_EVENT_ATTEMPTS_ENV, "1")
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    run_id = _hung_run(db, org, trigger, uids=["11"])
    email_trigger._release_stale_run(db, trigger, run_id)
    assert _events(db)[0].status == "failed"
    # A dead-lettered message must not be invisible to the customer.
    assert trigger.last_error is not None
