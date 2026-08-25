"""Build extra_tools for pipeline loading from standalone KnowledgeBaseRecords
(created via /api/config/knowledge_bases, manually or via file upload)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from bestteam import KnowledgeBaseSpec
from bestteam.core.hybrid_knowledge_base import HybridKnowledgeBase
from bestteam.core.knowledge_base import (
    LocalFolderKnowledgeBase,
    _Chunk,
    _validate_chunk_params,
    make_knowledge_base_tool,
)
from bestteam.core.loader import _build_knowledge_base, _KNOWLEDGE_BASE_TYPES
from bestteam.core.specification import _validate_tool_name
from bestteam.core.vector_knowledge_base import VectorKnowledgeBase
from bestteam.exceptions import BestTeamError, ConfigurationError
from bestteam.tools import REGISTRY

from . import ingestion
from .component_lock import component_mutation_lock
from .db.dependencies import pipelines_referencing
from .db.models import IngestionJob, KnowledgeBaseRecord, KnowledgeChunk, KnowledgeDocument
from .deploy_validation import find_kb_tool_collisions

_logger = logging.getLogger(__name__)


class KnowledgeBaseNotReady(ConfigurationError):
    """A knowledge base exists, but nothing behind it can answer a query yet.

    Its own subclass because it is the one `ConfigurationError` this module
    raises that the *customer* can resolve: an upload is still processing,
    the last one failed, or the collection was never uploaded through the app
    at all. `org_knowledge_bases.py`'s search endpoint turns exactly this into
    a `409` carrying the message; every other `ConfigurationError` (a missing
    optional extra, a bad `rerank_model`) is an operator's deployment problem
    and stays a logged `500`. `builder.py`'s `except ConfigurationError` still
    catches it, being a subclass.
    """


# --- KB path containment (CR-001) -------------------------------------------
# A KB `cache_path` is a server-file *write* target (the vector KB's
# `_save_embedding_cache` does `os.replace(tmp, cache_path)`). We keep the SDK
# loader permissive (CLI/YAML deployments legitimately point cache_path wherever
# they manage), and instead constrain every *backend* boundary + load path so a
# caller can never influence the write location beyond a filename: the cache is
# forced into an application-owned `_kb_cache/` subdirectory that holds no source
# files, so it can't clobber a pipeline YAML or escape the app roots.

_KB_CACHE_DIRNAME = "_kb_cache"

# Legacy only. A KB uploaded before the async-ingestion feature was stored as
# versioned subdirectories with an atomically-swapped `CURRENT` pointer file
# naming the active version, so a replacement never left the KB directory
# without a live version for a concurrent reader (CR-008). Nothing writes this
# file any more -- an upload-managed KB's live version is now its most recent
# `completed` IngestionJob (see ui/backend/ingestion.py) -- but
# `resolve_kb_upload_path` still *reads* it, so those older, never-re-uploaded
# KBs keep resolving their active version on the legacy file-based read path.
_KB_CURRENT_POINTER = "CURRENT"

# Files uploaded via a knowledge-base upload endpoint (admin `/api/config/...`
# or the org self-service `/api/org/...`) live here, one subdirectory per KB --
# a directory this backend owns (unlike the manual JSON config path, which
# points at a folder the user manages themselves).
_KB_UPLOADS_DIR = Path(__file__).parent / "data" / "knowledge_base_uploads"
_MAX_FILES_PER_UPLOAD = 30

# How many documents one collection may hold once an "add" upload is merged
# with the generation it extends. Without a ceiling, "add" is unbounded growth
# per collection -- and the per-org collection cap that bounds an org's
# footprint today only counts collections, not what is in them. The admin
# default is generous; `org_knowledge_bases.py` passes a tighter one, as it
# does for every other limit here.
_MAX_DOCUMENTS_PER_KB = 200
_MAX_FILE_SIZE_BYTES = 30 * 1024 * 1024  # 30MB
_MAX_TOTAL_SIZE_BYTES = 500 * 1024 * 1024  # ~500MB

# Per-KB locks serialising the upload staging/commit/dispatch critical section
# (and delete, in crud.py). Originally so concurrent uploads of the same KB
# couldn't interleave the shared CURRENT pointer + version cleanup and leave
# CURRENT naming a version the losing uploader then deletes (CR-008); the
# DB-backed path writes no pointer, but the lock is still load-bearing -- a
# concurrent delete rmtree's the whole KB root, and org_knowledge_bases.py's
# existence/cap/replace-confirmation check has to hold it across this call to
# stay atomic (F1). Keyed by
# "<org_id>/<name>"; a small guard lock protects the registry itself.
# Reentrant (RLock): org_knowledge_bases.py's self-service route also holds
# this same per-KB lock across its own existence/cap/replace-confirmation
# check, then calls into upload_knowledge_base() below, which re-acquires it
# by the same key on the same thread -- a plain Lock would deadlock there.
_kb_upload_locks_guard = threading.Lock()
_kb_upload_locks: Dict[str, threading.RLock] = {}


def _kb_upload_lock(name: str) -> threading.RLock:
    with _kb_upload_locks_guard:
        lock = _kb_upload_locks.get(name)
        if lock is None:
            lock = threading.RLock()
            _kb_upload_locks[name] = lock
        return lock


def _reject_builtin_kb_name(name: str) -> None:
    """Refuse a knowledge-base name that shadows a built-in tool (F1).

    All tools resolve through one flat name lookup, so a KB named after a
    built-in silently replaces it at load. Blocking the name at creation is the
    source fix -- a colliding KB can then never exist.
    """
    if name in REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A knowledge base can't reuse a built-in tool name: '{name}'. "
                "Choose a different name."
            ),
        )


def _invalidate_pipeline_cache() -> None:
    """Drop every cached Pipeline after a KB/skill mutation.

    A cached Pipeline may embed a knowledge-base tool or skill by value. The
    global `max(updated_at)` freshness key in `main._get_pipeline` misses a
    *delete* (removing a non-latest record leaves the maximum unchanged), so a
    cached pipeline could keep serving a deleted KB's documents (CR-005).
    Clearing the cache on every KB/skill create/update/delete is the simple,
    correct invalidation. Bumping the generation under the cache lock makes a
    concurrent `_get_pipeline` that started before this call skip caching its
    now-stale result instead of repopulating the cache (CR-005). Imported
    lazily to avoid a knowledge_bases<->main import cycle.
    """
    from . import main

    with main._pipeline_cache_lock:
        main._pipeline_cache.clear()
        main._pipeline_cache_generation += 1


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
    """Copy the live generation's files into `version_dir` beside the new ones.

    This is what makes "add" a mode of the existing pipeline rather than a
    second one: the new job still stages, parses and owns a complete document
    set, so nothing downstream has to learn that a collection can span two
    jobs. `ingestion._reusable_documents` then makes the copies nearly free --
    a file whose bytes are unchanged keeps the chunks and embeddings the
    previous job already paid for.

    Nothing to carry (a name that has never completed a job, or a version
    directory an operator has removed) is not an error: "add" to a collection
    that isn't there yet is just a first upload.

    `superseded` is matched case-insensitively because the filesystem the
    copies land on may be: on Windows and macOS, carrying `Policy.txt` beside
    a freshly uploaded `policy.txt` is one path, and the copy would land on
    top of the upload -- leaving the collection holding the *old* text under
    the new name, with nothing anywhere reporting it. Two documents whose
    names differ only in case cannot coexist there anyway, so the upload wins,
    which is what it does for an exact name match. `exact=True` matches the
    name as written instead -- a *removal* names one document the way the
    collection lists it, and on a case-sensitive filesystem (where
    `Policy.txt` and `policy.txt` can both be live) dropping the variant too
    would remove a document the customer never named (Codex review).

    `source` names the generation to stage from; the default is the live
    (newest completed) job. Restoring the previous upload passes the one
    before it: its files are still on disk (it is the grace-window
    generation), and staging them with nothing superseded is exactly a
    re-upload of that set.
    """
    record = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if record is None:
        return
    previous = source
    if previous is None:
        previous = (
            db.query(IngestionJob)
            .filter_by(kb_id=record.id, status="completed")
            # `id`, not `completed_at` -- see `resolve_knowledge_base`.
            .order_by(IngestionJob.id.desc())
            .first()
        )
    if previous is None:
        return
    previous_dir = kb_root / previous.version
    if not previous_dir.is_dir():
        return

    fold = (lambda name: name) if exact else str.lower
    superseded_keys = {fold(name) for name in superseded}
    carried = [
        path
        for path in sorted(previous_dir.rglob("*"))
        if path.is_file()
        and fold(path.relative_to(previous_dir).as_posix()) not in superseded_keys
    ]
    total = len(superseded) + len(carried)
    if total > max_documents:
        raise HTTPException(
            status_code=413,
            detail=(
                f"'{item_name}' would hold {total} documents; the limit is "
                f"{max_documents}. Remove some documents first, or start "
                "another collection."
            ),
        )
    for path in carried:
        target = version_dir / path.relative_to(previous_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def upload_knowledge_base(
    db: Session,
    org_id: Optional[int],
    item_name: str,
    files: List[UploadFile],
    *,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    top_k: int = 5,
    kb_type: Optional[str] = None,
    description: Optional[str] = None,
    embedding_model: Optional[str] = None,
    rerank_model: Optional[str] = None,
    query_expansion_model: Optional[str] = None,
    max_files: int = _MAX_FILES_PER_UPLOAD,
    max_file_size_bytes: int = _MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = _MAX_TOTAL_SIZE_BYTES,
    max_documents: int = _MAX_DOCUMENTS_PER_KB,
    mode: str = "replace",
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate uploaded documents and dispatch an async ingestion job.

    Shared by the admin `/api/config/knowledge_bases/{name}/upload` route
    (always `local_folder`, the historical default) and the org self-service
    `/api/org/knowledge-bases/{name}/upload` route -- the differences between
    them are how `org_id` gets resolved (an explicit `?org=` for the admin
    surface, the bearer token's own org for self-service), the tighter
    `max_*` limits `org_knowledge_bases.py` passes in, and, for the "smarter
    search" toggle described there, `kb_type`/`embedding_model`/
    `rerank_model`/`query_expansion_model`. `embedding_model` is required
    when `kb_type` is `vector` or `hybrid` and ignored otherwise.

    `kb_type is None` means "whatever shape this collection already has":
    the shape group (`type`/`embedding_model`/`rerank_model`/
    `query_expansion_model`) is inherited wholesale from an existing
    record's config, and defaults to `local_folder` for a name that doesn't
    exist yet. That is what the admin route sends -- it has no way to name a
    shape -- so without the inheritance an operator re-uploading documents
    for a `hybrid` collection would silently rebuild it as `local_folder`.
    A caller that *does* pass `kb_type` names the whole group itself; the
    org self-service route always does. `chunk_size`/`chunk_overlap`/`top_k`
    are per-upload knobs both routes always send, so they are never
    inherited.

    `description` is the customer's one sentence about what the documents
    cover. It is stored on the KB's config and becomes the agent tool's own
    description, so it is what tells a model which of an org's collections
    answers a question; both routes cap it at 500 characters. It is
    inherited independently of the shape, since both routes send `None` for
    "not given" and re-uploading documents shouldn't blank a description the
    customer can see.

    `mode` is `"replace"` (every upload's behaviour before this existed) or
    `"add"`. A collection's live document set is one completed job's rows, so
    "add" is implemented by *staging the previous generation's files
    alongside the new ones* rather than by teaching retrieval to span two
    jobs: the new job still owns a complete set, and every invariant built on
    that -- the status flip being the atomic swap, pruning, retention --
    holds unchanged. `ingestion.py` then carries an unchanged file's chunks
    and embeddings forward by content hash, so restaging is cheap rather than
    a re-embed of the whole collection. A file whose name matches one in this
    upload -- case-insensitively, as the filesystem itself may match -- is not
    carried: the upload supersedes it, since two documents of the same name in
    one collection would be two answers to the same question. `max_documents`
    bounds the merged set -- without it "add" is unbounded growth, per
    collection and so per org.

    This validates synchronously (name/size limits, `kb_type`, chunk params),
    writes the uploaded files to a fresh version directory, upserts the
    `KnowledgeBaseRecord` with its final config, and creates a `queued`
    `IngestionJob` row -- then submits `ingestion.run_ingestion_job` to
    `ingestion._executor` and returns immediately. The actual parsing/
    chunking/embedding happens on a background thread; callers poll
    `GET .../ingestion-jobs/{job_id}` (Task 9) for completion. The returned
    dict is `{"name", "job_id", "status"}` -- no `config`/`chunk_count`
    (those are only known once the job finishes).
    """
    try:
        _validate_tool_name(item_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _reject_builtin_kb_name(item_name)

    if mode not in ("replace", "add"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown upload mode '{mode}'. Use 'replace' or 'add'.",
        )
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > max_files:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files ({len(files)}); max {max_files} per upload",
        )

    # Read all file contents up front to enforce size limits before writing anything to disk.
    contents: Dict[str, bytes] = {}
    total_size = 0
    for f in files:
        # f.filename comes from the client-controlled Content-Disposition
        # header -- strip it to a bare filename so it can't escape upload_dir
        # via "../" segments or an absolute path.
        filename = Path(f.filename or "").name
        if filename in ("", ".", ".."):
            raise HTTPException(status_code=400, detail=f"Invalid filename: '{f.filename}'")

        data = f.file.read()
        if len(data) > max_file_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File '{filename}' exceeds the {max_file_size_bytes // (1024 * 1024)}MB per-file limit",
            )
        total_size += len(data)
        if total_size > max_total_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Total upload size exceeds the {max_total_size_bytes // (1024 * 1024)}MB limit",
            )
        contents[filename] = data

    # Read-only, and deliberately outside the per-KB lock below: it only
    # supplies defaults for what this call left unsaid. Note what the lock
    # does NOT buy here -- its own re-query decides insert-vs-update, but the
    # inherited shape is the copy read on these lines, so a wizard upgrade
    # committing between here and the lock is overwritten by this call's
    # stale reading of it. Accepted: the losing side re-uploads. Runs before
    # the type validation, so an inherited type is validated like any other.
    existing_config: Dict[str, Any] = {}
    existing_record = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if existing_record is not None:
        existing_config = existing_record.config or {}
    if kb_type is None:
        kb_type = existing_config.get("type", "local_folder")
        embedding_model = existing_config.get("embedding_model")
        rerank_model = existing_config.get("rerank_model")
        query_expansion_model = existing_config.get("query_expansion_model")
    if description is None:
        description = existing_config.get("description")

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

    # Write the new files into a fresh version subdirectory, then dispatch an
    # async ingestion job to parse/chunk/(embed) them. No CURRENT pointer is
    # written for this path -- nothing reads it; retrieval resolves the live
    # version via the ingestion job's own `status` (see ui/backend/ingestion.py).
    # Uploads are org-scoped on disk so two orgs' same-named KBs can't share
    # (or clobber) a directory. Legacy pre-multi-tenancy uploads at
    # `_KB_UPLOADS_DIR/<name>` keep working: KB configs embed the absolute
    # root, so existing records still resolve their old directory.
    kb_root = _KB_UPLOADS_DIR / str(org_id) / item_name
    version = f"v_{uuid.uuid4().hex[:12]}"
    version_dir = kb_root / version
    # Hold the per-KB lock across staging + commit + dispatch. A concurrent
    # delete takes the same lock and rmtree's the whole KB root; if staging ran
    # outside the lock, the delete could remove a version staged here (F1).
    # Same-KB uploads also serialize now (each still owns a unique version
    # dir; the extra contention is fine).
    with _kb_upload_lock(f"{org_id}/{item_name}"):
        version_dir.mkdir(parents=True, exist_ok=True)
        try:
            for filename, data in contents.items():
                (version_dir / filename).write_bytes(data)
            if mode == "add":
                _stage_previous_generation(
                    db, org_id, item_name, kb_root, version_dir,
                    superseded=set(contents), max_documents=max_documents,
                )

            spec = KnowledgeBaseSpec(
                name=item_name,
                path=str(kb_root),
                description=description,
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
                # The shape this job's chunks will actually be ingested
                # under. `item.config` above has already advanced to the new
                # spec, but the KB's live content stays the *previous*
                # completed job's chunks until this one finishes -- so the
                # read path resolves the shape from the serving job, not from
                # the record (see `_build_knowledge_base_from_job`).
                kb_type=kb_type,
                embedding_model=spec.embedding_model,
                # The chunk parameters too, not only at the top of
                # `run_ingestion_job`: a job interrupted while still `queued`
                # never reaches the worker's own write, and a retry after the
                # restart has only this row to re-dispatch from.
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                status="queued",
                # The whole staged set, not this request's files: in "add"
                # mode the job really is going to process the carried
                # generation too, and `documents_succeeded + documents_failed`
                # is checked against this.
                file_count=sum(1 for p in version_dir.rglob("*") if p.is_file()),
                created_by=created_by,
            )
            db.add(job)
            db.commit()
        except Exception:
            # No CURRENT pointer is written for the DB-backed path (nothing
            # reads it -- retrieval resolves the live version via the
            # ingestion job's own status), so on a pre-dispatch failure the
            # only cleanup needed is the just-written version directory.
            db.rollback()
            shutil.rmtree(version_dir, ignore_errors=True)
            raise

        _dispatch_ingestion_job(
            db, job, item.id, org_id, version_dir,
            kb_type=kb_type, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            embedding_model=spec.embedding_model,
        )
        return {"name": item_name, "job_id": job.id, "status": "queued"}


