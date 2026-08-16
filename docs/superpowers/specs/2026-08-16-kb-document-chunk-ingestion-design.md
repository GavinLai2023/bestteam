# Knowledge base Document/Chunk/IngestionJob persistence — design

## Context

This closes finding **P2-12** from the data-architecture review
(`docs/DATA_ARCHITECTURE_REVIEW_REPORT.md`, `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`),
deliberately deferred there with the note: "revisit as its own brainstorm →
spec → plan sub-project once real multi-org document-upload volume or a
compliance/retention requirement makes it concrete." The report's full
recommended target model has six new entity types (`KnowledgeBase`/
`KnowledgeBaseVersion`, `KnowledgeDocument`/`KnowledgeDocumentVersion`,
`KnowledgeIngestionJob`, `KnowledgeIndex`) plus object storage — explicitly
out of scope for a single pass. This spec implements a narrower slice: three
tables (`Document`, `Chunk`, `IngestionJob`), file storage unchanged (still
local disk, not object storage), and no per-document *versioning* (a
replace still works at whole-KB-upload granularity, like today).

Today, `knowledge_bases.config` stores only the KB's configuration (path,
type, chunk params, embedding/rerank model specs) as a JSON blob. The actual
uploaded files live on disk under `data/knowledge_base_uploads/<org_id>/<name>/<version>/`,
with an atomically-swapped `CURRENT` pointer file (CR-008). Chunks are
recomputed **in memory on every workflow load** by walking that folder and
re-parsing every file (`core/knowledge_base.py::_load_document_chunks`).
Vector/hybrid KBs optionally cache *embeddings only* in a JSON file
(`cache_path`) — never chunk text/metadata. Uploads are fully synchronous:
`ui/backend/knowledge_bases.py::upload_knowledge_base()` parses, chunks, and
(for vector/hybrid) embeds every file inline within the HTTP request before
responding.

## Goals

1. Uploads become asynchronous: the upload endpoint returns immediately with
   a trackable job, instead of blocking the HTTP request on parse/chunk/embed
   work (the stated primary driver — large uploads or an embedding-model
   call are timeout-prone today).
2. Persisted `Document`/`Chunk` rows become the source of truth for
   retrieval on upload-managed KBs — knowledge-base query/load time reads
   from the DB instead of re-parsing files from disk on every workflow load.
