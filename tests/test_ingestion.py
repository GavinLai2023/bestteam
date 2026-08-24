"""Tests for the async knowledge-base ingestion job (ui/backend/ingestion.py)."""

import functools
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from langchain_core.embeddings import Embeddings

from bestteam.core.embeddings import embed_documents_in_batches
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


def test_unsupported_file_is_recorded_as_failed_with_reason(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    job.file_count = 2
    db.commit()
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "good.txt").write_text("Refunds within 30 days.", encoding="utf-8")
    (version_dir / "photo.png").write_bytes(b"\x89PNG\r\n")

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
    # Every staged file is accounted for -- an unsupported one no longer
    # silently vanishes between the upload's count and the job's totals.
    assert job.documents_succeeded + job.documents_failed == job.file_count
    failed_doc = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id, status="failed").one()
    assert failed_doc.filename == "photo.png"
    assert "Unsupported file type" in failed_doc.error
    assert ".png" in failed_doc.error


def test_scanned_pdf_header_only_is_failed_not_a_content_free_chunk(db, engine, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    kb = _make_kb(db)
    job = _make_job(db, kb)
    job.file_count = 2
    db.commit()
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "good.txt").write_text("Refunds within 30 days.", encoding="utf-8")
    # A blank page stands in for a scanned one: pypdf extracts no text, so the
    # parser returns nothing but its own "[PDF: ...]" header line -- non-empty,
    # and previously chunked as if it were content.
    buffer = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    (version_dir / "scan.pdf").write_bytes(buffer.getvalue())

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
    assert failed_doc.filename == "scan.pdf"
    assert "OCR" in failed_doc.error
    texts = [c.text for c in db.query(KnowledgeChunk).filter_by(kb_id=kb.id).all()]
    assert not any("[PDF:" in text for text in texts)


def test_only_unsupported_files_fails_the_job_with_per_file_errors(db, engine, tmp_path):
    kb = _make_kb(db)
    job = _make_job(db, kb)
    job.file_count = 2
    db.commit()
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "photo.png").write_bytes(b"\x89PNG\r\n")
    (version_dir / "archive.zip").write_bytes(b"PK\x03\x04")

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "failed"
    assert job.documents_succeeded == 0
    assert job.documents_failed == 2
    # The customer sees which files were rejected and why, not just a bare
    # job-level "no readable documents".
    payload = ingestion.job_status_payload(db, job)
    assert {e["filename"] for e in payload["errors"]} == {"photo.png", "archive.zip"}
    assert all("Unsupported file type" in e["error"] for e in payload["errors"])


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


def test_transient_embedding_failure_is_retried_and_the_job_completes(db, engine, tmp_path, monkeypatch):
    """A provider hiccup on one batch must not throw away the whole upload."""
    kb = _make_kb(db, name="vec_kb")
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("Refunds within 30 days.", encoding="utf-8")

    calls = []

    class _FlakyEmbeddings(Embeddings):
        def embed_documents(self, texts):
            calls.append(list(texts))
            if len(calls) == 1:
                raise RuntimeError("provider hiccup")
            return [[float(len(text))] for text in texts]

        def embed_query(self, text):  # pragma: no cover - unused here
            return [float(len(text))]

    monkeypatch.setattr(ingestion, "resolve_embedding_model", lambda spec: _FlakyEmbeddings())
    # Stub only the backoff sleep; the real batching/retry logic is under test.
    monkeypatch.setattr(
        ingestion,
        "embed_documents_in_batches",
        functools.partial(embed_documents_in_batches, sleep=lambda _seconds: None),
    )

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="vector", chunk_size=1000, chunk_overlap=100, embedding_model="fake-spec:x",
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "completed"
    assert len(calls) == 2
    chunks = db.query(KnowledgeChunk).filter_by(kb_id=kb.id).all()
    assert chunks and all(c.embedding_json for c in chunks)


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


