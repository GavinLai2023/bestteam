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


def _no_workflow(name, db, org_id):  # get_workflow stub that must NOT be called
    raise AssertionError("get_workflow should not be called in this test")


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


def test_poll_org_error_clears_on_next_success(db, monkeypatch):
    org, trigger = _org_with_trigger(db)
    trigger.last_error = "Couldn't check the mailbox."
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is None
