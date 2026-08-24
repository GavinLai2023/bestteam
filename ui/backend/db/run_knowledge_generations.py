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