def test_prune_old_ingestion_versions_orders_by_submission_not_completion(db, tmp_path):
    # Overlapping uploads for the same KB can finish out of submission
    # order. Pruning must key off `id` (assigned in submission order), not
    # `completed_at`, or an older, slower job that finishes last could evict
    # a newer job's rows instead of an actually-older one's (Codex review
    # finding).
    from datetime import datetime, timedelta, timezone

    kb = _make_kb(db, name="racey_kb")
    base = datetime.now(timezone.utc)

    def _completed_job(version, completed_at):
        job = IngestionJob(
            kb_id=kb.id, org_id=1, version=version, status="completed",
            file_count=1, completed_at=completed_at,
        )
        db.add(job)
        db.commit()
        (tmp_path / version).mkdir()
        return job

    # job1 is submitted first (lowest id) but finishes LAST (latest
    # completed_at) -- e.g. a slow overlapping upload.
    job1 = _completed_job("v1", base + timedelta(seconds=100))
    job2 = _completed_job("v2", base + timedelta(seconds=1))
    job3 = _completed_job("v3", base + timedelta(seconds=2))

    ingestion._prune_old_ingestion_versions(db, kb.id, tmp_path)

    remaining = {j.id for j in db.query(IngestionJob).filter_by(kb_id=kb.id, status="completed").all()}
    # Keeps the 2 most recently *submitted* generations (job2, job3), not the
    # 2 with the latest completed_at (job1, job3) -- job1 was submitted
    # first and must be the one pruned despite finishing last.
    assert remaining == {job2.id, job3.id}
    assert (tmp_path / "v2").is_dir()
    assert (tmp_path / "v3").is_dir()
    assert not (tmp_path / "v1").exists()
    assert job1.id not in remaining


def test_job_status_payload_surfaces_job_level_error_with_no_document_rows(db):
    # A whole-job failure (the embed call raised, or every document failed
    # to parse) sets `job.error` but writes no per-document `failed` rows,
    # so a poller used to see a bare "failed" status with nothing to show
    # the customer (Codex review finding).
    kb = _make_kb(db, name="embed_fail_kb")
    job = IngestionJob(
        kb_id=kb.id, org_id=1, version="v1", status="failed",
        file_count=1, error="The embedding model is unavailable.",
    )
    db.add(job)
    db.commit()

    payload = ingestion.job_status_payload(db, job)
    assert payload["errors"] == [{"filename": None, "error": "The embedding model is unavailable."}]


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


def test_completed_job_invalidates_the_pipeline_cache(db, engine, tmp_path):
    # CR-005 recurring through the async path: a pipeline using this KB may
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

    backend_main._pipeline_cache.clear()
    backend_main._pipeline_cache[(kb.org_id, "some_pipeline")] = ("stale-pipeline", "key")
    generation_before = backend_main._pipeline_cache_generation

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    assert db.get(IngestionJob, job.id).status == "completed"
    assert backend_main._pipeline_cache == {}
    assert backend_main._pipeline_cache_generation != generation_before


def test_failed_job_does_not_invalidate_the_pipeline_cache(db, engine, tmp_path):
    # A failed job never changes what's servable (the prior completed
    # generation, if any, is still the live one) -- no cache invalidation is
    # needed or expected on this path.
    kb = _make_kb(db)
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "bad.pdf").write_bytes(b"not a real pdf")

    backend_main._pipeline_cache.clear()
    backend_main._pipeline_cache[(kb.org_id, "some_pipeline")] = ("still-fresh-pipeline", "key")
    generation_before = backend_main._pipeline_cache_generation

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    assert db.get(IngestionJob, job.id).status == "failed"
    assert backend_main._pipeline_cache == {(kb.org_id, "some_pipeline"): ("still-fresh-pipeline", "key")}
    assert backend_main._pipeline_cache_generation == generation_before


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

    monkeypatch.setattr(knowledge_bases, "_invalidate_pipeline_cache", _boom)

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


