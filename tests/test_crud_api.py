"""Tests for the `/api/config` CRUD API (Phase 2) -- the "advanced view" for
fine-tuning agents/teams/knowledge_bases/workflows directly."""

import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, get_org_id, make_concurrent_safe_engine, open_test_db
from ui.backend import crud as backend_crud
from ui.backend import ingestion as backend_ingestion
from ui.backend import knowledge_bases as backend_knowledge_bases
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord, SkillRecord, WorkflowRecord
from ui.backend.db_session import get_db


def _active_kb_dir(uploads: Path, name: str) -> Path:
    """Resolve an uploaded KB's most-recently-written version directory.

    Job-based uploads (Task 6) no longer maintain a CURRENT pointer: each
    upload's files land in a fresh, immutable version subdirectory (no old
    files carried over), and nothing promotes one to "active". For these
    tests' sequential, single-threaded uploads, the most-recently-modified
    version directory is unambiguously "this upload's own files".

    Uploads are org-scoped on disk (`<uploads>/<org_id>/<name>`); the fixture
    user's 'default' org is the first org created in each test DB, so id 1.
    """
    kb_root = uploads / "1" / name
    versions = [p for p in kb_root.iterdir() if p.is_dir()]
    return max(versions, key=lambda p: p.stat().st_mtime)


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


@contextmanager
def _ingestion_futures():
    """Record the `Future` of every ingestion job dispatched inside the block.

    `run_ingestion_job` commits `IngestionJob.status = "completed"` *before*
    it invalidates the workflow cache and prunes older generations -- both
    are deliberately best-effort steps that must never be able to
    retroactively un-complete a durable job (see ui/backend/ingestion.py).
    So a terminal status is the right signal for an assertion about the job
    itself, but not for one about pruning: it can (and intermittently does)
    arrive while the pruning that follows it has not run yet. A `Future`
    resolves only once the whole worker function has returned, which is the
    happens-before edge those assertions actually need.
    """
    futures = []
    submit = backend_ingestion._executor.submit

    def _recording_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        futures.append(future)
        return future

    with patch.object(backend_ingestion._executor, "submit", _recording_submit):
        yield futures


def _await_ingestion(future, deadline_seconds=10):
    """Block until one ingestion worker has fully returned.

    `run_ingestion_job` catches everything and never raises, so the only way
    to reach this bound is a genuinely wedged worker -- a hang detector, not
    a timing assumption.
    """
    try:
        future.result(timeout=deadline_seconds)
    except FutureTimeoutError:
        raise AssertionError(
            f"an ingestion worker did not finish within {deadline_seconds}s"
        ) from None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    # `_KB_UPLOADS_DIR` is defined in knowledge_bases.py (where the actual
    # upload/delete-on-disk logic lives) and merely re-imported into crud.py's
    # namespace for the DELETE handler's own read -- patch both bindings so
    # the two stay pointed at the same tmp directory during a test.
    monkeypatch.setattr(backend_knowledge_bases, "_KB_UPLOADS_DIR", tmp_path / "knowledge_base_uploads")
    monkeypatch.setattr(backend_crud, "_KB_UPLOADS_DIR", tmp_path / "knowledge_base_uploads")
    backend_main._workflow_cache.clear()

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
        test_client = TestClient(backend_main.app)
        # The Advanced/config API is admin-only; provision the fixture user as
        # a platform admin (org=None -- org members can't be admins, CR-030)
        # so the existing CRUD tests exercise the endpoints. A non-admin 403
        # is covered separately by test_config_forbidden_for_non_admin.
        token = create_user_and_login(test_client, org=None, admin=True)
        test_client.headers["Authorization"] = f"Bearer {token}"
        # Production bootstrap seeds the 'default' org; the org-less admin
        # fixture user no longer creates it as a side effect, so seed it here
        # for all the ?org=default requests below.
        get_org_id("default")
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _org_user_headers(client, username="runner"):
    # The fixture user is an org-less platform admin, which get_current_org
    # 403s on org-user surfaces (POST /api/runs) -- run as a 'default' org
    # member instead.
    token = create_user_and_login(client, username=username)
    return {"Authorization": f"Bearer {token}"}


def test_config_forbidden_for_non_admin(client):
    # Advanced config is admin-only: an authenticated non-admin user gets 403.
    token = create_user_and_login(client, username="regular", password="pw")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/config/knowledge_bases", headers=headers).status_code == 403
    assert client.put(
        "/api/config/knowledge_bases/x",
        json={"path": "./docs"},
        headers=headers,
    ).status_code == 403


def test_list_orgs(client):
    # The Advanced page's org selector is populated from here; without it the
    # admin has no way to target an org on the item routes below.
    body = client.get("/api/config/orgs").json()
    assert {o["name"] for o in body} == {"default"}
    assert "display_name" in body[0]


def test_list_orgs_forbidden_for_non_admin(client):
    token = create_user_and_login(client, username="nosy", password="pw")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/config/orgs", headers=headers).status_code == 403


def test_list_tools(client):
    # Read-only tool reference for skill authoring: tools are Python functions
    # in the registry, not config, so this lists them and nothing more.
    from bestteam.tools import REGISTRY

    body = client.get("/api/config/tools").json()
    assert {t["name"] for t in body} == set(REGISTRY)
    assert all(t["description"].strip() for t in body)


def test_list_tools_forbidden_for_non_admin(client):
    token = create_user_and_login(client, username="curious", password="pw")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/config/tools", headers=headers).status_code == 403


def test_org_query_param_is_required_on_item_routes(client):
    # Pins the contract the frontend must honor: omitting ?org= on a non-skills
    # item route is a 422, not a silent write to some default org. The Advanced
    # page regressed by not sending it.
    config = {"path": "./docs"}

    assert client.put("/api/config/knowledge_bases/kb1", json=config).status_code == 422
    assert client.put("/api/config/knowledge_bases/kb1?org=default", json=config).status_code == 200
    assert client.delete("/api/config/knowledge_bases/kb1").status_code == 422
    assert client.delete("/api/config/knowledge_bases/kb1?org=default").status_code == 204


def test_cross_org_config_access_is_404(client):
    # Explicit org targeting: an item created in org A is invisible through
    # org B's lens (404, not 403 -- existence is not revealed), and the same
    # name can exist in both orgs.
    from helpers import open_test_db
    from ui.backend.db.orgs import get_or_create_org

    with open_test_db() as db:
        get_or_create_org(db, "other")

    config = {"path": "./docs"}
    assert client.put("/api/config/knowledge_bases/dup?org=default", json=config).status_code == 200
    assert client.get("/api/config/knowledge_bases/dup?org=other").status_code == 404
    assert client.delete("/api/config/knowledge_bases/dup?org=other").status_code == 404
    assert client.put("/api/config/knowledge_bases/dup?org=other", json=config).status_code == 200

    listed = client.get("/api/config/knowledge_bases").json()
    assert sorted((item["name"], item["org"]) for item in listed) == [
        ("dup", "default"),
        ("dup", "other"),
    ]
    filtered = client.get("/api/config/knowledge_bases?org=other").json()
    assert [(item["name"], item["org"]) for item in filtered] == [("dup", "other")]


def test_config_mutations_require_org_param(client):
    config = {"path": "./docs"}
    assert client.put("/api/config/knowledge_bases/x", json=config).status_code == 422
    assert client.get("/api/config/knowledge_bases/x").status_code == 422
    assert client.delete("/api/config/knowledge_bases/x").status_code == 422
    assert client.put("/api/config/knowledge_bases/x?org=ghost", json=config).status_code == 404


def test_skills_without_org_hit_platform_tier(client):
    # Omitted ?org= on skills targets the built-in tier (org NULL); an org's
    # same-named skill lives alongside it without collision.
    skill = {"instructions": "Platform-wide playbook.", "tools": []}
    assert client.put("/api/config/skills/shared", json=skill).status_code == 200
    org_skill = {"instructions": "Org-specific playbook.", "tools": []}
    assert client.put("/api/config/skills/shared?org=default", json=org_skill).status_code == 200

    platform = client.get("/api/config/skills/shared").json()
    assert platform["org"] is None
    assert platform["config"]["instructions"] == "Platform-wide playbook."
    org_view = client.get("/api/config/skills/shared?org=default").json()
    assert org_view["org"] == "default"
    assert org_view["config"]["instructions"] == "Org-specific playbook."


def test_standalone_agents_and_teams_are_not_a_config_resource(client):
    # Nothing ever consumed AgentRecord/TeamRecord -- `_build_workflow` takes
    # only extra_tools/extra_skills, so a standalone agent or team could never
    # reach a running workflow. A workflow carries its agents/teams inline.
    for kind in ("agents", "teams"):
        assert client.get(f"/api/config/{kind}").status_code == 404
        assert client.put(f"/api/config/{kind}/x?org=default", json={}).status_code == 404


def test_knowledge_base_put_omits_vector_only_fields_for_local_folder(client):
    config = {"path": "./docs", "type": "local_folder", "embedding_model": "fake:8"}

    resp = client.put("/api/config/knowledge_bases/docs?org=default", json=config)

    assert resp.status_code == 200
    assert "embedding_model" not in resp.json()["config"]


def test_knowledge_base_put_allows_hybrid_type_with_vector_fields(client):
    config = {"path": "./docs", "type": "hybrid", "embedding_model": "fake:8"}

    resp = client.put("/api/config/knowledge_bases/docs?org=default", json=config)

    assert resp.status_code == 200
    assert resp.json()["config"]["embedding_model"] == "fake:8"


def test_knowledge_base_put_rejects_name_with_spaces(client):
    config = {"path": "./docs", "type": "local_folder"}

    resp = client.put("/api/config/knowledge_bases/bad name?org=default", json=config)

    assert resp.status_code == 400


