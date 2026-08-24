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
