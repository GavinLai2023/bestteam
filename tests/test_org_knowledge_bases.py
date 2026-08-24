"""Tests for the org self-service knowledge-base upload endpoint
(`/api/org/knowledge-bases/{name}/upload`) -- the wizard's "Your documents"
step. Mirrors `test_org_settings.py`'s auth/org-scoping patterns and reuses
`test_crud_api.py`'s upload-endpoint assertions for the shared
`knowledge_bases.upload_knowledge_base()` implementation."""

import io
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, make_concurrent_safe_engine, open_test_db
from ui.backend import knowledge_bases as backend_knowledge_bases
from ui.backend import main as backend_main
from ui.backend import org_knowledge_bases as backend_org_kb
from ui.backend.builder import _all_knowledge_base_tools, _with_knowledge_base_catalog
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import (
    IngestionJob,
    KnowledgeBaseRecord,
    KnowledgeChunk,
    KnowledgeDocument,
)
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db


def _wait_for_job_status(job_id, deadline_seconds=10):
    """Poll `IngestionJob.status` to a terminal state (completed/failed),
    opening a fresh session each time so we see the worker thread's commits.

    The deadline is a hang detector, not a timing assumption: these uploads
    are a handful of bytes, so a healthy worker resolves the job in
    milliseconds and the bound is never approached. Blowing it therefore
    means the job never resolved at all, which is reported as its own
    failure naming the job and its last-seen status -- returning that
    non-terminal status instead would surface as a bare
    `assert 'queued' == 'completed'` that says nothing about what was
    being waited for.
    """
    deadline = time.monotonic() + deadline_seconds
    status = None
    while time.monotonic() < deadline:
        with open_test_db() as db:
            job = db.get(IngestionJob, job_id)
            status = None if job is None else job.status
            if status in ("completed", "failed"):
                return status
        time.sleep(0.05)
    raise AssertionError(
        f"ingestion job {job_id} never reached a terminal status within "
        f"{deadline_seconds}s (last seen: {status!r})"
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(backend_knowledge_bases, "_KB_UPLOADS_DIR", tmp_path / "knowledge_base_uploads")
    backend_main._pipeline_cache.clear()

    # A file database, not `:memory:`: every upload here dispatches an
    # ingestion job onto `ingestion.py`'s executor, and that worker thread
    # opens its own `Session` on this same engine while the request that
    # dispatched it -- and the job-status polling below -- are still using it.
    # `make_engine(":memory:")` backs every Session with ONE `StaticPool`
    # connection, so those Sessions share a single transaction and a single
    # sqlite3 cursor; see `helpers.make_concurrent_safe_engine` for why that
    # is a harness artefact rather than production behaviour.
    engine = make_concurrent_safe_engine(tmp_path)
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
        data={"mode": "replace"},
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
        data={"mode": "replace"},
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
        data={"mode": "replace"},
        files=_files(name="v2.txt", content=b"Second generation content."),
    )
    assert resp.status_code == 200
    in_flight_job_id = resp.json()["job_id"]

    # A third upload while the second is still queued must be refused, not
    # queue a third round of work on top of it.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"mode": "replace"},
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
        data={"mode": "replace"},
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
        data={"mode": "replace", "smart_search": "true"},
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
    # BM25 keyword search requires term overlap with the query
    # ("refund policy"), so this shares those words rather than paraphrasing
    # them.
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


# --- P0-2: org-side self-service listing, inspection and deletion -----------

