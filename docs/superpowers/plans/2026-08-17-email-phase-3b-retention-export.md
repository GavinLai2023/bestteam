# Email automation Phase 3b — retention, deletion and export: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each org a run-history retention period, an on-demand purge (one
run or a batch), and a JSON export — so email-derived model output stops
persisting forever, without destroying the org's cost history.

**Architecture:** A purge clears *content* (`runs.input`/`output`,
`trace_events`, `automation_item_results.payload`) and leaves *accounting* (the
`runs` row, `usage_records`, `trigger_context`, and an item result's
`status`/`source_key`). Policy lives in a new `org_retention_settings` table;
a sweep runs from the email poller's maintenance tail. Export emits exactly
what a purge removes, and a test enforces that coupling.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0 (`Mapped`/
`mapped_column`), Alembic, pytest; React 18 + TypeScript + Vite, Vitest +
Testing Library.

**Spec:** `docs/superpowers/specs/2026-08-17-email-phase-3b-retention-export-design.md`

## Global Constraints

- **Branch:** `feat/email-phase-3a-health-alerting` (already checked out).
  All work continues on it — it is PR #64. Do not create a new branch.
- **Venv:** always `./.venv/Scripts/python.exe` on Windows.
- **No new dependencies.** Standard library plus what is already installed.
- **Every new test file needs a `pytestmark`** (`unit`/`integration`/`e2e`/
  `optional`) or `tests/test_marker_completeness.py` fails the suite.
- **Alembic migrations must be guarded** (`_tables`/`_columns` inspector
  helpers) because `ui/backend/db_session.py` runs `create_all` at import, so a
  fresh database already has every table and column when a migration runs.
- **Code comments in English**, British spelling in prose (organisation,
  behaviour, analyse).
- **Never delete a `runs` row and never touch `usage_records`.** Deleting a
  run takes the org's token/cost history with it (`usage_records.run_id` is
  non-nullable and `run_analytics_api.py` reports over it).
- **`automation_item_results.status` and `source_key` survive every purge.**
  `automation_results.py`'s `CONFIRMED_DRAFT_OUTCOMES` scan uses them to
  exclude already-drafted UIDs from a retry; clearing them would make a
  retention sweep cause duplicate drafts.
- **Default is keep-forever.** `run_retention_days` NULL means nothing is ever
  purged automatically. An upgrade must delete nothing.
- Follow the existing style of the file you are editing. Do **not** run
  Prettier on frontend files — the repo uses no-semicolon, single-quote style
  and Prettier reformats it wholesale.

## File Structure

| File | Responsibility |
|---|---|
| `ui/backend/db/models.py` (modify) | `OrgRetentionSetting`; `Run.content_purged_at` |
| `alembic/versions/k8l9m0n1o2p3_add_retention.py` (create) | guarded additive migration |
| `ui/backend/db/retention.py` (create) | settings row CRUD only — no purge logic |
| `ui/backend/retention.py` (create) | purge engine, sweep, export |
| `ui/backend/email_trigger.py` (modify) | `run_maintenance` / `maintenance_once`; call the sweep |
| `ui/backend/org_settings.py` (modify) | `/api/org/retention` GET/PUT/purge, `/api/org/export` |
| `ui/backend/main.py` (modify) | `POST /api/runs/{run_id}/purge` |
| `ui/frontend/src/lib/types.ts` / `api.ts` (modify) | retention types + 5 API helpers |
| `ui/frontend/src/components/DataRetentionPanel.tsx` (create) | the Data tab's panel |
| `ui/frontend/src/pages/ActivityPage.tsx` (modify) | fifth "Data" tab |
| `ui/frontend/src/components/RunDetail.tsx` (modify) | purged rendering + delete action |
| `tests/test_retention.py`, `tests/test_retention_api.py` (create) | engine + routes |
| docs (`STATUS.md`, `DECISIONS.md`, `deployment.md`, `.env.example`, CLAUDE.md files) | record it |

---

### Task 1: Schema and settings row

**Files:**
- Modify: `ui/backend/db/models.py`
- Create: `alembic/versions/k8l9m0n1o2p3_add_retention.py`
- Create: `ui/backend/db/retention.py`
- Modify: `tests/test_db.py:39-65` (the pinned table set)
- Test: `tests/test_retention.py` (new, this task adds the settings tests only)

**Interfaces:**
- Consumes: nothing.
- Produces: `OrgRetentionSetting` (fields `id`, `org_id`, `run_retention_days`,
  `last_swept_at`, `last_purged_count`); `Run.content_purged_at`;
  `get_retention_settings(db, org_id) -> Optional[OrgRetentionSetting]`,
  `set_retention_days(db, org_id, days) -> OrgRetentionSetting`,
  `record_sweep(db, org_id, *, purged, at) -> None`,
  `orgs_with_retention(db) -> list[tuple[int, int]]` returning `(org_id, days)`.

- [ ] **Step 1: Write the failing test**

Add to a new `tests/test_retention.py`:

```python
"""Phase 3b: retention settings, the purge engine, and export."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.orgs import create_org
from ui.backend.db.retention import (
    get_retention_settings,
    orgs_with_retention,
    record_sweep,
    set_retention_days,
)


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_retention_is_unset_until_configured(db):
    org = create_org(db, "acme")
    assert get_retention_settings(db, org.id) is None
    assert orgs_with_retention(db) == []


def test_set_and_clear_retention_days(db):
    org = create_org(db, "acme")

    row = set_retention_days(db, org.id, 30)
    db.commit()
    assert row.run_retention_days == 30
    assert orgs_with_retention(db) == [(org.id, 30)]

    set_retention_days(db, org.id, None)
    db.commit()
    # The row survives (it carries sweep history); the policy is off.
    assert get_retention_settings(db, org.id).run_retention_days is None
    assert orgs_with_retention(db) == []


def test_record_sweep_stamps_history(db):
    from datetime import datetime, timezone

    org = create_org(db, "acme")
    set_retention_days(db, org.id, 7)
    at = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

    record_sweep(db, org.id, purged=4, at=at)
    db.commit()

    row = get_retention_settings(db, org.id)
    assert row.last_purged_count == 4
    assert row.last_swept_at.replace(tzinfo=timezone.utc) == at
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention.py -v`
Expected: FAIL — `ModuleNotFoundError: ui.backend.db.retention`.