def test_fail_interrupted_jobs_marks_queued_and_running_failed_and_leaves_terminal_jobs_alone(db, engine):
    # A killed process leaves its jobs stuck queued/running forever, and the
    # delete guard (P0-1) refuses to delete a KB while one exists -- so the
    # startup sweep is what stops a restart making that refusal permanent.
    kb = _make_kb(db)
    queued = _make_job(db, kb, version="v_queued")
    running = _make_job(db, kb, version="v_running")
    running.status = "running"
    completed = _make_job(db, kb, version="v_completed")
    completed.status = "completed"
    already_failed = _make_job(db, kb, version="v_failed")
    already_failed.status = "failed"
    already_failed.error = "the original diagnosis"
    db.commit()
    queued_id, running_id = queued.id, running.id
    completed_id, already_failed_id = completed.id, already_failed.id

    assert ingestion.fail_interrupted_jobs(engine) == 2

    db.expire_all()
    for job_id in (queued_id, running_id):
        job = db.get(IngestionJob, job_id)
        assert job.status == "failed"
        assert job.error == (
            "Processing was interrupted by a server restart. "
            "Please upload the documents again."
        )
        assert job.completed_at is not None
    # A terminal job is never rewritten -- least of all a failed one, whose
    # `error` is the customer-visible record of what actually went wrong.
    assert db.get(IngestionJob, completed_id).status == "completed"
    stale = db.get(IngestionJob, already_failed_id)
    assert stale.status == "failed"
    assert stale.error == "the original diagnosis"


def test_chunk_page_and_heading_persist_and_round_trip_into_from_chunks(db, engine, tmp_path, monkeypatch):
    """Ingestion writes the two new location columns, and the DB-backed read
    path hands them back to the SDK so a citation survives the round trip."""
    from ui.backend.knowledge_bases import _build_knowledge_base_from_job

    kb = _make_kb(db)
    job = _make_job(db, kb)
    job.file_count = 2
    db.commit()
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    # pypdf can write a PDF but not a text-bearing one, so the parse result
    # for the .pdf is stubbed; the .md goes through the real parser.
    (version_dir / "manual.pdf").write_bytes(b"%PDF-1.4 stub")
    (version_dir / "guide.md").write_text(
        "## Refunds\nRefunds are allowed within 30 days.\n", encoding="utf-8"
    )
    real_parse_file = ingestion.parse_file

    def fake_parse_file(path):
        if path.endswith(".pdf"):
            return "[PDF: manual.pdf — 2 page(s)]\nShipping is free.\fReturns take five days."
        return real_parse_file(path)

    monkeypatch.setattr(ingestion, "parse_file", fake_parse_file)

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="local_folder", chunk_size=1000, chunk_overlap=100, embedding_model=None,
        engine=engine,
    )

    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "completed"
    pdf_doc = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id, filename="manual.pdf").one()
    pdf_chunks = (
        db.query(KnowledgeChunk).filter_by(document_id=pdf_doc.id).order_by(KnowledgeChunk.chunk_index).all()
    )
    assert [chunk.page for chunk in pdf_chunks] == [1, 2]
    md_doc = db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id, filename="guide.md").one()
    md_chunks = db.query(KnowledgeChunk).filter_by(document_id=md_doc.id).all()
    assert [chunk.heading for chunk in md_chunks] == ["Refunds"]

    record = db.get(KnowledgeBaseRecord, kb.id)
    rebuilt = _build_knowledge_base_from_job(record, job, db)
    assert {(c.source, c.page) for c in rebuilt._chunks} >= {("manual.pdf", 1), ("manual.pdf", 2)}
    assert "[source: manual.pdf, p.2]" in rebuilt.query("returns five days")
    assert "[source: guide.md § Refunds]" in rebuilt.query("refunds 30 days")


def _stub_embeddings(monkeypatch):
    """Let a job run under a *real* provider spec without a provider: only a
    non-`fake:` string spec is billable, so only that path is metered."""
    from langchain_core.embeddings import DeterministicFakeEmbedding

    monkeypatch.setattr(
        ingestion, "resolve_embedding_model", lambda spec: DeterministicFakeEmbedding(size=4)
    )


