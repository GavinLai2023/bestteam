# Email Phase 1: Durable Inbox Events — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the record of "this message needs processing" durable and independent of the run that processes it, closing the window where a process killed after the UID baseline advances silently consumes mail.

**Architecture:** A new `inbox_events` table is written in the *same commit* that advances the mailbox cursor, so mail is never consumed without a durable record. A run then *claims* pending rows via one atomic `UPDATE`; the claimed rows' external ids become the batch. Completion is driven from `runtime.py` using Phase 0's `already_drafted_uids` as the per-message evidence signal. Infrastructure-class failures release rows for reprocessing; workflow-class failures leave them terminal for the existing human retry.

**Tech Stack:** Python 3, SQLAlchemy 2 (`Mapped`/`mapped_column`), Alembic, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md`

## Global Constraints

- Run everything through the project venv: `./.venv/Scripts/python.exe`.
- Every test file needs a `pytestmark` marker (`unit`/`integration`/`e2e`/`optional`) or `tests/test_marker_completeness.py` fails the suite.
- Code comments in English. British spelling in prose.
- `EmailTrigger.last_uid` / `uidvalidity` must NOT be changed or removed — the spec's forward-looking mandate applies only to the new table.
- `_dispatch_lock` must NOT be removed — the overlap guard still reads the in-process `RunRegistry`. This phase does not claim multi-worker support.
- `mailbox_generation` is `str` defaulting to `""`, never `NULL` — SQLite treats `NULL`s as distinct in a `UNIQUE` constraint, which would silently disable dedup.
- `trigger_context` keeps its exact current shape (`uids`, `mailbox_credential_id`, `mailbox_host`, `mailbox_username`, `uidvalidity`, `folder`, `triggered_at`, optional `result_contract`) so `automation_results.py` and the property-maintenance contract need no changes.
- Alembic migrations must be guarded (`_has_table`) because `db_session.py` runs `create_all` at import.
- Do not use `-n auto` when verifying; `backend-full` parity means serial, one process.

---

### Task 1: The `InboxEvent` model and its migration

**Files:**
- Modify: `ui/backend/db/models.py` (add class after `EmailTrigger`, ~line 431)
- Create: `alembic/versions/h5i6j7k8l9m0_add_inbox_events.py`
- Test: `tests/test_inbox_events.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: `ui.backend.db.models.InboxEvent` with columns `id: int`, `org_id: int`, `connector_type: str`, `mailbox_identity: str`, `mailbox_generation: str`, `external_id: str`, `status: str`, `run_id: Optional[str]`, `attempts: int`, `decision: Optional[str]`, `last_error: Optional[str]`, `detected_at: datetime`, `claimed_at: Optional[datetime]`, `completed_at: Optional[datetime]`. Status constants `EVENT_PENDING = "pending"`, `EVENT_CLAIMED = "claimed"`, `EVENT_DONE = "done"`, `EVENT_FAILED = "failed"` live in `ui/backend/db/inbox_events.py` (Task 2), not in models.

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_events.py`:

```python
"""Durable per-message inbox ledger (email automation Phase 1)."""
import pytest

from ui.backend.db import make_engine, init_db, session_factory
from ui.backend.db.models import InboxEvent, Organization

pytestmark = pytest.mark.unit


@pytest.fixture()
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    with session_factory(engine)() as session:
        session.add(Organization(id=1, name="acme", slug="acme"))
        session.commit()
        yield session


def _event(**over):
    base = dict(
        org_id=1, connector_type="imap", mailbox_identity="imap.acme.com:u@acme.com",
        mailbox_generation="99", external_id="7", status="pending",
    )
    base.update(over)
    return InboxEvent(**base)


