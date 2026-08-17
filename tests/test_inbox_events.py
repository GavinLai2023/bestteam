"""The durable per-message inbox ledger (email automation Phase 1).

`inbox_events` decouples "this message needs processing" from the run that
processes it, so the commit that advances the mailbox cursor can no longer
consume mail that nothing ever ran. See
docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md.
"""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")

from sqlalchemy.exc import IntegrityError

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import InboxEvent, Organization


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    session.add(Organization(id=1, name="acme"))
    session.commit()
    yield session
    session.close()


def _event(**over):
    base = dict(
        org_id=1,
        connector_type="imap",
        mailbox_identity="imap.acme.com:u@acme.com",
        mailbox_generation="99",
        external_id="7",
        status="pending",
    )
    base.update(over)
    return InboxEvent(**base)


def test_the_same_message_cannot_be_recorded_twice(db):
    db.add(_event())
    db.commit()
    db.add(_event())
    with pytest.raises(IntegrityError):
        db.commit()


def test_the_same_uid_in_a_new_mailbox_generation_is_a_distinct_message(db):
    # After a mailbox rebuild UIDVALIDITY changes and UID 7 is a DIFFERENT
    # message -- if the unique key ignored the generation it would look like a
    # duplicate and be skipped forever.
    db.add(_event())
    db.add(_event(mailbox_generation="100"))
    db.commit()
    assert db.query(InboxEvent).count() == 2


def test_defaults_are_pending_with_no_run_and_no_attempts(db):
    db.add(_event())
    db.commit()
    row = db.query(InboxEvent).one()
    assert (row.status, row.run_id, row.attempts) == ("pending", None, 0)
    assert row.detected_at is not None
    # Phase 4's filter hook: reserved, never written today.
    assert row.decision is None


# ---------------------------------------------------------------------------
# The store: recording detected mail, and claiming it for a run.
# ---------------------------------------------------------------------------


def test_recording_the_same_ids_twice_inserts_each_only_once(db):
    from ui.backend.db import inbox_events as store

    kw = dict(org_id=1, mailbox_identity="m", mailbox_generation="99")
    assert store.record_events(db, external_ids=["1", "2", "3"], **kw) == 3
    db.commit()
    # Re-detection of an overlapping window must be a no-op, not a duplicate:
    # this is what lets the cursor be a performance optimisation rather than a
    # correctness requirement.
    assert store.record_events(db, external_ids=["2", "3", "4"], **kw) == 1
    db.commit()
    assert {e.external_id for e in db.query(InboxEvent).all()} == {"1", "2", "3", "4"}


def test_recording_nothing_is_a_no_op(db):
    from ui.backend.db import inbox_events as store

    assert store.record_events(
        db, org_id=1, mailbox_identity="m", mailbox_generation="99", external_ids=[]
    ) == 0


def test_claim_takes_the_oldest_pending_rows_up_to_the_limit(db):
    from ui.backend.db import inbox_events as store

    kw = dict(org_id=1, mailbox_identity="m", mailbox_generation="99")
    store.record_events(db, external_ids=["1", "2", "3", "4", "5"], **kw)
    db.commit()

    claimed = store.claim_events(db, org_id=1, run_id="run-a", limit=3)
    db.commit()
    assert [e.external_id for e in claimed] == ["1", "2", "3"]
    assert all(e.status == "claimed" and e.run_id == "run-a" for e in claimed)
    # Claiming does NOT charge an attempt -- a workflow that fails to build is
    # not the message's fault (see mark_dispatched).
    assert all(e.attempts == 0 for e in claimed)


def test_a_second_claim_never_overlaps_the_first(db):
    from ui.backend.db import inbox_events as store

    kw = dict(org_id=1, mailbox_identity="m", mailbox_generation="99")
    store.record_events(db, external_ids=["1", "2", "3", "4", "5"], **kw)
    db.commit()

    first = store.claim_events(db, org_id=1, run_id="run-a", limit=3)
    db.commit()
    second = store.claim_events(db, org_id=1, run_id="run-b", limit=3)
    db.commit()
    assert [e.external_id for e in second] == ["4", "5"]
    assert not ({e.id for e in first} & {e.id for e in second})


def test_claiming_an_empty_queue_returns_nothing(db):
    from ui.backend.db import inbox_events as store

    assert store.claim_events(db, org_id=1, run_id="run-a", limit=3) == []


def test_claim_is_scoped_to_one_org(db):
    from ui.backend.db import inbox_events as store

    db.add(Organization(id=2, name="other"))
    db.commit()
    store.record_events(db, org_id=2, mailbox_identity="m2", mailbox_generation="1",
                        external_ids=["9"])
    db.commit()
    assert store.claim_events(db, org_id=1, run_id="run-a", limit=3) == []
