# Anonymous Team Sharing With Continuous Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an org's single user generate a revocable link that lets colleagues chat with one deployed team, with no login and no new `users` row, holding a real multi-turn conversation.

**Architecture:** Three new tables (`share_links`, `share_sessions`, `share_messages`) plus a signed session cookie for anonymous visitor identity. Each chat turn is a normal `runs` row (reusing `run_in_background`/metering/trace unchanged) whose `input` is the session's transcript-so-far reformatted as one string — no SDK/engine change. A new hook in `runtime.py` appends the assistant's reply (or a friendly fallback) to `share_messages` on every terminal path. Org-side management reuses `get_current_org`; the visitor path is a new, unrelated cookie-auth mechanism, deliberately not an extension of the JWT bearer system.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), React + TypeScript + Vite + vitest (frontend), pytest + `fastapi.testclient.TestClient`.

**Spec:** `docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md`

## Global Constraints

- No SDK (`src/bestteam/`) or `EngineAdapter`/`LangGraphAdapter` changes — multi-turn state is transcript replay only (spec "Approach").
- Visitor auth is a signed cookie (`share_auth.py`), never a JWT/bearer token and never a new `users` row — the org's one-member invariant is untouched.
- Every real turn creates a normal `runs` row (`username="share-link"`, `trigger_context={"share_link_id", "share_session_id", "turn_number"}`) so metering/trace/cancellation are free (spec "Data model").
- Cross-org access is always 404, never a distinguishable error (existing platform convention, spec "Error handling").
- History is capped to the most recent 20 turns (40 messages) when building the replay transcript (spec "Approach").
- Visitor message length is capped at 4000 characters, rejected (not truncated) over that.
- UI copy is English, matching every existing customer-facing page in this app (`ui/frontend/CLAUDE.md`).

---

## Task 1: DB models + migration for share_links / share_sessions / share_messages

**Files:**
- Modify: `ui/backend/db/models.py` (append after `ModelCatalogEntry`, end of file)
- Create: `alembic/versions/a3f7c9d2e6b1_add_share_links.py`
- Modify: `tests/test_migrations.py:40-55` (`_EXPECTED_HEAD_TABLES`)
- Test: `tests/test_db.py` (new test function)