3. Per-document ingestion status is visible (a job can partially succeed;
   one bad file doesn't sink the batch).

## Non-goals (explicitly out of scope for this pass)

- Object storage / moving uploaded files off local disk. Files keep being
  written to the existing versioned upload directories exactly as today.
- Per-document versioning (`KnowledgeDocumentVersion`) or a separate
  `KnowledgeBaseVersion`/`KnowledgeIndex` entity — the report's fuller
  6-entity model. A replace/re-upload still operates at whole-KB
  granularity, same as today.
- The SDK core (`src/bestteam/core/`) becoming DB-aware. It stays a plain,
  file-based library with zero SQLAlchemy/DB dependency — the CLI/YAML/
  standalone-SDK path (`bestteam run workflow.yaml`, no backend running) is
  completely unaffected.
- Manually-configured KBs (admin `PUT` with an arbitrary server `path:`).
  Only KBs created through the upload endpoints (self-service wizard +
  admin upload route) get ingestion jobs; a manual-path KB keeps using the
  existing file-based read — the backend doesn't own that folder or know
  when its contents change.
- Backfilling existing (pre-this-change) uploaded KBs into the new tables.
  A KB with zero `IngestionJob` rows keeps being read via the legacy
  file-based path until it's next re-uploaded, at which point it gets a
  real job and DB-backed chunks. The two read paths coexist only as long as
  old records exist, which shrinks naturally.
- Frontend progress/status UI beyond the minimum needed to not break: a
  generic "processing your documents…" busy state that polls to completion.
  Per-file progress bars, retry-a-single-failed-file affordances, etc. are a
  future follow-up.
- Object-ACL, retention/deletion policy, per-org storage quota (P2-12 also
  names these; still out of scope — no compliance requirement forcing them
  yet).

## Schema

Three new tables in `ui/backend/db/models.py`, scoped to upload-managed KBs.

### `knowledge_ingestion_jobs`

| column | type | notes |
|---|---|---|
| `id` | PK | |
| `kb_id` | FK → `knowledge_bases.id`, NOT NULL | |
| `org_id` | FK → `organizations.id`, nullable | denormalized, matches every other org-owned table |
| `version` | str | the same `v_<hex>` identifier used for the on-disk version directory — traceable job ↔ directory correspondence |
| `status` | str, CHECK IN (`queued`,`running`,`completed`,`failed`) | |
| `file_count` | int | files in this upload |
| `documents_succeeded` | int, default 0 | |
| `documents_failed` | int, default 0 | |
| `error` | str, nullable | capped at `_MAX_ERROR_CHARS` (2000), never a raw traceback |
| `created_by` | str, nullable | username, audit-only (mirrors `runs.username`) |
| `created_at` | datetime | |
| `completed_at` | datetime, nullable | set on the `completed`/`failed` transition |

Index: `(kb_id, status, completed_at)` — backs "find this KB's latest
`completed` job" (`ORDER BY completed_at DESC LIMIT 1`), the query every
retrieval read and every replace-cleanup does.

### `knowledge_documents`

| column | type | notes |
|---|---|---|
| `id` | PK | |
| `kb_id` | FK → `knowledge_bases.id`, NOT NULL | denormalized |
| `ingestion_job_id` | FK → `knowledge_ingestion_jobs.id`, NOT NULL | |
| `filename` | str | the uploaded file's basename (matches today's `Document.source` convention — relative to the KB folder) |
| `content_hash` | str | sha256 hex of the raw file bytes |
| `size_bytes` | int | |
| `status` | str, CHECK IN (`pending`,`parsing`,`chunked`,`failed`) | |
| `error` | str, nullable | capped at `_MAX_ERROR_CHARS`, never a raw traceback or file content |
| `created_at` | datetime | |

Index: `(ingestion_job_id)`.

### `knowledge_chunks`

