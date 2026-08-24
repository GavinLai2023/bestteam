# KB Generation Audit Retention + Restore Previous Upload — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A knowledge-base generation that a run's trace references is never pruned while that trace exists, and a customer can restore the previous upload of a collection at no embedding cost.

**Architecture:** A link table `run_knowledge_generations` (run → ingestion job) is written by `runtime.py` from each KB `tool_completed` event; `ingestion._prune_old_ingestion_versions` keeps a referenced old generation's rows (vectors nulled, directory deleted) and `retention.purge_run` / KB deletion release the reference. "Restore previous upload" is PR #87's single-document-removal machinery with the *previous* completed job as the staging source and shape, plus a two-job lookup in `_reusable_documents` so every chunk is carried forward.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + Alembic (SQLite), pytest (`fake:` embeddings, `make_concurrent_safe_engine`), React + Vite + Vitest.

**Spec:** `docs/superpowers/specs/2026-08-24-kb-generation-audit-retention-and-restore-design.md`

## Global Constraints

- Run every Python command through the venv: `.\.venv\Scripts\python.exe` (Windows). Frontend commands from `ui/frontend`.
- Every new test file needs `pytestmark = pytest.mark.integration` (or `unit`) — `tests/test_marker_completeness.py` fails the suite otherwise.
- `db/` CRUD functions **never commit**; callers own the transaction.
- Migration revision id `s6t7u8v9w0x1`, `down_revision = 'r5s6t7u8v9w0'`. Guard every op by inspection (the backend's `create_all` at import may already have created the table).
- Customer-facing surfaces (`/api/org/*`, `KnowledgeBasesPanel`) show **no model names and no cost figures**.
- Cross-org access is a `404`, never a `403`.
- Code comments in English. British spelling in prose.
- `KnowledgeBasesPanel.tsx` is currently English-only literals (the F1 i18n long tail has not reached it). New copy follows the panel's existing convention — English literals — so the panel stays consistent; translating the panel as a whole is F1 work. (Deviation from the spec's "English and Chinese strings", flagged at handoff.)
- Branch: `feat/kb-generation-audit-and-restore` (already created from `main` at `8ab585c`; the spec is committed as `4e9609b`).

---

## File Structure

| File | Responsibility |
|---|---|
| `ui/backend/db/models.py` | + `RunKnowledgeGeneration` model |
| `alembic/versions/s6t7u8v9w0x1_run_knowledge_generations.py` | create the table (guarded) |
| `ui/backend/db/run_knowledge_generations.py` (new) | `record`, `referenced_job_ids`, `delete_for_run`, `delete_for_jobs` |
| `ui/backend/runtime.py` | write the reference from KB `tool_completed` events |
| `ui/backend/ingestion.py` | prune by reference; two-job `_reusable_documents`; KB delete drops links |
| `ui/backend/retention.py` | `purge_run` drops the run's links |
| `ui/backend/knowledge_bases.py` | `_stage_previous_generation(source=)`, `restorable_generation`, `restore_previous_generation` |
| `ui/backend/org_knowledge_bases.py` | `POST /knowledge-bases/{name}/restore`; `previous_generation` in `_kb_summary` |
| `ui/frontend/src/lib/types.ts`, `lib/api.ts` | `previous_generation` field; `restoreOwnKnowledgeBase` |
| `ui/frontend/src/components/KnowledgeBasesPanel.tsx` (+ `.test.tsx`) | "Restore previous upload" button + confirm |
| `tests/test_run_knowledge_generations.py` (new) | CRUD + runtime write tests |
| `tests/test_ingestion.py`, `tests/test_retention.py`, `tests/test_org_knowledge_bases.py` | prune / reuse / purge / restore tests |
| Docs: `ui/backend/CLAUDE.md`, `ui/backend/db/CLAUDE.md`, `ui/frontend/CLAUDE.md`, `docs/KNOWLEDGE_BASES.md`, `docs/STATUS.md`, `CHANGELOG.md`, root `CLAUDE.md` | |

---

### Task 1: Model, migration and CRUD for `run_knowledge_generations`

**Files:**
- Modify: `ui/backend/db/models.py` (append after `TraceEventRecord`, ~line 678)
- Create: `alembic/versions/s6t7u8v9w0x1_run_knowledge_generations.py`
- Create: `ui/backend/db/run_knowledge_generations.py`
- Create: `tests/test_run_knowledge_generations.py`

**Interfaces:**
- Produces: `RunKnowledgeGeneration` ORM class; `record(db, run_id: str, ingestion_job_id: int) -> None`; `referenced_job_ids(db, job_ids: Iterable[int]) -> set[int]`; `delete_for_run(db, run_id: str) -> None`; `delete_for_jobs(db, job_ids: Iterable[int]) -> None`. None commit.

- [ ] **Step 1: Write the failing CRUD tests**

Create `tests/test_run_knowledge_generations.py`:

```python
"""The run -> knowledge-base-generation reference (`run_knowledge_generations`):
what keeps an old generation's rows alive while a trace still names them.
See docs/superpowers/specs/2026-08-24-kb-generation-audit-retention-and-restore-design.md."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord, Run, RunKnowledgeGeneration
from ui.backend.db.run_knowledge_generations import (
    delete_for_jobs,
    delete_for_run,
    record,
    referenced_job_ids,
)


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def _job(db, kb, version):
    job = IngestionJob(kb_id=kb.id, org_id=1, version=version, status="completed", file_count=1)
    db.add(job)
    db.flush()
    return job


def _fixture(db):
    kb = KnowledgeBaseRecord(name="policies", org_id=1, config={"name": "policies", "type": "local_folder", "path": "x"})
    db.add(kb)
    db.flush()
    job1, job2 = _job(db, kb, "v1"), _job(db, kb, "v2")
    db.add(Run(id="r1", pipeline="wf", input="in", status="completed", org_id=1))
    db.add(Run(id="r2", pipeline="wf", input="in", status="completed", org_id=1))
    db.flush()
    return kb, job1, job2


def test_record_is_one_row_per_run_and_generation(db):
    _, job1, _ = _fixture(db)

    record(db, "r1", job1.id)
    record(db, "r1", job1.id)  # the agent searched the same collection twice
    db.commit()

    rows = db.query(RunKnowledgeGeneration).all()
    assert [(r.run_id, r.ingestion_job_id) for r in rows] == [("r1", job1.id)]


def test_referenced_job_ids_answers_which_of_these_jobs_a_run_names(db):
    _, job1, job2 = _fixture(db)
    record(db, "r1", job1.id)
    db.commit()

    assert referenced_job_ids(db, [job1.id, job2.id]) == {job1.id}
    assert referenced_job_ids(db, [job2.id]) == set()
    assert referenced_job_ids(db, []) == set()


def test_delete_for_run_releases_only_that_runs_references(db):
    _, job1, _ = _fixture(db)
    record(db, "r1", job1.id)
    record(db, "r2", job1.id)
    db.commit()

    delete_for_run(db, "r1")
    db.commit()

    assert referenced_job_ids(db, [job1.id]) == {job1.id}  # r2 still holds it
    delete_for_run(db, "r2")
    db.commit()
    assert referenced_job_ids(db, [job1.id]) == set()


def test_delete_for_jobs_drops_every_reference_to_those_jobs(db):
    _, job1, job2 = _fixture(db)
    record(db, "r1", job1.id)
    record(db, "r1", job2.id)
    db.commit()

    delete_for_jobs(db, [job1.id])
    db.commit()

    assert {r.ingestion_job_id for r in db.query(RunKnowledgeGeneration)} == {job2.id}
    delete_for_jobs(db, [])  # no-op, must not raise
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_knowledge_generations.py -q`
Expected: FAIL — `ImportError: cannot import name 'RunKnowledgeGeneration'`.

- [ ] **Step 3: Add the model**

In `ui/backend/db/models.py`, directly after the `TraceEventRecord` class (before `class UsageRecord`):

```python
class RunKnowledgeGeneration(Base):
    """One run's reference to one knowledge-base generation it searched.

    Written by `runtime.run_in_background` from a KB tool's `tool_completed`
    event (which carries `ingestion_job_id` and per-hit `chunk_id`s), so the
    ids that trace records keep resolving: `ingestion._prune_old_ingestion_versions`
    keeps a referenced generation's document/chunk rows (vectors nulled, files
    deleted) instead of deleting them. Released by `retention.purge_run` (the
    trace is gone) and by KB deletion. A materialised reference, the same idea
    as `pipeline_dependencies`. See
    docs/superpowers/specs/2026-08-24-kb-generation-audit-retention-and-restore-design.md.
    """

    __tablename__ = "run_knowledge_generations"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "ingestion_job_id",
            name="uq_run_knowledge_generations_run_id_job_id",
        ),
        Index("ix_run_knowledge_generations_ingestion_job_id", "ingestion_job_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    ingestion_job_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_ingestion_jobs.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

- [ ] **Step 4: Add the CRUD module**

Create `ui/backend/db/run_knowledge_generations.py`:

```python
"""CRUD for `run_knowledge_generations`: a run's reference to a knowledge-base
generation its trace names. Nothing here commits -- callers own the
transaction, like every other module in this package."""
from __future__ import annotations

