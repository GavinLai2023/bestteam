"""Tests for the `/api/config` CRUD API (Phase 2) -- the "advanced view" for
fine-tuning agents/teams/knowledge_bases/workflows directly."""

from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from ui.backend import crud as backend_crud
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db


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
        token = test_client.post("/api/auth/register", json={"username": "test", "password": "test"}).json()["access_token"]
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_agent_crud_round_trip(client):
    config = {"role": "Support", "goal": "Help customers", "model": "fake:hi", "tools": []}

    create = client.put("/api/config/agents/support_agent", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["role"] == "Support"

    listed = client.get("/api/config/agents")
    assert [item["name"] for item in listed.json()] == ["support_agent"]

    fetched = client.get("/api/config/agents/support_agent")
    assert fetched.status_code == 200
    assert fetched.json()["config"]["goal"] == "Help customers"

    deleted = client.delete("/api/config/agents/support_agent")
    assert deleted.status_code == 204
    assert client.get("/api/config/agents/support_agent").status_code == 404


def test_agent_put_rejects_invalid_shape(client):
    resp = client.put("/api/config/agents/support_agent", json={"role": "Support"})
    assert resp.status_code == 400


def test_team_crud_round_trip(client):
    config = {"agents": ["support_agent"], "mode": "sequential"}

    create = client.put("/api/config/teams/support_team", json=config)
    assert create.status_code == 200
    assert create.json()["config"]["agents"] == ["support_agent"]

    assert client.get("/api/config/teams/support_team").status_code == 200
    assert client.delete("/api/config/teams/support_team").status_code == 204


def test_knowledge_base_put_omits_vector_only_fields_for_local_folder(client):
    config = {"path": "./docs", "type": "local_folder", "embedding_model": "fake:8"}

    resp = client.put("/api/config/knowledge_bases/docs", json=config)

    assert resp.status_code == 200
    assert "embedding_model" not in resp.json()["config"]


def test_knowledge_base_put_rejects_name_with_spaces(client):
    config = {"path": "./docs", "type": "local_folder"}

    resp = client.put("/api/config/knowledge_bases/bad name", json=config)

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

    resp = client.put("/api/config/knowledge_bases/evil", json=config)

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_knowledge_base_put_rejects_traversal_in_cache_path(client):
    config = {
        "path": "./docs",
        "type": "vector",
        "embedding_model": "fake:8",
        "cache_path": "../../evil.json",
    }

    resp = client.put("/api/config/knowledge_bases/evil", json=config)

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_knowledge_base_put_rejects_traversal_in_path(client):
    config = {"path": "../../../../etc", "type": "local_folder"}

    resp = client.put("/api/config/knowledge_bases/evil", json=config)

    assert resp.status_code == 400
    assert "path" in resp.json()["detail"]


def test_knowledge_base_put_still_allows_absolute_local_folder_path(client, tmp_path):
    # Option A deliberately keeps the documented "point at a folder you manage
    # yourself" feature: an absolute local_folder path (no traversal) is fine.
    docs_dir = tmp_path / "client_docs"
    docs_dir.mkdir()
    config = {"path": str(docs_dir), "type": "local_folder"}

    resp = client.put("/api/config/knowledge_bases/ok", json=config)

    assert resp.status_code == 200


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

    resp = client.put("/api/config/workflows/evil_wf", json=workflow_config)

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_unknown_agent_returns_404(client):
    assert client.get("/api/config/agents/does-not-exist").status_code == 404
    assert client.delete("/api/config/agents/does-not-exist").status_code == 404


def test_reupload_replaces_files_and_drops_omitted_ones(client, tmp_path):
    # CR-008: re-uploading must replace the KB's files wholesale -- a file
    # present before but omitted from the new upload must not linger.
    uploads = tmp_path / "knowledge_base_uploads"
    client.post(
        "/api/config/knowledge_bases/kb/upload",
        files=[
            ("files", ("doc1.txt", b"first document content", "text/plain")),
            ("files", ("doc2.txt", b"second document content", "text/plain")),
        ],
    )
    resp = client.post(
        "/api/config/knowledge_bases/kb/upload",
        files=[("files", ("doc3.txt", b"third document content", "text/plain"))],
    )

    assert resp.status_code == 200
    live = uploads / "kb"
    assert sorted(p.name for p in live.iterdir()) == ["doc3.txt"]


def test_failed_reupload_preserves_prior_kb(client, tmp_path):
    # CR-008: a failed re-upload must leave the previously-valid KB directory
    # AND its DB record intact -- not rmtree the live directory on error.
    uploads = tmp_path / "knowledge_base_uploads"
    client.post(
        "/api/config/knowledge_bases/kb/upload",
        files=[("files", ("doc1.txt", b"good original content", "text/plain"))],
    )

    from bestteam.exceptions import ConfigurationError

    with patch.object(backend_crud, "LocalFolderKnowledgeBase", side_effect=ConfigurationError("bad upload")):
        resp = client.post(
            "/api/config/knowledge_bases/kb/upload",
            files=[("files", ("doc2.txt", b"new content that fails validation", "text/plain"))],
        )

    assert resp.status_code == 400
    live = uploads / "kb"
    assert sorted(p.name for p in live.iterdir()) == ["doc1.txt"]  # prior KB preserved
    assert client.get("/api/config/knowledge_bases/kb").status_code == 200  # DB record preserved


def test_deleting_knowledge_base_invalidates_workflow_cache(client, tmp_path):
    # CR-005: deleting a KB must drop any cached workflow that might embed it --
    # the global max(updated_at) freshness key does not change on a delete.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("hello", encoding="utf-8")
    client.put("/api/config/knowledge_bases/kb1", json={"path": str(docs_dir), "type": "local_folder"})
    backend_main._workflow_cache["cached_wf"] = ("stale-workflow", "key")

    assert client.delete("/api/config/knowledge_bases/kb1").status_code == 204

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

    client.put("/api/config/knowledge_bases/kb1", json={"path": str(docs_dir), "type": "local_folder"})

    assert backend_main._workflow_cache == {}


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
        "/api/config/knowledge_bases/kb/upload",
        files=[("files", ("doc1.txt", b"good original content", "text/plain"))],
    )

    with patch("sqlalchemy.orm.Session.commit", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError, match="db down"):
            client.post(
                "/api/config/knowledge_bases/kb/upload",
                files=[("files", ("doc2.txt", b"replacement content", "text/plain"))],
            )

    live = uploads / "kb"
    assert sorted(p.name for p in live.iterdir()) == ["doc1.txt"]  # prior files restored
    assert client.get("/api/config/knowledge_bases/kb").status_code == 200  # record intact


