"""Tests for the org self-service knowledge-base upload endpoint
(`/api/org/knowledge-bases/{name}/upload`) -- the wizard's "Your documents"
step. Mirrors `test_org_settings.py`'s auth/org-scoping patterns and reuses
`test_crud_api.py`'s upload-endpoint assertions for the shared
`knowledge_bases.upload_knowledge_base()` implementation."""

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import knowledge_bases as backend_knowledge_bases
from ui.backend import main as backend_main
from ui.backend import org_knowledge_bases as backend_org_kb
from ui.backend.builder import _all_knowledge_base_tools, _with_knowledge_base_catalog
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db


def _wait_for_job_status(job_id, deadline_seconds=10):
    """Poll `IngestionJob.status` to a terminal state (completed/failed),
    opening a fresh session each time so we see the worker thread's commits."""
    deadline = time.monotonic() + deadline_seconds
    job = None
    while time.monotonic() < deadline:
        with open_test_db() as db:
            job = db.get(IngestionJob, job_id)
            if job is not None and job.status in ("completed", "failed"):
                return job.status
        time.sleep(0.05)
    return job.status if job is not None else None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(backend_knowledge_bases, "_KB_UPLOADS_DIR", tmp_path / "knowledge_base_uploads")
    backend_main._workflow_cache.clear()

    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    try:
        c = TestClient(backend_main.app)
        token = create_user_and_login(c)  # plain org member of 'default'
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _files(name="doc.txt", content=b"The refund policy allows returns within 30 days."):
    return [("files", (name, content, "text/plain"))]


def test_unauthenticated_401(client):
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=_files(),
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 401


def test_platform_operator_gets_403(client):
    op = create_user_and_login(client, username="op", org=None, admin=True)
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=_files(),
        headers={"Authorization": f"Bearer {op}"},
    )
    assert resp.status_code == 403


def test_org_member_can_upload_own_kb(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "policies"
    assert body["status"] == "queued"
    assert isinstance(body["job_id"], int)


def test_uploaded_kb_ingestion_job_completes_and_becomes_queryable(client, tmp_path):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert "30 days" in tools["policies"]("refund policy")


def test_upload_rejects_builtin_tool_name(client):
    resp = client.post("/api/org/knowledge-bases/web_search/upload", files=_files())
    assert resp.status_code == 400
    assert "built-in tool name" in resp.json()["detail"]


def test_upload_rejects_invalid_name(client):
    resp = client.post("/api/org/knowledge-bases/bad name/upload", files=_files())
    assert resp.status_code == 400


def test_uploaded_kb_is_visible_to_spec_generation(client, tmp_path):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        catalog_text = _with_knowledge_base_catalog(db, "", org_id)
        assert "policies" in catalog_text

        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert "policies" in tools
        assert "30 days" in tools["policies"]("refund policy")


def test_reupload_existing_name_requires_confirmation(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    # Same name again, no confirmation -- refused, not silently replaced.
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files(name="other.txt"))
    assert resp.status_code == 409

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        tools = _all_knowledge_base_tools(db, Path("."), org_id)
        # The original content is untouched.
        assert "30 days" in tools["policies"]("refund policy")

    # Confirmed replace succeeds.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"replace": "true"},
        files=_files(name="other.txt", content=b"Something else entirely."),
    )
    assert resp.status_code == 200


def test_self_service_kb_count_capped_per_org(client, monkeypatch):
    monkeypatch.setattr(backend_org_kb, "_MAX_SELF_SERVICE_KBS_PER_ORG", 1)
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post("/api/org/knowledge-bases/other_docs/upload", files=_files())
    assert resp.status_code == 403

    # Re-uploading the already-existing name is unaffected by the cap.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"replace": "true"},
        files=_files(),
    )
    assert resp.status_code == 200


def test_replace_upload_refused_while_a_previous_job_is_still_in_flight(client, monkeypatch):
    """Without this guard, a member retrying a stalled upload can pile up
    unbounded queued work on ingestion.py's fixed-size executor -- each
    retry stages up to _MAX_TOTAL_SIZE_BYTES to disk and queues an embedding
    call before anything would catch it (Codex review finding)."""
    from ui.backend import ingestion as backend_ingestion

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    # Hold the next job at "queued" by intercepting dispatch, mirroring
    # test_reupload_advances_config_but_serves_prior_generation_until_ready
    # below.
    submitted = []
    monkeypatch.setattr(
        backend_ingestion._executor, "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )

    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"replace": "true"},
        files=_files(name="v2.txt", content=b"Second generation content."),
    )
    assert resp.status_code == 200
    in_flight_job_id = resp.json()["job_id"]

    # A third upload while the second is still queued must be refused, not
    # queue a third round of work on top of it.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"replace": "true"},
        files=_files(name="v3.txt", content=b"Third generation content."),
    )
    assert resp.status_code == 409
    assert "still processing" in resp.json()["detail"]

    # Let the held job actually run and reach a terminal state.
    args, kwargs = submitted[0]
    args[0](*args[1:], **kwargs)
    assert _wait_for_job_status(in_flight_job_id) == "completed"

    # Once it's terminal, a new replace upload is allowed again.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"replace": "true"},
        files=_files(name="v3.txt", content=b"Third generation content."),
    )
    assert resp.status_code == 200


