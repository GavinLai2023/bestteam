"""Tests for the builder session state-machine API (Phase 2)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import main as backend_main
from ui.backend.builder import _with_knowledge_base_catalog, _with_model_catalog, _with_skill_catalog
from ui.backend.db import SkillRecord, init_db, make_engine, session_factory
from ui.backend.db.model_catalog import upsert_entry
from ui.backend.db.models import KnowledgeBaseRecord
from ui.backend.db_session import get_db


@pytest.fixture
def db_session():
    from ui.backend.db import init_db, make_engine, session_factory
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_with_skill_catalog_unchanged_when_no_skills(db_session):
    text = "Some requirements."
    assert _with_skill_catalog(db_session, text) == text


def test_with_skill_catalog_appends_skill_list(db_session):
    db_session.add(SkillRecord(
        name="research_skill",
        config={
            "name": "research_skill",
            "description": "Deep research assistant",
            "instructions": "Use web_search to research topics.",
            "tools": ["web_search"],
        },
    ))
    db_session.commit()

    result = _with_skill_catalog(db_session, "Requirements here.")
    assert "research_skill" in result
    assert "Deep research assistant" in result
    assert "web_search" in result


def test_with_knowledge_base_catalog_unchanged_when_no_knowledge_bases(db_session):
    text = "Some requirements."
    assert _with_knowledge_base_catalog(db_session, text) == text


def test_with_knowledge_base_catalog_appends_kb_list(db_session):
    db_session.add(KnowledgeBaseRecord(
        name="product_info_kb",
        config={"name": "product_info_kb", "path": "/tmp/does-not-matter-here", "type": "local_folder"},
    ))
    db_session.commit()

    result = _with_knowledge_base_catalog(db_session, "Requirements here.")
    assert "product_info_kb" in result
    assert "local_folder" in result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
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
        # Builder endpoints are org-user surfaces (get_current_org), and org
        # members can't be admins (CR-030) -- the fixture user is a plain
        # 'default' org member; the few /api/config touches use a separate
        # platform-admin token via _admin_headers().
        token = create_user_and_login(test_client)
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _admin_headers(client):
    # The admin-only /api/config surface needs an org-less platform admin.
    token = create_user_and_login(client, username="op", org=None, admin=True)
    return {"Authorization": f"Bearer {token}"}


_VALID_SPEC = {
    "name": "support_workflow",
    "knowledge_bases": [],
    "agents": [
        {
            "name": "support_agent",
            "role": "Customer Support Specialist",
            "goal": "Answer customer questions",
            "backstory": "",
            "model": "fake:hello",
            "tools": [],
            "display_name": "Support Specialist",
            "friendly_description": "Answers customer questions.",
        }
    ],
    "teams": [
        {
            "name": "support_team",
            "agents": ["support_agent"],
            "mode": "sequential",
            "manager": None,
            "display_name": "Support Team",
            "friendly_description": "The support specialist handles every request.",
        }
    ],
    "workflow": {"steps": ["support_team"]},
}

_INVALID_SPEC = {**_VALID_SPEC, "agents": [{**_VALID_SPEC["agents"][0], "tools": ["does_not_exist"]}]}


def test_builder_sessions_are_org_scoped(client):
    # Fixes the pre-existing hole where any authenticated user could read any
    # session by id: sessions belong to the creator's org; cross-org access
    # is a 404 (existence is not revealed) and lists are scoped.
    session_id = client.post(
        "/api/builder/sessions", json={"intent_text": "Org A's bot"}
    ).json()["id"]

    bob_token = create_user_and_login(client, username="bob", org="orgb")
    bob = {"Authorization": f"Bearer {bob_token}"}

    assert client.get("/api/builder/sessions", headers=bob).json()["sessions"] == []
    assert client.get(f"/api/builder/sessions/{session_id}", headers=bob).status_code == 404
    assert (
        client.post(
            f"/api/builder/sessions/{session_id}/requirements",
            json={"requirements": {}},
            headers=bob,
        ).status_code
        == 404
    )
    assert client.post(f"/api/builder/sessions/{session_id}/deploy", headers=bob).status_code == 404

    # The owning org still sees and lists it.
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 200
    assert [s["id"] for s in client.get("/api/builder/sessions").json()["sessions"]] == [session_id]


def test_specification_generation_with_fake_model_returns_clear_error(client):
    # The exact failure a demo/non-admin user hit: the wizard fell back to a
    # fake model, so generation raised the cryptic "with_structured_output is
    # not implemented". It must now be a clean 400 with an actionable message,
    # not a 502.
    sid = client.post("/api/builder/sessions", json={"intent_text": "handle my email"}).json()["id"]
    resp = client.post(f"/api/builder/sessions/{sid}/specification", json={"model": "fake:ok"})
    assert resp.status_code == 400
    assert "real AI model" in resp.json()["detail"]


def test_deploy_stamps_workflow_with_session_org(client):
    from helpers import get_org_id, open_test_db
    from ui.backend.db.models import WorkflowRecord

    session_id = client.post("/api/builder/sessions", json={"intent_text": "bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    assert client.post(f"/api/builder/sessions/{session_id}/deploy").status_code == 200

    with open_test_db() as db:
        record = db.query(WorkflowRecord).filter_by(name="support_workflow").one()
        assert record.org_id == get_org_id()


def test_create_session_starts_in_intent_stage(client):
    resp = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot", "as_is_text": "Email today"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "intent"
    assert body["intent_text"] == "We need a support bot"
    assert body["feedback_history"] == []


def test_get_session_returns_404_for_unknown_id(client):
    resp = client.get("/api/builder/sessions/does-not-exist")
    assert resp.status_code == 404


def test_list_sessions_returns_most_recently_updated_first(client):
    first = client.post("/api/builder/sessions", json={"intent_text": "First team"}).json()
    second = client.post("/api/builder/sessions", json={"intent_text": "Second team"}).json()

    resp = client.get("/api/builder/sessions")

    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["sessions"]]
    assert ids.index(second["id"]) < ids.index(first["id"])


def test_submit_requirements_with_confirmed_payload(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    requirements = {"summary": "Faster support", "pain_points": ["slow replies"], "goals": ["reply within an hour"]}
    resp = client.post(f"/api/builder/sessions/{session_id}/requirements", json={"requirements": requirements})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "requirements"
    assert body["requirements_json"]["summary"] == "Faster support"


def test_submit_requirements_requires_payload_or_model(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "x"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/requirements", json={})

    assert resp.status_code == 400


def test_submit_specification_with_valid_payload(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "spec"
    assert body["specification_json"]["name"] == "support_workflow"


def test_submit_specification_rejects_invalid_payload(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _INVALID_SPEC})

    assert resp.status_code == 400
    assert "Unknown tool" in resp.json()["detail"]


def test_reject_unsafe_kb_paths_contains_relative_cache_path():
    # CR-001: a spec's relative cache_path is rewritten in place into the
    # app-owned _kb_cache/ subdir, so the contained value is what gets built,
    # stored, and deployed. (Covers the model-generation + stored-spec paths.)
    from bestteam import Specification

    from ui.backend.builder import _reject_unsafe_kb_paths

    spec = Specification.model_validate(
        {
            **_VALID_SPEC,
            "knowledge_bases": [
                {
                    "name": "kb",
                    "path": "/some/dir",
                    "type": "vector",
                    "embedding_model": "fake:8",
                    "cache_path": "deep/nested/embeddings.json",
                }
            ],
        }
    )

    _reject_unsafe_kb_paths(spec)

    assert spec.knowledge_bases[0].cache_path == "_kb_cache/embeddings.json"


def test_submit_specification_rejects_absolute_kb_cache_path(client, tmp_path):
    # CR-001: the builder specification endpoint is a third API boundary that
    # accepts caller-supplied KB paths (via the specification dict). An absolute
    # cache_path on a vector KB is the same server-file *write* primitive
    # guarded at the /api/config boundaries and must be rejected here too --
    # before the spec is validated/built (and thus before any cache write).
    docs_dir = tmp_path / "docs"  # empty: build would fail first, so the guard must run before it
    docs_dir.mkdir()
    spec_with_evil_kb = {
        **_VALID_SPEC,
        "knowledge_bases": [
            {
                "name": "evil_kb",
                "path": str(docs_dir),
                "type": "vector",
                "embedding_model": "fake:8",
                "cache_path": "/tmp/evil.json",
            }
        ],
    }
    session_id = client.post("/api/builder/sessions", json={"intent_text": "x"}).json()["id"]

    resp = client.post(
        f"/api/builder/sessions/{session_id}/specification",
        json={"specification": spec_with_evil_kb},
    )

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def _evil_vector_spec(docs_dir):
    return {
        **_VALID_SPEC,
        "knowledge_bases": [
            {
                "name": "evil_kb",
                "path": str(docs_dir),
                "type": "vector",
                "embedding_model": "fake:8",
                "cache_path": "/tmp/evil.json",
            }
        ],
    }


def _plant_stored_spec(session_id, spec):
    # Simulate a spec reaching the session from an origin other than the
    # guarded user-dict submit (the model-generation path, or a pre-fix
    # record) by writing it straight to the session via the same in-memory DB.
    from ui.backend.db.builder_sessions import update_session

    gen = backend_main.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        update_session(db, session_id, specification_json=spec, status="spec")
    finally:
        gen.close()


def test_deploy_rejects_stored_spec_with_absolute_kb_cache_path(client, tmp_path):
    # CR-001: deploy builds the stored spec via validate_specification, where a
    # vector KB's _save_embedding_cache write fires at construction. deploy must
    # reject an absolute cache_path before build. Empty docs dir => the pre-fix
    # (unguarded) path fails at "no readable documents" with no cache write.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    session_id = client.post("/api/builder/sessions", json={"intent_text": "x"}).json()["id"]
    _plant_stored_spec(session_id, _evil_vector_spec(docs_dir))

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_deploy_rejects_agent_model_not_in_catalog(client):
    bad_spec = {**_VALID_SPEC, "agents": [{**_VALID_SPEC["agents"][0], "model": "openai:gpt-nope"}]}
    sid = client.post("/api/builder/sessions", json={"intent_text": "bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{sid}/specification", json={"specification": bad_spec})
    resp = client.post(f"/api/builder/sessions/{sid}/deploy")
    assert resp.status_code == 400
    assert "openai:gpt-nope" in resp.json()["detail"]


def test_test_run_rejects_stored_spec_with_absolute_kb_cache_path(client, tmp_path):
    # Same as deploy: the sandbox test-run boundary builds the stored spec too.
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    session_id = client.post("/api/builder/sessions", json={"intent_text": "x"}).json()["id"]
    _plant_stored_spec(session_id, _evil_vector_spec(docs_dir))

    resp = client.post(f"/api/builder/sessions/{session_id}/test-runs", json={"input": "hello"})

    assert resp.status_code == 400
    assert "cache_path" in resp.json()["detail"]


def test_solution_feedback_appends_history_and_accepts_revised_spec(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(
        f"/api/builder/sessions/{session_id}/solution",
        json={"feedback": "Looks good", "specification": _VALID_SPEC},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "solution"
    assert len(body["feedback_history"]) == 1
    assert body["feedback_history"][0]["note"] == "Looks good"


def test_test_run_requires_specification(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/test-runs", json={"input": "hello"})

    assert resp.status_code == 400


def test_test_run_executes_validated_specification(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(f"/api/builder/sessions/{session_id}/test-runs", json={"input": "hi"})

    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    session = client.get(f"/api/builder/sessions/{session_id}").json()
    assert session["status"] == "testing"

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["workflow"] == "support_workflow"
    # Sandbox runs record who started them (CR-032) -- user_id stays None
    # (test runs never touch per-user memory), but the initiator is kept.
    assert run["username"] == "test"

    # ...and the initiator is persisted on the runs row, so it survives a
    # registry/process loss. The worker thread writes the row shortly after
    # the POST returns; poll briefly.
    import time

    from helpers import open_test_db
    from ui.backend.db.models import Run as RunRow

    deadline = time.time() + 10
    persisted = None
    while time.time() < deadline:
        with open_test_db() as db:
            row = db.get(RunRow, run_id)
            persisted = row.username if row is not None else None
        if persisted is not None:
            break
        time.sleep(0.05)
    assert persisted == "test"


def test_deploy_requires_specification(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")

    assert resp.status_code == 400


def test_deploy_is_atomic_across_workflow_and_session_updates(client, monkeypatch):
    # deploy_session persists a WorkflowRecord and then marks the
    # BuilderSession deployed as two separate writes. A failure completing
    # the second must not leave a durably-committed "deployed" WorkflowRecord
    # behind -- both belong to one transaction (P1-14).
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    from ui.backend import builder
    monkeypatch.setattr(
        builder, "update_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        client.post(f"/api/builder/sessions/{session_id}/deploy")

    resp = client.get(
        "/api/config/workflows/support_workflow?org=default", headers=_admin_headers(client)
    )
    assert resp.status_code == 404


def test_deploy_persists_workflow_record_and_marks_session_deployed(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deployed"

    workflows = client.get("/api/workflows").json()["workflows"]
    assert "support_workflow" in workflows

    config = client.get(
        "/api/config/workflows/support_workflow?org=default", headers=_admin_headers(client)
    ).json()
    assert config["status"] == "deployed"
    assert config["config"]["name"] == "support_workflow"


def test_deployed_workflow_can_be_run_via_get_workflow(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    client.post(f"/api/builder/sessions/{session_id}/deploy")

    resp = client.post("/api/runs", json={"workflow": "support_workflow", "input": "hi"})

    assert resp.status_code == 200


def test_specification_can_reference_existing_knowledge_base_by_name(client, tmp_path):
    kb_dir = tmp_path / "product_info"
    kb_dir.mkdir()
    (kb_dir / "policy.txt").write_text("Refunds accepted within 7 days.", encoding="utf-8")

    resp = client.put(
        "/api/config/knowledge_bases/product_info_kb?org=default",
        json={"path": str(kb_dir), "type": "local_folder"},
        headers=_admin_headers(client),
    )
    assert resp.status_code == 200

    spec_with_kb = {
        **_VALID_SPEC,
        "agents": [{**_VALID_SPEC["agents"][0], "tools": ["product_info_kb"]}],
    }

    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    resp = client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": spec_with_kb})
    assert resp.status_code == 200

    resp = client.post(f"/api/builder/sessions/{session_id}/test-runs", json={"input": "Can I get a refund?"})
    assert resp.status_code == 200

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")
    assert resp.status_code == 200


def test_with_model_catalog_appends_catalog_text_when_present():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)

    with Session() as db:
        assert _with_model_catalog(db, "Requirements text") == "Requirements text"

        upsert_entry(db, "openai:gpt-4o-mini", display_name="Quick Assistant", tier="fast")

        with_catalog = _with_model_catalog(db, "Requirements text")

    assert with_catalog.startswith("Requirements text\n\n")
    assert "openai:gpt-4o-mini" in with_catalog
    assert "Quick Assistant" in with_catalog
