# Autonomous Email-Triggered Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deployed email team runs automatically when new mail arrives in its org's connected mailbox — no human prompt.

**Architecture:** An in-process asyncio poller (started from `main.py`'s ASGI lifespan) checks each enabled org's IMAP mailbox on an interval, dedups by UID baseline stored in a new `email_triggers` table, and starts one run per cycle through the existing `run_in_background` path. Customer opt-in via new org-scoped API + wizard toggle; per-org daily run cap; activity list read from the already-persisted `runs` rows.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + Alembic + stdlib `imaplib` (via the existing `_ImapBackend`) + asyncio; React/Vite frontend. Spec: `docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md`.

## Global Constraints

- Python is ALWAYS `./.venv/Scripts/python.exe` (Windows venv); run backend tests as `./.venv/Scripts/python.exe -m pytest <path> -q` from repo root `C:/Projects/MyBestTeam`.
- Branch: work on `feature/email-trigger-autonomous-runs` (already created; the spec is committed there). NEVER commit `docs/STATUS.md` if it shows as modified with a PR-#20 entry — that edit belongs to another branch; only commit STATUS.md changes Task 10 itself makes ON TOP of that (see Task 10).
- TDD for all backend logic: failing test first, watch it fail, minimal code, watch it pass. Frontend (no JS test harness in this repo) is verified by `npm run lint` + `npm run build` in `ui/frontend`.
- Alembic migrations MUST be guarded/idempotent (`_has_table` inspection) because `db_session.py` runs `create_all` at import. New revision id: `f2a3b4c5d6e7`, down_revision `e1f2a3b4c5d6`.
- The email toolkit NEVER marks mail seen and NEVER sends — the poller must not change that (read-only `STATUS`/`SEARCH` only).
- Env vars introduced: `BESTTEAM_TRIGGER_POLL_SECONDS` (default `120`), `BESTTEAM_TRIGGER_DAILY_CAP` (default `50`), `BESTTEAM_TRIGGERS_DISABLED` (kill switch, default unset).
- Sentinel username for autonomous runs: exactly `email-trigger`.
- Customer-facing copy must never leak internals (env var names, `WinError` codes, tracebacks).
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility |
|---|---|
| `ui/backend/db/models.py` (modify) | `EmailTrigger` model (one row per org) |
| `alembic/versions/f2a3b4c5d6e7_add_email_triggers.py` (create) | guarded table migration |
| `ui/backend/db/email_triggers.py` (create) | tiny CRUD, mirrors `db/email_credentials.py` |
| `ui/backend/email_trigger.py` (create) | IMAP state helpers + per-org poll logic + async loop; NO FastAPI imports |
| `ui/backend/email_trigger_api.py` (create) | `/api/org/email-trigger` router (sibling of `org_settings.py`, avoids PR #21 merge conflicts) |
| `ui/backend/main.py` (modify) | include router; start/stop poller task in `_lifespan` |
| `ui/frontend/src/lib/api.js` (modify) | 3 new api methods |
| `ui/frontend/src/components/EmailTriggerToggle.jsx` (create) | opt-in toggle card (DeployPage) |
| `ui/frontend/src/components/EmailTriggerActivity.jsx` (create) | status chip + activity list (SessionsPage) |
| `tests/test_email_triggers_db.py`, `tests/test_email_trigger.py`, `tests/test_email_trigger_api.py` (create) | test files per layer |
| `.env.example`, `docs/deployment.md`, CLAUDE.md files, `docs/STATUS.md` (modify) | docs (Task 10) |

---

### Task 1: `EmailTrigger` model + guarded migration + CRUD

**Files:**
- Modify: `ui/backend/db/models.py` (append after `OrgEmailCredential`, before `BuilderSession`)
- Create: `alembic/versions/f2a3b4c5d6e7_add_email_triggers.py`
- Create: `ui/backend/db/email_triggers.py`
- Test: `tests/test_email_triggers_db.py`

**Interfaces:**
- Consumes: `Base`, `Organization`, `_utcnow` from `ui/backend/db/models.py`; `init_db`, `make_engine`, `session_factory` from `ui.backend.db`.
- Produces (later tasks rely on these exact names):
  - model `EmailTrigger` with columns `id, org_id (unique FK), workflow_name: str, enabled: bool, last_uid: int, uidvalidity: Optional[int], runs_today: int, runs_date: Optional[str], last_run_id: Optional[str], last_checked_at: Optional[datetime], last_error: Optional[str], created_at, updated_at`.
  - `get_email_trigger(db: Session, org_id: int) -> Optional[EmailTrigger]`
  - `upsert_email_trigger(db, org_id, *, workflow_name: str, enabled: bool, last_uid: int, uidvalidity: Optional[int]) -> EmailTrigger`
  - `list_enabled_triggers(db: Session) -> list[EmailTrigger]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_triggers_db.py`:

```python
"""CRUD tests for the `email_triggers` table (autonomous email trigger state)."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_triggers import (
    get_email_trigger,
    list_enabled_triggers,
    upsert_email_trigger,
)
from ui.backend.db.orgs import get_or_create_org


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    yield session
    session.close()


def test_get_returns_none_when_absent(db):
    org = get_or_create_org(db, "acme")
    assert get_email_trigger(db, org.id) is None


def test_upsert_creates_then_updates_in_place(db):
    org = get_or_create_org(db, "acme")
    t = upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=41, uidvalidity=7)
    assert (t.workflow_name, t.enabled, t.last_uid, t.uidvalidity) == ("triage", True, 41, 7)
    assert t.runs_today == 0 and t.runs_date is None and t.last_run_id is None
    t2 = upsert_email_trigger(db, org.id, workflow_name="other", enabled=False,
                              last_uid=99, uidvalidity=8)
    assert t2.id == t.id  # one row per org, updated in place
    assert (t2.workflow_name, t2.enabled, t2.last_uid) == ("other", False, 99)


def test_list_enabled_returns_only_enabled(db):
    a = get_or_create_org(db, "a")
    b = get_or_create_org(db, "b")
    upsert_email_trigger(db, a.id, workflow_name="wa", enabled=True, last_uid=0, uidvalidity=None)
    upsert_email_trigger(db, b.id, workflow_name="wb", enabled=False, last_uid=0, uidvalidity=None)
    enabled = list_enabled_triggers(db)
    assert [t.org_id for t in enabled] == [a.id]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_triggers_db.py -q`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'ui.backend.db.email_triggers'`.

- [ ] **Step 3: Implement model, migration, CRUD**

Append to `ui/backend/db/models.py` directly after the `OrgEmailCredential` class:

```python
class EmailTrigger(Base):
    """One org's autonomous new-mail trigger (opt-in) plus poller state.

    At most one auto-running team per org (unique `org_id`), mirroring
    one-mailbox-per-org. `last_uid`/`uidvalidity` are the dedup baseline --
    UIDs, never UNSEEN, because the draft-only toolkit deliberately never
    marks mail seen. `runs_today`/`runs_date` implement the daily run cap.
    See docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md.
    """

    __tablename__ = "email_triggers"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, nullable=False
    )
    workflow_name: Mapped[str]
    enabled: Mapped[bool] = mapped_column(default=False)
    # Dedup baseline: only UIDs above this trigger a run. Set to the mailbox's
    # current max UID at enable time so the existing backlog never triggers.
    last_uid: Mapped[int] = mapped_column(default=0)
    uidvalidity: Mapped[Optional[int]] = mapped_column(nullable=True)
    # Daily cap state; runs_date is an ISO date string (UTC).
    runs_today: Mapped[int] = mapped_column(default=0)
    runs_date: Mapped[Optional[str]] = mapped_column(nullable=True)
    # Overlap guard: skip a cycle while this run is still `running`.
    last_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
```

Create `alembic/versions/f2a3b4c5d6e7_add_email_triggers.py`:

```python
"""add email_triggers (autonomous new-mail trigger state)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-19 12:00:00.000000

One row per org: the customer's opt-in for autonomous email-triggered runs,
plus poller state (UID dedup baseline, daily-cap counters, health).

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the table when
this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "email_triggers"):
        op.create_table(
            "email_triggers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("workflow_name", sa.String(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_uid", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("uidvalidity", sa.Integer(), nullable=True),
            sa.Column("runs_today", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("runs_date", sa.String(), nullable=True),
            sa.Column("last_run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("org_id", name="uq_email_triggers_org_id"),
        )


def downgrade() -> None:
    """Downgrade schema (drops all trigger opt-ins and poller state)."""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "email_triggers"):
        op.drop_table("email_triggers")
```

Create `ui/backend/db/email_triggers.py`:

```python
"""CRUD for `EmailTrigger` -- one org's autonomous new-mail trigger state.

Mirrors `db/email_credentials.py`: small helpers over the one-row-per-org
table. Poll-state mutations (cap counters, baselines, errors) are done by
`ui/backend/email_trigger.py` directly on the row inside its own session.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .models import EmailTrigger


def get_email_trigger(db: Session, org_id: int) -> Optional[EmailTrigger]:
    return db.query(EmailTrigger).filter_by(org_id=org_id).one_or_none()


def upsert_email_trigger(
    db: Session,
    org_id: int,
    *,
    workflow_name: str,
    enabled: bool,
    last_uid: int,
    uidvalidity: Optional[int],
) -> EmailTrigger:
    """Create or replace an org's trigger config (upsert on `org_id`).

    Resets neither the daily-cap counters nor `last_run_id` -- re-enabling on
    the same day keeps counting against the same cap.
    """
    row = get_email_trigger(db, org_id)
    if row is None:
        row = EmailTrigger(org_id=org_id)
        db.add(row)
    row.workflow_name = workflow_name
    row.enabled = enabled
    row.last_uid = last_uid
    row.uidvalidity = uidvalidity
    db.commit()
    db.refresh(row)
    return row


def list_enabled_triggers(db: Session) -> List[EmailTrigger]:
    return db.query(EmailTrigger).filter_by(enabled=True).order_by(EmailTrigger.org_id).all()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_triggers_db.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/models.py ui/backend/db/email_triggers.py alembic/versions/f2a3b4c5d6e7_add_email_triggers.py tests/test_email_triggers_db.py
git commit -m "feat(trigger): EmailTrigger table + guarded migration + CRUD

One row per org: opt-in flag, UID dedup baseline, daily-cap state, health.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: IMAP mailbox-state helpers (`mailbox_state`, `check_mailbox`)

**Files:**
- Create: `ui/backend/email_trigger.py` (helpers only in this task)
- Test: `tests/test_email_trigger.py`

**Interfaces:**
- Consumes: `_ImapBackend` (only its `_connect()` method, duck-typed in tests).
- Produces:
  - `mailbox_state(backend) -> tuple[int, int]` — `(uidvalidity, max_uid)`; STATUS only, no search. Used by the PUT endpoint to set the enable-time baseline cheaply.
  - `check_mailbox(backend, last_uid: int) -> tuple[int, int, list[int]]` — `(uidvalidity, max_uid, new_uids_sorted)`; read-only SELECT + UID SEARCH. Raises `OSError` on malformed responses.
  - Module constant `TRIGGER_USERNAME = "email-trigger"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_trigger.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: ERROR with `ModuleNotFoundError: No module named 'ui.backend.email_trigger'`.

- [ ] **Step 3: Implement the helpers**

Create `ui/backend/email_trigger.py`:

```python
"""Autonomous email trigger: poll each opted-in org's mailbox for new mail and
start that org's deployed email team -- no human prompt.

Design: docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md.
- Dedup is a per-org UID baseline (`EmailTrigger.last_uid`/`uidvalidity`), never
  UNSEEN: the draft-only toolkit deliberately never marks mail seen.
- One run per poll cycle covering all new messages found in it.
- The poll loop lives in the backend process (started from main.py's lifespan)
  and must never die: every org is wrapped in try/except and failures are
  stored on the row (`last_error`) for the UI, then retried next cycle.

This module has NO FastAPI imports; the /api/org/email-trigger router lives in
`email_trigger_api.py`.
"""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

_logger = logging.getLogger(__name__)

TRIGGER_USERNAME = "email-trigger"

_STATUS_RE = re.compile(rb"UIDVALIDITY (\d+) UIDNEXT (\d+)")


def _parse_status(data) -> Tuple[int, int]:
    """Parse `(uidvalidity, max_uid)` out of a STATUS response line."""
    line = data[0] if data else b""
    match = _STATUS_RE.search(line or b"")
    if match is None:
        raise OSError(f"unexpected INBOX STATUS response: {line!r}")
    uidvalidity, uidnext = int(match.group(1)), int(match.group(2))
    return uidvalidity, uidnext - 1  # UIDNEXT is the *next* UID to be assigned


def mailbox_state(backend) -> Tuple[int, int]:
    """`(uidvalidity, current_max_uid)` via STATUS only -- cheap enable-time
    baseline (no SELECT, no SEARCH, nothing marked seen)."""
    conn = backend._connect()
    try:
        typ, data = conn.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        if typ != "OK":
            raise OSError(f"INBOX STATUS failed: {typ}")
        return _parse_status(data)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass


def check_mailbox(backend, last_uid: int) -> Tuple[int, int, List[int]]:
    """`(uidvalidity, max_uid, new_uids)` where new_uids are strictly above
    `last_uid`, sorted ascending. Read-only throughout."""
    conn = backend._connect()
    try:
        typ, data = conn.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        if typ != "OK":
            raise OSError(f"INBOX STATUS failed: {typ}")
        uidvalidity, max_uid = _parse_status(data)
        if max_uid <= last_uid:
            return uidvalidity, max_uid, []
        conn.select("INBOX", readonly=True)
        typ, search_data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            raise OSError(f"UID SEARCH failed: {typ}")
        raw = search_data[0].split() if search_data and search_data[0] else []
        # IMAP quirk: "N:*" returns the highest-UID message even when N > max,
        # so results at or below the baseline must be filtered out here.
        new_uids = sorted(int(u) for u in raw if int(u) > last_uid)
        return uidvalidity, max_uid, new_uids
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(trigger): IMAP mailbox-state helpers (UID baseline, read-only)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `poll_org` — cap, baseline, bookkeeping, error capture

**Files:**
- Modify: `ui/backend/email_trigger.py`
- Test: `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: Task 1's `EmailTrigger` row (mutated in place), Task 2's `check_mailbox`; `get_email_credentials(db, org_id)` from `ui.backend.db.email_credentials`; `secret_store.decrypt`; `_ImapBackend`.
- Produces:
  - `poll_seconds() -> float` (env `BESTTEAM_TRIGGER_POLL_SECONDS`, default 120)
  - `daily_cap() -> int` (env `BESTTEAM_TRIGGER_DAILY_CAP`, default 50)
  - `triggers_disabled() -> bool` (env `BESTTEAM_TRIGGERS_DISABLED`)
  - `poll_org(db: Session, trigger: EmailTrigger, get_workflow) -> None` — never raises; commits its own state changes. `get_workflow` has the signature of `main._get_workflow(name, db, org_id) -> Workflow`. (This task implements everything EXCEPT the new-mail run start, which returns early with a `NotImplementedError` guard replaced in Task 4 — see Step 3.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_trigger.py`:

```python
# --- poll_org: cap / baseline / bookkeeping / errors -------------------------

from datetime import datetime, timezone

from cryptography.fernet import Fernet

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import set_email_credentials
from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
from ui.backend.db.orgs import get_or_create_org
from ui.backend.email_trigger import daily_cap, poll_org


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    yield session
    session.close()


def _org_with_trigger(db, *, last_uid=45, uidvalidity=3, enabled=True):
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="imap.acme.com", username="u@acme.com",
                          password="pw")
    trigger = upsert_email_trigger(db, org.id, workflow_name="triage",
                                   enabled=enabled, last_uid=last_uid,
                                   uidvalidity=uidvalidity)
    return org, trigger


def _no_workflow(name, db, org_id):  # get_workflow stub that must NOT be called
    raise AssertionError("get_workflow should not be called in this test")


def test_poll_org_no_new_mail_updates_health_only(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_checked_at is not None
    assert trigger.last_error is None
    assert trigger.last_uid == 45 and trigger.runs_today == 0


def test_poll_org_uidvalidity_change_rebaselines_without_running(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=45, uidvalidity=3)
    # New validity 9, and the "new" mailbox reports max_uid 200 with new uids --
    # they must be skipped and the baseline reset instead.
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (9, 200, [199, 200]))
    poll_org(db, trigger, _no_workflow)
    assert trigger.uidvalidity == 9
    assert trigger.last_uid == 200
    assert trigger.runs_today == 0


def test_poll_org_cap_reached_skips_mailbox_entirely(db, monkeypatch):
    org, trigger = _org_with_trigger(db)
    trigger.runs_today = daily_cap()
    trigger.runs_date = datetime.now(timezone.utc).date().isoformat()
    db.commit()

    def _boom(backend, last_uid):
        raise AssertionError("must not touch the mailbox when capped")

    monkeypatch.setattr(email_trigger, "check_mailbox", _boom)
    poll_org(db, trigger, _no_workflow)  # must not raise


def test_poll_org_cap_resets_on_new_day(db, monkeypatch):
    org, trigger = _org_with_trigger(db)
    trigger.runs_today = daily_cap()
    trigger.runs_date = "2020-01-01"  # stale date -> counter must reset
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.runs_today == 0
    assert trigger.runs_date == datetime.now(timezone.utc).date().isoformat()


def test_poll_org_mailbox_failure_stores_friendly_error(db, monkeypatch):
    org, trigger = _org_with_trigger(db)

    def _fail(backend, last_uid):
        raise OSError("[WinError 10060] connection attempt failed")

    monkeypatch.setattr(email_trigger, "check_mailbox", _fail)
    poll_org(db, trigger, _no_workflow)  # must not raise
    assert trigger.last_error is not None
    assert "WinError" not in trigger.last_error  # no internals to customers
    assert trigger.last_checked_at is not None


def test_poll_org_error_clears_on_next_success(db, monkeypatch):
    org, trigger = _org_with_trigger(db)
    trigger.last_error = "Couldn't check the mailbox."
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: the 5 Task-2 tests pass; the new ones FAIL with `ImportError: cannot import name 'poll_org'` (or `daily_cap`).

- [ ] **Step 3: Implement env helpers + `poll_org` (without the run start)**

Append to `ui/backend/email_trigger.py` (extend the imports at the top as shown):

```python
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from bestteam.exceptions import ConfigurationError
from bestteam.tools.email_client import _ImapBackend

from . import secret_store
from .db.email_credentials import get_email_credentials
from .db.models import EmailTrigger, Run

POLL_SECONDS_ENV = "BESTTEAM_TRIGGER_POLL_SECONDS"
DAILY_CAP_ENV = "BESTTEAM_TRIGGER_DAILY_CAP"
DISABLED_ENV = "BESTTEAM_TRIGGERS_DISABLED"


def poll_seconds() -> float:
    return float(os.environ.get(POLL_SECONDS_ENV, "").strip() or 120)


def daily_cap() -> int:
    return int(os.environ.get(DAILY_CAP_ENV, "").strip() or 50)


def triggers_disabled() -> bool:
    return os.environ.get(DISABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _utcnow().date().isoformat()


def _friendly_poll_error(exc: Exception) -> str:
    """Customer-visible health message: actionable, never internals.

    `ConfigurationError` messages are already written for humans (e.g. login
    rejected, no mailbox connected); anything else is summarized generically
    -- the real exception goes to the server log.
    """
    if isinstance(exc, ConfigurationError):
        return str(exc)[:500]
    return "Couldn't check the mailbox. We'll keep retrying automatically."


def poll_org(db: Session, trigger: EmailTrigger, get_workflow: Callable) -> None:
    """One org's poll cycle. Never raises; all state changes committed here.

    `get_workflow` is `main._get_workflow` injected by the loop (avoids a
    circular import and lets tests pass a stub): `(name, db, org_id) -> Workflow`.
    """
    # Daily cap: reset on date rollover, and skip the mailbox entirely at cap.
    today = _today()
    if trigger.runs_date != today:
        trigger.runs_today = 0
        trigger.runs_date = today
    if trigger.runs_today >= daily_cap():
        db.commit()
        return

    # Overlap guard: previous triggered run still executing -> skip; new UIDs
    # simply accumulate for the next cycle.
    if trigger.last_run_id:
        prev = db.get(Run, trigger.last_run_id)
        if prev is not None and prev.status == "running":
            db.commit()
            return

    try:
        cred = get_email_credentials(db, trigger.org_id)
        if cred is None:
            raise ConfigurationError(
                "No mailbox is connected -- reconnect it to resume automatic runs."
            )
        password = secret_store.decrypt(cred.password_encrypted)
        backend = _ImapBackend(
            host=cred.host,
            user=cred.username,
            password=password,
            port=cred.port,
            drafts=cred.drafts_folder,
            restrict_to_public=True,  # customer-supplied host
        )
        uidvalidity, max_uid, new_uids = check_mailbox(backend, trigger.last_uid)
    except (InvalidToken, secret_store.SecretsKeyError) as exc:
        _logger.warning("email trigger: cannot decrypt credentials for org %s: %s",
                        trigger.org_id, exc)
        trigger.last_error = (
            "The mailbox connection can't be read right now -- reconnect it to "
            "resume automatic runs."
        )
        trigger.last_checked_at = _utcnow()
        db.commit()
        return
    except Exception as exc:  # noqa: BLE001 -- a poll failure must never kill the loop
        _logger.warning("email trigger: poll failed for org %s: %s", trigger.org_id, exc)
        trigger.last_error = _friendly_poll_error(exc)
        trigger.last_checked_at = _utcnow()
        db.commit()
        return

    trigger.last_checked_at = _utcnow()
    trigger.last_error = None

    # Mailbox rebuilt/migrated: UIDs are not comparable across validities --
    # re-baseline to now, never reprocess.
    if trigger.uidvalidity is None or trigger.uidvalidity != uidvalidity:
        trigger.uidvalidity = uidvalidity
        trigger.last_uid = max_uid
        db.commit()
        return

    if not new_uids:
        db.commit()
        return

    _start_triggered_run(db, trigger, new_uids, get_workflow)


def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow) -> None:
    raise NotImplementedError  # implemented in the next commit
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: `11 passed` (Task-2's 5 + these 6 — none of these reach `_start_triggered_run`).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(trigger): poll_org cap/baseline/health logic (no run start yet)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `poll_org` new-mail path — start the run

**Files:**
- Modify: `ui/backend/email_trigger.py` (replace `_start_triggered_run` stub)
- Test: `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: `registry`, `_executor`, `run_in_background` from `ui.backend.runtime` (exact call shape mirrors `main.create_run`: `registry.create(workflow_name, input, org_id=..., username=...)` then `_executor.submit(run_in_background, run.id, workflow, input, engine=..., org_id=..., username=...)`).
- Produces: completed `poll_org` behavior later tasks rely on: advances `last_uid` and increments `runs_today` BEFORE the run starts; sets `trigger.last_run_id`; run input format `N new email(s) arrived in the inbox (message ids: 42, 43). Read each message by id and triage it, drafting replies where appropriate.`; sentinel username `email-trigger`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_trigger.py`:

```python
# --- poll_org: the new-mail path ---------------------------------------------


class _SubmitRecorder:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


def _fake_workflow_getter(calls):
    def get_workflow(name, db, org_id):
        calls.append((name, org_id))
        return object()  # poll_org only hands it to the executor
    return get_workflow


def test_poll_org_new_mail_starts_one_run(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 43, 45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    calls = []
    poll_org(db, trigger, _fake_workflow_getter(calls))

    assert calls == [("triage", org.id)]
    assert len(recorder.calls) == 1  # ONE run for the whole batch
    fn, args, kwargs = recorder.calls[0]
    run_id, workflow, input_text = args[0], args[1], args[2]
    assert "3 new email(s)" in input_text
    assert "42, 43, 45" in input_text
    assert kwargs["username"] == "email-trigger"
    assert kwargs["org_id"] == org.id
    # State advanced BEFORE the run: a crashed run must not re-trigger.
    assert trigger.last_uid == 45
    assert trigger.runs_today == 1
    assert trigger.last_run_id == run_id


def test_poll_org_skips_while_previous_run_still_running(db, monkeypatch):
    from ui.backend.db.models import Run as RunRow

    org, trigger = _org_with_trigger(db, last_uid=41)
    db.add(RunRow(id="r-prev", workflow="triage", input="x", status="running",
                  org_id=org.id, username="email-trigger"))
    trigger.last_run_id = "r-prev"
    db.commit()

    def _boom(backend, last_uid):
        raise AssertionError("must not touch the mailbox while a run is active")

    monkeypatch.setattr(email_trigger, "check_mailbox", _boom)
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_uid == 41  # untouched


def test_poll_org_workflow_load_failure_recorded_not_raised(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [45]))
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    def _missing(name, db_, org_id):
        raise Exception("Unknown workflow 'triage'")

    poll_org(db, trigger, _missing)
    assert recorder.calls == []
    assert trigger.last_error is not None
    assert "triage" in trigger.last_error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: the 3 new tests FAIL — the first and third with `NotImplementedError`, the second passes only if the overlap guard already works (it does; it will pass — the two real failures are the point).

- [ ] **Step 3: Implement `_start_triggered_run`**

In `ui/backend/email_trigger.py`, add to the imports:

```python
from .runtime import _executor, registry, run_in_background
```

Replace the `_start_triggered_run` stub with:

```python
def _trigger_input(new_uids) -> str:
    ids = ", ".join(str(u) for u in new_uids)
    return (
        f"{len(new_uids)} new email(s) arrived in the inbox (message ids: {ids}). "
        "Read each message by id and triage it, drafting replies where appropriate."
    )


def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow) -> None:
    """Start ONE run covering all of this cycle's new messages.

    `last_uid`/`runs_today` are advanced and committed BEFORE the run starts:
    a crashed run must show as a failure in the activity list, not re-trigger
    forever. No per-user memory (`user_id=None`) -- there is no human user.
    """
    trigger.last_uid = max(new_uids)
    trigger.runs_today += 1
    input_text = _trigger_input(new_uids)
    try:
        workflow = get_workflow(trigger.workflow_name, db, trigger.org_id)
    except Exception as exc:  # noqa: BLE001 -- e.g. team deleted since enabling
        _logger.warning("email trigger: cannot load workflow %r for org %s: %s",
                        trigger.workflow_name, trigger.org_id, exc)
        trigger.last_error = (
            f"Couldn't start the team '{trigger.workflow_name}' -- it may have "
            "been changed or removed. Re-enable automatic runs from its page."
        )
        db.commit()
        return
    run = registry.create(
        trigger.workflow_name, input_text, org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )
    trigger.last_run_id = run.id
    db.commit()
    _executor.submit(
        run_in_background,
        run.id,
        workflow,
        input_text,
        engine=db.get_bind(),
        org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: `14 passed`.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(trigger): start one run per cycle for new mail (advance-before-run)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `poll_once` + `poll_forever` + lifespan wiring + kill switch

**Files:**
- Modify: `ui/backend/email_trigger.py`
- Modify: `ui/backend/main.py` (the `_lifespan` function; add `import asyncio`, `import contextlib`, and `from . import email_trigger` to its imports)
- Test: `tests/test_email_trigger.py` (append)

**Interfaces:**
- Consumes: `SessionLocal` from `ui.backend.db_session`; `list_enabled_triggers` from Task 1; `main._get_workflow` (injected at task-start, never imported by `email_trigger`).
- Produces:
  - `poll_once(get_workflow, session_factory=None) -> None` — one full pass over all enabled orgs; per-org exceptions logged, never raised.
  - `async poll_forever(stop_event: asyncio.Event, get_workflow) -> None` — sleep-FIRST loop (so app startup/tests never poll immediately); checks `triggers_disabled()` each cycle.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_trigger.py`:

```python
# --- poll_once / poll_forever -------------------------------------------------

import asyncio

from ui.backend.email_trigger import poll_forever, poll_once


def test_poll_once_covers_enabled_orgs_and_survives_failures(db, monkeypatch):
    a = get_or_create_org(db, "org_a")
    b = get_or_create_org(db, "org_b")
    for org in (a, b):
        set_email_credentials(db, org.id, host="h", username="u", password="p")
        upsert_email_trigger(db, org.id, workflow_name="w", enabled=True,
                             last_uid=0, uidvalidity=1)
    polled = []

    def fake_poll_org(session, trigger, get_workflow):
        polled.append(trigger.org_id)
        if trigger.org_id == a.id:
            raise RuntimeError("org A explodes")  # must not stop org B

    monkeypatch.setattr(email_trigger, "poll_org", fake_poll_org)

    class _Factory:  # context-manager session factory over the test db
        def __call__(self):
            return self

        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    poll_once(_no_workflow, session_factory=_Factory())
    assert polled == [a.id, b.id]


def test_poll_forever_sleeps_first_and_respects_kill_switch(monkeypatch):
    calls = []
    monkeypatch.setattr(email_trigger, "poll_once", lambda gw, session_factory=None: calls.append(1))
    monkeypatch.setattr(email_trigger, "poll_seconds", lambda: 0.01)

    async def run_briefly(disabled):
        monkeypatch.setenv("BESTTEAM_TRIGGERS_DISABLED", "1" if disabled else "")
        stop = asyncio.Event()
        task = asyncio.ensure_future(poll_forever(stop, _no_workflow))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly(disabled=True))
    assert calls == []  # kill switch: loop alive, no polling

    asyncio.run(run_briefly(disabled=False))
    assert len(calls) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -q`
Expected: 2 new FAILs with `ImportError: cannot import name 'poll_forever'`.

- [ ] **Step 3: Implement loop + wire into main**

Append to `ui/backend/email_trigger.py` (add `import asyncio` to its imports):

```python
def poll_once(get_workflow: Callable, session_factory=None) -> None:
    """One pass over every enabled org. Runs on a worker thread (imaplib and
    SQLAlchemy here are synchronous); a failure in one org never stops the rest."""
    from .db_session import SessionLocal  # late import: keep module import-light

    from .db.email_triggers import list_enabled_triggers

    factory = session_factory or SessionLocal
    with factory() as db:
        for trigger in list_enabled_triggers(db):
            try:
                poll_org(db, trigger, get_workflow)
            except Exception:  # noqa: BLE001 -- the loop must outlive any org's failure
                _logger.exception("email trigger: unexpected failure for org %s",
                                  trigger.org_id)


async def poll_forever(stop_event: "asyncio.Event", get_workflow: Callable) -> None:
    """The poller task: sleep FIRST, then poll, forever until `stop_event`.

    Sleeping first means app startup (and short-lived TestClient lifespans)
    never trigger an immediate poll against the live database."""
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds())
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        if triggers_disabled():
            continue
        try:
            await asyncio.to_thread(poll_once, get_workflow)
        except Exception:  # noqa: BLE001 -- never let the task die
            _logger.exception("email trigger: poll cycle failed")