def test_knowledge_base_put_rejects_absolute_cache_path(client):
    # CR-001: an absolute cache_path is the "server-file replacement" write
    # primitive -- on the next run the vector KB's _save_embedding_cache would
    # os.replace() this file. The API boundary must reject it.
    config = {
        "path": "./docs",
        "type": "vector",
        "embedding_model": "fake:8",
        "cache_path": "/etc/cron.d/pwned",
    }

    resp = client.put("/api/config/knowledge_bases/evil?org=default", json=config)

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_knowledge_base_put_rejects_traversal_in_cache_path(client):
    config = {
        "path": "./docs",
        "type": "vector",
        "embedding_model": "fake:8",
        "cache_path": "../../evil.json",
    }

    resp = client.put("/api/config/knowledge_bases/evil?org=default", json=config)

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_knowledge_base_put_rejects_traversal_in_path(client):
    config = {"path": "../../../../etc", "type": "local_folder"}

    resp = client.put("/api/config/knowledge_bases/evil?org=default", json=config)

    assert resp.status_code == 400
    assert "path" in resp.json()["detail"]


def test_knowledge_base_put_still_allows_absolute_local_folder_path(client, tmp_path):
    # Option A deliberately keeps the documented "point at a folder you manage
    # yourself" feature: an absolute local_folder path (no traversal) is fine.
    docs_dir = tmp_path / "client_docs"
    docs_dir.mkdir()
    config = {"path": str(docs_dir), "type": "local_folder"}

    resp = client.put("/api/config/knowledge_bases/ok?org=default", json=config)

    assert resp.status_code == 200


def test_knowledge_base_put_contains_relative_cache_path(client, tmp_path):
    # CR-001: a clean relative cache_path is accepted but rewritten into the
    # app-owned _kb_cache/ subdir, so it can only write there (never over a
    # workflow YAML). Stored via KnowledgeBaseSpec, no build triggered.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    config = {
        "path": str(docs_dir),
        "type": "vector",
        "embedding_model": "fake:8",
        "cache_path": "sub/dir/embeddings.json",
    }
    resp = client.put("/api/config/knowledge_bases/kb?org=default", json=config)

    assert resp.status_code == 200
    assert resp.json()["config"]["cache_path"] == "_kb_cache/embeddings.json"


def test_knowledge_base_put_contains_windows_rooted_cache_path(client, tmp_path):
    # CR-001: a Windows rooted-relative value (which slips the lexical absolute
    # check) is also confined to _kb_cache/ rather than escaping to a drive root.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    config = {
        "path": str(docs_dir),
        "type": "vector",
        "embedding_model": "fake:8",
        "cache_path": "\\Windows\\Temp\\pwned.json",
    }
    resp = client.put("/api/config/knowledge_bases/kb?org=default", json=config)

    assert resp.status_code == 200
    assert resp.json()["config"]["cache_path"] == "_kb_cache/pwned.json"


def test_workflow_put_rejects_inline_kb_absolute_cache_path(client, tmp_path):
    # The inline `knowledge_bases` list in a workflow config is a second API
    # boundary that accepts KB paths (it bypasses KnowledgeBaseSpec). It must
    # enforce the same containment as the standalone endpoint.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    workflow_config = {
        "knowledge_bases": [
            {
                "name": "evil_kb",
                "path": str(docs_dir),
                "type": "vector",
                "embedding_model": "fake:8",
                "cache_path": "/tmp/evil.json",
            }
        ],
        "agents": [
            {"name": "a", "role": "r", "goal": "g", "model": "fake:hi", "tools": ["evil_kb"]}
        ],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }

    resp = client.put("/api/config/workflows/evil_wf?org=default", json=workflow_config)

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_unknown_item_returns_404(client):
    assert client.get("/api/config/knowledge_bases/does-not-exist?org=default").status_code == 404
    assert client.delete("/api/config/knowledge_bases/does-not-exist?org=default").status_code == 404


def test_contain_kb_config_for_load_confines_legacy_cache_path():
    # CR-001: a record persisted before the boundary guards (or any raw config)
    # is confined at load time -- non-raising -- so it can't write outside
    # _kb_cache/. The input dict is not mutated.
    from ui.backend.knowledge_bases import contain_kb_config_for_load

    original = {"path": "/data", "type": "vector", "cache_path": "/etc/cron.d/pwned"}
    out = contain_kb_config_for_load(original)

    assert out["cache_path"] == "_kb_cache/pwned"
    assert original["cache_path"] == "/etc/cron.d/pwned"  # copy, not in-place


def test_contain_workflow_config_for_load_confines_inline_kb():
    from ui.backend.knowledge_bases import contain_workflow_config_for_load

    cfg = {"knowledge_bases": [{"name": "k", "path": "/d", "type": "vector", "cache_path": "../../evil.json"}]}
    out = contain_workflow_config_for_load(cfg)

    assert out["knowledge_bases"][0]["cache_path"] == "_kb_cache/evil.json"


def test_resolved_cache_path_must_stay_in_the_owned_cache_directory(tmp_path, monkeypatch):
    # CR-001: lexical containment alone is insufficient if _kb_cache is a
    # symlink/junction. Simulate a resolved target outside the workflow root
    # without requiring Windows symlink-creation privileges in the test runner.
    from fastapi import HTTPException
    from ui.backend import knowledge_bases

    source = tmp_path / "workflow.yaml"
    outside = tmp_path.parent / "outside" / "embeddings.json"
    original_resolve = knowledge_bases.Path.resolve

    def redirected_resolve(path, *args, **kwargs):
        if path.name == "embeddings.json":
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(knowledge_bases.Path, "resolve", redirected_resolve)

    with pytest.raises(HTTPException, match="outside"):
        knowledge_bases.ensure_contained_cache_path_for_source(
            {"cache_path": "_kb_cache/embeddings.json"}, source
        )


def test_reupload_replaces_files_and_drops_omitted_ones(client, tmp_path):
    # CR-008: re-uploading must replace the KB's files wholesale -- a file
    # present before but omitted from the new upload must not linger.
    uploads = tmp_path / "knowledge_base_uploads"
    client.post(
        "/api/config/knowledge_bases/kb/upload?org=default",
        files=[
            ("files", ("doc1.txt", b"first document content", "text/plain")),
            ("files", ("doc2.txt", b"second document content", "text/plain")),
        ],
    )
    resp = client.post(
        "/api/config/knowledge_bases/kb/upload?org=default",
        files=[("files", ("doc3.txt", b"third document content", "text/plain"))],
    )

    assert resp.status_code == 200
    # The active version resolves to only the new file; omitted ones are gone.
    assert sorted(p.name for p in _active_kb_dir(uploads, "kb").iterdir()) == ["doc3.txt"]


def test_failed_reupload_preserves_prior_kb(client, tmp_path):
    # CR-008: a failed re-upload must leave the previously-valid KB (the active
    # version) AND its DB record intact -- the CURRENT pointer never moves.
    uploads = tmp_path / "knowledge_base_uploads"
    client.post(
        "/api/config/knowledge_bases/kb/upload?org=default",
        files=[("files", ("doc1.txt", b"good original content", "text/plain"))],
    )

    from bestteam.exceptions import ConfigurationError

    with patch.object(backend_knowledge_bases, "_validate_chunk_params", side_effect=ConfigurationError("bad upload")):
        resp = client.post(
            "/api/config/knowledge_bases/kb/upload?org=default",
            files=[("files", ("doc2.txt", b"new content that fails validation", "text/plain"))],
        )

    assert resp.status_code == 400
    assert sorted(p.name for p in _active_kb_dir(uploads, "kb").iterdir()) == ["doc1.txt"]
    assert client.get("/api/config/knowledge_bases/kb?org=default").status_code == 200  # DB record preserved


def test_deleting_knowledge_base_invalidates_workflow_cache(client, tmp_path):
    # CR-005: deleting a KB must drop any cached workflow that might embed it --
    # the global max(updated_at) freshness key does not change on a delete.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("hello", encoding="utf-8")
    client.put("/api/config/knowledge_bases/kb1?org=default", json={"path": str(docs_dir), "type": "local_folder"})
    backend_main._workflow_cache["cached_wf"] = ("stale-workflow", "key")

    assert client.delete("/api/config/knowledge_bases/kb1?org=default").status_code == 204

    assert backend_main._workflow_cache == {}


def test_deleting_skill_invalidates_workflow_cache(client):
    client.put(
        "/api/config/skills/research",
        json={"description": "Research", "instructions": "Search.", "tools": []},
    )
    backend_main._workflow_cache["cached_wf"] = ("stale-workflow", "key")

    assert client.delete("/api/config/skills/research").status_code == 204

    assert backend_main._workflow_cache == {}


def test_upserting_knowledge_base_invalidates_workflow_cache(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("hello", encoding="utf-8")
    backend_main._workflow_cache["cached_wf"] = ("stale-workflow", "key")

    client.put("/api/config/knowledge_bases/kb1?org=default", json={"path": str(docs_dir), "type": "local_folder"})

    assert backend_main._workflow_cache == {}


def test_dependency_freshness_changes_when_non_latest_kb_deleted():
    # CR-005 root cause: deleting a KB whose updated_at is NOT the maximum must
    # still change the dependency fingerprint, so a cached workflow can't keep
    # serving the deleted KB even in the pre-invalidation window. A
    # max(updated_at)-only fingerprint (the old behavior) missed this.
    from datetime import datetime

    from ui.backend.db.models import KnowledgeBaseRecord

    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)
    with TestSessionLocal() as db:
        db.add_all([
            KnowledgeBaseRecord(name="older", config={}, updated_at=datetime(2020, 1, 1)),
            KnowledgeBaseRecord(name="newer", config={}, updated_at=datetime(2020, 1, 2)),
        ])
        db.commit()

        before = backend_main._dependency_freshness(db)
        db.delete(db.query(KnowledgeBaseRecord).filter_by(name="older").one())
        db.commit()  # deletes the non-maximum record -> max(updated_at) unchanged
        after = backend_main._dependency_freshness(db)

    assert before != after