def test_list_own_kbs_shows_latest_job_status_and_never_config_path(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.get("/api/org/knowledge-bases")
    assert resp.status_code == 200
    body = resp.json()
    assert [kb["name"] for kb in body] == ["policies"]
    kb = body[0]
    assert kb["type"] == "local_folder"
    assert kb["servable"] is True
    assert kb["used_by"] == []
    # `iso_utc`, not the bare column: SQLite round-trips these tz-naive, and a
    # timestamp with no UTC marker is parsed as local time by the panel.
    assert kb["updated_at"].endswith("+00:00")
    assert kb["latest_job"]["status"] == "completed"
    # `job_status_payload` includes the KB's `config` once a job completes,
    # and that config carries the server's absolute upload path. The summary
    # strips it: this list is customer-facing.
    assert "config" not in kb["latest_job"]
    assert "knowledge_base_uploads" not in resp.text

    # Single-item fetch reports the same shape.
    resp = client.get("/api/org/knowledge-bases/policies")
    assert resp.status_code == 200
    assert resp.json()["name"] == "policies"
    assert "config" not in resp.json()["latest_job"]


def test_get_own_kb_404_for_other_org(client):
    assert client.post(
        "/api/org/knowledge-bases/policies/upload", files=_files()
    ).status_code == 200

    other = create_user_and_login(client, username="bob", org="org_b")
    bob = {"Authorization": f"Bearer {other}"}
    assert client.get("/api/org/knowledge-bases/policies", headers=bob).status_code == 404
    assert client.delete("/api/org/knowledge-bases/policies", headers=bob).status_code == 404
    # Still there for its owner.
    assert client.get("/api/org/knowledge-bases/policies").status_code == 200


def test_delete_own_kb_removes_rows_and_files(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
    upload_dir = backend_knowledge_bases._KB_UPLOADS_DIR / str(org_id) / "policies"
    assert upload_dir.is_dir()

    assert client.delete("/api/org/knowledge-bases/policies").status_code == 204

    assert not upload_dir.exists()
    with open_test_db() as db:
        assert db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one_or_none() is None
        assert db.query(IngestionJob).count() == 0
    assert client.get("/api/org/knowledge-bases/policies").status_code == 404


def test_delete_own_kb_409_when_used_by_deployed_team(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    # The delete guard reads typed dependency rows, which only exist once a
    # pipeline is deployed for real -- so deploy through the admin CRUD route
    # rather than inserting a PipelineRecord directly.
    admin = create_user_and_login(client, username="op", org=None, admin=True)
    deploy = client.put(
        "/api/config/pipelines/kb_team?org=default",
        json={
            "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi", "tools": ["policies"]}],
            "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
            "pipeline": {"steps": ["team"]},
        },
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert deploy.status_code == 200

    resp = client.delete("/api/org/knowledge-bases/policies")
    assert resp.status_code == 409
    assert "kb_team" in resp.json()["detail"]
    assert client.get("/api/org/knowledge-bases/policies").status_code == 200


def test_delete_own_kb_409_while_processing(client, monkeypatch):
    from ui.backend import ingestion as backend_ingestion

    submitted = []
    monkeypatch.setattr(
        backend_ingestion._executor, "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    resp = client.delete("/api/org/knowledge-bases/policies")
    assert resp.status_code == 409
    assert "still processing" in resp.json()["detail"]

    # Once the held job resolves, the same delete succeeds.
    args, kwargs = submitted[0]
    args[0](*args[1:], **kwargs)
    assert _wait_for_job_status(job_id) == "completed"
    assert client.delete("/api/org/knowledge-bases/policies").status_code == 204


def test_resolve_failed_kb_reports_the_job_error_not_wait_message(client, tmp_path):
    """A KB whose only ingestion attempt failed is stuck forever, so the
    "wait for the current upload" wording was permanently wrong -- it told a
    customer to wait for something that will never finish instead of naming
    what went wrong and what to do about it."""
    from bestteam.exceptions import ConfigurationError

    resp = client.post(
        "/api/org/knowledge-bases/broken/upload",
        files=_files(name="blank.txt", content=b"   \n  "),
    )
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "failed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        record = db.query(KnowledgeBaseRecord).filter_by(name="broken", org_id=org_id).one()
        with pytest.raises(ConfigurationError) as excinfo:
            backend_knowledge_bases.resolve_knowledge_base(db, record, tmp_path)

    message = str(excinfo.value)
    assert "could not be indexed" in message
    # A whitespace-only document has no extractable text, which is the reason
    # P0-6 reports (it used to be the vaguer "produced no chunks").
    assert "No text could be extracted" in message
    assert "Wait for the current upload" not in message
    assert ".." not in message

    # And the customer-facing summary says the same thing, without a config.
    body = client.get("/api/org/knowledge-bases/broken").json()
    assert body["servable"] is False
    assert body["latest_job"]["status"] == "failed"
    assert body["latest_job"]["errors"][0]["error"]


def test_failed_kb_does_not_block_spec_generation(client, tmp_path):
    """One customer's unparseable upload used to make `_all_knowledge_base_tools`
    raise, 4xx-ing spec generation for the whole org."""
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"
    resp = client.post(
        "/api/org/knowledge-bases/broken/upload",
        files=_files(name="blank.txt", content=b"   \n  "),
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "failed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert set(tools) == {"policies"}

        # The architect is only told about the knowledge bases that actually
        # built, so it can't reference one no agent could ever use.
        catalog_text = _with_knowledge_base_catalog(db, "", org_id, names=set(tools))
        assert "policies" in catalog_text
        assert "broken" not in catalog_text


def test_upload_description_lands_in_config_and_tool_docstring(client, tmp_path):
    """The one sentence the wizard asks for is what the agent's tool
    description says -- it is the only thing telling a model when this
    collection is the right one to search."""
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"description": "Our refund and shipping policies"},
        files=_files(),
    )
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        config = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one().config
        assert config["description"] == "Our refund and shipping policies"

        tools = _all_knowledge_base_tools(db, tmp_path, org_id)
        assert "Search the 'policies' knowledge base: Our refund and shipping policies." in (
            tools["policies"].__doc__
        )

    # And the customer's own "My documents" panel shows it back.
    resp = client.get("/api/org/knowledge-bases/policies")
    assert resp.status_code == 200
    assert resp.json()["description"] == "Our refund and shipping policies"


# --- P1-3: shape inheritance, and reporting the shape that actually serves ---

def _upload_file(name="doc.txt", content=b"The refund policy allows returns within 30 days."):
    """One `UploadFile` for calling `upload_knowledge_base()` directly.

    The admin route is the caller that names no shape, and it isn't reachable
    from this file's org-scoped client -- so the inheritance branch is driven
    through the shared function itself here, and end to end over the admin
    route in `test_crud_api.py`.
    """
    return fastapi.UploadFile(io.BytesIO(content), filename=name)


def test_summary_type_reports_the_live_generation_not_the_pending_config(client, monkeypatch):
    """`config` advances to the new shape the moment a re-upload is
    dispatched, and stays there forever if that job fails -- so reading the
    customer's panel off `config` told them a failed Enhanced upgrade had
    taken effect while every search still ran against the old Standard
    generation."""
    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    # Upgrade to Enhanced with a document that has no extractable text, so
    # the new generation never becomes servable.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"mode": "replace", "smart_search": "true"},
        files=_files(name="blank.txt", content=b"   \n  "),
    )
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "failed"

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        record = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one()
        # The config records the intent -- what the next upload would use.
        assert record.config["type"] == "hybrid"

    body = client.get("/api/org/knowledge-bases/policies").json()
    # ...but the panel reports what a search runs against today.
    assert body["type"] == "local_folder"
    assert body["servable"] is True
    assert body["latest_job"]["status"] == "failed"


def test_replace_conflict_names_the_current_search_quality(client, monkeypatch):
    """The 409 is the only moment the wizard can tell a customer what the
    collection they are about to replace is like today, so it names it."""
    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"
    resp = client.post(
        "/api/org/knowledge-bases/handbook/upload",
        data={"smart_search": "true"},
        files=_files(),
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files(name="other.txt"))
    assert resp.status_code == 409
    assert "currently uses Standard search" in resp.json()["detail"]

    resp = client.post("/api/org/knowledge-bases/handbook/upload", files=_files(name="other.txt"))
    assert resp.status_code == 409
    assert "currently uses Enhanced search" in resp.json()["detail"]


def test_upload_without_a_shape_inherits_the_existing_configuration_and_description(client, monkeypatch):
    """A caller naming no `kb_type` is replacing a collection's documents,
    not redesigning it: the shape group and the description carry over from
    what the last upload asked for."""
    from ui.backend import ingestion as backend_ingestion

    # Nothing here needs the documents indexed -- the assertions are about
    # the record and the job row the upload writes.
    monkeypatch.setattr(backend_ingestion._executor, "submit", lambda *args, **kwargs: None)

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        backend_knowledge_bases.upload_knowledge_base(
            db,
            org_id,
            "policies",
            [_upload_file()],
            chunk_size=500,
            kb_type="hybrid",
            description="Our refund and shipping policies",
            embedding_model="fake:16",
            rerank_model="fake:",
            query_expansion_model="fake:expansion",
        )

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        result = backend_knowledge_bases.upload_knowledge_base(
            db, org_id, "policies", [_upload_file(name="v2.txt")]
        )

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        config = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one().config
        assert config["type"] == "hybrid"
        assert config["embedding_model"] == "fake:16"
        assert config["rerank_model"] == "fake:"
        assert config["query_expansion_model"] == "fake:expansion"
        assert config["description"] == "Our refund and shipping policies"
        # Chunking and top_k are per-upload knobs every route always sends,
        # so they take this call's values rather than the previous upload's.
        assert config["chunk_size"] == 1000
        # And the job that will do the indexing agrees with the record.
        job = db.get(IngestionJob, result["job_id"])
        assert job.kb_type == "hybrid"
        assert job.embedding_model == "fake:16"


def test_upload_without_a_shape_on_a_new_name_is_a_standard_collection(client, monkeypatch):
    """There is nothing to inherit from, so the historical default stands."""
    from ui.backend import ingestion as backend_ingestion

    monkeypatch.setattr(backend_ingestion._executor, "submit", lambda *args, **kwargs: None)

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        backend_knowledge_bases.upload_knowledge_base(db, org_id, "policies", [_upload_file()])

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        config = db.query(KnowledgeBaseRecord).filter_by(name="policies", org_id=org_id).one().config
        assert config["type"] == "local_folder"
        assert config.get("description") is None


# --- P1-4: the "Try a search" endpoint -------------------------------------

def test_search_returns_citations_and_capped_text(client):
    """The panel's whole point is showing a customer the passages an agent
    would retrieve, each labelled with the citation the agent sees -- and
    only enough of each to judge it by."""
    # One chunk far longer than the response cap, so the truncation is
    # exercised rather than assumed: this surface shows a passage, it is not
    # a document reader.
    long_text = "The refund policy allows returns within 30 days. " * 60
    assert len(long_text) > 1500

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        result = backend_knowledge_bases.upload_knowledge_base(
            db,
            org_id,
            "policies",
            [_upload_file(content=long_text.encode())],
            chunk_size=4000,
            chunk_overlap=0,
        )
    assert _wait_for_job_status(result["job_id"]) == "completed"

    resp = client.post(
        "/api/org/knowledge-bases/policies/search",
        json={"query": "refund policy", "top_k": 3},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "refund policy"
    assert body["hit_count"] == 1
    assert len(body["results"]) == 1
    hit = body["results"][0]
    assert hit["source"] == "doc.txt"
    assert hit["citation"] == "doc.txt"
    assert hit["page"] is None
    assert hit["heading"] is None
    assert len(hit["text"]) == 1500
    assert hit["text"].startswith("The refund policy allows returns within 30 days.")


def test_search_404_for_other_org(client):
    assert client.post(
        "/api/org/knowledge-bases/policies/upload", files=_files()
    ).status_code == 200

    other = create_user_and_login(client, username="bob", org="org_b")
    resp = client.post(
        "/api/org/knowledge-bases/policies/search",
        json={"query": "refund policy"},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


def test_search_409_while_processing_and_after_a_failed_upload(client, monkeypatch):
    """Neither state can answer a query, and both are the customer's own to
    resolve -- so each says which one it is instead of a bare 500."""
    from ui.backend import ingestion as backend_ingestion

    submitted = []
    monkeypatch.setattr(
        backend_ingestion._executor,
        "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )
    assert client.post(
        "/api/org/knowledge-bases/policies/upload", files=_files()
    ).status_code == 200

    resp = client.post("/api/org/knowledge-bases/policies/search", json={"query": "refund"})
    assert resp.status_code == 409
    assert "no completed ingestion yet" in resp.json()["detail"]

    # Let that held job run, then fail a second upload outright.
    args, kwargs = submitted[0]
    args[0](*args[1:], **kwargs)
    monkeypatch.undo()

    resp = client.post(
        "/api/org/knowledge-bases/broken/upload",
        files=_files(name="blank.txt", content=b"   \n  "),
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "failed"

    resp = client.post("/api/org/knowledge-bases/broken/search", json={"query": "refund"})
    assert resp.status_code == 409
    assert "could not be indexed" in resp.json()["detail"]


def test_search_409_for_a_legacy_file_backed_kb(client, tmp_path):
    """A knowledge base with no ingestion job at all is served from a folder
    on disk. Rebuilding it would re-parse every file (and, for a `vector`
    one, re-embed it unmetered) on every click, so this surface refuses it
    rather than offering a search that silently spends."""
    folder = tmp_path / "legacy_docs"
    folder.mkdir()
    (folder / "doc.txt").write_text("The refund policy allows returns within 30 days.")

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        db.add(
            KnowledgeBaseRecord(
                name="legacy",
                org_id=org_id,
                config={"name": "legacy", "type": "local_folder", "path": str(folder)},
            )
        )
        db.commit()

    resp = client.post("/api/org/knowledge-bases/legacy/search", json={"query": "refund"})
    assert resp.status_code == 409
    assert "was not uploaded through the app" in resp.json()["detail"]


def test_resolve_without_source_refuses_the_legacy_fallback_with_not_ready(client, tmp_path):
    """The same refusal at the function boundary: an omitted `source` is what
    turns the legacy disk fallback off. Both existing callers still pass a
    path, so their behaviour is unchanged."""
    folder = tmp_path / "legacy_docs"
    folder.mkdir()
    (folder / "doc.txt").write_text("The refund policy allows returns within 30 days.")

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        record = KnowledgeBaseRecord(
            name="legacy",
            org_id=org_id,
            config={"name": "legacy", "type": "local_folder", "path": str(folder)},
        )
        db.add(record)
        db.commit()

        with pytest.raises(backend_knowledge_bases.KnowledgeBaseNotReady):
            backend_knowledge_bases.resolve_knowledge_base(db, record)

        # With a source, the disk fallback still builds exactly as before.
        kb = backend_knowledge_bases.resolve_knowledge_base(db, record, tmp_path)
        assert "30 days" in kb.query("refund policy")


def test_search_records_kb_search_usage_for_a_hybrid_kb(client, monkeypatch):
    """A test search spends real money on a hybrid collection (it embeds the
    query), so it lands in the same ledger the org's monthly cap sums over --
    with both foreign keys null, because it belongs to no run and no upload."""
    from langchain_core.embeddings import DeterministicFakeEmbedding

    from bestteam.core import hybrid_knowledge_base
    from ui.backend import ingestion as backend_ingestion
    from ui.backend.db.models import UsageRecord

    # A *billable* spec (not `fake:`) resolved to a $0 deterministic model:
    # `billable_spec` keys off the string, which is what decides whether
    # anything is metered at all, so this exercises the real path for free.
    monkeypatch.setattr(
        backend_ingestion, "resolve_embedding_model", lambda spec: DeterministicFakeEmbedding(size=8)
    )
    monkeypatch.setattr(
        hybrid_knowledge_base, "resolve_embedding_model", lambda spec: DeterministicFakeEmbedding(size=8)
    )

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        result = backend_knowledge_bases.upload_knowledge_base(
            db,
            org_id,
            "policies",
            [_upload_file()],
            kb_type="hybrid",
            embedding_model="openai:text-embedding-3-small",
        )
    assert _wait_for_job_status(result["job_id"]) == "completed"

    resp = client.post("/api/org/knowledge-bases/policies/search", json={"query": "refund policy"})
    assert resp.status_code == 200, resp.text

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        rows = db.query(UsageRecord).filter_by(agent="kb:search").all()
        assert len(rows) == 1
        row = rows[0]
        assert row.run_id is None
        assert row.ingestion_job_id is None
        assert row.org_id == org_id
        assert row.model == "openai:text-embedding-3-small"
        assert row.input_tokens > 0

        # The ingestion spend is still its own, separately attributed row.
        ingest = db.query(UsageRecord).filter_by(agent="kb:ingest").all()
        assert len(ingest) == 1
        assert ingest[0].ingestion_job_id == result["job_id"]


def test_search_502s_and_still_meters_what_the_failed_search_spent(client, monkeypatch):
    """A query expansion is paid for *before* the embedding call that raises,
    so the money is gone whether or not the search returns anything. The 502
    tells the customer what to do about it; the ledger row still says what it
    cost."""
    from bestteam.core.tool_context import add_usage
    from ui.backend.db.models import UsageRecord

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    class _SpendsThenFails:
        def search_hits(self, query, top_k):
            add_usage({
                "model": "openai:gpt-4o-mini",
                "input_tokens": 12,
                "output_tokens": 30,
            })
            raise RuntimeError("the provider hung up mid-search")

    monkeypatch.setattr(
        backend_org_kb, "resolve_knowledge_base", lambda *a, **k: _SpendsThenFails()
    )

    resp = client.post("/api/org/knowledge-bases/policies/search", json={"query": "refund"})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail.startswith("The search could not be run:")
    # The provider's own words never reach the customer.
    assert "hung up" not in detail

    with open_test_db() as db:
        rows = db.query(UsageRecord).filter_by(agent="kb:search").all()
        assert len(rows) == 1
        assert rows[0].model == "openai:gpt-4o-mini"
        assert rows[0].input_tokens == 12
        assert rows[0].run_id is None
        assert rows[0].ingestion_job_id is None


def test_search_rejects_empty_query_and_top_k_out_of_range(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    for body in (
        {"query": ""},
        # Whitespace-only clears `min_length` but retrieves nothing, and on a
        # `vector`/`hybrid` collection it would still cost a query embedding.
        {"query": "   "},
        {"query": "x" * 501},
        {"query": "refund", "top_k": 0},
        {"query": "refund", "top_k": 11},
    ):
        resp = client.post("/api/org/knowledge-bases/policies/search", json=body)
        assert resp.status_code == 422, body


def test_search_500s_not_409s_on_a_non_readiness_configuration_error(client, monkeypatch):
    """Only a not-ready knowledge base is the customer's own conflict to
    resolve. A missing `rank-bm25` extra or a bad `rerank_model` is an
    operator's deployment problem, so it stays a logged 500 rather than a
    409 telling the customer to wait for something that has already
    finished."""
    from bestteam.exceptions import ConfigurationError

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_files())
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    def boom(*args, **kwargs):
        raise ConfigurationError("the optional 'rank-bm25' package is not installed")

    monkeypatch.setattr(backend_knowledge_bases, "_build_knowledge_base_from_job", boom)

    quiet = TestClient(backend_main.app, raise_server_exceptions=False)
    quiet.headers["Authorization"] = client.headers["Authorization"]
    resp = quiet.post("/api/org/knowledge-bases/policies/search", json={"query": "refund"})
    assert resp.status_code == 500
    # The generic handler's body, not the operator's own configuration detail.
    assert "rank-bm25" not in resp.text


# --- Adding documents to a collection instead of replacing it --------------
#
# Every upload used to replace a collection wholesale, and a self-service
# upload is capped at 10 files -- so a collection could never hold more than
# ten documents, and adding one meant re-uploading (and paying to re-embed)
# the other nine.


def _named_files(*names, content=b"The refund policy allows returns within 30 days."):
    return [("files", (name, io.BytesIO(content), "text/plain")) for name in names]


def test_adding_documents_keeps_the_ones_already_there(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("a.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("b.txt"), data={"mode": "add"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        docs = {
            d.filename
            for d in db.query(KnowledgeDocument).filter_by(ingestion_job_id=job_id)
        }
    assert docs == {"a.txt", "b.txt"}


def test_replacing_still_drops_the_ones_already_there(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("a.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("b.txt"), data={"mode": "replace"})
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        docs = {
            d.filename
            for d in db.query(KnowledgeDocument).filter_by(ingestion_job_id=job_id)
        }
    assert docs == {"b.txt"}


def test_adding_a_document_that_is_already_there_replaces_that_one(client):
    # Same name, new content: the upload wins for that filename, and nothing
    # else in the collection is touched. Two documents of the same name in
    # one collection would be two answers to the same question.
    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("a.txt", "b.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=[("files", ("a.txt", io.BytesIO(b"Refunds now take 60 days."), "text/plain"))],
        data={"mode": "add"},
    )
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        docs = {
            d.filename: d
            for d in db.query(KnowledgeDocument).filter_by(ingestion_job_id=job_id)
        }
        assert set(docs) == {"a.txt", "b.txt"}
        chunks = db.query(KnowledgeChunk).filter_by(document_id=docs["a.txt"].id).all()
    assert "60 days" in chunks[0].text


def test_adding_a_document_whose_name_differs_only_in_case_replaces_it(client):
    # Windows and macOS filesystems are case-insensitive, so the carried
    # `Policy.txt` and the newly uploaded `policy.txt` are one path there: the
    # carry copied over the upload and the collection silently kept the old
    # text. "The same name" has to mean what the filesystem means by it.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=[("files", ("Policy.txt", io.BytesIO(b"Refunds take 30 days."), "text/plain"))],
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=[("files", ("policy.txt", io.BytesIO(b"Refunds now take 60 days."), "text/plain"))],
        data={"mode": "add"},
    )
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    with open_test_db() as db:
        docs = {
            d.filename: d
            for d in db.query(KnowledgeDocument).filter_by(ingestion_job_id=job_id)
        }
        assert set(docs) == {"policy.txt"}
        chunks = db.query(KnowledgeChunk).filter_by(document_id=docs["policy.txt"].id).all()
    assert "60 days" in chunks[0].text


def test_adding_to_a_collection_that_does_not_exist_yet_just_creates_it(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("a.txt"), data={"mode": "add"})
    assert resp.status_code == 200
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"


def test_an_unconfirmed_upload_to_an_existing_name_is_still_refused(client):
    # The 409 exists so a customer typing a common label cannot silently
    # change a collection another deployed team is already using. Adding to
    # it is still a change to it, so it is still a decision they have to make.
    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("a.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("b.txt"))
    assert resp.status_code == 409


def test_a_collection_cannot_grow_past_its_document_cap(client, monkeypatch):
    monkeypatch.setattr(backend_org_kb, "_MAX_DOCUMENTS_PER_KB", 2)
    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("a.txt", "b.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("c.txt"), data={"mode": "add"})
    assert resp.status_code == 413
    assert "2" in resp.json()["detail"]


def test_an_unknown_mode_is_refused(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload",
                       files=_named_files("a.txt"), data={"mode": "merge"})
    assert resp.status_code == 400


# --- Removing one document from a collection -------------------------------
#
# Until this existed the only way to drop one document was `mode=replace`
# with "the whole set you want to keep" re-uploaded. A removal is a new
# generation built from the live one minus the named file, so every
# invariant built on "one completed job is the live set" holds, and every
# remaining document reuses its chunks and embeddings.


def _live_documents(job_id):
    with open_test_db() as db:
        return {
            d.filename: d
            for d in db.query(KnowledgeDocument).filter_by(ingestion_job_id=job_id)
        }


def test_removing_a_document_leaves_the_rest_and_re_embeds_nothing(client, monkeypatch):
    from ui.backend import ingestion as backend_ingestion
    from ui.backend.db.model_catalog import seed_default_catalog

    monkeypatch.setenv("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "fake:16")
    with open_test_db() as db:
        seed_default_catalog(db)
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        data={"smart_search": "true"},
        files=[
            ("files", ("refunds.txt", b"The refund policy allows returns within 30 days.", "text/plain")),
            ("files", ("shipping.txt", b"Shipping is free on orders over fifty dollars.", "text/plain")),
        ],
    )
    assert resp.status_code == 200
    first_job = resp.json()["job_id"]
    assert _wait_for_job_status(first_job) == "completed"

    embed_calls = []
    original = backend_ingestion.embed_documents_in_batches

    def counting(embeddings, texts):
        embed_calls.append(list(texts))
        return original(embeddings, texts)

    monkeypatch.setattr(backend_ingestion, "embed_documents_in_batches", counting)

    resp = client.delete("/api/org/knowledge-bases/policies/documents/shipping.txt")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["name"] == "policies" and body["status"] == "queued"
    job_id = body["job_id"]
    assert job_id != first_job
    assert _wait_for_job_status(job_id) == "completed"

    docs = _live_documents(job_id)
    assert set(docs) == {"refunds.txt"}
    # The surviving document's chunks and vectors were carried, not re-made.
    assert embed_calls == []
    with open_test_db() as db:
        chunks = db.query(KnowledgeChunk).filter_by(document_id=docs["refunds.txt"].id).all()
    assert chunks and all(c.embedding_json for c in chunks)

    # The collection now answers from the survivor only.
    resp = client.post("/api/org/knowledge-bases/policies/search", json={"query": "shipping orders"})
    assert resp.status_code == 200
    assert all("Shipping is free" not in r["text"] for r in resp.json()["results"])
    resp = client.post("/api/org/knowledge-bases/policies/search", json={"query": "refund policy"})
    assert resp.json()["hit_count"] == 1

    # And the panel's summary says so.
    summary = client.get("/api/org/knowledge-bases/policies").json()
    assert [d["filename"] for d in summary["documents"]] == ["refunds.txt"]
    assert summary["latest_job"]["status"] == "completed"


def test_summary_lists_the_live_documents_with_their_status(client):
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=[
            ("files", ("b.txt", b"Refunds within 30 days.", "text/plain")),
            ("files", ("a.txt", b"Shipping is free.", "text/plain")),
            ("files", ("blank.txt", b"   \n  ", "text/plain")),
        ],
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    summary = client.get("/api/org/knowledge-bases/policies").json()
    docs = summary["documents"]
    # Sorted by name, and a document that could not be read is listed too --
    # it is in the collection's files, so it can be removed like any other.
    assert [d["filename"] for d in docs] == ["a.txt", "b.txt", "blank.txt"]
    assert {d["filename"]: d["status"] for d in docs} == {
        "a.txt": "chunked", "b.txt": "chunked", "blank.txt": "failed",
    }
    assert all(isinstance(d["size_bytes"], int) for d in docs)
    # The list endpoint carries the same field.
    listed = {kb["name"]: kb for kb in client.get("/api/org/knowledge-bases").json()}
    assert [d["filename"] for d in listed["policies"]["documents"]] == ["a.txt", "b.txt", "blank.txt"]


def test_summary_documents_is_empty_before_any_upload_completes(client, monkeypatch):
    from ui.backend import ingestion as backend_ingestion

    monkeypatch.setattr(backend_ingestion._executor, "submit", lambda *a, **k: None)
    assert client.post("/api/org/knowledge-bases/policies/upload", files=_files()).status_code == 200
    assert client.get("/api/org/knowledge-bases/policies").json()["documents"] == []


def test_removing_a_failed_document_works_too(client):
    # An unreadable file is carried by every `add` and re-reported as skipped
    # each time; removing it is the way to stop that.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=[
            ("files", ("a.txt", b"Refunds within 30 days.", "text/plain")),
            ("files", ("blank.txt", b"   \n  ", "text/plain")),
        ],
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"
    resp = client.delete("/api/org/knowledge-bases/policies/documents/blank.txt")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"
    assert set(_live_documents(job_id)) == {"a.txt"}
    assert client.get("/api/org/knowledge-bases/policies").json()["latest_job"]["documents_failed"] == 0


def test_removing_an_unknown_document_is_404_and_names_it(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_named_files("a.txt", "b.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.delete("/api/org/knowledge-bases/policies/documents/c.txt")
    assert resp.status_code == 404
    assert "c.txt" in resp.json()["detail"]
    # Nothing was dispatched: the live job is still the upload.
    summary = client.get("/api/org/knowledge-bases/policies").json()
    assert summary["latest_job"]["status"] == "completed"
    assert [d["filename"] for d in summary["documents"]] == ["a.txt", "b.txt"]


def test_removing_the_last_document_is_refused(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_named_files("only.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.delete("/api/org/knowledge-bases/policies/documents/only.txt")
    assert resp.status_code == 409
    assert "delete the collection" in resp.json()["detail"].lower()


def test_removing_the_last_readable_document_is_refused_even_if_a_failed_one_remains(client):
    # Codex review: the guard used to count every document, so removing the
    # only readable one beside an unreadable one returned 202 and queued a job
    # that could only fail -- leaving the "removed" document live.
    resp = client.post(
        "/api/org/knowledge-bases/policies/upload",
        files=[
            ("files", ("valid.txt", b"Refunds within 30 days.", "text/plain")),
            ("files", ("blank.txt", b"   \n  ", "text/plain")),
        ],
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    resp = client.delete("/api/org/knowledge-bases/policies/documents/valid.txt")
    assert resp.status_code == 409
    assert "could be read" in resp.json()["detail"]
    # Nothing was queued: the collection still answers.
    assert client.post("/api/org/knowledge-bases/policies/search", json={"query": "refunds"}).json()["hit_count"] == 1


def test_removal_staging_matches_the_name_exactly(client, tmp_path):
    """The carry's case-insensitive match is right for an upload (the
    filesystem may fold case) and wrong for a removal on a case-sensitive
    one, where `Policy.txt` and `policy.txt` can both be live: naming one
    must not drop the other (Codex review). Exercised at the staging helper,
    since a Windows checkout cannot hold both files at once."""
    from ui.backend.knowledge_bases import _stage_previous_generation

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        record = KnowledgeBaseRecord(name="casey", org_id=org_id, config={"name": "casey", "type": "local_folder", "path": str(tmp_path)})
        db.add(record)
        db.flush()
        job = IngestionJob(kb_id=record.id, org_id=org_id, version="v_live", kb_type="local_folder", status="completed", file_count=2)
        db.add(job)
        db.commit()
        (tmp_path / "v_live").mkdir()
        (tmp_path / "v_live" / "policy.txt").write_text("lower")
        (tmp_path / "v_live" / "other.txt").write_text("other")

        staged = tmp_path / "v_new"
        staged.mkdir()
        _stage_previous_generation(
            db, org_id, "casey", tmp_path, staged, superseded={"Policy.txt"}, max_documents=30, exact=True,
        )
        assert sorted(p.name for p in staged.iterdir()) == ["other.txt", "policy.txt"]

        folded = tmp_path / "v_fold"
        folded.mkdir()
        _stage_previous_generation(
            db, org_id, "casey", tmp_path, folded, superseded={"Policy.txt"}, max_documents=30,
        )
        assert sorted(p.name for p in folded.iterdir()) == ["other.txt"]


def test_removing_while_an_upload_is_processing_is_409(client, monkeypatch):
    from ui.backend import ingestion as backend_ingestion

    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_named_files("a.txt", "b.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"

    submitted = []
    real_submit = backend_ingestion._executor.submit
    monkeypatch.setattr(
        backend_ingestion._executor, "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )
    assert client.post(
        "/api/org/knowledge-bases/policies/upload", files=_named_files("c.txt"), data={"mode": "add"}
    ).status_code == 200

    resp = client.delete("/api/org/knowledge-bases/policies/documents/a.txt")
    assert resp.status_code == 409
    assert "processing" in resp.json()["detail"].lower()

    # Once it finishes, the removal goes through. (Only the submit stub is
    # lifted -- `monkeypatch.undo()` would also revert the client fixture's
    # upload directory, under which this collection's files live.)
    args, kwargs = submitted[0]
    args[0](*args[1:], **kwargs)
    monkeypatch.setattr(backend_ingestion._executor, "submit", real_submit)
    resp = client.delete("/api/org/knowledge-bases/policies/documents/a.txt")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"
    assert set(_live_documents(job_id)) == {"b.txt", "c.txt"}


def test_removing_from_a_collection_with_no_finished_upload_is_409(client):
    resp = client.post(
        "/api/org/knowledge-bases/broken/upload",
        files=_files(name="blank.txt", content=b"   \n  "),
    )
    assert _wait_for_job_status(resp.json()["job_id"]) == "failed"
    resp = client.delete("/api/org/knowledge-bases/broken/documents/blank.txt")
    assert resp.status_code == 409


def test_removing_a_document_from_another_orgs_collection_is_404(client):
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_named_files("a.txt", "b.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"
    other = create_user_and_login(client, username="bob", org="org_b")
    resp = client.delete(
        "/api/org/knowledge-bases/policies/documents/a.txt",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


def test_removing_a_document_matches_the_name_as_the_filesystem_does(client):
    # The carry skips a superseded name case-insensitively (Windows/macOS
    # would treat `Policy.txt` and `policy.txt` as one path); a removal names
    # a document the way the collection lists it, so an exact match is
    # required to say "removed", but the file leaves the staged set either way.
    resp = client.post("/api/org/knowledge-bases/policies/upload", files=_named_files("Policy.txt", "other.txt"))
    assert _wait_for_job_status(resp.json()["job_id"]) == "completed"
    assert client.delete("/api/org/knowledge-bases/policies/documents/policy.txt").status_code == 404
    resp = client.delete("/api/org/knowledge-bases/policies/documents/Policy.txt")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"
    assert set(_live_documents(job_id)) == {"other.txt"}


def test_search_reports_the_generation_and_why_each_hit_ranked(client):
    """The same identity and scores the agent's trace event carries, so a
    customer (or the operator reading the JSON) can tie a passage back to
    its chunk row and the ingestion job that produced it."""
    from ui.backend.db.models import KnowledgeChunk, KnowledgeDocument

    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        result = backend_knowledge_bases.upload_knowledge_base(
            db, org_id, "policies",
            [_upload_file(name="refunds.txt", content=b"The refund policy allows returns within 30 days."),
             _upload_file(name="hours.txt", content=b"Office hours are 9am to 5pm on weekdays.")],
        )
    job_id = result["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    resp = client.post("/api/org/knowledge-bases/policies/search", json={"query": "refund policy", "top_k": 3})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingestion_job_id"] == job_id
    (hit,) = body["results"]
    with open_test_db() as db:
        row = (
            db.query(KnowledgeChunk)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .filter(KnowledgeDocument.ingestion_job_id == job_id, KnowledgeDocument.filename == "refunds.txt")
            .one()
        )
        assert hit["chunk_id"] == row.id
        assert hit["document_id"] == row.document_id
    assert hit["fused_score"] > 0
    assert set(hit["leg_scores"]) == {"bm25"}
    assert hit["rerank_score"] is None