```

In `ui/backend/main.py`: add `import asyncio` and `import contextlib` beside the existing stdlib imports, add `from . import email_trigger` beside the other relative imports, and replace the existing `_lifespan` body:

```python
@asynccontextmanager
async def _lifespan(_app):
    """ASGI startup: refuse to serve while the membership invariant is violated,
    then run the autonomous email-trigger poller for the app's lifetime.

    The membership guard is data-dependent (unlike the config guards below), so
    it runs when the server actually starts serving, not at import."""
    with SessionLocal() as session:
        try:
            _enforce_one_member_per_org_or_raise(session)
        except OperationalError:
            pass  # pre-migration schema (no users table yet); nothing to enforce
    stop_polling = asyncio.Event()
    poller = asyncio.create_task(
        email_trigger.poll_forever(stop_polling, _get_workflow)
    )
    try:
        yield
    finally:
        stop_polling.set()
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
```

Note: `_get_workflow` is defined later in `main.py` than `_lifespan`, which is fine — the name is resolved when the task first calls it, not at definition time.

- [ ] **Step 4: Run tests to verify they pass, plus the existing lifespan tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py tests/test_auth.py -q`
Expected: all pass (`test_auth.py` contains the existing lifespan startup-guard tests — they use `with TestClient(...)`, which now also starts/stops the poller; sleep-first keeps them from polling).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py ui/backend/main.py tests/test_email_trigger.py
git commit -m "feat(trigger): poll loop wired into app lifespan, with kill switch

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: API — `GET`/`PUT /api/org/email-trigger`