def test_concurrent_first_uploads_of_a_new_name_do_not_silently_replace(client):
    """Two concurrent first-time uploads of a name that doesn't exist yet can
    both observe `existing is None` before either enters the per-KB lock.
    Without re-checking existence inside that same lock, the second would
    silently replace the first's documents instead of getting the
    confirmation 409 this route exists to enforce (Codex review finding)."""
    import threading
    import time

    from ui.backend.knowledge_bases import _kb_upload_lock

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id

    # Simulate a first upload already inside its critical section.
    lock = _kb_upload_lock(f"{org_id}/newkb")
    lock.acquire()
    done = []

    def _second_upload():
        resp = client.post(
            "/api/org/knowledge-bases/newkb/upload",
            files=_files(name="second.txt", content=b"Second uploader's content."),
        )
        done.append(resp.status_code)

    worker = threading.Thread(target=_second_upload)
    worker.start()
    try:
        time.sleep(0.5)
        assert done == [], "the second upload must block on the per-KB lock, not race the existence check"

        # What the first uploader would have committed by now.
        with open_test_db() as db:
            db.add(KnowledgeBaseRecord(name="newkb", org_id=org_id, config={"type": "local_folder", "path": "x"}))
            db.commit()
    finally:
        lock.release()

    worker.join(timeout=5)
    assert done == [409], "second uploader must be refused, not silently replace the first"


def test_self_service_upload_rejects_oversized_file(client):
    big = b"x" * (backend_org_kb._MAX_FILE_SIZE_BYTES + 1)
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files(content=big))
    assert resp.status_code == 413


def test_smart_search_capability_reflects_env_var(client, monkeypatch):
    monkeypatch.delenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", raising=False)
    resp = client.get("/api/org/knowledge-bases/capabilities")
    assert resp.status_code == 200
    assert resp.json() == {"smart_search_available": False}

    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")
    resp = client.get("/api/org/knowledge-bases/capabilities")
    assert resp.json() == {"smart_search_available": True}


def test_smart_search_upload_builds_hybrid_kb_with_expansion_and_rerank(client, monkeypatch, tmp_path):
    from ui.backend.db.model_catalog import seed_default_catalog

    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")
    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_RERANK_MODEL", "fake:")
    with open_test_db() as db:
        seed_default_catalog(db)

    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"smart_search": "true"},
        files=_files(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        config = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one().config
        assert config["type"] == "hybrid"
        assert config["embedding_model"] == "fake:16"
        assert config["rerank_model"] == "fake:"
        # The wizard's own default chat model (seeded catalog's first non-fake
        # entry, alphabetically by spec -- list_entries orders by `spec`).
        assert config["query_expansion_model"] == "openai:gpt-4o"

        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert "30 days" in tools["policies"]("refund policy")


def test_smart_search_without_default_embedding_model_falls_back_to_local_folder(client, monkeypatch):
    monkeypatch.delenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", raising=False)
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"smart_search": "true"},
        files=_files(),
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        config = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one().config
        assert config["type"] == "local_folder"


def test_smart_search_off_by_default_stays_local_folder(client, monkeypatch):
    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        config = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one().config
        assert config["type"] == "local_folder"