| column | type | notes |
|---|---|---|
| `id` | PK | |
| `document_id` | FK → `knowledge_documents.id`, NOT NULL | |
| `kb_id` | FK → `knowledge_bases.id`, NOT NULL | denormalized, for KB-wide scans without a join through documents |
| `chunk_index` | int | ordering within the document |
| `text` | str | the chunk's text |
| `embedding_json` | JSON str, nullable | populated for `vector`/`hybrid` KBs only — same shape as `memories.embedding_json` |
| `embedding_model` | str, nullable | the spec string used, so a later query can detect a model mismatch (mirrors `vector_knowledge_base.py`'s existing cache-invalidation-on-model-change logic) |
| `created_at` | datetime | |

Indexes: `(document_id, chunk_index)`, `(kb_id)`.

A guarded/idempotent Alembic migration creates all three tables, following
the existing convention (inspection-guarded, since `db_session` runs
`create_all` at import — a fresh DB already has them via `create_all`; the
migration only matters for upgrading an existing deployment).

## Execution model

A new backend module, `ui/backend/ingestion.py`, structurally mirrors
`runtime.py::run_in_background` but owns a **separate**
`ThreadPoolExecutor` — ingestion work (file parsing, chunking, embedding
calls) is a different workload profile than agent LLM turns; sharing runs'
pool would let a large upload starve run capacity or vice versa.

**Upload flow** (`ui/backend/knowledge_bases.py::upload_knowledge_base`):

1. File validation and writing to the versioned upload directory is
   **unchanged** — same size limits, same `version_dir` staging, same
   filename sanitization.
2. Instead of building the KB in-request, create an `IngestionJob` row
   (`status="queued"`, `version=version`, `file_count=len(contents)`),
   commit, and submit the background task (`ingestion.submit_ingestion_job`)
   carrying `job_id`, `kb_id`, `version_dir`, `kb_type`, `chunk_size`,
   `chunk_overlap`, and (for vector/hybrid) `embedding_model`.
3. Return immediately: `{"name": item_name, "job_id": job.id, "status": "queued"}`.

**Background task** (own DB session opened via `db.get_bind()`, matching
`run_in_background`'s pattern):

1. Set `status="running"`.
2. Walk `version_dir` file by file (`sorted(version_dir.rglob("*"))`,
   filtered to `_SUPPORTED_SUFFIXES` — same as today). This is **not** a
   call to `_load_document_chunks()` (which silently folds a per-file parse
   error into a `warnings.warn` and skips the file) — each file needs its
   own `Document` row and its own success/failure outcome:
   ```python
   for file_path in files:
       doc = Document(kb_id=kb_id, ingestion_job_id=job.id, filename=..., content_hash=..., size_bytes=..., status="parsing")
       db.add(doc); db.flush()
       try:
           text = parse_file(str(file_path))
       except Exception as exc:
           doc.status = "failed"
           doc.error = _capped(str(exc))
           job.documents_failed += 1
           continue
       pieces = _chunk_text(text, chunk_size, chunk_overlap, suffix=file_path.suffix.lower())
       for i, piece in enumerate(pieces):
           db.add(Chunk(document_id=doc.id, kb_id=kb_id, chunk_index=i, text=piece))
       doc.status = "chunked"
       job.documents_succeeded += 1
   ```
   (`_chunk_text` and `parse_file` are already separable, file-scoped
   functions in the SDK — this reuses them without reusing
   `_load_document_chunks`'s whole-folder, warn-and-skip framing.)
3. For `kb_type in ("vector", "hybrid")`: one batched `embed_documents()`
   call over every chunk's text just written (mirrors today's single
   embedding call in `VectorKnowledgeBase.__init__`), writing
   `embedding_json`/`embedding_model` onto each `Chunk` row. A total
   embedding failure fails the whole job — a vector/hybrid KB cannot
   function with unembedded chunks, matching today's hard-fail
   `ConfigurationError` on a vector-count mismatch.
4. Resolve job status: `completed` if `documents_succeeded > 0` (the KB has
   something to serve), else `failed` (matches today's "no readable
   documents" `ConfigurationError`). Set `completed_at`.
5. **The `status="completed"` flip is the atomic swap** — no `CURRENT`
   pointer file is needed for the DB-backed path. Every read resolves a
   KB's live document set as its latest `completed` job
   (`ORDER BY completed_at DESC LIMIT 1`); a `queued`/`running`/`failed` job
   is invisible to readers, exactly preserving CR-008's "a reader never
   sees a half-ingested state" guarantee, translated from a pointer-file
   swap to a status-column flip (both are single atomic writes).
6. After a new job reaches `completed`, prune the **previous** completed
   job's `Document`/`Chunk` rows and its on-disk version directory — same
   grace-window pattern as `_cleanup_kb_versions` (the immediately-prior
   version is kept only until the new one is durable, then removed).

Concurrent uploads/replaces of the same KB name stay serialized behind the
existing per-KB lock (`_kb_upload_lock`), extended to cover job-row
creation, so two racing uploads can't both create a job for the same
version or interleave the "read prior job → delete it" cleanup.

## SDK changes (stays file-based, zero DB dependency)

`core/knowledge_base.py`, `core/vector_knowledge_base.py`,
`core/hybrid_knowledge_base.py` each gain an alternate constructor,
`from_chunks(...)`, that accepts already-built chunks (and, for
vector/hybrid, already-computed vectors) instead of a folder `path` —
skipping `_load_document_chunks()`/the embedding call entirely. `__init__`
is refactored to delegate to the same shared setup logic `from_chunks`
uses (BM25 index construction, RRF/rerank/query-expansion wiring), so there
is exactly one ranking implementation regardless of which constructor built
the instance:

```python
class LocalFolderKnowledgeBase(KnowledgeBase):
    def __init__(self, name, path, chunk_size=1000, chunk_overlap=100, top_k=5, **kwargs):
        chunks = _load_document_chunks(Path(path), chunk_size, chunk_overlap)
        self._init_from_chunks(name, chunks, top_k, **kwargs)
        self.path = Path(path)

    @classmethod
    def from_chunks(cls, name, chunks, top_k=5, **kwargs) -> "LocalFolderKnowledgeBase":
        """Build directly from pre-parsed chunks, skipping the file-parsing
        pipeline. Used by the backend's DB-backed ingestion path (see
        ui/backend/knowledge_bases.py) -- the SDK itself never touches a
        database; chunks are handed in as plain data."""
        self = cls.__new__(cls)
        self.path = None
        self._init_from_chunks(name, chunks, top_k, **kwargs)
        return self

    def _init_from_chunks(self, name, chunks, top_k, rerank_model=None, candidate_k=None,
                           query_expansion_model=None, query_expansion_count=3):
        # BM25Okapi import/raise, _validate_chunk_params, self.name/default_top_k/
        # _reranker/_candidate_k/query_expansion_* assignment, the "no readable
        # documents" ConfigurationError check, and BM25 index construction --
        # exactly today's __init__ body from `self._chunks = ...` onward.
        ...
```

`VectorKnowledgeBase.from_chunks(name, chunks, vectors, top_k=5,
score_threshold=None, **kwargs)` takes a pre-computed `List[List[float]]`
instead of an `embedding_model` + `cache_path`, skipping `_embed_chunks()`
— it only normalizes and stores the matrix. `HybridKnowledgeBase.from_chunks`
combines both (BM25 index from `chunks`, vector matrix from `vectors`).

## KB deletion

Deleting a `KnowledgeBaseRecord` (`crud.py`/`org_knowledge_bases.py`'s
existing delete routes — unchanged in every other respect: still 409s while
a deployed workflow's current version references the KB, still commits the
row delete before `rmtree`) must also delete every `IngestionJob` row for
that `kb_id` and, via `ON DELETE CASCADE` (or an explicit cascading delete
in the same transaction, matching this codebase's existing no-DB-level-FK-
enforcement posture — SQLite FK enforcement is off, P1-13), every
`Document`/`Chunk` row under those jobs. Without this, a deleted KB's
ingestion rows orphan permanently — no owner, no cleanup path, quietly
consuming space forever. This is in scope for this pass (it's a direct
consequence of adding the tables, not a new feature) even though a general
per-org storage quota is not (Non-goals).

## Backend read path

`ui/backend/knowledge_bases.py::load_knowledge_base_tools` (and the
autonomous-trigger loader that shares it) resolves each referenced KB:

1. Query the KB's latest `completed` `IngestionJob`.
2. **If one exists**: load its `Document`/`Chunk` rows, build `List[_Chunk(source=filename, text=text)]`
   ordered by `(document.filename, chunk.chunk_index)`, and — for
   `vector`/`hybrid` — a parallel `List[List[float]]` from
   `Chunk.embedding_json`. Call the matching class's `from_chunks(...)`.
3. **If none exists** (a KB predating this change, not yet re-uploaded):
   fall back to today's `_build_knowledge_base()` file-based construction,
   completely unchanged.

This is the entire backward-compatibility mechanism — no migration flag, no
schema version marker on `knowledge_bases` itself. Absence of any job row
is the signal.

## API changes

- `POST /api/org/knowledge-bases/{name}/upload` and the admin
  `/api/config/knowledge_bases/{name}/upload` now respond
  `{"name": ..., "job_id": ..., "status": "queued"}` instead of
  `{"name", "config", "file_count", "chunk_count"}`.
- New: `GET /api/org/knowledge-bases/{name}/ingestion-jobs/{job_id}` (+
  admin equivalent under `/api/config/...`) — returns job status,
  `documents_succeeded`/`documents_failed`, and up to the first 10 document
  errors (filename + capped error). Org-scoped like every other org route:
  another org's or an unknown job id is a 404, no existence oracle.
- `?replace=true` semantics are unchanged in spirit (still requires
  confirmation via 409 without it on an existing name) — it now creates a
  new job rather than synchronously swapping a pointer.

## Frontend compatibility (minimal — backend/API scope, not a UX redesign)

`DocumentsPage.tsx`'s `proceed()` currently awaits the upload call and
immediately generates the spec. With async ingestion this needs to poll the
new job-status endpoint to `completed`/`failed` **before** generating the
spec — not decorative: if spec generation ran while ingestion is still in
flight, the architect would reference a KB with no queryable content yet
(and the legacy-fallback path wouldn't apply either, since a `queued` job
still means zero `completed` jobs exist... except when this is a *replace*
of an already-ingested KB, in which case the fallback would silently serve
the *old* content while the new upload finishes — which is actually the
correct behavior, matching CR-008's "readers never see a half-ingested
state," not a bug to route around). The busy-state pattern already exists
in that file (`stage`/`STAGE_LABELS`); this adds an `"ingesting"` stage that
polls until the job resolves, then proceeds or shows the job's error. Same
minimal polling wait applies to the admin Advanced-page KB upload UI. No
per-file progress bar, no retry-single-file affordance — those are a future
follow-up (Non-goals).

## Error handling

- A document's parse failure never aborts the batch — per-document
  `status="failed"` + capped error; the job still resolves `completed` if
  anything else in the batch succeeded.
- A total embedding-model failure (vector/hybrid) fails the whole job — no
  partial vector KB with only some chunks embedded.
- `IngestionJob.error`/`Document.error` are always capped at
  `_MAX_ERROR_CHARS` (2000, matching the memory subsystem's
  `_MAX_RECORD_CHARS` precedent) and store only `str(exc)` — never a raw
  traceback or raw file content.
- Concurrent uploads of the same KB name stay serialized behind the
  existing per-KB `RLock`.

## Testing

- Ingestion job: full success; partial per-document failure (some files
  parse, one doesn't); total failure (no readable documents in the batch);
  embedding failure for vector/hybrid fails the whole job.
- Job-status API: auth, org scoping, 404 for another org's/unknown job.
- Retrieval: a `completed` job's chunks are queryable via
  `from_chunks()`-built KBs, for all three types (`local_folder`, `vector`,
  `hybrid`); a `queued`/`running`/`failed` job is invisible to queries (the
  prior completed job, or the legacy file path, stays live).
- Legacy fallback: a KB with zero `IngestionJob` rows still resolves via
  the file-based path, byte-for-byte unchanged.
- Replace/re-upload: a new job is created; the previous version's chunks
  stay queryable until the new job completes, then get pruned (grace-window
  version kept, matching `_cleanup_kb_versions`).
- Migration: Alembic creates the three tables, guarded/idempotent like
  existing migrations (`tests/test_migrations.py`).
- Deletion: deleting a KB removes its `IngestionJob`/`Document`/`Chunk`
  rows; no orphaned rows survive a delete.
- Frontend: `DocumentsPage` polls job status to completion before
  generating the spec; shows the busy `"ingesting"` stage; a `failed` job
  surfaces as the existing error banner with retry.

## Known limitations (post-implementation)

- No object storage — files remain on local disk, same single-process/
  single-SQLite-file posture as the rest of this project today (P2-13,
  separately deferred).
- No per-document versioning, no document ACL, no retention policy, no
  per-org storage quota beyond the existing self-service KB **count** cap.
  Each is its own future brainstorm → spec → plan sub-project, per P2-12's
  own disposition.
- A manually-configured (non-upload) KB never gets ingestion jobs; it keeps
  re-parsing its folder on every load, unchanged.
- Existing (pre-this-change) uploaded KBs stay on the legacy file-based
  read path until their owner next re-uploads them — no backfill job.
