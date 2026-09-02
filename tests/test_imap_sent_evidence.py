"""_ImapBackend's Sent-folder evidence methods for draft outcome tracking:
resolve the Sent folder, search it by source-key header and by In-Reply-To,
and fetch origin Message-IDs from INBOX. All read-only. See
docs/superpowers/specs/2026-09-03-draft-outcome-tracking-design.md."""

import pytest

from bestteam.tools.email_client import _ImapBackend


pytestmark = pytest.mark.unit


class _FakeConn:
    def __init__(self, *, list_lines=(), search_hits=None, fetch_headers=None):
        self.list_lines = list(list_lines)
        # {search key value: uid bytes} -- a SEARCH for a value in this dict
        # "finds" a message.
        self.search_hits = search_hits or {}
        # {uid: header block bytes} for UID FETCH.
        self.fetch_headers = fetch_headers or {}
        self.selected = None
        self.selected_readonly = None
        self.searches = []

    def list(self):
        return "OK", self.list_lines

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            # args: (None, "HEADER", <field>, '"<value>"')
            self.searches.append(args)
            value = args[-1].strip('"')
            hit = self.search_hits.get(value)
            return "OK", [hit if hit else b""]
        assert command == "fetch"
        uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
        header = self.fetch_headers.get(uid)
        if header is None:
            return "OK", [None]
        return "OK", [(f"1 (UID {uid})".encode(), header)]

    def logout(self):
        pass


def _backend(monkeypatch, conn):
    backend = _ImapBackend(host="h", user="u", password="p")
    monkeypatch.setattr(backend, "_connect", lambda: conn)
    return backend


def test_sent_folder_resolved_by_special_use_flag(monkeypatch):
    conn = _FakeConn(list_lines=[
        br'(\HasNoChildren) "/" "INBOX"',
        br'(\HasNoChildren \Sent) "/" "Sent Items"',
    ])
    backend = _backend(monkeypatch, conn)
    assert backend._sent_folder(conn) == "Sent Items"


def test_sent_folder_falls_back_to_sent(monkeypatch):
    conn = _FakeConn(list_lines=[br'(\HasNoChildren) "/" "INBOX"'])
    backend = _backend(monkeypatch, conn)
    assert backend._sent_folder(conn) == "Sent"


def test_sent_with_source_keys_searches_sent_readonly(monkeypatch):
    conn = _FakeConn(
        list_lines=[br'(\Sent) "/" "Sent Items"'],
        search_hits={"mailbox:7:uidvalidity:3:uid:42": b"9"},
    )
    backend = _backend(monkeypatch, conn)

    found = backend.sent_with_source_keys(
        ["mailbox:7:uidvalidity:3:uid:42", "mailbox:7:uidvalidity:3:uid:43"]
    )

    assert found == {"mailbox:7:uidvalidity:3:uid:42"}
    assert conn.selected == "Sent Items"
    assert conn.selected_readonly is True
    assert all(args[1] == "HEADER" and args[2] == "X-BestTeam-Source-Key"
               for args in conn.searches)


def test_sent_with_source_keys_empty_input_makes_no_connection(monkeypatch):
    backend = _ImapBackend(host="h", user="u", password="p")
    monkeypatch.setattr(
        backend, "_connect",
        lambda: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    assert backend.sent_with_source_keys([]) == set()


def test_sent_replies_to_searches_in_reply_to(monkeypatch):
    conn = _FakeConn(
        list_lines=[br'(\Sent) "/" "Sent"'],
        search_hits={"<orig@example.com>": b"9"},
    )
    backend = _backend(monkeypatch, conn)

    found = backend.sent_replies_to(["<orig@example.com>", "<other@example.com>"])

    assert found == {"<orig@example.com>"}
    assert conn.selected_readonly is True
    assert all(args[1] == "HEADER" and args[2] == "In-Reply-To"
               for args in conn.searches)


def test_message_ids_for_uids_fetches_from_inbox_readonly(monkeypatch):
    conn = _FakeConn(fetch_headers={
        "42": b"Message-ID: <orig@example.com>\r\n\r\n",
    })
    backend = _backend(monkeypatch, conn)

    out = backend.message_ids_for_uids(["42", "43"])

    assert out == {"42": "<orig@example.com>"}
    assert conn.selected == "INBOX"
    assert conn.selected_readonly is True
