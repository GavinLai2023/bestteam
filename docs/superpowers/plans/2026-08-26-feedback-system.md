# User Feedback System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let logged-in users and share-link visitors submit defect/suggestion feedback, collected on a fifth admin-only page for manual triage.

**Architecture:** One `feedback` table with helpers in `db/feedback.py`; a new `feedback_api.py` router (authed submit + admin list/patch) plus one anonymous route in `share_chat.py` reusing its link/session/cookie helpers; a shared `FeedbackModal` React component mounted from `Layout` (logged-in) and `ShareChatPage` (visitor); a new admin `FeedbackPage`.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + Alembic (guarded migrations), React + i18next + vitest.

**Spec:** `docs/superpowers/specs/2026-08-26-feedback-system-design.md`

## Global Constraints

- Body cap 4000 chars; kind ∈ {`defect`, `suggestion`}; status ∈ {`new`, `acknowledged`, `resolved`, `dismissed`}.
- Share-side cap: 5 feedback rows per session per UTC day (module constant `FEEDBACK_DAILY_CAP` in `share_chat.py`); over → 429.
- Context whitelist: `page`, `locale` (authed); + `run_id` (share). Values truncated to 200 chars.
- Platform-admin gate = `get_current_admin` (existing dependency). Authed submit gate = `get_current_user`.
- Alembic revision `u8v9w0x1y2z3`, down_revision `t7u8v9w0x1y2`, guarded by inspection like neighbours.
- English UI strings use British spelling; every `en.ts` key needs a `zh-CN.ts` translation (compile error otherwise). Admin page *content* uses English literals (MemoryPage convention); nav labels go through i18n.
- Code comments in English. Every new test file carries a `pytestmark` marker.
- Run backend tests with `C:\Projects\MyBestTeam\.venv\Scripts\python.exe -m pytest` from the worktree root; frontend with `npm test` in `ui/frontend` (after `npm install` in the worktree).

---

### Task 1: `feedback` table, DB helpers, migration

**Files:**
- Modify: `ui/backend/db/models.py` (append after `OrgNotificationSetting`)
- Create: `ui/backend/db/feedback.py`
- Create: `alembic/versions/u8v9w0x1y2z3_add_feedback_table.py`
- Test: `tests/test_feedback_db.py`

**Interfaces:**
- Produces: `Feedback` model; `create_feedback(db, *, kind, body, org_id=None, submitted_by=None, share_session_id=None, context=None) -> Feedback` (flushes, caller commits); `count_session_feedback_today(db, share_session_id) -> int`; `list_feedback(db, *, status=None, kind=None, org_id=None, limit=100, offset=0) -> List[Feedback]`; `get_feedback(db, feedback_id) -> Optional[Feedback]`; `update_feedback(db, row, *, status=None, admin_note=None) -> Feedback`; constants `KINDS`, `STATUSES`.

- [ ] **Step 1: failing tests** — `tests/test_feedback_db.py`:

