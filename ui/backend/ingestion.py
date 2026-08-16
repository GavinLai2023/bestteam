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
                # Discard this run's already-flushed-but-uncommitted document/
                # chunk inserts too: a vector/hybrid KB with no embeddings is
                # unservable, so a total embedding failure must leave no
                # partial rows behind, not just a "failed" job status.
                db.rollback()
                job = db.get(IngestionJob, job_id)
                if job is not None:
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