**Files:**
- Create: `ui/backend/email_trigger_api.py`
- Modify: `ui/backend/main.py` (include router: add `from .email_trigger_api import router as email_trigger_router` beside the other router imports and `app.include_router(email_trigger_router)` beside the other `include_router` calls)
- Test: `tests/test_email_trigger_api.py`

**Interfaces:**
- Consumes: `get_current_org` from `ui.backend.auth_api`; `get_db` from `ui.backend.db_session`; Task 1 CRUD; Task 2's `mailbox_state`; `spec_uses_email` from `ui.backend.email_tools`; `get_email_credentials`; `secret_store`; `WorkflowRecord`; `daily_cap`, `triggers_disabled` from `ui.backend.email_trigger`.
- Produces:
  - `GET /api/org/email-trigger` → `{enabled, workflow_name, status, runs_today, daily_cap, last_checked_at, last_error}` where `status ∈ {"off","active","paused_cap","error","disabled"}`.
  - `PUT /api/org/email-trigger` body `{workflow_name: str, enabled: bool}` → same payload. Enable validates: deployed workflow in this org + uses email + mailbox connected + mailbox reachable (baseline via `mailbox_state`). All rejection messages customer-readable.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_trigger_api.py`:

```python
"""API tests for /api/org/email-trigger (opt-in + status + activity)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import email_trigger_api
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import set_email_credentials
from ui.backend.db.email_triggers import get_email_trigger
from ui.backend.db.models import WorkflowRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db
from ui.backend.skills import seed_default_skills

_EMAIL_TEAM_CONFIG = {
    "name": "triage",
    "agents": [{"name": "t", "role": "Triager", "goal": "triage",
                "model": "fake:done", "skills": ["email_triage_reply"]}],
    "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}
_PLAIN_TEAM_CONFIG = {
    "name": "plain",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("BESTTEAM_TRIGGERS_DISABLED", raising=False)
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)

    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    try:
        c = TestClient(backend_main.app)
        token = create_user_and_login(c)  # plain member of 'default'
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _seed_team(config, status="deployed", org_name="default"):
    with open_test_db() as db:
        seed_default_skills(db)
        org = get_or_create_org(db, org_name)
        db.add(WorkflowRecord(name=config["name"], org_id=org.id,
                              config=config, status=status))
        db.commit()
        return org.id


def _connect_mailbox(org_id):
    with open_test_db() as db:
        set_email_credentials(db, org_id, host="imap.acme.com",
                              username="u@acme.com", password="pw")


def _stub_mailbox(monkeypatch, uidvalidity=3, max_uid=45):
    monkeypatch.setattr(email_trigger_api, "mailbox_state",
                        lambda backend: (uidvalidity, max_uid))


def test_get_status_off_by_default(client):
    body = client.get("/api/org/email-trigger").json()
    assert body["enabled"] is False
    assert body["status"] == "off"
    assert body["daily_cap"] > 0


def test_enable_happy_path_sets_baseline(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch, uidvalidity=7, max_uid=99)
    body = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True}).json()
    assert body["enabled"] is True and body["status"] == "active"
    with open_test_db() as db:
        t = get_email_trigger(db, org_id)
        assert (t.last_uid, t.uidvalidity) == (99, 7)  # backlog never triggers


def test_enable_rejects_undeployed_team(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG, status="draft")
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "launch" in resp.json()["detail"].lower()


def test_enable_rejects_non_email_team(client, monkeypatch):
    org_id = _seed_team(_PLAIN_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "plain", "enabled": True})
    assert resp.status_code == 400
    assert "email" in resp.json()["detail"].lower()


def test_enable_rejects_when_no_mailbox(client, monkeypatch):
    _seed_team(_EMAIL_TEAM_CONFIG)
    _stub_mailbox(monkeypatch)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "mailbox" in resp.json()["detail"].lower()


def test_enable_unreachable_mailbox_is_friendly_400(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)

    def _fail(backend):
        raise OSError("[WinError 10060] timed out")

    monkeypatch.setattr(email_trigger_api, "mailbox_state", _fail)
    resp = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400
    assert "WinError" not in resp.json()["detail"]


def test_disable_turns_off(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG)
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    client.put("/api/org/email-trigger", json={"workflow_name": "triage", "enabled": True})
    body = client.put("/api/org/email-trigger",
                      json={"workflow_name": "triage", "enabled": False}).json()
    assert body["enabled"] is False and body["status"] == "off"


def test_platform_operator_gets_403(client):
    op = create_user_and_login(client, username="op", org=None, admin=True)
    resp = client.get("/api/org/email-trigger",
                      headers={"Authorization": f"Bearer {op}"})
    assert resp.status_code == 403


def test_cross_org_cannot_enable_other_orgs_team(client, monkeypatch):
    org_id = _seed_team(_EMAIL_TEAM_CONFIG, org_name="default")
    _connect_mailbox(org_id)
    _stub_mailbox(monkeypatch)
    other = create_user_and_login(client, username="bob", org="org_b")
    resp = client.put("/api/org/email-trigger",
                      headers={"Authorization": f"Bearer {other}"},
                      json={"workflow_name": "triage", "enabled": True})
    assert resp.status_code == 400  # 'triage' is invisible from org_b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger_api.py -q`
Expected: ERROR with `ModuleNotFoundError: No module named 'ui.backend.email_trigger_api'`.

- [ ] **Step 3: Implement the router and include it**

Create `ui/backend/email_trigger_api.py`:

```python
"""Org-scoped API for the autonomous email trigger (`/api/org/email-trigger`).