def _dispatch_ingestion_job(
    db: Session,
    job: IngestionJob,
    kb_id: int,
    org_id: Optional[int],
    version_dir: Path,
    *,
    kb_type: str,
    chunk_size: int,
    chunk_overlap: int,
    embedding_model: Optional[str],
) -> None:
    """Submit a committed `queued` job to the ingestion worker.

    Called strictly AFTER the commit and outside the staging handler: the
    job and record rows are durable by now, so rmtree-ing the staged files on
    a submit failure would strand a permanently `queued` job pointing at a
    deleted directory. Resolve the job to `failed` instead, so the customer's
    poll terminates with a real error rather than spinning (F6).
    """
    try:
        ingestion._executor.submit(
            ingestion.run_ingestion_job,
            job.id,
            kb_id,
            org_id,
            version_dir,
            kb_type=kb_type,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            embedding_model=embedding_model,
            engine=db.get_bind(),
        )
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        # The staged files survive this failure, so the job is retryable --
        # the copy must not send the customer back to re-uploading (and for a
        # retry whose diagnostic rows were already replaced, this is the only
        # error left on the row).
        job.error = "Could not start processing these documents. Use Retry, or try uploading again."
        job.completed_at = ingestion._now()
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Could not start processing these documents. Use Retry, or try uploading again.",
        ) from exc