def test_the_same_message_cannot_be_recorded_twice(db):
    from sqlalchemy.exc import IntegrityError

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
    assert row.decision is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events.py -v`
Expected: FAIL — `ImportError: cannot import name 'InboxEvent'`

- [ ] **Step 3: Write minimal implementation**

In `ui/backend/db/models.py`, after the `EmailTrigger` class:

```python
class InboxEvent(Base):
    """One detected inbound message, durable and independent of any run.

    The row exists so the commit that consumes the mail (advancing
    `EmailTrigger.last_uid`) is the SAME commit that records the work. Before
    this, `_start_triggered_run` advanced the cursor and only then handed the
    workflow to a thread pool, so a process killed in between consumed mail
    that nothing ever processed.

    Identity is `(org, connector, mailbox, generation, external_id)`. The
    generation matters: an IMAP UID is only meaningful within a UIDVALIDITY,
    so after a mailbox rebuild UID 7 is a different message and must not be
    mistaken for a duplicate. It is `""` (never NULL) for connectors with no
    such concept -- SQLite treats NULLs as distinct in a UNIQUE constraint,
    which would silently disable dedup.

    `connector_type`/`mailbox_generation`/`external_id` are deliberately
    connector-neutral: Phase 2 adds Graph/Gmail, and this table will hold real
    customer rows by then. `decision` is reserved for Phase 4's pre-LLM filter
    (why a message was skipped) and is never written today; `filtered` is a
    documented but currently unreachable status.
    See docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md.
    """

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "connector_type", "mailbox_identity",
            "mailbox_generation", "external_id",
            name="uq_inbox_events_identity",
        ),
        Index("ix_inbox_events_org_id_status_id", "org_id", "status", "id"),
        Index("ix_inbox_events_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    connector_type: Mapped[str] = mapped_column(default="imap")
    mailbox_identity: Mapped[str]
    mailbox_generation: Mapped[str] = mapped_column(default="")
    external_id: Mapped[str]
    # pending | claimed | done | failed | filtered (filtered: Phase 4, unused)
    status: Mapped[str] = mapped_column(default="pending")
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    # Charged when a run is actually dispatched, never at claim -- see
    # inbox_events.py::mark_dispatched.
    attempts: Mapped[int] = mapped_column(default=0)
    decision: Mapped[Optional[str]] = mapped_column(nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(nullable=True)
    detected_at: Mapped[datetime] = mapped_column(default=_utcnow)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events.py -v`
Expected: 3 passed

- [ ] **Step 5: Write the migration**

Create `alembic/versions/h5i6j7k8l9m0_add_inbox_events.py`:

```python
"""add inbox_events (durable per-message ledger, email automation Phase 1)

Revision ID: h5i6j7k8l9m0
Revises: d2e3f4a5b6c7
Create Date: 2026-08-17 12:00:00.000000

Decouples "this message needs processing" from the run that processes it, so
advancing the mailbox cursor can no longer consume mail that nothing ran.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the table when
this migration runs. No backfill -- existing triggers keep their `last_uid`
and record events for whatever arrives above it on the next poll.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'h5i6j7k8l9m0'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "inbox_events"):
        return
    op.create_table(
        "inbox_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("connector_type", sa.String(), nullable=False, server_default="imap"),
        sa.Column("mailbox_identity", sa.String(), nullable=False),
        sa.Column("mailbox_generation", sa.String(), nullable=False, server_default=""),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "org_id", "connector_type", "mailbox_identity",
            "mailbox_generation", "external_id",
            name="uq_inbox_events_identity",
        ),
    )
    op.create_index(
        "ix_inbox_events_org_id_status_id", "inbox_events", ["org_id", "status", "id"]
    )
    op.create_index("ix_inbox_events_run_id", "inbox_events", ["run_id"])


def downgrade() -> None:
    """Downgrade schema (drops the durable ledger)."""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "inbox_events"):
        op.drop_index("ix_inbox_events_run_id", table_name="inbox_events")
        op.drop_index("ix_inbox_events_org_id_status_id", table_name="inbox_events")
        op.drop_table("inbox_events")
```

- [ ] **Step 6: Verify the migration applies to a real file database**

Run:
```bash
./.venv/Scripts/python.exe -c "
import subprocess, tempfile, os, pathlib
d = tempfile.mkdtemp(); db = pathlib.Path(d, 'x.db')
env = dict(os.environ, BESTTEAM_DB_PATH=str(db))
print(subprocess.run(['./.venv/Scripts/python.exe','-m','alembic','upgrade','head'], env=env, capture_output=True, text=True).stderr[-800:])
"
```
Expected: alembic runs through `h5i6j7k8l9m0` with no error. Also confirm `./.venv/Scripts/python.exe -m alembic heads` prints `h5i6j7k8l9m0 (head)` (single head — a second head means the `down_revision` is wrong).

- [ ] **Step 7: Commit**

```bash
git add ui/backend/db/models.py alembic/versions/h5i6j7k8l9m0_add_inbox_events.py tests/test_inbox_events.py
git commit -m "feat(email): add the inbox_events durable message ledger"
```

---

### Task 2: The store — record and claim

**Files:**
- Create: `ui/backend/db/inbox_events.py`
- Test: `tests/test_inbox_events.py` (append)

**Interfaces:**
- Consumes: `InboxEvent` from Task 1.
- Produces:
  - `EVENT_PENDING`, `EVENT_CLAIMED`, `EVENT_DONE`, `EVENT_FAILED` string constants.
  - `mailbox_identity(host: str, username: str) -> str`
  - `record_events(db, *, org_id, mailbox_identity, mailbox_generation, external_ids: Sequence[str], connector_type="imap") -> int` — inserts missing rows as `pending`, returns how many were inserted. **Does not commit.**
  - `claim_events(db, *, org_id, run_id: str, limit: int) -> list[InboxEvent]` — atomically claims up to `limit` oldest `pending` rows, returns them. **Does not commit.**

- [ ] **Step 1: Write the failing test**

Append to `tests/test_inbox_events.py`:

```python
def test_recording_the_same_ids_twice_inserts_each_only_once(db):
    from ui.backend.db import inbox_events as store

    kw = dict(org_id=1, mailbox_identity="m", mailbox_generation="99")
    assert store.record_events(db, external_ids=["1", "2", "3"], **kw) == 3
    db.commit()
    # Re-detection of an overlapping window must be a no-op, not a duplicate.
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

    db.add(Organization(id=2, name="other", slug="other"))
    db.commit()
    store.record_events(db, org_id=2, mailbox_identity="m2", mailbox_generation="1",
                        external_ids=["9"])
    db.commit()
    assert store.claim_events(db, org_id=1, run_id="run-a", limit=3) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events.py -v`
Expected: FAIL — `ModuleNotFoundError: ui.backend.db.inbox_events`

- [ ] **Step 3: Write minimal implementation**

Create `ui/backend/db/inbox_events.py`:

```python
"""Durable per-message inbox ledger (email automation Phase 1).

Detection records a `pending` row per new message in the SAME transaction that
advances the mailbox cursor, so mail can never be consumed without a durable
record of the work. A run then claims rows; the claimed rows' `external_id`s
are the batch it processes.

None of these helpers commit. Callers own the transaction boundary -- that is
the entire point of the design, since the durability guarantee comes from
detection's insert and the cursor advance landing in one commit.

See docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import InboxEvent

EVENT_PENDING = "pending"
EVENT_CLAIMED = "claimed"
EVENT_DONE = "done"
EVENT_FAILED = "failed"

DEFAULT_CONNECTOR = "imap"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mailbox_identity(host: str, username: str) -> str:
    """Stable identity for one mailbox, independent of the credential row id.

    `set_email_credentials` upserts one row per org, so the row id survives the
    customer replacing the mailbox entirely -- host/username are what actually
    change (the same reasoning `_start_triggered_run` already applies when it
    stamps `mailbox_host`/`mailbox_username` into `trigger_context`).
    """
    return f"{host}:{username}".lower()


def record_events(
    db: Session,
    *,
    org_id: int,
    mailbox_identity: str,
    mailbox_generation: str,
    external_ids: Sequence[str],
    connector_type: str = DEFAULT_CONNECTOR,
) -> int:
    """Record each id as a `pending` event, ignoring ones already known.

    Idempotent by the table's unique key, which is what lets the mailbox cursor
    degrade from a correctness requirement to a performance optimisation:
    losing it causes messages to be re-examined and skipped, never processed
    twice.

    `on_conflict_do_nothing` is SQLite-specific -- one of the places a future
    Postgres migration would touch (the Postgres dialect offers the same call).
    """
    if not external_ids:
        return 0
    now = _utcnow()
    rows = [
        {
            "org_id": org_id,
            "connector_type": connector_type,
            "mailbox_identity": mailbox_identity,
            "mailbox_generation": mailbox_generation,
            "external_id": str(external_id),
            "status": EVENT_PENDING,
            "attempts": 0,
            "detected_at": now,
        }
        for external_id in external_ids
    ]
    result = db.execute(
        sqlite_insert(InboxEvent).values(rows).on_conflict_do_nothing(
            index_elements=[
                "org_id", "connector_type", "mailbox_identity",
                "mailbox_generation", "external_id",
            ]
        )
    )
    return result.rowcount or 0


def claim_events(db: Session, *, org_id: int, run_id: str, limit: int) -> List[InboxEvent]:
    """Atomically claim up to `limit` of this org's oldest pending events.

    One UPDATE, so under SQLite's write lock two claimants cannot be handed the
    same message. (That removes one class of cross-process duplication; it does
    NOT make the poller multi-worker safe on its own -- the overlap guard still
    reads the in-process RunRegistry. See the spec's scope boundary.)

    Deliberately does not touch `attempts`: see `mark_dispatched`.
    """
    if limit <= 0:
        return []
    oldest_pending = (
        select(InboxEvent.id)
        .where(InboxEvent.org_id == org_id, InboxEvent.status == EVENT_PENDING)
        .order_by(InboxEvent.id)
        .limit(limit)
    )
    db.execute(
        update(InboxEvent)
        .where(InboxEvent.id.in_(oldest_pending))
        .values(status=EVENT_CLAIMED, run_id=run_id, claimed_at=_utcnow())
    )
    return list(
        db.execute(
            select(InboxEvent)
            .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED)
            .order_by(InboxEvent.id)
        ).scalars()
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/inbox_events.py tests/test_inbox_events.py
git commit -m "feat(email): record and claim inbox events"
```

---

### Task 3: The store — dispatch, complete, release, dead-letter

**Files:**
- Modify: `ui/backend/db/inbox_events.py`
- Modify: `ui/backend/email_trigger.py` (env var only, near `RUN_TIMEOUT_ENV` ~line 272 and `validate_trigger_env` ~line 316)
- Test: `tests/test_inbox_events.py` (append), `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: Task 2's constants and `claim_events`.
- Produces:
  - `mark_dispatched(db, run_id) -> None` — `attempts += 1` for the run's claimed rows.
  - `complete_events(db, run_id, *, done_external_ids: set[str], error: Optional[str]) -> None` — claimed rows whose `external_id` is in `done_external_ids` become `done`; the rest become `failed` with `last_error=error`.
  - `release_events(db, run_id, *, max_attempts: int, error: Optional[str]) -> int` — claimed rows go back to `pending` (`run_id` cleared), or to `failed` when `attempts >= max_attempts`. Returns how many were dead-lettered.
  - `ui.backend.email_trigger.max_event_attempts() -> int` (env `BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS`, default 3, minimum 1, validated in `validate_trigger_env`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_inbox_events.py`:

```python
def _claimed(db, ids, run_id="run-a"):
    from ui.backend.db import inbox_events as store

    store.record_events(db, org_id=1, mailbox_identity="m", mailbox_generation="99",
                        external_ids=ids)
    db.commit()
    claimed = store.claim_events(db, org_id=1, run_id=run_id, limit=len(ids))
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

    # A workflow that fails to BUILD never charged an attempt, so it returns to
    # pending and retries forever -- today's behaviour, and correct: a broken
    # team config must not dead-letter a whole day of an org's mail.
    _claimed(db, ["1"])
    assert store.release_events(db, "run-a", max_attempts=3, error=None) == 0
    db.commit()
    row = db.query(InboxEvent).one()
    assert (row.status, row.attempts) == ("pending", 0)
```

Append to `tests/test_email_trigger.py`:

```python
def test_max_event_attempts_defaults_to_three(monkeypatch):
    monkeypatch.delenv(email_trigger.MAX_EVENT_ATTEMPTS_ENV, raising=False)
    assert email_trigger.max_event_attempts() == 3


def test_a_bad_max_event_attempts_fails_fast_at_startup(monkeypatch):
    monkeypatch.setenv(email_trigger.MAX_EVENT_ATTEMPTS_ENV, "0")
    with pytest.raises(RuntimeError, match="MAX_EVENT_ATTEMPTS"):
        email_trigger.validate_trigger_env()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'mark_dispatched'`

- [ ] **Step 3: Write minimal implementation**

Append to `ui/backend/db/inbox_events.py`:

```python
def mark_dispatched(db: Session, run_id: str) -> None:
    """Charge one attempt against every event this run claimed.

    Charged here rather than at claim time on purpose. A workflow that fails to
    *build* (team deleted or edited into an invalid state) is not the message's
    fault, and today such mail is never consumed -- it retries until the
    customer fixes the team. Charging at claim would dead-letter a whole day of
    an org's mail because of a config mistake, so the release path that follows
    a build failure is penalty-free and only a real dispatch costs an attempt.
    """
    db.execute(
        update(InboxEvent)
        .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED)
        .values(attempts=InboxEvent.attempts + 1)
    )


def complete_events(
    db: Session, run_id: str, *, done_external_ids: set, error: str = None
) -> None:
    """Terminal outcome for a run that actually executed the model.

    `done_external_ids` is Phase 0's `already_drafted_uids` evidence: messages a
    draft demonstrably exists for. Those are `done` even on a failed run --
    reprocessing them would create a second draft, since `email_draft_reply`
    has no dedup of its own. Everything else is `failed` and waits for the
    existing human retry, which is the product behaviour today for a run whose
    model ran and failed.
    """
    now = _utcnow()
    done = {str(x) for x in done_external_ids}
    claimed = list(
        db.execute(
            select(InboxEvent).where(
                InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED
            )
        ).scalars()
    )
    for event in claimed:
        if event.external_id in done:
            event.status = EVENT_DONE
            event.last_error = None
        else:
            event.status = EVENT_FAILED
            event.last_error = error
        event.completed_at = now


def release_events(
    db: Session, run_id: str, *, max_attempts: int, error: str = None
) -> int:
    """Hand this run's claimed events back for reprocessing.

    For infrastructure-class failures only -- a killed process, a failed
    dispatch, a watchdog timeout -- where no model spend was incurred and the
    messages themselves are innocent. Rows that have used up `max_attempts` are
    dead-lettered instead of looping forever; the caller surfaces that on the
    trigger's health so a stuck message is not invisible.

    Returns the number dead-lettered.
    """
    now = _utcnow()
    claimed = list(
        db.execute(
            select(InboxEvent).where(
                InboxEvent.run_id == run_id, InboxEvent.status == EVENT_CLAIMED
            )
        ).scalars()
    )
    dead_lettered = 0
    for event in claimed:
        if event.attempts >= max_attempts:
            event.status = EVENT_FAILED
            event.last_error = error
            event.completed_at = now
            dead_lettered += 1
        else:
            event.status = EVENT_PENDING
            event.run_id = None
            event.claimed_at = None
            event.last_error = error
    return dead_lettered
```

In `ui/backend/email_trigger.py`, beside `RUN_TIMEOUT_ENV`:

```python
MAX_EVENT_ATTEMPTS_ENV = "BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS"
```

and beside `run_timeout_seconds`:

```python
def max_event_attempts() -> int:
    """How many times one detected message may be handed to a run before it is
    dead-lettered. Only infrastructure-class failures (crash, dispatch failure,
    watchdog timeout) consume an attempt -- a message that reaches the model
    and fails is terminal immediately and waits for a human retry."""
    return int(os.environ.get(MAX_EVENT_ATTEMPTS_ENV, "").strip() or 3)
```

and add to `validate_trigger_env`'s tuple:

```python
        (MAX_EVENT_ATTEMPTS_ENV, max_event_attempts, 1),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events.py tests/test_email_trigger.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/inbox_events.py ui/backend/email_trigger.py tests/test_inbox_events.py tests/test_email_trigger.py
git commit -m "feat(email): dispatch, complete and release inbox events"
```

---

### Task 4: Detection writes events and the cursor in one commit

**Files:**
- Modify: `ui/backend/email_trigger.py` — `poll_org` (~lines 588-604)
- Test: `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: `record_events`, `mailbox_identity` from Task 2.
- Produces: `_DETECT_MULTIPLIER = 10`; `poll_org` records events before dispatching, and `_start_triggered_run` no longer receives `new_uids`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_email_trigger.py`:

```python
def test_detection_records_events_and_advances_the_cursor_together(db_session, org, trigger):
    from ui.backend.db.models import InboxEvent

    # A poll that detects mail must leave a durable row per message in the same
    # commit that moves last_uid past it. Before this, the cursor advanced and
    # the work only existed inside a thread-pool submission.
    _poll_with_uids(db_session, trigger, uidvalidity=99, max_uid=13, new_uids=[11, 12, 13])

    rows = db_session.query(InboxEvent).order_by(InboxEvent.id).all()
    assert [r.external_id for r in rows] == ["11", "12", "13"]
    assert all(r.mailbox_generation == "99" for r in rows)
    assert db_session.get(EmailTrigger, trigger.id).last_uid == 13


def test_detection_is_idempotent_across_polls(db_session, org, trigger):
    from ui.backend.db.models import InboxEvent

    _poll_with_uids(db_session, trigger, uidvalidity=99, max_uid=11, new_uids=[11])
    # Same message seen again (e.g. the cursor write was lost): no duplicate.
    trigger.last_uid = 10
    db_session.commit()
    _poll_with_uids(db_session, trigger, uidvalidity=99, max_uid=11, new_uids=[11])
    assert db_session.query(InboxEvent).count() == 1


def test_detection_is_bounded_per_cycle(db_session, org, trigger, monkeypatch):
    from ui.backend.db.models import InboxEvent

    monkeypatch.setenv(email_trigger.BATCH_SIZE_ENV, "2")
    # A long outage can leave a large backlog; one cycle must not open an
    # unbounded transaction. Cursor advances only as far as it recorded.
    _poll_with_uids(db_session, trigger, uidvalidity=99, max_uid=100,
                    new_uids=list(range(1, 101)))
    rows = db_session.query(InboxEvent).all()
    assert len(rows) == 20  # batch_size (2) * _DETECT_MULTIPLIER (10)
    assert db_session.get(EmailTrigger, trigger.id).last_uid == 20
```

`_poll_with_uids` is a helper to add near the top of the file's test helpers:

```python
def _poll_with_uids(db, trigger, *, uidvalidity, max_uid, new_uids, get_workflow=None):
    """Drive one poll_org cycle with check_mailbox stubbed to a known result."""
    import unittest.mock as mock

    with mock.patch.object(
        email_trigger, "check_mailbox", return_value=(uidvalidity, max_uid, list(new_uids))
    ), mock.patch.object(email_trigger, "_ImapBackend"), mock.patch.object(
        email_trigger.secret_store, "decrypt", return_value="pw"
    ):
        email_trigger.poll_org(db, trigger, get_workflow or _never_called_workflow)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -k detection -v`
Expected: FAIL — no `InboxEvent` rows recorded

- [ ] **Step 3: Write minimal implementation**

In `email_trigger.py`, add near the other module constants:

```python
# How much of a backlog one detection cycle may record. Bounds the transaction
# after a long outage without changing steady-state behaviour: `new_uids` is
# sorted ascending, so slicing keeps the oldest and the rest are picked up next
# cycle.
_DETECT_MULTIPLIER = 10
```

Replace `poll_org`'s tail (from `if not new_uids:` through the `_start_triggered_run` call) with:

```python
        if not new_uids:
            db.commit()
            return

        # THE durability point. Recording the work and advancing the cursor in
        # ONE commit is what stops a process kill from consuming mail nothing
        # ran: before this, `_start_triggered_run` advanced `last_uid` and only
        # then handed the workflow to a thread pool, and a kill in between lost
        # the batch for good.
        detected = sorted(new_uids)[: batch_size() * _DETECT_MULTIPLIER]
        record_events(
            db,
            org_id=trigger.org_id,
            mailbox_identity=mailbox_identity(cred.host, cred.username),
            mailbox_generation=str(trigger.uidvalidity),
            external_ids=[str(u) for u in detected],
        )
        trigger.last_uid = max(detected)
        db.commit()

        if _at_daily_cap(db, trigger, today):
            db.commit()  # persist last_checked_at / error-clearing above; no dispatch
            return

        _start_triggered_run(db, trigger, get_workflow, backend, cred)
```

Add the import at the top of `email_trigger.py`:

```python
from .db.inbox_events import (
    claim_events, mailbox_identity, mark_dispatched, record_events,
    release_events,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -v`
Expected: pass (Task 5 completes the `_start_triggered_run` signature change; if the suite is red only on `_start_triggered_run`'s arity, proceed to Task 5 and re-run there)

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(email): record inbox events in the commit that advances the cursor"
```

---

### Task 5: Dispatch claims events, and failures release them

**Files:**
- Modify: `ui/backend/email_trigger.py` — `_start_triggered_run` (~lines 615-752)
- Test: `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: `claim_events`, `mark_dispatched`, `release_events`, `max_event_attempts`.
- Produces: `_start_triggered_run(db, trigger, get_workflow, backend, cred) -> None` (the `new_uids` parameter is gone — the batch now comes from the claim).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_email_trigger.py`:

```python
def test_dispatch_claims_events_and_charges_one_attempt(db_session, org, trigger):
    from ui.backend.db.models import InboxEvent

    _poll_with_uids(db_session, trigger, uidvalidity=99, max_uid=12, new_uids=[11, 12],
                    get_workflow=_stub_workflow)
    rows = db_session.query(InboxEvent).all()
    assert {r.status for r in rows} == {"claimed"}
    assert all(r.attempts == 1 for r in rows)
    assert all(r.run_id is not None for r in rows)


def test_a_workflow_build_failure_releases_the_events_without_penalty(db_session, org, trigger):
    from ui.backend.db.models import InboxEvent

    def _broken(*a, **k):
        raise RuntimeError("team deleted")

    _poll_with_uids(db_session, trigger, uidvalidity=99, max_uid=11, new_uids=[11],
                    get_workflow=_broken)
    row = db_session.query(InboxEvent).one()
    # Back to pending with no attempt charged: a broken team config must not
    # dead-letter the org's mail, and today such mail is never consumed.
    assert (row.status, row.attempts, row.run_id) == ("pending", 0, None)


def test_a_dispatch_failure_releases_the_events(db_session, org, trigger, monkeypatch):
    from ui.backend.db.models import InboxEvent

    monkeypatch.setattr(
        email_trigger._executor, "submit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pool down")),
    )
    _poll_with_uids(db_session, trigger, uidvalidity=99, max_uid=11, new_uids=[11],
                    get_workflow=_stub_workflow)
    row = db_session.query(InboxEvent).one()
    # An attempt WAS charged (the run really was dispatched), but the message
    # returns for reprocessing rather than being silently consumed.
    assert (row.status, row.attempts) == ("pending", 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -k "dispatch or build_failure" -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Change `_start_triggered_run`'s signature to drop `new_uids`, and at the top of the body replace the `batch = sorted(new_uids)[:batch_size()]` line with a claim. The run id must exist before the claim, so create the registry entry first:

```python
def _start_triggered_run(db: Session, trigger: EmailTrigger, get_workflow, backend, cred) -> None:
    run = registry.create(
        trigger.workflow_name, "", org_id=trigger.org_id, username=TRIGGER_USERNAME,
    )
    claimed = claim_events(db, org_id=trigger.org_id, run_id=run.id,
                           limit=batch_size())
    if not claimed:
        registry.discard(run.id)
        db.commit()
        return
    db.commit()  # the claim is durable before any workflow build is attempted
    batch = [int(e.external_id) for e in claimed]
    input_text = _trigger_input(batch)
    run.input = input_text
```

Keep the existing `trigger_context` construction verbatim (it already uses `batch`). Wrap the build failure branch so it releases:

```python
    except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since enabling
        _logger.warning(...)
        # Penalty-free release: the workflow is broken, not the message.
        release_events(db, run.id, max_attempts=max_event_attempts(), error=None)
        registry.discard(run.id)
        trigger.last_error = (...)
        trigger.last_error_kind = _ERROR_KIND_WORKFLOW
        db.commit()  # NB: runs_today deliberately NOT advanced
        return
```

In the `if not advanced:` branch (trigger disabled mid-build), release the same way before `db.commit()`.

After the successful CAS, in the same commit that persists the `Run` row:

```python
    db.add(run_row)
    mark_dispatched(db, run.id)
    db.commit()
```

In the dispatch-failure `except` block, after setting `run_row.status = "failed"`, release the events (an attempt is already charged, so this dead-letters at the limit) and surface a dead-letter on trigger health:

```python
        dead = release_events(db, run.id, max_attempts=max_event_attempts(),
                              error=message)
        if dead:
            trigger.last_error = _DEAD_LETTER_MESSAGE
            trigger.last_error_kind = _ERROR_KIND_WORKFLOW
```

with:

```python
_DEAD_LETTER_MESSAGE = (
    "Some new mail couldn't be processed after several attempts and has been "
    "set aside -- open the run list for details."
)
```

Note the dispatch-failure message must change: the batch is no longer permanently lost, so the existing "It won't be retried" copy is now false. Replace it with:

```python
        message = (
            "Couldn't start the automatic run. The affected mail will be "
            "picked up again on a later check."
        )
```

Finally update `poll_org`'s call site to the new signature (done in Task 4).

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(email): dispatch claims inbox events and failures release them"
```

---

### Task 6: The stale-run watchdog releases its events

**Files:**
- Modify: `ui/backend/email_trigger.py` — `_release_stale_run` (~lines 159-206)
- Test: `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: `release_events`, `max_event_attempts`.
- Produces: no signature change to `_release_stale_run(db, trigger, run_id) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
def test_the_watchdog_releases_a_hung_runs_events(db_session, org, trigger):
    from ui.backend.db.models import InboxEvent

    # Phase 0's watchdog frees the trigger from a hung run; its messages must
    # come back too, or they are consumed with nothing having processed them.
    run_id = _hung_run(db_session, org, trigger, age_seconds=4000, uids=["11", "12"])
    assert email_trigger._release_stale_run(db_session, trigger, run_id) is True
    rows = db_session.query(InboxEvent).all()
    assert {r.status for r in rows} == {"pending"}
    assert all(r.run_id is None for r in rows)


def test_the_watchdog_dead_letters_at_the_attempt_limit(db_session, org, trigger, monkeypatch):
    from ui.backend.db.models import InboxEvent

    monkeypatch.setenv(email_trigger.MAX_EVENT_ATTEMPTS_ENV, "1")
    run_id = _hung_run(db_session, org, trigger, age_seconds=4000, uids=["11"])
    email_trigger._release_stale_run(db_session, trigger, run_id)
    row = db_session.query(InboxEvent).one()
    assert row.status == "failed"
    # A dead-lettered message must not be invisible.
    assert db_session.get(EmailTrigger, trigger.id).last_error is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -k watchdog -v`
Expected: FAIL — events still `claimed`

- [ ] **Step 3: Write minimal implementation**

In `_release_stale_run`, after `registry.request_cancel(run_id)` and the row is marked failed, before the commit:

```python
    # Infrastructure-class: the run hung, the messages are innocent. Hand them
    # back so they are reprocessed rather than consumed by a wedged run.
    dead = release_events(db, run_id, max_attempts=max_event_attempts(),
                          error=_TRIGGER_RUN_TIMED_OUT)
    if dead:
        trigger.last_error = _DEAD_LETTER_MESSAGE
        trigger.last_error_kind = _ERROR_KIND_WORKFLOW
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(email): the stale-run watchdog releases its inbox events"
```

---

### Task 7: Completion from the runtime

**Files:**
- Modify: `ui/backend/runtime.py` — add `_safe_complete_inbox_events`, call from `_maybe_normalize` (~line 441)
- Test: `tests/test_runtime_run_row.py` (append)

**Interfaces:**
- Consumes: `complete_events` (Task 3), `automation_results.already_drafted_uids` (Phase 0).
- Produces: `_safe_complete_inbox_events(db, run_row) -> None`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_completed_run_marks_its_events_done(db_session, ...):
    from ui.backend.db.models import InboxEvent

    run_row = _triggered_run(db_session, uids=["11", "12"], status="completed")
    runtime._safe_complete_inbox_events(db_session, run_row)
    assert {e.status for e in db_session.query(InboxEvent)} == {"done"}


def test_a_failed_run_keeps_drafted_messages_done_and_fails_the_rest(db_session, ...):
    from ui.backend.db.models import InboxEvent

    # Phase 0's evidence layer is what decides: a draft demonstrably exists for
    # 11, so reprocessing it would duplicate the draft.
    run_row = _triggered_run(db_session, uids=["11", "12"], status="failed")
    _record_confirmed_draft(db_session, run_row, "11")
    runtime._safe_complete_inbox_events(db_session, run_row)
    rows = {e.external_id: e.status for e in db_session.query(InboxEvent)}
    assert rows == {"11": "done", "12": "failed"}


def test_a_completion_failure_never_breaks_the_run(db_session, monkeypatch, ...):
    monkeypatch.setattr(
        runtime, "complete_events",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    run_row = _triggered_run(db_session, uids=["11"], status="completed")
    runtime._safe_complete_inbox_events(db_session, run_row)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime_run_row.py -k inbox -v`
Expected: FAIL — `_safe_complete_inbox_events` undefined

- [ ] **Step 3: Write minimal implementation**

In `runtime.py`, after `_safe_record_trigger_health`:

```python
def _safe_complete_inbox_events(db: Session, run_row) -> None:
    """Give this run's claimed inbox events their terminal status.

    Everything that reaches here has actually executed the model, which is what
    makes "terminal here => workflow-class" sound: the two infrastructure-class
    paths (a failed dispatch, and the stale-run watchdog) never get this far and
    release their events at the site instead.

    A failed run still leaves real drafts behind for the messages it got
    through, so `already_drafted_uids` -- Phase 0's union of trace evidence, the
    X-BestTeam-Source-Key mailbox scan and automation_item_results -- decides
    which are done. The rest are terminal and wait for the human retry, which is
    today's product behaviour for a run whose model ran and failed.

    Isolated like `_safe_record_usage`: bookkeeping must never flip an otherwise
    successful run to failed.
    """
    trigger_context = getattr(run_row, "trigger_context", None) or {}
    if trigger_context.get("trigger_type") != "email" or run_row.org_id is None:
        return
    try:
        if run_row.status == "completed":
            done = {str(u) for u in (trigger_context.get("uids") or [])}
            error = None
        else:
            done = {str(u) for u in already_drafted_uids(db, run_row)}
            error = _TRIGGER_RUN_FAILED_MESSAGE
        complete_events(db, run_row.id, done_external_ids=done, error=error)
        db.commit()
    except Exception:  # noqa: BLE001 -- bookkeeping must never break a run
        _logger.warning(
            "Inbox event completion failed for run %s; run unaffected",
            run_row.id, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
```

Add the imports:

```python
from .automation_results import (
    RESULT_TYPE_BATCH_MARKER, already_drafted_uids, normalize_run_result,
)
from .db.inbox_events import complete_events
```

and call it in `_maybe_normalize` right after `_safe_record_trigger_health(db, run_row)`:

```python
            _safe_complete_inbox_events(db, run_row)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime_run_row.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add ui/backend/runtime.py tests/test_runtime_run_row.py
git commit -m "feat(email): mark inbox events terminal when a triggered run ends"
```

---

### Task 8: Manual retry reopens the original run's failed events

**Files:**
- Modify: `ui/backend/email_trigger.py` — `retry_triggered_run` (~lines 760-1010)
- Modify: `ui/backend/db/inbox_events.py` — add `reopen_events`
- Test: `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: everything above.
- Produces: `reopen_events(db, run_id) -> int` — `failed` rows for that run go back to `pending` with `run_id` cleared and `attempts` reset to 0; returns how many.

- [ ] **Step 1: Write the failing test**

```python
def test_retry_reopens_the_failed_events_of_the_original_run(db_session, org, trigger):
    from ui.backend.db.models import InboxEvent

    run_row = _failed_triggered_run(db_session, org, trigger, uids=["11", "12"],
                                    done=["11"])
    email_trigger.retry_triggered_run(db_session, run_row)
    rows = {e.external_id: e for e in db_session.query(InboxEvent)}
    assert rows["11"].status == "done"          # already drafted, never redone
    assert rows["12"].status == "claimed"       # reopened and claimed by the retry
    assert rows["12"].run_id != run_row.id


def test_a_run_predating_the_ledger_still_retries_via_trigger_context(db_session, org, trigger):
    # Runs in flight at upgrade time have no events at all; the Phase 0 path
    # must still work rather than raising "nothing left to retry".
    run_row = _failed_triggered_run(db_session, org, trigger, uids=["11"], done=[],
                                    with_events=False)
    new_run_id = email_trigger.retry_triggered_run(db_session, run_row)
    assert new_run_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -k retry_reopens -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add to `ui/backend/db/inbox_events.py`:

```python
def reopen_events(db: Session, run_id: str) -> int:
    """Return a failed run's terminal events to the queue for a human retry.

    Attempts reset to 0: the customer has looked at the failure and asked for
    it again, so the automatic dead-letter budget starts over.
    """
    result = db.execute(
        update(InboxEvent)
        .where(InboxEvent.run_id == run_id, InboxEvent.status == EVENT_FAILED)
        .values(status=EVENT_PENDING, run_id=None, claimed_at=None,
                completed_at=None, attempts=0, last_error=None)
    )
    return result.rowcount or 0
```

In `retry_triggered_run`, inside the `_dispatch_lock` block immediately before dispatch, reopen and then let the normal claim path run. Where the current code computes `retry_uids` and builds the new run from `trigger_context`, branch on whether the run has events:

```python
        reopened = reopen_events(db, run_row.id)
        db.commit()
        if reopened:
            # The ledger is authoritative: claim exactly what was reopened.
            _start_triggered_run(db, trigger, get_workflow, backend, cred)
            return _last_run_id_for(db, trigger)
        # Fall through to the pre-ledger path for runs that predate the
        # migration and have no events at all.
```

Keep the existing `trigger_context`-based path below, unchanged, as the fallback.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py ui/backend/db/inbox_events.py tests/test_email_trigger.py
git commit -m "feat(email): manual retry reopens the original run's failed events"
```

---

### Task 9: Documentation and full verification

**Files:**
- Modify: `ui/backend/db/CLAUDE.md`, `ui/backend/CLAUDE.md`, `docs/STATUS.md`, `.env.example`

- [ ] **Step 1: Document the table** in `ui/backend/db/CLAUDE.md` — `inbox_events`, its unique key and why the generation is in it, the `""`-not-`NULL` rule, and that `decision`/`filtered` are Phase 4 reservations.

- [ ] **Step 2: Document the lifecycle** in `ui/backend/CLAUDE.md` — detect/claim/dispatch/complete/release, the attempt-charging rule, and `BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS`.

- [ ] **Step 3: Update `docs/STATUS.md`** — move the "commit-then-crash consumes mail" item out of Known issues into Done; **keep** the multi-worker item and sharpen it to name the SQLite/Postgres blocker explicitly.

- [ ] **Step 4: Add the env var to `.env.example`.**

- [ ] **Step 5: Full serial verification**

Run: `./.venv/Scripts/python.exe -m pytest -m "not e2e"`
Expected: all pass, no `-n auto` (this is the `backend-full` equivalent that catches ordering and cross-test isolation bugs).

- [ ] **Step 6: Commit and open the PR**

```bash
git add -A
git commit -m "docs: record the durable inbox event ledger"
git push -u origin feat/email-phase-1-inbox-events
gh pr create --base feat/email-phase-0-hardening --title "..." --body "..."
```

---

## Self-Review

**Spec coverage:** table → T1; store → T2/T3; detection commit → T4; claim + attempt rule + build/dispatch release → T5; watchdog release → T6; completion via `already_drafted_uids` → T7; manual retry + pre-ledger fallback → T8; env var → T3; migration → T1; docs → T9. The spec's scope boundary (no `_dispatch_lock` removal, no cursor change) is carried in Global Constraints.

**Placeholder scan:** Task 7 and Task 8 tests use `...` in fixture argument lists and reference helpers (`_triggered_run`, `_failed_triggered_run`, `_record_confirmed_draft`, `_hung_run`, `_stub_workflow`, `_never_called_workflow`, `_last_run_id_for`) that must be written against the existing fixtures in those files. These are the one place the executor must read the surrounding test module before writing — flagged rather than invented, because guessing at fixture names that already exist in those files would produce worse code than reading them.

**Type consistency:** `record_events`/`claim_events`/`mark_dispatched`/`complete_events`/`release_events`/`reopen_events` all take `db` first and a `run_id: str` where relevant; `external_id` is `str` everywhere (the `int(e.external_id)` conversion happens only at the `trigger_context`/`allowed_uids` boundary, matching the existing `uids` list-of-int shape).
