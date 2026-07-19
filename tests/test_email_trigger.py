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
