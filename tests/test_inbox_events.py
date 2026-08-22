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


# The mailbox most of this module records under; `claim_events` and
# `has_pending_events` are scoped to a mailbox and generation, so a claim
# has to name the one the rows were recorded for.
_MAILBOX = dict(mailbox_identity="m", mailbox_generation="99")


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
    # The filter's `decision` is opt-in, not implicit: a row nobody filtered
    # carries no reason. Phase 4a writes it (via `record_events(decisions=)`)
    # only for a UID the pre-LLM filter actually rejected.
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

    claimed = store.claim_events(db, org_id=1, run_id="run-a", limit=3, **_MAILBOX)
    db.commit()
    assert [e.external_id for e in claimed] == ["1", "2", "3"]
    assert all(e.status == "claimed" and e.run_id == "run-a" for e in claimed)
    # Claiming does NOT charge an attempt -- a pipeline that fails to build is
    # not the message's fault (see mark_dispatched).
    assert all(e.attempts == 0 for e in claimed)


def test_a_second_claim_never_overlaps_the_first(db):
    from ui.backend.db import inbox_events as store

    kw = dict(org_id=1, mailbox_identity="m", mailbox_generation="99")
    store.record_events(db, external_ids=["1", "2", "3", "4", "5"], **kw)
    db.commit()

    first = store.claim_events(db, org_id=1, run_id="run-a", limit=3, **_MAILBOX)
    db.commit()
    second = store.claim_events(db, org_id=1, run_id="run-b", limit=3, **_MAILBOX)
    db.commit()
    assert [e.external_id for e in second] == ["4", "5"]
    assert not ({e.id for e in first} & {e.id for e in second})


def test_claiming_an_empty_queue_returns_nothing(db):
    from ui.backend.db import inbox_events as store

    assert store.claim_events(db, org_id=1, run_id="run-a", limit=3, **_MAILBOX) == []


def test_claim_is_scoped_to_one_org(db):
    from ui.backend.db import inbox_events as store

    db.add(Organization(id=2, name="other"))
    db.commit()
    store.record_events(db, org_id=2, mailbox_identity="m2", mailbox_generation="1",
                        external_ids=["9"])
    db.commit()
    assert store.claim_events(db, org_id=1, run_id="run-a", limit=3, **_MAILBOX) == []


# ---------------------------------------------------------------------------
# The store: terminal outcomes, release, and the dead-letter budget.
# ---------------------------------------------------------------------------


def _claimed(db, ids, run_id="run-a"):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="99",
                        external_ids=ids)
    db.commit()
    claimed = store.claim_events(db, org_id=1, run_id=run_id, limit=len(ids),
                                 mailbox_identity="m", mailbox_generation="99")
    db.commit()
    return claimed


def test_dispatch_charges_exactly_one_attempt(db):
    from ui.backend.db import inbox_events as store

    _claimed(db, ["1", "2"])
    store.mark_dispatched(db, "run-a")
    db.commit()
    assert [e.attempts for e in db.query(InboxEvent).order_by(InboxEvent.id)] == [1, 1]


def test_completion_splits_drafted_from_undrafted(db):
    from ui.backend.db import inbox_events as store

    _claimed(db, ["1", "2", "3"])
    # A run failed after drafting for 1 and 3 -- those must never be
    # reprocessed (email_draft_reply has no dedup), only 2 is retryable.
    store.complete_events(db, "run-a", done_external_ids={"1", "3"}, error="boom")
    db.commit()
    rows = {e.external_id: e for e in db.query(InboxEvent).all()}
    assert rows["1"].status == "done" and rows["3"].status == "done"
    assert rows["2"].status == "failed" and rows["2"].last_error == "boom"
    assert rows["1"].completed_at is not None


def test_completion_with_everything_done_marks_all_done(db):
    from ui.backend.db import inbox_events as store

    _claimed(db, ["1", "2"])
    store.complete_events(db, "run-a", done_external_ids={"1", "2"}, error=None)
    db.commit()
    assert {e.status for e in db.query(InboxEvent)} == {"done"}


def test_release_returns_rows_to_pending_below_the_attempt_limit(db):
    from ui.backend.db import inbox_events as store

    _claimed(db, ["1", "2"])
    store.mark_dispatched(db, "run-a")
    db.commit()
    assert store.release_events(db, "run-a", max_attempts=3, error="crashed") == 0
    db.commit()
    rows = db.query(InboxEvent).all()
    assert {e.status for e in rows} == {"pending"}
    assert all(e.run_id is None for e in rows)
    assert all(e.attempts == 1 for e in rows)  # the attempt stays charged