from typing import Iterable

from sqlalchemy.orm import Session

from .models import RunKnowledgeGeneration


def record(db: Session, run_id: str, ingestion_job_id: int) -> None:
    """Insert the reference unless it already exists. One row per (run,
    generation) however many times the run searched that collection."""
    exists = (
        db.query(RunKnowledgeGeneration.id)
        .filter_by(run_id=run_id, ingestion_job_id=ingestion_job_id)
        .first()
    )
    if exists is None:
        db.add(RunKnowledgeGeneration(run_id=run_id, ingestion_job_id=ingestion_job_id))


def referenced_job_ids(db: Session, job_ids: Iterable[int]) -> set[int]:
    """Which of `job_ids` some run still references."""
    ids = list(job_ids)
    if not ids:
        return set()
    rows = (
        db.query(RunKnowledgeGeneration.ingestion_job_id)
        .filter(RunKnowledgeGeneration.ingestion_job_id.in_(ids))
        .distinct()
        .all()
    )
    return {job_id for (job_id,) in rows}


def delete_for_run(db: Session, run_id: str) -> None:
    """Release every reference this run holds (its trace is being purged)."""
    db.query(RunKnowledgeGeneration).filter_by(run_id=run_id).delete(synchronize_session=False)


def delete_for_jobs(db: Session, job_ids: Iterable[int]) -> None:
    """Drop every reference to these jobs (the knowledge base is being deleted)."""
    ids = list(job_ids)
    if not ids:
        return
    db.query(RunKnowledgeGeneration).filter(
        RunKnowledgeGeneration.ingestion_job_id.in_(ids)
    ).delete(synchronize_session=False)
```

- [ ] **Step 5: Add the migration**

Create `alembic/versions/s6t7u8v9w0x1_run_knowledge_generations.py`:

```python
"""run_knowledge_generations: which KB generation a run's trace names

Revision ID: s6t7u8v9w0x1
Revises: r5s6t7u8v9w0
Create Date: 2026-08-24 00:00:00.000000

A knowledge-base search leaves `ingestion_job_id` and per-hit `chunk_id`s in
the run's trace (PR #86), and generation pruning deleted those rows two
uploads later. This table is the reference that stops the prune: a
generation some un-purged run names keeps its document/chunk rows (vectors
nulled, files deleted). No backfill -- generations already pruned are gone;
from here on a referenced one is never pruned while its reference stands.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 's6t7u8v9w0x1'
down_revision: Union[str, Sequence[str], None] = 'r5s6t7u8v9w0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "run_knowledge_generations"


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
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "ingestion_job_id", sa.Integer(),
            sa.ForeignKey("knowledge_ingestion_jobs.id"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "run_id", "ingestion_job_id",
            name="uq_run_knowledge_generations_run_id_job_id",
        ),
    )
    op.create_index(
        "ix_run_knowledge_generations_ingestion_job_id", _TABLE, ["ingestion_job_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_run_knowledge_generations_ingestion_job_id", table_name=_TABLE)
    op.drop_table(_TABLE)
```

- [ ] **Step 6: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_knowledge_generations.py -q`
Expected: 4 passed.

Also confirm the alembic chain is linear: `.\.venv\Scripts\python.exe -m alembic heads` → exactly one head, `s6t7u8v9w0x1`.

- [ ] **Step 7: Commit**

```bash
git add ui/backend/db/models.py ui/backend/db/run_knowledge_generations.py alembic/versions/s6t7u8v9w0x1_run_knowledge_generations.py tests/test_run_knowledge_generations.py
git commit -m "feat(kb): run_knowledge_generations -- which KB generation a run's trace names

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `runtime.py` records the reference from KB `tool_completed` events

**Files:**
- Modify: `ui/backend/runtime.py` — imports (~line 39–42), local sets (~line 689–690), the event loop after the email `tool_completed` checks (~line 880), and a new `_safe_record_knowledge_generation` helper next to `_safe_record_trace_event` (~line 441)
- Test: `tests/test_run_knowledge_generations.py` (append)

**Interfaces:**
- Consumes: `db.run_knowledge_generations.record`.
- Produces: `_safe_record_knowledge_generation(db, *, run_id: str, ingestion_job_id: int) -> None`.

- [ ] **Step 1: Write the failing runtime tests**

Append to `tests/test_run_knowledge_generations.py`:

```python
# --- runtime writes the reference ----------------------------------------------

from bestteam.core.trace import TraceEvent
from helpers import make_concurrent_safe_engine
from ui.backend import runtime
from ui.backend.runtime import registry, run_in_background


def _kb_search_event(job_id):
    return TraceEvent(
        type="tool_completed", pipeline="wf", agent="a",
        data={"tool": "policies", "success": True, "summary": "1 result",
              "query": "refunds", "hit_count": 1, "sources": ["a.txt"],
              "ingestion_job_id": job_id, "hits": []},
    )


class _SearchesTwicePipeline:
    name = "wf"

    def __init__(self, job_id):
        self.job_id = job_id

    def stream(self, *args, **kwargs):
        yield TraceEvent(type="run_started", pipeline="wf", data=None)
        yield _kb_search_event(self.job_id)
        yield _kb_search_event(self.job_id)
        # A folder-built collection reports no generation.
        yield _kb_search_event(None)
        yield TraceEvent(type="run_completed", pipeline="wf", data="done")


@pytest.fixture
def file_engine(tmp_path):
    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    return engine


def _seed_job(engine):
    with session_factory(engine)() as db:
        kb = KnowledgeBaseRecord(name="policies", org_id=1, config={"name": "policies", "type": "local_folder", "path": "x"})
        db.add(kb)
        db.flush()
        job = IngestionJob(kb_id=kb.id, org_id=1, version="v1", status="completed", file_count=1)
        db.add(job)
        db.commit()
        return job.id


def test_run_in_background_records_each_generation_once(file_engine):
    job_id = _seed_job(file_engine)
    run = registry.create("wf", "in", org_id=1)

    run_in_background(run.id, _SearchesTwicePipeline(job_id), "in", file_engine, org_id=1)

    with session_factory(file_engine)() as db:
        rows = db.query(RunKnowledgeGeneration).filter_by(run_id=run.id).all()
    assert [r.ingestion_job_id for r in rows] == [job_id]
    assert registry.get(run.id).status == "completed"


def test_a_failed_reference_write_never_fails_the_run(file_engine, monkeypatch):
    job_id = _seed_job(file_engine)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(runtime, "record_knowledge_generation", _boom)
    run = registry.create("wf", "in", org_id=1)

    run_in_background(run.id, _SearchesTwicePipeline(job_id), "in", file_engine, org_id=1)

    assert registry.get(run.id).status == "completed"
    with session_factory(file_engine)() as db:
        assert db.query(RunKnowledgeGeneration).count() == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_knowledge_generations.py -q`
Expected: the first new test FAILS (`[] == [job_id]`), the second FAILS with `AttributeError: ... has no attribute 'record_knowledge_generation'`.

- [ ] **Step 3: Implement**

In `ui/backend/runtime.py`, after `from .db.models import InboxEvent, Run, TraceEventRecord`:

```python
from .db.run_knowledge_generations import record as record_knowledge_generation
```

After `_safe_record_trace_event` (before `_SHARE_REPLY_MAX_ATTEMPTS`):

```python
def _safe_record_knowledge_generation(db: Session, *, run_id: str, ingestion_job_id: int) -> None:
    """Persist "this run's trace names KB generation `ingestion_job_id`" --
    what stops `ingestion._prune_old_ingestion_versions` deleting the rows the
    trace's chunk ids point at. Written the moment the search event arrives,
    not at the terminal event: a run cancelled or crashed afterwards has still
    read that generation. Isolated like `_safe_record_usage` -- an audit record
    failing must never fail the run."""
    try:
        record_knowledge_generation(db, run_id, ingestion_job_id)
        db.commit()
    except Exception:  # noqa: BLE001 -- bookkeeping must never break a run
        _logger.warning(
            "Knowledge generation reference failed for run %s; run unaffected",
            run_id, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
```

Next to the two local sets (`confirmed_draft_message_ids` / `failed_tool_message_ids`, ~line 689):

```python
    # Generations already referenced by this run, so one row per collection
    # searched however many times the agent searched it.
    referenced_generation_ids: set[int] = set()
```

In the event loop, immediately after the `failed_tool_message_ids` block (the `if event.type == "tool_completed" ... failed_tool_message_ids.add(message_id)` block, before `if event.type in ("run_completed", "run_failed"):`):

```python
                if (
                    db is not None
                    and event.type == "tool_completed"
                    and isinstance(event.data, dict)
                    and event.data.get("ingestion_job_id") is not None
                    and event.data["ingestion_job_id"] not in referenced_generation_ids
                ):
                    # A knowledge-base search: its trace carries chunk ids from
                    # this generation, so record the reference that keeps those
                    # rows alive (see db/run_knowledge_generations.py). A
                    # folder-built collection reports None and needs nothing.
                    referenced_generation_ids.add(event.data["ingestion_job_id"])
                    _safe_record_knowledge_generation(
                        db, run_id=run_id, ingestion_job_id=event.data["ingestion_job_id"]
                    )
```

Note `db` here is the worker-thread `Session` the loop already uses for `_safe_record_trace_event` (it is `None` when no engine was given); use the same variable name the surrounding code uses.

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_knowledge_generations.py tests/test_run_lifecycle.py tests/test_usage_metering.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/runtime.py tests/test_run_knowledge_generations.py
git commit -m "feat(runtime): record which KB generation a run searched

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Prune by reference; `_reusable_documents` looks at the newest two jobs; KB delete drops links

**Files:**
- Modify: `ui/backend/ingestion.py` — imports (~line 43), `_reusable_documents` (342–396), `_prune_old_ingestion_versions` (473–506), `delete_kb_ingestion_data` (556–563)
- Test: `tests/test_ingestion.py` (append)

**Interfaces:**
- Consumes: `referenced_job_ids`, `delete_for_jobs`.
- Produces: unchanged signatures; `_reusable_documents` now merges the newest `_KEEP_COMPLETED_GENERATIONS` completed jobs, newest first.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingestion.py`:

```python
# --- Generations a run's trace references are kept, not pruned ----------------

from ui.backend.db.models import Run, RunKnowledgeGeneration
from ui.backend.db.run_knowledge_generations import record as record_generation


def _completed_generation(db, kb, tmp_path, version, filename="doc.txt", embedding='[0.1, 0.2]'):
    """One completed job with one chunked document (and a vector, so the
    prune's 'vectors nulled' branch is observable) and its version directory."""
    job = IngestionJob(
        kb_id=kb.id, org_id=1, version=version, status="completed", file_count=1,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100,
    )
    db.add(job)
    db.flush()
    doc = KnowledgeDocument(
        kb_id=kb.id, ingestion_job_id=job.id, filename=filename,
        content_hash=f"hash-{version}-{filename}", size_bytes=10, status="chunked",
    )
    db.add(doc)
    db.flush()
    db.add(KnowledgeChunk(
        document_id=doc.id, kb_id=kb.id, chunk_index=0, text=f"text of {version}",
        embedding_json=embedding,
    ))
    db.commit()
    (tmp_path / version).mkdir()
    return job


def _reference(db, job, run_id="r1"):
    if db.get(Run, run_id) is None:
        db.add(Run(id=run_id, pipeline="wf", input="in", status="completed", org_id=1))
        db.flush()
    record_generation(db, run_id, job.id)
    db.commit()


def test_prune_keeps_a_referenced_old_generations_rows_without_its_vectors_or_files(db, tmp_path):
    kb = _make_kb(db, name="audited")
    job1 = _completed_generation(db, kb, tmp_path, "v1")
    job2 = _completed_generation(db, kb, tmp_path, "v2")
    job3 = _completed_generation(db, kb, tmp_path, "v3")
    _reference(db, job1)

    ingestion._prune_old_ingestion_versions(db, kb.id, tmp_path)

    # job1 is outside the keep-2 window but a run's trace names it: rows stay.
    assert db.get(IngestionJob, job1.id) is not None
    docs = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job1.id).all()
    assert len(docs) == 1
    chunks = db.query(KnowledgeChunk).filter_by(document_id=docs[0].id).all()
    assert len(chunks) == 1 and chunks[0].text == "text of v1"
    # An audit resolves a chunk id to text, page, heading, filename -- never a
    # vector, which is the bulk of the storage.
    assert chunks[0].embedding_json is None
    assert not (tmp_path / "v1").exists()
    # The window itself is untouched.
    for job in (job2, job3):
        (chunk,) = db.query(KnowledgeChunk).join(KnowledgeDocument).filter(
            KnowledgeDocument.ingestion_job_id == job.id
        ).all()
        assert chunk.embedding_json == '[0.1, 0.2]'
        assert (tmp_path / job.version).is_dir()


