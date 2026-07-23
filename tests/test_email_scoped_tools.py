"""UID-scoped email tools: an autonomous run may only touch its detected batch."""

import pytest

from bestteam.tools.email_client import make_email_tools

_OUT_OF_BATCH = "That message isn't part of this batch of new mail."


class _FakeBackend:
    """Records calls; find() would return the whole inbox if not scoped."""

    def __init__(self):
        self.read_calls = []
        self.draft_calls = []

    def find(self, query):
        # The unscoped path -- returns "everything" so a scoping bug is visible.
        return [{"id": "99", "from": "x@x", "subject": "unrelated", "date": "d", "snippet": ""}]

    def summaries_for(self, uids):
        return [{"id": str(u), "from": "a@b", "subject": f"s{u}", "date": "d", "snippet": ""}
                for u in uids]

    def read(self, message_id):
        self.read_calls.append(message_id)
        return {"id": message_id, "from": "a@b", "to": "", "subject": "s", "date": "d", "body": "hi"}

    def draft_reply(self, message_id, body):
        self.draft_calls.append((message_id, body))
        return f"Draft reply saved (reply to message {message_id})."


def test_scoped_find_ignores_query_and_shows_only_the_batch():
    b = _FakeBackend()
    tools = make_email_tools(b, allowed_uids={42, 43, 45})
    out = tools["email_find"]("anything at all")
    assert "42" in out and "43" in out and "45" in out
    assert "99" not in out  # the unscoped find() result never leaks in


def test_scoped_read_refuses_out_of_batch_uid():
    b = _FakeBackend()
    tools = make_email_tools(b, allowed_uids={42, 43})
    assert tools["email_read"]("44") == _OUT_OF_BATCH
    assert b.read_calls == []  # backend never touched
    assert "hi" in tools["email_read"]("42")  # in-batch works
    assert b.read_calls == ["42"]


def test_scoped_draft_refuses_out_of_batch_uid():
    b = _FakeBackend()
    tools = make_email_tools(b, allowed_uids={42})
    assert tools["email_draft_reply"]("77", "body") == _OUT_OF_BATCH
    assert b.draft_calls == []
    tools["email_draft_reply"]("42", "body")
    assert b.draft_calls == [("42", "body")]


def test_unscoped_mode_is_unchanged():
    b = _FakeBackend()
    tools = make_email_tools(b)  # allowed_uids=None
    out = tools["email_find"]("")
    assert "99" in out  # uses backend.find(), today's behavior
    assert "hi" in tools["email_read"]("99")
