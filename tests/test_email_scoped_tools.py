"""UID-scoped email tools: an autonomous run may only touch its detected batch."""

import threading
import time

import pytest

from bestteam.tools.email_client import make_email_tools


pytestmark = pytest.mark.unit
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


def test_attachment_reading_is_confined_to_the_batch():
    # Same containment as email_read: a run may only touch the messages the
    # poller detected for it.
    tools = make_email_tools(_FakeBackend(), allowed_uids={"42", "43"})
    assert tools["email_read_attachment"]("44", "quote.pdf") == _OUT_OF_BATCH


def test_the_toolkit_exposes_exactly_four_tools():
    tools = make_email_tools(_FakeBackend())
    assert set(tools) == {
        "email_find", "email_read", "email_draft_reply", "email_read_attachment",
    }


# ---------------------------------------------------------------------------
# Phase 0 (0.1): deterministic draft marker header.
#
# `email_draft_reply` APPENDs unconditionally, so a draft that was written to
# the mailbox but whose confirming trace event never got persisted (process
# killed between the two) is invisible to the trace-based retry guard. Stamping
# a deterministic source key on the draft itself lets a retry reconcile against
# the mailbox and spot exactly that case.
# ---------------------------------------------------------------------------


class _MarkerRecordingBackend(_FakeBackend):
    def __init__(self):
        super().__init__()
        self.source_keys = []

    def draft_reply(self, message_id, body, source_key=None):
        self.draft_calls.append((message_id, body))
        self.source_keys.append(source_key)
        return f"Draft reply saved (reply to message {message_id})."


def test_draft_marker_prefix_is_passed_through_as_a_per_message_source_key():
    b = _MarkerRecordingBackend()
    tools = make_email_tools(
        b, allowed_uids={42}, draft_marker_prefix="mailbox:7:uidvalidity:3:uid:"
    )
    tools["email_draft_reply"]("42", "hello")
    assert b.source_keys == ["mailbox:7:uidvalidity:3:uid:42"]


def test_without_a_marker_prefix_the_backend_is_called_the_old_way():
    # Backwards compatibility: a backend whose draft_reply takes only
    # (message_id, body) -- including the Graph backend, whose createReply
    # builds the draft server-side -- must keep working untouched.
    b = _FakeBackend()
    tools = make_email_tools(b, allowed_uids={42})
    assert "Draft reply saved" in tools["email_draft_reply"]("42", "hello")
    assert b.draft_calls == [("42", "hello")]


def test_marker_uses_the_stripped_message_id():
    b = _MarkerRecordingBackend()
    tools = make_email_tools(b, allowed_uids={42}, draft_marker_prefix="p:")
    tools["email_draft_reply"]("  42  ", "hello")
    assert b.source_keys == ["p:42"]


# ---------------------------------------------------------------------------
# Phase 3a (3a.5): drafting is idempotent per source key.
#
# The stale-run watchdog releases a timed-out run's overlap guard without being
# able to stop the worker -- a node inside `workflow.stream()` can't be
# interrupted. So the wedged worker and the retry it enabled can both be alive,
# in the SAME process, drafting for the same message. Trace evidence can't
# close that: the window is exactly the one where the original's APPEND hasn't
# landed yet. Idempotency has to live at the point of writing.
# ---------------------------------------------------------------------------


class _IdempotentRecordingBackend:
    """Records APPENDs and reports the source keys already in Drafts."""

    def __init__(self, existing_keys=None, delay=0.0):
        self.existing_keys = set(existing_keys or ())
        self.delay = delay
        self.append_calls = 0
        self.scanned = []

    def summaries_for(self, uids):
        return []

    def drafts_with_source_keys(self, source_keys):
        self.scanned.append(list(source_keys))
        return {k for k in source_keys if k in self.existing_keys}

    def draft_reply(self, message_id, body, source_key=None):
        # The delay models the real gap between deciding to write and the
        # APPEND landing, which is what makes the race observable.
        if self.delay:
            time.sleep(self.delay)
        self.append_calls += 1
        if source_key:
            self.existing_keys.add(source_key)
        return f"Draft reply saved (reply to message {message_id})."


def test_a_draft_that_already_exists_is_not_written_again():
    b = _IdempotentRecordingBackend()
    tools = make_email_tools(b, draft_marker_prefix="mailbox:1:uidvalidity:9:uid:")
    first = tools["email_draft_reply"]("7", "first")
    assert "saved" in first
    second = tools["email_draft_reply"]("7", "second")
    assert "already exists" in second
    assert b.append_calls == 1


def test_two_threads_drafting_the_same_message_append_once():
    b = _IdempotentRecordingBackend(delay=0.05)
    tools = make_email_tools(b, draft_marker_prefix="p:")
    threads = [
        threading.Thread(target=tools["email_draft_reply"], args=("7", "body"))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.append_calls == 1


def test_different_messages_do_not_block_each_other():
    b = _IdempotentRecordingBackend()
    tools = make_email_tools(b, draft_marker_prefix="p:")
    tools["email_draft_reply"]("7", "a")
    tools["email_draft_reply"]("8", "b")
    assert b.append_calls == 2


def test_a_failing_idempotency_scan_never_blocks_drafting():
    class _ScanExplodes(_IdempotentRecordingBackend):
        def drafts_with_source_keys(self, source_keys):
            raise RuntimeError("IMAP SEARCH refused")

    b = _ScanExplodes()
    tools = make_email_tools(b, draft_marker_prefix="p:")
    assert "saved" in tools["email_draft_reply"]("7", "body")
    assert b.append_calls == 1


def test_without_a_marker_prefix_no_idempotency_scan_happens():
    # The env-only single-mailbox path has no source keys, and the Graph
    # backend has no Drafts search at all.
    b = _IdempotentRecordingBackend()
    tools = make_email_tools(b)
    tools["email_draft_reply"]("7", "body")
    assert b.scanned == []
    assert b.append_calls == 1