def test_backup_is_preserved_when_restore_fails(client, tmp_path):
    # CR-008: if rollback can't restore the prior KB, the backup dir (the last
    # valid copy) must be preserved for manual recovery, not silently deleted.
    uploads = tmp_path / "knowledge_base_uploads"
    client.post(
        "/api/config/knowledge_bases/kb/upload",
        files=[("files", ("doc1.txt", b"good original content", "text/plain"))],
    )

    real_replace = backend_crud.os.replace

    def flaky_replace(src, dst):
        if ".backup_" in str(src):  # fail specifically on the restore step
            raise OSError("cannot restore backup")
        return real_replace(src, dst)

    with patch.object(backend_crud.os, "replace", flaky_replace), \
         patch("sqlalchemy.orm.Session.commit", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError, match="db down"):
            client.post(
                "/api/config/knowledge_bases/kb/upload",
                files=[("files", ("doc2.txt", b"replacement content", "text/plain"))],
            )

    backups = list(uploads.glob(".backup_kb_*"))
    assert backups, "backup holding the last valid copy must be preserved"
    assert (backups[0] / "doc1.txt").exists()


def test_upload_creates_queryable_local_folder_kb(client):
    files = [
        ("files", ("doc1.txt", b"The refund policy allows returns within 30 days.", "text/plain")),
        ("files", ("doc2.md", b"# Shipping\nStandard shipping takes 5-7 business days.", "text/markdown")),
    ]
    resp = client.post("/api/config/knowledge_bases/support_docs/upload", files=files)

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "support_docs"
    assert body["file_count"] == 2
    assert body["chunk_count"] >= 2
    assert body["config"]["type"] == "local_folder"
    assert "knowledge_base_uploads" in body["config"]["path"]

    get_resp = client.get("/api/config/knowledge_bases/support_docs")
    assert get_resp.status_code == 200