```python
"""DB helpers for the feedback table (db/feedback.py)."""

import pytest

pytest.importorskip("sqlalchemy")

pytestmark = pytest.mark.unit

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.feedback import (
    count_session_feedback_today, create_feedback, get_feedback,
    list_feedback, update_feedback,
)
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.share_links import create_share_link
from ui.backend.db.share_sessions import create_share_session
from ui.backend.db.users import create_user
from ui.backend.db.models import PipelineRecord


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    SessionLocal = session_factory(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org_user(db):
    org = get_or_create_org(db, "acme")
    user = create_user(db, "alice", "pw", org_id=org.id)
    return org, user


def test_create_and_list_newest_first(db):
    org, user = _org_user(db)
    first = create_feedback(db, kind="defect", body="b1", org_id=org.id, submitted_by=user.id)
    second = create_feedback(db, kind="suggestion", body="b2", org_id=org.id, submitted_by=user.id)
    db.commit()
    rows = list_feedback(db)
    assert [r.id for r in rows] == [second.id, first.id]
    assert rows[0].status == "new"


def test_filters(db):
    org, user = _org_user(db)
    kept = create_feedback(db, kind="defect", body="x", org_id=org.id, submitted_by=user.id)
    create_feedback(db, kind="suggestion", body="y", org_id=org.id, submitted_by=user.id)
    db.commit()
    assert [r.id for r in list_feedback(db, kind="defect")] == [kept.id]
    assert list_feedback(db, status="resolved") == []
    assert [r.id for r in list_feedback(db, status="new", org_id=org.id, kind="defect")] == [kept.id]


def test_update_status_and_note(db):
    org, user = _org_user(db)
    row = create_feedback(db, kind="defect", body="x", org_id=org.id, submitted_by=user.id)
    db.commit()
    update_feedback(db, row, status="acknowledged", admin_note="looking")
    db.commit()
    got = get_feedback(db, row.id)
    assert got.status == "acknowledged"
    assert got.admin_note == "looking"


def test_session_daily_count(db):
    org, user = _org_user(db)
    team = PipelineRecord(name="t", org_id=org.id, config={}, status="deployed")
    db.add(team)
    db.commit()
    link = create_share_link(db, pipeline_id=team.id, org_id=org.id, created_by=user.id)
    session = create_share_session(db, link.id)
    assert count_session_feedback_today(db, session.id) == 0
    create_feedback(db, kind="defect", body="x", org_id=org.id, share_session_id=session.id)
    db.commit()
    assert count_session_feedback_today(db, session.id) == 1
```

- [ ] **Step 2: run, expect ImportError** — `pytest tests/test_feedback_db.py -v` fails: no module `ui.backend.db.feedback`.

- [ ] **Step 3: model** — append to `ui/backend/db/models.py`:

```python
class Feedback(Base):
    """One defect report or suggestion, from a logged-in user or a share-link
    visitor, triaged by the platform operator on the admin Feedback page.

    `org_id` is provenance, not ownership: all feedback belongs to the
    operator (there is no org-facing read surface). Exactly one of
    `submitted_by` / `share_session_id` is set -- enforced by the two write
    paths, not a CHECK. See docs/superpowers/specs/
    2026-08-26-feedback-system-design.md.
    """

    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("kind IN ('defect', 'suggestion')", name="ck_feedback_kind"),
        CheckConstraint(
            "status IN ('new', 'acknowledged', 'resolved', 'dismissed')",
            name="ck_feedback_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    kind: Mapped[str]  # "defect" | "suggestion"
    body: Mapped[str]
    status: Mapped[str] = mapped_column(default="new")
    admin_note: Mapped[Optional[str]] = mapped_column(nullable=True)
    submitted_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    share_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("share_sessions.id"), nullable=True, index=True
    )
    context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

- [ ] **Step 4: helpers** — `ui/backend/db/feedback.py`:

```python
"""CRUD for `Feedback` -- defect reports and suggestions.

Write paths guarantee exactly one of submitted_by/share_session_id is set;
there is no delete (triage is a status change, the row is the record).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .models import Feedback

KINDS = ("defect", "suggestion")
STATUSES = ("new", "acknowledged", "resolved", "dismissed")


def create_feedback(
    db: Session,
    *,
    kind: str,
    body: str,
    org_id: Optional[int] = None,
    submitted_by: Optional[int] = None,
    share_session_id: Optional[int] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Feedback:
    if (submitted_by is None) == (share_session_id is None):
        raise ValueError("exactly one of submitted_by/share_session_id must be set")
    row = Feedback(
        kind=kind, body=body, org_id=org_id, submitted_by=submitted_by,
        share_session_id=share_session_id, context=context,
    )
    db.add(row)
    db.flush()
    return row


def count_session_feedback_today(db: Session, share_session_id: int) -> int:
    """Today's rows for one visitor session (UTC midnight boundary, matching
    the naive-UTC created_at convention). Plain count -- see the spec for why
    the turn-cap CAS machinery is not warranted here."""
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    return (
        db.query(Feedback)
        .filter(Feedback.share_session_id == share_session_id, Feedback.created_at >= midnight)
        .count()
    )


def list_feedback(
    db: Session,
    *,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    org_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Feedback]:
    query = db.query(Feedback)
    if status is not None:
        query = query.filter(Feedback.status == status)
    if kind is not None:
        query = query.filter(Feedback.kind == kind)
    if org_id is not None:
        query = query.filter(Feedback.org_id == org_id)
    return (
        query.order_by(Feedback.created_at.desc(), Feedback.id.desc())
        .offset(offset).limit(limit).all()
    )


def get_feedback(db: Session, feedback_id: int) -> Optional[Feedback]:
    return db.query(Feedback).filter_by(id=feedback_id).one_or_none()


def update_feedback(
    db: Session, row: Feedback, *, status: Optional[str] = None, admin_note: Optional[str] = None
) -> Feedback:
    if status is not None:
        row.status = status
    if admin_note is not None:
        row.admin_note = admin_note
    db.flush()
    return row
```

- [ ] **Step 5: migration** — `alembic/versions/u8v9w0x1y2z3_add_feedback_table.py`, same guarded shape as `s6t7u8v9w0x1_run_knowledge_generations.py`:

```python
"""feedback: defect reports and suggestions for the platform operator

