# Knowledge Base Document/Chunk/IngestionJob Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make knowledge-base uploads asynchronous and persist their parsed
chunks (and, for vector/hybrid, embeddings) into new `IngestionJob`/
`KnowledgeDocument`/`KnowledgeChunk` tables, so retrieval reads from the DB
instead of re-parsing files from disk on every workflow load.

**Architecture:** Three new SQLAlchemy tables scoped to upload-managed KBs.
A new backend module (`ui/backend/ingestion.py`) runs ingestion on its own
`ThreadPoolExecutor`, writing per-document/per-chunk rows with the job's
`status="completed"` flip as the atomic "this version is now live" swap
(replacing the old CURRENT-pointer-file mechanism for this path only). The
SDK core (`src/bestteam/core/`) stays 100% file-based and DB-free — each of
the three `KnowledgeBase` classes gains a `from_chunks(...)` alternate
constructor that accepts already-built chunks/vectors as plain data,
reusing the exact same BM25/RRF/rerank/query-expansion logic `__init__`
already has. The backend's `load_knowledge_base_tools()` reads DB rows and
calls `from_chunks(...)` for a KB with a completed ingestion job, falling
back to the existing file-based `_build_knowledge_base()` for a KB that
predates this change.

**Tech Stack:** Python, SQLAlchemy 2.0, Alembic, FastAPI, pytest; React/TS
for the two minimal frontend compatibility updates.

**Spec:** `docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`

## Global Constraints

- SDK core (`src/bestteam/core/`) must never import SQLAlchemy or any
  backend module — it stays usable standalone via the CLI/YAML path with no
  backend DB running.
- Only KBs created through the upload endpoints get ingestion jobs.
  Manually-configured (`PUT` with an arbitrary `path:`) KBs are untouched.
- A KB with zero `IngestionJob` rows must keep resolving via the existing
  file-based path, byte-for-byte unchanged (no backfill, no migration
  script for pre-existing KBs).
- Error text stored on `IngestionJob.error`/`KnowledgeDocument.error` is
  always capped at `_MAX_ERROR_CHARS = 2000` and is `str(exc)` only — never
  a raw traceback or raw file content.
- A document's parse failure never aborts the batch; a job resolves
  `completed` only if at least one chunk was actually written (not merely
  "at least one document succeeded" — an empty/blank file can "succeed"
  parsing yet produce zero chunks, which must not count as a servable KB).
- A total embedding-model failure (`vector`/`hybrid`) fails the whole job.
- Every test file needs a `pytestmark` (`unit` for SDK-only files under
  `tests/`, `integration` for anything touching the backend DB/FastAPI —
  matches the existing convention in `tests/test_knowledge_base.py` /
  `tests/test_org_knowledge_bases.py`).
- Run the backend suite via `./.venv/Scripts/python.exe -m pytest`, the
  frontend suite via `cd ui/frontend && npx vitest run` (must `cd` first —
  running from the repo root breaks environment resolution).

---

## Task 1: Schema — `IngestionJob`/`KnowledgeDocument`/`KnowledgeChunk` models + migration