def live_documents(db: Session, record: KnowledgeBaseRecord) -> List[KnowledgeDocument]:
    """The documents of the collection's live generation -- the newest
    completed job's rows, every status, sorted by name -- or `[]` when no
    job has completed. A `failed` document is listed too: its file is still
    in the generation (every `add` carries it and reports it skipped again),
    so it is something the customer can remove."""
    live = (
        db.query(IngestionJob)
        .filter_by(kb_id=record.id, status="completed")
        .order_by(IngestionJob.id.desc())
        .first()
    )
    if live is None:
        return []
    return (
        db.query(KnowledgeDocument)
        .filter_by(ingestion_job_id=live.id)
        .order_by(KnowledgeDocument.filename)
        .all()
    )


def remove_knowledge_base_document(
    db: Session,
    org_id: Optional[int],
    item_name: str,
    filename: str,
    *,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Drop one document from a collection and dispatch the job that makes
    that the live set. Returns `{"name", "job_id", "status": "queued"}`,
    exactly like an upload -- callers poll the job the same way.

    A removal is the upload pipeline with no new files: the live generation
    is staged into a fresh version directory minus the named file
    (`_stage_previous_generation` with that name as the one superseded
    entry), and a new job ingests the staged set under the live job's own
    shape and chunk parameters, so `ingestion._reusable_documents` carries
    every remaining document's chunks and embeddings forward and nothing is
    re-parsed or re-embedded. One completed job is still the live set, the
    status flip is still the atomic swap, and retention/pruning see an
    ordinary generation. `record.config` is not touched: removing a document
    changes what the collection holds, not how it searches.

    Refused (409) while a `queued`/`running` job exists for the collection:
    whichever of two jobs finished last would otherwise become live, and a
    removal built from the previous generation could then silently undo an
    upload that was still being processed. Refused (409) when the named file
    is the last one **that could be read** -- an empty collection cannot be
    built (`_init_from_chunks` raises on no chunks), so the job would only
    fail and the document would stay live; a `failed` document left behind
    does not count, it has no chunks (Codex review). Deleting the collection
    is the operation that means "nothing left". Unknown names
    are a 404 naming the file. The name must match the document's exactly:
    that is how the collection lists it, and on a case-insensitive filesystem
    the carry would drop a case-variant too, which would make "removed
    `policy.txt`" true of a file the customer never named.
    """
    record = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge base '{item_name}'")

    kb_root = _KB_UPLOADS_DIR / str(org_id) / item_name
    version = f"v_{uuid.uuid4().hex[:12]}"
    version_dir = kb_root / version
    # The same per-KB lock uploads and deletes take, held across the in-flight
    # check, staging, commit and dispatch, so no upload can slip a job in
    # between "nothing is processing" and this job's own row.
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
                    "to finish, then remove the document."
                ),
            )
        live = (
            db.query(IngestionJob)
            .filter_by(kb_id=record.id, status="completed")
            .order_by(IngestionJob.id.desc())
            .first()
        )
        if live is None:
            raise HTTPException(
                status_code=409,
                detail=f"'{item_name}' has no finished upload to remove a document from.",
            )
        documents = (
            db.query(KnowledgeDocument).filter_by(ingestion_job_id=live.id).all()
        )
        names = {doc.filename for doc in documents}
        if filename not in names:
            raise HTTPException(
                status_code=404,
                detail=f"'{item_name}' has no document named '{filename}'.",
            )
        readable_after = [
            doc for doc in documents if doc.status == "chunked" and doc.filename != filename
        ]
        if not readable_after:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{filename}' is the only document in '{item_name}' that "
                    "could be read. A collection can't be empty -- delete the "
                    "collection instead."
                ),
            )
        if not _kb_version_dir(org_id, item_name, live.version).is_dir():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"The files for '{item_name}' are no longer on the server. "
                    "Upload the documents you want to keep, replacing the collection."
                ),
            )

        # The live job's own shape and chunk parameters, so every remaining
        # document is reusable (`ingestion._carryable`).
        kb_type, chunk_size, chunk_overlap, embedding_model = _job_shape(live, record.config or {})

        version_dir.mkdir(parents=True, exist_ok=True)
        try:
            _stage_previous_generation(
                db, org_id, item_name, kb_root, version_dir,
                superseded={filename}, max_documents=_MAX_DOCUMENTS_PER_KB, exact=True,
            )
            job = IngestionJob(
                kb_id=record.id,
                org_id=org_id,
                version=version,
                kb_type=kb_type,
                embedding_model=embedding_model,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
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


def _kb_version_dir(org_id: Optional[int], item_name: str, version: str) -> Path:
    """The on-disk directory holding one ingestion job's staged files. One
    definition on purpose: `job_is_retryable` (which drives the panel's Retry
    button) and `retry_ingestion_job` (the endpoint's own gate) must compute
    the identical path, or the UI enables a retry the endpoint refuses."""
    return _KB_UPLOADS_DIR / str(org_id) / item_name / version


def _job_shape(job: IngestionJob, config: Dict[str, Any]) -> Tuple[str, int, int, Optional[str]]:
    """`(kb_type, chunk_size, chunk_overlap, embedding_model)` -- the shape a
    job's chunks were actually cut with, falling back to the record's config
    for a row written before those columns were filled at creation (which
    re-chunks once under today's config, the same one-time cost the first
    `add` after an upgrade pays). Shared by removal, restore and retry so
    the three fallbacks cannot drift."""
    kb_type = job.kb_type or config.get("type", "local_folder")
    chunk_size = job.chunk_size if job.chunk_size is not None else config.get("chunk_size", 1000)
    chunk_overlap = (
        job.chunk_overlap if job.chunk_overlap is not None else config.get("chunk_overlap", 100)
    )
    embedding_model = job.embedding_model if kb_type in ("vector", "hybrid") else None
    return kb_type, chunk_size, chunk_overlap, embedding_model


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
    if not _kb_version_dir(org_id, record.name, previous.version).is_dir():
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
        if not _kb_version_dir(org_id, item_name, previous.version).is_dir():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"The files for '{item_name}' are no longer on the server. "
                    "Upload the documents you want, replacing the collection."
                ),
            )

        # The previous job's own shape and chunk parameters -- what makes its
        # every document reusable.
        kb_type, chunk_size, chunk_overlap, embedding_model = _job_shape(previous, record.config or {})

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
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
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


def job_is_retryable(org_id: Optional[int], record: KnowledgeBaseRecord, job: IngestionJob) -> bool:
    """Whether Retry can act on this job: it failed and its staged files are
    still on disk. Callers pass the collection's newest job -- an older one
    is never retryable regardless, because `retry_ingestion_job` refuses any
    job a newer one has superseded."""
    if job.status != "failed":
        return False
    return _kb_version_dir(org_id, record.name, job.version).is_dir()


def retry_ingestion_job(
    db: Session,
    org_id: Optional[int],
    item_name: str,
    job_id: int,
    *,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-run a failed ingestion job in place. Returns `{"name", "job_id",
    "status": "queued"}` with the SAME job id: the row is reset and
    re-dispatched, not copied.

    The same row on purpose: a second job sharing the failed one's version
    directory would break `_prune_failed_ingestion_versions`, which reclaims
    every failed job's directory but the newest failed job's -- a shared
    directory would be deleted out from under the retry the next time that
    pruning ran. Resetting the row keeps "one job, one version directory"
    true everywhere.

    Only the collection's newest job (by `id`, the ordering everything else
    uses) can be retried: an older failure was superseded by whatever was
    uploaded after it. That check rules out a NEWER queued/running job, but
    not an older one still processing -- the admin upload path has no
    in-flight guard, so a newer job can fail fast while an older worker is
    still ingesting -- hence the explicit queued/running 409 below, without
    which a retry would put a second worker on the same collection. The
    failed attempt's KnowledgeDocument/KnowledgeChunk rows -- its per-file
    diagnostic record -- are deleted first, because `run_ingestion_job`
    inserts a fresh row per staged file and increments the counters from the
    row's current values. (On a dispatch-submission failure those rows are
    already gone; the shared dispatch-failure copy points at Retry, so
    nothing tells the customer to re-upload files that are still staged.)

    Nothing is double-billed: ingestion usage is recorded only when a job
    completes, so the failed attempt was never metered. Unchanged documents
    still reuse the previous *completed* generation's chunks and embeddings
    (`_reusable_documents`), exactly as the original attempt would have.

    Refused (409) when the job is not `failed`, when a newer job exists, and
    when the version directory is no longer on disk (pruned, or removed by
    an operator). An unknown collection or job -- another org's included --
    is a 404.
    """
    record = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge base '{item_name}'")

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
                    "to finish, then retry."
                ),
            )
        job = db.get(IngestionJob, job_id)
        if job is None or job.kb_id != record.id:
            raise HTTPException(
                status_code=404, detail=f"'{item_name}' has no upload with id {job_id}."
            )
        if job.status != "failed":
            raise HTTPException(status_code=409, detail="Only a failed upload can be retried.")
        newer = (
            db.query(IngestionJob.id)
            .filter(IngestionJob.kb_id == record.id, IngestionJob.id > job.id)
            .first()
        )
        if newer is not None:
            raise HTTPException(
                status_code=409,
                detail=f"A newer upload exists for '{item_name}'. Retry or replace that one instead.",
            )
        version_dir = _kb_version_dir(org_id, item_name, job.version)
        if not version_dir.is_dir():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"The files for '{item_name}' are no longer on the server. "
                    "Upload the documents again."
                ),
            )

        # The job's own shape and chunk parameters -- written at creation
        # since this feature exists, so an interrupted `queued` job carries
        # them too, with `_job_shape`'s config fallback for older rows.
        kb_type, chunk_size, chunk_overlap, embedding_model = _job_shape(job, record.config or {})

        doc_ids = [
            doc_id
            for (doc_id,) in db.query(KnowledgeDocument.id).filter_by(ingestion_job_id=job.id)
        ]
        if doc_ids:
            db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id.in_(doc_ids)).delete(
                synchronize_session=False
            )
            db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id).delete(
                synchronize_session=False
            )
        job.status = "queued"
        job.error = None
        job.documents_succeeded = 0
        job.documents_failed = 0
        job.completed_at = None
        # The retried attempt belongs to whoever asked for it; an anonymous
        # direct call keeps the original attribution rather than blanking it.
        if created_by is not None:
            job.created_by = created_by
        db.commit()

        _dispatch_ingestion_job(
            db, job, record.id, org_id, version_dir,
            kb_type=kb_type, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            embedding_model=embedding_model,
        )
        return {"name": item_name, "job_id": job.id, "status": "queued"}


