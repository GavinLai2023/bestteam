"""Tests for the `/api/config` CRUD API (Phase 2) -- the "advanced view" for
fine-tuning agents/teams/knowledge_bases/workflows directly."""

from pathlib import Path
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import crud as backend_crud
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db


def _active_kb_dir(uploads: Path, name: str) -> Path:
    """Resolve an uploaded KB's active version dir via its CURRENT pointer.

    Uploads are org-scoped on disk (`<uploads>/<org_id>/<name>`); the fixture
    user's 'default' org is the first org created in each test DB, so id 1.
    """
    from ui.backend.knowledge_bases import resolve_kb_upload_path

    resolved = resolve_kb_upload_path({"path": str(uploads / "1" / name)})
    return Path(resolved["path"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(backend_crud, "_KB_UPLOADS_DIR", tmp_path / "knowledge_base_uploads")
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
        test_client = TestClient(backend_main.app)
        # The Advanced/config API is admin-only; provision the fixture user as
        # an admin so the existing CRUD tests exercise the endpoints. A
        # non-admin 403 is covered separately by test_config_forbidden_for_non_admin.
        token = create_user_and_login(test_client, admin=True)
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_config_forbidden_for_non_admin(client):
    # Advanced config is admin-only: an authenticated non-admin user gets 403.
    token = create_user_and_login(client, username="regular", password="pw")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/config/agents", headers=headers).status_code == 403
    assert client.put(
        "/api/config/agents/x",
        json={"role": "R", "goal": "G", "model": "fake:hi", "tools": []},
        headers=headers,
    ).status_code == 403


def test_cross_org_config_access_is_404(client):
    # Explicit org targeting: an item created in org A is invisible through
    # org B's lens (404, not 403 -- existence is not revealed), and the same
    # name can exist in both orgs.
    from helpers import open_test_db
    from ui.backend.db.orgs import get_or_create_org

    with open_test_db() as db:
        get_or_create_org(db, "other")

    config = {"role": "Support", "goal": "Help", "model": "fake:hi", "tools": []}
    assert client.put("/api/config/agents/dup?org=default", json=config).status_code == 200
    assert client.get("/api/config/agents/dup?org=other").status_code == 404
    assert client.delete("/api/config/agents/dup?org=other").status_code == 404
    assert client.put("/api/config/agents/dup?org=other", json=config).status_code == 200

    listed = client.get("/api/config/agents").json()
    assert sorted((item["name"], item["org"]) for item in listed) == [
        ("dup", "default"),
        ("dup", "other"),
    ]
    filtered = client.get("/api/config/agents?org=other").json()
    assert [(item["name"], item["org"]) for item in filtered] == [("dup", "other")]


def test_config_mutations_require_org_param(client):
    config = {"role": "R", "goal": "G", "model": "fake:hi", "tools": []}
    assert client.put("/api/config/agents/x", json=config).status_code == 422
    assert client.get("/api/config/agents/x").status_code == 422
    assert client.delete("/api/config/agents/x").status_code == 422
    assert client.put("/api/config/agents/x?org=ghost", json=config).status_code == 404


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


def test_agent_crud_round_trip(client):
    config = {"role": "Support", "goal": "Help customers", "model": "fake:hi", "tools": []}

    create = client.put("/api/config/agents/support_agent?org=default", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["role"] == "Support"

    listed = client.get("/api/config/agents")
    assert [item["name"] for item in listed.json()] == ["support_agent"]

    fetched = client.get("/api/config/agents/support_agent?org=default")
    assert fetched.status_code == 200
    assert fetched.json()["config"]["goal"] == "Help customers"

    deleted = client.delete("/api/config/agents/support_agent?org=default")
    assert deleted.status_code == 204
    assert client.get("/api/config/agents/support_agent?org=default").status_code == 404


def test_agent_put_rejects_invalid_shape(client):
    resp = client.put("/api/config/agents/support_agent?org=default", json={"role": "Support"})
    assert resp.status_code == 400


def test_team_crud_round_trip(client):
    config = {"agents": ["support_agent"], "mode": "sequential"}

    create = client.put("/api/config/teams/support_team?org=default", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["agents"] == ["support_agent"]

    assert client.get("/api/config/teams/support_team?org=default").status_code == 200
    assert client.delete("/api/config/teams/support_team?org=default").status_code == 204


def test_knowledge_base_put_omits_vector_only_fields_for_local_folder(client):
    config = {"path": "./docs", "type": "local_folder", "embedding_model": "fake:8"}

    resp = client.put("/api/config/knowledge_bases/docs?org=default", json=config)

    assert resp.status_code == 200
    assert "embedding_model" not in resp.json()["config"]


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


def test_unknown_agent_returns_404(client):
    assert client.get("/api/config/agents/does-not-exist?org=default").status_code == 404
    assert client.delete("/api/config/agents/does-not-exist?org=default").status_code == 404


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

    with patch.object(backend_crud, "LocalFolderKnowledgeBase", side_effect=ConfigurationError("bad upload")):
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
    # CR-008 (pointer layout): CURRENT always resolves to a complete version --
    # there is no rename-swap window where the KB dir has no live version. The
    # immediately-previous version is retained as a grace window for readers
    # that just resolved to it; older versions are cleaned up.
    uploads = tmp_path / "knowledge_base_uploads"
    client.post("/api/config/knowledge_bases/kb/upload?org=default", files=[("files", ("v1.txt", b"one", "text/plain"))])
    v1 = _active_kb_dir(uploads, "kb")
    client.post("/api/config/knowledge_bases/kb/upload?org=default", files=[("files", ("v2.txt", b"two", "text/plain"))])
    v2 = _active_kb_dir(uploads, "kb")

    assert v1 != v2
    assert sorted(p.name for p in v2.iterdir()) == ["v2.txt"]  # active version
    assert v1.is_dir()  # previous version kept as a grace window

    # A third upload cleans the now-two-generations-old version.
    client.post("/api/config/knowledge_bases/kb/upload?org=default", files=[("files", ("v3.txt", b"three", "text/plain"))])
    assert not v1.is_dir()


def test_concurrent_upload_promotion_is_serialized_per_kb(client, tmp_path):
    # CR-008: concurrent uploads of the same KB must not interleave the CURRENT
    # pointer flip + version cleanup. The promotion critical section is guarded
    # by a per-KB lock; holding it must block a second upload's promotion until
    # released, after which the KB ends in a consistent state.
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
    # CURRENT resolves to a real version dir holding the last writer's file.
    active = _active_kb_dir(uploads, "kb")
    assert active.is_dir()
    assert sorted(p.name for p in active.iterdir()) == ["second.txt"]


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
    assert body["file_count"] == 2
    assert body["chunk_count"] >= 2
    assert body["config"]["type"] == "local_folder"
    assert "knowledge_base_uploads" in body["config"]["path"]

    get_resp = client.get("/api/config/knowledge_bases/support_docs?org=default")
    assert get_resp.status_code == 200


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


def test_upload_rejects_unparseable_file_and_cleans_up(client):
    files = [("files", ("bad.exe", b"\x00\x01\x02", "application/octet-stream"))]
    resp = client.post("/api/config/knowledge_bases/bad_kb/upload?org=default", files=files)
    assert resp.status_code == 400
    get_resp = client.get("/api/config/knowledge_bases/bad_kb?org=default")
    assert get_resp.status_code == 404


def test_uploaded_kb_is_queryable_by_a_workflow(client):
    files = [("files", ("policy.txt", b"Refunds are processed within 5 business days of approval.", "text/plain"))]
    upload_resp = client.post("/api/config/knowledge_bases/policy_kb/upload?org=default", files=files)
    assert upload_resp.status_code == 200
    uploaded_path = upload_resp.json()["config"]["path"]

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

    run_resp = client.post("/api/runs", json={"workflow": "policy_test_wf", "input": "How long do refunds take?"})
    assert run_resp.status_code == 200


def test_delete_knowledge_base_removes_uploaded_files(client):
    files = [("files", ("doc.txt", b"some content here", "text/plain"))]
    client.post("/api/config/knowledge_bases/to_delete/upload?org=default", files=files)

    from ui.backend.crud import _KB_UPLOADS_DIR

    upload_dir = _KB_UPLOADS_DIR / "1" / "to_delete"  # org-scoped: <org_id>/<name>
    assert upload_dir.is_dir()

    resp = client.delete("/api/config/knowledge_bases/to_delete?org=default")
    assert resp.status_code == 204
    assert not upload_dir.exists()


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
    assert body["status"] == "draft"
    assert body["config"]["name"] == "support_workflow"

    listed = client.get("/api/config/workflows")
    assert [item["name"] for item in listed.json()] == ["support_workflow"]

    fetched = client.get("/api/config/workflows/support_workflow?org=default")
    assert fetched.status_code == 200

    assert client.delete("/api/config/workflows/support_workflow?org=default").status_code == 204
    assert client.get("/api/config/workflows/support_workflow?org=default").status_code == 404


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

    resp = client.post("/api/runs", json={"workflow": "support_workflow", "input": "hi"})

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
    run_resp = client.post("/api/runs", json={"workflow": "policy_wf", "input": "How long do refunds take?"})
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


def test_cached_workflow_picks_up_skill_update(client):
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

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        wf1 = _get_workflow("skill_wf", db)
        assert "Say hello warmly." in wf1.steps[0].agents[0].backstory

        client.put(
            "/api/config/skills/greeting",
            json={"description": "How to greet", "instructions": "Say hello formally.", "tools": []},
        )

        wf2 = _get_workflow("skill_wf", db)
        assert "Say hello formally." in wf2.steps[0].agents[0].backstory
        assert wf2 is not wf1
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

    def counting_load_skills(db, org_id=None):
        calls.append(1)
        return original(db, org_id)

    monkeypatch.setattr(backend_main, "load_skills", counting_load_skills)

    from ui.backend.main import _get_workflow
    from ui.backend.db_session import get_db as real_get_db

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        _get_workflow("cached_wf", db)
        _get_workflow("cached_wf", db)
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

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db = next(db_gen)
    try:
        workflow = _get_workflow("priority_wf", db)
    finally:
        db_gen.close()

    team = workflow.steps[0]
    agent = team.agents[0]
    tool = next(t for t in agent.tools if t.__name__ == "shared_name")
    assert "INLINE" in tool("policy")


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