def test_prune_is_idempotent_over_an_audit_only_generation(db, tmp_path):
    kb = _make_kb(db, name="audited_twice")
    job1 = _completed_generation(db, kb, tmp_path, "v1")
    _completed_generation(db, kb, tmp_path, "v2")
    _completed_generation(db, kb, tmp_path, "v3")
    _reference(db, job1)

    ingestion._prune_old_ingestion_versions(db, kb.id, tmp_path)
    ingestion._prune_old_ingestion_versions(db, kb.id, tmp_path)

    assert db.get(IngestionJob, job1.id) is not None
    assert db.query(KnowledgeDocument).filter_by(ingestion_job_id=job1.id).count() == 1


def test_an_unreferenced_old_generation_is_still_deleted(db, tmp_path):
    kb = _make_kb(db, name="unreferenced")
    job1 = _completed_generation(db, kb, tmp_path, "v1")
    _completed_generation(db, kb, tmp_path, "v2")
    _completed_generation(db, kb, tmp_path, "v3")

    ingestion._prune_old_ingestion_versions(db, kb.id, tmp_path)

    assert db.get(IngestionJob, job1.id) is None
    assert db.query(KnowledgeDocument).filter_by(ingestion_job_id=job1.id).count() == 0
    assert not (tmp_path / "v1").exists()


def test_a_released_reference_lets_the_next_prune_delete_the_generation(db, tmp_path):
    from ui.backend.db.run_knowledge_generations import delete_for_run

    kb = _make_kb(db, name="released")
    job1 = _completed_generation(db, kb, tmp_path, "v1")
    _completed_generation(db, kb, tmp_path, "v2")
    _completed_generation(db, kb, tmp_path, "v3")
    _reference(db, job1)
    ingestion._prune_old_ingestion_versions(db, kb.id, tmp_path)
    assert db.get(IngestionJob, job1.id) is not None

    delete_for_run(db, "r1")  # what retention.purge_run does
    db.commit()
    ingestion._prune_old_ingestion_versions(db, kb.id, tmp_path)

    assert db.get(IngestionJob, job1.id) is None


def test_reusable_documents_looks_at_the_newest_two_completed_jobs(db, tmp_path):
    # Restoring the previous upload stages the second-newest generation's
    # files; for that to cost nothing its chunks have to be reusable too.
    kb = _make_kb(db, name="two_jobs")
    _completed_generation(db, kb, tmp_path, "v1", filename="old.txt")
    job2 = _completed_generation(db, kb, tmp_path, "v2", filename="b.txt")
    job3 = _completed_generation(db, kb, tmp_path, "v3", filename="c.txt")
    new_job = IngestionJob(
        kb_id=kb.id, org_id=1, version="v4", status="queued", file_count=2,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100,
    )
    db.add(new_job)
    db.commit()

    reusable = ingestion._reusable_documents(db, kb.id, new_job)

    assert set(reusable) == {("b.txt", f"hash-v2-b.txt"), ("c.txt", f"hash-v3-c.txt")}
    assert reusable[("c.txt", "hash-v3-c.txt")][0].text == "text of v3"
    assert reusable[("b.txt", "hash-v2-b.txt")][0].text == "text of v2"
    # The third-newest job (audit-only, if it survives at all) is never a source.
    assert ("old.txt", "hash-v1-old.txt") not in reusable
    # A non-carryable job in the window contributes nothing.
    job2.chunk_size = 500
    db.commit()
    assert set(ingestion._reusable_documents(db, kb.id, new_job)) == {("c.txt", "hash-v3-c.txt")}
    del job3


