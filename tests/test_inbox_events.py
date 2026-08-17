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