def test_vector_ingestion_records_kb_ingest_usage_row(db, engine, tmp_path, monkeypatch):
    from bestteam.core.embeddings import estimate_embedding_tokens
    from ui.backend.db.models import UsageRecord

    kb = _make_kb(db, name="vec_kb")
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("Refunds within 30 days.", encoding="utf-8")
    _stub_embeddings(monkeypatch)

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="vector", chunk_size=1000, chunk_overlap=100,
        embedding_model="openai:text-embedding-3-small", engine=engine,
    )

    db.expire_all()
    assert db.get(IngestionJob, job.id).status == "completed"
    chunks = db.query(KnowledgeChunk).filter_by(kb_id=kb.id).all()
    rows = db.query(UsageRecord).all()
    assert len(rows) == 1  # one row per job, not per chunk
    assert rows[0].agent == "kb:ingest"
    assert rows[0].run_id is None
    assert rows[0].ingestion_job_id == job.id
    assert rows[0].org_id == kb.org_id
    assert rows[0].model == "openai:text-embedding-3-small"
    assert rows[0].input_tokens == sum(estimate_embedding_tokens(c.text) for c in chunks)
    assert rows[0].output_tokens == 0


def test_local_folder_and_fake_spec_ingestion_record_nothing(db, engine, tmp_path):
    """A `local_folder` KB embeds nothing and a `fake:` spec is $0 -- neither
    is spend, so neither gets a row."""
    from ui.backend.db.models import UsageRecord

    kb = _make_kb(db, name="mixed_kb")
    for kb_type, spec, version in (("local_folder", None, "v_plain"), ("vector", "fake:4", "v_fake")):
        job = _make_job(db, kb, version=version)
        version_dir = tmp_path / version
        version_dir.mkdir()
        (version_dir / "doc.txt").write_text("Refunds within 30 days.", encoding="utf-8")
        ingestion.run_ingestion_job(
            job.id, kb.id, kb.org_id, version_dir,
            kb_type=kb_type, chunk_size=1000, chunk_overlap=100, embedding_model=spec,
            engine=engine,
        )
        db.expire_all()
        assert db.get(IngestionJob, job.id).status == "completed"

    assert db.query(UsageRecord).count() == 0


def test_metering_failure_never_flips_completed_job(db, engine, tmp_path, monkeypatch):
    """Metering is best-effort cleanup like cache invalidation and pruning: the
    job's chunks are already durable, so a failed `usage_records` write must
    not turn a completed ingestion into a failed one."""
    from ui.backend.db.models import UsageRecord

    kb = _make_kb(db, name="vec_kb")
    job = _make_job(db, kb)
    version_dir = tmp_path / "v_test"
    version_dir.mkdir()
    (version_dir / "doc.txt").write_text("Refunds within 30 days.", encoding="utf-8")
    _stub_embeddings(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated metering failure")

    monkeypatch.setattr(ingestion, "record_usage", _boom)

    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir,
        kb_type="vector", chunk_size=1000, chunk_overlap=100,
        embedding_model="openai:text-embedding-3-small", engine=engine,
    )

    db.expire_all()
    assert db.get(IngestionJob, job.id).status == "completed"
    chunk = db.query(KnowledgeChunk).filter_by(kb_id=kb.id).one()
    assert chunk.embedding_json is not None
    assert db.query(UsageRecord).count() == 0


# --- Incremental ingestion -------------------------------------------------
#
# Every upload replaces a collection wholesale, so before this a customer who
# changed one document in ten paid to re-embed the other nine. The new job
# carries an unchanged document's chunks -- embeddings included -- forward from
# the previous completed job, keyed on the content hash already being stored.


def _run(db, engine, job, kb, version_dir, **kwargs):
    options = {
        "kb_type": "local_folder",
        "chunk_size": 1000,
        "chunk_overlap": 100,
        "embedding_model": None,
    }
    options.update(kwargs)
    ingestion.run_ingestion_job(
        job.id, kb.id, kb.org_id, version_dir, engine=engine, **options
    )
    db.expire_all()
    return db.get(IngestionJob, job.id)