def test_deleting_kb_ingestion_data_drops_its_generation_references(db, tmp_path):
    kb = _make_kb(db, name="deleted")
    job1 = _completed_generation(db, kb, tmp_path, "v1")
    _reference(db, job1)

    ingestion.delete_kb_ingestion_data(db, kb.id)
    db.commit()

    assert db.query(RunKnowledgeGeneration).count() == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ingestion.py -q -k "referenced or audit_only or unreferenced or released or newest_two or generation_references"`
Expected: `test_prune_keeps_...`, `test_prune_is_idempotent...`, `test_a_released_reference...`, `test_reusable_documents_looks_at...`, `test_deleting_kb_ingestion_data...` FAIL; `test_an_unreferenced_old_generation_is_still_deleted` passes already (today's behaviour).

- [ ] **Step 3: Implement**

In `ui/backend/ingestion.py`, after `from .db.models import ...`:

```python
from .db.run_knowledge_generations import delete_for_jobs, referenced_job_ids
```

Replace `_reusable_documents` (keep the signature) — the body from `previous = (` to the final `return`:

```python
    candidates = (
        db.query(IngestionJob)
        .filter(
            IngestionJob.kb_id == kb_id,
            IngestionJob.status == "completed",
            IngestionJob.id != job.id,
        )
        # `id`, not `completed_at` -- the same ordering rule the pruning
        # functions below spell out: completion order is not guaranteed to
        # match submission order.
        .order_by(IngestionJob.id.desc())
        # The keep window (the live generation and the one before it) -- the
        # only completed jobs whose chunks still carry vectors; an audit-only
        # generation outside it has had them nulled (`_prune_old_ingestion_versions`).
        # Looking at both is what makes restoring the previous upload free:
        # its files are staged again, and its chunks are found here.
        .limit(_KEEP_COMPLETED_GENERATIONS)
        .all()
    )
    result: Dict[Tuple[str, str], List[KnowledgeChunk]] = {}
    for previous in candidates:  # newest first, so the live job's copy wins
        if not _carryable(previous, job):
            continue
        documents = (
            db.query(KnowledgeDocument)
            .filter_by(ingestion_job_id=previous.id, status="chunked")
            .all()
        )
        if not documents:
            continue
        # One query for every chunk of every candidate document, rather than a
        # lazy load per document: a collection of thirty documents would
        # otherwise be thirty round-trips on the worker thread before any of the
        # work starts.
        by_document: Dict[int, List[KnowledgeChunk]] = {}
        chunks = (
            db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.document_id.in_([doc.id for doc in documents]))
            .order_by(KnowledgeChunk.chunk_index)
            .all()
        )
        for chunk in chunks:
            by_document.setdefault(chunk.document_id, []).append(chunk)
        for doc in documents:
            key = (doc.filename, doc.content_hash)
            if key not in result and doc.id in by_document:
                result[key] = by_document[doc.id]
    return result
```

Update the docstring's "Only the most recent **completed** job is a candidate" paragraph to:

```
    The newest `_KEEP_COMPLETED_GENERATIONS` completed jobs are candidates,
    newest first: the live set and the generation before it -- exactly the
    window pruning keeps intact, so an audit-only generation (rows kept for a
    trace, vectors nulled) is never a source. Two rather than one so that
    restoring the previous upload re-embeds nothing. A failed job's rows are
    a diagnostic record of something that was never served. A candidate
    contributes nothing unless its shape matches this job's (`_carryable`).
```

Replace the loop in `_prune_old_ingestion_versions`:

```python
    old = completed[_KEEP_COMPLETED_GENERATIONS:]
    # A generation some un-purged run's trace names keeps its rows: the trace
    # carries chunk ids, and an audit has to be able to resolve them to text,
    # page, heading and filename. It loses its files and its vectors -- the
    # bulk of the storage, and nothing an audit needs. Released when
    # `retention.purge_run` deletes the trace (or the KB is deleted), after
    # which the next prune here takes the rows too. Idempotent: an audit-only
    # generation is seen again on every later prune.
    referenced = referenced_job_ids(db, [job.id for job in old])
    for old_job in old:
        version_dir = kb_root / old_job.version
        if version_dir.is_dir():
            shutil.rmtree(version_dir, ignore_errors=True)
        document_ids = db.query(KnowledgeDocument.id).filter_by(ingestion_job_id=old_job.id)
        if old_job.id in referenced:
            db.query(KnowledgeChunk).filter(
                KnowledgeChunk.document_id.in_(document_ids),
                KnowledgeChunk.embedding_json.isnot(None),
            ).update({"embedding_json": None}, synchronize_session=False)
            continue
        db.query(KnowledgeChunk).filter(
            KnowledgeChunk.document_id.in_(document_ids)
        ).delete(synchronize_session=False)
        db.query(KnowledgeDocument).filter_by(ingestion_job_id=old_job.id).delete(synchronize_session=False)
        db.delete(old_job)
    if old:
        db.commit()