A sibling of `org_settings.py` (same `get_current_org` guard) kept in its own
module. All rejection messages are written for the non-technical customer --
never env-var names, OS codes, or tracebacks (those go to the server log).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from bestteam.tools.email_client import _ImapBackend

from . import secret_store
from .auth_api import get_current_org
from .db.email_credentials import get_email_credentials
from .db.email_triggers import get_email_trigger, upsert_email_trigger
from .db.models import EmailTrigger, Organization, WorkflowRecord
from .db_session import get_db
from .email_tools import spec_uses_email
from .email_trigger import daily_cap, mailbox_state, triggers_disabled, _today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org", tags=["email-trigger"])


class EmailTriggerRequest(BaseModel):
    workflow_name: str
    enabled: bool


def _status_of(trigger: EmailTrigger | None) -> str:
    if trigger is None or not trigger.enabled:
        return "off"
    if triggers_disabled():
        return "disabled"
    if trigger.last_error:
        return "error"
    if trigger.runs_date == _today() and trigger.runs_today >= daily_cap():
        return "paused_cap"
    return "active"


def _payload(trigger: EmailTrigger | None) -> Dict[str, Any]:
    runs_today = 0
    if trigger is not None and trigger.runs_date == _today():
        runs_today = trigger.runs_today
    return {
        "enabled": bool(trigger is not None and trigger.enabled),
        "workflow_name": trigger.workflow_name if trigger is not None else None,
        "status": _status_of(trigger),
        "runs_today": runs_today,
        "daily_cap": daily_cap(),
        "last_checked_at": (
            trigger.last_checked_at.isoformat()
            if trigger is not None and trigger.last_checked_at
            else None
        ),
        "last_error": trigger.last_error if trigger is not None else None,
    }


