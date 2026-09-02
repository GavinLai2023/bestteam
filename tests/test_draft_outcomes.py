"""Unit tests for draft_outcomes.py: recording one row per confirmed
platform-written draft and reconciling those rows against the mailbox
(still in Drafts / found in Sent / gone) on the poll cycle. See
docs/superpowers/specs/2026-09-03-draft-outcome-tracking-design.md."""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")

from ui.backend import draft_outcomes
from ui.backend.draft_outcomes import (
    MISS_THRESHOLD,
    RECONCILE_BATCH,
    WINDOW_DAYS,
    reconcile,
    reconcile_org,
    record_outcomes_for_run,
    summary,
)
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import DraftOutcome, Run, TraceEventRecord
from ui.backend.db.orgs import get_or_create_org

_NOW = datetime(2026, 9, 3, 12, 0, 0)
_PREFIX = "mailbox:7:uidvalidity:3:uid:"


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    yield session
    session.close()


def _make_run(db, *, org_id, uids, status="completed", uidvalidity=3,
              mailbox_credential_id=7, run_id="run-1", trigger_type="email"):
    run = Run(
        id=run_id,
        pipeline="email_triage_reply",
        input="triage",
        output="Triaged.",
        status=status,
        org_id=org_id,
        username="email-trigger",
        trigger_context={
            "trigger_type": trigger_type,
            "mailbox_credential_id": mailbox_credential_id,
            "uidvalidity": uidvalidity,
            "uids": uids,
            "folder": "INBOX",
            "triggered_at": "2026-09-03T00:00:00+00:00",
        },
    )
    db.add(run)
    db.commit()
    return run


def _draft_trace_event(db, run_id, message_id, *, seq=0):
    db.add(
        TraceEventRecord(
            run_id=run_id,
            seq=seq,
            type="tool_completed",
            agent="Triage Agent",
            data=json.dumps({
                "tool": "email_draft_reply",
                "success": True,
                "duration_ms": 12,
                "summary": f"Draft reply saved for message '{message_id}'.",
                "message_id": message_id,
                "outcome": "draft_created",
            }),
        )
    )
    db.commit()


def _row(db, *, org_id, uid="42", status="pending", created_at=None,
         prefix=_PREFIX, run_id="run-1", **kwargs):
    row = DraftOutcome(
        org_id=org_id,
        run_id=run_id,
        source_key=f"{prefix}{uid}",
        status=status,
        created_at=created_at or _NOW,
        **kwargs,
    )
    db.add(row)
    db.commit()
    return row


class StubBackend:
    """Mailbox stand-in: constructor lists decide what each folder 'contains'.
    Every call is recorded so tests can assert what was (not) asked."""

    def __init__(self, *, drafts=(), sent=(), replied_to=(), msgids=None):
        self.drafts = set(drafts)
        self.sent = set(sent)
        self.replied_to = set(replied_to)
        self.msgids = dict(msgids or {})
        self.calls = []

    def drafts_with_source_keys(self, keys):
        self.calls.append(("drafts", sorted(keys)))
        return self.drafts & set(keys)

    def sent_with_source_keys(self, keys):
        self.calls.append(("sent", sorted(keys)))
        return self.sent & set(keys)

    def sent_replies_to(self, message_ids):
        self.calls.append(("replies", sorted(message_ids)))
        return self.replied_to & set(message_ids)

    def message_ids_for_uids(self, uids):
        self.calls.append(("msgids", sorted(uids)))
        return {u: self.msgids[u] for u in uids if u in self.msgids}


class FailingBackend:
    def drafts_with_source_keys(self, keys):
        raise OSError("IMAP connection lost")


# ---------------------------------------------------------------------------
# record_outcomes_for_run
# ---------------------------------------------------------------------------


