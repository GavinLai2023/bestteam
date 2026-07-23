"""_ImapBackend.summaries_for fetches exactly the given UIDs, read-only."""

import pytest

from bestteam.tools.email_client import _ImapBackend

_HDR = b"From: a@b\r\nSubject: hello\r\nDate: today\r\n\r\n"


class _FakeConn:
    def __init__(self):
        self.selected_readonly = None
        self.fetched = []

    def select(self, mailbox, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        assert command == "fetch"
        self.fetched.append(args[0])
        return "OK", [(b"1 (UID x)", _HDR)]

    def logout(self):
        pass


def test_summaries_for_fetches_given_uids_readonly(monkeypatch):
    backend = _ImapBackend(host="h", user="u", password="p")
    conn = _FakeConn()
    monkeypatch.setattr(backend, "_connect", lambda: conn)
    out = backend.summaries_for([42, 43])
    assert conn.selected_readonly is True
    assert conn.fetched == [b"42", b"43"]  # exactly these, as UID fetches
    assert len(out) == 2 and out[0]["subject"] == "hello"