@router.get("/email-trigger")
def get_trigger_status(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    return _payload(get_email_trigger(db, org.id))


@router.put("/email-trigger")
def set_trigger(
    req: EmailTriggerRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    if not req.enabled:
        trigger = get_email_trigger(db, org.id)
        if trigger is not None:
            trigger.enabled = False
            db.commit()
        return _payload(get_email_trigger(db, org.id))

    record = (
        db.query(WorkflowRecord)
        .filter_by(name=req.workflow_name, org_id=org.id)
        .one_or_none()
    )
    if record is None or record.status != "deployed":
        raise HTTPException(
            status_code=400,
            detail="That team isn't live yet -- launch it before turning on automatic runs.",
        )
    if not spec_uses_email(db, record.config, org.id):
        raise HTTPException(
            status_code=400,
            detail="This team doesn't use email, so new mail can't trigger it.",
        )
    cred = get_email_credentials(db, org.id)
    if cred is None:
        raise HTTPException(
            status_code=400,
            detail="Connect your mailbox before turning on automatic runs.",
        )
    try:
        password = secret_store.decrypt(cred.password_encrypted)
        backend = _ImapBackend(
            host=cred.host, user=cred.username, password=password,
            port=cred.port, drafts=cred.drafts_folder, restrict_to_public=True,
        )
        # Baseline = the mailbox's current max UID: the backlog never triggers.
        uidvalidity, max_uid = mailbox_state(backend)
    except Exception as exc:  # noqa: BLE001 -- always a friendly message outward
        logger.warning("email trigger enable: mailbox check failed for org %s: %s",
                       org.id, exc)
        raise HTTPException(
            status_code=400,
            detail=(
                "Couldn't reach your mailbox to start automatic runs. Test your "
                "mailbox connection and try again."
            ),
        ) from exc
    trigger = upsert_email_trigger(
        db, org.id, workflow_name=req.workflow_name, enabled=True,
        last_uid=max_uid, uidvalidity=uidvalidity,
    )
    return _payload(trigger)
```

In `ui/backend/main.py`, beside the other router imports add:

```python
from .email_trigger_api import router as email_trigger_router
```

and beside the other `app.include_router(...)` calls add:

```python
app.include_router(email_trigger_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger_api.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger_api.py ui/backend/main.py tests/test_email_trigger_api.py
git commit -m "feat(trigger): org-scoped enable/disable + status API

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: API — activity list

**Files:**
- Modify: `ui/backend/email_trigger_api.py`
- Test: `tests/test_email_trigger_api.py` (append)

**Interfaces:**
- Consumes: `Run` model (`ui.backend.db.models`), `TRIGGER_USERNAME` from `ui.backend.email_trigger`.
- Produces: `GET /api/org/email-trigger/activity` → `{"runs": [{id, workflow, status, started_at, autonomous}]}` newest-first, max 50, this org only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_trigger_api.py`:

```python
# --- activity list ------------------------------------------------------------

from datetime import datetime, timedelta, timezone

from ui.backend.db.models import Run


def _add_run(org_id, run_id, username, minutes_ago, status="completed"):
    with open_test_db() as db:
        db.add(Run(id=run_id, workflow="triage", input="x", status=status,
                   org_id=org_id, username=username,
                   created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
        db.commit()


def test_activity_lists_org_runs_newest_first_with_autonomous_flag(client):
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
    _add_run(org_id, "r-old", "email-trigger", minutes_ago=10)
    _add_run(org_id, "r-new", "demo", minutes_ago=1)
    body = client.get("/api/org/email-trigger/activity").json()
    assert [r["id"] for r in body["runs"]] == ["r-new", "r-old"]
    assert body["runs"][0]["autonomous"] is False
    assert body["runs"][1]["autonomous"] is True


def test_activity_is_org_scoped(client):
    with open_test_db() as db:
        mine = get_or_create_org(db, "default").id
        theirs = get_or_create_org(db, "org_b").id
    _add_run(mine, "r-mine", "email-trigger", minutes_ago=1)
    _add_run(theirs, "r-theirs", "email-trigger", minutes_ago=1)
    ids = [r["id"] for r in client.get("/api/org/email-trigger/activity").json()["runs"]]
    assert ids == ["r-mine"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger_api.py -q`
Expected: 2 new FAILs with 404 (route missing).

- [ ] **Step 3: Implement the endpoint**

Append to `ui/backend/email_trigger_api.py` (add `Run` to the models import and `TRIGGER_USERNAME` to the `email_trigger` import):

```python
@router.get("/email-trigger/activity")
def trigger_activity(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """Recent runs for this org (newest first, max 50) from the persisted
    `runs` rows -- so autonomous activity is visible even though full Phase-5
    trace persistence doesn't exist yet."""
    rows = (
        db.query(Run)
        .filter(Run.org_id == org.id)
        .order_by(Run.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "runs": [
            {
                "id": r.id,
                "workflow": r.workflow,
                "status": r.status,
                "started_at": r.created_at.isoformat() if r.created_at else None,
                "autonomous": r.username == TRIGGER_USERNAME,
            }
            for r in rows
        ]
    }
```

- [ ] **Step 4: Run tests to verify they pass, then the whole backend suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger_api.py -q` → Expected: `11 passed`.
Run: `./.venv/Scripts/python.exe -m pytest -q` (long, ~2.5 min) → Expected: all pass, no regressions.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger_api.py tests/test_email_trigger_api.py
git commit -m "feat(trigger): org activity list from persisted runs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Frontend — api methods + opt-in toggle on DeployPage

**Files:**
- Modify: `ui/frontend/src/lib/api.js`
- Create: `ui/frontend/src/components/EmailTriggerToggle.jsx`
- Modify: `ui/frontend/src/pages/wizard/DeployPage.jsx`

**Interfaces:**
- Consumes: Task 6's endpoints; existing `request` helper in `api.js`; wizard CSS classes (`wizard-card`, `subtitle`, `banner banner-error`, `wizard-actions`, `btn`, `hint`).
- Produces: `api.getEmailTrigger()`, `api.setEmailTrigger(payload)`, `api.emailTriggerActivity()`; `<EmailTriggerToggle workflowName={...} />` rendered on the deployed card of DeployPage for email teams.

- [ ] **Step 1: Add the api methods**

In `ui/frontend/src/lib/api.js`, after the "Org self-service settings" block (`clearOrgEmail` line), add:

```js
  // Autonomous email trigger: org-level "run on new mail" opt-in + activity.
  getEmailTrigger: () => request('/api/org/email-trigger'),
  setEmailTrigger: (payload) =>
    request('/api/org/email-trigger', { method: 'PUT', body: JSON.stringify(payload) }),
  emailTriggerActivity: () => request('/api/org/email-trigger/activity'),
```

- [ ] **Step 2: Create the toggle component**

Create `ui/frontend/src/components/EmailTriggerToggle.jsx`:

```jsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'

// Org-level opt-in: run this deployed email team automatically on new mail.
// Off by default; shown on the Deploy page once the team is live.
export default function EmailTriggerToggle({ workflowName }) {
  const [trigger, setTrigger] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.getEmailTrigger().then(setTrigger).catch((e) => setError(e.message))
  }, [])

  if (!trigger) return error ? <p className="banner banner-error">{error}</p> : null

  const onForThis = trigger.enabled && trigger.workflow_name === workflowName

  const toggle = async () => {
    setBusy(true)
    setError(null)
    try {
      setTrigger(await api.setEmailTrigger({ workflow_name: workflowName, enabled: !onForThis }))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="wizard-card" style={{ background: '#f9fafb' }}>
      <h3>Automatic runs</h3>
      <p className="subtitle">
        Let "{workflowName}" watch the inbox on its own: it checks for new email every few minutes
        and drafts replies without you having to start it — up to {trigger.daily_cap} automatic runs
        per day. It still only ever saves drafts; it never sends.
      </p>

      {error && <p className="banner banner-error">{error}</p>}
      {onForThis && trigger.status === 'paused_cap' && (
        <p className="banner banner-error">
          Paused — today's limit of {trigger.daily_cap} automatic runs was reached. Runs resume tomorrow.
        </p>
      )}
      {onForThis && trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}

      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={toggle} disabled={busy}>
          {busy ? 'Saving…' : onForThis ? 'Turn off automatic runs' : 'Run automatically when new email arrives'}
        </button>
        {onForThis && trigger.status === 'active' && (
          <span className="hint">On — watching for new email.</span>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 3: Render it on the deployed card of DeployPage**

In `ui/frontend/src/pages/wizard/DeployPage.jsx`: add the import

```jsx
import EmailTriggerToggle from '../../components/EmailTriggerToggle'
```

and inside the `session.status === 'deployed'` branch, after the success banner `<p>` and before the `wizard-actions` div, insert:

```jsx
        {session.uses_email && <EmailTriggerToggle workflowName={spec.name} />}
```

- [ ] **Step 4: Verify lint + build**

Run: `cd ui/frontend && npm run lint && npm run build`
Expected: lint silent, build succeeds.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/lib/api.js ui/frontend/src/components/EmailTriggerToggle.jsx ui/frontend/src/pages/wizard/DeployPage.jsx
git commit -m "feat(trigger): wizard opt-in toggle for automatic runs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Frontend — status + activity on the "My teams" page

**Files:**
- Create: `ui/frontend/src/components/EmailTriggerActivity.jsx`
- Modify: `ui/frontend/src/pages/wizard/SessionsPage.jsx`

**Interfaces:**
- Consumes: Task 8's api methods; `status-badge`/`hint`/`wizard-card` CSS classes.
- Produces: `<EmailTriggerActivity />` — renders nothing when the trigger is off; otherwise a status line + recent autonomous runs.

- [ ] **Step 1: Create the component**

Create `ui/frontend/src/components/EmailTriggerActivity.jsx`:

```jsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'

const STATUS_LABELS = {
  active: 'Active — watching for new email',
  paused_cap: 'Paused — daily limit reached (resumes tomorrow)',
  error: 'Problem checking the mailbox',
  disabled: 'Paused by the operator',
}

// Org-level automatic-runs status + recent autonomous activity, shown on
// "My teams". Renders nothing while automatic runs are off.
export default function EmailTriggerActivity() {
  const [trigger, setTrigger] = useState(null)
  const [runs, setRuns] = useState([])

  useEffect(() => {
    api.getEmailTrigger().then(setTrigger).catch(() => setTrigger(null))
    api
      .emailTriggerActivity()
      .then((d) => setRuns(d.runs.filter((r) => r.autonomous).slice(0, 10)))
      .catch(() => setRuns([]))
  }, [])

  if (!trigger?.enabled) return null

  return (
    <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
      <h3>Automatic runs — "{trigger.workflow_name}"</h3>
      <p className="subtitle">{STATUS_LABELS[trigger.status] ?? trigger.status}</p>
      {trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}
      {runs.length === 0 ? (
        <p className="hint">No automatic runs yet — they'll show up here when new email arrives.</p>
      ) : (
        <ul className="session-list">
          {runs.map((r) => (
            <li key={r.id} className="hint">
              <span className="status-badge">{r.status}</span>{' '}
              {r.started_at ? new Date(r.started_at).toLocaleString() : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Render it on SessionsPage**

In `ui/frontend/src/pages/wizard/SessionsPage.jsx`: add the import

```jsx
import EmailTriggerActivity from '../../components/EmailTriggerActivity'
```

and directly after the closing `</header>` tag, insert:

```jsx
      <EmailTriggerActivity />
```

- [ ] **Step 3: Verify lint + build**

Run: `cd ui/frontend && npm run lint && npm run build`
Expected: lint silent, build succeeds.

- [ ] **Step 4: Commit**

```bash
git add ui/frontend/src/components/EmailTriggerActivity.jsx ui/frontend/src/pages/wizard/SessionsPage.jsx
git commit -m "feat(trigger): automatic-runs status + activity on My teams

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Docs, env, CLAUDE.md, STATUS.md + full verification

**Files:**
- Modify: `.env.example`, `docs/deployment.md`, `src/bestteam/tools/CLAUDE.md`, `ui/backend/CLAUDE.md`, `ui/backend/db/CLAUDE.md`, `docs/STATUS.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: `.env.example`** — after the `BESTTEAM_DEMO_WORKFLOWS` block, add:

```bash
# Autonomous email trigger (customer opt-in, per org, in the wizard). The
# poller checks each opted-in org's mailbox for new mail and runs that org's
# deployed email team automatically. All three optional:
#   BESTTEAM_TRIGGER_POLL_SECONDS  how often to check (default 120)
#   BESTTEAM_TRIGGER_DAILY_CAP     max automatic runs per org per day (default 50)
#   BESTTEAM_TRIGGERS_DISABLED     set to 1 to pause ALL automatic runs platform-wide
BESTTEAM_TRIGGER_POLL_SECONDS=
BESTTEAM_TRIGGER_DAILY_CAP=
BESTTEAM_TRIGGERS_DISABLED=
```

- [ ] **Step 2: `docs/deployment.md`** — add a subsection at the end of §4c ("Connect each org's mailbox"):

```markdown
### Automatic runs (autonomous email trigger)

Once a customer's email team is deployed and their mailbox is connected, they
can opt in (Deploy page: "Run automatically when new email arrives") to have
the platform poll their inbox every `BESTTEAM_TRIGGER_POLL_SECONDS` (default
120) and run the team on new mail — no prompt needed. Safety rails:
`BESTTEAM_TRIGGER_DAILY_CAP` automatic runs per org per day (default 50, then
paused until midnight UTC), and `BESTTEAM_TRIGGERS_DISABLED=1` as a
platform-wide operator kill switch. Autonomous runs appear in the org's
activity list attributed to the `email-trigger` user; the team still only
ever saves drafts. Dedup is by IMAP UID baseline, set at enable time, so the
existing mailbox backlog never triggers runs. Design:
`docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md`.
```

- [ ] **Step 3: CLAUDE.md updates**

`src/bestteam/tools/CLAUDE.md`: change the final line

```
Tier 2 tools (SQL executor, Python sandbox), real email *sending*, and
ambient run-on-new-mail triggering are planned but not yet implemented.
```

to

```
Ambient run-on-new-mail triggering exists at the UI-backend layer (an opt-in
per-org poller -- see `ui/backend/email_trigger.py` and `ui/backend/CLAUDE.md`),
not in this SDK layer. Tier 2 tools (SQL executor, Python sandbox) and real
email *sending* are planned but not yet implemented.
```

`ui/backend/CLAUDE.md`: add a short section (beside the org-settings/email material):

```markdown
## Autonomous email trigger (`email_trigger.py` + `email_trigger_api.py`)

Opt-in per org (wizard Deploy page; `/api/org/email-trigger`): an asyncio
poller started from `main._lifespan` checks each enabled org's mailbox every
`BESTTEAM_TRIGGER_POLL_SECONDS` (default 120) and starts ONE run per cycle
covering that cycle's new messages, attributed to the sentinel username
`email-trigger`. Dedup is a per-org IMAP UID baseline in `email_triggers`
(never UNSEEN -- the toolkit never marks mail seen); the baseline is set to
the mailbox's current max UID at enable time so the backlog never triggers.
Guards: per-org daily cap (`BESTTEAM_TRIGGER_DAILY_CAP`, default 50),
platform kill switch (`BESTTEAM_TRIGGERS_DISABLED=1`), overlap guard (skips a
cycle while the previous triggered run is still `running`), and per-org
try/except so one org's mail-server failure never stops the loop (stored as
customer-readable `last_error` on the row). Single-process poller: if the
backend ever runs multiple workers, it needs a leader lock (known limitation).
```

`ui/backend/db/CLAUDE.md`: add to the schema list:

```markdown
- `email_triggers` — one org's autonomous new-mail trigger: opt-in flag +
  target `workflow_name`, UID dedup baseline (`last_uid`/`uidvalidity`),
  daily-cap counters (`runs_today`/`runs_date`), overlap guard
  (`last_run_id`), and health (`last_checked_at`/`last_error`). Unique
  `org_id` — at most one auto-running team per org. CRUD in
  `db/email_triggers.py`; poll-state mutations in `ui/backend/email_trigger.py`.
```

- [ ] **Step 4: `docs/STATUS.md`** — add to **Done** (after the PR #21 / mailbox entries; adjust placement to whatever the section looks like on this branch):

```markdown
- Autonomous email-triggered runs (feature/email-trigger-autonomous-runs):
  customers opt in at Deploy ("Run automatically when new email arrives") and
  the platform polls their mailbox (default 120s) and runs their deployed
  email team on new mail — no prompt. Per-org UID-baseline dedup (backlog
  never triggers), one run per cycle, daily cap (default 50) with midnight
  reset, operator kill switch (`BESTTEAM_TRIGGERS_DISABLED`), overlap guard,
  activity list on "My teams" from persisted `runs` rows (sentinel username
  `email-trigger`). Spec: `2026-07-19-email-trigger-autonomous-runs-design.md`.
```

Also remove "ambient triggering" from any "Deferred/known gaps" phrasing it appears in within STATUS.md's email-toolkit entry, if present.

- [ ] **Step 5: Full verification**

Run: `./.venv/Scripts/python.exe -m pytest -q` → Expected: all pass.
Run: `cd ui/frontend && npm run lint && npm run build` → Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add .env.example docs/deployment.md src/bestteam/tools/CLAUDE.md ui/backend/CLAUDE.md ui/backend/db/CLAUDE.md docs/STATUS.md
git commit -m "docs(trigger): env vars, deployment guide, CLAUDE.md, STATUS for autonomous runs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Post-plan self-review notes

- **Spec coverage:** data model → Task 1; detection/dedup → Tasks 2–3; run triggering → Task 4; poll loop/lifespan/kill switch → Task 5; API/UI → Tasks 6–9; config/docs → Task 10; error-handling table → Tasks 3–6 tests (friendly errors, never-dies loop, overlap guard, cap, restart-persistence via DB state).
- **Push/PR:** after Task 10, push the branch and open a PR against `main` titled "Autonomous email-triggered runs (opt-in new-mail polling)" — but ONLY when the user asks, per repo convention.
- The existing `test_auth.py` lifespan tests are re-run in Task 5 because `_lifespan` changes there.