def test_record_creates_pending_rows_for_confirmed_drafts_only(db):
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, uids=[5, 6])
    _draft_trace_event(db, run.id, "5")

    assert record_outcomes_for_run(db, run) == 1
    rows = db.query(DraftOutcome).all()
    assert len(rows) == 1
    assert rows[0].source_key == f"{_PREFIX}5"
    assert rows[0].status == "pending"
    assert rows[0].org_id == org.id
    assert rows[0].run_id == run.id


def test_record_is_idempotent_across_retry_family(db):
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, uids=[5], status="failed")
    _draft_trace_event(db, run.id, "5")
    record_outcomes_for_run(db, run)

    retry = _make_run(db, org_id=org.id, uids=[5], run_id="run-2")
    _draft_trace_event(db, retry.id, "5")
    assert record_outcomes_for_run(db, retry) == 0
    assert db.query(DraftOutcome).count() == 1


def test_record_ignores_non_email_runs(db):
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, uids=[5], trigger_type="other")
    _draft_trace_event(db, run.id, "5")
    assert record_outcomes_for_run(db, run) == 0
    assert db.query(DraftOutcome).count() == 0


def test_record_ignores_runs_without_confirmed_drafts(db):
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, uids=[5, 6])
    assert record_outcomes_for_run(db, run) == 0
    assert db.query(DraftOutcome).count() == 0


# ---------------------------------------------------------------------------
# reconcile: the decision ladder
# ---------------------------------------------------------------------------


def test_still_in_drafts_stays_pending(db):
    org = get_or_create_org(db, "acme")
    row = _row(db, org_id=org.id, uid="42", miss_count=1)
    backend = StubBackend(drafts={f"{_PREFIX}42"})

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    db.refresh(row)
    assert row.status == "pending"
    assert row.checked_at == _NOW
    assert row.miss_count == 0
    assert row.resolved_at is None


def test_gone_and_in_sent_by_header_is_sent(db):
    org = get_or_create_org(db, "acme")
    row = _row(db, org_id=org.id, uid="42")
    backend = StubBackend(sent={f"{_PREFIX}42"})

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    db.refresh(row)
    assert row.status == "sent"
    assert row.evidence == "source_key_header"
    assert row.resolved_at == _NOW


def test_gone_and_replied_to_in_sent_is_sent_via_in_reply_to(db):
    org = get_or_create_org(db, "acme")
    row = _row(db, org_id=org.id, uid="42")
    backend = StubBackend(
        msgids={"42": "<orig@example.com>"},
        replied_to={"<orig@example.com>"},
    )

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    db.refresh(row)
    assert row.status == "sent"
    assert row.evidence == "in_reply_to"
    assert row.origin_message_id == "<orig@example.com>"


def test_stored_origin_message_id_is_not_refetched(db):
    org = get_or_create_org(db, "acme")
    _row(db, org_id=org.id, uid="42", origin_message_id="<orig@example.com>")
    backend = StubBackend(replied_to={"<orig@example.com>"})

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    assert ("msgids", ["42"]) not in backend.calls


def test_gone_everywhere_needs_two_misses_before_handled(db):
    org = get_or_create_org(db, "acme")
    row = _row(db, org_id=org.id, uid="42")
    backend = StubBackend()

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)
    db.refresh(row)
    assert row.status == "pending"
    assert row.miss_count == 1

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX,
              now=_NOW + timedelta(seconds=120))
    db.refresh(row)
    assert row.status == "handled"
    assert row.miss_count == MISS_THRESHOLD
    assert row.resolved_at is not None


def test_generation_mismatch_is_unknown_without_mailbox_calls(db):
    org = get_or_create_org(db, "acme")
    stale = _row(db, org_id=org.id, uid="42", prefix="mailbox:7:uidvalidity:2:uid:")
    backend = StubBackend()

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    db.refresh(stale)
    assert stale.status == "unknown"
    assert backend.calls == []


def test_pending_older_than_window_is_unknown(db):
    org = get_or_create_org(db, "acme")
    old = _row(db, org_id=org.id, uid="42",
               created_at=_NOW - timedelta(days=WINDOW_DAYS + 1))
    backend = StubBackend(drafts={f"{_PREFIX}42"})

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    db.refresh(old)
    assert old.status == "unknown"
    assert backend.calls == []