- [ ] **Step 3: Add the model and the column**

In `ui/backend/db/models.py`, add `content_purged_at` to `Run` (after
`retry_of_run_id`, before `created_at`):

```python
    # Phase 3b: when this run's content (input/output/trace/item payloads) was
    # cleared by a retention purge. The row itself survives -- usage_records
    # hangs off it and carries the org's cost history. NULL = never purged.
    content_purged_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
```

and a new model beside `OrgNotificationSetting`:

```python
class OrgRetentionSetting(Base):
    """One org's run-history retention policy, plus proof it is running.

    `run_retention_days` NULL means keep forever -- the default, so an upgrade
    deletes nothing. `last_swept_at`/`last_purged_count` exist because a
    retention policy whose job silently stopped is indistinguishable from one
    that is working, until an audit.
    """

    __tablename__ = "org_retention_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True, nullable=False
    )
    run_retention_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    last_swept_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_purged_count: Mapped[int] = mapped_column(default=0)
```

- [ ] **Step 4: Write `ui/backend/db/retention.py`**

```python
"""Per-org run-history retention settings (Phase 3b).

Row CRUD only. The purge itself lives in `ui/backend/retention.py` so it can be
tested without any notion of policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from .models import OrgRetentionSetting


def get_retention_settings(db: Session, org_id: int) -> Optional[OrgRetentionSetting]:
    return (
        db.query(OrgRetentionSetting)
        .filter(OrgRetentionSetting.org_id == org_id)
        .one_or_none()
    )


def set_retention_days(db: Session, org_id: int, days: Optional[int]) -> OrgRetentionSetting:
    """Set (or clear, with None) the policy. The row is kept either way -- it
    carries the sweep history, which outlives any one policy value."""
    row = get_retention_settings(db, org_id)
    if row is None:
        row = OrgRetentionSetting(org_id=org_id)
        db.add(row)
    row.run_retention_days = days
    db.flush()
    return row


def record_sweep(db: Session, org_id: int, *, purged: int, at: datetime) -> None:
    row = get_retention_settings(db, org_id)
    if row is None:
        return
    row.last_swept_at = at
    row.last_purged_count = purged
    db.flush()


def orgs_with_retention(db: Session) -> List[Tuple[int, int]]:
    """`(org_id, days)` for every org with a policy actually set."""
    rows = (
        db.query(OrgRetentionSetting)
        .filter(OrgRetentionSetting.run_retention_days.isnot(None))
        .order_by(OrgRetentionSetting.org_id)
        .all()
    )
    return [(r.org_id, int(r.run_retention_days)) for r in rows]
```

- [ ] **Step 5: Write the guarded migration**

Create `alembic/versions/k8l9m0n1o2p3_add_retention.py` with
`revision = 'k8l9m0n1o2p3'`, `down_revision = 'j7k8l9m0n1o2'`. Copy the
`_tables`/`_columns` inspector-helper shape from
`alembic/versions/j7k8l9m0n1o2_add_notifications.py` exactly. Upgrade creates
`org_retention_settings` if absent and adds `runs.content_purged_at` if absent;
downgrade reverses both. Purely additive, no backfill.

- [ ] **Step 6: Pin the new table**

In `tests/test_db.py`, add `"org_retention_settings",` to the expected set in
`test_init_db_creates_all_tables`.

- [ ] **Step 7: Verify**

```
./.venv/Scripts/python.exe -m pytest tests/test_retention.py tests/test_db.py tests/test_migrations.py -v
./.venv/Scripts/python.exe -m alembic heads
```
Expected: tests PASS; `alembic heads` prints exactly one head, `k8l9m0n1o2p3`.

- [ ] **Step 8: Commit**

```bash
git add ui/backend/db/models.py ui/backend/db/retention.py alembic/versions/k8l9m0n1o2p3_add_retention.py tests/test_retention.py tests/test_db.py
git commit -m "feat(retention): per-org policy row and a purged-at stamp on runs"
```

---

### Task 2: The purge engine

**Files:**
- Create: `ui/backend/retention.py`
- Test: `tests/test_retention.py` (extend)

**Interfaces:**
- Consumes: Task 1's `OrgRetentionSetting`, `Run.content_purged_at`.
- Produces: `PURGED_FIELDS: dict[str, tuple[str, ...]]`;
  `purge_run(db, run) -> bool`;
  `purge_org_runs(db, *, org_id, older_than_days, now=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_retention.py`. These encode the spec's invariants; they
are the point of the task.