**Interfaces:**
- Produces: `ShareLink(id, workflow_id, org_id, token, created_by, active, expires_at, daily_cap, created_at)`, `ShareSession(id, share_link_id, session_token, created_at, last_active_at, turns_today, turns_date)`, `ShareMessage(id, share_session_id, turn_number, role, content, run_id, created_at)` — all in `ui.backend.db.models`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_db.py` (follow the file's existing style — check its imports first; it already imports `init_db`/`make_engine` and asserts table presence for other models):

```python
def test_share_tables_exist():
    from sqlalchemy import inspect

    from ui.backend.db import init_db, make_engine

    engine = make_engine(":memory:")
    init_db(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"share_links", "share_sessions", "share_messages"} <= tables
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py::test_share_tables_exist -v`
Expected: FAIL (tables don't exist yet).

- [ ] **Step 3: Add the models**

Append to `ui/backend/db/models.py`, after the `ModelCatalogEntry` class:

```python
class ShareLink(Base):
    """A shareable, revocable entry point letting anonymous colleagues chat
    with one deployed team without a real account.

    Visitor identity is a per-browser ShareSession, never a `users` row --
    this exists specifically so sharing a team doesn't require lifting the
    one-member-per-org constraint (docs/DECISIONS.md). See
    docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md.
    """

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), nullable=False)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    token: Mapped[str] = mapped_column(unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    daily_cap: Mapped[int] = mapped_column(default=30)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class ShareSession(Base):
    """One anonymous visitor's browser against one ShareLink.

    Cookie-identified (`session_token`, embedded in a signed cookie by
    `ui/backend/share_auth.py`) -- never cross-visible to another session on
    the same link. `turns_today`/`turns_date` is the daily rate-limit CAS,
    same shape as `EmailTrigger.runs_today`/`runs_date`.
    """

    __tablename__ = "share_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    share_link_id: Mapped[int] = mapped_column(ForeignKey("share_links.id"), nullable=False)
    session_token: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_active_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    turns_today: Mapped[int] = mapped_column(default=0)
    turns_date: Mapped[Optional[str]] = mapped_column(nullable=True)


class ShareMessage(Base):
    """One turn of a ShareSession's human-readable transcript.

    Deliberately separate from the replay-formatted text actually sent as a
    Run's `input` (see `ui/backend/share_chat.py`) -- this is the clean chat
    log the visitor UI and the org's audit view render. `run_id` links a
    turn to the Run that produced it (metering/trace/cancellation all reuse
    the existing `runs` machinery unchanged).
    """

    __tablename__ = "share_messages"
    __table_args__ = (
        UniqueConstraint("share_session_id", "turn_number", name="uq_share_messages_session_turn"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    share_session_id: Mapped[int] = mapped_column(ForeignKey("share_sessions.id"), nullable=False)
    turn_number: Mapped[int]
    role: Mapped[str]  # "user" | "assistant"
    content: Mapped[str]
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py::test_share_tables_exist -v`
Expected: PASS

- [ ] **Step 5: Write the migration**

Create `alembic/versions/a3f7c9d2e6b1_add_share_links.py` (down_revision is the current head — confirm with `.venv/Scripts/python.exe -m alembic heads` before writing; use whatever it prints if different from `5e806924cfec`):

```python
"""add share_links, share_sessions, share_messages (anonymous team sharing)

Revision ID: a3f7c9d2e6b1
Revises: 5e806924cfec
Create Date: 2026-08-14 12:00:00.000000

Three tables backing anonymous, revocable colleague access to one deployed
team with continuous chat. See
docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md.

Guarded ops (same reason as every other migration here):
`ui/backend/db_session.py` runs `create_all` at import, so a fresh database
already has these tables when this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f7c9d2e6b1'
down_revision: Union[str, Sequence[str], None] = '5e806924cfec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "share_links"):
        op.create_table(
            "share_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workflow_id", sa.Integer(), sa.ForeignKey("workflows.id"), nullable=False),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("token", sa.String(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("token", name="uq_share_links_token"),
        )
    if not _has_table(inspector, "share_sessions"):
        op.create_table(
            "share_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("share_link_id", sa.Integer(), sa.ForeignKey("share_links.id"), nullable=False),
            sa.Column("session_token", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("last_active_at", sa.DateTime(), nullable=True),
            sa.Column("turns_today", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("turns_date", sa.String(), nullable=True),
            sa.UniqueConstraint("session_token", name="uq_share_sessions_session_token"),
        )
    if not _has_table(inspector, "share_messages"):
        op.create_table(
            "share_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("share_session_id", sa.Integer(), sa.ForeignKey("share_sessions.id"), nullable=False),
            sa.Column("turn_number", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("content", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "share_session_id", "turn_number", name="uq_share_messages_session_turn"
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "share_messages"):
        op.drop_table("share_messages")
    if _has_table(inspector, "share_sessions"):
        op.drop_table("share_sessions")
    if _has_table(inspector, "share_links"):
        op.drop_table("share_links")
```

- [ ] **Step 6: Update the migration regression test's expected table set**

In `tests/test_migrations.py`, add the three new tables to `_EXPECTED_HEAD_TABLES` (around line 40-55):

```python
_EXPECTED_HEAD_TABLES = {
    "organizations",
    "users",
    "knowledge_bases",
    "skills",
    "skill_versions",
    "workflows",
    "workflow_dependencies",
    "builder_sessions",
    "email_triggers",
    "model_catalog",
    "runs",
    "trace_events",
    "usage_records",
    "org_email_credentials",
    "share_links",
    "share_sessions",
    "share_messages",
}
```

- [ ] **Step 7: Run the full migration test + model test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_migrations.py tests/test_db.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add ui/backend/db/models.py alembic/versions/a3f7c9d2e6b1_add_share_links.py tests/test_migrations.py tests/test_db.py
git commit -m "feat(db): add share_links/share_sessions/share_messages tables"
```

---

## Task 2: `db/share_links.py` CRUD

**Files:**
- Create: `ui/backend/db/share_links.py`
- Test: `tests/test_share_db.py` (new file — shared by Tasks 2-4)

**Interfaces:**
- Consumes: `ShareLink` from `ui.backend.db.models` (Task 1).
- Produces: `create_share_link(db, *, workflow_id, org_id, created_by, daily_cap=30, expires_at=None) -> ShareLink`, `get_share_link_by_token(db, token) -> Optional[ShareLink]`, `get_share_link(db, link_id, org_id) -> Optional[ShareLink]`, `list_share_links(db, workflow_id, org_id) -> List[ShareLink]`, `patch_share_link(db, link, *, active=None, daily_cap=None, expires_at=None, clear_expiry=False) -> ShareLink`, `count_active_share_links(db, workflow_id) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_share_db.py`:

```python
"""Tests for the share-link/session/message CRUD layer (db/share_links.py,
db/share_sessions.py, db/share_messages.py)."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.models import WorkflowRecord
from ui.backend.db.share_links import (
    count_active_share_links,
    create_share_link,
    get_share_link,
    get_share_link_by_token,
    list_share_links,
    patch_share_link,
)
from ui.backend.db.users import create_user


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _deployed_team(db, org_id, name="team1"):
    record = WorkflowRecord(
        name=name, org_id=org_id,
        config={"name": name, "agents": [], "teams": [], "workflow": {"steps": []}},
        status="deployed",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def test_create_and_get_share_link(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)

    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    assert link.active is True
    assert link.daily_cap == 30
    assert len(link.token) > 20
    fetched = get_share_link_by_token(db, link.token)
    assert fetched.id == link.id


def test_get_share_link_scoped_to_org(db):
    org_a = get_or_create_org(db, "org-a")
    org_b = get_or_create_org(db, "org-b")
    user = create_user(db, "owner", "pw", org_id=org_a.id)
    team = _deployed_team(db, org_a.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org_a.id, created_by=user.id)

    assert get_share_link(db, link.id, org_a.id) is not None
    assert get_share_link(db, link.id, org_b.id) is None


def test_list_share_links_newest_first(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    first = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    second = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    links = list_share_links(db, team.id, org.id)
    assert [l.id for l in links] == [second.id, first.id]


def test_patch_share_link_revoke(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    patched = patch_share_link(db, link, active=False)
    assert patched.active is False
    assert get_share_link_by_token(db, link.token).active is False


def test_patch_share_link_daily_cap_and_expiry(db):
    from datetime import datetime, timezone

    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    expiry = datetime(2027, 1, 1, tzinfo=timezone.utc)

    patched = patch_share_link(db, link, daily_cap=5, expires_at=expiry)
    assert patched.daily_cap == 5
    assert patched.expires_at is not None

    cleared = patch_share_link(db, link, clear_expiry=True)
    assert cleared.expires_at is None


def test_count_active_share_links(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    a = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    assert count_active_share_links(db, team.id) == 2
    patch_share_link(db, a, active=False)
    assert count_active_share_links(db, team.id) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.backend.db.share_links'`

- [ ] **Step 3: Write the implementation**

Create `ui/backend/db/share_links.py`:

```python
"""CRUD for `ShareLink` -- an anonymous, revocable entry point to one
deployed team. Mirrors the shape of `db/email_triggers.py`: small helpers
over one table, no business logic beyond straightforward reads/writes.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from .models import ShareLink


def create_share_link(
    db: Session,
    *,
    workflow_id: int,
    org_id: int,
    created_by: int,
    daily_cap: int = 30,
    expires_at: Optional[datetime] = None,
) -> ShareLink:
    link = ShareLink(
        workflow_id=workflow_id,
        org_id=org_id,
        created_by=created_by,
        token=secrets.token_urlsafe(32),
        daily_cap=daily_cap,
        expires_at=expires_at,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def get_share_link_by_token(db: Session, token: str) -> Optional[ShareLink]:
    return db.query(ShareLink).filter_by(token=token).one_or_none()


def get_share_link(db: Session, link_id: int, org_id: int) -> Optional[ShareLink]:
    """Org-scoped lookup -- another org's link id returns None (404 upstream),
    never revealing whether the id exists at all."""
    return db.query(ShareLink).filter_by(id=link_id, org_id=org_id).one_or_none()


def list_share_links(db: Session, workflow_id: int, org_id: int) -> List[ShareLink]:
    return (
        db.query(ShareLink)
        .filter_by(workflow_id=workflow_id, org_id=org_id)
        .order_by(ShareLink.created_at.desc())
        .all()
    )


def patch_share_link(
    db: Session,
    link: ShareLink,
    *,
    active: Optional[bool] = None,
    daily_cap: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    clear_expiry: bool = False,
) -> ShareLink:
    if active is not None:
        link.active = active
    if daily_cap is not None:
        link.daily_cap = daily_cap
    if clear_expiry:
        link.expires_at = None
    elif expires_at is not None:
        link.expires_at = expires_at
    db.commit()
    db.refresh(link)
    return link


def count_active_share_links(db: Session, workflow_id: int) -> int:
    """Used by the workflow-delete guard (crud.py) -- an active link blocks
    deletion of the team it points at."""
    return db.query(ShareLink).filter_by(workflow_id=workflow_id, active=True).count()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/share_links.py tests/test_share_db.py
git commit -m "feat(db): add ShareLink CRUD"
```

---

## Task 3: `db/share_sessions.py` CRUD + daily-cap CAS

**Files:**
- Create: `ui/backend/db/share_sessions.py`
- Modify: `tests/test_share_db.py` (append)

**Interfaces:**
- Consumes: `ShareSession` from `ui.backend.db.models` (Task 1).
- Produces: `create_share_session(db, share_link_id) -> ShareSession`, `get_share_session_by_token(db, session_token) -> Optional[ShareSession]`, `list_share_sessions(db, share_link_id) -> List[ShareSession]`, `try_consume_turn(db, session, daily_cap) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_share_db.py`:

```python
from ui.backend.db.share_sessions import (
    create_share_session,
    get_share_session_by_token,
    list_share_sessions,
    try_consume_turn,
)


def test_create_and_get_share_session(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)

    session = create_share_session(db, link.id)
    assert session.turns_today == 0
    fetched = get_share_session_by_token(db, session.session_token)
    assert fetched.id == session.id


def test_list_share_sessions_newest_active_first(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    first = create_share_session(db, link.id)
    second = create_share_session(db, link.id)

    sessions = list_share_sessions(db, link.id)
    assert [s.id for s in sessions] == [second.id, first.id]


def test_try_consume_turn_respects_daily_cap(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)

    assert try_consume_turn(db, session, daily_cap=2) is True
    assert try_consume_turn(db, session, daily_cap=2) is True
    assert try_consume_turn(db, session, daily_cap=2) is False
    db.refresh(session)
    assert session.turns_today == 2


def test_try_consume_turn_resets_on_new_day(db, monkeypatch):
    import ui.backend.db.share_sessions as share_sessions_module

    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)

    monkeypatch.setattr(share_sessions_module, "_today", lambda: "2026-08-14")
    assert try_consume_turn(db, session, daily_cap=1) is True
    assert try_consume_turn(db, session, daily_cap=1) is False

    monkeypatch.setattr(share_sessions_module, "_today", lambda: "2026-08-15")
    assert try_consume_turn(db, session, daily_cap=1) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_db.py -v -k share_session`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `ui/backend/db/share_sessions.py`:

```python
"""CRUD for `ShareSession` -- one anonymous visitor's browser against one
ShareLink, plus its daily turn-rate CAS.

Mirrors `db/email_triggers.py`'s `runs_today`/`runs_date` shape, simplified:
no per-org lock is needed here (unlike the mailbox-side-effect stakes of the
email trigger) since a rare CAS-race overshoot of a few extra turns is a
minor cost concern, not a correctness one -- YAGNI per the design's approved
scope.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import ShareSession


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def create_share_session(db: Session, share_link_id: int) -> ShareSession:
    session = ShareSession(share_link_id=share_link_id, session_token=secrets.token_urlsafe(24))
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_share_session_by_token(db: Session, session_token: str) -> Optional[ShareSession]:
    return db.query(ShareSession).filter_by(session_token=session_token).one_or_none()


def list_share_sessions(db: Session, share_link_id: int) -> List[ShareSession]:
    return (
        db.query(ShareSession)
        .filter_by(share_link_id=share_link_id)
        .order_by(ShareSession.last_active_at.desc())
        .all()
    )


def try_consume_turn(db: Session, session: ShareSession, daily_cap: int) -> bool:
    """Atomically claim one turn against today's cap. Returns True if granted.

    Resets the counter first if the stored date has rolled over, then does a
    single conditional UPDATE so two near-simultaneous sends from the same
    session can't both slip through under the cap.
    """
    today = _today()
    if session.turns_date != today:
        db.execute(
            update(ShareSession)
            .where(ShareSession.id == session.id)
            .values(turns_today=0, turns_date=today)
        )
        db.commit()
        db.refresh(session)
    advanced = db.execute(
        update(ShareSession)
        .where(ShareSession.id == session.id, ShareSession.turns_today < daily_cap)
        .values(turns_today=ShareSession.turns_today + 1, last_active_at=datetime.now(timezone.utc))
    ).rowcount
    db.commit()
    db.refresh(session)
    return bool(advanced)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_db.py -v -k share_session`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/share_sessions.py tests/test_share_db.py
git commit -m "feat(db): add ShareSession CRUD and daily-cap CAS"
```

---

## Task 4: `db/share_messages.py` CRUD

**Files:**
- Create: `ui/backend/db/share_messages.py`
- Modify: `tests/test_share_db.py` (append)

**Interfaces:**
- Consumes: `ShareMessage` from `ui.backend.db.models` (Task 1).
- Produces: `append_message(db, share_session_id, *, turn_number, role, content, run_id=None) -> ShareMessage`, `list_messages(db, share_session_id) -> List[ShareMessage]`, `next_turn_number(db, share_session_id) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_share_db.py`:

```python
from ui.backend.db.share_messages import append_message, list_messages, next_turn_number


def test_next_turn_number_starts_at_one(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)

    assert next_turn_number(db, session.id) == 1


def test_append_and_list_messages_in_turn_order(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = _deployed_team(db, org.id)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)

    append_message(db, session.id, turn_number=1, role="user", content="hi")
    assert next_turn_number(db, session.id) == 2
    append_message(db, session.id, turn_number=2, role="assistant", content="hello", run_id=None)

    messages = list_messages(db, session.id)
    assert [(m.turn_number, m.role, m.content) for m in messages] == [
        (1, "user", "hi"),
        (2, "assistant", "hello"),
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_db.py -v -k "turn_number or append_and_list"`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `ui/backend/db/share_messages.py`:

```python
"""CRUD for `ShareMessage` -- the human-readable transcript for a
ShareSession (see `ui/backend/share_chat.py` for how a turn is produced)."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .models import ShareMessage


def append_message(
    db: Session,
    share_session_id: int,
    *,
    turn_number: int,
    role: str,
    content: str,
    run_id: Optional[str] = None,
) -> ShareMessage:
    message = ShareMessage(
        share_session_id=share_session_id,
        turn_number=turn_number,
        role=role,
        content=content,
        run_id=run_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def list_messages(db: Session, share_session_id: int) -> List[ShareMessage]:
    return (
        db.query(ShareMessage)
        .filter_by(share_session_id=share_session_id)
        .order_by(ShareMessage.turn_number)
        .all()
    )


def next_turn_number(db: Session, share_session_id: int) -> int:
    last = (
        db.query(ShareMessage.turn_number)
        .filter_by(share_session_id=share_session_id)
        .order_by(ShareMessage.turn_number.desc())
        .first()
    )
    return (last[0] + 1) if last else 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_db.py -v`
Expected: PASS (all of Tasks 2-4's tests)

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/share_messages.py tests/test_share_db.py
git commit -m "feat(db): add ShareMessage CRUD"
```

---

## Task 5: `share_auth.py` — signed session cookie

**Files:**
- Create: `ui/backend/share_auth.py`
- Test: `tests/test_share_auth.py`

**Interfaces:**
- Consumes: `SECRET_KEY` from `ui.backend.auth`.
- Produces: `COOKIE_NAME: str`, `sign_session_token(session_token: str) -> str`, `verify_cookie_value(value: str) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_share_auth.py`:

```python
"""Tests for the anonymous share-session signed cookie (share_auth.py) --
deliberately separate from auth.py's JWT bearer tokens."""

from ui.backend.share_auth import sign_session_token, verify_cookie_value


def test_sign_then_verify_round_trips():
    signed = sign_session_token("abc123")
    assert verify_cookie_value(signed) == "abc123"


def test_verify_rejects_tampered_token():
    signed = sign_session_token("abc123")
    tampered = signed.replace("abc123", "xyz999")
    assert verify_cookie_value(tampered) is None


def test_verify_rejects_tampered_signature():
    signed = sign_session_token("abc123")
    token_part, _sig = signed.rsplit(".", 1)
    assert verify_cookie_value(f"{token_part}.notarealsignature") is None


def test_verify_rejects_malformed_value():
    assert verify_cookie_value("no-dot-separator") is None
    assert verify_cookie_value("") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_auth.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `ui/backend/share_auth.py`:

```python
"""Signed session-cookie primitives for anonymous share-link visitors.

Deliberately separate from `auth.py`'s JWT bearer tokens: a share session has
no username, no password-reset-driven revocation, and no expiry tied to
`User.security_stamp` -- reusing those primitives would borrow semantics that
don't apply to an anonymous visitor. Uses the same HMAC building block and
`SECRET_KEY` as `auth.py`, in its own function pair.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from .auth import SECRET_KEY

COOKIE_NAME = "bestteam_share_session"


def _signature_for(session_token: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), session_token.encode("ascii"), hashlib.sha256).hexdigest()


def sign_session_token(session_token: str) -> str:
    """Wrap `session_token` (the ShareSession.session_token column value) in
    an HMAC signature suitable for a cookie value."""
    return f"{session_token}.{_signature_for(session_token)}"


def verify_cookie_value(value: str) -> Optional[str]:
    """Return the embedded session_token if `value`'s signature is valid,
    else None (malformed, empty, or tampered)."""
    try:
        session_token, signature = value.rsplit(".", 1)
    except ValueError:
        return None
    if not session_token:
        return None
    expected = _signature_for(session_token)
    if not hmac.compare_digest(expected, signature):
        return None
    return session_token
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/share_auth.py tests/test_share_auth.py
git commit -m "feat(auth): add signed session cookie for anonymous share-link visitors"
```

---

## Task 6: `share_links_api.py` — org-authenticated management router

**Files:**
- Create: `ui/backend/share_links_api.py`
- Test: `tests/test_share_links_api.py`

**Interfaces:**
- Consumes: `get_current_org`, `get_current_user` (`ui.backend.auth_api`); `create_share_link`, `get_share_link`, `list_share_links`, `patch_share_link` (Task 2); `create_share_session`... no — this router does NOT create sessions, only reads them: `list_share_sessions` (Task 3); `list_messages` (Task 4).
- Produces: `router: APIRouter` (mounted in Task 10 at `/api`, giving `POST /api/workflows/{workflow_id}/share-links`, `GET /api/workflows/{workflow_id}/share-links`, `PATCH /api/share-links/{id}`, `GET /api/share-links/{id}/sessions`, `GET /api/share-links/{id}/sessions/{session_id}/messages`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_share_links_api.py`:

```python
"""API tests for org-side share-link management (/api/workflows/{id}/share-links,
/api/share-links/{id}, .../sessions)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import WorkflowRecord
from ui.backend.db_session import get_db

_TEAM_CONFIG = {
    "name": "greeter",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def client():
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
        token = create_user_and_login(c)
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _deploy_team(status="deployed", org_name="default"):
    with open_test_db() as db:
        org_id = get_org_id(org_name)
        record = WorkflowRecord(name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status=status)
        db.add(record)
        db.commit()
        db.refresh(record)
        return record.id


def test_create_share_link_requires_deployed_team(client):
    workflow_id = _deploy_team(status="draft")
    resp = client.post(f"/api/workflows/{workflow_id}/share-links", json={})
    assert resp.status_code == 404


def test_create_and_list_share_links(client):
    workflow_id = _deploy_team()
    created = client.post(f"/api/workflows/{workflow_id}/share-links", json={"daily_cap": 10})
    assert created.status_code == 201
    body = created.json()
    assert body["active"] is True
    assert body["daily_cap"] == 10
    assert body["token"]

    listed = client.get(f"/api/workflows/{workflow_id}/share-links")
    assert listed.status_code == 200
    assert [l["id"] for l in listed.json()] == [body["id"]]


def test_patch_share_link_revokes(client):
    workflow_id = _deploy_team()
    link = client.post(f"/api/workflows/{workflow_id}/share-links", json={}).json()

    patched = client.patch(f"/api/share-links/{link['id']}", json={"active": False})
    assert patched.status_code == 200
    assert patched.json()["active"] is False


def test_patch_unknown_share_link_is_404(client):
    resp = client.patch("/api/share-links/999999", json={"active": False})
    assert resp.status_code == 404


def test_share_links_are_org_scoped(client):
    workflow_id = _deploy_team()
    link = client.post(f"/api/workflows/{workflow_id}/share-links", json={}).json()

    other_client = TestClient(backend_main.app)
    other_token = create_user_and_login(other_client, username="other", org="other-org")
    other_client.headers["Authorization"] = f"Bearer {other_token}"

    resp = other_client.patch(f"/api/share-links/{link['id']}", json={"active": False})
    assert resp.status_code == 404
    resp = other_client.get(f"/api/workflows/{workflow_id}/share-links")
    assert resp.status_code == 404  # not this org's deployed team either


def test_list_sessions_and_messages_for_a_link(client):
    from ui.backend.db.share_messages import append_message
    from ui.backend.db.share_sessions import create_share_session

    workflow_id = _deploy_team()
    link = client.post(f"/api/workflows/{workflow_id}/share-links", json={}).json()

    with open_test_db() as db:
        session = create_share_session(db, link["id"])
        append_message(db, session.id, turn_number=1, role="user", content="hi there")
        append_message(db, session.id, turn_number=2, role="assistant", content="hello!")
        session_id = session.id

    sessions = client.get(f"/api/share-links/{link['id']}/sessions")
    assert sessions.status_code == 200
    assert [s["id"] for s in sessions.json()] == [session_id]

    messages = client.get(f"/api/share-links/{link['id']}/sessions/{session_id}/messages")
    assert messages.status_code == 200
    assert [(m["role"], m["content"]) for m in messages.json()] == [
        ("user", "hi there"),
        ("assistant", "hello!"),
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_links_api.py -v`
Expected: FAIL (404s everywhere — the router isn't mounted, or `ModuleNotFoundError` for `ui.backend.share_links_api`)

- [ ] **Step 3: Write the implementation**

Create `ui/backend/share_links_api.py`:

```python
"""Org-scoped management of ShareLinks -- lets the org's one user share a
deployed team with colleagues without giving them a real account (see
docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md).

Every route here requires `get_current_org` (a logged-in org member). A
colleague using the resulting link never authenticates this way -- see
`share_chat.py` for the separate, anonymous cookie-auth surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth_api import get_current_org, get_current_user
from .db.models import Organization, ShareLink, ShareSession, User, WorkflowRecord
from .db.share_links import create_share_link, get_share_link, list_share_links, patch_share_link
from .db.share_messages import list_messages
from .db.share_sessions import list_share_sessions
from .db_session import get_db

router = APIRouter(prefix="/api", tags=["share-links"])


class ShareLinkCreate(BaseModel):
    daily_cap: int = Field(default=30, ge=1, le=1000)
    expires_at: Optional[datetime] = None


class ShareLinkPatch(BaseModel):
    active: Optional[bool] = None
    daily_cap: Optional[int] = Field(default=None, ge=1, le=1000)
    expires_at: Optional[datetime] = None
    clear_expiry: bool = False


def _share_link_dict(link: ShareLink) -> dict:
    return {
        "id": link.id,
        "workflow_id": link.workflow_id,
        "token": link.token,
        "active": link.active,
        "daily_cap": link.daily_cap,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "created_at": link.created_at.isoformat(),
    }


def _share_session_dict(session: ShareSession) -> dict:
    return {
        "id": session.id,
        "created_at": session.created_at.isoformat(),
        "last_active_at": session.last_active_at.isoformat(),
        "turns_today": session.turns_today,
    }


def _get_deployed_workflow_or_404(db: Session, workflow_id: int, org_id: int) -> WorkflowRecord:
    record = (
        db.query(WorkflowRecord)
        .filter_by(id=workflow_id, org_id=org_id, status="deployed")
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown deployed team '{workflow_id}'")
    return record


@router.post("/workflows/{workflow_id}/share-links", status_code=201)
def create_share_link_endpoint(
    workflow_id: int,
    body: ShareLinkCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> dict:
    _get_deployed_workflow_or_404(db, workflow_id, org.id)
    link = create_share_link(
        db,
        workflow_id=workflow_id,
        org_id=org.id,
        created_by=user.id,
        daily_cap=body.daily_cap,
        expires_at=body.expires_at,
    )
    return _share_link_dict(link)


@router.get("/workflows/{workflow_id}/share-links")
def list_share_links_endpoint(
    workflow_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> List[dict]:
    _get_deployed_workflow_or_404(db, workflow_id, org.id)
    return [_share_link_dict(link) for link in list_share_links(db, workflow_id, org.id)]


@router.patch("/share-links/{link_id}")
def patch_share_link_endpoint(
    link_id: int,
    body: ShareLinkPatch,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> dict:
    link = get_share_link(db, link_id, org.id)
    if link is None:
        raise HTTPException(status_code=404, detail=f"Unknown share link '{link_id}'")
    link = patch_share_link(
        db, link,
        active=body.active, daily_cap=body.daily_cap,
        expires_at=body.expires_at, clear_expiry=body.clear_expiry,
    )
    return _share_link_dict(link)


@router.get("/share-links/{link_id}/sessions")
def list_share_sessions_endpoint(
    link_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> List[dict]:
    link = get_share_link(db, link_id, org.id)
    if link is None:
        raise HTTPException(status_code=404, detail=f"Unknown share link '{link_id}'")
    return [_share_session_dict(s) for s in list_share_sessions(db, link_id)]


@router.get("/share-links/{link_id}/sessions/{session_id}/messages")
def get_share_session_messages_endpoint(
    link_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> List[dict]:
    link = get_share_link(db, link_id, org.id)
    if link is None:
        raise HTTPException(status_code=404, detail=f"Unknown share link '{link_id}'")
    session = db.query(ShareSession).filter_by(id=session_id, share_link_id=link_id).one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session '{session_id}'")
    return [
        {
            "role": m.role,
            "content": m.content,
            "turn_number": m.turn_number,
            "created_at": m.created_at.isoformat(),
        }
        for m in list_messages(db, session_id)
    ]
```

Then wire it into `main.py` (import + `include_router`) — **this is done in Task 10**, since `main.py` also needs the `share_chat` router and the CORS change in the same place; mounting one router alone here would make this task's own tests fail to route. For now, add the import/include directly so this task's tests pass in isolation, and Task 10 will not duplicate it (check first):

In `ui/backend/main.py`, add near the other router imports (after `from .run_analytics_api import router as run_analytics_router`):

```python
from .share_links_api import router as share_links_router
```

And near the other `app.include_router(...)` calls (after `app.include_router(run_analytics_router)`):

```python
app.include_router(share_links_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_links_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/share_links_api.py ui/backend/main.py tests/test_share_links_api.py
git commit -m "feat(api): add org-side share-link management endpoints"
```

---

## Task 7: `share_transcript.py` + wire into `runtime.py`

**Files:**
- Create: `ui/backend/share_transcript.py`
- Modify: `ui/backend/runtime.py:23` (import), `:309-330` (add closure), `:390` (`_mark_cancelled`), `:471` (streaming-loop terminal branch), `:581` (except-Exception fallback)
- Test: `tests/test_share_transcript.py`

**Interfaces:**
- Consumes: `Run` (`ui.backend.db.models`), `append_message` (Task 4).
- Produces: `record_share_reply(db: Session, run_row: Run, output: Optional[str]) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_share_transcript.py`:

```python
"""Tests for share_transcript.record_share_reply -- the hook that appends a
share-chat run's assistant reply (or a friendly fallback) to share_messages
on every terminal path."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.share_links import create_share_link
from ui.backend.db.share_messages import append_message, list_messages
from ui.backend.db.share_sessions import create_share_session
from ui.backend.db.users import create_user
from ui.backend.db.models import WorkflowRecord
from ui.backend.share_transcript import record_share_reply


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _share_session(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "owner", "pw", org_id=org.id)
    team = WorkflowRecord(
        name="t", org_id=org.id,
        config={"name": "t", "agents": [], "teams": [], "workflow": {"steps": []}},
        status="deployed",
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    link = create_share_link(db, workflow_id=team.id, org_id=org.id, created_by=user.id)
    return create_share_session(db, link.id)


def test_records_the_reply_for_a_share_run(db):
    session = _share_session(db)
    append_message(db, session.id, turn_number=1, role="user", content="hi")
    run_row = Run(
        id="run-1", workflow="t", input="hi", org_id=1,
        trigger_context={"share_link_id": 1, "share_session_id": session.id, "turn_number": 1},
    )
    db.add(run_row)
    db.commit()

    record_share_reply(db, run_row, "hello there")

    messages = list_messages(db, session.id)
    assert [(m.turn_number, m.role, m.content) for m in messages] == [
        (1, "user", "hi"),
        (2, "assistant", "hello there"),
    ]


def test_falls_back_to_a_friendly_message_when_output_is_none(db):
    session = _share_session(db)
    append_message(db, session.id, turn_number=1, role="user", content="hi")
    run_row = Run(
        id="run-2", workflow="t", input="hi", org_id=1,
        trigger_context={"share_link_id": 1, "share_session_id": session.id, "turn_number": 1},
    )
    db.add(run_row)
    db.commit()

    record_share_reply(db, run_row, None)

    messages = list_messages(db, session.id)
    assert messages[-1].role == "assistant"
    assert messages[-1].content  # non-empty friendly fallback


def test_no_op_for_a_run_without_trigger_context(db):
    run_row = Run(id="run-3", workflow="t", input="hi", org_id=1, trigger_context=None)
    # No share_session_id anywhere -- must not raise, must not write anything.
    record_share_reply(None, run_row, "irrelevant")


def test_no_op_for_a_run_with_unrelated_trigger_context(db):
    # e.g. an email-triggered run's trigger_context (no share_session_id key).
    run_row = Run(
        id="run-4", workflow="t", input="hi", org_id=1,
        trigger_context={"mailbox_credential_id": 1},
    )
    record_share_reply(db, run_row, "irrelevant")
    # No exception, and no share_messages row was created for anyone.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_transcript.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `ui/backend/share_transcript.py`:

```python
"""Appends a share-chat run's assistant reply to `share_messages` once it
reaches a terminal state -- called from `runtime.py::run_in_background` on
every terminal path a run with `trigger_context["share_session_id"]` can
take, mirroring `automation_results.py::normalize_run_result`'s placement
for the email-trigger vertical.

No-op for any run without that key (a regular manual run, or an
email-triggered run's `trigger_context`, which has different keys) -- this
never touches an unrelated execution.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .db.models import Run
from .db.share_messages import append_message

_FALLBACK_REPLY = "Sorry, something went wrong producing a reply."


def record_share_reply(db: Optional[Session], run_row: Run, output: Optional[str]) -> None:
    """`output` is the real final text for a completed run, or `None`/a
    friendly string for a failed/cancelled/crashed one. This must be called
    on every terminal path so a share session's "last message is still
    unanswered" guard (`share_chat.py::_has_pending_turn`) never wedges a
    visitor's chat shut after a failure.
    """
    if db is None or run_row.trigger_context is None:
        return
    share_session_id = run_row.trigger_context.get("share_session_id")
    turn_number = run_row.trigger_context.get("turn_number")
    if share_session_id is None or turn_number is None:
        return
    append_message(
        db,
        share_session_id,
        turn_number=turn_number + 1,
        role="assistant",
        content=output or _FALLBACK_REPLY,
        run_id=run_row.id,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_transcript.py -v`
Expected: PASS

- [ ] **Step 5: Wire the hook into `runtime.py`**

In `ui/backend/runtime.py`, add the import near the other local imports (after `from .automation_results import RESULT_TYPE_BATCH_MARKER, normalize_run_result`):

```python
from .share_transcript import record_share_reply
```

Add a sibling closure right after the existing `_maybe_normalize` closure (it ends at the line `_maybe_normalize` docstring/body finishes, immediately before the outer `try:`):

```python
    def _maybe_record_share_reply(output: Optional[str]) -> None:
        # Share-chat turns (share_chat.py) are regular runs stamped with
        # trigger_context["share_session_id"] -- append the assistant's
        # reply (or record_share_reply's own friendly fallback) so the
        # visitor's chat page sees an answer and share_chat.py's
        # "last message is unanswered" guard never wedges the session shut.
        # No-op for every other run (see record_share_reply's own guard).
        if run_row is not None:
            record_share_reply(db, run_row, output)
```

Call it alongside each existing `_maybe_normalize(...)` call:

1. Inside `_mark_cancelled()`, right after `_maybe_normalize()`:

```python
                run_row.status = "cancelled"
                run_row.output = cancelled.data
                db.commit()
                _maybe_normalize()
                _maybe_record_share_reply("This conversation was stopped before a reply was ready.")
```

2. Inside the streaming loop's terminal branch, right after `_maybe_normalize(raw_run_completed_output)`:

```python
                        db.commit()
                        _maybe_normalize(raw_run_completed_output)
                        _maybe_record_share_reply(
                            run_row.output if event.type == "run_completed" else None
                        )
                    terminal_seen = True
```

3. Inside the outer `except Exception` fallback, right after its `_maybe_normalize()` call:

```python
                    db.commit()
                    _maybe_normalize()
                    _maybe_record_share_reply(None)
                except Exception:  # noqa: BLE001
```

- [ ] **Step 6: Add a runtime-level integration test**

Add to `tests/test_share_transcript.py`:

```python
def test_run_in_background_records_share_reply_on_success(db, monkeypatch):
    from bestteam import Workflow
    from bestteam.core.loader import _build_workflow
    from ui.backend.runtime import run_in_background

    session = _share_session(db)
    append_message(db, session.id, turn_number=1, role="user", content="hi")

    config = {
        "name": "t", "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hello!"}],
        "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["tm"]},
    }
    workflow = _build_workflow(config)

    db.add(Run(
        id="run-5", workflow="t", input="hi", org_id=1,
        trigger_context={"share_link_id": 1, "share_session_id": session.id, "turn_number": 1},
    ))
    db.commit()

    run_in_background("run-5", workflow, "hi", engine=db.get_bind())

    messages = list_messages(db, session.id)
    assert messages[-1].role == "assistant"
    assert messages[-1].content  # the fake model's output
```

- [ ] **Step 7: Run all tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_transcript.py -v`
Expected: PASS

- [ ] **Step 8: Run the full existing runtime/email-trigger test suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py tests/test_automation_results_api.py tests/test_trace_persistence.py -v`
Expected: PASS (unchanged — `record_share_reply` no-ops for every run without a `share_session_id` key, including every email-triggered run)

- [ ] **Step 9: Commit**

```bash
git add ui/backend/share_transcript.py ui/backend/runtime.py tests/test_share_transcript.py
git commit -m "feat(runtime): record share-chat assistant replies on every terminal run path"
```

---

## Task 8: `share_chat.py` — public POST/GET message endpoints

**Files:**
- Create: `ui/backend/share_chat.py`
- Test: `tests/test_share_chat_api.py`

**Interfaces:**
- Consumes: `_resolve_workflow_and_version` (`ui.backend.main`, imported locally to avoid a circular import since `main.py` will import this router in Task 10); `registry`, `run_in_background`, `_executor` (`ui.backend.runtime`); `COOKIE_NAME`, `sign_session_token`, `verify_cookie_value` (Task 5); CRUD from Tasks 2-4.
- Produces: `router: APIRouter` with `POST /api/share/{token}/messages`, `GET /api/share/{token}/messages` (mounted in Task 10).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_share_chat_api.py`:

```python
"""API tests for the anonymous share-chat surface (/api/share/{token}/messages)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Organization, WorkflowRecord
from ui.backend.db.share_links import create_share_link, patch_share_link
from ui.backend.db.users import create_user
from ui.backend.db_session import get_db

_TEAM_CONFIG = {
    "name": "greeter",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hello!"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()

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
        yield TestClient(backend_main.app)
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _make_link(**overrides):
    with open_test_db() as db:
        org_id = get_org_id()
        user = create_user(db, "owner", "pw", org_id=org_id)
        team = WorkflowRecord(name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status="deployed")
        db.add(team)
        db.commit()
        db.refresh(team)
        link = create_share_link(db, workflow_id=team.id, org_id=org_id, created_by=user.id, **overrides)
        return link.token, link.id


def test_unknown_token_is_404(client):
    resp = client.post("/api/share/not-a-real-token/messages", json={"content": "hi"})
    assert resp.status_code == 404


def test_send_message_dispatches_a_run_and_sets_cookie(client):
    token, _ = _make_link()
    resp = client.post(f"/api/share/{token}/messages", json={"content": "hi there"})
    assert resp.status_code == 202
    assert resp.json()["run_id"]
    assert client.cookies.get("bestteam_share_session")


def test_revoked_link_is_404(client):
    token, link_id = _make_link()
    with open_test_db() as db:
        from ui.backend.db.share_links import get_share_link_by_token
        link = get_share_link_by_token(db, token)
        patch_share_link(db, link, active=False)

    resp = client.post(f"/api/share/{token}/messages", json={"content": "hi"})
    assert resp.status_code == 404


def test_second_visitor_gets_an_isolated_session(client):
    token, _ = _make_link()
    client.post(f"/api/share/{token}/messages", json={"content": "from visitor A"})

    other = TestClient(backend_main.app)
    other.post(f"/api/share/{token}/messages", json={"content": "from visitor B"})

    a_history = client.get(f"/api/share/{token}/messages").json()["messages"]
    b_history = other.get(f"/api/share/{token}/messages").json()["messages"]
    assert a_history[0]["content"] == "from visitor A"
    assert b_history[0]["content"] == "from visitor B"


def test_get_messages_with_no_cookie_returns_empty(client):
    token, _ = _make_link()
    resp = client.get(f"/api/share/{token}/messages")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_daily_cap_is_enforced(client):
    token, _ = _make_link(daily_cap=1)
    first = client.post(f"/api/share/{token}/messages", json={"content": "one"})
    assert first.status_code == 202
    second = client.post(f"/api/share/{token}/messages", json={"content": "two"})
    assert second.status_code == 429


def test_message_over_length_cap_is_rejected(client):
    token, _ = _make_link()
    resp = client.post(f"/api/share/{token}/messages", json={"content": "x" * 5000})
    assert resp.status_code == 422


def test_deactivated_org_makes_link_unavailable(client):
    token, _ = _make_link()
    with open_test_db() as db:
        org = db.query(Organization).filter_by(id=get_org_id()).one()
        org.active = False
        db.commit()

    resp = client.post(f"/api/share/{token}/messages", json={"content": "hi"})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_chat_api.py -v`
Expected: FAIL (404 everywhere — router not mounted yet / `ModuleNotFoundError`)

- [ ] **Step 3: Write the implementation**

Create `ui/backend/share_chat.py`:

```python
"""Public, anonymous chat surface for a ShareLink -- no login (see
docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md).

Visitor identity is a signed session cookie (`share_auth.py`), never a
`users` row. Every route re-validates the link's active/expiry/org-active
state fresh from the DB (no push-invalidation needed -- mirrors the run-
stream WebSocket's own re-authorize-per-event philosophy, see
`share_stream_api.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db.models import Organization, Run, ShareLink, ShareSession, WorkflowRecord
from .db.share_links import get_share_link_by_token
from .db.share_messages import append_message, list_messages, next_turn_number
from .db.share_sessions import create_share_session, get_share_session_by_token, try_consume_turn
from .db_session import get_db
from .runtime import _executor, registry, run_in_background
from .share_auth import COOKIE_NAME, sign_session_token, verify_cookie_value

router = APIRouter(prefix="/api/share", tags=["share-chat"])

MAX_MESSAGE_LENGTH = 4000
MAX_HISTORY_TURNS = 20
_UNAVAILABLE = "This share link is no longer available."


class ShareMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


def _is_expired(link: ShareLink) -> bool:
    if link.expires_at is None:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return link.expires_at.replace(tzinfo=None) < now


def _resolve_active_link(db: Session, token: str) -> ShareLink:
    link = get_share_link_by_token(db, token)
    if link is None or not link.active or _is_expired(link):
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)
    org = db.get(Organization, link.org_id)
    if org is None or not org.active:
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)
    return link


def _resolve_session_from_cookie(request: Request, db: Session, link: ShareLink) -> Optional[ShareSession]:
    cookie_value = request.cookies.get(COOKIE_NAME)
    session_token = verify_cookie_value(cookie_value) if cookie_value else None
    if session_token is None:
        return None
    session = get_share_session_by_token(db, session_token)
    if session is None or session.share_link_id != link.id:
        return None
    return session


def _get_or_create_session(request: Request, response: Response, db: Session, link: ShareLink) -> ShareSession:
    session = _resolve_session_from_cookie(request, db, link)
    if session is not None:
        return session
    session = create_share_session(db, link.id)
    response.set_cookie(
        COOKIE_NAME,
        sign_session_token(session.session_token),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
    )
    return session


def _has_pending_turn(db: Session, session: ShareSession) -> bool:
    """True if the session's last message is an unanswered user turn --
    either a run still in flight, or one whose terminal event never made it
    to record_share_reply (should not happen, but this still blocks sending
    into an inconsistent state rather than silently overwriting it)."""
    messages = list_messages(db, session.id)
    return bool(messages) and messages[-1].role == "user"


@router.post("/{token}/messages", status_code=202)
def send_share_message(
    token: str,
    body: ShareMessageCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    link = _resolve_active_link(db, token)
    session = _get_or_create_session(request, response, db, link)

    if _has_pending_turn(db, session):
        raise HTTPException(status_code=409, detail="Please wait for the previous reply to finish.")
    if not try_consume_turn(db, session, link.daily_cap):
        raise HTTPException(
            status_code=429, detail="Today's message limit has been reached -- try again tomorrow."
        )

    workflow_record = (
        db.query(WorkflowRecord)
        .filter_by(id=link.workflow_id, org_id=link.org_id, status="deployed")
        .one_or_none()
    )
    if workflow_record is None:
        raise HTTPException(status_code=404, detail="This team is temporarily unavailable.")

    from .main import _resolve_workflow_and_version  # local import: main.py imports this router

    workflow, version_id, workflow_id = _resolve_workflow_and_version(
        workflow_record.name, db, link.org_id
    )

    turn_number = next_turn_number(db, session.id)
    append_message(db, session.id, turn_number=turn_number, role="user", content=body.content)

    history = list_messages(db, session.id)[-(MAX_HISTORY_TURNS * 2):]
    transcript = "\n".join(
        f"{'User' if m.role == 'user' else 'Team'}: {m.content}" for m in history
    )

    run = registry.create(workflow_record.name, transcript, org_id=link.org_id, username="share-link")
    db.add(
        Run(
            id=run.id,
            workflow=workflow_record.name,
            input=transcript,
            org_id=link.org_id,
            username="share-link",
            workflow_version_id=version_id,
            trigger_context={
                "share_link_id": link.id,
                "share_session_id": session.id,
                "turn_number": turn_number,
            },
        )
    )
    db.commit()

    _executor.submit(
        run_in_background,
        run.id,
        workflow,
        transcript,
        engine=db.get_bind(),
        org_id=link.org_id,
        username="share-link",
        workflow_version_id=version_id,
        workflow_id=workflow_id,
    )
    return {"run_id": run.id, "turn_number": turn_number}


@router.get("/{token}/messages")
def get_share_messages(token: str, request: Request, db: Session = Depends(get_db)) -> dict:
    link = _resolve_active_link(db, token)
    session = _resolve_session_from_cookie(request, db, link)
    if session is None:
        return {"messages": []}
    return {
        "messages": [
            {"role": m.role, "content": m.content, "turn_number": m.turn_number}
            for m in list_messages(db, session.id)
        ]
    }
```

- [ ] **Step 4: Mount the router (temporarily, for this task's tests)**

In `ui/backend/main.py`, add the import next to `share_links_router`'s:

```python
from .share_chat import router as share_chat_router
```

And add its `include_router` call next to `share_links_router`'s:

```python
app.include_router(share_chat_router)
```

(Task 10 will add the WebSocket route to this same module and verify both are mounted exactly once — no duplication needed here.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_chat_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ui/backend/share_chat.py ui/backend/main.py tests/test_share_chat_api.py
git commit -m "feat(api): add public anonymous share-chat message endpoints"
```

---

## Task 9: WebSocket streaming for share-chat

**Files:**
- Modify: `ui/backend/share_chat.py` (append the WS route)
- Test: `tests/test_share_chat_ws.py`

**Interfaces:**
- Consumes: `registry` (`ui.backend.runtime`), everything from Task 8.
- Produces: `GET /api/share/{token}/stream/{run_id}` (WebSocket, mounted already via Task 8's `include_router`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_share_chat_ws.py`:

```python
"""Tests for the share-chat WebSocket stream (/api/share/{token}/stream/{run_id})
-- cookie-authenticated, no ticket (contrast tests/test_ws_stream.py)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend import runtime
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run, WorkflowRecord
from ui.backend.db.share_links import create_share_link
from ui.backend.db.users import create_user
from ui.backend.db_session import get_db

_TEAM_CONFIG = {
    "name": "greeter",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hello!"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()

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
        yield TestClient(backend_main.app)
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _make_link():
    with open_test_db() as db:
        org_id = get_org_id()
        user = create_user(db, "owner", "pw", org_id=org_id)
        team = WorkflowRecord(name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status="deployed")
        db.add(team)
        db.commit()
        db.refresh(team)
        link = create_share_link(db, workflow_id=team.id, org_id=org_id, created_by=user.id)
        return link.token


def test_stream_rejects_missing_cookie(client):
    token = _make_link()
    run = runtime.registry.create("greeter", "hi", org_id=get_org_id(), username="share-link")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/share/{token}/stream/{run.id}") as ws:
            ws.receive_json()


def test_stream_delivers_events_for_the_sending_sessions_own_run(client):
    token = _make_link()
    sent = client.post(f"/api/share/{token}/messages", json={"content": "hi"})
    run_id = sent.json()["run_id"]

    with client.websocket_connect(f"/api/share/{token}/stream/{run_id}") as ws:
        event = ws.receive_json()
        assert event["type"] in ("run_queued", "run_started", "agent_started", "agent_completed", "run_completed")


def test_stream_rejects_a_run_id_belonging_to_another_session(client):
    token = _make_link()
    client.post(f"/api/share/{token}/messages", json={"content": "from A"})

    # `other` shares the same TestClient app (and thus the same dependency-
    # overridden in-memory DB) as `client`, but has its own cookie jar -- so
    # its POST creates a second, independent share session.
    other = TestClient(backend_main.app)
    sent_b = other.post(f"/api/share/{token}/messages", json={"content": "from B"})
    run_id_b = sent_b.json()["run_id"]

    # Client A's cookie jar doesn't carry B's session -- must be rejected.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/share/{token}/stream/{run_id_b}") as ws:
            ws.receive_json()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_chat_ws.py -v`
Expected: FAIL (WS route doesn't exist yet — connection refused/404)

- [ ] **Step 3: Write the implementation**

Append to `ui/backend/share_chat.py` (add these imports to the existing import block at the top: `WebSocket`, `WebSocketDisconnect` from `fastapi`; `Session` is already imported from `sqlalchemy.orm`):

```python
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
```

(replace the existing `from fastapi import ...` line with the one above), then append at the end of the file:

```python
def _link_and_org_active(engine, link_id: int) -> bool:
    """Fresh re-check used before delivering every WS event -- a revoke or
    org deactivation mid-stream must stop delivery immediately (mirrors
    main.py::stream_run's own per-event `_stream_access` re-check)."""
    with Session(engine) as check_db:
        link = check_db.get(ShareLink, link_id)
        if link is None or not link.active:
            return False
        org = check_db.get(Organization, link.org_id)
        return org is not None and org.active


@router.websocket("/{token}/stream/{run_id}")
async def stream_share_run(
    websocket: WebSocket, token: str, run_id: str, db: Session = Depends(get_db)
):
    """Replays a share-chat run's trace events for the visitor session that
    started it. Authenticated by the signed session cookie (`share_auth.py`)
    -- sent automatically on the WS handshake, so no ticket exchange is
    needed here (contrast `main.py::stream_run`'s `?ticket=`, which exists
    only to work around a WebSocket handshake not carrying an
    `Authorization` header)."""
    engine = db.get_bind()
    cookie_value = websocket.cookies.get(COOKIE_NAME)
    session_token = verify_cookie_value(cookie_value) if cookie_value else None
    link = get_share_link_by_token(db, token)
    session = get_share_session_by_token(db, session_token) if session_token else None
    run_row = db.get(Run, run_id)

    authorized = (
        link is not None
        and session is not None
        and session.share_link_id == link.id
        and run_row is not None
        and run_row.trigger_context is not None
        and run_row.trigger_context.get("share_session_id") == session.id
    )
    run = registry.get(run_id)
    if not authorized or run is None:
        db.close()
        await websocket.close(code=4404)
        return
    link_id = link.id
    db.close()

    await websocket.accept()
    subscriber_queue = registry.subscribe(run_id)
    if subscriber_queue is None:
        await websocket.close(code=4404)
        return
    try:
        while True:
            event = await subscriber_queue.get()
            if not _link_and_org_active(engine, link_id):
                await websocket.close(code=4404)
                return
            await websocket.send_json(event)
            if event["type"] in ("run_completed", "run_failed", "run_cancelled"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run_id, subscriber_queue)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_chat_ws.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/share_chat.py tests/test_share_chat_ws.py
git commit -m "feat(api): add cookie-authenticated WebSocket stream for share-chat runs"
```

---

## Task 10: CORS credentials + confirm router wiring

**Files:**
- Modify: `ui/backend/main.py:262-267` (CORS middleware)
- Test: `tests/test_share_chat_api.py` (append one test)

**Interfaces:**
- No new interfaces — this task only changes middleware config. `share_links_router`/`share_chat_router` are already imported/mounted (Tasks 6 and 8) — this task just verifies there's exactly one `include_router` call for each (no duplication was introduced) and adds `allow_credentials=True`.

- [ ] **Step 1: Verify no duplicate router registration**

Run: `grep -n "share_links_router\|share_chat_router" ui/backend/main.py`
Expected: each name appears exactly twice (one `from .X import router as Y`, one `app.include_router(Y)`). If either appears more than twice, remove the duplicate line before continuing.

- [ ] **Step 2: Write the failing test**

The share-session cookie is `httponly`/`samesite=lax`, which `TestClient` already handles transparently (same-process, no real cross-origin browser involved) — so this specific regression can't be caught by a backend-only test in the same way a browser would hit it. Instead, assert the middleware config directly:

Append to `tests/test_share_chat_api.py`:

```python
def test_cors_allows_credentials():
    from ui.backend.main import app

    cors_middleware = next(
        m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"
    )
    assert cors_middleware.kwargs.get("allow_credentials") is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_chat_api.py::test_cors_allows_credentials -v`
Expected: FAIL (`allow_credentials` not set / `None`)

- [ ] **Step 4: Make the change**

In `ui/backend/main.py`, the CORS block currently reads:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Change to:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

`allow_credentials=True` is safe here because `_cors_origins` is always an explicit list (never `"*"`, see `_default_cors_origins` above it) — Starlette only forbids the combination of `allow_credentials=True` with a wildcard origin.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_share_chat_api.py::test_cors_allows_credentials -v`
Expected: PASS

- [ ] **Step 6: Run the full backend test suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS (all tests, including every existing auth/CORS-adjacent test)

- [ ] **Step 7: Commit**

```bash
git add ui/backend/main.py tests/test_share_chat_api.py
git commit -m "fix(cors): allow credentials so the share-session cookie reaches the API from the frontend origin"
```

---

## Task 11: Block deleting a team with active share links

**Files:**
- Modify: `ui/backend/crud.py:41-51` (import), `:495-502` (delete guard)
- Test: `tests/test_crud_api.py` (append)

**Interfaces:**
- Consumes: `count_active_share_links` (Task 2).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_crud_api.py` (check the file's existing fixture name first — it's very likely `client`, matching every other test file read so far; reuse it):

```python
def test_delete_workflow_blocked_by_active_share_link(client):
    from helpers import get_org_id, open_test_db
    from ui.backend.db.models import WorkflowRecord
    from ui.backend.db.share_links import create_share_link
    from ui.backend.db.users import get_user_by_username

    org_id = get_org_id()
    config = {
        "name": "shared_team",
        "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
        "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["tm"]},
    }
    with open_test_db() as db:
        record = WorkflowRecord(name="shared_team", org_id=org_id, config=config, status="deployed")
        db.add(record)
        db.commit()
        db.refresh(record)
        user = get_user_by_username(db, "test")
        create_share_link(db, workflow_id=record.id, org_id=org_id, created_by=user.id)

    resp = client.delete("/api/config/workflows/shared_team?org=default")
    assert resp.status_code == 409
    assert "share" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_crud_api.py::test_delete_workflow_blocked_by_active_share_link -v`
Expected: FAIL (delete succeeds, no 409)

- [ ] **Step 3: Write the implementation**

In `ui/backend/crud.py`, add `ShareLink` to the existing `from .db.models import (...)` block (line 41-51), alphabetically after `SkillVersion`:

```python
from .db.models import (
    BuilderSession,
    KnowledgeBaseRecord,
    Organization,
    Run,
    ShareLink,
    SkillRecord,
    SkillVersion,
    User,
    WorkflowDependency,
    WorkflowRecord,
    WorkflowVersion,
```

Add the import for the CRUD helper near the top with the other same-directory imports:

```python
from .db.share_links import count_active_share_links
```

In `delete_workflow_config`, right after the existing `run_refs` guard (the block ending at `raise HTTPException(status_code=409, ...)` for run provenance, around line 502), add:

```python
        active_share_links = count_active_share_links(db, item.id)
        if active_share_links:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Can't delete '{item_name}': {active_share_links} active share "
                    "link(s) point at it. Revoke them first."
                ),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_crud_api.py::test_delete_workflow_blocked_by_active_share_link -v`
Expected: PASS

- [ ] **Step 5: Run the full crud test file to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ui/backend/crud.py tests/test_crud_api.py
git commit -m "fix(crud): block deleting a team with active share links"
```

---

## Task 12: Frontend types + org-side `api.ts` methods

**Files:**
- Modify: `ui/frontend/src/lib/types.ts` (append)
- Modify: `ui/frontend/src/lib/api.ts` (append methods)
- Test: `ui/frontend/src/lib/api.test.ts` (append)

**Interfaces:**
- Produces: TS types `ShareLink`, `ShareSessionSummary`, `ShareMessage`; `api.createShareLink`, `api.listShareLinks`, `api.patchShareLink`, `api.listShareSessions`, `api.getShareSessionMessages`.

- [ ] **Step 1: Write the failing test**

Check `ui/frontend/src/lib/api.test.ts`'s existing style first (it tests the `request` wrapper against a mocked `fetch`), then append a test in the same style:

```typescript
it('createShareLink posts to the workflow share-links endpoint', async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 201,
    json: async () => ({ id: 1, workflow_id: 5, token: 'tok', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' }),
  })
  vi.stubGlobal('fetch', fetchMock)

  const result = await api.createShareLink(5, { daily_cap: 30 })

  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/api/workflows/5/share-links'),
    expect.objectContaining({ method: 'POST' }),
  )
  expect(result.token).toBe('tok')
})
```

(Adapt the exact mocking idiom to whatever `api.test.ts` already uses for `fetch` — read the file's first 30 lines before writing this step for real, to match its conventions exactly rather than guessing.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npm test -- api.test.ts`
Expected: FAIL (`api.createShareLink is not a function`)

- [ ] **Step 3: Add the types**

Append to `ui/frontend/src/lib/types.ts`:

```typescript
export interface ShareLink {
  id: number
  workflow_id: number
  token: string
  active: boolean
  daily_cap: number
  expires_at: string | null
  created_at: string
}

export interface ShareSessionSummary {
  id: number
  created_at: string
  last_active_at: string
  turns_today: number
}

export interface ShareMessage {
  role: 'user' | 'assistant'
  content: string
  turn_number: number
  created_at?: string
}
```

- [ ] **Step 4: Add the api.ts methods**

Add to `ui/frontend/src/lib/api.ts`'s `import type { ... } from './types'` line: `ShareLink, ShareMessage, ShareSessionSummary,`.

Append to the `api` object (near the other org-scoped methods):

```typescript
  // Org self-service: share a deployed team with colleagues via a
  // revocable, anonymous link (see docs/superpowers/specs/
  // 2026-08-14-team-sharing-continuous-chat-design.md).
  createShareLink: (workflowId: number, payload: { daily_cap?: number; expires_at?: string | null }) =>
    request<ShareLink>(`/api/workflows/${workflowId}/share-links`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listShareLinks: (workflowId: number) =>
    request<ShareLink[]>(`/api/workflows/${workflowId}/share-links`),
  patchShareLink: (
    linkId: number,
    payload: { active?: boolean; daily_cap?: number; expires_at?: string | null; clear_expiry?: boolean },
  ) =>
    request<ShareLink>(`/api/share-links/${linkId}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  listShareSessions: (linkId: number) =>
    request<ShareSessionSummary[]>(`/api/share-links/${linkId}/sessions`),
  getShareSessionMessages: (linkId: number, sessionId: number) =>
    request<ShareMessage[]>(`/api/share-links/${linkId}/sessions/${sessionId}/messages`),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ui/frontend && npm test -- api.test.ts`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/lib/types.ts ui/frontend/src/lib/api.ts ui/frontend/src/lib/api.test.ts
git commit -m "feat(frontend): add org-side share-link API client methods"
```

---

## Task 13: Public share-chat API client + friendly status phrases

**Files:**
- Create: `ui/frontend/src/lib/shareChatApi.ts`
- Create: `ui/frontend/src/lib/shareTraceEvents.ts`
- Test: `ui/frontend/src/lib/shareTraceEvents.test.ts`

**Interfaces:**
- Consumes: `API_BASE`, `WS_BASE` (`ui/frontend/src/lib/api.ts`); `TraceEvent`, `ShareMessage` (Task 12).
- Produces: `shareChatApi.sendMessage(token, content)`, `shareChatApi.getMessages(token)`, `shareChatApi.streamUrl(token, runId)`; `friendlyStatusFor(events: TraceEvent[]): string`.

- [ ] **Step 1: Write the failing test**

Create `ui/frontend/src/lib/shareTraceEvents.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { friendlyStatusFor } from './shareTraceEvents'
import type { TraceEvent } from './types'

describe('friendlyStatusFor', () => {
  it('returns a generic starting phrase with no events yet', () => {
    expect(friendlyStatusFor([])).toBe('Sending your message…')
  })

  it('maps the most recent known event type to a friendly phrase', () => {
    const events: TraceEvent[] = [
      { type: 'run_started', agent: undefined, data: null },
      { type: 'tool_started', agent: 'a', data: { tool: 'web_search' } },
    ]
    expect(friendlyStatusFor(events)).toBe('Working on your question…')
  })

  it('never leaks a raw tool or agent name into the phrase', () => {
    const events: TraceEvent[] = [
      { type: 'tool_started', agent: 'a', data: { tool: 'email_find' } },
    ]
    expect(friendlyStatusFor(events)).not.toMatch(/email_find/)
  })

  it('falls back to a generic phrase for an unmapped event type', () => {
    const events: TraceEvent[] = [{ type: 'some_future_event', agent: undefined, data: null }]
    expect(friendlyStatusFor(events)).toBe('Working on it…')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npm test -- shareTraceEvents.test.ts`
Expected: FAIL with `ModuleNotFoundError`/`Cannot find module './shareTraceEvents'`

- [ ] **Step 3: Write the implementation**

Create `ui/frontend/src/lib/shareTraceEvents.ts`:

```typescript
import type { TraceEvent } from './types'

// A visitor chat page shows a short, non-technical progress line instead of
// the raw trace `lib/traceEvents.ts` renders for the logged-in Activity
// page -- deliberately generic (never a raw tool/agent name), since a
// colleague using a shared link shouldn't see the team's internal wiring.
const FRIENDLY_STATUS: Record<string, string> = {
  run_queued: 'Sending your message…',
  run_started: 'Getting started…',
  agent_started: 'Working on your question…',
  agent_progress: 'Working on your question…',
  tool_started: 'Working on your question…',
  tool_completed: 'Working on your question…',
  delegation_started: 'Checking with the team…',
  subagent_started: 'Checking with the team…',
  subagent_completed: 'Checking with the team…',
  delegation_completed: 'Putting together a reply…',
  agent_completed: 'Putting together a reply…',
}

const DEFAULT_STATUS = 'Working on it…'
const INITIAL_STATUS = 'Sending your message…'

export function friendlyStatusFor(events: TraceEvent[]): string {
  if (events.length === 0) return INITIAL_STATUS
  const last = events[events.length - 1]
  return FRIENDLY_STATUS[last.type] ?? DEFAULT_STATUS
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npm test -- shareTraceEvents.test.ts`
Expected: PASS

- [ ] **Step 5: Write `shareChatApi.ts`** (no test — a thin `fetch` wrapper mirroring `api.ts`'s `request`, exercised end-to-end by Task 14's `ShareChatPage` tests)

Create `ui/frontend/src/lib/shareChatApi.ts`:

```typescript
import { API_BASE, WS_BASE } from './api'
import type { ShareMessage } from './types'

// The public, anonymous counterpart to lib/api.ts's authenticated `request`.
// No bearer token: the visitor's identity is a signed session cookie the
// backend sets via Set-Cookie on the first message (share_chat.py), so every
// call here must send credentials -- unlike api.ts's `request`, which never
// needs cookies at all.

interface ApiError extends Error {
  status?: number
}

async function shareRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers as Record<string, string> | undefined) },
    ...options,
  })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      // no JSON body
    }
    const error: ApiError = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    error.status = res.status
    throw error
  }
  return res.json()
}

export const shareChatApi = {
  sendMessage: (token: string, content: string) =>
    shareRequest<{ run_id: string; turn_number: number }>(`/api/share/${encodeURIComponent(token)}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  getMessages: (token: string) =>
    shareRequest<{ messages: ShareMessage[] }>(`/api/share/${encodeURIComponent(token)}/messages`),
  streamUrl: (token: string, runId: string) =>
    `${WS_BASE}/api/share/${encodeURIComponent(token)}/stream/${encodeURIComponent(runId)}`,
}
```

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/lib/shareTraceEvents.ts ui/frontend/src/lib/shareTraceEvents.test.ts ui/frontend/src/lib/shareChatApi.ts
git commit -m "feat(frontend): add public share-chat API client and friendly status phrases"
```

---

## Task 14: Visitor chat page (`/share/:token`)

**Files:**
- Create: `ui/frontend/src/pages/ShareChatPage.tsx`
- Create: `ui/frontend/src/pages/ShareChatPage.css`
- Modify: `ui/frontend/src/App.tsx` (add public route)
- Test: `ui/frontend/src/pages/ShareChatPage.test.tsx`

**Interfaces:**
- Consumes: `shareChatApi` (Task 13), `friendlyStatusFor` (Task 13), `ShareMessage`/`TraceEvent` (Task 12).

- [ ] **Step 1: Write the failing test**

Create `ui/frontend/src/pages/ShareChatPage.test.tsx` (mirror `RunDetail.test.tsx`'s `FakeWebSocket` pattern from Task 13's research):

```typescript
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import ShareChatPage from './ShareChatPage'
import { shareChatApi } from '../lib/shareChatApi'

vi.mock('../lib/shareChatApi', () => ({
  shareChatApi: {
    getMessages: vi.fn(),
    sendMessage: vi.fn(),
    streamUrl: vi.fn(() => 'ws://127.0.0.1:8000/api/share/tok/stream/run-1'),
  },
}))

const mockedApi = vi.mocked(shareChatApi)

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onmessage?: (event: { data: string }) => void
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  close() {}
  emit(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/share/tok']}>
      <Routes>
        <Route path="/share/:token" element={<ShareChatPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ShareChatPage', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket
    mockedApi.getMessages.mockResolvedValue({ messages: [] })
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('loads existing history on mount', async () => {
    mockedApi.getMessages.mockResolvedValue({
      messages: [{ role: 'user', content: 'hi', turn_number: 1 }, { role: 'assistant', content: 'hello!', turn_number: 2 }],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('hello!')).toBeInTheDocument())
  })

  it('sends a message, shows a friendly status, then the reply', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalledWith('tok', 'hi there'))
    expect(screen.getByText('hi there')).toBeInTheDocument()
    expect(await screen.findByText(/sending your message|working on/i)).toBeInTheDocument()

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.emit({ type: 'run_completed', agent: null, data: 'General Kenobi!', usage: [] })
    })
    expect(await screen.findByText('General Kenobi!')).toBeInTheDocument()
  })

  it('shows a friendly message when the link is unavailable', async () => {
    mockedApi.getMessages.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    renderPage()
    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npm test -- ShareChatPage.test.tsx`
Expected: FAIL with `Cannot find module './ShareChatPage'`

- [ ] **Step 3: Write the implementation**

Create `ui/frontend/src/pages/ShareChatPage.css`:

```css
.share-chat {
  max-width: 640px;
  margin: 0 auto;
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  height: 100vh;
  box-sizing: border-box;
}

.share-chat-messages {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding-bottom: 1rem;
}

.share-chat-bubble {
  padding: 0.6rem 0.9rem;
  border-radius: 0.9rem;
  max-width: 80%;
  white-space: pre-wrap;
}

.share-chat-bubble.user {
  align-self: flex-end;
  background: #2563eb;
  color: white;
}

.share-chat-bubble.assistant {
  align-self: flex-start;
  background: #f1f5f9;
  color: #0f172a;
}

.share-chat-bubble.status {
  align-self: flex-start;
  font-style: italic;
  color: #64748b;
  background: transparent;
}

.share-chat-form {
  display: flex;
  gap: 0.5rem;
  padding-top: 0.75rem;
  border-top: 1px solid #e2e8f0;
}

.share-chat-form input {
  flex: 1;
  padding: 0.6rem 0.8rem;
  border-radius: 0.6rem;
  border: 1px solid #cbd5e1;
}

.share-chat-unavailable {
  margin: auto;
  text-align: center;
  color: #64748b;
}
```

Create `ui/frontend/src/pages/ShareChatPage.tsx`:

```typescript
import { FormEvent, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { shareChatApi } from '../lib/shareChatApi'
import { friendlyStatusFor } from '../lib/shareTraceEvents'
import type { ShareMessage, TraceEvent } from '../lib/types'
import './ShareChatPage.css'

const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled']

// The public, anonymous, multi-turn counterpart to MonitorPage's one-shot
// "Run a team" -- a colleague reaches this page via a link an org member
// generated (ShareLinksPanel), never logs in, and gets a real back-and-forth
// conversation. See docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md.
export default function ShareChatPage() {
  const { token = '' } = useParams<{ token: string }>()
  const [messages, setMessages] = useState<ShareMessage[]>([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [liveEvents, setLiveEvents] = useState<TraceEvent[]>([])
  const [unavailable, setUnavailable] = useState<string | null>(null)
  const [rateLimited, setRateLimited] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    shareChatApi
      .getMessages(token)
      .then((data) => setMessages(data.messages))
      .catch((e: Error & { status?: number }) => {
        setUnavailable(
          e.status === 404
            ? 'This share link is no longer available.'
            : "Couldn't load this conversation.",
        )
      })
  }, [token])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, liveEvents])

  useEffect(() => {
    return () => {
      wsRef.current?.close()
    }
  }, [])

  const handleSend = async (event: FormEvent) => {
    event.preventDefault()
    const content = draft.trim()
    if (!content || sending) return

    setSending(true)
    setRateLimited(false)
    setDraft('')
    setMessages((prev) => [...prev, { role: 'user', content, turn_number: prev.length + 1 }])
    setLiveEvents([])

    try {
      const { run_id: runId } = await shareChatApi.sendMessage(token, content)
      const ws = new WebSocket(shareChatApi.streamUrl(token, runId))
      wsRef.current = ws
      ws.onmessage = (msg: MessageEvent<string>) => {
        const traceEvent = JSON.parse(msg.data) as TraceEvent
        setLiveEvents((prev) => [...prev, traceEvent])
        if (traceEvent.type === 'run_completed') {
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: String(traceEvent.data ?? ''), turn_number: prev.length + 1 },
          ])
          setSending(false)
        } else if (TERMINAL_TYPES.includes(traceEvent.type)) {
          setSending(false)
        }
      }
      ws.onerror = () => setSending(false)
    } catch (e) {
      const status = (e as Error & { status?: number }).status
      setSending(false)
      if (status === 429) {
        setRateLimited(true)
      } else if (status === 404) {
        setUnavailable('This share link is no longer available.')
      }
    }
  }

  if (unavailable) {
    return (
      <div className="share-chat">
        <p className="share-chat-unavailable">{unavailable}</p>
      </div>
    )
  }

  return (
    <div className="share-chat">
      <div className="share-chat-messages">
        {messages.map((m, i) => (
          <div key={i} className={`share-chat-bubble ${m.role}`}>
            {m.content}
          </div>
        ))}
        {sending && <div className="share-chat-bubble status">{friendlyStatusFor(liveEvents)}</div>}
        <div ref={messagesEndRef} />
      </div>
      {rateLimited && (
        <p className="share-chat-bubble status">Today's message limit has been reached — try again tomorrow.</p>
      )}
      <form className="share-chat-form" onSubmit={handleSend}>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Type a message…"
          disabled={sending || rateLimited}
        />
        <button type="submit" disabled={sending || rateLimited || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  )
}
```

- [ ] **Step 4: Add the public route**

In `ui/frontend/src/App.tsx`, add the import:

```typescript
import ShareChatPage from './pages/ShareChatPage'
```

Add the route **outside** every auth guard (it must be reachable with no token) — right after the `/login` route:

```typescript
      <Route path="/login" element={<LoginPage />} />
      <Route path="/share/:token" element={<ShareChatPage />} />
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ui/frontend && npm test -- ShareChatPage.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/pages/ShareChatPage.tsx ui/frontend/src/pages/ShareChatPage.css ui/frontend/src/pages/ShareChatPage.test.tsx ui/frontend/src/App.tsx
git commit -m "feat(frontend): add the anonymous visitor share-chat page"
```

---

## Task 15: Org-side "Share" panel on My teams

**Files:**
- Create: `ui/frontend/src/components/ShareLinksPanel.tsx`
- Modify: `ui/frontend/src/pages/wizard/SessionsPage.tsx:130-147` (render the panel per deployed team card)
- Test: `ui/frontend/src/components/ShareLinksPanel.test.tsx`

**Interfaces:**
- Consumes: `api.createShareLink`, `api.listShareLinks`, `api.patchShareLink` (Task 12).
- Produces: `<ShareLinksPanel workflowId={number} />`.

- [ ] **Step 1: Write the failing test**

Create `ui/frontend/src/components/ShareLinksPanel.test.tsx`:

```typescript
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import ShareLinksPanel from './ShareLinksPanel'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listShareLinks: vi.fn(),
    createShareLink: vi.fn(),
    patchShareLink: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

describe('ShareLinksPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listShareLinks.mockResolvedValue([])
  })

  it('lists existing links and shows their status', async () => {
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, workflow_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    render(<ShareLinksPanel workflowId={5} />)
    await waitFor(() => expect(screen.getByText(/active/i)).toBeInTheDocument())
  })

  it('creates a new link on click', async () => {
    mockedApi.createShareLink.mockResolvedValue({
      id: 2, workflow_id: 5, token: 'newtoken', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel workflowId={5} />)
    fireEvent.click(await screen.findByRole('button', { name: /generate/i }))
    await waitFor(() => expect(mockedApi.createShareLink).toHaveBeenCalledWith(5, expect.any(Object)))
  })

  it('revokes a link on click', async () => {
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, workflow_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    mockedApi.patchShareLink.mockResolvedValue({
      id: 1, workflow_id: 5, token: 'abc123token', active: false, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel workflowId={5} />)
    fireEvent.click(await screen.findByRole('button', { name: /revoke/i }))
    await waitFor(() => expect(mockedApi.patchShareLink).toHaveBeenCalledWith(1, { active: false }))
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npm test -- ShareLinksPanel.test.tsx`
Expected: FAIL with `Cannot find module './ShareLinksPanel'`

- [ ] **Step 3: Write the implementation**

Create `ui/frontend/src/components/ShareLinksPanel.tsx`:

```typescript
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ShareLink } from '../lib/types'

interface ShareLinksPanelProps {
  workflowId: number
}

function shareUrlFor(token: string): string {
  return `${window.location.origin}/share/${token}`
}

// Lets the org's one user generate/revoke anonymous, continuous-chat links
// for a deployed team (see docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md). Rendered inline on
// each deployed team's card in "My teams" (SessionsPage.tsx).
export default function ShareLinksPanel({ workflowId }: ShareLinksPanelProps) {
  const [links, setLinks] = useState<ShareLink[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<number | null>(null)

  const refresh = () => {
    api
      .listShareLinks(workflowId)
      .then(setLinks)
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    if (open) refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const handleCreate = async () => {
    try {
      await api.createShareLink(workflowId, {})
      refresh()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleRevoke = async (linkId: number) => {
    try {
      await api.patchShareLink(linkId, { active: false })
      refresh()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleCopy = async (link: ShareLink) => {
    await navigator.clipboard.writeText(shareUrlFor(link.token))
    setCopiedId(link.id)
    setTimeout(() => setCopiedId(null), 2000)
  }

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}>
        Share
      </button>
    )
  }

  return (
    <div className="share-links-panel" onClick={(e) => e.stopPropagation()}>
      {error && <p className="banner banner-error">{error}</p>}
      <button type="button" onClick={handleCreate}>
        Generate a new link
      </button>
      <ul>
        {links.map((link) => (
          <li key={link.id}>
            <span>{link.active ? 'Active' : 'Revoked'}</span>
            {link.active && (
              <>
                <button type="button" onClick={() => handleCopy(link)}>
                  {copiedId === link.id ? 'Copied!' : 'Copy link'}
                </button>
                <button type="button" onClick={() => handleRevoke(link.id)}>
                  Revoke
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
      <button type="button" onClick={() => setOpen(false)}>
        Close
      </button>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npm test -- ShareLinksPanel.test.tsx`
Expected: PASS

- [ ] **Step 5: Wire it into SessionsPage.tsx**

In `ui/frontend/src/pages/wizard/SessionsPage.tsx`, add the import:

```typescript
import ShareLinksPanel from '../../components/ShareLinksPanel'
```

`session.workflow_id` is exactly the `WorkflowRecord.id` the share-link API needs — it's already serialized by `GET /api/builder/sessions` (`builder.py:111`, `"workflow_id": session.workflow_id`) and already typed on `BuilderSession` (`lib/types.ts:61`, `workflow_id?: number | null`), so no backend or type change is needed for this step. Render the panel only for a deployed session with a resolved workflow id, replacing this block (lines 135-147):

```typescript
                return (
                  <li key={session.id ?? `workflow:${teamName}`} className="session-item">
                    <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                      <h3>{displayName ?? session.intent_text}</h3>
                      <p className="subtitle">{descriptionFor(session)}</p>
                      {isAutomated && (
                        <p className="hint automation-tag">
                          {AUTOMATION_STATUS_LABELS[trigger.status] ?? trigger.status}
                        </p>
                      )}
                      <div className="session-card-footer">
                        <span className="session-updated">Updated {formatDateTime(session.updated_at)}</span>
                      </div>
                    </button>
```

with:

```typescript
                return (
                  <li key={session.id ?? `workflow:${teamName}`} className="session-item">
                    <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                      <h3>{displayName ?? session.intent_text}</h3>
                      <p className="subtitle">{descriptionFor(session)}</p>
                      {isAutomated && (
                        <p className="hint automation-tag">
                          {AUTOMATION_STATUS_LABELS[trigger.status] ?? trigger.status}
                        </p>
                      )}
                      <div className="session-card-footer">
                        <span className="session-updated">Updated {formatDateTime(session.updated_at)}</span>
                      </div>
                    </button>
                    {session.status === 'deployed' && session.workflow_id != null && (
                      <ShareLinksPanel workflowId={session.workflow_id} />
                    )}
```

- [ ] **Step 6: Run the SessionsPage test suite to check for regressions**

Run: `cd ui/frontend && npm test -- SessionsPage.test.tsx`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add ui/frontend/src/components/ShareLinksPanel.tsx ui/frontend/src/components/ShareLinksPanel.test.tsx ui/frontend/src/pages/wizard/SessionsPage.tsx
git commit -m "feat(frontend): add Share panel to My teams for generating/revoking links"
```

---

## Task 16: Org-side "Shared sessions" audit tab on Activity

**Files:**
- Create: `ui/frontend/src/components/SharedSessionsPanel.tsx`
- Modify: `ui/frontend/src/pages/ActivityPage.tsx:43` (tab union type), `:128-139` (tab buttons), `:173` (render block boundary)
- Test: `ui/frontend/src/components/SharedSessionsPanel.test.tsx`

**Interfaces:**
- Consumes: `api.listShareLinks`, `api.listShareSessions`, `api.getShareSessionMessages` (Task 12). Needs a workflow list to pick a team to inspect — reuse `api.listWorkflows()` (already used elsewhere in `ActivityPage.tsx`), but that returns names, not ids; the share-link endpoints are keyed by `WorkflowRecord.id`. For this task, scope the panel per already-known `workflowId` the same way `ShareLinksPanel` does, driven from `SessionsPage`'s existing per-team data rather than re-deriving ids from names in `ActivityPage` — i.e. render `SharedSessionsPanel` from a `?workflow=<id>` link `ShareLinksPanel` grows (Step 3 below), not from an independent workflow picker.

- [ ] **Step 1: Write the failing test**

Create `ui/frontend/src/components/SharedSessionsPanel.test.tsx`:

```typescript
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import SharedSessionsPanel from './SharedSessionsPanel'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listShareLinks: vi.fn(),
    listShareSessions: vi.fn(),
    getShareSessionMessages: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

describe('SharedSessionsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, workflow_id: 5, token: 'tok', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    mockedApi.listShareSessions.mockResolvedValue([
      { id: 9, created_at: '2026-08-14T00:00:00+00:00', last_active_at: '2026-08-14T01:00:00+00:00', turns_today: 3 },
    ])
  })

  it('lists sessions for a share link', async () => {
    render(<SharedSessionsPanel workflowId={5} />)
    await waitFor(() => expect(screen.getByText(/3/)).toBeInTheDocument())
  })

  it('shows a session transcript on click', async () => {
    mockedApi.getShareSessionMessages.mockResolvedValue([
      { role: 'user', content: 'hi', turn_number: 1 },
      { role: 'assistant', content: 'hello!', turn_number: 2 },
    ])
    render(<SharedSessionsPanel workflowId={5} />)
    fireEvent.click(await screen.findByText(/view/i))
    await waitFor(() => expect(screen.getByText('hello!')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npm test -- SharedSessionsPanel.test.tsx`
Expected: FAIL with `Cannot find module './SharedSessionsPanel'`

- [ ] **Step 3: Write the implementation**

Create `ui/frontend/src/components/SharedSessionsPanel.tsx`:

```typescript
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { ShareLink, ShareMessage, ShareSessionSummary } from '../lib/types'

interface SharedSessionsPanelProps {
  workflowId: number
}

// Read-only audit view: every anonymous visitor session against every share
// link for one team, with a drill-in transcript. See docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md ("Frontend").
export default function SharedSessionsPanel({ workflowId }: SharedSessionsPanelProps) {
  const [links, setLinks] = useState<ShareLink[]>([])
  const [sessionsByLink, setSessionsByLink] = useState<Record<number, ShareSessionSummary[]>>({})
  const [transcript, setTranscript] = useState<{ linkId: number; sessionId: number; messages: ShareMessage[] } | null>(
    null,
  )
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .listShareLinks(workflowId)
      .then(async (fetchedLinks) => {
        setLinks(fetchedLinks)
        const entries = await Promise.all(
          fetchedLinks.map(async (link) => [link.id, await api.listShareSessions(link.id)] as const),
        )
        setSessionsByLink(Object.fromEntries(entries))
      })
      .catch((e: Error) => setError(e.message))
  }, [workflowId])

  const openTranscript = async (linkId: number, sessionId: number) => {
    try {
      const messages = await api.getShareSessionMessages(linkId, sessionId)
      setTranscript({ linkId, sessionId, messages })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (transcript) {
    return (
      <div className="shared-sessions-transcript">
        <button type="button" onClick={() => setTranscript(null)}>
          Back
        </button>
        <ul>
          {transcript.messages.map((m, i) => (
            <li key={i} className={`share-chat-bubble ${m.role}`}>
              {m.content}
            </li>
          ))}
        </ul>
      </div>
    )
  }

  return (
    <div className="shared-sessions-panel">
      {error && <p className="banner banner-error">{error}</p>}
      {links.length === 0 && <p className="hint">No share links for this team yet.</p>}
      {links.map((link) => (
        <div key={link.id}>
          <h4>{link.active ? 'Active link' : 'Revoked link'}</h4>
          <ul>
            {(sessionsByLink[link.id] ?? []).map((session) => (
              <li key={session.id}>
                <span>Last active {formatDateTime(session.last_active_at)}</span>
                <span>{session.turns_today} turns today</span>
                <button type="button" onClick={() => openTranscript(link.id, session.id)}>
                  View transcript
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npm test -- SharedSessionsPanel.test.tsx`
Expected: PASS

- [ ] **Step 5: Expose workflow ids from `GET /api/workflows`**

`ActivityPage.tsx` only has workflow *names* (`GET /api/workflows` returns `{"workflows": string[]}`), but the share-link endpoints are keyed by `WorkflowRecord.id`. Extend the response additively — a new `workflow_ids` field alongside the existing `workflows: string[]` — so every existing consumer of `workflows: string[]` is unchanged.

In `ui/backend/main.py::list_workflows` (around line 512-536), change the return statement from:

```python
    return {"workflows": sorted(db_names | yaml_names)}
```

to:

```python
    id_by_name = {
        row.name: row.id
        for row in db.query(WorkflowRecord.name, WorkflowRecord.id).filter(
            WorkflowRecord.org_id == org.id,
            WorkflowRecord.status == "deployed",
            or_(WorkflowRecord.created_by == user.principal_id, WorkflowRecord.created_by.is_(None)),
        )
    }
    return {"workflows": sorted(db_names | yaml_names), "workflow_ids": id_by_name}
```

In `ui/frontend/src/lib/api.ts`, change `listWorkflows`'s return type from `request<{ workflows: string[] }>('/api/workflows')` to `request<{ workflows: string[]; workflow_ids?: Record<string, number> }>('/api/workflows')`.

- [ ] **Step 6: Add a backend regression test for `workflow_ids`**

Append to `tests/test_crud_api.py` (or the relevant existing workflows-list test file — check `tests/test_org_isolation.py`/wherever `GET /api/workflows` is already tested and add there instead if a more specific file exists):

```python
def test_list_workflows_includes_workflow_ids(client):
    from helpers import get_org_id, open_test_db
    from ui.backend.db.models import WorkflowRecord

    with open_test_db() as db:
        record = WorkflowRecord(
            name="idtest", org_id=get_org_id(),
            config={"name": "idtest", "agents": [], "teams": [], "workflow": {"steps": []}},
            status="deployed",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        expected_id = record.id

    resp = client.get("/api/workflows")
    assert resp.status_code == 200
    body = resp.json()
    assert "idtest" in body["workflows"]
    assert body["workflow_ids"]["idtest"] == expected_id
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_crud_api.py::test_list_workflows_includes_workflow_ids -v`
Expected: PASS

- [ ] **Step 7: Wire the tab into ActivityPage.tsx**

Add the import:

```typescript
import SharedSessionsPanel from '../components/SharedSessionsPanel'
```

Change the tab state type (line 43) from:

```typescript
  const [tab, setTab] = useState<'automations' | 'runs'>('automations') // automations | runs
```

to:

```typescript
  const [tab, setTab] = useState<'automations' | 'runs' | 'shared'>('automations') // automations | runs | shared
```

Add two more `useState` declarations next to the existing ones — the panel needs a concrete workflow id, and `workflowIds` is the name→id map Step 5 added to `GET /api/workflows`'s response:

```typescript
  const [sharedWorkflowId, setSharedWorkflowId] = useState<number | null>(null)
  const [workflowIds, setWorkflowIds] = useState<Record<string, number>>({})
```

Extend the existing workflow-list fetch (lines 69-74) to also capture `workflow_ids`:

```typescript
  useEffect(() => {
    api
      .listWorkflows()
      .then((d) => {
        setWorkflows(d.workflows)
        setWorkflowIds(d.workflow_ids ?? {})
      })
      .catch(() => {})
  }, [])
```

Replace the tab-button block (lines 128-139):

```typescript
      <div className="activity-tabs">
        <button
          type="button"
          className={tab === 'automations' ? 'active' : ''}
          onClick={() => setTab('automations')}
        >
          Automations
        </button>
        <button type="button" className={tab === 'runs' ? 'active' : ''} onClick={() => setTab('runs')}>
          Runs
        </button>
      </div>
```

with:

```typescript
      <div className="activity-tabs">
        <button
          type="button"
          className={tab === 'automations' ? 'active' : ''}
          onClick={() => setTab('automations')}
        >
          Automations
        </button>
        <button type="button" className={tab === 'runs' ? 'active' : ''} onClick={() => setTab('runs')}>
          Runs
        </button>
        <button type="button" className={tab === 'shared' ? 'active' : ''} onClick={() => setTab('shared')}>
          Shared
        </button>
      </div>
```

Add the render block right before the existing `{tab === 'runs' && (` block (before line 173):

```typescript
      {tab === 'shared' && (
        <section className="activity-shared">
          <label>
            Team
            <select
              value={sharedWorkflowId ?? ''}
              onChange={(e) => setSharedWorkflowId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Pick a team…</option>
              {workflows.map((name) => (
                <option key={name} value={workflowIds[name] ?? ''}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          {sharedWorkflowId != null && <SharedSessionsPanel workflowId={sharedWorkflowId} />}
        </section>
      )}

```

- [ ] **Step 8: Run the full frontend and backend test suites**

Run: `cd ui/frontend && npm test` and `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS (no regressions)

- [ ] **Step 9: Commit**

```bash
git add ui/frontend/src/components/SharedSessionsPanel.tsx ui/frontend/src/components/SharedSessionsPanel.test.tsx ui/frontend/src/pages/ActivityPage.tsx ui/frontend/src/lib/api.ts ui/backend/main.py tests/test_crud_api.py
git commit -m "feat(frontend): add Shared audit tab on Activity, expose workflow ids from GET /api/workflows"
```