def test_release_dead_letters_at_the_attempt_limit(db):
    from ui.backend.db import inbox_events as store

    claimed = _claimed(db, ["1"])
    claimed[0].attempts = 3
    db.commit()
    assert store.release_events(db, "run-a", max_attempts=3, error="crashed") == 1
    db.commit()
    row = db.query(InboxEvent).one()
    assert row.status == "failed" and row.last_error == "crashed"


def test_release_without_an_attempt_charged_is_penalty_free(db):
    from ui.backend.db import inbox_events as store

    # A pipeline that fails to BUILD never charged an attempt, so it returns to
    # pending and retries forever -- today's behaviour, and correct: a broken
    # team config must not dead-letter a whole day of an org's mail.
    _claimed(db, ["1"])
    assert store.release_events(db, "run-a", max_attempts=3, error=None) == 0
    db.commit()
    row = db.query(InboxEvent).one()
    assert (row.status, row.attempts) == ("pending", 0)


def test_retry_moves_only_the_redone_events_and_resets_the_budget(db):
    from ui.backend.db import inbox_events as store

    _claimed(db, ["1", "2"])
    store.mark_dispatched(db, "run-a")
    store.complete_events(db, "run-a", done_external_ids={"1"}, error="boom")
    db.commit()
    moved = store.resolve_retry_events(
        db, from_run_id="run-a", to_run_id="run-b",
        retry_external_ids=["2"], done_external_ids=[],
    )
    db.commit()
    assert moved == 1
    rows = {e.external_id: e for e in db.query(InboxEvent)}
    assert rows["1"].status == "done"  # already drafted: never redone
    assert (rows["2"].status, rows["2"].attempts, rows["2"].run_id) == ("claimed", 0, "run-b")


def test_retry_marks_late_discovered_drafts_done_instead_of_redoing_them(db):
    from ui.backend.db import inbox_events as store

    # The mailbox scan can only discover an APPENDed-but-unrecorded draft at
    # retry time. Such a message must go terminal, not move to the new run.
    _claimed(db, ["1", "2"])
    store.complete_events(db, "run-a", done_external_ids=set(), error="boom")
    db.commit()
    moved = store.resolve_retry_events(
        db, from_run_id="run-a", to_run_id="run-b",
        retry_external_ids=["2"], done_external_ids=["1"],
    )
    db.commit()
    assert moved == 1
    rows = {e.external_id: e for e in db.query(InboxEvent)}
    assert (rows["1"].status, rows["1"].run_id) == ("done", "run-a")
    assert (rows["2"].status, rows["2"].run_id) == ("claimed", "run-b")


def test_retry_of_a_run_with_no_events_moves_nothing(db):
    from ui.backend.db import inbox_events as store

    assert store.resolve_retry_events(
        db, from_run_id="pre-ledger", to_run_id="run-b",
        retry_external_ids=["1"], done_external_ids=[],
    ) == 0


def test_the_attempt_count_release_reads_is_not_stale(db):
    from ui.backend.db import inbox_events as store

    # `session_factory` sets expire_on_commit=False, so a Core UPDATE that does
    # not synchronise the session leaves the ORM object holding the OLD attempt
    # count. release_events reads that value to decide on dead-lettering, so a
    # stale read silently disables the budget entirely and a poison message
    # loops forever.
    _claimed(db, ["1"])
    store.mark_dispatched(db, "run-a")
    db.commit()
    assert db.query(InboxEvent).one().attempts == 1
    assert store.release_events(db, "run-a", max_attempts=1, error="crashed") == 1
    db.commit()
    assert db.query(InboxEvent).one().status == "failed"


# ---------------------------------------------------------------------------
# The store: the pre-LLM filter (Phase 4a) -- `filtered` status and release.
# ---------------------------------------------------------------------------


def test_a_decision_records_the_row_filtered_not_pending(db):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="1",
                        external_ids=["10", "11"], decisions={"11": "bulk:list-id"})
    db.commit()
    rows = {r.external_id: r for r in db.query(InboxEvent).all()}
    assert rows["10"].status == "pending" and rows["10"].decision is None
    assert rows["11"].status == "filtered" and rows["11"].decision == "bulk:list-id"


def test_a_filtered_row_is_never_claimed(db):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="1",
                        external_ids=["10", "11"], decisions={"10": "not_allowlisted"})
    db.commit()
    claimed = store.claim_events(db, org_id=1, run_id="run-a", limit=10,
                                 mailbox_identity="m", mailbox_generation="1")
    assert [e.external_id for e in claimed] == ["11"]