def test_stale_load_does_not_repopulate_cache_after_invalidation():
    # CR-005: a load that snapshotted the generation, then had the cache
    # invalidated mid-build, must not write its now-stale result back.
    backend_main._workflow_cache.clear()
    generation = backend_main._workflow_cache_generation

    with backend_main._workflow_cache_lock:  # simulate a concurrent invalidation
        backend_main._workflow_cache_generation += 1

    backend_main._store_workflow_in_cache("wf", object(), "key", generation)

    assert "wf" not in backend_main._workflow_cache


def test_failed_commit_during_upload_preserves_prior_kb(client, tmp_path):
    # CR-008: if the DB commit fails, the filesystem is rolled back to the prior
    # KB so the live directory and DB record stay consistent.
    uploads = tmp_path / "knowledge_base_uploads"
    client.post(
        "/api/config/knowledge_bases/kb/upload?org=default",
        files=[("files", ("doc1.txt", b"good original content", "text/plain"))],
    )

    with patch("sqlalchemy.orm.Session.commit", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError, match="db down"):
            client.post(
                "/api/config/knowledge_bases/kb/upload?org=default",
                files=[("files", ("doc2.txt", b"replacement content", "text/plain"))],
            )

    # CURRENT was pointed back at the prior version; the active KB is doc1.
    assert sorted(p.name for p in _active_kb_dir(uploads, "kb").iterdir()) == ["doc1.txt"]
    assert client.get("/api/config/knowledge_bases/kb?org=default").status_code == 200  # record intact


def test_reupload_never_leaves_kb_without_a_live_version(client, tmp_path):
    # No CURRENT pointer exists for job-based uploads any more; each upload's
    # files land in their own fresh version directory, and pruning of old
    # generations now happens asynchronously as part of a completed ingestion
    # job (ui/backend/ingestion.py::_prune_old_ingestion_versions), keeping
    # the current generation plus one grace-window generation -- the same
    # "immediately-previous version survives, older ones are cleaned up"
    # invariant as the old CR-008 synchronous pointer swap, just driven by
    # job completion instead.
    #
    # Every assertion here is about what pruning left on disk, which the
    # worker does *after* committing the job's terminal status -- so each
    # upload waits on the worker's own `Future`, not on that status. See
    # `_ingestion_futures`.
    uploads = tmp_path / "knowledge_base_uploads"
    with _ingestion_futures() as futures:
        resp1 = client.post("/api/config/knowledge_bases/kb/upload?org=default", files=[("files", ("v1.txt", b"one", "text/plain"))])
        _await_ingestion(futures.pop())
        assert _wait_for_job_status(resp1.json()["job_id"]) == "completed"
        v1 = _active_kb_dir(uploads, "kb")

        resp2 = client.post("/api/config/knowledge_bases/kb/upload?org=default", files=[("files", ("v2.txt", b"two", "text/plain"))])
        _await_ingestion(futures.pop())
        assert _wait_for_job_status(resp2.json()["job_id"]) == "completed"
        v2 = _active_kb_dir(uploads, "kb")

        assert v1 != v2
        assert sorted(p.name for p in v2.iterdir()) == ["v2.txt"]  # active version
        assert v1.is_dir()  # previous version kept as a grace window

        # A third upload's completed job prunes the now-two-generations-old version.
        resp3 = client.post("/api/config/knowledge_bases/kb/upload?org=default", files=[("files", ("v3.txt", b"three", "text/plain"))])
        _await_ingestion(futures.pop())
        assert _wait_for_job_status(resp3.json()["job_id"]) == "completed"
        assert not v1.is_dir()


def test_concurrent_upload_promotion_is_serialized_per_kb(client, tmp_path):
    # CR-008 (F1): concurrent uploads of the same KB must not interleave file
    # staging + the DB commit/job dispatch. The critical section is guarded
    # by a per-KB lock; holding it must block a second upload until released,
    # after which the KB ends in a consistent state.
    import threading
    import time

    uploads = tmp_path / "knowledge_base_uploads"
    client.post(
        "/api/config/knowledge_bases/kb/upload?org=default",
        files=[("files", ("first.txt", b"first content", "text/plain"))],
    )

    lock = backend_crud._kb_upload_lock("1/kb")  # keyed by <org_id>/<name>
    lock.acquire()
    done = []

    def _upload():
        resp = client.post(
            "/api/config/knowledge_bases/kb/upload?org=default",
            files=[("files", ("second.txt", b"second content", "text/plain"))],
        )
        done.append(resp.status_code)

    worker = threading.Thread(target=_upload)
    worker.start()
    try:
        time.sleep(0.5)
        assert done == [], "the second upload must block on the per-KB lock during promotion"
    finally:
        lock.release()

    worker.join(timeout=5)
    assert done == [200]
    # The most recent version dir holds the last writer's file.
    active = _active_kb_dir(uploads, "kb")
    assert active.is_dir()
    assert sorted(p.name for p in active.iterdir()) == ["second.txt"]


def test_skill_version_publish_is_serialized_with_team_deploys(client):
    # Skill PUT and both workflow deploy paths share this lock. Holding it must
    # block publication, preventing a deploy from pairing one skill head id
    # with another content version during a concurrent admin edit.
    import threading
    import time

    backend_crud.component_mutation_lock.acquire()
    done = []

    def _save_skill():
        response = client.put(
            "/api/config/skills/concurrent",
            json={"instructions": "atomic", "tools": []},
        )
        done.append(response.status_code)

    worker = threading.Thread(target=_save_skill)
    worker.start()
    try:
        time.sleep(0.5)
        assert done == [], "skill publication must wait for the deploy/component lock"
    finally:
        backend_crud.component_mutation_lock.release()

    worker.join(timeout=5)
    assert done == [200]


def test_resolve_kb_upload_path_falls_back_for_flat_layout(tmp_path):
    # A manual-config / legacy KB with no CURRENT pointer is scanned as-is.
    from ui.backend.knowledge_bases import resolve_kb_upload_path

    flat = tmp_path / "manual_kb"
    flat.mkdir()
    config = {"path": str(flat), "type": "local_folder"}
    assert resolve_kb_upload_path(config)["path"] == str(flat)


def test_upload_creates_queryable_local_folder_kb(client):
    files = [
        ("files", ("doc1.txt", b"The refund policy allows returns within 30 days.", "text/plain")),
        ("files", ("doc2.md", b"# Shipping\nStandard shipping takes 5-7 business days.", "text/markdown")),
    ]
    resp = client.post("/api/config/knowledge_bases/support_docs/upload?org=default", files=files)

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "support_docs"
    assert body["status"] == "queued"
    job_id = body["job_id"]

    assert _wait_for_job_status(job_id) == "completed"

    from ui.backend import ingestion as backend_ingestion

    with open_test_db() as db:
        job = db.get(IngestionJob, job_id)
        payload = backend_ingestion.job_status_payload(db, job)
        assert payload["file_count"] == 2
        assert payload["chunk_count"] >= 2
        assert payload["config"]["type"] == "local_folder"
        assert "knowledge_base_uploads" in payload["config"]["path"]

    get_resp = client.get("/api/config/knowledge_bases/support_docs?org=default")
    assert get_resp.status_code == 200