```python
from datetime import datetime, timedelta, timezone

from ui.backend.db.models import (
    AutomationItemResult,
    Run,
    TraceEventRecord,
    UsageRecord,
)
from ui.backend.retention import purge_org_runs, purge_run


def _run(db, org_id, *, run_id="r1", status="completed", age_days=0):
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    run = Run(
        id=run_id, workflow="support", input="From alice@example.com: my boiler leaks",
        output="Drafted a reply to alice@example.com", status=status,
        org_id=org_id, created_at=created,
    )
    db.add(run)
    db.add(TraceEventRecord(run_id=run_id, seq=1, type="agent_completed",
                            agent="writer", data='{"text": "alice@example.com"}'))
    db.add(UsageRecord(run_id=run_id, agent="writer", model="fake:",
                       input_tokens=10, output_tokens=5, org_id=org_id))
    db.add(AutomationItemResult(
        org_id=org_id, run_id=run_id, source_key="mbx:7", result_type="email",
        status="processed", needs_attention=False,
        payload={"sender": "alice@example.com", "summary": "boiler"},
    ))
    db.flush()
    return run


def test_purge_clears_content(db):
    org = create_org(db, "acme")
    run = _run(db, org.id)

    assert purge_run(db, run) is True
    db.commit()

    assert run.input == ""
    assert run.output is None
    assert run.content_purged_at is not None
    assert db.query(TraceEventRecord).filter_by(run_id=run.id).count() == 0
    assert db.query(AutomationItemResult).filter_by(run_id=run.id).one().payload == {}


def test_purge_keeps_the_accounting(db):
    """I2: usage rows and the run row itself survive -- they are the org's
    cost history, not email content."""
    org = create_org(db, "acme")
    run = _run(db, org.id)

    purge_run(db, run)
    db.commit()

    assert db.get(Run, run.id) is not None
    usage = db.query(UsageRecord).filter_by(run_id=run.id).one()
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)


def test_purge_keeps_item_status_and_source_key(db):
    """I1: clearing these would make a sweep cause duplicate drafts, because
    automation_results.py excludes already-drafted UIDs by exactly these two
    fields."""
    org = create_org(db, "acme")
    run = _run(db, org.id)

    purge_run(db, run)
    db.commit()

    item = db.query(AutomationItemResult).filter_by(run_id=run.id).one()
    assert item.source_key == "mbx:7"
    assert item.status == "processed"


def test_purge_refuses_a_running_run(db):
    """I3: the worker is still writing trace events."""
    org = create_org(db, "acme")
    run = _run(db, org.id, status="running")

    assert purge_run(db, run) is False
    assert run.input != ""


def test_purge_is_idempotent(db):
    """I4: the sweep re-selects rows on overlapping cycles."""
    org = create_org(db, "acme")
    run = _run(db, org.id)

    assert purge_run(db, run) is True
    db.commit()
    first = run.content_purged_at

    assert purge_run(db, run) is False
    db.commit()
    assert run.content_purged_at == first


def test_purge_org_runs_respects_the_cutoff(db):
    org = create_org(db, "acme")
    _run(db, org.id, run_id="old", age_days=40)
    _run(db, org.id, run_id="new", age_days=2)

    assert purge_org_runs(db, org_id=org.id, older_than_days=30) == 1
    db.commit()

    assert db.get(Run, "old").content_purged_at is not None
    assert db.get(Run, "new").content_purged_at is None


def test_purge_org_runs_is_scoped_to_one_org(db):
    a = create_org(db, "acme")
    b = create_org(db, "beta")
    _run(db, a.id, run_id="a1", age_days=40)
    _run(db, b.id, run_id="b1", age_days=40)

    assert purge_org_runs(db, org_id=a.id, older_than_days=30) == 1
    db.commit()

    assert db.get(Run, "b1").content_purged_at is None


def test_purge_org_runs_zero_days_takes_everything_terminal(db):
    org = create_org(db, "acme")
    _run(db, org.id, run_id="done", age_days=0)
    _run(db, org.id, run_id="live", age_days=0, status="running")

    assert purge_org_runs(db, org_id=org.id, older_than_days=0) == 1
    db.commit()

    assert db.get(Run, "done").content_purged_at is not None
    assert db.get(Run, "live").content_purged_at is None
```