def test_cross_org_upload_isolation(client, tmp_path):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files(
        content=b"Org A's refund policy: 30 days.",
    ))
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    other = create_user_and_login(client, username="bob", org="org_b")
    bob = {"Authorization": f"Bearer {other}"}
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=_files(content=b"Org B's refund policy: 14 days."),
        headers=bob,
    )
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    with open_test_db() as db:
        org_a_id = get_or_create_org(db, "default").id
        org_b_id = get_or_create_org(db, "org_b").id
        assert org_a_id != org_b_id

        rows = db.query(KnowledgeBaseRecord).filter_by(name="policies").all()
        assert {r.org_id for r in rows} == {org_a_id, org_b_id}
        # Disk-scoped under <org_id>/<name>, so the two orgs' same-named
        # uploads land in independent directories, not overwriting each other.
        paths = {r.org_id: Path(r.config["path"]) for r in rows}
        assert paths[org_a_id] != paths[org_b_id]

        tools_a = _all_knowledge_base_tools(db, tmp_path, org_a_id)
        tools_b = _all_knowledge_base_tools(db, tmp_path, org_b_id)
        assert "30 days" in tools_a["policies"]("refund policy")
        assert "14 days" in tools_b["policies"]("refund policy")


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
    bob = {"Authorization": f"Bearer {other}"}
    # org_b has its own same-named "policies" KB (with its own job_id/kb_id),
    # so the request below genuinely exercises the kb_id mismatch -> 404 path
    # rather than "no KB named 'policies' exists for this org at all" -> 404.
    assert client.post(
        "/api/org/knowledge-bases/policies/upload", files=_files(), headers=bob
    ).status_code == 200

    resp = client.get(
        f"/api/org/knowledge-bases/policies/ingestion-jobs/{job_id}",
        headers=bob,
    )
    assert resp.status_code == 404


def test_dispatch_failure_resolves_the_job_instead_of_stranding_it_queued(client, monkeypatch):
    """`_executor.submit` runs after the commit, outside the handler that
    rmtree's the staged version directory: the job/KB rows are already
    durable by then, so wiping the files would leave a permanently `queued`
    job pointing at a deleted directory (and a client polling it forever).
    A submit failure resolves the job to `failed` instead."""
    from ui.backend import ingestion as backend_ingestion

    def _boom(*args, **kwargs):
        raise RuntimeError("executor is shut down")

    monkeypatch.setattr(backend_ingestion._executor, "submit", _boom)

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 503

    with open_test_db() as db:
        job = db.query(IngestionJob).one()
        assert job.status == "failed"
        assert job.error
        assert job.completed_at is not None


def test_replace_keeps_previous_generation_live_until_the_new_job_completes(client, tmp_path, monkeypatch):
    """Spec's Testing section: "the previous version's chunks stay queryable
    until the new job completes", and "a queued/running/failed job is
    invisible to queries".

    Re-uploading also *changes the KB's type* here (Standard -> Enhanced),
    which is where this used to break: `upload_knowledge_base` advances
    `KnowledgeBaseRecord.config` to `hybrid` at dispatch time, while the
    live content is still the first job's `local_folder` chunks (whose
    `embedding_json` is NULL). Reading the type from `config` sent the read
    path down the vector branch and raised a raw `TypeError` from
    `json.loads(None)` -- transient during any re-upload, and permanent if
    the new job then failed. The shape now comes from the serving job's own
    `kb_type`/`embedding_model`."""
    from ui.backend import ingestion as backend_ingestion
    from ui.backend.db.model_catalog import seed_default_catalog

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")
    with open_test_db() as db:
        seed_default_catalog(db)

    # Hold the second job at "queued" by intercepting the dispatch, so the
    # window this test is about stays open for as long as we need it.
    submitted = []
    monkeypatch.setattr(
        backend_ingestion._executor, "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )

    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"replace": "true", "smart_search": "true"},
        files=_files(name="v2.txt", content=b"The refund policy allows returns within 90 days."),
    )
    assert resp.status_code == 200
    new_job_id = resp.json()["job_id"]
    assert len(submitted) == 1

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        record = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one()
        assert record.config["type"] == "hybrid"  # config has already advanced
        assert db.get(IngestionJob, new_job_id).status == "queued"

        # ...but the KB still serves the first (local_folder) generation.
        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        answer = tools["policies"]("refund policy")
        assert "30 days" in answer
        assert "90 days" not in answer

    # Let the queued job actually run, then the new generation takes over.
    args, kwargs = submitted[0]
    args[0](*args[1:], **kwargs)
    assert _wait_for_job_status(new_job_id) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        answer = tools["policies"]("refund policy")
        assert "90 days" in answer
        assert "30 days" not in answer


def test_kb_with_no_ingestion_job_falls_back_to_legacy_file_path(client, tmp_path, monkeypatch):
    """Simulates a pre-existing KB from before this feature: a
    KnowledgeBaseRecord whose config points at a real on-disk folder, with
    zero IngestionJob rows."""
    legacy_dir = tmp_path / "legacy_kb"
    legacy_dir.mkdir()
    # BM25 keyword search requires literal term overlap with the query
    # ("refund policy") -- no stemming, so this must share those exact words,
    # not just paraphrase them ("Refunds" alone wouldn't match "refund").
    (legacy_dir / "doc.txt").write_text("Refund policy: returns accepted within 14 days.", encoding="utf-8")

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
