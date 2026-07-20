"""Unit tests for the autonomous email trigger (poller logic, no real IMAP)."""

import pytest

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


def _no_workflow(name, db, org_id, allowed_uids):  # must NOT be called
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


# --- poll_org: the new-mail path ---------------------------------------------


class _SubmitRecorder:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


def _fake_workflow_getter(calls):
    def build(name, db, org_id, allowed_uids):
        calls.append((name, org_id, set(allowed_uids)))
        return object()
    return build


def test_poll_org_new_mail_starts_one_run(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 43, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    calls = []
    poll_org(db, trigger, _fake_workflow_getter(calls))

    assert calls == [("triage", org.id, {42, 43, 45})]  # scoped to the batch
    assert len(recorder.calls) == 1
    _, args, kwargs = recorder.calls[0]
    run_id, input_text = args[0], args[2]
    assert "42, 43, 45" in input_text
    assert kwargs["username"] == "email-trigger"
    # Durable run row exists BEFORE dispatch.
    from ui.backend.db.models import Run
    assert db.get(Run, run_id) is not None
    assert trigger.last_uid == 45 and trigger.runs_today == 1 and trigger.last_run_id == run_id


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
    assert trigger.last_uid == 42           # baseline advanced only past the batch
    assert trigger.runs_today == 1


def test_poll_org_build_failure_advances_nothing(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    def _boom(name, db_, org_id, allowed_uids):
        raise ValueError("No team named 'triage'")

    poll_org(db, trigger, _boom)
    assert recorder.calls == []
    assert trigger.last_uid == 41          # NOT advanced -- no message consumed
    assert trigger.runs_today == 0         # NO cap burned
    assert trigger.last_error is not None and "triage" in trigger.last_error


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

    def _missing(name, db_, org_id, allowed_uids):
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
            from sqlalchemy.exc import SQLAlchemyError
            raise SQLAlchemyError("boom")  # leaves the session needing rollback

    monkeypatch.setattr(email_trigger, "poll_org", fake_poll_org)

    class _Factory:
        def __call__(self): return self
        def __enter__(self): return db
        def __exit__(self, *exc): return False

    email_trigger.poll_once(_no_workflow, session_factory=_Factory())
    assert seen == [a.id, b.id]   # org B still ran despite org A poisoning the session


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