def test_releasing_a_filtered_row_makes_it_claimable(db):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="1",
                        external_ids=["10"], decisions={"10": "bulk:precedence"})
    db.commit()
    row = db.query(InboxEvent).one()

    assert store.release_filtered_event(db, org_id=1, event_id=row.id) is True
    db.commit()

    db.refresh(row)
    assert row.status == "pending"
    assert row.decision is None
    claimed = store.claim_events(db, org_id=1, run_id="run-a", limit=10,
                                 mailbox_identity="m", mailbox_generation="1")
    assert [e.external_id for e in claimed] == ["10"]


def test_releasing_twice_reports_the_second_as_nothing_to_do(db):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="1",
                        external_ids=["10"], decisions={"10": "bulk:precedence"})
    db.commit()
    row = db.query(InboxEvent).one()
    assert store.release_filtered_event(db, org_id=1, event_id=row.id) is True
    db.commit()
    # Already pending -- a second release changes nothing and says so, which is
    # what lets the route answer 404 without a separate existence query.
    assert store.release_filtered_event(db, org_id=1, event_id=row.id) is False


def test_releasing_never_resurrects_a_row_that_already_ran(db):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="1",
                        external_ids=["10"])
    db.commit()
    row = db.query(InboxEvent).one()
    row.status = "done"
    db.commit()
    assert store.release_filtered_event(db, org_id=1, event_id=row.id) is False
    db.refresh(row)
    assert row.status == "done"


def test_releasing_another_orgs_row_does_nothing(db):
    from ui.backend.db import inbox_events as store

    db.add(Organization(id=2, name="other"))
    db.commit()
    store.record_events(db, org_id=2, mailbox_identity="m2", mailbox_generation="1",
                        external_ids=["10"], decisions={"10": "bulk:precedence"})
    db.commit()
    row = db.query(InboxEvent).one()
    assert store.release_filtered_event(db, org_id=1, event_id=row.id) is False
    db.refresh(row)
    assert row.status == "filtered"


def test_filtered_events_list_newest_first_and_only_this_org(db):
    from ui.backend.db import inbox_events as store

    db.add(Organization(id=2, name="other"))
    db.commit()
    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="1",
                        external_ids=["10", "11"],
                        decisions={"10": "bulk:list-id", "11": "not_allowlisted"})
    store.record_events(db, org_id=2, mailbox_identity="m2", mailbox_generation="1",
                        external_ids=["99"], decisions={"99": "bulk:list-id"})
    db.commit()
    rows = store.list_filtered_events(db, org_id=1, limit=10)
    assert [r.external_id for r in rows] == ["11", "10"]


def test_recording_with_no_decisions_is_unchanged(db):
    # The existing call site passes no `decisions` at all; that path must stay
    # exactly equivalent to today's behaviour.
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="1",
                        external_ids=["10", "11"])
    db.commit()
    assert {r.status for r in db.query(InboxEvent).all()} == {"pending"}
    assert all(r.decision is None for r in db.query(InboxEvent).all())


# --- mailbox scoping (spec 2026-08-22, D3/D4) ---------------------------

def test_a_claim_never_reaches_another_mailbox_generation(db):
    """An IMAP UID is only meaningful within one UIDVALIDITY.

    A mailbox rebuild reissues UIDs, so a `pending` row from generation 99 is
    a *different message* under generation 100. Claiming it and handing it to
    a run bound to the new generation makes that run read, and possibly reply
    to, an unrelated email.
    """
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m",
                        mailbox_generation="99", external_ids=["7"])
    db.commit()

    claimed = store.claim_events(db, org_id=1, run_id="run-a", limit=10,
                                 mailbox_identity="m", mailbox_generation="100")
    assert claimed == []
    assert store.has_pending_events(db, org_id=1, mailbox_identity="m",
                                    mailbox_generation="100") is False


def test_a_claim_never_reaches_another_mailbox(db):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="imap.old:u@old",
                        mailbox_generation="99", external_ids=["7"])
    db.commit()

    assert store.claim_events(db, org_id=1, run_id="run-a", limit=10,
                              mailbox_identity="imap.new:u@new",
                              mailbox_generation="99") == []
    assert store.has_pending_events(db, org_id=1,
                                    mailbox_identity="imap.new:u@new",
                                    mailbox_generation="99") is False


def test_the_current_mailbox_and_generation_still_claim_normally(db):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m",
                        mailbox_generation="99", external_ids=["7", "8"])
    db.commit()

    assert store.has_pending_events(db, org_id=1, mailbox_identity="m",
                                    mailbox_generation="99") is True
    claimed = store.claim_events(db, org_id=1, run_id="run-a", limit=10,
                                 mailbox_identity="m", mailbox_generation="99")
    assert [e.external_id for e in claimed] == ["7", "8"]