def delete_knowledge_base(db: Session, org_id: Optional[int], item_name: str) -> None:
    """Delete a knowledge base: its record, its ingestion rows, its uploads.

    Lives here rather than in `crud.py`'s generic component-delete route
    because it needs the per-KB `_kb_upload_lock` this module owns, and
    because it takes `component_mutation_lock` itself -- that lock is NOT
    reentrant, so the route has to call this *before* entering its own
    `with component_mutation_lock` block, not inside it.

    Refuses (409) while a `queued`/`running` `IngestionJob` exists for this
    KB. The ingestion worker runs on its own thread and commits
    Document/Chunk rows against `kb_id` when it finishes, so deleting the
    record out from under it left orphan rows behind (FK enforcement is off,
    so nothing caught them) and, on Windows, leaked the upload directory --
    `rmtree` fails with `WinError 32` against the handle the worker still
    holds open. Refusing is what makes "a KB being deleted has no worker"
    true: uploads and deletes both serialize on `_kb_upload_lock`, and only
    an upload creates a job. `ingestion.fail_interrupted_jobs` (called at
    startup) is what stops a killed process making this refusal permanent.
    """
    with component_mutation_lock:
        item = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown knowledge_base '{item_name}'")
        used_by = pipelines_referencing(db, kind="knowledge_base", resource_id=item.id)
        if used_by:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Can't delete '{item_name}': it's used by deployed team(s): "
                    + ", ".join(used_by)
                    + ". Update or remove those teams first."
                ),
            )
        # Hold the per-KB lock across the in-flight check, delete, commit and
        # rmtree so a concurrent upload can't dispatch a job (or recreate the
        # row/files) in a gap and then have them removed here (F1). Commit
        # before rmtree so a commit failure keeps the files for the
        # still-present record; a failed rmtree is logged (the record is
        # already gone), not silently swallowed (F3-prev).
        with _kb_upload_lock(f"{org_id}/{item_name}"):
            in_flight = (
                db.query(IngestionJob)
                .filter(IngestionJob.kb_id == item.id, IngestionJob.status.in_(("queued", "running")))
                .first()
            )
            if in_flight is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{item_name}' is still processing an upload. "
                        "Wait for it to finish, then delete it."
                    ),
                )
            ingestion.delete_kb_ingestion_data(db, item.id)
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
    _invalidate_pipeline_cache()