Revision ID: u8v9w0x1y2z3
Revises: t7u8v9w0x1y2
Create Date: 2026-08-26 00:00:00.000000

One row per submission, from a logged-in user (`submitted_by`) or a
share-link visitor (`share_session_id`); triage is `status`/`admin_note`
edited on the admin Feedback page. See docs/superpowers/specs/
2026-08-26-feedback-system-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'u8v9w0x1y2z3'
down_revision: Union[str, Sequence[str], None] = 't7u8v9w0x1y2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "feedback"


def upgrade() -> None:
    """Guarded (create_all-at-import idempotency, same as the other
    migrations): a database booted by the backend before `alembic upgrade
    head` already has the table."""
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("admin_note", sa.String(), nullable=True),
        sa.Column("submitted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("share_session_id", sa.Integer(), sa.ForeignKey("share_sessions.id"), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind IN ('defect', 'suggestion')", name="ck_feedback_kind"),
        sa.CheckConstraint(
            "status IN ('new', 'acknowledged', 'resolved', 'dismissed')",
            name="ck_feedback_status",
        ),
    )
    op.create_index("ix_feedback_share_session_id", _TABLE, ["share_session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_feedback_share_session_id", table_name=_TABLE)
    op.drop_table(_TABLE)
```

- [ ] **Step 6: run tests** — `pytest tests/test_feedback_db.py tests/test_migrations.py -v`, expect PASS (test_migrations walks every revision).

- [ ] **Step 7: commit** — `git add -A && git commit -m "feat(backend): feedback table, DB helpers, migration"`

---

### Task 2: `feedback_api.py` — authed submit + admin list/patch

**Files:**
- Create: `ui/backend/feedback_api.py`
- Modify: `ui/backend/main.py` (import + `app.include_router(feedback_router)`)
- Test: `tests/test_feedback_api.py`

**Interfaces:**
- Consumes: Task 1 helpers; `get_current_user`/`get_current_admin` from `auth_api`.
- Produces: `POST /api/feedback` (201, `{"id": int}`); `GET /api/admin/feedback` (`{"feedback": [...]}`, items carry `id, kind, body, status, admin_note, org_name, username, source, context, created_at`); `PATCH /api/admin/feedback/{id}` (`{"ok": true}`).

- [ ] **Step 1: failing tests** — `tests/test_feedback_api.py`, fixture copied from `tests/test_notifications_api.py`'s `ctx` (in-memory engine, `create_user_and_login`); admin client via `create_user_and_login(c, username="root", org=None, admin=True)` (check `helpers.py` signature at implementation time and match it):

```python
"""Feedback API: authed submit (/api/feedback) + admin triage (/api/admin/feedback)."""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.feedback import list_feedback
from ui.backend.db_session import get_db

# fixture `ctx` as in test_notifications_api.py; yields (client, SessionLocal)
# plus a `login(username, *, org, admin)` closure for a second principal.

def test_submit_requires_auth(ctx): ...          # POST /api/feedback w/o token -> 401
def test_submit_happy_path(ctx): ...             # 201; row has kind/body/org_id/submitted_by; status new
def test_submit_rejects_bad_kind(ctx): ...       # kind="rant" -> 422
def test_submit_rejects_empty_and_too_long(ctx): ...  # "   " -> 422; 4001 chars -> 422
def test_submit_whitelists_context(ctx): ...     # {"page": "/run", "evil": "x"} -> stored context lacks "evil"
def test_admin_list_requires_admin(ctx): ...     # org member GET /api/admin/feedback -> 403
def test_admin_list_filters_and_enrichment(ctx): ...  # filters status/kind; org_name/username/source fields
def test_admin_patch_status_and_note(ctx): ...   # PATCH -> row updated; bad status -> 422; unknown id -> 404
```

Write these as real tests (the bodies above are the checklist, not the code to paste).

- [ ] **Step 2: run, expect 404s/ImportError** — routes don't exist yet.

- [ ] **Step 3: implement** — `ui/backend/feedback_api.py`:

```python
"""Feedback: defect reports and suggestions.

Submission is open to every authenticated principal (org members and platform
admins); reading and triage are platform-admin only -- all feedback belongs to
the operator, `org_id` on a row is provenance. The anonymous share-link
counterpart lives in `share_chat.py` (it needs that module's link/session
helpers). See docs/superpowers/specs/2026-08-26-feedback-system-design.md.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth_api import get_current_admin, get_current_user
from .db.models import Organization, User, iso_utc
from .db.feedback import (
    KINDS, STATUSES, create_feedback, get_feedback, list_feedback, update_feedback,
)
from .db_session import get_db

router = APIRouter(prefix="/api", tags=["feedback"])

MAX_BODY_CHARS = 4000
_CONTEXT_VALUE_CHARS = 200


def sanitize_context(raw: Optional[Dict[str, Any]], allowed: frozenset) -> Optional[Dict[str, str]]:
    """Keep only whitelisted keys, coerced to bounded strings -- the client
    dict is attacker-shaped on the share surface and merely untrusted here."""
    if not raw:
        return None
    kept = {
        key: str(value)[:_CONTEXT_VALUE_CHARS]
        for key, value in raw.items()
        if key in allowed and value is not None
    }
    return kept or None


_AUTHED_CONTEXT_KEYS = frozenset({"page", "locale"})


class FeedbackCreate(BaseModel):
    kind: Literal["defect", "suggestion"]
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    context: Optional[Dict[str, Any]] = None


class FeedbackPatch(BaseModel):
    status: Optional[Literal["new", "acknowledged", "resolved", "dismissed"]] = None
    admin_note: Optional[str] = Field(default=None, max_length=MAX_BODY_CHARS)


@router.post("/feedback", status_code=201)
def submit_feedback(
    payload: FeedbackCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Feedback body is empty")
    row = create_feedback(
        db, kind=payload.kind, body=body, org_id=user.org_id, submitted_by=user.id,
        context=sanitize_context(payload.context, _AUTHED_CONTEXT_KEYS),
    )
    db.commit()
    return {"id": row.id}


def _serialise(row, db: Session) -> Dict[str, Any]:
    org_name = None
    if row.org_id is not None:
        org = db.query(Organization).filter_by(id=row.org_id).one_or_none()
        org_name = org.name if org else None
    username = None
    if row.submitted_by is not None:
        submitter = db.query(User).filter_by(id=row.submitted_by).one_or_none()
        username = submitter.username if submitter else None
    return {
        "id": row.id,
        "kind": row.kind,
        "body": row.body,
        "status": row.status,
        "admin_note": row.admin_note,
        "org_name": org_name,
        "username": username,
        "source": "user" if row.submitted_by is not None else "visitor",
        "context": row.context,
        "created_at": iso_utc(row.created_at) if row.created_at else None,
    }


@router.get("/admin/feedback")
def admin_list_feedback(
    status: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    org_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail="Unknown status")
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=422, detail="Unknown kind")
    rows = list_feedback(db, status=status, kind=kind, org_id=org_id, limit=limit, offset=offset)
    return {"feedback": [_serialise(row, db) for row in rows]}


@router.patch("/admin/feedback/{feedback_id}")
def admin_patch_feedback(
    feedback_id: int,
    payload: FeedbackPatch,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    row = get_feedback(db, feedback_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    update_feedback(db, row, status=payload.status, admin_note=payload.admin_note)
    db.commit()
    return {"ok": True}
```

In `main.py`: `from .feedback_api import router as feedback_router` beside the other imports and `app.include_router(feedback_router)` beside the other includes.

- [ ] **Step 4: run tests** — `pytest tests/test_feedback_api.py -v`, expect PASS.

- [ ] **Step 5: commit** — `git commit -m "feat(backend): feedback submit + admin triage API"`

---

### Task 3: anonymous share feedback route

**Files:**
- Modify: `ui/backend/share_chat.py`
- Test: `tests/test_share_feedback_api.py`

**Interfaces:**
- Consumes: `_resolve_active_link`, `_resolve_session_from_cookie` (share_chat), Task 1 helpers, `sanitize_context`/`FeedbackCreate`/`MAX_BODY_CHARS` from `feedback_api` (import the model — share body shape is identical).
- Produces: `POST /api/share/{token}/feedback` → 201 `{"id": int}`; 404 invalid link (same `_UNAVAILABLE` detail), 403 no session cookie, 429 over `FEEDBACK_DAILY_CAP` (=5).

- [ ] **Step 1: failing tests** — `tests/test_share_feedback_api.py`, fixture copied from `tests/test_share_chat_api.py` (file-based engine + `_make_link`); a session cookie is obtained by first POSTing one `/messages` turn:

```python
def test_unknown_token_is_404(client): ...
def test_no_session_cookie_is_403(client): ...        # valid link, fresh client, no prior message
def test_submit_after_chat_records_visitor_row(client): ...
    # send one message (mints cookie), then POST feedback ->
    # row.share_session_id set, row.submitted_by None, row.org_id == link org,
    # context.share_link_id == link id, run_id whitelisted through
def test_revoked_link_is_404(client): ...             # patch_share_link(active=False) then POST
def test_daily_cap_429(client): ...                   # 5 accepted, 6th -> 429
def test_body_cap_422(client): ...                    # 4001 chars
```

- [ ] **Step 2: run, expect 404/405** — route missing.

- [ ] **Step 3: implement** — in `share_chat.py` (imports at top; route beside the other POST):

```python
from .db.feedback import count_session_feedback_today, create_feedback
from .feedback_api import FeedbackCreate, sanitize_context

FEEDBACK_DAILY_CAP = 5
_SHARE_CONTEXT_KEYS = frozenset({"page", "locale", "run_id"})
_NO_SESSION_MESSAGE = "Open the chat before sending feedback"


@router.post("/{token}/feedback", status_code=201)
def submit_share_feedback(
    token: str,
    payload: FeedbackCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Visitor feedback. Deliberately does NOT mint a session (unlike a
    message send): a visitor with no cookie has never opened the chat, and
    feedback alone must not grow share_sessions unboundedly -- 403 instead."""
    link = _resolve_active_link(db, token)
    session = _resolve_session_from_cookie(request, db, link)
    if session is None:
        raise HTTPException(status_code=403, detail=_NO_SESSION_MESSAGE)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Feedback body is empty")
    if count_session_feedback_today(db, session.id) >= FEEDBACK_DAILY_CAP:
        raise HTTPException(status_code=429, detail=_RATE_LIMITED_MESSAGE)
    context = sanitize_context(payload.context, _SHARE_CONTEXT_KEYS) or {}
    context["share_link_id"] = link.id
    row = create_feedback(
        db, kind=payload.kind, body=body, org_id=link.org_id,
        share_session_id=session.id, context=context,
    )
    db.commit()
    return {"id": row.id}
```

- [ ] **Step 4: run** — `pytest tests/test_share_feedback_api.py tests/test_share_chat_api.py -v`, expect PASS (no regressions).

- [ ] **Step 5: commit** — `git commit -m "feat(backend): anonymous share-link feedback endpoint"`

---

### Task 4: `FeedbackModal` + Layout entry + i18n

**Files:**
- Create: `ui/frontend/src/components/FeedbackModal.tsx`, `FeedbackModal.css`
- Modify: `ui/frontend/src/components/Layout.tsx`, `ui/frontend/src/lib/api.ts`, `ui/frontend/src/lib/types.ts`, `ui/frontend/src/locales/en.ts`, `ui/frontend/src/locales/zh-CN.ts`
- Test: `ui/frontend/src/components/FeedbackModal.test.tsx`

**Interfaces:**
- Produces: `<FeedbackModal open onClose={() => void} onSubmit={(kind: 'defect' | 'suggestion', body: string) => Promise<void>} />` — the caller owns the POST (URL + context differ per surface). `api.submitFeedback(payload: { kind: string; body: string; context?: Record<string, string> })`.

- [ ] **Step 1: i18n keys** — `en.ts` gains `nav.feedback: 'Feedback'` and a `feedback` section:

```ts
feedback: {
  title: 'Send feedback',
  kindDefect: 'Report a problem',
  kindSuggestion: 'Make a suggestion',
  placeholder: 'Tell us what went wrong, or what you would like to see…',
  submit: 'Send',
  sending: 'Sending…',
  thanks: 'Thank you — your feedback has been recorded.',
  tooMany: 'Feedback limit reached for today — please try again tomorrow.',
  failed: "Couldn't send your feedback. Please try again.",
},
```

`zh-CN.ts` mirrors every key (`nav.feedback: '反馈'`, `title: '意见反馈'`, `kindDefect: '报告问题'`, `kindSuggestion: '提出建议'`, `placeholder: '告诉我们哪里出了问题,或您希望看到什么…'`, `submit: '发送'`, `sending: '发送中…'`, `thanks: '谢谢,您的反馈已记录。'`, `tooMany: '今天的反馈次数已达上限,请明天再试。'`, `failed: '反馈发送失败,请重试。'`).

- [ ] **Step 2: failing component test** — `FeedbackModal.test.tsx`: renders nothing when closed; toggle switches kind; submit disabled on empty body; successful submit calls `onSubmit('defect', 'text')` then shows `feedback.thanks`; a rejection with `status = 429` shows `feedback.tooMany`; other rejection shows `feedback.failed` and keeps the draft. Follow the render/i18n harness used by existing component tests (see `ShareChatPage.test.tsx` / `LanguageSelect` tests for the provider setup).

- [ ] **Step 3: implement `FeedbackModal.tsx`** — controlled modal, ~90 lines:

```tsx
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import './FeedbackModal.css'

const MAX_BODY = 4000

interface Props {
  open: boolean
  onClose: () => void
  onSubmit: (kind: 'defect' | 'suggestion', body: string) => Promise<void>
}

export default function FeedbackModal({ open, onClose, onSubmit }: Props) {
  const { t } = useTranslation()
  const [kind, setKind] = useState<'defect' | 'suggestion'>('defect')
  const [body, setBody] = useState('')
  const [state, setState] = useState<'idle' | 'sending' | 'done'>('idle')
  const [errorKey, setErrorKey] = useState<'feedback.tooMany' | 'feedback.failed' | null>(null)

  if (!open) return null

  const close = () => {
    setState('idle'); setErrorKey(null); setBody(''); setKind('defect'); onClose()
  }

  const submit = async (e: { preventDefault(): void }) => {
    e.preventDefault()
    const text = body.trim()
    if (!text || state === 'sending') return
    setState('sending'); setErrorKey(null)
    try {
      await onSubmit(kind, text)
      setState('done')
    } catch (err) {
      setState('idle')
      setErrorKey((err as Error & { status?: number }).status === 429 ? 'feedback.tooMany' : 'feedback.failed')
    }
  }
  // render: overlay + dialog; when state === 'done' show thanks + close button;
  // otherwise kind radio pair, textarea (maxLength MAX_BODY), error line,
  // cancel/submit buttons (submit label t(state === 'sending' ? 'feedback.sending' : 'feedback.submit')).
}
```

(The render body is ordinary JSX per the comment — write it out fully; reuse `ConfirmDialog.css`'s overlay/dialog class conventions for `FeedbackModal.css`.)

- [ ] **Step 4: Layout entry** — in `Layout.tsx`, a `nav-action` button before the change-password one, visible to every principal, plus modal state; submit posts via `api.submitFeedback` with `context: { page: pathname, locale: i18n.language }`. Add to `api.ts`:

```ts
submitFeedback: (payload: { kind: string; body: string; context?: Record<string, string> }) =>
  request<{ id: number }>('/api/feedback', { method: 'POST', body: JSON.stringify(payload) }),
```

- [ ] **Step 5: run** — `npm test -- FeedbackModal` then the full `npm test`; `npx tsc -b` clean (zh-CN typing enforces translation parity).

- [ ] **Step 6: commit** — `git commit -m "feat(frontend): feedback modal + logged-in entry point"`

---

### Task 5: visitor entry on ShareChatPage

**Files:**
- Modify: `ui/frontend/src/pages/ShareChatPage.tsx`, `ui/frontend/src/lib/shareChatApi.ts`
- Test: extend `ui/frontend/src/pages/ShareChatPage.test.tsx`

**Interfaces:**
- Consumes: `FeedbackModal` (Task 4).
- Produces: `shareChatApi.sendFeedback(token, payload)` → POST `/api/share/{token}/feedback` (credentials included, like every shareRequest).

- [ ] **Step 1: failing test** — ShareChatPage shows a feedback button in the header; clicking opens the modal; submit posts to the share feedback URL (mock fetch, assert URL + body).

- [ ] **Step 2: implement** — `shareChatApi.sendFeedback` beside `sendMessage`; in `ShareChatPage` add `const [feedbackOpen, setFeedbackOpen] = useState(false)` and a `lastRunIdRef` updated where `setRunId(dispatchedRunId)` happens; header gains a `btn-link` feedback button (`t('nav.feedback')`) next to `LanguageSelect`; modal's `onSubmit` calls `shareChatApi.sendFeedback(token, { kind, body, context: { page: '/share', locale: i18n.language, ...(lastRunIdRef.current ? { run_id: lastRunIdRef.current } : {}) } })`.

- [ ] **Step 3: run** — `npm test -- ShareChatPage`, expect PASS.

- [ ] **Step 4: commit** — `git commit -m "feat(frontend): share-link visitor feedback entry"`

---

### Task 6: admin FeedbackPage

**Files:**
- Create: `ui/frontend/src/pages/FeedbackPage.tsx`
- Modify: `ui/frontend/src/App.tsx` (route under `RequireAdmin`), `ui/frontend/src/components/Layout.tsx` (admin NavLink), `ui/frontend/src/lib/api.ts`, `ui/frontend/src/lib/types.ts`
- Test: `ui/frontend/src/pages/FeedbackPage.test.tsx`

**Interfaces:**
- Consumes: `api.adminFeedback(filters)`, `api.patchFeedback(id, patch)`:

```ts
adminFeedback: (opts: { status?: string; kind?: string } = {}) => {
  const params = new URLSearchParams()
  if (opts.status) params.set('status', opts.status)
  if (opts.kind) params.set('kind', opts.kind)
  const qs = params.toString()
  return request<{ feedback: FeedbackItem[] }>(`/api/admin/feedback${qs ? `?${qs}` : ''}`)
},
patchFeedback: (id: number, patch: { status?: string; admin_note?: string }) =>
  request<{ ok: boolean }>(`/api/admin/feedback/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
```

with `FeedbackItem` in `types.ts` matching Task 2's serialisation.

- [ ] **Step 1: failing tests** — FeedbackPage renders rows from a mocked `api.adminFeedback`; status filter refetches with the filter; clicking a row expands full body + context; changing status calls `api.patchFeedback`; body renders as text (a `<b>` payload stays literal).

- [ ] **Step 2: implement** — follow `MemoryPage.tsx`'s structure (English literals for content, `AdvancedPage.css` reuse): filter selects for status (All/new/acknowledged/resolved/dismissed) and kind (All/defect/suggestion); a table of `created_at` (via `dateFormat`), kind, status, org_name ?? '—', source, body excerpt (first 120 chars); expanded row shows full body in a `<pre className="feedback-body">`-style block (plain text), the context dict as `key: value` lines, a status `<select>`, an admin-note textarea and a Save button calling `patchFeedback` then refetching. Route: `<Route path="/feedback" element={<FeedbackPage />} />` under `RequireAdmin`; nav: admin NavLink `t('nav.feedback')` after Trace.

- [ ] **Step 3: run** — `npm test`, `npx tsc -b`, `npm run lint`, expect clean.

- [ ] **Step 4: commit** — `git commit -m "feat(frontend): admin feedback triage page"`

---

### Task 7: documentation

**Files:**
- Modify: `docs/ADMIN_MANUAL.md` (four → five pages; a Feedback-page section: what arrives, the status lifecycle, the caps), root `CLAUDE.md` (admin pages list mention), `ui/backend/CLAUDE.md` (feedback_api + share route + caps paragraph), `ui/backend/db/CLAUDE.md` (`feedback` table entry), `ui/frontend/CLAUDE.md` (FeedbackModal/FeedbackPage/nav), `docs/STATUS.md` (done + phase-3 next-step item).

- [ ] **Step 1: write the updates** — each is a short paragraph in the file's existing voice; STATUS.md's next-steps gains "Feedback phase 3: LLM categorisation + self-closing improvement loop (spec'd out of scope 2026-08-26)".
- [ ] **Step 2: commit** — `git commit -m "docs: feedback system"`

---

### Task 8: gates and delivery

- [ ] **Step 1: backend** — `.venv\Scripts\python.exe -m pytest -m "not e2e"` (serial) from the worktree root: all green.
- [ ] **Step 2: frontend** — in `ui/frontend`: `npm run lint && npx tsc -b && npm test && npm run build`: all green.
- [ ] **Step 3: e2e smoke** — `python -m pytest tests/e2e -m "e2e and not slow"` per CI's e2e-smoke selection (ports 8000/5173 free, serial). If the environment blocks it (known 100%-CPU import-timeout issue), record that in the final report instead of faking it.
- [ ] **Step 4: push + draft PR** — push `worktree-feat-feedback-system` (or a nicer `feat/feedback-system` branch), open a draft PR titled "feat: user feedback (defects/suggestions) from app + share links, admin triage page".