```

Update that function's docstring first sentence to: `"""Keep the `_KEEP_COMPLETED_GENERATIONS` most recent completed jobs for this KB intact; every older completed job loses its on-disk version directory and, unless a run's trace still references it, its rows.` (keep the rest).

In `delete_kb_ingestion_data`, before the three deletes:

```python
    delete_for_jobs(db, [job_id for (job_id,) in db.query(IngestionJob.id).filter_by(kb_id=kb_id)])
```

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ingestion.py tests/test_org_knowledge_bases.py tests/test_crud_api.py -q`
Expected: all pass (the incremental-ingestion tests in `test_org_knowledge_bases.py` still see the live job first).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/ingestion.py tests/test_ingestion.py
git commit -m "feat(kb): keep a referenced generation's rows on prune; reuse chunks from the newest two jobs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `retention.purge_run` releases the run's references

**Files:**
- Modify: `ui/backend/retention.py:24-25` (import), `purge_run` (~line 56)
- Test: `tests/test_retention.py` (append after `test_purge_keeps_item_status_and_source_key`)

- [ ] **Step 1: Write the failing test**

```python
def test_purge_releases_the_runs_knowledge_generation_references(db):
    """The link row exists only to keep rows a trace points at; when the
    trace goes, so does the reference -- and it is NOT a purged *field*: it
    is derived from an `ingestion_job_id` inside a trace event the export
    already emits, so `PURGED_FIELDS` and the export are unchanged."""
    from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord, RunKnowledgeGeneration
    from ui.backend.db.run_knowledge_generations import record as record_generation
    from ui.backend.retention import PURGED_FIELDS

    org = create_org(db, "acme")
    run = _run(db, org.id)
    kb = KnowledgeBaseRecord(name="policies", org_id=org.id, config={"name": "policies", "type": "local_folder", "path": "x"})
    db.add(kb)
    db.flush()
    job = IngestionJob(kb_id=kb.id, org_id=org.id, version="v1", status="completed", file_count=1)
    db.add(job)
    db.flush()
    record_generation(db, run.id, job.id)
    db.commit()

    assert purge_run(db, run) is True
    db.commit()

    assert db.query(RunKnowledgeGeneration).filter_by(run_id=run.id).count() == 0
    assert "run_knowledge_generations" not in PURGED_FIELDS
```

- [ ] **Step 2: Run to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_retention.py -q -k knowledge_generation`
Expected: FAIL — `1 == 0`.

- [ ] **Step 3: Implement**

In `ui/backend/retention.py`, after `from .db.retention import orgs_with_retention, record_sweep`:

```python
from .db.run_knowledge_generations import delete_for_run as _release_knowledge_generations
```

In `purge_run`, directly after the `TraceEventRecord` delete:

```python
    # The trace is what named a knowledge-base generation's chunk ids, so the
    # reference keeping that generation's rows alive goes with it (see
    # db/run_knowledge_generations.py). Not a purged field: it is an index
    # over content the export already carries.
    _release_knowledge_generations(db, run.id)
```

Also add one line to the module docstring's list of what content is: after "every `trace_events` row," add "the run's `run_knowledge_generations` references (derived from those events),".

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_retention.py -q`
Expected: all pass, including `test_export_covers_everything_purge_clears` unchanged.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/retention.py tests/test_retention.py
git commit -m "feat(retention): a purged run releases the KB generations its trace named

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Restore the previous upload (backend)

**Files:**
- Modify: `ui/backend/knowledge_bases.py` — `_stage_previous_generation` (156–229), new `restorable_generation` + `restore_previous_generation` after `remove_knowledge_base_document` (~line 681)
- Modify: `ui/backend/org_knowledge_bases.py` — import (~line 58), `_kb_summary` (149–197), new route after `remove_own_knowledge_base_document` (~line 546)
- Test: `tests/test_org_knowledge_bases.py` (append at end)

**Interfaces:**
- Produces: `_stage_previous_generation(..., source: Optional[IngestionJob] = None)`; `restorable_generation(db, org_id, record) -> Optional[IngestionJob]`; `restore_previous_generation(db, org_id, item_name, *, created_by=None) -> Dict[str, Any]`; `POST /api/org/knowledge-bases/{name}/restore` → 202 `{"name", "job_id", "status": "queued"}`; `_kb_summary()["previous_generation"]` = `{"completed_at": str, "filenames": [str, ...]} | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_org_knowledge_bases.py`:

```python
# --- Restoring the previous upload --------------------------------------------
#
# A customer who uploaded the wrong file gets the previous generation back:
# a new generation staged from the previous one's files under the previous
# job's shape, so every chunk and vector is reused and nothing is billed. It
# reaches back exactly one generation -- the only one whose files are still
# on disk.


def _upload(client, *names, mode=None, smart=False):
    data = {}
    if mode:
        data["mode"] = mode
    if smart:
        data["smart_search"] = "true"
    resp = client.post("/api/org/knowledge-bases/policies/upload", data=data, files=_named_files(*names))
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"
    return job_id


def test_restoring_brings_back_the_previous_documents_and_embeds_nothing(client, monkeypatch):
    from ui.backend import ingestion as backend_ingestion
    from ui.backend.db.model_catalog import seed_default_catalog

    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")
    with open_test_db() as db:
        seed_default_catalog(db)
    first = _upload(client, "a.txt", "b.txt", smart=True)
    second = _upload(client, "wrong.txt", smart=True)  # replace -- the mistake

    embed_calls = []
    original = backend_ingestion.embed_documents_in_batches

    def counting(embeddings, texts):
        embed_calls.append(list(texts))
        return original(embeddings, texts)

    monkeypatch.setattr(backend_ingestion, "embed_documents_in_batches", counting)

    resp = client.post("/api/org/knowledge-bases/policies/restore")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["name"] == "policies" and body["status"] == "queued"
    job_id = body["job_id"]
    assert job_id not in (first, second)
    assert _wait_for_job_status(job_id) == "completed"

    assert set(_live_documents(job_id)) == {"a.txt", "b.txt"}
    assert embed_calls == []
    with open_test_db() as db:
        restored = db.get(IngestionJob, job_id)
        previous = db.get(IngestionJob, first)
        assert (restored.kb_type, restored.embedding_model, restored.chunk_size, restored.chunk_overlap) == (
            previous.kb_type, previous.embedding_model, previous.chunk_size, previous.chunk_overlap
        )
        for doc in db.query(KnowledgeDocument).filter_by(ingestion_job_id=job_id):
            chunks = db.query(KnowledgeChunk).filter_by(document_id=doc.id).all()
            assert chunks and all(c.embedding_json for c in chunks)
    summary = client.get("/api/org/knowledge-bases/policies").json()
    assert [d["filename"] for d in summary["documents"]] == ["a.txt", "b.txt"]
    # Restoring again undoes the restore: symmetric by construction.
    assert summary["previous_generation"]["filenames"] == ["wrong.txt"]


def test_restore_keeps_the_config_and_serves_under_the_previous_jobs_type(client, monkeypatch):
    from ui.backend.db.model_catalog import seed_default_catalog

    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")
    with open_test_db() as db:
        seed_default_catalog(db)
    _upload(client, "a.txt")                       # local_folder
    _upload(client, "b.txt", smart=True)            # hybrid -- config now says hybrid

    resp = client.post("/api/org/knowledge-bases/policies/restore")
    assert resp.status_code == 202
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    summary = client.get("/api/org/knowledge-bases/policies").json()
    assert summary["type"] == "local_folder"      # what serves today
    with open_test_db() as db:
        record = db.query(KnowledgeBaseRecord).filter_by(name="policies").one()
        assert record.config["type"] == "hybrid"  # what the next upload builds


def test_summary_previous_generation_is_null_until_a_second_upload_completes(client):
    _upload(client, "a.txt")
    assert client.get("/api/org/knowledge-bases/policies").json()["previous_generation"] is None

    _upload(client, "b.txt", mode="add")
    previous = client.get("/api/org/knowledge-bases/policies").json()["previous_generation"]
    assert previous["filenames"] == ["a.txt"]
    assert previous["completed_at"].endswith("+00:00")


def test_restore_is_refused_with_nothing_to_go_back_to(client):
    _upload(client, "a.txt")
    resp = client.post("/api/org/knowledge-bases/policies/restore")
    assert resp.status_code == 409
    assert "earlier upload" in resp.json()["detail"]


def test_restore_is_refused_while_an_upload_is_processing(client, monkeypatch):
    from ui.backend import ingestion as backend_ingestion

    _upload(client, "a.txt")
    _upload(client, "b.txt")
    monkeypatch.setattr(backend_ingestion._executor, "submit", lambda *a, **k: None)
    assert client.post("/api/org/knowledge-bases/policies/upload", files=_named_files("c.txt")).status_code == 200

    resp = client.post("/api/org/knowledge-bases/policies/restore")
    assert resp.status_code == 409
    assert "still processing" in resp.json()["detail"]


def test_restore_is_refused_when_the_previous_files_are_gone(client):
    import shutil

    first = _upload(client, "a.txt")
    _upload(client, "b.txt")
    with open_test_db() as db:
        job = db.get(IngestionJob, first)
        version, org_id = job.version, job.org_id
    shutil.rmtree(backend_knowledge_bases._KB_UPLOADS_DIR / str(org_id) / "policies" / version)

    assert client.get("/api/org/knowledge-bases/policies").json()["previous_generation"] is None
    resp = client.post("/api/org/knowledge-bases/policies/restore")
    assert resp.status_code == 409
    assert "no longer on the server" in resp.json()["detail"]


def test_restore_of_another_orgs_collection_is_404(client):
    _upload(client, "a.txt")
    _upload(client, "b.txt")
    with open_test_db() as db:
        other = get_or_create_org(db, "other")
        db.commit()
        other_id = other.id
    token = create_user_and_login(client, username="stranger", org="other")

    resp = client.post(
        "/api/org/knowledge-bases/policies/restore",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    del other_id
```

(`backend_knowledge_bases._KB_UPLOADS_DIR` is already monkeypatched by the `client` fixture to `tmp_path / "knowledge_base_uploads"`, and the KB's `org_id` is read off the job row rather than assumed.)

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_org_knowledge_bases.py -q -k "restor or previous_generation"`
Expected: all 7 FAIL (`404`/`405` from the missing route; `KeyError: 'previous_generation'`).

- [ ] **Step 3: Implement `_stage_previous_generation(source=)`**

In `ui/backend/knowledge_bases.py`, change the signature and the lookup:

```python
def _stage_previous_generation(
    db: Session,
    org_id: Optional[int],
    item_name: str,
    kb_root: Path,
    version_dir: Path,
    *,
    superseded: Set[str],
    max_documents: int,
    exact: bool = False,
    source: Optional[IngestionJob] = None,
) -> None:
```

Add to the docstring, after the `exact=True` paragraph:

```
    `source` names the generation to stage from; the default is the live
    (newest completed) job. Restoring the previous upload passes the one
    before it: its files are still on disk (it is the grace-window
    generation), and staging them with nothing superseded is exactly a
    re-upload of that set.
```

Replace the `previous = (...)` query block with:

```python
    previous = source
    if previous is None:
        previous = (
            db.query(IngestionJob)
            .filter_by(kb_id=record.id, status="completed")
            # `id`, not `completed_at` -- see `resolve_knowledge_base`.
            .order_by(IngestionJob.id.desc())
            .first()
        )
```

- [ ] **Step 4: Implement `restorable_generation` and `restore_previous_generation`**

After `remove_knowledge_base_document` (before `delete_knowledge_base`):

```python
def restorable_generation(db: Session, org_id: Optional[int], record: KnowledgeBaseRecord) -> Optional[IngestionJob]:
    """The generation "restore the previous upload" would bring back, or None.

    The second-newest completed job, provided its version directory is still
    on disk -- it is the grace-window generation, so normally it is; an
    operator who removed the files leaves nothing to stage from. One
    generation back only: anything older has lost its files to pruning.
    """
    completed = (
        db.query(IngestionJob)
        .filter_by(kb_id=record.id, status="completed")
        .order_by(IngestionJob.id.desc())
        .limit(2)
        .all()
    )
    if len(completed) < 2:
        return None
    previous = completed[1]
    kb_root = _KB_UPLOADS_DIR / str(org_id) / record.name
    if not (kb_root / previous.version).is_dir():
        return None
    return previous


def restore_previous_generation(
    db: Session,
    org_id: Optional[int],
    item_name: str,
    *,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Make the previous upload the live set again. Returns `{"name",
    "job_id", "status": "queued"}`, exactly like an upload or a removal.

    A restore is `remove_knowledge_base_document` with a different source: the
    generation before the live one is staged into a fresh version directory
    with nothing superseded, and a new job ingests it under THAT job's shape
    and chunk parameters (not the live job's), so every document is
    `ingestion._carryable` from it and -- `_reusable_documents` looking at the
    newest two completed jobs -- nothing is re-parsed, re-embedded or metered.
    The status flip is still the atomic swap; afterwards the keep window is
    {restored, undone}, so restoring again undoes the restore.

    `record.config` is not touched: if the undone upload changed the
    collection's type, the restored generation serves under the previous type
    while `config` keeps the new one -- the existing "config is the next
    upload's shape, the job is the serving shape" split, reported by
    `_live_kb_type`.

    Refused (409) while a `queued`/`running` job exists, when there is no
    earlier completed upload, and when the previous generation's files are no
    longer on the server. Allowed while teams use the collection, as `add`
    and removal are.
    """
    record = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge base '{item_name}'")

    kb_root = _KB_UPLOADS_DIR / str(org_id) / item_name
    version = f"v_{uuid.uuid4().hex[:12]}"
    version_dir = kb_root / version
    with _kb_upload_lock(f"{org_id}/{item_name}"):
        in_flight = (
            db.query(IngestionJob)
            .filter(IngestionJob.kb_id == record.id, IngestionJob.status.in_(("queued", "running")))
            .first()
        )
        if in_flight is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{item_name}' is still processing an upload. Wait for it "
                    "to finish, then restore the previous upload."
                ),
            )
        completed = (
            db.query(IngestionJob)
            .filter_by(kb_id=record.id, status="completed")
            .order_by(IngestionJob.id.desc())
            .limit(2)
            .all()
        )
        if len(completed) < 2:
            raise HTTPException(
                status_code=409,
                detail=f"'{item_name}' has no earlier upload to restore.",
            )
        previous = completed[1]
        if not (kb_root / previous.version).is_dir():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"The files for '{item_name}' are no longer on the server. "
                    "Upload the documents you want, replacing the collection."
                ),
            )

        # The previous job's own shape and chunk parameters -- what makes its
        # every document reusable. A job written before those columns existed
        # re-chunks once under the record's config, as a removal does.
        config = record.config or {}
        kb_type = previous.kb_type or config.get("type", "local_folder")
        chunk_size = previous.chunk_size if previous.chunk_size is not None else config.get("chunk_size", 1000)
        chunk_overlap = (
            previous.chunk_overlap if previous.chunk_overlap is not None else config.get("chunk_overlap", 100)
        )
        embedding_model = previous.embedding_model if kb_type in ("vector", "hybrid") else None

        version_dir.mkdir(parents=True, exist_ok=True)
        try:
            _stage_previous_generation(
                db, org_id, item_name, kb_root, version_dir,
                superseded=set(), max_documents=_MAX_DOCUMENTS_PER_KB, source=previous,
            )
            job = IngestionJob(
                kb_id=record.id,
                org_id=org_id,
                version=version,
                kb_type=kb_type,
                embedding_model=embedding_model,
                status="queued",
                file_count=sum(1 for p in version_dir.rglob("*") if p.is_file()),
                created_by=created_by,
            )
            db.add(job)
            db.commit()
        except Exception:
            db.rollback()
            shutil.rmtree(version_dir, ignore_errors=True)
            raise

        _dispatch_ingestion_job(
            db, job, record.id, org_id, version_dir,
            kb_type=kb_type, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            embedding_model=embedding_model,
        )
        return {"name": item_name, "job_id": job.id, "status": "queued"}