def test_reconcile_only_touches_this_org_and_pending_rows(db):
    org = get_or_create_org(db, "acme")
    other = get_or_create_org(db, "globex")
    done = _row(db, org_id=org.id, uid="1", status="sent")
    theirs = _row(db, org_id=other.id, uid="2")
    backend = StubBackend()

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    db.refresh(done)
    db.refresh(theirs)
    assert done.status == "sent"
    assert theirs.status == "pending"
    assert theirs.miss_count == 0
    assert backend.calls == []


def test_reconcile_batch_is_bounded(db):
    org = get_or_create_org(db, "acme")
    for uid in range(RECONCILE_BATCH + 5):
        _row(db, org_id=org.id, uid=str(uid))
    backend = StubBackend(drafts={f"{_PREFIX}{u}" for u in range(RECONCILE_BATCH + 5)})

    reconcile(db, org_id=org.id, backend=backend, marker_prefix=_PREFIX, now=_NOW)

    checked = db.query(DraftOutcome).filter(DraftOutcome.checked_at.isnot(None)).count()
    assert checked == RECONCILE_BATCH


# ---------------------------------------------------------------------------
# reconcile_org: glue + isolation
# ---------------------------------------------------------------------------


class _Trigger:
    def __init__(self, org_id, uidvalidity=3):
        self.org_id = org_id
        self.uidvalidity = uidvalidity


def test_reconcile_org_short_circuits_with_no_pending_rows(db, monkeypatch):
    org = get_or_create_org(db, "acme")
    _row(db, org_id=org.id, uid="1", status="sent")

    def _boom(*a, **k):
        raise AssertionError("credentials must not be touched when nothing is pending")

    monkeypatch.setattr("ui.backend.db.email_credentials.get_email_credentials", _boom)
    reconcile_org(db, _Trigger(org.id))


def test_reconcile_org_swallows_mailbox_failure(db, monkeypatch):
    org = get_or_create_org(db, "acme")
    row = _row(db, org_id=org.id, uid="42")

    class _Cred:
        id = 7
        password_encrypted = "token"

    monkeypatch.setattr(
        "ui.backend.db.email_credentials.get_email_credentials", lambda db, org_id: _Cred()
    )
    monkeypatch.setattr("ui.backend.secret_store.decrypt", lambda token: "pw")
    monkeypatch.setattr(
        "ui.backend.email_tools.build_backend_for_credential",
        lambda cred, secret: FailingBackend(),
    )

    reconcile_org(db, _Trigger(org.id))  # must not raise

    db.refresh(row)
    assert row.status == "pending"
    assert row.miss_count == 0


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_counts_recent_rows_by_status(db):
    org = get_or_create_org(db, "acme")
    other = get_or_create_org(db, "globex")
    _row(db, org_id=org.id, uid="1", status="sent")
    _row(db, org_id=org.id, uid="2", status="sent")
    _row(db, org_id=org.id, uid="3", status="handled")
    _row(db, org_id=org.id, uid="4", status="pending")
    _row(db, org_id=org.id, uid="5", status="unknown")
    _row(db, org_id=org.id, uid="6", status="sent",
         created_at=_NOW - timedelta(days=WINDOW_DAYS + 1))
    _row(db, org_id=other.id, uid="7", status="sent")

    assert summary(db, org.id, now=_NOW) == {
        "sent": 2, "handled": 1, "pending": 1, "window_days": WINDOW_DAYS,
    }


def test_marker_prefix_matches_email_trigger():
    """The prefix is repeated in draft_outcomes (import cycle), so pin the
    two definitions together."""
    from ui.backend.email_trigger import draft_marker_prefix

    assert draft_outcomes._marker_prefix(7, 3) == draft_marker_prefix(7, 3)