**Files:**
- Modify: `ui/backend/db/models.py`
- Create: `alembic/versions/d2e3f4a5b6c7_knowledge_ingestion_tables.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Produces: `IngestionJob` (table `knowledge_ingestion_jobs`; columns `id`,
  `kb_id`, `org_id`, `version: str`, `status: str` (`queued`/`running`/
  `completed`/`failed`), `file_count: int`, `documents_succeeded: int`,
  `documents_failed: int`, `error: Optional[str]`, `created_by:
  Optional[str]`, `created_at`, `completed_at: Optional[datetime]`).
- Produces: `KnowledgeDocument` (table `knowledge_documents`; columns `id`,
  `kb_id`, `ingestion_job_id`, `filename: str`, `content_hash: str`,
  `size_bytes: int`, `status: str` (`pending`/`parsing`/`chunked`/
  `failed`), `error: Optional[str]`, `created_at`).
- Produces: `KnowledgeChunk` (table `knowledge_chunks`; columns `id`,
  `document_id`, `kb_id`, `chunk_index: int`, `text: str`,
  `embedding_json: Optional[str]`, `embedding_model: Optional[str]`,
  `created_at`).

- [ ] **Step 1: Add the three model classes**

Open `ui/backend/db/models.py`. Insert immediately after the
`KnowledgeBaseRecord` class (after its closing blank line, before
`class SkillRecord(Base):`):

```python
class IngestionJob(Base):
    """One async ingestion run for an upload-managed KnowledgeBaseRecord.

    A KB's live document set is always its most recent `completed` job's
    KnowledgeDocument/KnowledgeChunk rows -- the `status="completed"` flip
    is the atomic swap (no CURRENT-pointer file needed for this path,
    unlike the legacy file-based read path). A `queued`/`running`/`failed`
    job is invisible to retrieval. See
    docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md.
    """

    __tablename__ = "knowledge_ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_knowledge_ingestion_jobs_status",
        ),
        Index(
            "ix_knowledge_ingestion_jobs_kb_id_status_completed_at",
            "kb_id", "status", "completed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=False)
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # The same `v_<hex>` identifier used for the on-disk version directory
    # (see ui/backend/knowledge_bases.py) -- traceable job <-> directory
    # correspondence.
    version: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued")
    file_count: Mapped[int] = mapped_column(default=0)
    documents_succeeded: Mapped[int] = mapped_column(default=0)
    documents_failed: Mapped[int] = mapped_column(default=0)
    error: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class KnowledgeDocument(Base):
    """One uploaded file's ingestion outcome within an IngestionJob.

    Per-document status means one bad file in a batch doesn't fail the
    whole job -- see IngestionJob's docstring.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'parsing', 'chunked', 'failed')",
            name="ck_knowledge_documents_status",
        ),
        Index("ix_knowledge_documents_ingestion_job_id", "ingestion_job_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=False)
    ingestion_job_id: Mapped[int] = mapped_column(ForeignKey("knowledge_ingestion_jobs.id"), nullable=False)
    filename: Mapped[str]
    content_hash: Mapped[str]
    size_bytes: Mapped[int]
    status: Mapped[str] = mapped_column(default="pending")
    error: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class KnowledgeChunk(Base):
    """One chunk of a KnowledgeDocument's parsed text.

    `embedding_json` is a JSON-encoded `List[float]` (same TEXT-column
    shape as `memories.embedding_json` in core/memory.py), populated only
    for `vector`/`hybrid` KBs.
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_knowledge_chunks_document_id_chunk_index", "document_id", "chunk_index"),
        Index("ix_knowledge_chunks_kb_id", "kb_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=False)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=False)
    chunk_index: Mapped[int]
    text: Mapped[str]
    embedding_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    embedding_model: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

- [ ] **Step 2: Write the migration**

Create `alembic/versions/d2e3f4a5b6c7_knowledge_ingestion_tables.py`:

```python
"""knowledge_ingestion_jobs, knowledge_documents, knowledge_chunks tables

Revision ID: d2e3f4a5b6c7
Revises: b5c1d8e3f2a9
Create Date: 2026-08-16 00:00:00.000000

Guarded/idempotent, same reason as every other migration here:
ui/backend/db_session.py runs create_all at import before
`alembic upgrade head` runs, so a fresh database already has these tables.
See docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "b5c1d8e3f2a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "knowledge_ingestion_jobs" not in tables:
        op.create_table(
            "knowledge_ingestion_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kb_id", sa.Integer(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
            sa.Column("version", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("documents_succeeded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("documents_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('queued', 'running', 'completed', 'failed')",
                name="ck_knowledge_ingestion_jobs_status",
            ),
        )
        op.create_index(
            "ix_knowledge_ingestion_jobs_kb_id_status_completed_at",
            "knowledge_ingestion_jobs", ["kb_id", "status", "completed_at"],
        )

    if "knowledge_documents" not in tables:
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kb_id", sa.Integer(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column(
                "ingestion_job_id", sa.Integer(),
                sa.ForeignKey("knowledge_ingestion_jobs.id"), nullable=False,
            ),
            sa.Column("filename", sa.String(), nullable=False),
            sa.Column("content_hash", sa.String(), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'parsing', 'chunked', 'failed')",
                name="ck_knowledge_documents_status",
            ),
        )
        op.create_index(
            "ix_knowledge_documents_ingestion_job_id",
            "knowledge_documents", ["ingestion_job_id"],
        )

    if "knowledge_chunks" not in tables:
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "document_id", sa.Integer(),
                sa.ForeignKey("knowledge_documents.id"), nullable=False,
            ),
            sa.Column("kb_id", sa.Integer(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("text", sa.String(), nullable=False),
            sa.Column("embedding_json", sa.String(), nullable=True),
            sa.Column("embedding_model", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_knowledge_chunks_document_id_chunk_index",
            "knowledge_chunks", ["document_id", "chunk_index"],
        )
        op.create_index("ix_knowledge_chunks_kb_id", "knowledge_chunks", ["kb_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "knowledge_chunks" in tables:
        op.drop_table("knowledge_chunks")
    if "knowledge_documents" in tables:
        op.drop_table("knowledge_documents")
    if "knowledge_ingestion_jobs" in tables:
        op.drop_table("knowledge_ingestion_jobs")
```

- [ ] **Step 3: Update the migration regression test**

In `tests/test_migrations.py`, add the three new table names to
`_EXPECTED_HEAD_TABLES`:

```python
_EXPECTED_HEAD_TABLES = {
    "organizations",
    "users",
    "knowledge_bases",
    "knowledge_ingestion_jobs",
    "knowledge_documents",
    "knowledge_chunks",
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

- [ ] **Step 4: Run the migration test suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py -v`

Expected: all tests pass, including the guarded-upgrade-is-idempotent test
and whichever test asserts `_EXPECTED_HEAD_TABLES` against the post-upgrade
schema.

- [ ] **Step 5: Run the full DB model test file**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_db.py -v`

Expected: PASS (this file instantiates every model against `create_all`; if
it doesn't exist or doesn't cover new tables, this step is a no-op check —
proceed if `pytest` reports "no tests ran" for a nonexistent path).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/db/models.py alembic/versions/d2e3f4a5b6c7_knowledge_ingestion_tables.py tests/test_migrations.py
git commit -m "feat(db): add IngestionJob/KnowledgeDocument/KnowledgeChunk tables"
```

---

## Task 2: SDK — `LocalFolderKnowledgeBase.from_chunks`

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py`
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `_Chunk(source: str, text: str)` NamedTuple (already defined in
  this file), `_validate_chunk_params`, `resolve_reranker`,
  `_resolve_candidate_k`, `_MAX_RERANK_CANDIDATE_K` (already imported in
  this file).
- Produces: `LocalFolderKnowledgeBase.from_chunks(name: str, chunks:
  List[_Chunk], top_k: int = 5, rerank_model: Any = None, candidate_k:
  Optional[int] = None, query_expansion_model: Any = None,
  query_expansion_count: int = 3) -> LocalFolderKnowledgeBase` — a
  classmethod. Raises `ConfigurationError` if `chunks` is empty or if
  `rank-bm25` isn't installed (identical error text/conditions as today's
  `__init__`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_knowledge_base.py` (near the other `LocalFolderKnowledgeBase`
tests):

```python
def test_from_chunks_builds_queryable_kb():
    from bestteam.core.knowledge_base import _Chunk

    chunks = [
        _Chunk(source="a.txt", text="Refunds are allowed within 30 days of purchase."),
        _Chunk(source="b.txt", text="Our office hours are 9am to 5pm on weekdays."),
    ]
    kb = LocalFolderKnowledgeBase.from_chunks("policies", chunks, top_k=1)
    result = kb.query("refund policy")
    assert "30 days" in result
    assert "[source: a.txt]" in result


def test_from_chunks_empty_list_raises_configuration_error():
    with pytest.raises(ConfigurationError, match="no readable documents"):
        LocalFolderKnowledgeBase.from_chunks("empty_kb", [])


def test_from_chunks_and_init_produce_identical_query_results(tmp_path):
    (tmp_path / "doc.txt").write_text(
        "Refunds are allowed within 30 days of purchase.", encoding="utf-8"
    )
    from_path = LocalFolderKnowledgeBase("kb", tmp_path, top_k=1)

    from bestteam.core.knowledge_base import _Chunk
    chunks = [_Chunk(source="doc.txt", text="Refunds are allowed within 30 days of purchase.")]
    from_chunks = LocalFolderKnowledgeBase.from_chunks("kb", chunks, top_k=1)

    assert from_path.query("refund") == from_chunks.query("refund")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_knowledge_base.py -k from_chunks -v`
Expected: FAIL with `AttributeError: type object 'LocalFolderKnowledgeBase' has no attribute 'from_chunks'`

- [ ] **Step 3: Refactor `__init__` to delegate to `_init_from_chunks`, add `from_chunks`**

In `src/bestteam/core/knowledge_base.py`, replace the entire `__init__`
method of `LocalFolderKnowledgeBase` (currently lines 58-101, from `def
__init__` through the `self._bm25 = BM25Okapi(self._chunk_tokens)` line)
with:

```python
    def __init__(
        self,
        name: str,
        path: str | Path,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        top_k: int = 5,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> None:
        _validate_chunk_params(name, chunk_size, chunk_overlap)
        self.path = Path(path)
        chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        self._init_from_chunks(
            name, chunks, top_k,
            rerank_model=rerank_model,
            candidate_k=candidate_k,
            query_expansion_model=query_expansion_model,
            query_expansion_count=query_expansion_count,
        )

    @classmethod
    def from_chunks(
        cls,
        name: str,
        chunks: List["_Chunk"],
        top_k: int = 5,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> "LocalFolderKnowledgeBase":
        """Build directly from pre-parsed chunks, skipping the file-parsing
        pipeline. Used by the backend's DB-backed ingestion path (see
        ui/backend/knowledge_bases.py) -- the SDK itself never touches a
        database; chunks are handed in as plain data."""
        self = cls.__new__(cls)
        self.path = None
        self._init_from_chunks(
            name, chunks, top_k,
            rerank_model=rerank_model,
            candidate_k=candidate_k,
            query_expansion_model=query_expansion_model,
            query_expansion_count=query_expansion_count,
        )
        return self

    def _init_from_chunks(
        self,
        name: str,
        chunks: List["_Chunk"],
        top_k: int,
        *,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> None:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ConfigurationError(
                "Knowledge bases require the 'rank-bm25' package. "
                "Install it with: pip install 'bestteam[tools-rag]'"
            ) from exc

        self.name = name
        self.default_top_k = top_k
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count

        self._chunks = chunks
        if not self._chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents"
                + (f" in {self.path}" if self.path is not None else "")
            )

        self._chunk_tokens = [tokenize(chunk.text) for chunk in self._chunks]
        self._chunk_terms = [significant_terms(tokens) for tokens in self._chunk_tokens]
        self._bm25 = BM25Okapi(self._chunk_tokens)
```

Note: `_validate_chunk_params` is now called only from `__init__` (it
validates `chunk_size`/`chunk_overlap`, which `from_chunks` doesn't take —
the DB-backed path validated those params once already, at ingestion-job
time). The "no readable documents in {path}" message keeps its original
wording for the file-based path (`self.path` set) and drops the trailing
`in {path}` clause when built `from_chunks` (`self.path is None`), since
there's no path to report there.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_knowledge_base.py -v`
Expected: PASS, including every pre-existing test in this file (the
refactor must not change `__init__`'s observable behavior).

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "feat(sdk): add LocalFolderKnowledgeBase.from_chunks alternate constructor"
```

---

## Task 3: SDK — `VectorKnowledgeBase.from_chunks`

**Files:**
- Modify: `src/bestteam/core/vector_knowledge_base.py`
- Test: `tests/test_vector_knowledge_base.py`

**Interfaces:**
- Consumes: `_Chunk` from Task 2's file (unchanged); `resolve_embedding_model`,
  `normalize_rows` (already imported in this file).
- Produces: `VectorKnowledgeBase.from_chunks(name: str, chunks:
  List[_Chunk], vectors: List[List[float]], top_k: int = 5,
  score_threshold: Optional[float] = None, rerank_model: Any = None,
  candidate_k: Optional[int] = None, query_expansion_model: Any = None,
  query_expansion_count: int = 3) -> VectorKnowledgeBase` — skips
  `_embed_chunks()` entirely; `vectors` must already be pre-computed
  (raises `ConfigurationError` if `len(vectors) != len(chunks)`, same check
  `__init__` does today).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_vector_knowledge_base.py`:

```python
def test_from_chunks_builds_queryable_kb():
    from bestteam.core.knowledge_base import _Chunk

    chunks = [
        _Chunk(source="a.txt", text="Refunds are allowed within 30 days."),
        _Chunk(source="b.txt", text="Office hours are 9am to 5pm."),
    ]
    # "fake:4" gives deterministic, cheap vectors -- exact values don't matter
    # here, only that from_chunks accepts a pre-computed list the same shape
    # embed_documents() would have produced.
    from bestteam.core.embeddings import resolve_embedding_model
    embeddings = resolve_embedding_model("fake:4")
    vectors = embeddings.embed_documents([c.text for c in chunks])

    kb = VectorKnowledgeBase.from_chunks("policies", chunks, vectors, "fake:4", top_k=1)
    result = kb.query("refund policy")
    assert "30 days" in result


def test_from_chunks_vector_count_mismatch_raises_configuration_error():
    from bestteam.core.knowledge_base import _Chunk

    chunks = [_Chunk(source="a.txt", text="hello")]
    with pytest.raises(ConfigurationError, match="embedding model returned"):
        VectorKnowledgeBase.from_chunks("kb", chunks, vectors=[], embedding_model="fake:4")
```

Confirm `tests/test_vector_knowledge_base.py` already imports `pytest`,
`ConfigurationError`, and `VectorKnowledgeBase` at the top (it does, per
its existing tests) — add these two functions alongside them.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_vector_knowledge_base.py -k from_chunks -v`
Expected: FAIL with `AttributeError: type object 'VectorKnowledgeBase' has no attribute 'from_chunks'`

- [ ] **Step 3: Refactor `__init__` to delegate, add `from_chunks`**

In `src/bestteam/core/vector_knowledge_base.py`, replace the `__init__`
method (currently lines 56-111) with:

```python
    def __init__(
        self,
        name: str,
        path: str | Path,
        embedding_model: Any,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        cache_path: Optional[str | Path] = None,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> None:
        _validate_chunk_params(name, chunk_size, chunk_overlap)
        self.path = Path(path)
        chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        if not chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents in {self.path}"
            )
        self._init_common(name, chunks, top_k, score_threshold, rerank_model, candidate_k,
                           query_expansion_model, query_expansion_count)
        self._embeddings = resolve_embedding_model(embedding_model)
        vectors = self._embed_chunks(embedding_model, cache_path)
        self._set_vectors(name, vectors)

    @classmethod
    def from_chunks(
        cls,
        name: str,
        chunks: List["_Chunk"],
        vectors: List[List[float]],
        embedding_model: Any,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> "VectorKnowledgeBase":
        """Build directly from pre-parsed chunks and pre-computed vectors,
        skipping both the file-parsing pipeline and the embedding call for
        those chunks. `embedding_model` is still required and resolved
        (cheap, no API call) -- query() needs a live embeddings model to
        embed the QUERY text at search time, even though the document
        vectors themselves are supplied pre-computed. Used by the backend's
        DB-backed ingestion path (see ui/backend/knowledge_bases.py)."""
        self = cls.__new__(cls)
        self.path = None
        if not chunks:
            raise ConfigurationError(f"Knowledge base '{name}' has no readable documents")
        self._init_common(name, chunks, top_k, score_threshold, rerank_model, candidate_k,
                           query_expansion_model, query_expansion_count)
        self._embeddings = resolve_embedding_model(embedding_model)
        self._set_vectors(name, vectors)
        return self

    def _init_common(
        self, name, chunks, top_k, score_threshold, rerank_model, candidate_k,
        query_expansion_model, query_expansion_count,
    ) -> None:
        try:
            import numpy as np  # noqa: F401
        except ImportError as exc:
            raise ConfigurationError(
                "Vector knowledge bases require the 'numpy' package. "
                "Install it with: pip install 'bestteam[tools-rag-vector]'"
            ) from exc

        self.name = name
        self.default_top_k = top_k
        self.score_threshold = score_threshold
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count
        self._chunks = chunks

    def _set_vectors(self, name: str, vectors: List[List[float]]) -> None:
        import numpy as np

        if not vectors or len(vectors) != len(self._chunks):
            raise ConfigurationError(
                f"Knowledge base '{name}': embedding model returned "
                f"{len(vectors)} vectors for {len(self._chunks)} chunks"
            )
        matrix = np.array(vectors, dtype=np.float64)
        self._matrix = normalize_rows(matrix)
```

Note `query()`'s existing body is untouched — it still reads
`self._embeddings` to embed the query text (`self._embeddings.embed_query(...)`
inside `_vector_leg`), so `from_chunks` resolves and sets `self._embeddings`
too, even though the document vectors themselves are supplied pre-computed
(only the per-document embedding CALL is skipped, not the model
resolution).

This changes `__init__`'s validation order slightly: the "no readable
documents" check now runs before the numpy-availability check inside
`_init_common` (previously numpy was checked first, at the very top of
`__init__`). Update the test that currently asserts on that ordering: run
`./.venv/Scripts/python.exe -m pytest tests/test_vector_knowledge_base.py -k numpy -v`
first to find it, then check whichever fixture it patches — if it patches
`sys.modules["numpy"] = None` while ALSO pointing `path` at an empty
directory, adjust the test to use a directory with at least one readable
file (so it exercises the numpy-missing branch inside `_init_common`
specifically, not the newly-reordered "no readable documents" branch which
now runs first for an empty directory). This is the only expected
behavior-order change from the refactor; everything else must produce
identical results to before.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_vector_knowledge_base.py -v`
Expected: PASS, including every pre-existing test.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/vector_knowledge_base.py tests/test_vector_knowledge_base.py
git commit -m "feat(sdk): add VectorKnowledgeBase.from_chunks alternate constructor"
```

---

## Task 4: SDK — `HybridKnowledgeBase.from_chunks`

**Files:**
- Modify: `src/bestteam/core/hybrid_knowledge_base.py`
- Test: `tests/test_hybrid_knowledge_base.py`

**Interfaces:**
- Consumes: `_Chunk`, `resolve_embedding_model`, `normalize_rows`,
  `tokenize`, `significant_terms` (already imported in this file).
- Produces: `HybridKnowledgeBase.from_chunks(name: str, chunks:
  List[_Chunk], vectors: List[List[float]], embedding_model: Any, top_k:
  int = 5, score_threshold: Optional[float] = None, rerank_model: Any =
  None, candidate_k: Optional[int] = None, query_expansion_model: Any =
  None, query_expansion_count: int = 3) -> HybridKnowledgeBase` — combines
  Task 2's BM25-index-from-chunks logic with Task 3's
  vectors-from-precomputed logic.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hybrid_knowledge_base.py`:

```python
def test_from_chunks_builds_queryable_kb():
    from bestteam.core.knowledge_base import _Chunk
    from bestteam.core.embeddings import resolve_embedding_model

    chunks = [
        _Chunk(source="a.txt", text="Refunds are allowed within 30 days."),
        _Chunk(source="b.txt", text="Office hours are 9am to 5pm."),
    ]
    embeddings = resolve_embedding_model("fake:4")
    vectors = embeddings.embed_documents([c.text for c in chunks])

    kb = HybridKnowledgeBase.from_chunks("policies", chunks, vectors, "fake:4", top_k=1)
    result = kb.query("refund policy")
    assert "30 days" in result


def test_from_chunks_vector_count_mismatch_raises_configuration_error():
    from bestteam.core.knowledge_base import _Chunk

    chunks = [_Chunk(source="a.txt", text="hello")]
    with pytest.raises(ConfigurationError, match="embedding model returned"):
        HybridKnowledgeBase.from_chunks("kb", chunks, vectors=[], embedding_model="fake:4")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_hybrid_knowledge_base.py -k from_chunks -v`
Expected: FAIL with `AttributeError: type object 'HybridKnowledgeBase' has no attribute 'from_chunks'`

- [ ] **Step 3: Refactor `__init__` to delegate, add `from_chunks`**

In `src/bestteam/core/hybrid_knowledge_base.py`, replace the `__init__`
method (currently lines 40-109) with:

```python
    def __init__(
        self,
        name: str,
        path: str | Path,
        embedding_model: Any,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        cache_path: Optional[str | Path] = None,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> None:
        _validate_chunk_params(name, chunk_size, chunk_overlap)
        self.path = Path(path)
        chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        if not chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents in {self.path}"
            )
        self._init_common(name, chunks, top_k, score_threshold, rerank_model, candidate_k,
                           query_expansion_model, query_expansion_count)
        self._embeddings = resolve_embedding_model(embedding_model)
        vectors = self._embed_chunks(embedding_model, cache_path)
        self._set_vectors(name, vectors)

    @classmethod
    def from_chunks(
        cls,
        name: str,
        chunks: List["_Chunk"],
        vectors: List[List[float]],
        embedding_model: Any,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> "HybridKnowledgeBase":
        """Build directly from pre-parsed chunks and pre-computed vectors --
        see VectorKnowledgeBase.from_chunks for the embedding_model
        rationale (query-time embedding still needs a live model). Used by
        the backend's DB-backed ingestion path."""
        self = cls.__new__(cls)
        self.path = None
        if not chunks:
            raise ConfigurationError(f"Knowledge base '{name}' has no readable documents")
        self._init_common(name, chunks, top_k, score_threshold, rerank_model, candidate_k,
                           query_expansion_model, query_expansion_count)
        self._embeddings = resolve_embedding_model(embedding_model)
        self._set_vectors(name, vectors)
        return self

    def _init_common(
        self, name, chunks, top_k, score_threshold, rerank_model, candidate_k,
        query_expansion_model, query_expansion_count,
    ) -> None:
        # numpy is checked before rank_bm25: rank_bm25 imports numpy
        # internally, so if rank_bm25 hasn't been imported yet in this
        # process and numpy is unavailable, `import rank_bm25` fails too --
        # checking numpy first ensures a numpy-only failure is reported as
        # "numpy", not misattributed to "rank-bm25".
        try:
            import numpy as np  # noqa: F401
        except ImportError as exc:
            raise ConfigurationError(
                "Hybrid knowledge bases require the 'numpy' package. "
                "Install it with: pip install 'bestteam[tools-rag-vector]'"
            ) from exc
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ConfigurationError(
                "Hybrid knowledge bases require the 'rank-bm25' package. "
                "Install it with: pip install 'bestteam[tools-rag]'"
            ) from exc

        self.name = name
        self.default_top_k = top_k
        self.score_threshold = score_threshold
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)

        self._chunks = chunks
        self._chunk_tokens = [tokenize(chunk.text) for chunk in self._chunks]
        self._chunk_terms = [significant_terms(tokens) for tokens in self._chunk_tokens]
        self._bm25 = BM25Okapi(self._chunk_tokens)

    def _set_vectors(self, name: str, vectors: List[List[float]]) -> None:
        import numpy as np

        if not vectors or len(vectors) != len(self._chunks):
            raise ConfigurationError(
                f"Knowledge base '{name}': embedding model returned "
                f"{len(vectors)} vectors for {len(self._chunks)} chunks"
            )
        matrix = np.array(vectors, dtype=np.float64)
        self._matrix = normalize_rows(matrix)
```

Same test-ordering caveat as Task 3 applies here for the
numpy/rank-bm25-missing tests (the "no readable documents" check now runs
in `__init__` before `_init_common`'s numpy/rank-bm25 checks — verify
against `tests/test_hybrid_knowledge_base.py`'s existing missing-dependency
tests and point them at a non-empty directory if needed).

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_hybrid_knowledge_base.py -v`
Expected: PASS, including every pre-existing test.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/hybrid_knowledge_base.py tests/test_hybrid_knowledge_base.py
git commit -m "feat(sdk): add HybridKnowledgeBase.from_chunks alternate constructor"
```

---

## Task 5: Backend — ingestion job execution module

**Files:**
- Create: `ui/backend/ingestion.py`
- Test: `tests/test_ingestion.py`

**Interfaces:**
- Consumes: `IngestionJob`, `KnowledgeDocument`, `KnowledgeChunk` (Task 1);
  `_chunk_text`, `_SUPPORTED_SUFFIXES` from `bestteam.core.knowledge_base`;
  `parse_file` from `bestteam.tools`; `resolve_embedding_model` from
  `bestteam.core.embeddings`; `ConfigurationError`/`BestTeamError` from
  `bestteam.exceptions`.
- Produces:
  - `_executor: ThreadPoolExecutor` — module-level, `max_workers=4,
    thread_name_prefix="bestteam-ingest"`.
  - `run_ingestion_job(job_id: int, kb_id: int, org_id: Optional[int],
    version_dir: Path, kb_type: str, chunk_size: int, chunk_overlap: int,
    embedding_model: Optional[str], engine: Engine) -> None` — the
    synchronous worker-thread function (mirrors `runtime.py::run_in_background`'s
    shape: opens its own `Session(engine)`, never raises).
  - `delete_kb_ingestion_data(db: Session, kb_id: int) -> None` — bulk-deletes
    every `KnowledgeChunk`/`KnowledgeDocument`/`IngestionJob` row for a KB
    (does not commit — caller commits, matching `crud.py`'s existing delete
    transaction shape).
  - `job_status_payload(db: Session, job: IngestionJob) -> Dict[str, Any]`
    — `{"job_id", "status", "file_count", "documents_succeeded",
    "documents_failed", "chunk_count", "errors": [{"filename", "error"}, ...
    up to 10], "config": dict | None}` (`config` only populated when
    `status == "completed"`, read from the job's `KnowledgeBaseRecord`).
  - `_MAX_ERROR_CHARS = 2000`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingestion.py`:

```python
"""Tests for the async knowledge-base ingestion job (ui/backend/ingestion.py)."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from ui.backend import ingestion
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord, KnowledgeChunk, KnowledgeDocument


@pytest.fixture
def engine():
    eng = make_engine(":memory:")
    init_db(eng)
    return eng


@pytest.fixture
def db(engine):
    Session = session_factory(engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _make_kb(db, name="policies"):
    kb = KnowledgeBaseRecord(name=name, org_id=1, config={"name": name, "type": "local_folder", "path": "x"})
    db.add(kb)
    db.commit()
    return kb


def _make_job(db, kb, version="v_test"):
    job = IngestionJob(kb_id=kb.id, org_id=1, version=version, status="queued", file_count=1)
    db.add(job)
    db.commit()
    return job


def test_successful_ingestion_marks_job_completed_and_writes_chunks(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("Refunds are allowed within 30 days.", encoding="utf-8")

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "completed"
    assert job.documents_succeeded == 1
    assert job.documents_failed == 0
    docs = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id).all()
    assert len(docs) == 1
    assert docs[0].status == "chunked"
    chunks = db.query(KnowledgeChunk).filter_by(document_id=docs[0].id).all()
    assert len(chunks) == 1
    assert "30 days" in chunks[0].text
    assert chunks[0].embedding_json is None


def test_one_bad_file_does_not_fail_the_whole_job(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "good.txt").write_text("Refunds within 30 days.", encoding="utf-8")
    # An unsupported/corrupt file that parse_file will raise on: use a
    # .pdf extension (in _SUPPORTED_SUFFIXES) whose content isn't valid PDF.
    (version_dir / "bad.pdf").write_bytes(b"not a real pdf")

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "completed"
    assert job.documents_succeeded == 1
    assert job.documents_failed == 1
    failed_doc = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id, status="failed").one()
    assert failed_doc.error is not None
    assert len(failed_doc.error) <= ingestion._MAX_ERROR_CHARS


def test_total_failure_marks_job_failed(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "bad.pdf").write_bytes(b"not a real pdf")

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "failed"
    assert job.error is not None


def test_empty_file_produces_zero_chunks_and_does_not_count_as_servable(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "empty.txt").write_text("   ", encoding="utf-8")  # blank after strip()

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    # Parsed "successfully" (no exception) but produced zero chunks -- must
    # not resolve to a completed job with nothing to serve.
    assert job.status == "failed"


def test_vector_kb_embeds_all_chunks(db, engine, tmp_path):
    kb = _make_kb(db, name="vec_kb")
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("Refunds within 30 days.", encoding="utf-8")

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="vector", chunk_size=1000, chunk_overlap=100, embedding_model="fake:4",
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "completed"
    chunk = db.query(KnowledgeChunk).filter_by(kb_id=kb.id).one()
    assert chunk.embedding_json is not None
    assert chunk.embedding_model == "fake:4"


def test_vector_kb_bad_embedding_model_fails_whole_job(db, engine, tmp_path):
    kb = _make_kb(db, name="vec_kb")
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("Refunds within 30 days.", encoding="utf-8")

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="vector", chunk_size=1000, chunk_overlap=100, embedding_model="not-a-real-spec-format:x",
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "failed"
    assert db.query(KnowledgeChunk).filter_by(kb_id=kb.id).count() == 0


def test_delete_kb_ingestion_data_removes_all_rows(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("hello world", encoding="utf-8")
    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )
    db.expire_all()

    ingestion.delete_kb_ingestion_data(db, kb.id)
    db.commit()

    assert db.query(IngestionJob).filter_by(kb_id=kb.id).count() == 0
    assert db.query(KnowledgeDocument).filter_by(kb_id=kb.id).count() == 0
    assert db.query(KnowledgeChunk).filter_by(kb_id=kb.id).count() == 0


def test_job_status_payload_includes_config_only_when_completed(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("hello world", encoding="utf-8")

    payload = ingestion.job_status_payload(db, job)
    assert payload["status"] == "queued"
    assert payload["config"] is None

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )
    db.expire_all()
    job = db.get(IngestionJob, job.id)
    payload = ingestion.job_status_payload(db, job)
    assert payload["status"] == "completed"
    assert payload["chunk_count"] == 1
    assert payload["config"] == kb.config
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ingestion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.backend.ingestion'`

- [ ] **Step 3: Implement `ui/backend/ingestion.py`**

```python
"""Async knowledge-base ingestion: parses, chunks, and (for vector/hybrid)
embeds an uploaded KB's documents on a background thread, persisting
KnowledgeDocument/KnowledgeChunk rows keyed to an IngestionJob.

A KB's live document set is always its most recent `completed` job's rows
-- the status="completed" flip is the atomic swap (no CURRENT-pointer file
needed for this path). See ui/backend/knowledge_bases.py (dispatch site,
read path) and
docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from bestteam.core.embeddings import resolve_embedding_model
from bestteam.core.knowledge_base import _SUPPORTED_SUFFIXES, _chunk_text
from bestteam.exceptions import BestTeamError
from bestteam.tools import parse_file

from .db.models import IngestionJob, KnowledgeBaseRecord, KnowledgeChunk, KnowledgeDocument

_logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-ingest")

_MAX_ERROR_CHARS = 2000

# How many completed-job generations to retain per KB: the current one plus
# one grace-window generation, matching the file-based path's CR-008
# "prior version kept only until the new one is durable" precedent.
_KEEP_COMPLETED_GENERATIONS = 2


def _capped(text: str) -> str:
    return text[:_MAX_ERROR_CHARS]


def run_ingestion_job(
    job_id: int,
    kb_id: int,
    org_id: Optional[int],
    version_dir: Path,
    *,
    kb_type: str,
    chunk_size: int,
    chunk_overlap: int,
    embedding_model: Optional[str],
    engine: Engine,
) -> None:
    """Parse/chunk/embed `version_dir`'s files into Document/Chunk rows for
    `job_id`, then resolve the job to completed/failed. Runs on a worker
    thread (submitted via `_executor.submit`); opens its own `Session` on
    `engine` since a Session isn't thread-safe to share with the dispatching
    request. Never raises -- any unexpected failure is caught and recorded
    on the job row, mirroring runtime.py::run_in_background's shape.
    """
    db = Session(engine)
    try:
        job = db.get(IngestionJob, job_id)
        if job is None:
            return
        job.status = "running"
        db.commit()

        all_chunks: List[KnowledgeChunk] = []
        files = sorted(
            p for p in version_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in _SUPPORTED_SUFFIXES
        )
        for file_path in files:
            data = file_path.read_bytes()
            doc = KnowledgeDocument(
                kb_id=kb_id,
                ingestion_job_id=job.id,
                filename=file_path.relative_to(version_dir).as_posix(),
                content_hash=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                status="parsing",
            )
            db.add(doc)
            db.flush()

            try:
                text = parse_file(str(file_path))
                pieces = _chunk_text(text, chunk_size, chunk_overlap, suffix=file_path.suffix.lower())
                if not pieces:
                    raise ValueError("document produced no chunks (empty or whitespace-only content)")
            except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
                doc.status = "failed"
                doc.error = _capped(str(exc))
                job.documents_failed += 1
                continue

            for i, piece in enumerate(pieces):
                chunk = KnowledgeChunk(document_id=doc.id, kb_id=kb_id, chunk_index=i, text=piece)
                db.add(chunk)
                all_chunks.append(chunk)
            doc.status = "chunked"
            job.documents_succeeded += 1

        if kb_type in ("vector", "hybrid") and all_chunks:
            try:
                embeddings = resolve_embedding_model(embedding_model)
                vectors = embeddings.embed_documents([c.text for c in all_chunks])
                if len(vectors) != len(all_chunks):
                    raise ValueError(
                        f"embedding model returned {len(vectors)} vectors for {len(all_chunks)} chunks"
                    )
            except Exception as exc:  # noqa: BLE001 -- a vector/hybrid KB can't function unembedded
                job.status = "failed"
                job.error = _capped(str(exc))
                job.completed_at = _now(db)
                db.commit()
                return
            for chunk, vector in zip(all_chunks, vectors):
                chunk.embedding_json = json.dumps(vector)
                chunk.embedding_model = embedding_model

        if all_chunks:
            job.status = "completed"
        else:
            job.status = "failed"
            job.error = _capped("Knowledge base has no readable documents")
        job.completed_at = _now(db)
        db.commit()

        if job.status == "completed":
            _prune_old_ingestion_versions(db, kb_id, version_dir.parent)
    except Exception:  # noqa: BLE001 -- a worker-thread failure must never propagate silently
        _logger.exception("Ingestion job %s failed on the worker thread", job_id)
        try:
            db.rollback()
            job = db.get(IngestionJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = "The ingestion job failed due to an internal error."
                job.completed_at = _now(db)
                db.commit()
        except Exception:  # noqa: BLE001
            _logger.warning("Could not persist failed status for ingestion job %s", job_id)
    finally:
        db.close()


def _now(db: Session):
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _prune_old_ingestion_versions(db: Session, kb_id: int, kb_root: Path) -> None:
    """Keep only the `_KEEP_COMPLETED_GENERATIONS` most recent completed
    jobs for this KB; delete every older completed job's rows and on-disk
    version directory. A failed/queued/running job is never pruned here --
    only completed jobs count as "old versions" (a still-failed job's rows
    are its own diagnostic record, left for the operator/customer to see).
    """
    completed = (
        db.query(IngestionJob)
        .filter_by(kb_id=kb_id, status="completed")
        .order_by(IngestionJob.completed_at.desc())
        .all()
    )
    for old_job in completed[_KEEP_COMPLETED_GENERATIONS:]:
        db.query(KnowledgeChunk).filter(
            KnowledgeChunk.document_id.in_(
                db.query(KnowledgeDocument.id).filter_by(ingestion_job_id=old_job.id)
            )
        ).delete(synchronize_session=False)
        db.query(KnowledgeDocument).filter_by(ingestion_job_id=old_job.id).delete(synchronize_session=False)
        version_dir = kb_root / old_job.version
        if version_dir.is_dir():
            import shutil

            shutil.rmtree(version_dir, ignore_errors=True)
        db.delete(old_job)
    if completed[_KEEP_COMPLETED_GENERATIONS:]:
        db.commit()


def delete_kb_ingestion_data(db: Session, kb_id: int) -> None:
    """Bulk-delete every IngestionJob/KnowledgeDocument/KnowledgeChunk row
    for a KB. Does NOT commit -- called from crud.py's delete route inside
    its own existing delete+commit+rmtree transaction, so this participates
    in that same commit rather than creating a separate one."""
    db.query(KnowledgeChunk).filter_by(kb_id=kb_id).delete(synchronize_session=False)
    db.query(KnowledgeDocument).filter_by(kb_id=kb_id).delete(synchronize_session=False)
    db.query(IngestionJob).filter_by(kb_id=kb_id).delete(synchronize_session=False)


def job_status_payload(db: Session, job: IngestionJob) -> Dict[str, Any]:
    """Format one IngestionJob for the ingestion-jobs read API (shared by
    the admin and org-scoped routes -- see ui/backend/crud.py,
    ui/backend/org_knowledge_bases.py)."""
    chunk_count = db.query(KnowledgeChunk).filter_by(kb_id=job.kb_id).join(
        KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id
    ).filter(KnowledgeDocument.ingestion_job_id == job.id).count()
    failed_docs = (
        db.query(KnowledgeDocument)
        .filter_by(ingestion_job_id=job.id, status="failed")
        .limit(10)
        .all()
    )
    config = None
    if job.status == "completed":
        kb = db.get(KnowledgeBaseRecord, job.kb_id)
        if kb is not None:
            config = kb.config
    return {
        "job_id": job.id,
        "status": job.status,
        "file_count": job.file_count,
        "documents_succeeded": job.documents_succeeded,
        "documents_failed": job.documents_failed,
        "chunk_count": chunk_count,
        "errors": [{"filename": d.filename, "error": d.error} for d in failed_docs],
        "config": config,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ingestion.py -v`
Expected: PASS.

For `test_one_bad_file_does_not_fail_the_whole_job` and
`test_total_failure_marks_job_failed`: verify that `parse_file()` on a
`.pdf` file with content `b"not a real pdf"` actually raises (rather than,
say, returning empty text silently). If `parse_file` instead returns `""`
for malformed PDF content without raising, `_chunk_text("")` returns `[]`,
which the `if not pieces: raise ValueError(...)` branch inside the `try`
already converts into the same per-document `status="failed"` outcome —
either way the test's assertions (`documents_failed == 1`,
`failed_doc.error is not None`) hold. If it does something else entirely
(e.g., raises an exception whose type breaks the `except Exception` catch —
not possible in Python, `Exception` catches everything non-`BaseException`),
no adjustment needed.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/ingestion.py tests/test_ingestion.py
git commit -m "feat(backend): add async knowledge-base ingestion job module"
```

---

## Task 6: Backend — rewire `upload_knowledge_base()` to dispatch async jobs

**Files:**
- Modify: `ui/backend/knowledge_bases.py`
- Modify: `ui/backend/crud.py` (upload route: add `created_by`)
- Modify: `ui/backend/org_knowledge_bases.py` (upload route: add `created_by`)
- Test: `tests/test_org_knowledge_bases.py`
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `ingestion._executor`, `ingestion.run_ingestion_job` (Task 5).
- Produces: `upload_knowledge_base(..., created_by: Optional[str] = None)
  -> Dict[str, Any]` now returns `{"name": item_name, "job_id": int,
  "status": "queued"}` instead of `{"name", "config", "file_count",
  "chunk_count"}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_org_knowledge_bases.py`, update the response-shape
assertions. Replace `test_org_member_can_upload_own_kb`:

```python
def test_org_member_can_upload_own_kb(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "policies"
    assert body["status"] == "queued"
    assert isinstance(body["job_id"], int)
```

Add a new test verifying the job actually completes and the KB becomes
queryable (this exercises the real dispatch path end-to-end, not
`run_ingestion_job` called directly like Task 5's tests do):

```python
import time


def test_uploaded_kb_ingestion_job_completes_and_becomes_queryable(client, tmp_path):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        from ui.backend.db.models import IngestionJob

        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            db.expire_all()
            job = db.get(IngestionJob, job_id)
            if job is not None and job.status in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert job is not None and job.status == "completed"

        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert "30 days" in tools["policies"]("refund policy")
```

Update `test_smart_search_upload_builds_hybrid_kb_with_expansion_and_rerank`
(from the earlier smart-search work) to poll for job completion the same
way, then assert `job.status == "completed"` — the `config` assertions
(`type == "hybrid"`, `embedding_model`, etc.) move from reading
`resp.json()["config"]` to reading the `KnowledgeBaseRecord.config` fetched
from `open_test_db()` after the job completes (the upload response no
longer carries `config`).

In `tests/test_crud_api.py`, find the test(s) asserting on
`upload_knowledge_base_files`'s response shape (`chunk_count`/`config`
keys) and update them the same way: assert `job_id`/`status == "queued"`
on the immediate response, then poll `IngestionJob` to `completed` before
asserting on `config`/chunk-derived behavior. Also revisit
`test_failed_reupload_preserves_prior_kb` (patches
`backend_knowledge_bases._build_knowledge_base` with a `ConfigurationError`
side effect) — `upload_knowledge_base()` no longer calls
`_build_knowledge_base` for the validation step (Step 3 below removes that
call entirely; validation is now just `kb_type`/chunk-param checks). Change
this test's patch target to raise from the chunk-param validation instead:
patch `backend_knowledge_bases._validate_chunk_params` with a
`side_effect=ConfigurationError("bad upload")`, keeping the same assertion
that the prior KB's content survives a failed re-upload attempt.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_knowledge_bases.py tests/test_crud_api.py -v`
Expected: FAIL — the current synchronous response still has `config`/
`chunk_count`, not `job_id`/`status`.

- [ ] **Step 3: Rewrite `upload_knowledge_base()`**

In `ui/backend/knowledge_bases.py`:

Add to the imports at the top:

```python
from bestteam.core.knowledge_base import _validate_chunk_params
from bestteam.core.loader import _KNOWLEDGE_BASE_TYPES

from . import ingestion
from .db.models import IngestionJob, KnowledgeBaseRecord
```

(`KnowledgeBaseRecord` is already imported — don't duplicate; add
`IngestionJob` to that existing import line instead of a new one.)

Replace the body of `upload_knowledge_base()` from the `with
_kb_upload_lock(...)` block onward (i.e. everything currently inside that
`with` block, replacing the whole `try:`/`except Exception:` structure at
lines 232-317 in the file as read) with:

```python
    if kb_type not in _KNOWLEDGE_BASE_TYPES:
        valid = ", ".join(sorted(_KNOWLEDGE_BASE_TYPES))
        raise HTTPException(
            status_code=400,
            detail=f"Knowledge base has unknown type '{kb_type}'. Valid types: {valid}",
        )
    try:
        _validate_chunk_params(item_name, chunk_size, chunk_overlap)
    except BestTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with _kb_upload_lock(f"{org_id}/{item_name}"):
        version_dir.mkdir(parents=True, exist_ok=True)
        try:
            for filename, data in contents.items():
                (version_dir / filename).write_bytes(data)

            spec = KnowledgeBaseSpec(
                name=item_name,
                path=str(kb_root),
                type=kb_type,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                top_k=top_k,
                embedding_model=embedding_model if kb_type in ("vector", "hybrid") else None,
                cache_path=(f"kb_{org_id}_{item_name}.json" if kb_type in ("vector", "hybrid") else None),
                rerank_model=rerank_model,
                query_expansion_model=query_expansion_model,
            )
            raw = spec.to_raw()
            item = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
            if item is None:
                item = KnowledgeBaseRecord(name=item_name, config=raw, org_id=org_id)
                db.add(item)
            else:
                item.config = raw
            db.flush()  # need item.id below, before the outer commit

            job = IngestionJob(
                kb_id=item.id,
                org_id=org_id,
                version=version,
                status="queued",
                file_count=len(contents),
                created_by=created_by,
            )
            db.add(job)
            db.commit()

            ingestion._executor.submit(
                ingestion.run_ingestion_job,
                job.id,
                item.id,
                org_id,
                version_dir,
                kb_type=kb_type,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                embedding_model=embedding_model,
                engine=db.get_bind(),
            )

            return {"name": item_name, "job_id": job.id, "status": "queued"}
        except Exception:
            # No CURRENT pointer is written for the DB-backed path (nothing
            # reads it -- retrieval resolves the live version via the
            # ingestion job's own status), so on a pre-dispatch failure the
            # only cleanup needed is the just-written version directory.
            db.rollback()
            shutil.rmtree(version_dir, ignore_errors=True)
            raise
```

Delete the now-unused `_KB_CURRENT_POINTER`-writing lines that used to sit
between file-writing and the KB-record save (`previous_version =
_read_pointer(pointer)` / `_write_pointer(pointer, version)`) — they are
part of the block being replaced above, so this happens automatically as
long as the full old block (through the old function's final `return`
statement and its enclosing `except Exception:` handler) is replaced by
the new code above. `_read_pointer`/`_write_pointer`/`_cleanup_kb_versions`
stay in this file, unchanged and still used — by `resolve_kb_upload_path()`
and the legacy (job-less) read path in Task 7.

Add `created_by: Optional[str] = None` to `upload_knowledge_base()`'s
signature (alongside the existing `query_expansion_model` parameter).

Update the function's docstring to describe the new async contract: the
returned dict is now `{"name", "job_id", "status"}`; callers poll
`GET .../ingestion-jobs/{job_id}` (Task 9) for completion.

- [ ] **Step 4: Thread `created_by` through both call sites**

In `ui/backend/org_knowledge_bases.py`, add `from .auth_api import
get_current_org, get_current_user` (extend the existing `from .auth_api
import get_current_org` line) and `from .db.models import ..., User` if not
already imported (check — `User` likely isn't imported in this file yet;
add it to the existing `from .db.models import KnowledgeBaseRecord,
Organization` line). Add a `user: User = Depends(get_current_user)`
parameter to `upload_own_knowledge_base`, and pass `created_by=user.username`
into the `upload_knowledge_base(...)` call at the bottom of that function.

In `ui/backend/crud.py`, add `admin: User = Depends(get_current_admin)` to
`upload_knowledge_base_files`'s parameters (matching `upsert_item`'s own
pattern in the same file) and pass `created_by=admin.username` into its
`upload_knowledge_base(...)` call.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_knowledge_bases.py tests/test_crud_api.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full backend suite for regressions**

Run: `./.venv/Scripts/python.exe -m pytest -m "unit or integration" -q`
Expected: PASS (any other test relying on the old synchronous
`chunk_count`/`config` upload response shape needs the same poll-then-assert
update as Step 1 — search for `upload_knowledge_base` and
`uploadKnowledgeBaseFiles`/`uploadOwnKnowledgeBaseFiles` call sites across
`tests/` and fix any stragglers the same way).

- [ ] **Step 7: Commit**

```bash
git add ui/backend/knowledge_bases.py ui/backend/crud.py ui/backend/org_knowledge_bases.py tests/test_org_knowledge_bases.py tests/test_crud_api.py
git commit -m "feat(backend): dispatch knowledge-base uploads as async ingestion jobs"
```

---

## Task 7: Backend — DB-backed read path with legacy fallback

**Files:**
- Modify: `ui/backend/knowledge_bases.py`
- Test: `tests/test_org_knowledge_bases.py`

**Interfaces:**
- Consumes: `LocalFolderKnowledgeBase.from_chunks`,
  `VectorKnowledgeBase.from_chunks`, `HybridKnowledgeBase.from_chunks`
  (Tasks 2-4); `IngestionJob`, `KnowledgeDocument`, `KnowledgeChunk` (Task 1).
- Modifies: `load_knowledge_base_tools(db, raw, source, *, org_id=None)` —
  signature unchanged (every existing call site in `crud.py`/`builder.py`/
  `main.py`/`email_trigger.py` needs no changes), only its internal
  per-record resolution logic changes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_org_knowledge_bases.py`:

```python
def test_completed_job_kb_serves_from_db_not_disk(client, tmp_path):
    """After the ingestion job completes, deleting the on-disk files must
    not affect retrieval -- proof the DB-backed read path never touches
    disk for a job-based KB."""
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    job_id = resp.json()["job_id"]

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        from ui.backend.db.models import IngestionJob
        import time

        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            db.expire_all()
            job = db.get(IngestionJob, job_id)
            if job is not None and job.status in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert job.status == "completed"

    # Blow away the whole upload tree -- if the read path fell back to
    # disk it would find nothing.
    import shutil

    shutil.rmtree(backend_knowledge_bases._KB_UPLOADS_DIR, ignore_errors=True)

    with open_test_db() as db:
        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert "30 days" in tools["policies"]("refund policy")


def test_kb_with_no_ingestion_job_falls_back_to_legacy_file_path(client, tmp_path, monkeypatch):
    """Simulates a pre-existing KB from before this feature: a
    KnowledgeBaseRecord whose config points at a real on-disk folder, with
    zero IngestionJob rows."""
    legacy_dir = tmp_path / "legacy_kb"
    legacy_dir.mkdir()
    (legacy_dir / "doc.txt").write_text("Refunds within 14 days.", encoding="utf-8")

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        from ui.backend.db.models import KnowledgeBaseRecord

        db.add(KnowledgeBaseRecord(
            name="legacy_kb", org_id=org_id,
            config={"name": "legacy_kb", "type": "local_folder", "path": str(legacy_dir)},
        ))
        db.commit()

        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert "14 days" in tools["legacy_kb"]("refund policy")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_knowledge_bases.py -k "db_not_disk or legacy_file_path" -v`
Expected: FAIL (`test_completed_job_kb_serves_from_db_not_disk` fails
because today's read path still reads from disk; the legacy test may
already incidentally pass since today's path IS the file-based one — if
so, that's fine, it's establishing a baseline that must keep passing after
Step 3).

- [ ] **Step 3: Add the DB-backed branch to `load_knowledge_base_tools`**

In `ui/backend/knowledge_bases.py`, add these imports:

```python
import json

from bestteam.core.hybrid_knowledge_base import HybridKnowledgeBase
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase, _Chunk
from bestteam.core.vector_knowledge_base import VectorKnowledgeBase

from .db.models import IngestionJob, KnowledgeChunk, KnowledgeDocument
```

(`KnowledgeChunk`/`KnowledgeDocument` may already be imported from Task 6's
changes if that task added them to the same `from .db.models import ...`
line — merge into one import line rather than duplicating.)

Replace the body of `load_knowledge_base_tools` from the `for record in
records:` loop onward with:

```python
    tools: Dict[str, Any] = {}
    for record in records:
        # Fail closed on a legacy KB whose name shadows a built-in tool (F4).
        if record.name in REGISTRY:
            raise ConfigurationError(
                f"Knowledge base '{record.name}' shadows a built-in tool of the "
                "same name; rename the knowledge base."
            )
        job = (
            db.query(IngestionJob)
            .filter_by(kb_id=record.id, status="completed")
            .order_by(IngestionJob.completed_at.desc())
            .first()
        )
        if job is not None:
            kb = _build_knowledge_base_from_job(record, job, db)
        else:
            # Pre-existing KB (predates this feature, never re-uploaded) --
            # fall back to the original file-based construction, unchanged.
            config = resolve_kb_upload_path(contain_kb_config_for_load(record.config))
            ensure_contained_cache_path_for_source(config, source)
            kb = _build_knowledge_base(config, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools


def _build_knowledge_base_from_job(record: KnowledgeBaseRecord, job: "IngestionJob", db: Session) -> Any:
    """Build the matching KnowledgeBase subclass from a completed job's
    Document/Chunk rows -- the DB-backed read path (see
    docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md).
    Never reads from disk."""
    rows = (
        db.query(KnowledgeChunk, KnowledgeDocument.filename)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .filter(KnowledgeDocument.ingestion_job_id == job.id)
        .order_by(KnowledgeDocument.filename, KnowledgeChunk.chunk_index)
        .all()
    )
    chunks = [_Chunk(source=filename, text=chunk.text) for chunk, filename in rows]

    config = record.config
    kb_type = config.get("type", "local_folder")
    common_kwargs: Dict[str, Any] = {
        "top_k": config.get("top_k", 5),
        "rerank_model": config.get("rerank_model"),
        "candidate_k": config.get("candidate_k"),
        "query_expansion_model": config.get("query_expansion_model"),
        "query_expansion_count": config.get("query_expansion_count", 3),
    }

    if kb_type == "local_folder":
        return LocalFolderKnowledgeBase.from_chunks(record.name, chunks, **common_kwargs)

    vectors = [json.loads(chunk.embedding_json) for chunk, _filename in rows]
    embedding_model = config.get("embedding_model")
    vector_kwargs = {**common_kwargs, "score_threshold": config.get("score_threshold")}
    if kb_type == "vector":
        return VectorKnowledgeBase.from_chunks(record.name, chunks, vectors, embedding_model, **vector_kwargs)
    if kb_type == "hybrid":
        return HybridKnowledgeBase.from_chunks(record.name, chunks, vectors, embedding_model, **vector_kwargs)
    raise ConfigurationError(f"Knowledge base '{record.name}' has unknown type '{kb_type}'")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_knowledge_bases.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full backend suite for regressions**

Run: `./.venv/Scripts/python.exe -m pytest -m "unit or integration" -q`
Expected: PASS. Pay particular attention to
`tests/test_crud_api.py`/`tests/test_builder_api.py`/anything exercising
`load_knowledge_base_tools`, `_all_knowledge_base_tools`, or the
autonomous-trigger loader (`email_trigger.py`) — every one of those
consumers is unaffected in signature but now has two live code paths
inside; a pre-existing test using a manually-configured (non-upload) KB
must keep exercising the legacy branch (it will — those KBs have no
`IngestionJob` rows).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/knowledge_bases.py tests/test_org_knowledge_bases.py
git commit -m "feat(backend): read knowledge-base chunks from the DB when an ingestion job exists"
```

---

## Task 8: Backend — KB deletion cascade

**Files:**
- Modify: `ui/backend/crud.py`
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `ingestion.delete_kb_ingestion_data` (Task 5).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_crud_api.py` (find the existing KB-delete test(s) for
the surrounding pattern/fixtures to match):

```python
def test_deleting_kb_removes_ingestion_rows(client, tmp_path):
    # Upload creates a KnowledgeBaseRecord + an IngestionJob.
    resp = client.post(
        "/api/config/knowledge_bases/policies/upload",
        files=[("files", ("doc.txt", b"Refunds within 30 days.", "text/plain"))],
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    with open_test_db() as db:
        from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord

        import time

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            db.expire_all()
            job = db.get(IngestionJob, job_id)
            if job is not None and job.status in ("completed", "failed"):
                break
            time.sleep(0.05)
        kb_id = db.query(KnowledgeBaseRecord).filter_by(name="policies").one().id

    resp = client.delete("/api/config/knowledge_bases/policies")
    assert resp.status_code == 204

    with open_test_db() as db:
        from ui.backend.db.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

        assert db.query(IngestionJob).filter_by(kb_id=kb_id).count() == 0
        assert db.query(KnowledgeDocument).filter_by(kb_id=kb_id).count() == 0
        assert db.query(KnowledgeChunk).filter_by(kb_id=kb_id).count() == 0
```

(Adjust the upload call's exact request shape and auth headers to match
whatever fixture/helper the rest of `tests/test_crud_api.py` already uses
for an admin-authenticated client — follow the pattern of the nearest
existing KB upload/delete test in that file rather than inventing a new
one.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k deleting_kb_removes_ingestion_rows -v`
Expected: FAIL — the rows still exist after delete (no cascade yet).

- [ ] **Step 3: Add the cascade to the delete route**

In `ui/backend/crud.py`, add `from .ingestion import delete_kb_ingestion_data`
to the imports. In `delete_item`'s `if name == "knowledge_bases":` branch
(inside the `with _kb_upload_lock(...)` block), call the cascade delete
right before `db.delete(item)`:

```python
            if name == "knowledge_bases":
                with _kb_upload_lock(f"{org_id}/{item_name}"):
                    delete_kb_ingestion_data(db, item.id)
                    db.delete(item)
                    db.commit()
                    upload_dir = _KB_UPLOADS_DIR / str(org_id) / item_name
                    if upload_dir.is_dir():
                        try:
                            shutil.rmtree(upload_dir)
                        except OSError as exc:
                            _logger.warning(
                                "Knowledge base '%s' (org %s) deleted, but its upload "
                                "directory couldn't be removed: %s",
                                item_name, org_id, exc,
                            )
```

(This is the same block already in the file — only the added
`delete_kb_ingestion_data(db, item.id)` line is new, placed before
`db.delete(item)` so both deletes land in the one existing commit.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -v`
Expected: PASS, including every pre-existing KB-delete test (409-while-referenced,
built-in-skill-undeletable, etc. — none of that logic changed).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/crud.py tests/test_crud_api.py
git commit -m "fix(backend): cascade-delete ingestion job/document/chunk rows on KB delete"
```

---

## Task 9: Backend — job-status API endpoints

**Files:**
- Modify: `ui/backend/crud.py` (admin route)
- Modify: `ui/backend/org_knowledge_bases.py` (org-scoped route)
- Test: `tests/test_crud_api.py`
- Test: `tests/test_org_knowledge_bases.py`

**Interfaces:**
- Produces: `GET /api/config/knowledge_bases/{item_name}/ingestion-jobs/{job_id}`
  and `GET /api/org/knowledge-bases/{item_name}/ingestion-jobs/{job_id}`,
  both returning `ingestion.job_status_payload(db, job)`'s shape (Task 5),
  404 for an unknown/other-org/other-KB job id.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_org_knowledge_bases.py`:

```python
def test_ingestion_job_status_endpoint(client, tmp_path):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    job_id = resp.json()["job_id"]

    resp = client.get(f"/api/org/knowledge-bases/policies/ingestion-jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] in ("queued", "running", "completed")
    assert "errors" in body
    assert "chunk_count" in body


def test_ingestion_job_status_404_for_unknown_job(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    resp = client.get("/api/org/knowledge-bases/policies/ingestion-jobs/999999")
    assert resp.status_code == 404


def test_ingestion_job_status_404_for_another_orgs_job(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    job_id = resp.json()["job_id"]

    other = create_user_and_login(client, username="bob", org="org_b")
    resp = client.get(
        f"/api/org/knowledge-bases/policies/ingestion-jobs/{job_id}",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404
```

Add analogous tests to `tests/test_crud_api.py` for the admin route
(`GET /api/config/knowledge_bases/{item_name}/ingestion-jobs/{job_id}`),
following that file's existing admin-auth fixture pattern.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_knowledge_bases.py -k ingestion_job_status -v`
Expected: FAIL with 404 (route doesn't exist yet — FastAPI returns 404 for
an unmatched route too, so also check the response body/`detail` to
confirm it's genuinely "not found route" not "not found job"; either way
this step is red before Step 3).

- [ ] **Step 3: Add the org-scoped route**

In `ui/backend/org_knowledge_bases.py`, add `from .ingestion import
job_status_payload` and `from .db.models import IngestionJob` (merge into
the existing `from .db.models import KnowledgeBaseRecord, Organization,
User` import line from Task 6). Add:

```python
@router.get("/knowledge-bases/{item_name}/ingestion-jobs/{job_id}")
def get_ingestion_job_status(
    item_name: str,
    job_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    kb = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org.id).one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge base '{item_name}'")
    job = db.query(IngestionJob).filter_by(id=job_id, kb_id=kb.id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown ingestion job")
    return job_status_payload(db, job)
```

- [ ] **Step 4: Add the admin route**

In `ui/backend/crud.py`, add `from .ingestion import (delete_kb_ingestion_data,
job_status_payload)` (merge with the import added in Task 8) and `from
.db.models import IngestionJob` (merge with existing model imports). Add,
near `upload_knowledge_base_files`:

```python
@router.get("/knowledge_bases/{item_name}/ingestion-jobs/{job_id}")
def get_ingestion_job_status(
    item_name: str,
    job_id: int,
    org: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    kb = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge_base '{item_name}'")
    job = db.query(IngestionJob).filter_by(id=job_id, kb_id=kb.id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown ingestion job")
    return job_status_payload(db, job)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_org_knowledge_bases.py tests/test_crud_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/org_knowledge_bases.py ui/backend/crud.py tests/test_org_knowledge_bases.py tests/test_crud_api.py
git commit -m "feat(backend): add ingestion-job status read endpoints"
```

---

## Task 10: Frontend — `DocumentsPage.tsx` polls ingestion to completion

**Files:**
- Modify: `ui/frontend/src/lib/types.ts`
- Modify: `ui/frontend/src/lib/api.ts`
- Modify: `ui/frontend/src/pages/wizard/DocumentsPage.tsx`
- Modify: `ui/frontend/src/pages/wizard/DocumentsPage.test.tsx`

**Interfaces:**
- Produces: `IngestionJobStatus` type; `api.orgKnowledgeBaseUploadJob(name:
  string, jobId: number) => Promise<IngestionJobStatus>`;
  `api.uploadOwnKnowledgeBaseFiles(...)`'s resolved value changes shape
  from `{name, file_count, chunk_count, config}` to `{name, job_id,
  status}`.

- [ ] **Step 1: Write the failing test**

In `ui/frontend/src/pages/wizard/DocumentsPage.test.tsx`, add
`orgKnowledgeBaseUploadJob: vi.fn()` to the `vi.mock('../../lib/api', ...)`
block. Update every test that currently mocks
`uploadOwnKnowledgeBaseFiles` to resolve `{name, file_count, chunk_count,
config}` — change those mocks to resolve `{name: '...', job_id: 1, status:
'queued'}` instead, and add `mockedApi.orgKnowledgeBaseUploadJob.mockResolvedValue({job_id: 1, status: 'completed', ...})`
(or set it per-test where a specific status transition matters) in
`beforeEach`.

Add a new test:

```typescript
it('polls the ingestion job to completion before generating the spec', async () => {
  mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
  mockedApi.orgKnowledgeBaseUploadJob
    .mockResolvedValueOnce({ job_id: 1, status: 'running', file_count: 1, documents_succeeded: 0, documents_failed: 0, chunk_count: 0, errors: [], config: null })
    .mockResolvedValueOnce({ job_id: 1, status: 'completed', file_count: 1, documents_succeeded: 1, documents_failed: 0, chunk_count: 2, errors: [], config: {} })
  mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

  renderPage()
  await screen.findByText('Add your documents')

  fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
  const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
  fireEvent.change(fileInput, { target: { files: [file] } })

  fireEvent.click(screen.getByText('Continue'))

  await waitFor(() => expect(mockedApi.orgKnowledgeBaseUploadJob).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(mockedApi.submitSpecification).toHaveBeenCalled())
})

it('shows an error and does not generate a spec when the ingestion job fails', async () => {
  mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
  mockedApi.orgKnowledgeBaseUploadJob.mockResolvedValue({
    job_id: 1, status: 'failed', file_count: 1, documents_succeeded: 0, documents_failed: 1,
    chunk_count: 0, errors: [{ filename: 'doc.txt', error: 'could not parse' }], config: null,
  })

  renderPage()
  await screen.findByText('Add your documents')

  fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
  const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
  fireEvent.change(fileInput, { target: { files: [file] } })

  fireEvent.click(screen.getByText('Continue'))

  await screen.findByText(/could not parse|processing failed/i)
  expect(mockedApi.submitSpecification).not.toHaveBeenCalled()
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ui/frontend && npx vitest run src/pages/wizard/DocumentsPage.test.tsx`
Expected: FAIL — `orgKnowledgeBaseUploadJob` doesn't exist on the mocked
`api`, and `proceed()` doesn't poll it.

- [ ] **Step 3: Add the type**

In `ui/frontend/src/lib/types.ts`, add:

```typescript
export interface IngestionJobStatus {
  job_id: number
  status: 'queued' | 'running' | 'completed' | 'failed'
  file_count: number
  documents_succeeded: number
  documents_failed: number
  chunk_count: number
  errors: { filename: string; error: string }[]
  config: ConfigItem | null
}
```

- [ ] **Step 4: Add the API method**

In `ui/frontend/src/lib/api.ts`, add `IngestionJobStatus` to the existing
type import list. Change `uploadOwnKnowledgeBaseFiles`'s return type
annotation from `{ name: string; file_count: number; chunk_count: number;
config: ConfigItem }` to `{ name: string; job_id: number; status: string
}`. Add:

```typescript
orgKnowledgeBaseUploadJob: (name: string, jobId: number) =>
  request<IngestionJobStatus>(
    `/api/org/knowledge-bases/${encodeURIComponent(name)}/ingestion-jobs/${jobId}`,
  ),
```

- [ ] **Step 5: Poll to completion in `DocumentsPage.tsx`**

In `ui/frontend/src/pages/wizard/DocumentsPage.tsx`:

Add `"ingesting"` to `STAGE_LABELS`:

```typescript
const STAGE_LABELS: Record<string, string> = {
  uploading: 'Uploading your documents…',
  ingesting: 'Processing your documents…',
  generating: 'Putting your team together…',
}
```

Update the `Stage` type: `type Stage = null | 'uploading' | 'ingesting' | 'generating'`.

Add a small polling helper above `DocumentsPage`:

```typescript
async function pollIngestionJob(slug: string, jobId: number): Promise<import('../../lib/types').IngestionJobStatus> {
  for (;;) {
    const job = await api.orgKnowledgeBaseUploadJob(slug, jobId)
    if (job.status === 'completed' || job.status === 'failed') return job
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
}
```

In `proceed()`, replace the `if (useFiles) { ... }` block's body (the
`try`/`catch` around `api.uploadOwnKnowledgeBaseFiles(...)`) so that after
a successful (or successfully-replaced) upload response, it polls the job
before moving on:

```typescript
    if (useFiles) {
      setStage('uploading')
      let uploadResult: { job_id: number }
      try {
        uploadResult = await api.uploadOwnKnowledgeBaseFiles(slug, files, false, smartSearchEnabled)
      } catch (e) {
        const err = e as Error & { status?: number }
        if (err.status === 409) {
          if (!window.confirm(`${err.message}\n\nReplace it with these documents?`)) {
            setBusy(false)
            setStage(null)
            return
          }
          try {
            uploadResult = await api.uploadOwnKnowledgeBaseFiles(slug, files, true, smartSearchEnabled)
          } catch (e2) {
            setError((e2 as Error).message)
            setBusy(false)
            setStage(null)
            return
          }
        } else {
          setError(err.message)
          setBusy(false)
          setStage(null)
          return
        }
      }

      setStage('ingesting')
      try {
        const job = await pollIngestionJob(slug, uploadResult.job_id)
        if (job.status === 'failed') {
          const detail = job.errors[0]?.error
          setError(detail ? `Processing failed: ${detail}` : 'Processing your documents failed.')
          setBusy(false)
          setStage(null)
          return
        }
      } catch (e) {
        setError((e as Error).message)
        setBusy(false)
        setStage(null)
        return
      }
    }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd ui/frontend && npx vitest run src/pages/wizard/DocumentsPage.test.tsx`
Expected: PASS. If the polling test hangs (real `setTimeout` in a test),
either use `vi.useFakeTimers()`/`vi.advanceTimersByTimeAsync(500)` around
the poll loop, or reduce the two-`mockResolvedValueOnce` sequence to
resolve `completed` on the *first* call so no real delay is exercised —
follow whichever pattern this repo's other polling-style frontend tests
already use (check `ActivityPage.test.tsx` or similar for the established
convention before picking one).

- [ ] **Step 7: Run the full frontend suite for regressions**

Run: `cd ui/frontend && npx vitest run`
Expected: PASS (172+ tests). Also run `npx tsc --noEmit -p .` from
`ui/frontend` — expected: no type errors.

- [ ] **Step 8: Commit**

```bash
git add ui/frontend/src/lib/types.ts ui/frontend/src/lib/api.ts ui/frontend/src/pages/wizard/DocumentsPage.tsx ui/frontend/src/pages/wizard/DocumentsPage.test.tsx
git commit -m "feat(frontend): poll knowledge-base ingestion job to completion in the wizard"
```

---

## Task 11: Frontend — `AdvancedPage.tsx` compatibility

**Files:**
- Modify: `ui/frontend/src/lib/api.ts`
- Modify: `ui/frontend/src/pages/AdvancedPage.tsx`
- Test: whichever existing test file covers `AdvancedPage`'s KB upload flow
  (locate via `Grep` for `uploadNew` or `uploadKnowledgeBaseFiles` under
  `ui/frontend/src` test files before writing new assertions, and follow
  its existing structure).

**Interfaces:**
- Produces: `api.knowledgeBaseUploadJob(name: string, jobId: number, org?:
  string) => Promise<IngestionJobStatus>` (the admin/`?org=` counterpart to
  Task 10's `orgKnowledgeBaseUploadJob`).

- [ ] **Step 1: Write the failing test**

Locate the existing AdvancedPage test file covering `uploadNew()`
(`Grep -e "uploadNew\|uploadKnowledgeBaseFiles" ui/frontend/src -r --include='*.test.tsx'`).
Update its mock of `api.uploadKnowledgeBaseFiles` to resolve `{name:
'policies', job_id: 1, status: 'queued'}` instead of the old
`{name, file_count, chunk_count, config}` shape, add a mock for the new
`api.knowledgeBaseUploadJob` resolving `{job_id: 1, status: 'completed',
file_count: 1, documents_succeeded: 1, documents_failed: 0, chunk_count: 2,
errors: [], config: {type: 'local_folder', ...}}`, and update the
assertion on the success message text (currently `` `Created
'${result.name}' — ${result.file_count} file(s), ${result.chunk_count}
chunk(s) indexed.` `` — Step 3 changes this to read from the polled job
instead of the immediate upload response) to match Step 3's new wording.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npx vitest run <the file located in Step 1>`
Expected: FAIL — `api.knowledgeBaseUploadJob` doesn't exist yet, and
`uploadNew()` still reads `result.chunk_count`/`result.config` directly
off the immediate (now job-shaped) response.

- [ ] **Step 3: Add the API method and update `uploadNew()`**

In `ui/frontend/src/lib/api.ts`, add:

```typescript
knowledgeBaseUploadJob: (name: string, jobId: number, org?: string) =>
  request<IngestionJobStatus>(
    `/api/config/knowledge_bases/${encodeURIComponent(name)}/ingestion-jobs/${jobId}` +
      (org ? `?org=${encodeURIComponent(org)}` : ''),
  ),
```

In `ui/frontend/src/pages/AdvancedPage.tsx`, replace `uploadNew()`'s body
from the `const result = await api.uploadKnowledgeBaseFiles(...)` line
through the `setJsonText(...)` line with:

```typescript
      const uploadResult = await api.uploadKnowledgeBaseFiles(newId.trim(), uploadFiles, apiOrg)
      let job = await api.knowledgeBaseUploadJob(uploadResult.name, uploadResult.job_id, apiOrg)
      while (job.status !== 'completed' && job.status !== 'failed') {
        await new Promise((resolve) => setTimeout(resolve, 500))
        job = await api.knowledgeBaseUploadJob(uploadResult.name, uploadResult.job_id, apiOrg)
      }
      if (job.status === 'failed') {
        const detail = job.errors[0]?.error
        throw new Error(detail ? `Processing failed: ${detail}` : 'Processing your documents failed.')
      }
      setMessage(`Created '${uploadResult.name}' — ${job.documents_succeeded} file(s), ${job.chunk_count} chunk(s) indexed.`)
      setNewId('')
      setUploadFiles([])
      setSelectedId(uploadResult.name)
      setJsonText(JSON.stringify(job.config, null, 2))
```

(The surrounding `try`/`catch`/`finally` in `uploadNew()` is unchanged —
this replaces only the success-path body inside the existing `try`, and a
thrown `Error` from the `job.status === 'failed'` branch is caught by the
existing `catch (e) { setError((e as Error).message) }`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npx vitest run <the file located in Step 1>`
Expected: PASS.

- [ ] **Step 5: Run the full frontend suite for regressions**

Run: `cd ui/frontend && npx vitest run && npx tsc --noEmit -p .`
Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/lib/api.ts ui/frontend/src/pages/AdvancedPage.tsx <the test file from Step 1>
git commit -m "feat(frontend): poll knowledge-base ingestion job to completion in Advanced page"
```

---

## Task 12: Full regression pass + documentation

**Files:**
- Modify: `docs/KNOWLEDGE_BASES.md`
- Modify: `src/bestteam/core/CLAUDE.md`
- Modify: `ui/backend/CLAUDE.md`
- Modify: `ui/backend/db/CLAUDE.md`
- Modify: `docs/STATUS.md`

- [ ] **Step 1: Run the full backend suite**

Run: `./.venv/Scripts/python.exe -m pytest -m "unit or integration" -q`
Expected: PASS, 0 failures. Note the total test count for the STATUS.md
entry in Step 6.

- [ ] **Step 2: Run the full frontend suite**

Run: `cd ui/frontend && npx vitest run && npx tsc --noEmit -p .`
Expected: both PASS.

- [ ] **Step 3: Update `docs/KNOWLEDGE_BASES.md`**

Read the file first. Add a new subsection (near the existing "Managing
knowledge bases through the backend API" section) documenting: uploads are
now asynchronous (`{name, job_id, status}` response), the new
`GET .../ingestion-jobs/{job_id}` endpoint and its response shape, the
DB-backed retrieval path for a KB with a completed ingestion job vs. the
legacy file-based fallback for a pre-existing KB, and the per-document
partial-failure semantics. Update the file-reference table to add
`ui/backend/ingestion.py`. Cross-reference
`docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`.

- [ ] **Step 4: Update `src/bestteam/core/CLAUDE.md`**

In the "Known limitations: knowledge base storage, chunking, and
reranking" section, add a short paragraph noting that upload-managed KBs
served by the backend now persist chunks (and embeddings) in the database
via `from_chunks()` alternate constructors on all three `KnowledgeBase`
classes, instead of re-parsing files on every load — the SDK core itself
remains file-based and DB-free; this is purely a backend consumption
pattern. Cross-reference the spec.

- [ ] **Step 5: Update `ui/backend/CLAUDE.md`**

Add a short subsection (near the existing knowledge-base-adjacent content,
or as its own subsection) describing: `ui/backend/ingestion.py`'s
`ThreadPoolExecutor`-based async ingestion, the `IngestionJob` atomicity
model (status flip = the swap, no CURRENT-pointer file for this path), the
per-document partial-failure model, and the two new read endpoints.

- [ ] **Step 6: Update `ui/backend/db/CLAUDE.md`**

Add `knowledge_ingestion_jobs`/`knowledge_documents`/`knowledge_chunks` to
the persistence-layer table list, following the same one-paragraph-per-table
style as the existing entries (`knowledge_bases`, `runs`, etc.).

- [ ] **Step 7: Update `docs/STATUS.md`**

Add a new entry under "Done" describing this feature (mirroring the style
of other recent entries — one paragraph, what shipped, the test count from
Step 1, and a link to the spec). Do not remove or rewrite any existing
entries.

- [ ] **Step 8: Commit**

```bash
git add docs/KNOWLEDGE_BASES.md src/bestteam/core/CLAUDE.md ui/backend/CLAUDE.md ui/backend/db/CLAUDE.md docs/STATUS.md
git commit -m "docs: document knowledge-base Document/Chunk/IngestionJob persistence"
```