- [ ] **Step 2: Run and watch them fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention.py -v`
Expected: FAIL — `ModuleNotFoundError: ui.backend.retention`.

- [ ] **Step 3: Write `ui/backend/retention.py`**

```python
"""Run-history retention: the purge engine, the sweep, and export (Phase 3b).

A purge clears CONTENT and keeps ACCOUNTING. Content is `runs.input`/`output`,
every `trace_events` row, and `automation_item_results.payload`. Accounting is
the `runs` row itself, `usage_records`, `trigger_context`, and an item result's
`status`/`source_key` -- see the design spec's invariants I1-I5. Deleting the
run row instead would take the org's token/cost history with it, and clearing
an item's status/source_key would make a sweep cause duplicate drafts on retry.

See docs/superpowers/specs/2026-08-17-email-phase-3b-retention-export-design.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db.models import AutomationItemResult, Run, TraceEventRecord

_logger = logging.getLogger(__name__)

# The purge surface, declared once. `export_org_runs` must emit every one of
# these, and tests/test_retention.py::test_export_covers_everything_purge_clears
# is what enforces it -- an export that stopped covering a purged field would
# make deletion quietly unsafe.
PURGED_FIELDS: dict[str, tuple[str, ...]] = {
    "runs": ("input", "output"),
    "trace_events": ("*",),
    "automation_item_results": ("payload",),
}

_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def purge_run(db: Session, run: Run) -> bool:
    """Clear one run's content. Does NOT commit.

    Returns False without touching anything when the run is still running (its
    worker is mid-write) or was already purged -- both are ordinary, not
    errors, so callers can loop over a batch without special cases.
    """
    if run.status not in _TERMINAL_STATUSES or run.content_purged_at is not None:
        return False

    db.query(TraceEventRecord).filter(TraceEventRecord.run_id == run.id).delete(
        synchronize_session=False
    )
    for item in db.query(AutomationItemResult).filter(
        AutomationItemResult.run_id == run.id
    ):
        item.payload = {}

    run.input = ""
    run.output = None
    run.content_purged_at = _utcnow()
    db.flush()
    return True


def purge_org_runs(
    db: Session, *, org_id: int, older_than_days: int, now: Optional[datetime] = None
) -> int:
    """Purge every terminal, unpurged run of this org older than the cutoff.

    `older_than_days=0` means everything terminal, right now. Does NOT commit.
    """
    cutoff = (now or _utcnow()) - timedelta(days=older_than_days)
    runs = (
        db.query(Run)
        .filter(
            Run.org_id == org_id,
            Run.created_at < cutoff,
            Run.content_purged_at.is_(None),
            Run.status.in_(_TERMINAL_STATUSES),
        )
        .all()
    )
    return sum(1 for run in runs if purge_run(db, run))
```

- [ ] **Step 4: Verify**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention.py -v`
Expected: PASS (all settings + engine tests).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/retention.py tests/test_retention.py
git commit -m "feat(retention): purge a run's content and keep its accounting"
```

---

### Task 3: Sweep and export

**Files:**
- Modify: `ui/backend/retention.py`
- Test: `tests/test_retention.py` (extend)

**Interfaces:**
- Consumes: Task 2's `purge_org_runs`, `PURGED_FIELDS`; Task 1's
  `orgs_with_retention`/`record_sweep`.
- Produces: `retention_default_days() -> Optional[int]`;
  `sweep_retention(db, *, now=None) -> int` (commits);
  `export_org_runs(db, *, org_id, days=None, limit=None) -> dict`;
  `export_max_runs() -> int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sweep_applies_each_orgs_own_policy(db):
    from ui.backend.db.retention import set_retention_days
    from ui.backend.retention import sweep_retention

    a = create_org(db, "acme")
    b = create_org(db, "beta")
    set_retention_days(db, a.id, 30)
    _run(db, a.id, run_id="a-old", age_days=40)
    _run(db, b.id, run_id="b-old", age_days=40)  # no policy at all
    db.commit()

    assert sweep_retention(db) == 1

    assert db.get(Run, "a-old").content_purged_at is not None
    assert db.get(Run, "b-old").content_purged_at is None  # I5: NULL keeps forever


def test_sweep_records_that_it_ran(db):
    from ui.backend.db.retention import get_retention_settings, set_retention_days
    from ui.backend.retention import sweep_retention

    org = create_org(db, "acme")
    set_retention_days(db, org.id, 30)
    _run(db, org.id, run_id="old", age_days=40)
    db.commit()

    sweep_retention(db)

    row = get_retention_settings(db, org.id)
    assert row.last_swept_at is not None
    assert row.last_purged_count == 1


def test_export_carries_the_content(db):
    from ui.backend.retention import export_org_runs

    org = create_org(db, "acme")
    _run(db, org.id, run_id="r1")
    db.commit()

    bundle = export_org_runs(db, org_id=org.id)

    assert bundle["truncated"] is False
    run = bundle["runs"][0]
    assert run["id"] == "r1"
    assert "alice@example.com" in run["output"]
    assert run["trace_events"][0]["data"] == '{"text": "alice@example.com"}'
    assert run["automation_item_results"][0]["payload"]["sender"] == "alice@example.com"


def test_export_is_scoped_to_one_org(db):
    from ui.backend.retention import export_org_runs

    a = create_org(db, "acme")
    b = create_org(db, "beta")
    _run(db, a.id, run_id="a1")
    _run(db, b.id, run_id="b1")
    db.commit()

    assert [r["id"] for r in export_org_runs(db, org_id=a.id)["runs"]] == ["a1"]


def test_export_flags_truncation(db):
    from ui.backend.retention import export_org_runs

    org = create_org(db, "acme")
    for i in range(3):
        _run(db, org.id, run_id=f"r{i}", age_days=i)
    db.commit()

    bundle = export_org_runs(db, org_id=org.id, limit=2)

    assert bundle["truncated"] is True
    assert len(bundle["runs"]) == 2
    assert bundle["oldest_included"] is not None


def test_export_covers_everything_purge_clears(db):
    """The coupling that makes deletion safe: if a field is added to the purge
    and not to the export, the export silently stops being a way out."""
    from ui.backend.retention import PURGED_FIELDS, export_org_runs

    org = create_org(db, "acme")
    _run(db, org.id, run_id="r1")
    db.commit()

    run = export_org_runs(db, org_id=org.id)["runs"][0]

    for field in PURGED_FIELDS["runs"]:
        assert field in run
    assert "trace_events" in run
    for field in PURGED_FIELDS["automation_item_results"]:
        assert field in run["automation_item_results"][0]
```

- [ ] **Step 2: Run and watch them fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention.py -k "sweep or export" -v`
Expected: FAIL with `ImportError: cannot import name 'sweep_retention'`.

- [ ] **Step 3: Implement**

Append to `ui/backend/retention.py`:

```python
def _int_env(name: str, default: Optional[int], *, minimum: int) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        _logger.warning("%s is not an integer (%r); using %r", name, raw, default)
        return default


def retention_default_days() -> Optional[int]:
    """The policy a NEWLY created org starts with. Never applied to an existing
    org: an upgrade must not delete anybody's history (I5)."""
    return _int_env("BESTTEAM_RUN_RETENTION_DAYS", None, minimum=1)


def export_max_runs() -> int:
    return _int_env("BESTTEAM_EXPORT_MAX_RUNS", 5000, minimum=1)


def sweep_retention(db: Session, *, now: Optional[datetime] = None) -> int:
    """Apply every org's configured policy. Commits; returns the total purged.

    Orgs with no policy (the default) are not touched at all.
    """
    at = now or _utcnow()
    total = 0
    for org_id, days in orgs_with_retention(db):
        purged = purge_org_runs(db, org_id=org_id, older_than_days=days, now=at)
        record_sweep(db, org_id, purged=purged, at=at)
        total += purged
    db.commit()
    if total:
        _logger.info("retention sweep purged %d run(s)", total)
    return total