def test_upload_rejects_name_with_spaces_before_writing_files(client):
    from ui.backend.crud import _KB_UPLOADS_DIR

    files = [("files", ("doc1.txt", b"some content here for parsing", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/bad name/upload", files=files)

    assert resp.status_code == 400
    assert not (_KB_UPLOADS_DIR / "bad name").exists()


def test_upload_rejects_too_many_files(client):
    files = [("files", (f"doc{i}.txt", b"x", "text/plain")) for i in range(31)]
    resp = client.post("/api/config/knowledge_bases/too_many/upload", files=files)
    assert resp.status_code == 413


def test_upload_rejects_oversized_file(client):
    big = b"x" * (30 * 1024 * 1024 + 1)
    files = [("files", ("big.txt", big, "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/too_big/upload", files=files)
    assert resp.status_code == 413


def test_upload_sanitizes_path_traversal_filename(client):
    from ui.backend.crud import _KB_UPLOADS_DIR

    files = [("files", ("../../evil.txt", b"some content here for parsing", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/traversal_kb/upload", files=files)

    assert resp.status_code == 200
    upload_dir = _KB_UPLOADS_DIR / "traversal_kb"
    assert (upload_dir / "evil.txt").is_file()
    assert not (_KB_UPLOADS_DIR.parent / "evil.txt").exists()


def test_upload_rejects_filename_with_no_basename(client):
    files = [("files", ("..", b"some content here for parsing", "text/plain"))]
    resp = client.post("/api/config/knowledge_bases/dotdot_kb/upload", files=files)
    assert resp.status_code == 400


def test_upload_rejects_unparseable_file_and_cleans_up(client):
    files = [("files", ("bad.exe", b"\x00\x01\x02", "application/octet-stream"))]
    resp = client.post("/api/config/knowledge_bases/bad_kb/upload", files=files)
    assert resp.status_code == 400
    get_resp = client.get("/api/config/knowledge_bases/bad_kb")
    assert get_resp.status_code == 404


def test_uploaded_kb_is_queryable_by_a_workflow(client):
    files = [("files", ("policy.txt", b"Refunds are processed within 5 business days of approval.", "text/plain"))]
    upload_resp = client.post("/api/config/knowledge_bases/policy_kb/upload", files=files)
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
    put_resp = client.put("/api/config/workflows/policy_test_wf", json=workflow_config)
    assert put_resp.status_code == 200

    run_resp = client.post("/api/runs", json={"workflow": "policy_test_wf", "input": "How long do refunds take?"})
    assert run_resp.status_code == 200


def test_delete_knowledge_base_removes_uploaded_files(client):
    files = [("files", ("doc.txt", b"some content here", "text/plain"))]
    client.post("/api/config/knowledge_bases/to_delete/upload", files=files)

    from ui.backend.crud import _KB_UPLOADS_DIR

    upload_dir = _KB_UPLOADS_DIR / "to_delete"
    assert upload_dir.is_dir()

    resp = client.delete("/api/config/knowledge_bases/to_delete")
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
    create = client.put("/api/config/workflows/support_workflow", json=_VALID_WORKFLOW_CONFIG)
    assert create.status_code == 200
    body = create.json()
    assert body["status"] == "draft"
    assert body["config"]["name"] == "support_workflow"

    listed = client.get("/api/config/workflows")
    assert [item["name"] for item in listed.json()] == ["support_workflow"]

    fetched = client.get("/api/config/workflows/support_workflow")
    assert fetched.status_code == 200

    assert client.delete("/api/config/workflows/support_workflow").status_code == 204
    assert client.get("/api/config/workflows/support_workflow").status_code == 404


def test_workflow_put_rejects_invalid_config(client):
    bad_config = {**_VALID_WORKFLOW_CONFIG, "teams": [{"name": "support_team", "agents": ["does_not_exist"], "mode": "sequential"}]}

    resp = client.put("/api/config/workflows/support_workflow", json=bad_config)

    assert resp.status_code == 400
    assert "unknown agent" in resp.json()["detail"]


def test_workflow_put_non_list_knowledge_bases_returns_400(client):
    # Regression: a malformed non-list `knowledge_bases` value must be rejected
    # with 400 by the workflow validator, not crash the request with an
    # uncaught TypeError (500). The CR-001 path-guard iteration must run inside
    # the error-handling try block, like the baseline _build_workflow did.
    bad_config = {**_VALID_WORKFLOW_CONFIG, "knowledge_bases": 1}

    resp = client.put("/api/config/workflows/support_workflow", json=bad_config)

    assert resp.status_code == 400


def test_workflow_config_is_runnable_via_get_workflow(client):
    client.put("/api/config/workflows/support_workflow", json=_VALID_WORKFLOW_CONFIG)

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
    resp = client.put("/api/config/workflows/my_workflow", json=config)
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
    resp = client.put("/api/config/workflows/my_workflow", json=config)
    assert resp.status_code == 400
    assert "Unknown skill" in resp.json()["detail"]


def test_load_knowledge_base_tools_builds_only_referenced_kbs(client, tmp_path):
    from sqlalchemy.orm import Session

    from ui.backend.knowledge_bases import load_knowledge_base_tools

    docs_dir = tmp_path / "policy_docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refund processing is completed within 5 business days.")

    client.put(
        "/api/config/knowledge_bases/policy_kb",
        json={"path": str(docs_dir), "type": "local_folder"},
    )
    client.put(
        "/api/config/knowledge_bases/unused_kb",
        json={"path": "./does/not/exist", "type": "local_folder"},
    )

    # Use the same DB the test client's overridden get_db uses.
    from ui.backend.db_session import get_db as real_get_db

    db_gen = backend_main.app.dependency_overrides[real_get_db]()
    db: Session = next(db_gen)
    try:
        raw = {"agents": [{"name": "a", "tools": ["policy_kb", "calculator"]}]}
        tools = load_knowledge_base_tools(db, raw, tmp_path / "wf.yaml")
    finally:
        db_gen.close()

    assert set(tools) == {"policy_kb"}
    assert "Refund" in tools["policy_kb"]("refund processing")


def test_workflow_put_resolves_standalone_knowledge_base_by_name(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")
    client.put("/api/config/knowledge_bases/policy_kb", json={"path": str(docs_dir), "type": "local_folder"})

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
    resp = client.put("/api/config/workflows/policy_wf", json=workflow_config)

    assert resp.status_code == 200


def test_run_resolves_standalone_knowledge_base_by_name(client, tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds are processed within 5 business days.")
    client.put("/api/config/knowledge_bases/policy_kb", json={"path": str(docs_dir), "type": "local_folder"})

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
    put_resp = client.put("/api/config/workflows/policy_wf", json=workflow_config)
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
        "/api/config/knowledge_bases/policy_kb",
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
    resp = client.put("/api/config/workflows/policy_wf", json=workflow_config)

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
    client.put("/api/config/workflows/skill_wf", json=workflow_config)

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
    client.put("/api/config/workflows/cached_wf", json=workflow_config)

    calls = []
    original = backend_main.load_skills

    def counting_load_skills(db):
        calls.append(1)
        return original(db)

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
    client.put("/api/config/knowledge_bases/shared_name", json={"path": str(standalone_dir), "type": "local_folder"})

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
    resp = client.put("/api/config/workflows/priority_wf", json=workflow_config)
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
    client.put("/api/config/knowledge_bases/broken_kb", json={"path": "/no/such/path", "type": "local_folder"})

    broken_workflow = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi", "tools": ["broken_kb"]}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp = client.put("/api/config/workflows/broken_wf", json=broken_workflow)
    assert resp.status_code == 400
    assert "broken_kb" in resp.json()["detail"]

    unrelated_workflow = {
        "agents": [{"name": "a", "role": "r", "goal": "g", "model": "fake:hi"}],
        "teams": [{"name": "team", "agents": ["a"], "mode": "sequential"}],
        "workflow": {"steps": ["team"]},
    }
    resp2 = client.put("/api/config/workflows/unrelated_wf", json=unrelated_workflow)
    assert resp2.status_code == 200