```

- [ ] **Step 5: Route and summary field**

In `ui/backend/org_knowledge_bases.py`, add `restorable_generation` and `restore_previous_generation` to the `from .knowledge_bases import (...)` list, and `KnowledgeDocument` to the `from .db.models import (...)` list.

In `_kb_summary`, after `live = _latest_completed_job(db, record)`:

```python
    previous = restorable_generation(db, org_id, record)
```

`_kb_summary` needs `org_id`: change its signature to `def _kb_summary(db: Session, org_id: Optional[int], record: KnowledgeBaseRecord)` and update its two call sites in this file — line 218 `return [_kb_summary(db, org.id, record) for record in records]` and line 373 `return _kb_summary(db, org.id, _own_kb_or_404(db, org.id, item_name))`. Add to the returned dict, after `"documents"`:

```python
        # What "Restore previous upload" would bring back, or null when there
        # is nothing to go back to (one upload so far, or the files are gone).
        # Filenames rather than a date: the customer should see what they get.
        "previous_generation": None if previous is None else {
            "completed_at": iso_utc(previous.completed_at) if previous.completed_at else None,
            "filenames": [
                doc.filename
                for doc in db.query(KnowledgeDocument)
                .filter_by(ingestion_job_id=previous.id)
                .order_by(KnowledgeDocument.filename)
            ],
        },