def test_abandoning_supersedes_every_pending_row_from_another_mailbox(db):
    """Scoping alone leaves the old rows `pending` forever with nothing
    reporting them, so the two generation-change sites mark them terminal."""
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="imap.old:u@old",
                        mailbox_generation="99", external_ids=["7"])
    store.record_events(db, org_id=1, mailbox_identity="imap.new:u@new",
                        mailbox_generation="1", external_ids=["1"])
    db.commit()

    abandoned = store.abandon_superseded_events(
        db, org_id=1, mailbox_identity="imap.new:u@new", mailbox_generation="1"
    )
    db.commit()

    assert abandoned == 1
    rows = {e.mailbox_identity: e for e in db.query(InboxEvent).all()}
    old = rows["imap.old:u@old"]
    assert old.status == "failed"
    assert old.last_error and "mailbox" in old.last_error.lower()
    assert old.completed_at is not None
    assert rows["imap.new:u@new"].status == "pending"  # the live mailbox is untouched


def test_abandoning_without_a_generation_matches_on_the_mailbox_alone(db):
    """The identity-change site does not yet know the new mailbox's
    UIDVALIDITY, so it must be able to supersede on identity alone."""
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="imap.old:u@old",
                        mailbox_generation="99", external_ids=["7"])
    store.record_events(db, org_id=1, mailbox_identity="imap.new:u@new",
                        mailbox_generation="42", external_ids=["1"])
    db.commit()

    assert store.abandon_superseded_events(
        db, org_id=1, mailbox_identity="imap.new:u@new"
    ) == 1
    db.commit()

    rows = {e.mailbox_identity: e.status for e in db.query(InboxEvent).all()}
    assert rows == {"imap.old:u@old": "failed", "imap.new:u@new": "pending"}


def test_abandoning_leaves_claimed_and_terminal_rows_alone(db):
    """A `claimed` row belongs to a run that will complete, be released by the
    watchdog, or be released at startup -- racing that gives one batch two
    owners."""
    from ui.backend.db import inbox_events as store

    for external_id, status in (("7", "claimed"), ("8", "done"), ("9", "failed")):
        db.add(InboxEvent(org_id=1, connector_type="imap",
                          mailbox_identity="imap.old:u@old", mailbox_generation="99",
                          external_id=external_id, status=status))
    db.commit()

    assert store.abandon_superseded_events(
        db, org_id=1, mailbox_identity="imap.new:u@new", mailbox_generation="1"
    ) == 0
    db.commit()

    rows = {e.external_id: e.status for e in db.query(InboxEvent).all()}
    assert rows == {"7": "claimed", "8": "done", "9": "failed"}


def test_abandoning_retires_a_superseded_mailboxs_filtered_rows(db):
    """A `filtered` row is releasable, and release is a flip to `pending` that
    does not re-check the mailbox. Left behind, it stays in the release list,
    the admin releases it, the API answers `released: true` -- and the scoped
    claim query can never take it, so the message silently disappears. It has
    to be retired with the backlog it belongs to."""
    from ui.backend.db import inbox_events as store

    db.add(InboxEvent(org_id=1, connector_type="imap",
                      mailbox_identity="imap.old:u@old", mailbox_generation="99",
                      external_id="7", status="filtered", decision="not_allowlisted"))
    db.commit()

    assert store.abandon_superseded_events(
        db, org_id=1, mailbox_identity="imap.new:u@new", mailbox_generation="1"
    ) == 1
    db.commit()

    row = db.query(InboxEvent).filter_by(external_id="7").one()
    assert row.status == "failed"
    assert store.list_filtered_events(db, org_id=1, limit=10) == []


def test_abandoning_leaves_the_current_mailboxs_filtered_rows_releasable(db):
    from ui.backend.db import inbox_events as store

    db.add(InboxEvent(org_id=1, connector_type="imap",
                      mailbox_identity="imap.new:u@new", mailbox_generation="1",
                      external_id="7", status="filtered", decision="not_allowlisted"))
    db.commit()

    assert store.abandon_superseded_events(
        db, org_id=1, mailbox_identity="imap.new:u@new", mailbox_generation="1"
    ) == 0
    db.commit()

    assert [e.external_id for e in store.list_filtered_events(db, org_id=1, limit=10)] == ["7"]


def test_abandoning_is_scoped_to_one_org(db):
    from ui.backend.db import inbox_events as store

    db.add(Organization(id=2, name="other"))
    db.commit()
    store.record_events(db, org_id=2, mailbox_identity="imap.other:u@other",
                        mailbox_generation="1", external_ids=["9"])
    db.commit()

    assert store.abandon_superseded_events(
        db, org_id=1, mailbox_identity="imap.new:u@new", mailbox_generation="1"
    ) == 0
    assert db.query(InboxEvent).filter_by(org_id=2).one().status == "pending"