def _version(tmp_path, name, files):
    version_dir = tmp_path / name
    version_dir.mkdir()
    for filename, text in files.items():
        (version_dir / filename).write_text(text, encoding="utf-8")
    return version_dir


def test_an_unchanged_document_is_not_re_embedded(db, engine, tmp_path):
    kb = _make_kb(db, name="vec_kb")
    files = {"a.txt": "Refunds within 30 days.", "b.txt": "Shipping is free."}

    first = _make_job(db, kb, version="v_1")
    _run(db, engine, first, kb, _version(tmp_path, "v_1", files),
         kb_type="vector", embedding_model="fake:4")

    calls = []
    original = ingestion.embed_documents_in_batches

    def counting(embeddings, texts):
        calls.append(list(texts))
        return original(embeddings, texts)

    ingestion.embed_documents_in_batches = counting
    try:
        second = _make_job(db, kb, version="v_2")
        changed = dict(files, b="")
        changed = {"a.txt": files["a.txt"], "b.txt": "Shipping now costs money."}
        job = _run(db, engine, second, kb, _version(tmp_path, "v_2", changed),
                   kb_type="vector", embedding_model="fake:4")
    finally:
        ingestion.embed_documents_in_batches = original

    assert job.status == "completed"
    # Only the document that actually changed reached the provider.
    assert calls == [["Shipping now costs money."]]

    docs = {d.filename: d for d in db.query(KnowledgeDocument).filter_by(ingestion_job_id=job.id)}
    assert set(docs) == {"a.txt", "b.txt"}
    carried = db.query(KnowledgeChunk).filter_by(document_id=docs["a.txt"].id).one()
    assert carried.text == "Refunds within 30 days."
    # The carried chunk keeps a usable vector -- a KB rebuilt from this job
    # has to be servable, and nothing re-embedded it.
    assert carried.embedding_json is not None
    assert carried.embedding_model == "fake:4"


def test_carrying_forward_meters_only_what_was_embedded(db, engine, tmp_path, monkeypatch):
    # A `fake:` spec is $0 and writes no row at all, so this needs a billable
    # one -- stubbed, like the existing kb:ingest metering test above.
    from bestteam.core.embeddings import estimate_embedding_tokens
    from ui.backend.db.models import UsageRecord

    _stub_embeddings(monkeypatch)
    model = "openai:text-embedding-3-small"
    kb = _make_kb(db, name="vec_kb")
    files = {"a.txt": "Refunds within 30 days.", "b.txt": "Shipping is free."}
    first = _make_job(db, kb, version="v_1")
    _run(db, engine, first, kb, _version(tmp_path, "v_1", files),
         kb_type="vector", embedding_model=model)

    second = _make_job(db, kb, version="v_2")
    job = _run(db, engine, second, kb,
               _version(tmp_path, "v_2", dict(files, **{"b.txt": "Shipping now costs money."})),
               kb_type="vector", embedding_model=model)

    assert job.status == "completed"
    rows = db.query(UsageRecord).filter_by(ingestion_job_id=job.id).all()
    assert len(rows) == 1
    first_rows = db.query(UsageRecord).filter_by(ingestion_job_id=first.id).all()
    # The second upload billed for the one document that changed, the first
    # for both.
    assert rows[0].input_tokens == estimate_embedding_tokens("Shipping now costs money.")
    assert rows[0].input_tokens < first_rows[0].input_tokens