def resolve_kb_upload_path(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve an upload-managed KB path to its active version directory.

    If the KB's `path` contains a `CURRENT` pointer file, return a copy with
    `path` pointing at the named version subdir. Manual-config KBs and legacy
    flat upload dirs (no pointer) are returned unchanged, so they scan their
    path directly. The pointer always names a complete version, so a reader
    never observes the brief no-live-dir window a rename-based swap would create
    (CR-008)."""
    path = config.get("path")
    if not isinstance(path, str):
        return config
    try:
        version = (Path(path) / _KB_CURRENT_POINTER).read_text(encoding="utf-8").strip()
    except OSError:
        return config  # no pointer -> flat/manual layout, scan path as-is
    version_dir = Path(path) / version
    if not version_dir.is_dir():
        return config
    return {**config, "path": str(version_dir)}


def has_traversal(value: str) -> bool:
    # Check under both path flavors so the guard behaves the same on the Linux
    # server and a Windows dev box (e.g. "foo/../bar" vs "foo\\..\\bar").
    return ".." in PurePosixPath(value).parts or ".." in PureWindowsPath(value).parts


def looks_absolute(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _cache_basename(value: str) -> str:
    # Strip to the final path component across both separators, so a rooted or
    # nested value can only ever contribute a filename.
    base = re.split(r"[\\/]", value)[-1].strip()
    return base if base not in ("", ".", "..") else "cache.json"


def contained_cache_path(value: str) -> str:
    """Force any cache_path into the app-owned `_kb_cache/` subdir (CR-001)."""
    return f"{_KB_CACHE_DIRNAME}/{_cache_basename(value)}"


def checked_contained_cache_path(value: str) -> str:
    """Boundary guard: reject absolute/`..` cache_path with a clear 400, then
    return the contained relative path. Callers store the returned value."""
    if has_traversal(value):
        raise HTTPException(status_code=400, detail="Knowledge base 'cache_path' must not contain '..' path segments")
    if looks_absolute(value):
        raise HTTPException(
            status_code=400,
            detail="Knowledge base 'cache_path' must be a relative path (it is stored under an application-owned directory)",
        )
    return contained_cache_path(value)


def check_path_traversal(value: str) -> None:
    """Boundary guard for a KB `path` (absolute local_folder paths stay allowed;
    only `..` traversal is rejected)."""
    if has_traversal(value):
        raise HTTPException(status_code=400, detail="Knowledge base 'path' must not contain '..' path segments")


def contain_kb_config_for_load(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a KB config with cache_path contained. Non-raising, for
    load time -- so a record persisted before the boundary guards existed still
    can't write outside `_kb_cache/`."""
    cache_path = config.get("cache_path")
    if isinstance(cache_path, str):
        return {**config, "cache_path": contained_cache_path(cache_path)}
    return config


def contain_pipeline_config_for_load(config: Dict[str, Any]) -> Dict[str, Any]:
    """As above, for a pipeline config's inline `knowledge_bases` list."""
    kbs = config.get("knowledge_bases")
    if not isinstance(kbs, list):
        return config
    return {
        **config,
        "knowledge_bases": [
            contain_kb_config_for_load(kb) if isinstance(kb, dict) else kb for kb in kbs
        ],
    }


def ensure_contained_cache_path_for_source(config: Dict[str, Any], source: Path) -> None:
    """Reject a cache path whose resolved target escapes the owned cache dir.

    ``contained_cache_path`` removes lexical traversal, but an existing
    ``_kb_cache`` directory could itself be a symlink/junction. Resolve both
    sides before the vector KB creates its cache so backend-managed pipelines
    never turn that into an arbitrary server-file write (CR-001).
    """
    cache_path = config.get("cache_path")
    if not isinstance(cache_path, str):
        return

    root = source.parent.resolve()
    cache_root = (root / _KB_CACHE_DIRNAME).resolve()
    target = (source.parent / cache_path).resolve()
    try:
        target.relative_to(cache_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Knowledge base 'cache_path' resolves outside the application-owned cache directory",
        ) from exc


def ensure_pipeline_cache_paths_for_source(config: Dict[str, Any], source: Path) -> None:
    """Apply resolved-target containment to every inline knowledge base."""
    knowledge_bases = config.get("knowledge_bases")
    if not isinstance(knowledge_bases, list):
        return
    for kb in knowledge_bases:
        if isinstance(kb, dict):
            ensure_contained_cache_path_for_source(kb, source)


def load_knowledge_base_tools(
    db: Session, raw: Dict[str, Any], source: Path, *, org_id: Optional[int] = None
) -> Dict[str, Any]:
    """Return a name -> tool mapping for only the standalone knowledge bases
    `raw`'s agents actually reference by name in their `tools:` lists.

    Building a knowledge base costs real work either way: an upload-managed
    KB with a completed ingestion job is rebuilt from its persisted
    Document/Chunk rows (`resolve_knowledge_base`), and one without a job --
    a manual-path KB, or an upload predating this feature -- falls back to
    re-reading and re-chunking every file from disk (and, for type: vector,
    calling an embedding model). This only pays either cost for knowledge
    bases the pipeline being loaded actually uses, not every standalone
    knowledge base in the database.

    Name resolution is org-scoped: only `org_id`'s knowledge bases resolve
    (KBs have no platform tier), so one org can never reference another
    org's KB by name.
    """
    referenced = {
        tool_name
        for agent in raw.get("agents", [])
        for tool_name in agent.get("tools", [])
    }
    if not referenced:
        return {}

    records = (
        db.query(KnowledgeBaseRecord)
        .filter(
            KnowledgeBaseRecord.name.in_(referenced),
            KnowledgeBaseRecord.org_id == org_id,
        )
        .all()
    )
    tools: Dict[str, Any] = {}
    for record in records:
        # Fail closed on a legacy KB whose name shadows a built-in tool (F4).
        # New collisions are blocked at KB PUT/upload, but a record predating
        # that guard would silently replace the built-in at load; refuse instead
        # (covers both `_get_pipeline` and the autonomous trigger, which share
        # this loader).
        if record.name in REGISTRY:
            raise ConfigurationError(
                f"Knowledge base '{record.name}' shadows a built-in tool of the "
                "same name; rename the knowledge base."
            )
        kb = resolve_knowledge_base(db, record, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools


def resolve_knowledge_base(
    db: Session, record: KnowledgeBaseRecord, source: Optional[Path] = None
) -> Any:
    """Build the `KnowledgeBase` for one `KnowledgeBaseRecord`: DB-backed
    (from its most recent completed `IngestionJob`'s Document/Chunk rows) if
    one exists, else the original file-based construction (a pre-existing KB
    that predates this feature and was never re-uploaded). Shared by
    `load_knowledge_base_tools` (above) and `builder.py::_all_knowledge_base_tools`
    (the pre-Specification "every standalone KB" catalog builder), so both
    resolve a KB's live content the same way.

    `source` is the pipeline file the file-based fallback resolves relative
    paths against, so omitting it turns that fallback **off**: a caller with
    no pipeline in hand (the "Try a search" endpoint, which resolves a
    collection on its own) gets `KnowledgeBaseNotReady` for a legacy
    file-backed KB rather than a rebuild that re-parses every file -- and, for
    a `vector` one, re-embeds all of them unmetered -- on every click."""
    job = (
        db.query(IngestionJob)
        .filter_by(kb_id=record.id, status="completed")
        # Order by `id`, not `completed_at`: overlapping uploads for the
        # same KB (e.g. rapid `replace=true` retries) can finish out of
        # submission order on the 4-worker executor. `id` is assigned inside
        # the serialized `_kb_upload_lock` staging block, so it's monotonic
        # with submission order even when completion isn't -- ordering by
        # `completed_at` could let an older, slower upload's job "win" over
        # a newer one that already completed (Codex review finding).
        .order_by(IngestionJob.id.desc())
        .first()
    )
    if job is not None:
        return _build_knowledge_base_from_job(record, job, db)
    # No completed job. Distinguish two cases: a true legacy KB (predates
    # this feature, never had any IngestionJob row -- fall back to the
    # original file-based construction) from an upload-managed KB whose
    # ingestion is still queued/running, or has only ever failed. The latter
    # must NOT take the legacy fallback: `record.config["path"]` is the KB's
    # upload root, which recursively contains every version subdirectory
    # (including the currently-staging one), so scanning it directly would
    # serve un-vetted, possibly-partial, or entirely un-embedded content
    # instead of treating the KB as not yet servable (Codex review finding).
    latest = (
        db.query(IngestionJob)
        .filter_by(kb_id=record.id)
        .order_by(IngestionJob.id.desc())
        .first()
    )
    if latest is not None:
        # Distinguish "not ready yet" from "will never be ready". A KB whose
        # newest attempt failed is stuck until someone re-uploads or deletes
        # it, so telling the customer to wait was permanently wrong advice --
        # say what actually went wrong and what to do about it instead.
        if latest.status == "failed":
            errors = ingestion.job_status_payload(db, latest)["errors"]
            detail = errors[0]["error"] if errors else "the documents could not be processed"
            # A per-document error may already be a full sentence (the
            # unsupported-type and no-extractable-text messages both are), so
            # don't append a second full stop onto it.
            raise KnowledgeBaseNotReady(
                f"Knowledge base '{record.name}' could not be indexed: "
                f"{detail.rstrip('.')}. Upload the documents again, or delete it."
            )
        raise KnowledgeBaseNotReady(
            f"Knowledge base '{record.name}' has no completed ingestion yet. "
            "Wait for the current upload to finish processing and try again."
        )
    if source is None:
        raise KnowledgeBaseNotReady(
            f"Knowledge base '{record.name}' was not uploaded through the app "
            "and cannot be searched here."
        )
    config = resolve_kb_upload_path(contain_kb_config_for_load(record.config))
    ensure_contained_cache_path_for_source(config, source)
    return _build_knowledge_base(config, source)


def _build_knowledge_base_from_job(record: KnowledgeBaseRecord, job: "IngestionJob", db: Session) -> Any:
    """Build the matching KnowledgeBase subclass from a completed job's
    Document/Chunk rows -- the DB-backed read path (see
    docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md).
    Never reads from disk.

    Every *shape* decision (which subclass, whether the rows carry vectors,
    which model embeds the query) comes from the **job**, not from
    `record.config`: `upload_knowledge_base` advances `config` to the new
    spec the moment an upload is dispatched, while this job -- the newest
    *completed* one -- may still be a previous generation ingested under a
    different type or embedding model. That window is a normal re-upload's
    ingestion time, and permanent if the new job fails. Reading `config`'s
    type here meant `json.loads(None)` on local_folder chunks whenever the
    new spec said vector/hybrid, and silently mismatched vector spaces
    whenever only `embedding_model` changed. `config` remains the source for
    the retrieval knobs below, which apply uniformly to whichever generation
    is live."""
    rows = (
        db.query(KnowledgeChunk, KnowledgeDocument.filename)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .filter(KnowledgeDocument.ingestion_job_id == job.id)
        .order_by(KnowledgeDocument.filename, KnowledgeChunk.chunk_index)
        .all()
    )
    # Each chunk carries its row identity, so a search hit (and the trace
    # event built from it) can name the chunk, its document and the job --
    # i.e. which generation of the collection -- it came from.
    chunks = [
        _Chunk(
            source=filename, text=chunk.text, page=chunk.page, heading=chunk.heading,
            chunk_id=chunk.id, document_id=chunk.document_id, ingestion_job_id=job.id,
        )
        for chunk, filename in rows
    ]

    config = record.config
    kb_type = job.kb_type or "local_folder"
    common_kwargs: Dict[str, Any] = {
        # From `config`, not the job: the description is what the agent's
        # tool says about the collection, so an edited one should take effect
        # immediately rather than waiting for the next ingestion.
        "description": config.get("description"),
        "top_k": config.get("top_k", 5),
        "rerank_model": config.get("rerank_model"),
        "candidate_k": config.get("candidate_k"),
        "query_expansion_model": config.get("query_expansion_model"),
        "query_expansion_count": config.get("query_expansion_count", 3),
    }

    if kb_type == "local_folder":
        return LocalFolderKnowledgeBase.from_chunks(record.name, chunks, **common_kwargs)

    vectors = [json.loads(chunk.embedding_json) for chunk, _filename in rows]
    embedding_model = job.embedding_model
    vector_kwargs = {**common_kwargs, "score_threshold": config.get("score_threshold")}
    if kb_type == "vector":
        return VectorKnowledgeBase.from_chunks(record.name, chunks, vectors, embedding_model, **vector_kwargs)
    if kb_type == "hybrid":
        return HybridKnowledgeBase.from_chunks(record.name, chunks, vectors, embedding_model, **vector_kwargs)
    raise ConfigurationError(f"Knowledge base '{record.name}' has unknown type '{kb_type}'")


def kb_name_collisions(db: Session, org_id: Optional[int], raw_spec: Dict[str, Any]) -> List[str]:
    """KB names in `raw_spec` (inline + referenced standalone) that shadow a built-in tool.

    Name-only (no KB is built), so it can run before path validation / build.
    """
    referenced = {
        tool
        for agent in raw_spec.get("agents", []) or []
        for tool in (agent.get("tools") or [])
    }
    standalone: set = set()
    if referenced:
        standalone = {
            row.name
            for row in db.query(KnowledgeBaseRecord.name).filter(
                KnowledgeBaseRecord.org_id == org_id,
                KnowledgeBaseRecord.name.in_(referenced),
            )
        }
    return find_kb_tool_collisions(raw_spec, standalone, REGISTRY)
