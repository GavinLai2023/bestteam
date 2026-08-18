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
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from bestteam.core.embeddings import resolve_embedding_model
from bestteam.core.knowledge_base import (
    _NO_TEXT_MESSAGE,
    _SUPPORTED_SUFFIXES,
    _chunk_text,
    _has_extractable_text,
    _unsupported_suffix_message,
)
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


def _scrubbed(text: str, version_dir: Path) -> str:
    """Strip the server's absolute upload path out of an error message.

    `parse_file` and the third-party parsers under it routinely embed the
    full path they were handed in their exception text, and
    `job_status_payload` hands that text straight back to a self-service org
    member -- which would leak the deployment's on-disk layout
    (`.../data/knowledge_base_uploads/<org_id>/<name>/v_<hex>/...`). Removing
    the version directory prefix leaves only the customer's own filename,
    which is the part that's actually useful to them. Both separator flavors
    are stripped so the guard behaves the same on a Linux server and a
    Windows dev box.
    """
    raw = str(version_dir)
    for prefix in {raw, raw.replace("\\", "/"), raw.replace("/", "\\")}:
        for sep in ("\\", "/", ""):
            text = text.replace(prefix + sep, "")
    return text


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

    Every Document/Chunk row is buffered in plain Python until the parse loop
    AND the embedding call are both done, then written in one short
    transaction at the end. Flushing per file instead would take SQLite's
    RESERVED write lock on the first file and hold it -- through every
    remaining file's parse/chunk work and through the embedding provider's
    network round-trip -- until the final commit, blocking every other writer
    in the process (run rows, trace events, usage records, share messages)
    for the whole duration of a large upload.
    """
    db = Session(engine)
    try:
        job = db.get(IngestionJob, job_id)
        if job is None:
            return
        job.status = "running"
        db.commit()

        # (document, its chunks) pairs, none of them added to the session yet.
        # A chunk's `document_id` can't be set until its document has a real
        # id, so the pairing is what carries that association until the single
        # flush below assigns them.
        pending: List[Tuple[KnowledgeDocument, List[KnowledgeChunk]]] = []
        all_chunks: List[KnowledgeChunk] = []
        # Every staged file, not only the ones with a readable suffix: an
        # unsupported file filtered out here would leave no Document row at
        # all, so the customer's upload count and the job's
        # succeeded+failed totals would silently disagree and nothing would
        # ever say why. Rejecting it inside the parse loop instead makes it a
        # `failed` document with a reason, like any other bad file.
        files = sorted(p for p in version_dir.rglob("*") if p.is_file())
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
            doc_chunks: List[KnowledgeChunk] = []
            pending.append((doc, doc_chunks))

            try:
                suffix = file_path.suffix.lower()
                if suffix not in _SUPPORTED_SUFFIXES:
                    raise ValueError(_unsupported_suffix_message(suffix))
                text = parse_file(str(file_path))
                # A scanned PDF parses to its header line and nothing else --
                # non-empty, so it would chunk into a content-free chunk that
                # matches no query and reports no problem.
                if not _has_extractable_text(text):
                    raise ValueError(_NO_TEXT_MESSAGE)
                pieces = _chunk_text(text, chunk_size, chunk_overlap, suffix=suffix)
                if not pieces:
                    raise ValueError("document produced no chunks (empty or whitespace-only content)")
            except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
                doc.status = "failed"
                doc.error = _capped(_scrubbed(str(exc), version_dir))
                job.documents_failed += 1
                continue

            for i, piece in enumerate(pieces):
                chunk = KnowledgeChunk(kb_id=kb_id, chunk_index=i, text=piece)
                doc_chunks.append(chunk)
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
                # Discard this run's buffered document/chunk objects (never
                # added to the session) along with the job's own pending
                # counter increments: a vector/hybrid KB with no embeddings is
                # unservable, so a total embedding failure must leave no
                # partial rows behind, not just a "failed" job status.
                db.rollback()
                job = db.get(IngestionJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = _capped(_scrubbed(str(exc), version_dir))
                    job.completed_at = _now()
                    db.commit()
                _safe_prune_failed_versions(db, kb_id, version_dir.parent, job_id)
                return
            for chunk, vector in zip(all_chunks, vectors):
                chunk.embedding_json = json.dumps(vector)
                chunk.embedding_model = embedding_model

        # The one write transaction: insert the documents, flush to get their
        # ids, point each buffered chunk at its document, and commit the whole
        # batch together with the job's terminal status.
        for doc, _doc_chunks in pending:
            db.add(doc)
        db.flush()
        for doc, doc_chunks in pending:
            for chunk in doc_chunks:
                chunk.document_id = doc.id
                db.add(chunk)

        if all_chunks:
            job.status = "completed"
        else:
            job.status = "failed"
            job.error = _capped("Knowledge base has no readable documents")
        job.completed_at = _now()
        db.commit()

        if job.status == "completed":
            # A cached workflow may have been compiled against this KB's
            # prior document set (or, for a first upload, may not know the
            # KB is servable yet). This is the point the KB's live content
            # actually changes -- earlier, at upload-dispatch time, it
            # doesn't yet (CR-005: the freshness key alone doesn't catch a
            # KB's underlying documents changing without its own row's
            # `updated_at` changing). Imported lazily -- `knowledge_bases.py`
            # imports this module, so a module-level import here would be
            # circular; same workaround `_invalidate_workflow_cache()` itself
            # already uses for its own `main` circularity. Isolated in its
            # own try/except, same reasoning as the pruning call below: if
            # this raised uncaught, the outer except would mark the
            # just-completed job "failed" even though its Document/Chunk
            # rows are already durable and correct -- see the module
            # docstring's "atomic swap" invariant.
            try:
                from .knowledge_bases import _invalidate_workflow_cache

                _invalidate_workflow_cache()
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "Workflow cache invalidation failed for KB %s after ingestion "
                    "job %s completed; a cached workflow may briefly keep serving "
                    "stale content", kb_id, job_id, exc_info=True,
                )

            # Best-effort cleanup only: pruning must never be able to
            # retroactively invalidate an already-committed successful
            # ingestion. If this raised uncaught, the outer except below
            # would mark the just-completed job "failed" even though its
            # Document/Chunk rows are already durable and correct -- see
            # the module docstring's "atomic swap" invariant. Kept in its
            # own try/except, independent of cache invalidation above, so a
            # pruning failure can never suppress (or be blamed for) the
            # cache having already been correctly invalidated (and vice
            # versa).
            try:
                _prune_old_ingestion_versions(db, kb_id, version_dir.parent)
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "Pruning old ingestion versions failed for KB %s; "
                    "the just-completed job is unaffected", kb_id, exc_info=True,
                )
                db.rollback()

        # Runs on the failed path too (and it's the only cleanup that does):
        # a customer retrying an unparseable upload never produces a completed
        # job, so nothing above would ever reclaim those attempts' staged
        # files.
        _safe_prune_failed_versions(db, kb_id, version_dir.parent, job_id)
    except Exception:  # noqa: BLE001 -- a worker-thread failure must never propagate silently
        _logger.exception("Ingestion job %s failed on the worker thread", job_id)
        try:
            db.rollback()
            job = db.get(IngestionJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = "The ingestion job failed due to an internal error."
                job.completed_at = _now()
                db.commit()
        except Exception:  # noqa: BLE001
            _logger.warning("Could not persist failed status for ingestion job %s", job_id)
    finally:
        db.close()


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _prune_old_ingestion_versions(db: Session, kb_id: int, kb_root: Path) -> None:
    """Keep only the `_KEEP_COMPLETED_GENERATIONS` most recent completed
    jobs for this KB; delete every older completed job's rows and on-disk
    version directory. A failed/queued/running job is never pruned here --
    only completed jobs count as "old versions" (a still-failed job's rows
    are its own diagnostic record, left for the operator/customer to see).
    Reclaiming a failed job's on-disk directory is
    `_prune_failed_ingestion_versions`' job, below.
    """
    completed = (
        db.query(IngestionJob)
        .filter_by(kb_id=kb_id, status="completed")
        # `id`, not `completed_at`: see resolve_knowledge_base()'s matching
        # comment in knowledge_bases.py -- completion order isn't guaranteed
        # to match submission order, so ordering by `completed_at` here could
        # prune the newest upload's rows instead of an older one's (Codex
        # review finding). Matches _prune_failed_ingestion_versions below,
        # which already orders by `id`.
        .order_by(IngestionJob.id.desc())
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
            shutil.rmtree(version_dir, ignore_errors=True)
        db.delete(old_job)
    if completed[_KEEP_COMPLETED_GENERATIONS:]:
        db.commit()


def _prune_failed_ingestion_versions(db: Session, kb_id: int, kb_root: Path, current_job_id: int) -> None:
    """Delete the on-disk version directory of every `failed` job for this KB
    except the most recent one.

    `_prune_old_ingestion_versions` above only ever looks at `completed`
    jobs, so without this a customer repeatedly retrying an upload that can't
    be parsed accumulates one staged version directory per attempt with
    nothing ever reclaiming them (the legacy file-based path's
    cleanup-on-failure no longer exists). The most recent failed job keeps
    its directory as a diagnostic copy of exactly what the customer sent --
    the same one-grace-generation shape `_KEEP_COMPLETED_GENERATIONS` gives
    completed jobs, which bounds the leak at a single extra version.

    Failed jobs' *rows* are deliberately kept either way: they're the
    customer-visible error record behind the job-status API, and they cost
    bytes rather than the megabytes this reclaims. `current_job_id` is never
    pruned -- a job resolving right now is the caller's own, and on the
    queued/running paths its directory may still be in use.
    """
    failed = (
        db.query(IngestionJob)
        .filter_by(kb_id=kb_id, status="failed")
        .order_by(IngestionJob.id.desc())
        .all()
    )
    for old_job in failed[1:]:
        if old_job.id == current_job_id:
            continue
        version_dir = kb_root / old_job.version
        if version_dir.is_dir():
            shutil.rmtree(version_dir, ignore_errors=True)


def _safe_prune_failed_versions(db: Session, kb_id: int, kb_root: Path, current_job_id: int) -> None:
    """Best-effort wrapper: cleanup must never be able to flip an
    already-resolved job's status (same reasoning as the completed-generation
    pruning call above)."""
    try:
        _prune_failed_ingestion_versions(db, kb_id, kb_root, current_job_id)
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Pruning failed ingestion versions for KB %s did not complete; "
            "the resolved job is unaffected", kb_id, exc_info=True,
        )
        db.rollback()


def delete_kb_ingestion_data(db: Session, kb_id: int) -> None:
    """Bulk-delete every IngestionJob/KnowledgeDocument/KnowledgeChunk row
    for a KB. Does NOT commit -- called from crud.py's delete route inside
    its own existing delete+commit+rmtree transaction, so this participates
    in that same commit rather than creating a separate one."""
    db.query(KnowledgeChunk).filter_by(kb_id=kb_id).delete(synchronize_session=False)
    db.query(KnowledgeDocument).filter_by(kb_id=kb_id).delete(synchronize_session=False)
    db.query(IngestionJob).filter_by(kb_id=kb_id).delete(synchronize_session=False)


def fail_interrupted_jobs(engine: Engine) -> int:
    """Resolve every `queued`/`running` job to `failed` and return how many.

    Called once from `main.py::_lifespan`. The executor lives in this
    process, so a job still queued/running when the app starts belongs to a
    process that no longer exists -- its worker died mid-flight and nothing
    will ever resolve it. Left alone they are permanent: the job-status API
    spins forever, and `knowledge_bases.delete_knowledge_base` refuses to
    delete the KB for as long as one exists. This is what bounds that
    refusal to "until the next restart" instead of "forever".

    One bulk UPDATE, no ORM objects loaded: this runs on the startup path,
    before anything is served.
    """
    with Session(engine) as db:
        updated = (
            db.query(IngestionJob)
            .filter(IngestionJob.status.in_(("queued", "running")))
            .update(
                {
                    IngestionJob.status: "failed",
                    IngestionJob.error: (
                        "Processing was interrupted by a server restart. "
                        "Please upload the documents again."
                    ),
                    IngestionJob.completed_at: _now(),
                },
                synchronize_session=False,
            )
        )
        db.commit()
    return updated


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
    errors = [{"filename": d.filename, "error": d.error} for d in failed_docs]
    # A whole-job failure (the embed call raised, or every document failed
    # to parse) sets `job.error` but writes no per-document `failed` rows,
    # so `errors` above stays empty and a poller sees only a bare "failed"
    # status with nothing to show the customer (Codex review finding).
    # `job.error` is already scrubbed/capped at write time, so it's safe to
    # return as-is.
    if job.status == "failed" and job.error and not errors:
        errors = [{"filename": None, "error": job.error}]
    return {
        "job_id": job.id,
        "status": job.status,
        "file_count": job.file_count,
        "documents_succeeded": job.documents_succeeded,
        "documents_failed": job.documents_failed,
        "chunk_count": chunk_count,
        "errors": errors,
        "config": config,
    }