def test_a_changed_chunk_size_re_chunks_everything(db, engine, tmp_path):
    # Carried chunks were cut by the previous job's parameters. Reusing them
    # under new ones would leave a collection half-chunked one way and half
    # the other, with nothing saying so.
    kb = _make_kb(db)
    files = {"a.txt": "Refunds within 30 days. " * 20}
    first = _make_job(db, kb, version="v_1")
    _run(db, engine, first, kb, _version(tmp_path, "v_1", files), chunk_size=1000)

    second = _make_job(db, kb, version="v_2")
    job = _run(db, engine, second, kb, _version(tmp_path, "v_2", files),
               chunk_size=100, chunk_overlap=10)

    assert job.status == "completed"
    chunks = (
        db.query(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .filter(KnowledgeDocument.ingestion_job_id == job.id)
        .all()
    )
    assert len(chunks) > 1
    assert all(len(c.text) <= 100 for c in chunks)


def test_a_changed_embedding_model_re_embeds_everything(db, engine, tmp_path):
    kb = _make_kb(db, name="vec_kb")
    files = {"a.txt": "Refunds within 30 days."}
    first = _make_job(db, kb, version="v_1")
    _run(db, engine, first, kb, _version(tmp_path, "v_1", files),
         kb_type="vector", embedding_model="fake:4")

    second = _make_job(db, kb, version="v_2")
    job = _run(db, engine, second, kb, _version(tmp_path, "v_2", files),
               kb_type="vector", embedding_model="fake:8")

    chunk = (
        db.query(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .filter(KnowledgeDocument.ingestion_job_id == job.id)
        .one()
    )
    assert chunk.embedding_model == "fake:8"
    assert len(json.loads(chunk.embedding_json)) == 8


def test_a_job_records_the_parameters_it_chunked_with(db, engine, tmp_path):
    # The reuse decision reads them back off the previous job; the
    # KnowledgeBaseRecord's own config has already advanced to the new spec.
    kb = _make_kb(db)
    job = _make_job(db, kb)
    job = _run(db, engine, job, kb, _version(tmp_path, "v_test", {"a.txt": "hello"}),
               chunk_size=500, chunk_overlap=50)
    assert (job.chunk_size, job.chunk_overlap) == (500, 50)


def test_a_previous_job_with_unknown_parameters_is_not_reused(db, engine, tmp_path):
    # Every job written before the columns existed reads back as NULL. The
    # first upload after an upgrade re-embeds once; every one after that is
    # incremental.
    kb = _make_kb(db, name="vec_kb")
    files = {"a.txt": "Refunds within 30 days."}
    first = _make_job(db, kb, version="v_1")
    _run(db, engine, first, kb, _version(tmp_path, "v_1", files),
         kb_type="vector", embedding_model="fake:4")
    first.chunk_size = None
    first.chunk_overlap = None
    db.commit()

    calls = []
    original = ingestion.embed_documents_in_batches
    ingestion.embed_documents_in_batches = lambda e, t: (calls.append(list(t)), original(e, t))[1]
    try:
        second = _make_job(db, kb, version="v_2")
        _run(db, engine, second, kb, _version(tmp_path, "v_2", files),
             kb_type="vector", embedding_model="fake:4")
    finally:
        ingestion.embed_documents_in_batches = original

    assert calls == [["Refunds within 30 days."]]


def test_a_failed_job_is_never_carried_forward(db, engine, tmp_path):
    # Only a completed job is a collection's live document set, and a failed
    # one's rows are a diagnostic record.
    kb = _make_kb(db, name="vec_kb")
    files = {"a.txt": "Refunds within 30 days."}
    first = _make_job(db, kb, version="v_1")
    _run(db, engine, first, kb, _version(tmp_path, "v_1", files),
         kb_type="vector", embedding_model="fake:4")
    first.status = "failed"
    db.commit()

    calls = []
    original = ingestion.embed_documents_in_batches
    ingestion.embed_documents_in_batches = lambda e, t: (calls.append(list(t)), original(e, t))[1]
    try:
        second = _make_job(db, kb, version="v_2")
        _run(db, engine, second, kb, _version(tmp_path, "v_2", files),
             kb_type="vector", embedding_model="fake:4")
    finally:
        ingestion.embed_documents_in_batches = original

    assert calls == [["Refunds within 30 days."]]


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