def test_resolve_knowledge_base_refuses_when_no_job_has_completed(client, tmp_path):
    """A KB with an IngestionJob that isn't `completed` (still queued/running,
    or every attempt has failed) must not fall back to scanning the raw
    upload directory -- that directory recursively contains every version
    subdirectory, including the currently-staging one, so the fallback would
    serve un-vetted or unembedded content instead of treating the KB as not
    yet servable (Codex review finding)."""
    files = [("files", ("doc.txt", b"The refund policy allows returns within 30 days.", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/support_docs/upload?org=default", files=files)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    # Simulate a read landing mid-ingestion (or after a total failure) by
    # forcing the only job back to a non-completed state.
    with open_test_db() as db:
        job = db.get(IngestionJob, job_id)
        job.status = "running"
        db.commit()

    from bestteam.exceptions import ConfigurationError

    from ui.backend.knowledge_bases import resolve_knowledge_base

    with open_test_db() as db:
        record = db.query(KnowledgeBaseRecord).filter_by(name="support_docs").one()
        with pytest.raises(ConfigurationError):
            resolve_knowledge_base(db, record, tmp_path)


def test_resolve_knowledge_base_prefers_latest_submitted_job_over_latest_completed(client, tmp_path):
    """Overlapping uploads for the same KB can finish out of submission
    order. `resolve_knowledge_base` must pick the most recently *submitted*
    completed job (highest id), not the one with the latest `completed_at`
    -- ordering by `completed_at` could let an older, slower upload's job
    "win" over a newer one that already completed (Codex review finding)."""
    from datetime import datetime, timedelta, timezone

    from ui.backend.db.models import KnowledgeChunk, KnowledgeDocument
    from ui.backend.knowledge_bases import resolve_knowledge_base

    with open_test_db() as db:
        kb = KnowledgeBaseRecord(
            name="racey_kb", org_id=1,
            config={"type": "local_folder", "path": "unused", "top_k": 5},
        )
        db.add(kb)
        db.commit()

        base = datetime.now(timezone.utc)

        def _completed_job(version, completed_at, text):
            job = IngestionJob(
                kb_id=kb.id, org_id=1, version=version, status="completed",
                kb_type="local_folder", file_count=1, completed_at=completed_at,
            )
            db.add(job)
            db.commit()
            doc = KnowledgeDocument(
                kb_id=kb.id, ingestion_job_id=job.id, filename="doc.txt",
                content_hash="x", size_bytes=len(text), status="chunked",
            )
            db.add(doc)
            db.flush()
            db.add(KnowledgeChunk(kb_id=kb.id, document_id=doc.id, chunk_index=0, text=text))
            db.commit()
            return job

        # job1 is submitted first (lowest id) but -- simulating an
        # overlapping upload that took longer -- finishes LAST (latest
        # completed_at).
        _completed_job("v1", base + timedelta(seconds=100), "first generation content")
        _completed_job("v2", base + timedelta(seconds=1), "second generation content")

        resolved = resolve_knowledge_base(db, kb, tmp_path)
        # Picks job2 (the more recently *submitted* generation), not job1
        # (which merely has the latest completed_at).
        assert "second generation" in resolved.query("content")


def test_ingestion_job_status_endpoint(client):
    files = [("files", ("doc.txt", b"The refund policy allows returns within 30 days.", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/support_docs/upload?org=default", files=files)
    job_id = resp.json()["job_id"]

    resp = client.get(f"/api/config/knowledge_bases/support_docs/ingestion-jobs/{job_id}?org=default")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] in ("queued", "running", "completed")
    assert "errors" in body
    assert "chunk_count" in body


def test_ingestion_job_status_404_for_unknown_job(client):
    files = [("files", ("doc.txt", b"The refund policy allows returns within 30 days.", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/support_docs/upload?org=default", files=files)
    assert resp.status_code == 200
    resp = client.get("/api/config/knowledge_bases/support_docs/ingestion-jobs/999999?org=default")
    assert resp.status_code == 404


def test_ingestion_job_status_404_for_another_orgs_job(client):
    from helpers import open_test_db
    from ui.backend.db.orgs import get_or_create_org

    with open_test_db() as db:
        get_or_create_org(db, "other")

    files = [("files", ("doc.txt", b"The refund policy allows returns within 30 days.", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/support_docs/upload?org=default", files=files)
    job_id = resp.json()["job_id"]

    # "other" has its own same-named "support_docs" KB (with its own
    # job_id/kb_id), so the request below genuinely exercises the kb_id
    # mismatch -> 404 path rather than "no KB named 'support_docs' exists
    # for this org at all" -> 404.
    assert client.post(
        "/api/config/knowledge_bases/support_docs/upload?org=other", files=files
    ).status_code == 200

    resp = client.get(f"/api/config/knowledge_bases/support_docs/ingestion-jobs/{job_id}?org=other")
    assert resp.status_code == 404


def test_upload_rejects_name_with_spaces_before_writing_files(client):
    from ui.backend.crud import _KB_UPLOADS_DIR

    files = [("files", ("doc1.txt", b"some content here for parsing", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/bad name/upload?org=default", files=files)

    assert resp.status_code == 400
    assert not (_KB_UPLOADS_DIR / "bad name").exists()


def test_upload_rejects_too_many_files(client):
    files = [("files", (f"doc{i}.txt", b"x", "text/plain")) for i in range(31)]
    resp = client.post("/api/config/knowledge_bases/too_many/upload?org=default", files=files)
    assert resp.status_code == 413


def test_upload_rejects_oversized_file(client):
    big = b"x" * (30 * 1024 * 1024 + 1)
    files = [("files", ("big.txt", big, "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/too_big/upload?org=default", files=files)
    assert resp.status_code == 413


def test_upload_sanitizes_path_traversal_filename(client):
    from ui.backend.crud import _KB_UPLOADS_DIR

    files = [("files", ("../../evil.txt", b"some content here for parsing", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/traversal_kb/upload?org=default", files=files)

    assert resp.status_code == 200
    active = _active_kb_dir(_KB_UPLOADS_DIR, "traversal_kb")
    assert (active / "evil.txt").is_file()
    assert not (_KB_UPLOADS_DIR.parent / "evil.txt").exists()


def test_upload_rejects_filename_with_no_basename(client):
    files = [("files", ("..", b"some content here for parsing", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/dotdot_kb/upload?org=default", files=files)
    assert resp.status_code == 400


def test_upload_of_unparseable_file_fails_the_ingestion_job(client):
    # Content validation is no longer synchronous (upload_knowledge_base()
    # only validates kb_type/chunk params before dispatching) -- an
    # unparseable/unsupported file fails asynchronously inside the ingestion
    # job instead of the upload request itself, and the KB record it created
    # is NOT rolled back (the job's failure is its own diagnostic record).
    files = [("files", ("bad.exe", b"\x00\x01\x02", "application/octet-stream"))]
    resp = client.post("/api/config/knowledge_bases/bad_kb/upload?org=default", files=files)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    assert _wait_for_job_status(job_id) == "failed"

    get_resp = client.get("/api/config/knowledge_bases/bad_kb?org=default")
    assert get_resp.status_code == 200


def test_uploaded_kb_is_queryable_by_a_workflow(client):
    files = [("files", ("policy.txt", b"Refunds are processed within 5 business days of approval.", "text/plain"))]
    upload_resp = client.post("/api/config/knowledge_bases/policy_kb/upload?org=default", files=files)
    assert upload_resp.status_code == 200
    job_id = upload_resp.json()["job_id"]
    assert _wait_for_job_status(job_id) == "completed"

    get_kb_resp = client.get("/api/config/knowledge_bases/policy_kb?org=default")
    assert get_kb_resp.status_code == 200
    uploaded_path = get_kb_resp.json()["config"]["path"]

    # Standalone knowledge_bases created via /api/config aren't auto-wired into a
    # workflow's tools (see module docstring) -- a workflow only sees knowledge_bases
    # it embeds inline itself. Point the workflow's own entry at the same uploaded
    # directory to prove the uploaded content is real, indexed, and queryable.
    workflow_config = {
        "knowledge_bases": [{"name": "policy_kb", "path": uploaded_path, "type": "local_folder"}],
        "agents": [
            {
                "name": "support_agent",
                "role": "Support",
                "goal": "Answer policy questions",
                "model": "fake:Refunds take 5 business days per the policy doc.",
                "tools": ["policy_kb"],
            }
        ],
        "teams": [{"name": "team", "agents": ["support_agent"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    put_resp = client.put("/api/config/workflows/policy_test_wf?org=default", json=workflow_config)
    assert put_resp.status_code == 200

    run_resp = client.post(
        "/api/runs",
        json={"workflow": "policy_test_wf", "input": "How long do refunds take?"},
        headers=_org_user_headers(client),
    )
    assert run_resp.status_code == 200


def test_delete_knowledge_base_removes_uploaded_files(client):
    files = [("files", ("doc.txt", b"some content here", "text/plain"))]
    with _ingestion_futures() as futures:
        client.post("/api/config/knowledge_bases/to_delete/upload?org=default", files=files)
        # Let the ingestion worker finish before deleting. It reads every
        # staged file, and on Windows an open handle makes the delete route's
        # `rmtree` fail with WinError 32 -- which the route deliberately only
        # logs, leaving the directory behind. That is the route's documented
        # behaviour under a live reader, not what this test is about, so the
        # race is removed rather than asserted around.
        _await_ingestion(futures.pop())

    from ui.backend.crud import _KB_UPLOADS_DIR

    upload_dir = _KB_UPLOADS_DIR / "1" / "to_delete"  # org-scoped: <org_id>/<name>
    assert upload_dir.is_dir()

    resp = client.delete("/api/config/knowledge_bases/to_delete?org=default")
    assert resp.status_code == 204
    assert not upload_dir.exists()


def test_deleting_kb_removes_ingestion_rows(client):
    # Upload creates a KnowledgeBaseRecord + an IngestionJob.
    resp = client.post(
        "/api/config/knowledge_bases/policies/upload?org=default",
        files=[("files", ("doc.txt", b"Refunds within 30 days.", "text/plain"))],
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    with open_test_db() as db:
        from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord

        import time

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            db.expire_all()
            job = db.get(IngestionJob, job_id)
            if job is not None and job.status in ("completed", "failed"):
                break
            time.sleep(0.05)
        kb_id = db.query(KnowledgeBaseRecord).filter_by(name="policies").one().id

    resp = client.delete("/api/config/knowledge_bases/policies?org=default")
    assert resp.status_code == 204

    with open_test_db() as db:
        from ui.backend.db.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

        assert db.query(IngestionJob).filter_by(kb_id=kb_id).count() == 0
        assert db.query(KnowledgeDocument).filter_by(kb_id=kb_id).count() == 0
        assert db.query(KnowledgeChunk).filter_by(kb_id=kb_id).count() == 0


_VALID_WORKFLOW_CONFIG = {
    "knowledge_bases": [],
    "agents": [
        {
            "name": "support_agent",
            "role": "Customer Support Specialist",
            "goal": "Answer customer questions",
            "model": "fake:hello",
        }
    ],
    "teams": [{"name": "support_team", "agents": ["support_agent"], "mode": "sequential"}],
    "workflow": {"steps": ["support_team"]},
}


def test_workflow_crud_round_trip_and_validation(client):
    create = client.put("/api/config/workflows/support_workflow?org=default", json=_VALID_WORKFLOW_CONFIG)
    assert create.status_code == 200
    body = create.json()
    assert body["status"] == "deployed"
    assert body["config"]["name"] == "support_workflow"

    listed = client.get("/api/config/workflows")
    assert [item["name"] for item in listed.json()] == ["support_workflow"]

    fetched = client.get("/api/config/workflows/support_workflow?org=default")
    assert fetched.status_code == 200

    assert client.delete("/api/config/workflows/support_workflow?org=default").status_code == 204
    assert client.get("/api/config/workflows/support_workflow?org=default").status_code == 404


def test_workflow_put_appends_immutable_versions(client):
    """Two PUTs of the same workflow -> versions 1 then 2; v1 config frozen."""
    from helpers import open_test_db
    from ui.backend.db.models import WorkflowRecord, WorkflowVersion

    def _config(marker):
        return {**_VALID_WORKFLOW_CONFIG,
                 "agents": [{**_VALID_WORKFLOW_CONFIG["agents"][0], "goal": marker}]}

    assert client.put("/api/config/workflows/wf?org=default",
                       json=_config("one")).status_code == 200
    assert client.put("/api/config/workflows/wf?org=default",
                       json=_config("two")).status_code == 200

    with open_test_db() as db:
        head = db.query(WorkflowRecord).filter_by(name="wf").one()
        versions = (db.query(WorkflowVersion)
                      .filter_by(workflow_id=head.id)
                      .order_by(WorkflowVersion.version_number).all())
        assert [v.version_number for v in versions] == [1, 2]
        assert versions[0].config != versions[1].config  # v1 preserved distinctly
        assert head.current_version_id == versions[1].id  # pointer at latest


def test_create_run_stamps_the_deployed_workflow_version(client, monkeypatch):
    """POST /api/runs dispatches run_in_background with workflow_version_id set to
    the deployed workflow's current version (the /api/runs -> stamp glue). The
    executor submit is captured so the assertion is deterministic (no threadpool
    wait) and no background run actually executes."""
    import ui.backend.main as main
    from helpers import open_test_db
    from ui.backend.db.models import WorkflowRecord

    assert client.put("/api/config/workflows/stamp_wf?org=default",
                      json=_VALID_WORKFLOW_CONFIG).status_code == 200
    with open_test_db() as db:
        expected = db.query(WorkflowRecord).filter_by(name="stamp_wf").one().current_version_id
    assert expected is not None

    captured = {}

    def _fake_submit(fn, *args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(main._executor, "submit", _fake_submit)
    resp = client.post("/api/runs", json={"workflow": "stamp_wf", "input": "hi"},
                       headers=_org_user_headers(client))
    assert resp.status_code == 200
    assert captured["workflow_version_id"] == expected


def test_create_run_stamps_the_deployed_workflow_id(client, monkeypatch):
    """POST /api/runs dispatches run_in_background with workflow_id set to the
    deployed workflow's stable head id (WorkflowRecord.id) -- the cross-workflow
    memory-scoping key, distinct from workflow_version_id."""
    import ui.backend.main as main
    from helpers import open_test_db
    from ui.backend.db.models import WorkflowRecord

    assert client.put("/api/config/workflows/id_stamp_wf?org=default",
                      json=_VALID_WORKFLOW_CONFIG).status_code == 200
    with open_test_db() as db:
        expected = db.query(WorkflowRecord).filter_by(name="id_stamp_wf").one().id

    captured = {}

    def _fake_submit(fn, *args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(main._executor, "submit", _fake_submit)
    resp = client.post("/api/runs", json={"workflow": "id_stamp_wf", "input": "hi"},
                       headers=_org_user_headers(client))
    assert resp.status_code == 200
    assert captured["workflow_id"] == expected


def test_resolve_workflow_and_version_binds_version_to_the_built_record(client):
    """_resolve_workflow_and_version returns the current_version_id of the same
    record it built the workflow from (one read), so a run stamps the version it
    actually executed rather than a separately-queried, possibly-newer one."""
    import ui.backend.main as main
    from helpers import open_test_db, get_org_id
    from ui.backend.db.models import WorkflowRecord

    assert client.put("/api/config/workflows/rv_wf?org=default",
                      json=_VALID_WORKFLOW_CONFIG).status_code == 200
    with open_test_db() as db:
        record = db.query(WorkflowRecord).filter_by(name="rv_wf").one()
        expected_version, expected_workflow_id = record.current_version_id, record.id
        workflow, version_id, workflow_id = main._resolve_workflow_and_version(
            "rv_wf", db, get_org_id("default")
        )
    assert workflow is not None
    assert version_id == expected_version
    assert workflow_id == expected_workflow_id


def test_delete_workflow_also_deletes_its_version_history(client):
    """Deleting a workflow head removes its immutable versions too -- no orphaned
    workflow_versions rows survive a deleted head (FK enforcement is off)."""
    from helpers import open_test_db
    from ui.backend.db.models import WorkflowRecord, WorkflowVersion

    assert client.put("/api/config/workflows/del_wf?org=default",
                      json=_VALID_WORKFLOW_CONFIG).status_code == 200
    with open_test_db() as db:
        head = db.query(WorkflowRecord).filter_by(name="del_wf").one()
        wf_id = head.id
        assert db.query(WorkflowVersion).filter_by(workflow_id=wf_id).count() == 1

    assert client.delete("/api/config/workflows/del_wf?org=default").status_code == 204

    with open_test_db() as db:
        assert db.query(WorkflowRecord).filter_by(name="del_wf").one_or_none() is None
        assert db.query(WorkflowVersion).filter_by(workflow_id=wf_id).count() == 0


def test_delete_workflow_refused_when_a_run_references_its_version(client):
    """A workflow whose version a run recorded can't be hard-deleted (409) --
    deleting would orphan that run's provenance. History is preserved."""
    from helpers import open_test_db, get_org_id
    from ui.backend.db.models import WorkflowRecord, WorkflowVersion, Run

    assert client.put("/api/config/workflows/run_wf?org=default",
                      json=_VALID_WORKFLOW_CONFIG).status_code == 200
    with open_test_db() as db:
        head = db.query(WorkflowRecord).filter_by(name="run_wf").one()
        db.add(Run(id="run-ref-1", workflow="run_wf", input="hi",
                   org_id=get_org_id("default"), workflow_version_id=head.current_version_id))
        db.commit()

    resp = client.delete("/api/config/workflows/run_wf?org=default")
    assert resp.status_code == 409
    assert "run" in resp.json()["detail"].lower()
    with open_test_db() as db:
        head = db.query(WorkflowRecord).filter_by(name="run_wf").one_or_none()
        assert head is not None  # head preserved
        assert db.query(WorkflowVersion).filter_by(workflow_id=head.id).count() == 1  # history intact


def test_delete_workflow_detaches_builder_sessions(client):
    """Deleting a never-run workflow nulls any builder session's workflow_id, so
    none is left pointing at a deleted head (it self-heals on next deploy)."""
    from helpers import open_test_db, get_org_id
    from ui.backend.db.models import WorkflowRecord, BuilderSession

    assert client.put("/api/config/workflows/sess_wf?org=default",
                      json=_VALID_WORKFLOW_CONFIG).status_code == 200
    with open_test_db() as db:
        head = db.query(WorkflowRecord).filter_by(name="sess_wf").one()
        db.add(BuilderSession(id="sess-detach-1", org_id=get_org_id("default"),
                              status="deployed", workflow_id=head.id))
        db.commit()

    assert client.delete("/api/config/workflows/sess_wf?org=default").status_code == 204
    with open_test_db() as db:
        sess = db.get(BuilderSession, "sess-detach-1")
        assert sess is not None and sess.workflow_id is None


def test_only_deployed_workflows_are_listed_and_runnable(client):
    from helpers import open_test_db, get_org_id
    from ui.backend.db.models import WorkflowRecord

    # crud save = deploy -> runnable immediately
    client.put("/api/config/workflows/live_wf?org=default", json=_VALID_WORKFLOW_CONFIG)
    # a draft can only exist as a legacy/direct row now
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(WorkflowRecord(name="draft_wf",
                              config={**_VALID_WORKFLOW_CONFIG, "name": "draft_wf"},
                              status="draft", org_id=org_id))
        db.commit()

    headers = _org_user_headers(client)
    workflows = client.get("/api/workflows", headers=headers).json()["workflows"]
    assert "live_wf" in workflows
    assert "draft_wf" not in workflows
    assert client.post("/api/runs", json={"workflow": "live_wf", "input": "hi"},
                       headers=headers).status_code == 200
    assert client.post("/api/runs", json={"workflow": "draft_wf", "input": "hi"},
                       headers=headers).status_code == 404


def test_workflow_put_rejects_agent_model_not_in_catalog(client):
    bad_config = {**_VALID_WORKFLOW_CONFIG,
                  "agents": [{**_VALID_WORKFLOW_CONFIG["agents"][0], "model": "openai:gpt-nope"}]}
    resp = client.put("/api/config/workflows/support_workflow?org=default", json=bad_config)
    assert resp.status_code == 400
    assert "openai:gpt-nope" in resp.json()["detail"]


def test_workflow_put_rejects_agent_with_missing_none_empty_or_nonstring_model(client):
    # The operator CRUD path builds Agent(**spec) directly, bypassing the
    # AgentSpec.model:str check -- so a missing/None/empty/non-string model must
    # be rejected at save, not persisted as deployed and failing at first run.
    base_agent = _VALID_WORKFLOW_CONFIG["agents"][0]
    cases = [
        {k: v for k, v in base_agent.items() if k != "model"},  # missing
        {**base_agent, "model": None},
        {**base_agent, "model": ""},
        {**base_agent, "model": 42},
    ]
    headers = _org_user_headers(client)
    for i, agent in enumerate(cases):
        bad_config = {**_VALID_WORKFLOW_CONFIG, "agents": [agent]}
        resp = client.put(f"/api/config/workflows/no_model_{i}?org=default", json=bad_config)
        assert resp.status_code == 400, f"case {i} should be rejected"
        # nothing persisted -> not runnable/listed
        assert f"no_model_{i}" not in client.get(
            "/api/workflows", headers=headers
        ).json()["workflows"]


def test_workflow_put_rejects_invalid_config(client):
    bad_config = {**_VALID_WORKFLOW_CONFIG, "teams": [{"name": "support_team", "agents": ["does_not_exist"], "mode": "sequential"}]}

    resp = client.put("/api/config/workflows/support_workflow?org=default", json=bad_config)

    assert resp.status_code == 400
    assert "unknown agent" in resp.json()["detail"]


def test_workflow_put_non_list_knowledge_bases_returns_400(client):
    # Regression: a malformed non-list `knowledge_bases` value must be rejected
    # with 400 by the workflow validator, not crash the request with an
    # uncaught TypeError (500). The CR-001 path-guard iteration must run inside
    # the error-handling try block, like the baseline _build_workflow did.
    bad_config = {**_VALID_WORKFLOW_CONFIG, "knowledge_bases": 1}

    resp = client.put("/api/config/workflows/support_workflow?org=default", json=bad_config)

    assert resp.status_code == 400


def test_workflow_config_is_runnable_via_get_workflow(client):
    client.put("/api/config/workflows/support_workflow?org=default", json=_VALID_WORKFLOW_CONFIG)

    resp = client.post(
        "/api/runs",
        json={"workflow": "support_workflow", "input": "hi"},
        headers=_org_user_headers(client),
    )

    assert resp.status_code == 200


def test_skill_crud_round_trip(client):
    config = {"instructions": "Use web_search for research.", "tools": []}

    create = client.put("/api/config/skills/research_skill", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["instructions"] == "Use web_search for research."

    listed = client.get("/api/config/skills")
    assert [item["name"] for item in listed.json()] == ["research_skill"]

    fetched = client.get("/api/config/skills/research_skill")
    assert fetched.status_code == 200
    assert fetched.json()["config"].get("tools", []) == []

    deleted = client.delete("/api/config/skills/research_skill")
    assert deleted.status_code == 204
    assert client.get("/api/config/skills/research_skill").status_code == 404


def test_skill_updates_append_visible_immutable_versions(client):
    first = client.put(
        "/api/config/skills/versioned",
        json={"instructions": "First playbook.", "tools": []},
    )
    assert first.status_code == 200
    assert first.json()["version"] == 1

    second = client.put(
        "/api/config/skills/versioned",
        json={"instructions": "Second playbook.", "tools": []},
    )
    assert second.status_code == 200
    assert second.json()["version"] == 2
    assert client.get("/api/config/skills/versioned").json()["version"] == 2

    history = client.get("/api/config/skills/versioned/versions")
    assert history.status_code == 200
    versions = history.json()
    assert [row["version"] for row in versions] == [2, 1]
    assert versions[0]["current"] is True
    assert versions[0]["created_by"]
    assert versions[0]["config"]["instructions"] == "Second playbook."
    assert versions[1]["current"] is False
    assert versions[1]["config"]["instructions"] == "First playbook."


def test_skill_put_rejects_missing_instructions(client):
    resp = client.put("/api/config/skills/bad_skill", json={"description": "no instructions"})
    assert resp.status_code == 400


def test_workflow_put_accepts_skill_reference_when_skill_exists(client):
    client.put("/api/config/skills/research_skill", json={
        "instructions": "Research topics thoroughly.",
        "tools": [],
    })
    config = {
        "knowledge_bases": [],
        "agents": [{
            "name": "agent1",
            "role": "Researcher",
            "goal": "Research topics",
            "model": "fake:hello",
            "tools": [],
            "skills": ["research_skill"],
        }],
        "teams": [{"name": "team1", "agents": ["agent1"], "mode": "sequential"}],
        "workflow": {"steps": ["team1"]},
    }
    resp = client.put("/api/config/workflows/my_workflow?org=default", json=config)
    assert resp.status_code == 200


def test_workflow_put_rejects_unknown_skill_reference(client):
    config = {
        "knowledge_bases": [],
        "agents": [{
            "name": "agent1",
            "role": "Researcher",
            "goal": "Research topics",
            "model": "fake:hello",
            "tools": [],
            "skills": ["nonexistent_skill"],
        }],
        "teams": [{"name": "team1", "agents": ["agent1"], "mode": "sequential"}],
        "workflow": {"steps": ["team1"]},
    }
    resp = client.put("/api/config/workflows/my_workflow?org=default", json=config)
    assert resp.status_code == 400
    assert "Unknown skill" in resp.json()["detail"]


def test_load_knowledge_base_tools_builds_only_referenced_kbs(client, tmp_path):
    from sqlalchemy.orm import Session

    from ui.backend.knowledge_bases import load_knowledge_base_tools

    docs_dir = tmp_path / "policy_docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refund processing is completed within 5 business days.")

    client.put(
        "/api/config/knowledge_bases/policy_kb?org=default",
        json={"path": str(docs_dir), "type": "local_folder"},
    )
    client.put(
        "/api/config/knowledge_bases/unused_kb?org=default",
        json={"path": "./does/not/exist", "type": "local_folder"},
    )

    # Use the same DB the test client's overridden get_db uses.
    from ui.backend.db_session import get_db as real_get_db

    from helpers import get_org_id

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db: Session = next(db_gen)
    try:
        raw = {"agents": [{"name": "a", "tools": ["policy_kb", "calculator"]}]}
        tools = load_knowledge_base_tools(db, raw, tmp_path / "wf.yaml", org_id=get_org_id())
    finally:
        db_gen.close()

    assert set(tools) == {"policy_kb"}
    assert "Refund" in tools["policy_kb"]("refund processing")


def test_workflow_put_resolves_standalone_knowledge_base_by_name(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")
    client.put("/api/config/knowledge_bases/policy_kb?org=default", json={"path": str(docs_dir), "type": "local_folder"})

    workflow_config = {
        "agents": [
            {
                "name": "support_agent",
                "role": "Support",
                "goal": "Answer policy questions",
                "model": "fake:hi",
                "tools": ["policy_kb"],
            }
        ],
        "teams": [{"name": "team", "agents": ["support_agent"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/policy_wf?org=default", json=workflow_config)

    assert resp.status_code == 200


def test_run_resolves_standalone_knowledge_base_by_name(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")
    client.put("/api/config/knowledge_bases/policy_kb?org=default", json={"path": str(docs_dir), "type": "local_folder"})

    workflow_config = {
        "agents": [
            {
                "name": "support_agent",
                "role": "Support",
                "goal": "Answer policy questions",
                "model": "fake:hi",
                "tools": ["policy_kb"],
            }
        ],
        "teams": [{"name": "team", "agents": ["support_agent"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    put_resp = client.put("/api/config/workflows/policy_wf?org=default", json=workflow_config)
    assert put_resp.status_code == 200

    # Defensive only: the `client` fixture already clears the cache at setup,
    # and the workflow PUT route never populates `_workflow_cache` itself.
    backend_main._workflow_cache.clear()
    run_resp = client.post(
        "/api/runs",
        json={"workflow": "policy_wf", "input": "How long do refunds take?"},
        headers=_org_user_headers(client),
    )
    assert run_resp.status_code == 200


def test_workflow_put_resolves_standalone_vector_knowledge_base_by_name(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")
    client.put(
        "/api/config/knowledge_bases/policy_kb?org=default",
        json={"path": str(docs_dir), "type": "vector", "embedding_model": "fake:8"},
    )

    workflow_config = {
        "agents": [
            {
                "name": "support_agent",
                "role": "Support",
                "goal": "Answer policy questions",
                "model": "fake:hi",
                "tools": ["policy_kb"],
            }
        ],
        "teams": [{"name": "team", "agents": ["support_agent"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/policy_wf?org=default", json=workflow_config)

    assert resp.status_code == 200


def test_deployed_workflow_keeps_pinned_skill_until_redeploy(client):
    client.put(
        "/api/config/skills/greeting",
        json={"description": "How to greet", "instructions": "Say hello warmly.", "tools": []},
    )
    workflow_config = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi", "skills": ["greeting"]}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    client.put("/api/config/workflows/skill_wf?org=default", json=workflow_config)

    from ui.backend.main import _get_workflow
    from ui.backend.db_session import get_db as real_get_db

    from helpers import get_org_id

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        wf1 = _get_workflow("skill_wf", db, get_org_id())
        assert "Say hello warmly." in wf1.steps[0].agents[0].backstory

        client.put(
            "/api/config/skills/greeting",
            json={"description": "How to greet", "instructions": "Say hello formally.", "tools": []},
        )

        wf2 = _get_workflow("skill_wf", db, get_org_id())
        assert "Say hello warmly." in wf2.steps[0].agents[0].backstory
        assert wf2 is not wf1

        client.put("/api/config/workflows/skill_wf?org=default", json=workflow_config)
        wf3 = _get_workflow("skill_wf", db, get_org_id())
        assert "Say hello formally." in wf3.steps[0].agents[0].backstory
    finally:
        db_gen.close()


def test_load_skills_only_runs_on_workflow_cache_miss(client, monkeypatch):
    workflow_config = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi"}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    client.put("/api/config/workflows/cached_wf?org=default", json=workflow_config)

    calls = []
    original = backend_main.load_skills

    def counting_load_skills(db, org_id=None, **kwargs):
        calls.append(1)
        return original(db, org_id, **kwargs)

    monkeypatch.setattr(backend_main, "load_skills", counting_load_skills)

    from ui.backend.main import _get_workflow
    from ui.backend.db_session import get_db as real_get_db

    from helpers import get_org_id

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        _get_workflow("cached_wf", db, get_org_id())
        _get_workflow("cached_wf", db, get_org_id())
    finally:
        db_gen.close()

    assert len(calls) == 1


def test_inline_knowledge_base_wins_over_standalone_of_same_name(client, tmp_path):
    standalone_dir = tmp_path / "standalone_docs"
    standalone_dir.mkdir()
    (standalone_dir / "doc.txt").write_text("STANDALONE: days")
    client.put("/api/config/knowledge_bases/shared_name?org=default", json={"path": str(standalone_dir), "type": "local_folder"})

    inline_dir = tmp_path / "inline_docs"
    inline_dir.mkdir()
    (inline_dir / "doc.txt").write_text("INLINE: policy")

    workflow_config = {
        "knowledge_bases": [{"name": "shared_name", "path": str(inline_dir), "type": "local_folder"}],
        "agents": [
            {
                "name": "a",
                "role": "Support",
                "goal": "Answer",
                "model": "fake:hi",
                "tools": ["shared_name"],
            }
        ],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/priority_wf?org=default", json=workflow_config)
    assert resp.status_code == 200

    from ui.backend.main import _get_workflow
    from ui.backend.db_session import get_db as real_get_db

    from helpers import get_org_id

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        workflow = _get_workflow("priority_wf", db, get_org_id())
    finally:
        db_gen.close()

    team = workflow.steps[0]
    agent = team.agents[0]
    tool = next(t for t in agent.tools if t.__name__ == "shared_name")
    assert "INLINE" in tool("policy")


def test_same_named_workflow_in_two_orgs_resolves_independently(client):
    # The workflow cache is keyed by (org_id, name): two orgs' same-named
    # workflows must build and cache as separate entries.
    from helpers import get_org_id, open_test_db
    from ui.backend.db.orgs import get_or_create_org
    from ui.backend.db_session import get_db as real_get_db
    from ui.backend.main import _get_workflow

    with open_test_db() as db:
        get_or_create_org(db, "other")

    def workflow_config(reply):
        return {
            "agents": [{"name": "a", "role": "r", "goal": "g", "model": f"fake:{reply}"}],
            "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
            "workflow": {"steps": ["team"]},
        }

    assert client.put("/api/config/workflows/wf?org=default", json=workflow_config("AAA")).status_code == 200
    assert client.put("/api/config/workflows/wf?org=other", json=workflow_config("BBB")).status_code == 200

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        wf_default = _get_workflow("wf", db, get_org_id("default"))
        wf_other = _get_workflow("wf", db, get_org_id("other"))
    finally:
        db_gen.close()

    assert wf_default is not wf_other
    assert wf_default.steps[0].agents[0].model == "fake:AAA"
    assert wf_other.steps[0].agents[0].model == "fake:BBB"


def test_broken_standalone_kb_only_breaks_workflows_that_reference_it(client):
    client.put("/api/config/knowledge_bases/broken_kb?org=default", json={"path": "/no/such/path", "type": "local_folder"})

    broken_workflow = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi", "tools": ["broken_kb"]}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/broken_wf?org=default", json=broken_workflow)
    assert resp.status_code == 400
    assert "broken_kb" in resp.json()["detail"]

    unrelated_workflow = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi"}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp2 = client.put("/api/config/workflows/unrelated_wf?org=default", json=unrelated_workflow)
    assert resp2.status_code == 200


def test_workflow_put_rejects_kb_named_after_builtin(client):
    # An inline KB named after a built-in tool would silently shadow it.
    bad = {**_VALID_WORKFLOW_CONFIG,
           "knowledge_bases": [{"name": "calculator", "path": "docs"}],
           "agents": [{**_VALID_WORKFLOW_CONFIG["agents"][0], "tools": ["calculator"]}]}
    resp = client.put("/api/config/workflows/collide_wf?org=default", json=bad)
    assert resp.status_code == 400
    assert "calculator" in resp.json()["detail"]
    assert "built-in tool name" in resp.json()["detail"]
    assert "collide_wf" not in client.get(
        "/api/workflows", headers=_org_user_headers(client)
    ).json()["workflows"]


def _deployed_wf(config_agents):
    return {
        "agents": config_agents,
        "teams": [{"name": "team", "agents": [a["name"] for a in config_agents], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }


def test_delete_skill_referenced_by_deployed_workflow_is_409(client):
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(SkillRecord(name="greeting", org_id=org_id,
                           config={"name": "greeting", "instructions": "hi", "tools": []}))
        db.commit()
    # The typed-row guard only sees dependencies recorded at deploy time
    # (record_version_dependencies), so deploy for real via PUT rather than
    # inserting the WorkflowRecord directly.
    deploy = client.put(
        "/api/config/workflows/greeter_team?org=default",
        json=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                            "model": "fake:hi", "skills": ["greeting"]}]),
    )
    assert deploy.status_code == 200
    resp = client.delete("/api/config/skills/greeting?org=default")
    assert resp.status_code == 409
    assert "greeter_team" in resp.json()["detail"]
    assert client.get("/api/config/skills/greeting?org=default").status_code == 200  # not deleted


def test_delete_kb_referenced_by_deployed_workflow_is_409(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("hello", encoding="utf-8")
    assert client.put(
        "/api/config/knowledge_bases/mykb?org=default",
        json={"path": str(docs_dir), "type": "local_folder"},
    ).status_code == 200
    # The typed-row guard only sees dependencies recorded at deploy time
    # (record_version_dependencies), so deploy for real via PUT rather than
    # inserting the WorkflowRecord directly.
    deploy = client.put(
        "/api/config/workflows/kb_team?org=default",
        json=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                            "model": "fake:hi", "tools": ["mykb"]}]),
    )
    assert deploy.status_code == 200
    resp = client.delete("/api/config/knowledge_bases/mykb?org=default")
    assert resp.status_code == 409
    assert "kb_team" in resp.json()["detail"]


def test_delete_unreferenced_skill_still_204(client):
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(SkillRecord(name="unused", org_id=org_id,
                           config={"name": "unused", "instructions": "x", "tools": []}))
        db.commit()
    assert client.delete("/api/config/skills/unused?org=default").status_code == 204


# --- Post-review hardening (P1-08 boundary, delete robustness, undeletable built-ins) ---

def test_kb_put_rejects_builtin_name(client):
    # F1: a KB can't be *created* with a built-in tool name (else it shadows the
    # built-in at load, bypassing the deploy-time collision check).
    resp = client.put("/api/config/knowledge_bases/calculator?org=default", json={"path": "docs"})
    assert resp.status_code == 400
    assert "built-in tool name" in resp.json()["detail"]


def test_kb_upload_rejects_builtin_name(client):
    # F1: same guard on the upload creation path.
    resp = client.post(
        "/api/config/knowledge_bases/calculator/upload?org=default",
        files=[("files", ("d.txt", b"hi", "text/plain"))],
    )
    assert resp.status_code == 400
    assert "built-in tool name" in resp.json()["detail"]


def test_delete_scan_tolerates_malformed_deployed_workflow(client):
    # F6: a malformed deployed workflow must not 500 an unrelated skill/KB delete.
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(SkillRecord(name="unused_sk", org_id=org_id,
                           config={"name": "unused_sk", "instructions": "x", "tools": []}))
        db.add(WorkflowRecord(name="broken", org_id=org_id, status="deployed",
                              config={"agents": 5, "teams": [], "workflow": {"steps": []}}))
        db.commit()
    resp = client.delete("/api/config/skills/unused_sk?org=default")
    assert resp.status_code == 204  # not 500 despite the malformed workflow


def test_delete_builtin_platform_skill_is_refused(client):
    # F4: a seeded platform built-in skill can't be deleted (would orphan bundled
    # YAML workflows that depend on it).
    from ui.backend.skills import seed_default_skills
    with open_test_db() as db:
        seed_default_skills(db)
    resp = client.delete("/api/config/skills/email_triage_reply")  # platform (no ?org)
    assert resp.status_code == 409
    assert "built-in" in resp.json()["detail"].lower()


# --- Typed dependency records (P1-04): cross-org matching, current-config
# semantics, and head-delete cascade ---

def test_delete_platform_builtin_skill_referenced_by_org_workflow_is_409(client):
    # A platform built-in skill (org omitted at creation -> org_id NULL) is
    # referenced by an org's deployed workflow; deleting it platform-wide (no
    # ?org=) must still 409, proving the guard matches by resource_id across
    # orgs rather than needing an all-orgs name scan.
    assert client.put(
        "/api/config/skills/greeting",
        json={"instructions": "hi", "tools": []},
    ).status_code == 200
    assert client.put(
        "/api/config/workflows/org_team?org=default",
        json=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                            "model": "fake:hi", "skills": ["greeting"]}]),
    ).status_code == 200
    resp = client.delete("/api/config/skills/greeting")  # platform tier, no ?org=
    assert resp.status_code == 409
    assert "org_team" in resp.json()["detail"]


def test_skill_dropped_from_current_version_becomes_deletable(client):
    # Deploy a workflow referencing skill 's', then redeploy the same workflow
    # name with an agent config that no longer references 's'. The superseded
    # version still has a dep row, but the guard only reads the current
    # version, so 's' must become deletable.
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(SkillRecord(name="s", org_id=org_id,
                           config={"name": "s", "instructions": "hi", "tools": []}))
        db.commit()
    assert client.put(
        "/api/config/workflows/redeploy_team?org=default",
        json=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                            "model": "fake:v1", "skills": ["s"]}]),
    ).status_code == 200
    assert client.put(
        "/api/config/workflows/redeploy_team?org=default",
        json=_deployed_wf([{"name": "a", "role": "r", "goal": "g", "model": "fake:v2"}]),
    ).status_code == 200
    from ui.backend.db.models import SkillVersion, WorkflowDependency
    with open_test_db() as db:
        pinned_version_id = db.query(WorkflowDependency.resource_version_id).filter_by(
            resource_kind="skill", resource_name="s"
        ).scalar()
    assert client.delete("/api/config/skills/s?org=default").status_code == 204
    with open_test_db() as db:
        retained = db.get(SkillVersion, pinned_version_id)
        assert retained is not None
        assert retained.skill_id is None
        assert retained.config["instructions"] == "hi"


def test_org_skill_created_after_deploy_does_not_retarget_team(client):
    # A deployed team is pinned to the platform skill it resolved. Creating an
    # org-local same-named skill affects only future deploys, not this version.
    assert client.put(
        "/api/config/skills/helper",  # platform tier (org omitted -> org_id NULL)
        json={"instructions": "hi", "tools": []},
    ).status_code == 200
    assert client.put(
        "/api/config/workflows/helper_team?org=default",
        json=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                            "model": "fake:hi", "skills": ["helper"]}]),
    ).status_code == 200
    # Org creates its own "helper" after the deploy.
    assert client.put(
        "/api/config/skills/helper?org=default",
        json={"instructions": "hey", "tools": []},
    ).status_code == 200
    # The new org skill is not used yet and can be deleted.
    assert client.delete("/api/config/skills/helper?org=default").status_code == 204
    # The pinned platform head remains protected by the current team version.
    resp = client.delete("/api/config/skills/helper")
    assert resp.status_code == 409
    assert "helper_team" in resp.json()["detail"]


def test_inline_kb_shadowing_standalone_leaves_standalone_deletable(client, tmp_path):
    # A standalone KB "faq" and a workflow that defines an inline KB also named
    # "faq". The inline KB shadows the standalone at runtime, so the workflow
    # does not depend on the standalone -- deleting the standalone must 204, not
    # 409 (the inline KB has no standalone row to protect).
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("hello", encoding="utf-8")
    assert client.put(
        "/api/config/knowledge_bases/faq?org=default",
        json={"path": str(docs_dir), "type": "local_folder"},
    ).status_code == 200
    assert client.put(
        "/api/config/workflows/inline_team?org=default",
        json={"knowledge_bases": [{"name": "faq", "path": str(docs_dir), "type": "local_folder"}],
              "agents": [{"name": "a", "role": "r", "goal": "g",
                          "model": "fake:hi", "tools": ["faq"]}],
              "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
              "workflow": {"steps": ["team"]}},
    ).status_code == 200
    assert client.delete("/api/config/knowledge_bases/faq?org=default").status_code == 204


def test_delete_workflow_head_removes_dependency_rows(client):
    # Deploy a never-run workflow referencing a skill, then hard-delete the
    # head. FK enforcement is off, so no DB cascade removes WorkflowDependency
    # rows -- delete_workflow_config must clean them up itself.
    from ui.backend.db.models import WorkflowDependency, WorkflowVersion

    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(SkillRecord(name="cascade_skill", org_id=org_id,
                           config={"name": "cascade_skill", "instructions": "hi", "tools": []}))
        db.commit()
    assert client.put(
        "/api/config/workflows/cascade_team?org=default",
        json=_deployed_wf([{"name": "a", "role": "r", "goal": "g",
                            "model": "fake:hi", "skills": ["cascade_skill"]}]),
    ).status_code == 200

    with open_test_db() as db:
        workflow_id = db.query(WorkflowRecord).filter_by(name="cascade_team", org_id=org_id).one().id
        version_ids = [v for (v,) in db.query(WorkflowVersion.id).filter_by(workflow_id=workflow_id)]
        assert version_ids  # sanity: a version was published
        dep_count_before = db.query(WorkflowDependency).filter(
            WorkflowDependency.workflow_version_id.in_(version_ids)
        ).count()
        assert dep_count_before > 0  # sanity: a dep row was recorded

    assert client.delete("/api/config/workflows/cascade_team?org=default").status_code == 204

    with open_test_db() as db:
        remaining = db.query(WorkflowDependency).filter(
            WorkflowDependency.workflow_version_id.in_(version_ids)
        ).count()
        assert remaining == 0


# --- Third-round review fixes (dict-shaped refs, legacy-collision fail-closed) ---

def test_delete_kb_referenced_via_dict_shaped_tools_is_409(client, tmp_path):
    # F2: the loader honors a dict-shaped `tools` ({"mykb": true} -> ["mykb"]), so
    # record_version_dependencies (which the delete guard now reads) must too, else
    # the reference is missed (bypass). The workflow PUT body is un-validated raw
    # JSON (no Specification pydantic model), so this shape reaches deploy for real.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("hello", encoding="utf-8")
    assert client.put(
        "/api/config/knowledge_bases/mykb?org=default",
        json={"path": str(docs_dir), "type": "local_folder"},
    ).status_code == 200
    deploy = client.put(
        "/api/config/workflows/dict_team?org=default",
        json={"agents": [{"name": "a", "role": "r", "goal": "g",
                          "model": "fake:hi", "tools": {"mykb": True}}],
              "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
              "workflow": {"steps": ["team"]}},
    )
    assert deploy.status_code == 200
    resp = client.delete("/api/config/knowledge_bases/mykb?org=default")
    assert resp.status_code == 409
    assert "dict_team" in resp.json()["detail"]


def test_run_fails_closed_on_legacy_kb_shadowing_builtin(client):
    # F4: a legacy KB named after a built-in (predating the KB-PUT guard) must
    # fail closed at load, not silently shadow the built-in tool.
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(KnowledgeBaseRecord(name="calculator", org_id=org_id,
                                   config={"name": "calculator", "path": "docs"}))
        db.add(WorkflowRecord(name="legacy_wf", org_id=org_id, status="deployed",
                              config={"agents": [{"name": "a", "role": "r", "goal": "g",
                                                  "model": "fake:hi", "tools": ["calculator"]}],
                                      "teams": [{"name": "t", "agents": ["a"], "mode": "sequential"}],
                                      "workflow": {"steps": ["t"]}}))
        db.commit()
    resp = client.post("/api/runs", json={"workflow": "legacy_wf", "input": "hi"},
                       headers=_org_user_headers(client))
    assert resp.status_code == 400
    assert "built-in" in resp.json()["detail"]


# --- Fourth-round review fixes (string refs, inline-KB shadow) ---

def test_delete_kb_referenced_via_string_shaped_tools_is_409(client, tmp_path):
    # F2: the loader does list(tools), so a string "x" resolves to ["x"] and runs;
    # record_version_dependencies (which the delete guard now reads) must use the
    # same normalization or it misses the reference. The workflow PUT body is
    # un-validated raw JSON (no Specification pydantic model), so this shape
    # reaches deploy for real.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("hello", encoding="utf-8")
    assert client.put(
        "/api/config/knowledge_bases/x?org=default",
        json={"path": str(docs_dir), "type": "local_folder"},
    ).status_code == 200
    deploy = client.put(
        "/api/config/workflows/str_team?org=default",
        json={"agents": [{"name": "a", "role": "r", "goal": "g",
                          "model": "fake:hi", "tools": "x"}],
              "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
              "workflow": {"steps": ["team"]}},
    )
    assert deploy.status_code == 200
    resp = client.delete("/api/config/knowledge_bases/x?org=default")
    assert resp.status_code == 409
    assert "str_team" in resp.json()["detail"]


def test_run_fails_closed_on_legacy_inline_kb_shadowing_builtin(client):
    # F3: an inline KB (built by the loader, not load_knowledge_base_tools) named
    # after a built-in must also fail closed at load, not silently shadow it.
    with open_test_db() as db:
        org_id = get_org_id("default")
        db.add(WorkflowRecord(name="inline_wf", org_id=org_id, status="deployed",
                              config={"knowledge_bases": [{"name": "calculator", "path": "docs"}],
                                      "agents": [{"name": "a", "role": "r", "goal": "g",
                                                  "model": "fake:hi", "tools": ["calculator"]}],
                                      "teams": [{"name": "t", "agents": ["a"], "mode": "sequential"}],
                                      "workflow": {"steps": ["t"]}}))
        db.commit()
    resp = client.post("/api/runs", json={"workflow": "inline_wf", "input": "hi"},
                       headers=_org_user_headers(client))
    assert resp.status_code == 400
    assert "built-in" in resp.json()["detail"]


def test_delete_workflow_blocked_by_active_share_link(client):
    from helpers import get_org_id, open_test_db
    from ui.backend.db.models import WorkflowRecord
    from ui.backend.db.share_links import create_share_link
    from ui.backend.db.users import get_user_by_username

    org_id = get_org_id()
    config = {
        "name": "shared_team",
        "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
        "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["tm"]},
    }
    with open_test_db() as db:
        record = WorkflowRecord(name="shared_team", org_id=org_id, config=config, status="deployed")
        db.add(record)
        db.commit()
        db.refresh(record)
        user = get_user_by_username(db, "test")
        create_share_link(db, workflow_id=record.id, org_id=org_id, created_by=user.id)

    resp = client.delete("/api/config/workflows/shared_team?org=default")
    assert resp.status_code == 409
    assert "share" in resp.json()["detail"].lower()


def test_delete_workflow_allowed_with_only_revoked_share_link(client):
    from helpers import get_org_id, open_test_db
    from ui.backend.db.models import WorkflowRecord
    from ui.backend.db.share_links import create_share_link, patch_share_link
    from ui.backend.db.users import get_user_by_username

    org_id = get_org_id()
    config = {
        "name": "revoked_share_team",
        "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
        "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["tm"]},
    }
    with open_test_db() as db:
        record = WorkflowRecord(name="revoked_share_team", org_id=org_id, config=config, status="deployed")
        db.add(record)
        db.commit()
        db.refresh(record)
        user = get_user_by_username(db, "test")
        link = create_share_link(db, workflow_id=record.id, org_id=org_id, created_by=user.id)
        patch_share_link(db, link, active=False)

    resp = client.delete("/api/config/workflows/revoked_share_team?org=default")
    assert resp.status_code == 204


def test_deleting_workflow_removes_revoked_share_links_sessions_and_messages(client):
    """A revoked share_links row (and its sessions/messages) must not become
    a permanent orphan when its team is deleted -- FK enforcement is off, so
    nothing else does this, and SQLite's non-AUTOINCREMENT integer primary
    key means a later workflow could otherwise silently inherit another
    team's old share links and visitor transcripts (Codex review finding)."""
    from helpers import get_org_id, open_test_db
    from ui.backend.db.models import ShareLink, ShareMessage, ShareSession, WorkflowRecord
    from ui.backend.db.share_links import create_share_link, patch_share_link
    from ui.backend.db.share_messages import append_message
    from ui.backend.db.share_sessions import create_share_session
    from ui.backend.db.users import get_user_by_username

    org_id = get_org_id()
    config = {
        "name": "orphan_check_team",
        "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
        "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["tm"]},
    }
    with open_test_db() as db:
        record = WorkflowRecord(name="orphan_check_team", org_id=org_id, config=config, status="deployed")
        db.add(record)
        db.commit()
        db.refresh(record)
        user = get_user_by_username(db, "test")
        link = create_share_link(db, workflow_id=record.id, org_id=org_id, created_by=user.id)
        session = create_share_session(db, link.id)
        append_message(db, session.id, turn_number=1, role="user", content="hi")
        link_id, session_id = link.id, session.id
        patch_share_link(db, link, active=False)

    resp = client.delete("/api/config/workflows/orphan_check_team?org=default")
    assert resp.status_code == 204

    with open_test_db() as db:
        assert db.get(ShareLink, link_id) is None
        assert db.get(ShareSession, session_id) is None
        assert db.query(ShareMessage).filter_by(share_session_id=session_id).count() == 0


def test_list_workflows_includes_workflow_ids(client):
    with open_test_db() as db:
        record = WorkflowRecord(
            name="idtest", org_id=get_org_id(),
            config={"name": "idtest", "agents": [], "teams": [], "workflow": {"steps": []}},
            status="deployed",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        expected_id = record.id

    headers = _org_user_headers(client)
    resp = client.get("/api/workflows", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "idtest" in body["workflows"]
    assert body["workflow_ids"]["idtest"] == expected_id
