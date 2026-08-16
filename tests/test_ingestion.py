"""Tests for the async knowledge-base ingestion job (ui/backend/ingestion.py)."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from ui.backend import ingestion
from ui.backend import main as backend_main
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


def test_prune_failure_does_not_revert_completed_job_status(db, engine, tmp_path, monkeypatch):
    kb = _make_kb(db, name="prune_kb")

    def _run(version):
        job = _make_job(db, kb, version=version)
        version_dir = tmp_path / version
        version_dir.mkdir()
        (version_dir / "doc.txt").write_text("hello world", encoding="utf-8")
        ingestion.run_ingestion_job(
            job.id, kb.id, kb.org_id, version_dir,
            kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
            engine=engine,
        )
        db.expire_all()
        return db.get(IngestionJob, job.id)

    # Two completed generations first: with _KEEP_COMPLETED_GENERATIONS == 2,
    # pruning only has real deletion work to do once a THIRD completed job
    # exists (completed[2:] is non-empty).
    job1 = _run("v1")
    job2 = _run("v2")
    assert job1.status == "completed"
    assert job2.status == "completed"

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated pruning failure")

    monkeypatch.setattr(ingestion, "_prune_old_ingestion_versions", _boom)

    job3 = _run("v3")

    # The just-completed job's status/rows must survive a pruning failure --
    # pruning is best-effort cleanup, not part of what makes a job succeed.
    assert job3.status == "completed"
    docs = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job3.id).all()
    assert len(docs) == 1
    assert docs[0].status == "chunked"
    chunks = db.query(KnowledgeChunk).filter_by(document_id=docs[0].id).all()
    assert len(chunks) == 1


def test_failed_jobs_version_directories_are_pruned_except_the_most_recent(db, engine, tmp_path):
    # _prune_old_ingestion_versions only ever looks at completed jobs, so a
    # customer repeatedly retrying an unparseable upload used to accumulate
    # one staged version directory per attempt with nothing cleaning them up
    # (the legacy file-based path's cleanup-on-failure is gone).
    kb = _make_kb(db, name="failing_kb")

    def _run_failing(version):
        job = _make_job(db, kb, version=version)
        version_dir = tmp_path / version
        version_dir.mkdir()
        (version_dir / "bad.pdf").write_bytes(b"not a real pdf")
        ingestion.run_ingestion_job(
            job.id, kb.id, kb.org_id, version_dir,
            kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
            engine=engine,
        )
        db.expire_all()
        return db.get(IngestionJob, job.id)

    assert _run_failing("v1").status == "failed"
    assert _run_failing("v2").status == "failed"
    assert _run_failing("v3").status == "failed"

    assert not (tmp_path / "v1").exists()
    assert not (tmp_path / "v2").exists()
    # The most recent failure keeps its files as the operator's diagnostic
    # copy of exactly what the customer sent.
    assert (tmp_path / "v3").is_dir()
    # The rows themselves are the customer-visible error record and stay.
    assert db.query(IngestionJob).filter_by(kb_id=kb.id, status="failed").count() == 3


def test_document_error_does_not_leak_the_server_upload_path(db, engine, tmp_path, monkeypatch):
    # `job_status_payload` hands a document's error text straight back to a
    # self-service org member, and third-party parsers routinely embed the
    # absolute path they were given -- which would expose the deployment's
    # on-disk layout.
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("hello world", encoding="utf-8")

    def _boom(path):
        raise ValueError(f"Failed to parse {path}: unsupported encoding")

    monkeypatch.setattr(ingestion, "parse_file", _boom)

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    failed_doc = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id, status="failed").one()
    assert str(version_dir) not in failed_doc.error
    assert str(tmp_path) not in failed_doc.error
    # The customer's own filename -- the useful part -- still survives.
    assert "doc.txt" in failed_doc.error


def test_completed_job_invalidates_the_workflow_cache(db, engine, tmp_path):
    # CR-005 recurring through the async path: a workflow using this KB may
    # have been cached against its prior (or, on a first upload, not-yet-
    # servable) document set. The upload-dispatch request commits the KB
    # record's own `updated_at` (correctly busting the freshness key for the
    # transition), but the KB's live content only actually changes here, when
    # the job resolves "completed" -- so cache invalidation has to happen at
    # THIS point, not (only) at dispatch time, or a cache rebuilt during the
    # queued/running window keeps serving stale content forever after.
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("Refunds are allowed within 30 days.", encoding="utf-8")

    backend_main._workflow_cache.clear()
    backend_main._workflow_cache[(kb.org_id, "some_workflow")] = ("stale-workflow", "key")
    generation_before = backend_main._workflow_cache_generation

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    assert db.get(IngestionJob, job.id).status == "completed"
    assert backend_main._workflow_cache == {}
    assert backend_main._workflow_cache_generation != generation_before


def test_failed_job_does_not_invalidate_the_workflow_cache(db, engine, tmp_path):
    # A failed job never changes what's servable (the prior completed
    # generation, if any, is still the live one) -- no cache invalidation is
    # needed or expected on this path.
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "bad.pdf").write_bytes(b"not a real pdf")

    backend_main._workflow_cache.clear()
    backend_main._workflow_cache[(kb.org_id, "some_workflow")] = ("still-fresh-workflow", "key")
    generation_before = backend_main._workflow_cache_generation

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    assert db.get(IngestionJob, job.id).status == "failed"
    assert backend_main._workflow_cache == {(kb.org_id, "some_workflow"): ("still-fresh-workflow", "key")}
    assert backend_main._workflow_cache_generation == generation_before


def test_cache_invalidation_failure_does_not_revert_completed_job_status(db, engine, tmp_path, monkeypatch):
    # Mirrors test_prune_failure_does_not_revert_completed_job_status: cache
    # invalidation is isolated in its own try/except, same as pruning, so a
    # failure in it can never propagate to the outer handler and flip an
    # already-durable, already-successful job to "failed" (it runs after
    # job.status = "completed" is already committed).
    from ui.backend import knowledge_bases

    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("hello world", encoding="utf-8")

    def _boom():
        raise RuntimeError("simulated cache invalidation failure")

    monkeypatch.setattr(knowledge_bases, "_invalidate_workflow_cache", _boom)

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "completed"
    docs = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id).all()
    assert len(docs) == 1
    assert docs[0].status == "chunked"
    chunks = db.query(KnowledgeChunk).filter_by(document_id=docs[0].id).all()
    assert len(chunks) == 1