```

After `remove_own_knowledge_base_document`:

```python
@router.post("/knowledge-bases/{item_name}/restore", status_code=202)
def restore_own_knowledge_base(
    item_name: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Make the previous upload the live set again. A `202` with the job to
    poll, like an upload: the collection keeps answering from its current
    documents until the restored generation is ready. Nothing is re-embedded.
    Refused while an upload is processing, with no earlier upload, or when
    the previous files are gone; see `knowledge_bases.restore_previous_generation`."""
    return restore_previous_generation(db, org.id, item_name, created_by=user.username)
```

- [ ] **Step 6: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_org_knowledge_bases.py tests/test_crud_api.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add ui/backend/knowledge_bases.py ui/backend/org_knowledge_bases.py tests/test_org_knowledge_bases.py
git commit -m "feat(kb): restore the previous upload of a collection at no embedding cost

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: "Restore previous upload" in the My documents panel

**Files:**
- Modify: `ui/frontend/src/lib/types.ts:294-312` (`OrgKnowledgeBase`)
- Modify: `ui/frontend/src/lib/api.ts:422-426` (after `removeOwnKnowledgeBaseDocument`)
- Modify: `ui/frontend/src/components/KnowledgeBasesPanel.tsx`
- Test: `ui/frontend/src/components/KnowledgeBasesPanel.test.tsx`

**Interfaces:**
- Consumes: `POST /api/org/knowledge-bases/{name}/restore` → `{name, job_id, status}`; `OrgKnowledgeBase.previous_generation`.
- Produces: `api.restoreOwnKnowledgeBase(name: string)`.

- [ ] **Step 1: Types and API**

In `types.ts`, inside `OrgKnowledgeBase` after `documents`:

```ts
  // What "Restore previous upload" brings back, or null when there is
  // nothing to go back to (one upload so far, or its files are gone).
  previous_generation: { completed_at: string | null; filenames: string[] } | null
```

In `api.ts`, after `removeOwnKnowledgeBaseDocument`:

```ts
  // Makes the previous upload the live set again; a 202 with the job to
  // poll, like an upload. Nothing is re-embedded.
  restoreOwnKnowledgeBase: (name: string) =>
    request<{ name: string; job_id: number; status: string }>(
      `/api/org/knowledge-bases/${encodeURIComponent(name)}/restore`,
      { method: 'POST' },
    ),
```

- [ ] **Step 2: Write the failing panel tests**

In `KnowledgeBasesPanel.test.tsx`: add `restoreOwnKnowledgeBase: vi.fn(),` to the `vi.mock('../lib/api', ...)` factory, add `previous_generation: null,` to the `kb()` factory (before `...overrides`), and append inside the `describe` block:

```tsx
  it('restores the previous upload once confirmed, naming what comes back', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        documents: [threeDocuments[0]],
        used_by: ['support_team'],
        previous_generation: { completed_at: '2026-08-20T00:00:00Z', filenames: ['a.txt', 'b.txt'] },
      }),
    ])
    mockedApi.restoreOwnKnowledgeBase.mockResolvedValue({ name: 'policies', job_id: 9, status: 'queued' })
    render(<KnowledgeBasesPanel />)

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Restore previous upload' }))
    })
    expect(screen.getByText(/Restore the previous upload to "policies"\?/)).toBeInTheDocument()
    expect(screen.getByText(/a\.txt, b\.txt/)).toBeInTheDocument()
    expect(screen.getByText(/Teams using "policies": support_team/)).toBeInTheDocument()
    await act(async () => {
      await answerConfirm(true)
    })
    expect(mockedApi.restoreOwnKnowledgeBase).toHaveBeenCalledWith('policies')
    expect(await screen.findByText('Processing…')).toBeInTheDocument()
    await waitFor(() => expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(2))
  })

  it('does not restore if the reader cancels', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ previous_generation: { completed_at: null, filenames: ['a.txt'] } }),
    ])
    render(<KnowledgeBasesPanel />)
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Restore previous upload' }))
    })
    await act(async () => {
      await answerConfirm(false)
    })
    expect(mockedApi.restoreOwnKnowledgeBase).not.toHaveBeenCalled()
  })

  it('disables Restore with nothing to go back to, and while an upload is processing', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ name: 'one_upload', previous_generation: null }),
      kb({
        name: 'busy',
        previous_generation: { completed_at: null, filenames: ['a.txt'] },
        latest_job: { job_id: 2, status: 'running', file_count: 1, documents_succeeded: 0, documents_failed: 0, chunk_count: 0, errors: [] },
      }),
    ])
    render(<KnowledgeBasesPanel />)
    const buttons = await screen.findAllByRole('button', { name: 'Restore previous upload' })
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toBeDisabled()
    expect(buttons[0]).toHaveAttribute('title', expect.stringMatching(/no earlier upload/i))
    expect(buttons[1]).toBeDisabled()
    expect(buttons[1]).toHaveAttribute('title', expect.stringMatching(/still processing/i))
  })

  it("shows a refused restore's message on the row", async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ previous_generation: { completed_at: null, filenames: ['a.txt'] } }),
    ])
    mockedApi.restoreOwnKnowledgeBase.mockRejectedValue(
      new Error("The files for 'policies' are no longer on the server. Upload the documents you want, replacing the collection."),
    )
    render(<KnowledgeBasesPanel />)
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Restore previous upload' }))
    })
    await act(async () => {
      await answerConfirm(true)
    })
    expect(await screen.findByText(/no longer on the server/)).toBeInTheDocument()
  })
```

- [ ] **Step 3: Run to verify they fail**

Run (from `ui/frontend`): `npm run test -- --run src/components/KnowledgeBasesPanel.test.tsx`
Expected: the 4 new tests FAIL (no such button).

- [ ] **Step 4: Implement the panel**

In `KnowledgeBasesPanel.tsx`:

After `removeBlockedReason`:

```tsx
// Why restoring the previous upload is refused, in the reader's own terms, or
// null when it's allowed. The backend refuses both with a 409 regardless.
function restoreBlockedReason(kb: OrgKnowledgeBase): string | null {
  if (isProcessing(kb)) return 'This upload is still processing. Wait for it to finish, then restore the previous upload.'
  if (!kb.previous_generation) return 'There is no earlier upload to go back to.'
  return null
}
```

State, after `const [removing, setRemoving] = useState<string | null>(null)`:

```tsx
  // The collection whose restore is in flight, so one click is one restore.
  const [restoring, setRestoring] = useState<string | null>(null)
```

Handler, after `handleRemoveDocument`:

```tsx
  const handleRestore = async (kb: OrgKnowledgeBase) => {
    const previous = kb.previous_generation
    if (!previous) return
    const usedBy = kb.used_by.length > 0 ? ` Teams using "${kb.name}": ${kb.used_by.join(', ')}.` : ''
    const ok = await confirm({
      title: `Restore the previous upload to "${kb.name}"?`,
      body: `The collection goes back to: ${previous.filenames.join(', ')}. Its current documents are replaced.${usedBy}`,
      confirmLabel: 'Restore',
      destructive: true,
    })
    if (!ok) return
    setRestoring(kb.name)
    try {
      const job = await api.restoreOwnKnowledgeBase(kb.name)
      setRowErrors((prev) => {
        const next = { ...prev }
        delete next[kb.name]
        return next
      })
      // Mark the row processing from the 202 itself (same reasoning as a
      // removal): the poll keys off this even if the refresh below fails.
      setItems((prev) =>
        prev.map((i) =>
          i.name === kb.name && i.latest_job
            ? { ...i, latest_job: { ...i.latest_job, job_id: job.job_id, status: 'queued' } }
            : i,
        ),
      )
      await refresh()
    } catch (e) {
      setRowErrors((prev) => ({ ...prev, [kb.name]: (e as Error).message }))
    } finally {
      setRestoring(null)
    }
  }
```

In the row's render, add `const restoreBlocked = restoreBlockedReason(kb)` beside `removeBlocked`, and insert the button between "Try a search" and "Delete":

```tsx
              <button
                type="button"
                className="btn btn-secondary"
                disabled={restoreBlocked !== null || restoring === kb.name}
                title={restoreBlocked ?? 'Restore previous upload'}
                onClick={() => void handleRestore(kb)}
              >
                Restore previous upload
              </button>
```

- [ ] **Step 5: Run the frontend gates**

From `ui/frontend`: `npm run lint && npm run build && npm run test -- --run`
Expected: lint clean, build clean, all tests pass (the panel's existing tests still pass — `kb()` now carries `previous_generation: null`).

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/lib/types.ts ui/frontend/src/lib/api.ts ui/frontend/src/components/KnowledgeBasesPanel.tsx ui/frontend/src/components/KnowledgeBasesPanel.test.tsx
git commit -m "feat(ui): Restore previous upload on the My documents panel

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Documentation

**Files:**
- Modify: `ui/backend/db/CLAUDE.md` (after the `knowledge_chunks` bullet), `ui/backend/CLAUDE.md` (~line 1477 "keeping the current one plus one grace-window generation", and the org-KB paragraph that describes the per-document DELETE), `ui/frontend/CLAUDE.md` ("My documents panel" section), `docs/KNOWLEDGE_BASES.md:831-836`, `docs/STATUS.md` (top of `## Done`), `CHANGELOG.md` (`[Unreleased]` → `### Added`), root `CLAUDE.md:108-111`.

- [ ] **Step 1: `ui/backend/db/CLAUDE.md`** — add a bullet after `knowledge_chunks`:

```
- `run_knowledge_generations` (`RunKnowledgeGeneration`) — one row per (run,
  ingestion job): *this run's trace names chunk/document ids from this
  generation*. Written by `runtime.run_in_background` from a KB tool's
  `tool_completed` event the moment it arrives (a cancelled run has still
  read the generation), one row per generation per run. Read by
  `ingestion._prune_old_ingestion_versions`: a completed job outside the
  newest-two window that some run references keeps its job/document/chunk
  rows with `embedding_json` set NULL and its version directory deleted — an
  audit resolves a chunk id to text, page, heading and filename, never a
  vector. Released by `retention.purge_run` (with the trace; deliberately
  **not** in `PURGED_FIELDS`, being an index over exported content) and by
  `ingestion.delete_kb_ingestion_data`. No backfill (migration
  `s6t7u8v9w0x1`). See
  `docs/superpowers/specs/2026-08-24-kb-generation-audit-retention-and-restore-design.md`.
```

- [ ] **Step 2: `ui/backend/CLAUDE.md`** — replace the clause "best-effort prunes older completed generations, keeping the current one plus one grace-window generation, mirroring the legacy path's "prior version kept only until the new one is durable" precedent" with:

```
best-effort prunes older completed generations: the current one plus one
grace-window generation stay intact (mirroring the legacy path's "prior
version kept only until the new one is durable" precedent); an older one
loses its version directory and, unless a run's trace still references it
(`run_knowledge_generations`, see `db/CLAUDE.md`), its rows -- a referenced
"audit-only" generation keeps document/chunk rows with the vectors nulled,
and is reclaimed at this KB's next completed job after retention purges the
referencing run. `_reusable_documents` looks at exactly that intact window
(the newest two completed jobs, newest first), never at an audit-only one
```

In the org-KB paragraph ("**An org manages its own knowledge bases**"), after the sentence describing the per-document DELETE's 404 rule, add:

```
`POST /api/org/knowledge-bases/{name}/restore` (`knowledge_bases.restore_previous_generation`)
is the same machinery with the *previous* completed job as
`_stage_previous_generation`'s `source` and as the new job's shape, so every
chunk and vector is carried forward and nothing is metered; 409 while a job is
queued/running, 409 with no earlier completed upload, 409 when the previous
version directory is gone (`restorable_generation` is what `_kb_summary`'s
`previous_generation` -- `{completed_at, filenames}` or null -- reports from).
One generation back only: it is the only one whose files are still on disk.
`record.config` is untouched, so a restore that undoes a type change serves
under the previous type while `config` keeps the new one.
```

- [ ] **Step 3: `ui/frontend/CLAUDE.md`** — in "My documents panel", after the Remove paragraph:

```
Each row also has **"Restore previous upload"** beside "Try a search" —
`POST /api/org/knowledge-bases/{name}/restore` behind a confirm that lists the
filenames coming back (`kb.previous_generation.filenames`) and the teams using
the collection. Disabled with the reason in its `title` while an upload is
processing and when `previous_generation` is null (one upload so far, or the
files are gone). A success marks the row `queued` from the 202 and re-fetches,
like a removal; a refusal's message renders on the row. The mock factory lists
`restoreOwnKnowledgeBase`. The panel's copy is still English-only literals
(the F1 long tail).
```

- [ ] **Step 4: `docs/KNOWLEDGE_BASES.md:831-836`** — replace the "Older completed ingestion generations are pruned automatically…" paragraph with:

```
Older completed ingestion generations are pruned automatically once a new job
completes: the current generation and the one before it are kept intact, and
an older one loses its files. Its rows go too — unless a run's trace still
references it. Every knowledge-base search leaves the generation's id and each
hit's chunk id in the run's trace, and those ids keep resolving for as long as
the trace exists: a referenced generation keeps its document and chunk rows
with the vectors dropped (text, page, heading and filename are what an audit
needs), and is reclaimed at the collection's next completed upload after the
run's content is purged by retention. A `failed` job's on-disk version
directory is reclaimed the same way — every failed job except the most recent
one loses its directory (its rows stay, as the customer-visible error record),
so repeatedly retrying an upload that can't be parsed doesn't accumulate
storage.

**Restoring the previous upload.** "Restore previous upload" on the "My
documents" panel (`POST /api/org/knowledge-bases/{name}/restore`) makes the
generation before the live one the live set again: its files are staged into
a new generation under its own settings, every chunk and embedding is reused,
and nothing is billed. It reaches back one upload only — the one whose files
are still on the server — and restoring again undoes the restore. Refused
while an upload is processing, when there is no earlier upload, and when the
previous files are gone.
```

- [ ] **Step 5: `docs/STATUS.md`** — insert at the top of `## Done`:

```
- **A trace's knowledge-base ids keep resolving, and a customer can restore
  the previous upload** (2026-08-24). PR #86 put `ingestion_job_id` and each
  hit's `chunk_id` into every KB `tool_completed` event, and
  `_KEEP_COMPLETED_GENERATIONS = 2` deleted those rows two uploads later — ids
  recorded and then destroyed. The 2026-08-24 external review called it P0 and
  again proposed pinning a generation to a `PipelineVersion`; refused again
  (a customer's upload must take effect at once), and the gap closed without
  it: `run_knowledge_generations` (written by `runtime.py` from the search
  event) makes `_prune_old_ingestion_versions` keep a referenced generation's
  rows with the vectors nulled and the files gone, released by
  `retention.purge_run` and KB deletion. "Restore previous upload" is PR #87's
  removal machinery with the previous job as source and shape, and
  `_reusable_documents` now looks at the newest two completed jobs, so it
  re-embeds nothing. Not done, on purpose: a read surface for a retained
  chunk (the admin Trace page rendering `hits`), restoring further back than
  one generation, a maintenance sweep for an idle KB's audit-only
  generations, and backfilling references from pre-migration traces. Spec:
  `docs/superpowers/specs/2026-08-24-kb-generation-audit-retention-and-restore-design.md`.
```

- [ ] **Step 6: `CHANGELOG.md`** — under `## [Unreleased]` → `### Added`, before the "Remove one document" entry:

```
- **Restore the previous upload** — "Restore previous upload" on "My
  documents" (`POST /api/org/knowledge-bases/{name}/restore`) makes the
  upload before the current one live again, reusing every chunk and embedding
  (nothing re-embedded or billed). One upload back; restoring again undoes it.
- **Search evidence in a run's trace keeps resolving** — a knowledge-base
  generation that some run's trace references is no longer deleted when newer
  uploads push it out of the keep window: its text rows are kept (vectors
  and files are not) until the run's content is purged by retention.
  Migration `s6t7u8v9w0x1` adds `run_knowledge_generations`; run
  `alembic upgrade head`. Nothing is backfilled.
```

- [ ] **Step 7: root `CLAUDE.md:108-111`** — after "so only genuinely new documents are embedded and billed." add:

```
  **Generations a run's trace references are kept**: pruning keeps the newest
  two completed generations intact, and an older one that some un-purged run
  searched keeps its text rows (vectors nulled) — see `ui/backend/db/CLAUDE.md`
  `run_knowledge_generations`. A customer can restore the previous upload
  (one generation back) at no embedding cost.
```

- [ ] **Step 8: Commit**

```bash
git add ui/backend/db/CLAUDE.md ui/backend/CLAUDE.md ui/frontend/CLAUDE.md docs/KNOWLEDGE_BASES.md docs/STATUS.md CHANGELOG.md CLAUDE.md
git commit -m "docs(kb): generation retention by reference and restore previous upload

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Full local gates, push, PR, Codex review

**Files:** none new.

- [ ] **Step 1: Backend, serial, full**

Run: `.\.venv\Scripts\python.exe -m pytest -m "not e2e" -q`
Expected: all pass (baseline ~2230 tests, ~7 min). Fix anything red before continuing.

- [ ] **Step 2: Frontend gates**

From `ui/frontend`: `npm run lint && npm run build && npm run test -- --run`
Expected: clean.

- [ ] **Step 3: E2E**

Requires ports 8000/5173 free. Run: `.\.venv\Scripts\python.exe -m pytest tests/e2e -q`
Expected: all pass (the panel gained a button; no e2e test targets it, but the wizard tier must still pass).

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/kb-generation-audit-and-restore
gh pr create --title "feat(kb): keep generations a trace references; restore the previous upload" --body "$(cat <<'EOF'
## Summary
- `run_knowledge_generations`: `runtime.py` records which KB generation a run searched; `_prune_old_ingestion_versions` keeps a referenced old generation's rows (vectors nulled, files deleted); released by retention purge and KB delete. Closes the gap #86 opened (ids recorded, then pruned) without pinning a generation to a pipeline version.
- `POST /api/org/knowledge-bases/{name}/restore` + "Restore previous upload" on My documents: the previous generation staged again under its own shape; `_reusable_documents` now looks at the newest two completed jobs, so nothing is re-embedded.
- Migration `s6t7u8v9w0x1`; docs + CHANGELOG.

Spec: `docs/superpowers/specs/2026-08-24-kb-generation-audit-retention-and-restore-design.md`

## Test plan
- [x] `pytest -m "not e2e"` serial
- [x] frontend lint / build / test
- [x] `pytest tests/e2e`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Codex review**

Run `/codex:review --background --base main`, then `/codex:status`; triage every finding (fix the real ones, answer the rest on the PR as a comment), re-run the gates on anything changed, push. Wait for the four PR-gate CI jobs to pass before reporting done. **Do not merge** — the user merges.

---

## Self-review

**Spec coverage.** §1 data model → Task 1. §2 writing → Task 2 (immediate write, per-run dedup, best-effort, None skipped). §3 pruning → Task 3 (two branches, idempotent, `_reusable_documents` two-job window, KB delete); retention release → Task 4 (not in `PURGED_FIELDS`, asserted). §4 restore → Task 5 (all 409s, previous job's shape, `config` untouched, `source=` param, summary field, route) + Task 6 (panel). §5 error handling → covered by the best-effort helper (Task 2), the existing prune wrapper (Task 3), and the route's HTTPExceptions (Task 5). §6 testing → every listed case has a test in Tasks 1–6 except "prune twice: identical state" (Task 3 `test_prune_is_idempotent...`, present) — nothing missing. §7 docs → Task 7. "Out of scope" items are not built.

**Placeholder scan.** No TBD/TODO; every code step shows the code.

**Type consistency.** `record(db, run_id, ingestion_job_id)` (Task 1) is what Task 2 imports as `record_knowledge_generation` and Task 2's test monkeypatches under that name on `runtime`. `referenced_job_ids` / `delete_for_jobs` (Task 1) are what Task 3 imports; `delete_for_run` (Task 1) is what Task 4 imports as `_release_knowledge_generations` and what Task 3's release test calls directly. `restorable_generation(db, org_id, record)` and `restore_previous_generation(db, org_id, item_name, *, created_by)` (Task 5) match the route and `_kb_summary(db, org_id, record)`. `previous_generation` shape `{completed_at: string|null, filenames: string[]} | null` matches between Task 5 (backend), Task 6 (`types.ts`, tests) and Task 7 (docs).