def export_org_runs(
    db: Session, *, org_id: int, days: Optional[int] = None, limit: Optional[int] = None
) -> dict:
    """Everything a purge would remove, plus the context needed to read it.

    Newest first, so a truncated export is the part a customer most likely
    wants. `truncated` is explicit: a partial export that looked complete would
    be worse than no export at all.
    """
    cap = limit or export_max_runs()
    query = db.query(Run).filter(Run.org_id == org_id)
    if days is not None:
        query = query.filter(Run.created_at >= _utcnow() - timedelta(days=days))
    rows = query.order_by(Run.created_at.desc(), Run.id).limit(cap + 1).all()
    truncated = len(rows) > cap
    rows = rows[:cap]

    runs = []
    for run in rows:
        events = (
            db.query(TraceEventRecord)
            .filter(TraceEventRecord.run_id == run.id)
            .order_by(TraceEventRecord.seq)
            .all()
        )
        items = (
            db.query(AutomationItemResult)
            .filter(AutomationItemResult.run_id == run.id)
            .order_by(AutomationItemResult.id)
            .all()
        )
        runs.append({
            "id": run.id,
            "workflow": run.workflow,
            "status": run.status,
            "input": run.input,
            "output": run.output,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "content_purged_at": (
                run.content_purged_at.isoformat() if run.content_purged_at else None
            ),
            "trigger_context": run.trigger_context,
            "trace_events": [
                {"seq": e.seq, "type": e.type, "agent": e.agent, "data": e.data,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in events
            ],
            "automation_item_results": [
                {"source_key": i.source_key, "status": i.status,
                 "needs_attention": i.needs_attention, "payload": i.payload,
                 "created_at": i.created_at.isoformat() if i.created_at else None}
                for i in items
            ],
        })

    return {
        "org_id": org_id,
        "exported_at": _utcnow().isoformat(),
        "truncated": truncated,
        "oldest_included": runs[-1]["created_at"] if runs else None,
        "runs": runs,
    }
```

Add `import os` to the module's imports and
`from .db.retention import orgs_with_retention, record_sweep`.

- [ ] **Step 4: Verify**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/retention.py tests/test_retention.py
git commit -m "feat(retention): nightly sweep, and an export of exactly what it removes"
```

---

### Task 4: Run the sweep even when automation is paused

**Files:**
- Modify: `ui/backend/email_trigger.py` (the `poll_once` tail, ~line 1243; and
  `poll_forever`, ~line 1267)
- Test: `tests/test_email_trigger.py` (extend)

**Interfaces:**
- Consumes: Task 3's `sweep_retention`.
- Produces: `run_maintenance(db) -> None`, `maintenance_once(session_factory=None) -> None`.

**Why:** `poll_forever` `continue`s past the whole cycle when
`BESTTEAM_TRIGGERS_DISABLED=1`. Pausing *automation* platform-wide must not
also pause *data deletion* — those are not the same decision.

- [ ] **Step 1: Write the failing tests**

```python
def test_maintenance_runs_the_retention_sweep(db_session, monkeypatch):
    from ui.backend.db.retention import get_retention_settings, set_retention_days
    from ui.backend.email_trigger import run_maintenance

    org = create_org(db_session, "acme")
    set_retention_days(db_session, org.id, 30)
    db_session.commit()

    run_maintenance(db_session)

    assert get_retention_settings(db_session, org.id).last_swept_at is not None


def test_maintenance_survives_a_failing_sweep(db_session, monkeypatch):
    """The poll loop must outlive any one maintenance job."""
    import ui.backend.email_trigger as et

    def boom(*a, **k):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(et, "sweep_retention", boom)
    et.run_maintenance(db_session)  # must not raise
```

- [ ] **Step 2: Run and watch them fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py -k maintenance -v`
Expected: FAIL — `cannot import name 'run_maintenance'`.

- [ ] **Step 3: Extract the tail**

In `ui/backend/email_trigger.py`, add `from .retention import sweep_retention`
to the imports and replace the `poll_once` tail's inline try/except with:

```python
def run_maintenance(db: Session) -> None:
    """Timer-driven upkeep: secret-expiry warnings, the retention sweep, and
    notification delivery.

    Piggy-backs on the poller because it already runs on a timer. Never raises:
    the poll loop must outlive any one of these.
    """
    try:
        sweep_secret_expiry(db)
        sweep_retention(db)
        dispatch_pending(db)
    except Exception:  # noqa: BLE001 -- upkeep must never break polling
        db.rollback()
        _logger.exception("email trigger: maintenance failed")


def maintenance_once(session_factory=None) -> None:
    """`run_maintenance` with its own session, for the paused branch of
    `poll_forever` -- retention is not part of what a trigger pause pauses."""
    factory = session_factory or SessionLocal
    with factory() as db:
        run_maintenance(db)
```

`poll_once`'s tail becomes a single `run_maintenance(db)` call.

In `poll_forever`, replace:

```python
        if triggers_disabled():
            continue
```

with:

```python
        if triggers_disabled():
            # A platform-wide pause of AUTOMATION is not a pause of data
            # deletion -- an org's retention policy keeps running.
            try:
                await asyncio.to_thread(maintenance_once)
            except Exception:  # noqa: BLE001 -- never let the task die
                _logger.exception("email trigger: maintenance cycle failed")
            continue
```

- [ ] **Step 4: Verify**

```
./.venv/Scripts/python.exe -m pytest tests/test_email_trigger.py tests/test_notifications.py tests/test_retention.py -v
```
Expected: PASS, including the pre-existing tests that assert
`sweep_secret_expiry`/`dispatch_pending` run from `poll_once`.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(retention): keep sweeping while automation is paused"
```

---

### Task 5: The org retention and export API

**Files:**
- Modify: `ui/backend/org_settings.py`
- Test: `tests/test_retention_api.py` (create)

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `GET/PUT /api/org/retention`, `POST /api/org/retention/purge`,
  `GET /api/org/export`.

**Note on the guard:** use `get_current_org`, the same dependency that already
governs `/api/org/email` (which connects and disconnects the mailbox). No new
role — there is one user per org today, and when that changes these routes move
with the mailbox routes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_retention_api.py`. Copy the fixture style from
`tests/test_org_settings.py` — it uses `client` (bearer token already set) and
`from helpers import create_user_and_login`; there is no `admin_headers`
fixture, and the import path is `helpers`, not `tests.helpers.auth`.

```python
"""Phase 3b: the retention/export HTTP surface."""

import pytest

pytestmark = pytest.mark.integration

...

def test_get_retention_defaults_to_off(client):
    body = client.get("/api/org/retention").json()
    assert body["run_retention_days"] is None
    assert body["last_swept_at"] is None
    assert body["purgeable_now"] == 0


def test_put_retention_round_trips(client):
    assert client.put("/api/org/retention", json={"run_retention_days": 30}).status_code == 200
    assert client.get("/api/org/retention").json()["run_retention_days"] == 30


def test_put_retention_rejects_out_of_range(client):
    assert client.put("/api/org/retention", json={"run_retention_days": 0}).status_code == 422
    assert client.put("/api/org/retention", json={"run_retention_days": 99999}).status_code == 422


def test_put_retention_null_turns_it_off(client):
    client.put("/api/org/retention", json={"run_retention_days": 30})
    assert client.put("/api/org/retention", json={"run_retention_days": None}).status_code == 200
    assert client.get("/api/org/retention").json()["run_retention_days"] is None


def test_purgeable_now_counts_what_the_policy_would_take(client, seeded_runs):
    client.put("/api/org/retention", json={"run_retention_days": 30})
    assert client.get("/api/org/retention").json()["purgeable_now"] == 1


def test_purge_requires_an_explicit_window(client):
    # No body at all: this is a destructive button and must say what it does.
    assert client.post("/api/org/retention/purge", json={}).status_code == 422


def test_purge_removes_and_reports(client, seeded_runs):
    body = client.post("/api/org/retention/purge", json={"older_than_days": 30}).json()
    assert body["purged"] == 1


def test_export_returns_an_attachment(client, seeded_runs):
    resp = client.get("/api/org/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.json()["truncated"] is False
    assert len(resp.json()["runs"]) >= 1


def test_export_respects_the_cap(client, seeded_runs, monkeypatch):
    monkeypatch.setenv("BESTTEAM_EXPORT_MAX_RUNS", "1")
    body = client.get("/api/org/export").json()
    assert body["truncated"] is True
    assert len(body["runs"]) == 1
```

`seeded_runs` is a fixture in this file that inserts one 40-day-old completed
run and one fresh one for the client's org, using the same helper shape as
Task 2's `_run`.

- [ ] **Step 2: Run and watch them fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention_api.py -v`
Expected: FAIL with 404s (routes do not exist).

- [ ] **Step 3: Implement the routes**

In `ui/backend/org_settings.py`:

```python
class RetentionRequest(BaseModel):
    """NULL means keep forever -- the default, so an upgrade deletes nothing."""

    run_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)


class PurgeRequest(BaseModel):
    """`older_than_days` is required on purpose: a destructive action whose
    body says what it will remove is the difference between a confirmed action
    and a slip. 0 means everything terminal."""

    older_than_days: int = Field(ge=0, le=3650)


@router.get("/retention")
def get_retention(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    row = get_retention_settings(db, org.id)
    days = row.run_retention_days if row else None
    return {
        "run_retention_days": days,
        "last_swept_at": iso_utc(row.last_swept_at) if row and row.last_swept_at else None,
        "last_purged_count": row.last_purged_count if row else 0,
        # What saving this setting would remove on the next sweep -- the number
        # that makes it safe to press save.
        "purgeable_now": (
            0 if days is None
            else purgeable_run_count(db, org_id=org.id, older_than_days=days)
        ),
    }


@router.put("/retention")
def put_retention(
    req: RetentionRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    set_retention_days(db, org.id, req.run_retention_days)
    db.commit()
    return get_retention(db=db, org=org)


@router.post("/retention/purge")
def purge_retention(
    req: PurgeRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    purged = purge_org_runs(db, org_id=org.id, older_than_days=req.older_than_days)
    db.commit()
    return {"purged": purged}


@router.get("/export")
def export_org(
    days: Optional[int] = None,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> JSONResponse:
    """The org's run history as JSON, so a customer can take their data out
    before a retention policy removes it."""
    bundle = export_org_runs(db, org_id=org.id, days=days)
    filename = f"bestteam-export-org-{org.id}.json"
    return JSONResponse(
        content=bundle,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

Add `purgeable_run_count(db, *, org_id, older_than_days, now=None) -> int` to
`ui/backend/retention.py` — the same query as `purge_org_runs` but `.count()`
and no mutation. Factor the filter into one private `_purgeable_query` used by
both, so the preview can never disagree with what the purge does.

`iso_utc` is already imported in this codebase (`ui/backend/main.py` uses it);
import it from wherever it lives rather than re-implementing.

- [ ] **Step 4: Verify**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention_api.py tests/test_org_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/org_settings.py ui/backend/retention.py tests/test_retention_api.py
git commit -m "feat(retention): org retention settings, immediate purge, and export"
```

---

### Task 6: Purge a single run

**Files:**
- Modify: `ui/backend/main.py` (beside `POST /api/runs/{run_id}/retry`, ~line 672)
- Test: `tests/test_retention_api.py` (extend)

**Interfaces:**
- Consumes: Task 2's `purge_run`.
- Produces: `POST /api/runs/{run_id}/purge`.

- [ ] **Step 1: Write the failing tests**

```python
def test_purge_one_run(client, seeded_runs):
    assert client.post(f"/api/runs/{seeded_runs['old']}/purge").json() == {"purged": True}


def test_purging_twice_is_not_an_error(client, seeded_runs):
    client.post(f"/api/runs/{seeded_runs['old']}/purge")
    assert client.post(f"/api/runs/{seeded_runs['old']}/purge").json() == {"purged": False}


def test_purge_another_orgs_run_is_404(client, other_org_run):
    assert client.post(f"/api/runs/{other_org_run}/purge").status_code == 404


def test_purge_a_running_run_is_409(client, running_run):
    assert client.post(f"/api/runs/{running_run}/purge").status_code == 409
```

- [ ] **Step 2: Run and watch them fail**

Expected: FAIL with 404 on the route itself.

- [ ] **Step 3: Implement**

```python
@app.post("/api/runs/{run_id}/purge")
def purge_run_content(
    run_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Remove one run's content on request (Phase 3b). The run row, its usage
    records and its automation results' status/source_key survive -- see
    ui/backend/retention.py. Cross-org is a 404 like every other run route:
    existence is not revealed."""
    run_row = db.get(Run, run_id)
    if run_row is None or run_row.org_id != org.id:
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    if run_row.status == "running":
        raise HTTPException(
            status_code=409,
            detail="This run is still going. Wait for it to finish, or cancel it first.",
        )
    purged = retention.purge_run(db, run_row)
    db.commit()
    return {"purged": purged}
```

- [ ] **Step 4: Verify**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_retention_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/main.py tests/test_retention_api.py
git commit -m "feat(retention): delete one run's content on request"
```

---

### Task 7: The Data tab

**Files:**
- Modify: `ui/frontend/src/lib/types.ts`, `ui/frontend/src/lib/api.ts`
- Create: `ui/frontend/src/components/DataRetentionPanel.tsx`
- Create: `ui/frontend/src/components/DataRetentionPanel.test.tsx`
- Modify: `ui/frontend/src/pages/ActivityPage.tsx:139-163`

**Interfaces:**
- Consumes: Task 5's routes.
- Produces: `api.getRetention()`, `api.setRetention(days)`,
  `api.purgeRuns(olderThanDays)`, `api.exportOrgData(days?)`,
  `api.purgeRun(runId)`.

- [ ] **Step 1: Write the failing component test**

`DataRetentionPanel.test.tsx`, following the shape of
`WebhookSettings.test.tsx` (mock `../lib/api`, `render`, `await screen.findBy…`):

```tsx
it('shows the policy as off by default', async () => { … expect(await screen.findByText(/kept forever/i)).toBeInTheDocument() })
it('saves a retention period', async () => { … expect(api.setRetention).toHaveBeenCalledWith(30) })
it('warns how many runs a new period would remove', async () => { … /* purgeable_now */ })
it('requires typing DELETE before purging', async () => { … expect(api.purgeRuns).not.toHaveBeenCalled() })
it('purges after the confirmation is typed', async () => { … expect(api.purgeRuns).toHaveBeenCalledWith(30) })
it('shows when the last cleanup ran', async () => { … })
it('surfaces an export failure', async () => { … })
```

- [ ] **Step 2: Run and watch it fail**

Run: `cd ui/frontend && npm test -- DataRetentionPanel`
Expected: FAIL — module not found.

- [ ] **Step 3: Add the types and API helpers**

```ts
export interface RetentionSettings {
  run_retention_days: number | null
  last_swept_at: string | null
  last_purged_count: number
  purgeable_now: number
}
```

`api.exportOrgData` fetches the JSON and triggers a browser download via an
object URL — this is the real app, not a sandboxed artifact, so a
`<a download>` works.

- [ ] **Step 4: Write `DataRetentionPanel.tsx`**

Requirements, in the customer-facing register the rest of the wizard uses (no
jargon, say what is kept and what goes):

- A period selector: *Keep forever* (default) / 30 / 90 / 180 / 365 days.
- When a period is selected but not yet saved, show `purgeable_now`: "Saving
  this will remove the content of N past runs."
- Copy stating plainly what a purge does: **"We remove what was in the email —
  the message text, the reply we drafted and the step-by-step trace. We keep
  that the run happened, when, and what it cost."**
- "Download export" button, and copy telling the customer to export before
  turning retention on.
- "Delete now" behind a typed `DELETE` confirmation, using the currently
  selected period.
- "Last cleanup: <time>, removed N runs" when `last_swept_at` is set.
- Fetch-on-mount uses the same explicit
  `// eslint-disable-next-line react-hooks/set-state-in-effect` that
  `NotificationsPanel.tsx:51` uses — `npm run lint` is a CI step and this rule
  fails it otherwise.

- [ ] **Step 5: Add the tab**

In `ActivityPage.tsx`, add a fifth button (`tab === 'data'`) after Alerts, and
render `<DataRetentionPanel />` for it. Extend the `tab` union type.

- [ ] **Step 6: Verify**

```
cd ui/frontend && npm test && npx tsc --noEmit && npm run lint && npm run build
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add ui/frontend/src
git commit -m "feat(ui): a Data tab for retention, export and immediate deletion"
```

---

### Task 8: Show a purged run honestly

**Files:**
- Modify: `ui/frontend/src/components/RunDetail.tsx`
- Modify: `ui/frontend/src/components/RunDetail.test.tsx`
- Modify: `ui/frontend/src/components/NeedsAttentionList.tsx`
- Modify: `ui/backend/main.py` (`GET /api/runs/{id}/trace` response)
- Test: `tests/test_retention_api.py` (extend)

**Why:** a purged run currently renders as an empty timeline, which reads as a
bug. The customer must see that *they* removed it.

**Interfaces:**
- Consumes: Task 6's route; `Run.content_purged_at`.
- Produces: `content_purged_at` on the `GET /api/runs/{id}/trace` response.

- [ ] **Step 1: Write the failing tests**

Backend:

```python
def test_trace_reports_a_purged_run(client, seeded_runs):
    client.post(f"/api/runs/{seeded_runs['old']}/purge")
    body = client.get(f"/api/runs/{seeded_runs['old']}/trace").json()
    assert body["events"] == []
    assert body["content_purged_at"] is not None
```

Frontend, in `RunDetail.test.tsx`:

```tsx
it('says the content was removed rather than showing an empty timeline', async () => {
  // trace returns { events: [], usage: [], content_purged_at: '2026-08-17T00:00:00Z' }
  expect(await screen.findByText(/content was removed/i)).toBeInTheDocument()
})

it('offers to delete this run\'s content', async () => {
  … expect(api.purgeRun).toHaveBeenCalledWith('r1')
})
```

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Implement**

- `GET /api/runs/{run_id}/trace` gains
  `"content_purged_at": iso_utc(run.content_purged_at) if run.content_purged_at else None`.
- `useRunTrace` passes it through (check `ui/frontend/src/lib/useRunTrace.ts`
  — extend its return shape rather than adding a second fetch).
- `RunDetail` renders, in place of the timeline: *"The content of this run was
  removed on <date> by your data retention settings. What it cost and when it
  ran are still on record."* plus a "Delete this run's content" button for
  unpurged terminal runs, behind a single confirm.
- `NeedsAttentionList` renders an item with an empty `payload` as "Content
  removed" rather than blank fields.

- [ ] **Step 4: Verify**

```
./.venv/Scripts/python.exe -m pytest tests/test_retention_api.py tests/test_trace_granularity.py -v
cd ui/frontend && npm test && npx tsc --noEmit && npm run lint
```

- [ ] **Step 5: Commit**

```bash
git add ui/backend/main.py ui/frontend/src tests/test_retention_api.py
git commit -m "feat(ui): show a purged run as removed, not as empty"
```

---

### Task 9: Document it, and verify the whole thing

**Files:**
- Modify: `docs/STATUS.md`, `docs/DECISIONS.md`, `docs/deployment.md`,
  `.env.example`, `CLAUDE.md`, `ui/backend/CLAUDE.md`,
  `ui/backend/db/CLAUDE.md`, `ui/frontend/CLAUDE.md`

- [ ] **Step 1: Replace the known issue**

In `docs/STATUS.md`, the bullet **"Generic email runs have no retention policy
for their output"** is now fixed — move it to Done with the design's summary,
and add the honest replacements:

- Per-data-subject erasure is not possible: the address is not stored anywhere
  indexed, only inside free text the model may have paraphrased.
- `inbox_events` is never purged (UID + the customer's own mailbox address, no
  data-subject content) so the detection ledger grows without bound.
- Visitor share transcripts still have no retention story — separate issue,
  separate data subject.
- A purge is not a secure erase: SQLite leaves the old page contents on disk
  until `VACUUM`.

- [ ] **Step 2: Add the decisions**

`docs/DECISIONS.md`: (a) why a purge keeps the run row and `usage_records`;
(b) why retention covers all of an org's runs rather than only email-triggered
ones; (c) why per-data-subject erasure was refused rather than approximated.

- [ ] **Step 3: Document the operator surface**

`docs/deployment.md`: a "Keeping and removing run history" section — what the
policy covers, what a purge keeps, where the customer finds it, and the two env
vars. `.env.example`: `BESTTEAM_RUN_RETENTION_DAYS` (default for newly created
orgs only) and `BESTTEAM_EXPORT_MAX_RUNS` (default 5000), each with the same
commented explanation style as the existing block.

- [ ] **Step 4: Update the CLAUDE.md files**

Root `CLAUDE.md`'s "Known limitations" list, `ui/backend/CLAUDE.md`
(`retention.py`, the maintenance tail, the four routes),
`ui/backend/db/CLAUDE.md` (`org_retention_settings`, `runs.content_purged_at`),
`ui/frontend/CLAUDE.md` (the Data tab).

- [ ] **Step 5: Full verification — this is the gate**

```
./.venv/Scripts/python.exe -m pytest -m "not e2e"      # SERIAL, no -n auto
cd ui/frontend && npm test && npx tsc --noEmit && npm run lint && npm run build
./.venv/Scripts/python.exe -m alembic heads            # exactly one head
```

Serial and in one process on purpose: that is `backend-full`'s configuration
and it is what catches ordering and cross-test isolation bugs. Expected: 1595 +
the new tests passing, 0 failures. Do not report completion on a partial run.

- [ ] **Step 6: Commit and push**

```bash
git add -A
git commit -m "docs: record retention, and what it deliberately does not cover"
git push
```

PR #64 already exists for this branch; the push updates it.

---

## Self-Review

**Spec coverage:** 3b.1 → Tasks 1-4 (policy + sweep). 3b.2 → Tasks 5-6 (batch
and single-run purge). 3b.3 → Task 3/5 (export). 3b.4 → Task 1
(`last_swept_at`) surfaced in Tasks 5 and 7. Invariants I1-I5 → Task 2's tests
one-for-one. "Purge content, keep the row" → Task 2. "All runs, not email-only"
→ `purge_org_runs` filters on `org_id` alone. Export/purge coupling →
`PURGED_FIELDS` + Task 3's test. Sweep-while-paused → Task 4. Out-of-scope
items appear in Task 9's documentation, not in code.

**Placeholders:** the frontend tasks (7, 8) give requirements and test names
rather than full component source. That is deliberate and bounded — the
components are ordinary forms whose shape is fully specified by the listed
tests, the named sibling files to copy (`WebhookSettings.tsx`,
`NotificationsPanel.tsx`), and the exact copy strings. No backend step is left
to interpretation.

**Type consistency:** `purge_run(db, run) -> bool`, `purge_org_runs(db, *,
org_id, older_than_days, now=None) -> int`, `purgeable_run_count` (same
keywords), `sweep_retention(db, *, now=None) -> int`, `export_org_runs(db, *,
org_id, days=None, limit=None) -> dict` are used with those exact signatures in
Tasks 4, 5 and 6. `run_retention_days` is the field name in the model, the
Pydantic body, the JSON response and the TypeScript interface. `content_purged_at`
is the column, the trace-response key and the frontend field.
