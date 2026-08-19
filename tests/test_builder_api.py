"""Tests for the builder session state-machine API (Phase 2)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.integration
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from bestteam import AgentSpec, Specification, TeamSpec, PipelineSpec
from helpers import create_user_and_login, get_user_principal_id, make_concurrent_safe_engine
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
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()

    # Two tests in here drive real concurrency on purpose -- the deploy/
    # skill-edit lock snapshot and the delete-during-sandbox-run test -- and
    # both were intermittently failing because `make_engine(":memory:")` gives
    # every Session in the process one shared transaction. See the helper's
    # docstring for why that silently loses writes.
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
    "name": "support_pipeline",
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
    "pipeline": {"steps": ["support_team"]},
}

_INVALID_SPEC = {**_VALID_SPEC, "agents": [{**_VALID_SPEC["agents"][0], "tools": ["does_not_exist"]}]}


def _make_deployable_session(client, *, name="support_pipeline", marker=None):
    """Create a session and store a deployable spec named `name`; `marker`
    (embedded in the agent's backstory) lets two sessions with the same name
    be told apart."""
    spec = {**_VALID_SPEC, "name": name}
    if marker is not None:
        spec = {**spec, "agents": [{**spec["agents"][0], "backstory": marker}]}
    session_id = client.post("/api/builder/sessions", json={"intent_text": name}).json()["id"]
    resp = client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": spec})
    assert resp.status_code == 200, resp.text
    return session_id


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


def test_deploy_stamps_pipeline_with_session_org(client):
    from helpers import get_org_id, open_test_db
    from ui.backend.db.models import PipelineRecord

    session_id = client.post("/api/builder/sessions", json={"intent_text": "bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    assert client.post(f"/api/builder/sessions/{session_id}/deploy").status_code == 200

    with open_test_db() as db:
        record = db.query(PipelineRecord).filter_by(name="support_pipeline").one()
        assert record.org_id == get_org_id()


def test_create_session_starts_in_intent_stage(client):
    resp = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot", "as_is_text": "Email today"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "intent"
    assert body["intent_text"] == "We need a support bot"
    assert body["feedback_history"] == []


def test_session_timestamps_carry_an_explicit_utc_marker(client):
    # A timestamp with no timezone marker (e.g. "2026-08-08T10:27:00") is
    # parsed by JS `Date` as local time, not UTC -- on My Teams that showed
    # the wrong wall-clock time versus Activity's (already-marked) timestamps
    # for the very same run, a several-hour discrepancy depending on the
    # viewer's timezone.
    resp = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"})
    body = resp.json()
    for field in ("created_at", "updated_at"):
        value = body[field]
        assert value.endswith("Z") or "+00:00" in value, f"{field}={value!r} has no explicit UTC marker"


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


def test_combined_session_and_advanced_pipeline_list_is_most_recent_first(client):
    from datetime import datetime

    from helpers import open_test_db
    from ui.backend.db.models import BuilderSession, PipelineRecord

    session_id = client.post(
        "/api/builder/sessions", json={"intent_text": "Older wizard team"}
    ).json()["id"]
    advanced_config = {
        "agents": [{
            "name": "agent", "role": "Assistant", "goal": "Help",
            "model": "fake:hello",
        }],
        "teams": [{"name": "team", "agents": ["agent"], "mode": "sequential"}],
        "pipeline": {"steps": ["team"]},
    }
    assert client.put(
        "/api/config/pipelines/newer_advanced_team?org=default",
        json=advanced_config,
        headers=_admin_headers(client),
    ).status_code == 200

    principal_id = get_user_principal_id()
    with open_test_db() as db:
        db.get(BuilderSession, session_id).updated_at = datetime(2020, 1, 1)
        advanced = db.query(PipelineRecord).filter_by(name="newer_advanced_team").one()
        advanced.updated_at = datetime(2030, 1, 1)
        # An orphan (no-session) pipeline only shows on My Teams for the user
        # who owns it -- simulate an admin deploying this one on behalf of the
        # test client's own user, so this test can focus on ordering.
        advanced.created_by = principal_id
        db.commit()

    sessions = client.get("/api/builder/sessions").json()["sessions"]
    assert sessions[0]["id"] is None
    assert sessions[0]["specification_json"]["name"] == "newer_advanced_team"


def test_list_sessions_excludes_admin_deployed_pipeline_with_no_owner(client):
    # A pipeline with no recorded creator (a legacy pre-migration row, or one
    # deployed to an org before it had a member) must NOT clutter an org
    # member's My Teams list -- that list should only ever show teams the
    # member personally built. It remains runnable from Run a Team (see
    # /api/pipelines' own creator filter, which treats an unowned pipeline as
    # an admin-shared template). A normal CRUD deploy to an org that already
    # has its one member auto-attributes to them (see
    # test_admin_deployed_pipeline_auto_attributes_to_the_orgs_sole_member
    # below), so the no-owner state is simulated directly here rather than
    # via that path.
    raw_pipeline_config = {
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
        "pipeline": {"steps": ["support_team"]},
    }
    resp = client.put(
        "/api/config/pipelines/orphan_team?org=default",
        json=raw_pipeline_config,
        headers=_admin_headers(client),
    )
    assert resp.status_code == 200

    from helpers import open_test_db
    from ui.backend.db.models import PipelineRecord

    with open_test_db() as db:
        record = db.query(PipelineRecord).filter_by(name="orphan_team").one()
        record.created_by = None
        db.commit()

    resp = client.get("/api/builder/sessions")

    assert resp.status_code == 200
    names = [s["specification_json"]["name"] for s in resp.json()["sessions"]]
    assert "orphan_team" not in names


def test_admin_deployed_pipeline_auto_attributes_to_the_orgs_sole_member(client):
    # A CRUD-page deploy to an org that already has its one member (the
    # "one member per org" invariant, ui/backend/db/CLAUDE.md) auto-stamps
    # created_by to that member (crud.py::upsert_pipeline_config) -- so it
    # shows up on their My Teams instead of being permanently invisible
    # there, without needing any manual attribution step.
    raw_pipeline_config = {
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
        "pipeline": {"steps": ["support_team"]},
    }
    resp = client.put(
        "/api/config/pipelines/auto_owned_team?org=default",
        json=raw_pipeline_config,
        headers=_admin_headers(client),
    )
    assert resp.status_code == 200

    resp = client.get("/api/builder/sessions")

    assert resp.status_code == 200
    names = [s["specification_json"]["name"] for s in resp.json()["sessions"]]
    assert "auto_owned_team" in names


def test_pipeline_owned_by_a_stale_username_string_is_not_visible_to_the_current_principal(client):
    # Regression test (Codex review finding): PipelineRecord.created_by must
    # bind to the immutable User.principal_id, not the reusable username --
    # otherwise deleting an account and creating a new one with the same
    # username would let the new account see/run the old account's personal
    # pipelines. A row whose created_by is literally the plain username
    # string "test" (as it would be under the old, vulnerable comparison, or
    # if left over from before this fix) must NOT match the current "test"
    # user's own principal_id (a random, unrelated string).
    raw_pipeline_config = {
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
        "pipeline": {"steps": ["support_team"]},
    }
    resp = client.put(
        "/api/config/pipelines/legacy_owned_team?org=default",
        json=raw_pipeline_config,
        headers=_admin_headers(client),
    )
    assert resp.status_code == 200

    from helpers import open_test_db
    from ui.backend.db.models import PipelineRecord

    with open_test_db() as db:
        record = db.query(PipelineRecord).filter_by(name="legacy_owned_team").one()
        record.created_by = "test"  # a plain username string, not a principal_id
        db.commit()

    resp = client.get("/api/builder/sessions")

    names = [s["specification_json"]["name"] for s in resp.json()["sessions"]]
    assert "legacy_owned_team" not in names


def test_list_sessions_includes_orphan_pipeline_owned_by_this_user(client):
    # An orphan pipeline (no matching BuilderSession) that DOES carry this
    # user's own username as its creator -- e.g. its session was removed out
    # of band -- should still show up, unlike the no-owner case above.
    raw_pipeline_config = {
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
        "pipeline": {"steps": ["support_team"]},
    }
    assert client.put(
        "/api/config/pipelines/owned_orphan_team?org=default",
        json=raw_pipeline_config,
        headers=_admin_headers(client),
    ).status_code == 200

    from helpers import open_test_db
    from ui.backend.db.models import PipelineRecord

    principal_id = get_user_principal_id()
    with open_test_db() as db:
        record = db.query(PipelineRecord).filter_by(name="owned_orphan_team").one()
        record.created_by = principal_id
        db.commit()

    resp = client.get("/api/builder/sessions")

    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    orphan = next(s for s in sessions if s["specification_json"]["name"] == "owned_orphan_team")
    assert orphan["id"] is None
    assert orphan["status"] == "deployed"
    assert orphan["pipeline_id"] is not None


def test_list_sessions_does_not_duplicate_a_deployed_pipeline_that_has_a_session(client):
    session_id = _make_deployable_session(client)
    assert client.post(f"/api/builder/sessions/{session_id}/deploy").status_code == 200

    sessions = client.get("/api/builder/sessions").json()["sessions"]
    matches = [s for s in sessions if s.get("specification_json", {}).get("name") == "support_pipeline"]
    assert len(matches) == 1
    assert matches[0]["id"] == session_id


def test_deployed_session_uses_email_stays_on_pinned_skill_version(client):
    import copy

    admin = _admin_headers(client)
    assert client.put(
        "/api/config/skills/capability",
        json={"instructions": "Plain capability.", "tools": []},
        headers=admin,
    ).status_code == 200
    spec = copy.deepcopy(_VALID_SPEC)
    spec["name"] = "pinned_capability_team"
    spec["agents"][0]["skills"] = ["capability"]
    session_id = client.post(
        "/api/builder/sessions", json={"intent_text": "Pinned capability"}
    ).json()["id"]
    client.post(
        f"/api/builder/sessions/{session_id}/specification",
        json={"specification": spec},
    ).raise_for_status()
    client.post(f"/api/builder/sessions/{session_id}/deploy").raise_for_status()

    # Move the mutable head to an email version. The live team remains on v1.
    assert client.put(
        "/api/config/skills/capability",
        json={"instructions": "Email capability.", "tools": ["email_find"]},
        headers=admin,
    ).status_code == 200

    deployed = client.get(f"/api/builder/sessions/{session_id}").json()
    assert deployed["uses_email"] is False


def test_advanced_deployed_team_uses_email_comes_from_pinned_skill(client):
    admin = _admin_headers(client)
    assert client.put(
        "/api/config/skills/email_capability",
        json={"instructions": "Read mail.", "tools": ["email_find"]},
        headers=admin,
    ).status_code == 200
    spec = {
        "agents": [{
            "name": "agent", "role": "Assistant", "goal": "Handle email",
            "model": "fake:hello", "skills": ["email_capability"],
        }],
        "teams": [{"name": "team", "agents": ["agent"], "mode": "sequential"}],
        "pipeline": {"steps": ["team"]},
    }
    assert client.put(
        "/api/config/pipelines/advanced_email_team?org=default",
        json=spec,
        headers=admin,
    ).status_code == 200
    assert client.put(
        "/api/config/skills/email_capability",
        json={"instructions": "No mail now.", "tools": []},
        headers=admin,
    ).status_code == 200

    from helpers import open_test_db
    from ui.backend.db.models import PipelineRecord

    principal_id = get_user_principal_id()
    with open_test_db() as db:
        record = db.query(PipelineRecord).filter_by(name="advanced_email_team").one()
        record.created_by = principal_id
        db.commit()

    sessions = client.get("/api/builder/sessions").json()["sessions"]
    synthetic = next(
        item
        for item in sessions
        if item["specification_json"]["name"] == "advanced_email_team"
    )
    assert synthetic["id"] is None
    assert synthetic["uses_email"] is True


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
    assert body["specification_json"]["name"] == "support_pipeline"


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


def test_deploy_rejects_agent_with_empty_model(client):
    # AgentSpec.model:str accepts "" (no min-length), so the empty-model case
    # must be caught by the deploy-time model check, not slip through to a run.
    bad_spec = {**_VALID_SPEC, "agents": [{**_VALID_SPEC["agents"][0], "model": ""}]}
    sid = client.post("/api/builder/sessions", json={"intent_text": "bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{sid}/specification", json={"specification": bad_spec})
    resp = client.post(f"/api/builder/sessions/{sid}/deploy")
    assert resp.status_code == 400


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


def test_solution_feedback_pins_every_agent_to_the_customers_chosen_model(client):
    """Customer report: picking a model in the wizard's "Which assistant should
    make this change?" control had no effect on the deployed agents' models --
    the architect assigned each agent whatever it judged best for that role,
    independent of the customer's own pick. The architect call itself may use
    any model internally; every agent in the *resulting* spec must end up on
    the model the customer picked."""
    architect_drafted_spec = Specification(
        name="support_pipeline",
        agents=[
            AgentSpec(
                name="support_agent",
                role="Customer Support Specialist",
                goal="Answer customer questions",
                model="openai:gpt-4o-mini",
            ),
            AgentSpec(
                name="drafting_agent",
                role="Response Drafter",
                goal="Draft replies",
                model="openai:gpt-4o",
            ),
        ],
        teams=[TeamSpec(name="support_team", agents=["support_agent", "drafting_agent"])],
        pipeline=PipelineSpec(steps=["support_team"]),
    )

    class _FakeArchitectChatModel:
        def with_structured_output(self, schema, **kwargs):
            return SimpleNamespace(invoke=lambda messages: architect_drafted_spec)

    session_id = client.post("/api/builder/sessions", json={"intent_text": "handle support email"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    with patch("ui.backend.builder._resolve_model", return_value=_FakeArchitectChatModel()):
        resp = client.post(
            f"/api/builder/sessions/{session_id}/solution",
            json={"feedback": "Make replies friendlier", "model": "deepseek:friendly-assistant"},
        )

    assert resp.status_code == 200
    agents = resp.json()["specification_json"]["agents"]
    assert len(agents) == 2
    assert {a["model"] for a in agents} == {"deepseek:friendly-assistant"}


def test_solution_feedback_with_blank_feedback_skips_architect_and_repins_model(client):
    """The wizard's feedback box is optional -- a customer switching which
    assistant their team uses, with nothing else to describe, must not need to
    invent filler feedback text. That case should keep the current design
    as-is (no architect call, no drift) and just re-pin every agent's model."""
    session_id = client.post("/api/builder/sessions", json={"intent_text": "handle support email"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    with patch("ui.backend.builder._resolve_model", return_value=object()), \
         patch("ui.backend.builder.generate_specification") as mock_generate:
        resp = client.post(
            f"/api/builder/sessions/{session_id}/solution",
            json={"feedback": "   ", "model": "deepseek:friendly-assistant"},
        )

    assert resp.status_code == 200
    mock_generate.assert_not_called()
    body = resp.json()
    assert body["feedback_history"] == []
    agents = body["specification_json"]["agents"]
    assert len(agents) == len(_VALID_SPEC["agents"])
    assert {a["model"] for a in agents} == {"deepseek:friendly-assistant"}


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
    assert run["pipeline"] == "support_pipeline"
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


def test_deploy_is_atomic_across_pipeline_and_session_updates(client, monkeypatch):
    # deploy_session persists a PipelineRecord and then marks the
    # BuilderSession deployed as two separate writes. A failure completing
    # the second must not leave a durably-committed "deployed" PipelineRecord
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
        "/api/config/pipelines/support_pipeline?org=default", headers=_admin_headers(client)
    )
    assert resp.status_code == 404


def test_deploy_mailbox_gate_and_skill_pin_share_one_lock_snapshot(client, monkeypatch):
    import copy
    import threading
    import time

    from helpers import open_test_db
    from ui.backend import builder
    from ui.backend.db.models import SkillRecord, SkillVersion, PipelineDependency, PipelineRecord

    admin = _admin_headers(client)
    assert client.put(
        "/api/config/skills/race_capability",
        json={"instructions": "Initially plain.", "tools": []},
        headers=admin,
    ).status_code == 200
    spec = copy.deepcopy(_VALID_SPEC)
    spec["name"] = "race_team"
    spec["agents"][0]["skills"] = ["race_capability"]
    session_id = client.post(
        "/api/builder/sessions", json={"intent_text": "Race-safe deploy"}
    ).json()["id"]
    client.post(
        f"/api/builder/sessions/{session_id}/specification",
        json={"specification": spec},
    ).raise_for_status()

    gate_entered = threading.Event()
    release_gate = threading.Event()
    original = builder.spec_uses_email

    def blocking_gate(*args, **kwargs):
        if not gate_entered.is_set():
            gate_entered.set()
            assert release_gate.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(builder, "spec_uses_email", blocking_gate)
    results = {}

    def deploy():
        results["deploy"] = client.post(
            f"/api/builder/sessions/{session_id}/deploy"
        ).status_code

    def edit_skill():
        results["edit"] = client.put(
            "/api/config/skills/race_capability",
            json={"instructions": "Now uses email.", "tools": ["email_find"]},
            headers=admin,
        ).status_code

    deploy_thread = threading.Thread(target=deploy)
    deploy_thread.start()
    assert gate_entered.wait(timeout=5)
    edit_thread = threading.Thread(target=edit_skill)
    edit_thread.start()
    try:
        time.sleep(0.3)
        assert "edit" not in results, "skill edit must wait for the deploy snapshot lock"
    finally:
        release_gate.set()

    deploy_thread.join(timeout=10)
    edit_thread.join(timeout=10)
    assert results == {"deploy": 200, "edit": 200}

    with open_test_db() as db:
        pipeline = db.query(PipelineRecord).filter_by(name="race_team").one()
        dependency = db.query(PipelineDependency).filter_by(
            pipeline_version_id=pipeline.current_version_id,
            resource_kind="skill",
            resource_name="race_capability",
        ).one()
        pinned = db.get(SkillVersion, dependency.resource_version_id)
        current = db.query(SkillRecord).filter_by(name="race_capability").one()
        assert pinned.config.get("tools", []) == []
        assert current.config["tools"] == ["email_find"]


def test_redeploy_same_session_keeps_head_and_bumps_version(client):
    """One session deployed twice -> same pipeline_id, versions 1 then 2."""
    from helpers import open_test_db
    from ui.backend.db.models import BuilderSession, PipelineVersion

    session_id = _make_deployable_session(client, name="Acme")

    client.post(f"/api/builder/sessions/{session_id}/deploy").raise_for_status()
    with open_test_db() as db:
        sess = db.get(BuilderSession, session_id)
        head_id = sess.pipeline_id
        assert head_id is not None
        assert db.query(PipelineVersion).filter_by(pipeline_id=head_id).count() == 1

    client.post(f"/api/builder/sessions/{session_id}/deploy").raise_for_status()
    with open_test_db() as db:
        sess2 = db.get(BuilderSession, session_id)
        assert sess2.pipeline_id == head_id  # same head
        assert db.query(PipelineVersion).filter_by(pipeline_id=head_id).count() == 2


def test_two_sessions_same_name_converge_on_one_head_v1_preserved(client):
    """P1-02: two sessions with the same team name deploy to the SAME head;
    the first config survives as v1 (no silent clobber)."""
    from helpers import open_test_db
    from ui.backend.db.models import BuilderSession, PipelineVersion

    s_a = _make_deployable_session(client, name="Dup", marker="A")
    s_b = _make_deployable_session(client, name="Dup", marker="B")

    client.post(f"/api/builder/sessions/{s_a}/deploy").raise_for_status()
    client.post(f"/api/builder/sessions/{s_b}/deploy").raise_for_status()

    with open_test_db() as db:
        head_a = db.get(BuilderSession, s_a).pipeline_id
        head_b = db.get(BuilderSession, s_b).pipeline_id
        assert head_a == head_b and head_a is not None  # one shared head
        versions = (
            db.query(PipelineVersion)
            .filter_by(pipeline_id=head_a)
            .order_by(PipelineVersion.version_number)
            .all()
        )
        assert [v.version_number for v in versions] == [1, 2]  # both preserved
        assert versions[0].config["agents"][0]["backstory"] == "A"
        assert versions[1].config["agents"][0]["backstory"] == "B"


def test_deploy_persists_pipeline_record_and_marks_session_deployed(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deployed"

    pipelines = client.get("/api/pipelines").json()["pipelines"]
    assert "support_pipeline" in pipelines

    config = client.get(
        "/api/config/pipelines/support_pipeline?org=default", headers=_admin_headers(client)
    ).json()
    assert config["status"] == "deployed"
    assert config["config"]["name"] == "support_pipeline"


def test_deployed_config_preserves_team_display_name(client):
    # Codex review finding: Specification.to_raw() deliberately strips
    # TeamSpec.display_name/friendly_description (it matches the engine
    # loader's minimal shape -- see test_to_raw_strips_friendly_fields_and_
    # matches_loader_shape in test_specification.py). But GET /api/runs'
    # team_display_name (main.py) reads display_name from this exact
    # persisted config, so a wizard deploy must merge it back in for what
    # gets persisted, without changing to_raw()'s own contract.
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})

    resp = client.post(f"/api/builder/sessions/{session_id}/deploy")
    assert resp.status_code == 200

    config = client.get(
        "/api/config/pipelines/support_pipeline?org=default", headers=_admin_headers(client)
    ).json()["config"]
    assert config["teams"][0]["display_name"] == "Support Team"
    assert config["teams"][0]["friendly_description"] == "The support specialist handles every request."


def test_deployed_pipeline_can_be_run_via_get_pipeline(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "We need a support bot"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    client.post(f"/api/builder/sessions/{session_id}/deploy")

    resp = client.post("/api/runs", json={"pipeline": "support_pipeline", "input": "hi"})

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


def test_delete_never_deployed_session_removes_it(client, tmp_path, monkeypatch):
    from ui.backend import builder as builder_module

    monkeypatch.setattr(builder_module, "_SESSIONS_DIR", tmp_path)

    session_id = client.post("/api/builder/sessions", json={"intent_text": "abandoned idea"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    workspace = tmp_path / session_id
    assert workspace.exists()

    resp = client.delete(f"/api/builder/sessions/{session_id}")
    assert resp.status_code == 204
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 404
    assert not workspace.exists()


def test_delete_session_with_in_flight_sandbox_run_does_not_disrupt_the_run(client, tmp_path, monkeypatch):
    # A test-run dispatches to a worker thread and returns immediately (the
    # run may still be executing when the request completes); the customer
    # can delete the never-deployed draft the instant that response lands.
    # Deletion must succeed regardless, and the already-dispatched run --
    # its `Pipeline` was fully built and handed to the executor before the
    # delete request even arrived, and a `Run` row carries no session_id --
    # must still reach a normal terminal state rather than erroring out from
    # under the just-removed session row/workspace directory.
    from ui.backend import builder as builder_module

    monkeypatch.setattr(builder_module, "_SESSIONS_DIR", tmp_path)

    session_id = client.post("/api/builder/sessions", json={"intent_text": "abandoned idea"}).json()["id"]
    client.post(f"/api/builder/sessions/{session_id}/specification", json={"specification": _VALID_SPEC})
    workspace = tmp_path / session_id
    assert workspace.exists()

    run_id = client.post(f"/api/builder/sessions/{session_id}/test-runs", json={"input": "hi"}).json()["run_id"]

    resp = client.delete(f"/api/builder/sessions/{session_id}")
    assert resp.status_code == 204
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 404
    assert not workspace.exists()

    import time

    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/runs/{run_id}").json()["status"]
        if status != "running":
            break
        time.sleep(0.05)
    assert status == "completed"


def test_delete_deployed_session_is_refused(client):
    session_id = _make_deployable_session(client)
    assert client.post(f"/api/builder/sessions/{session_id}/deploy").status_code == 200

    resp = client.delete(f"/api/builder/sessions/{session_id}")
    assert resp.status_code == 409
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 200


def test_delete_unknown_session_is_404(client):
    assert client.delete("/api/builder/sessions/does-not-exist").status_code == 404


def test_delete_another_orgs_session_is_404(client):
    session_id = client.post("/api/builder/sessions", json={"intent_text": "Org A's bot"}).json()["id"]
    bob_token = create_user_and_login(client, username="bob", org="orgb")
    bob = {"Authorization": f"Bearer {bob_token}"}

    assert client.delete(f"/api/builder/sessions/{session_id}", headers=bob).status_code == 404
    # Still there -- the owning org can still see it (delete didn't leak through).
    assert client.get(f"/api/builder/sessions/{session_id}").status_code == 200


def test_session_dict_exposes_pipeline_id(client):
    session_id = _make_deployable_session(client)
    assert client.get(f"/api/builder/sessions/{session_id}").json()["pipeline_id"] is None

    client.post(f"/api/builder/sessions/{session_id}/deploy")
    assert client.get(f"/api/builder/sessions/{session_id}").json()["pipeline_id"] is not None


def test_with_knowledge_base_catalog_includes_description(db_session):
    db_session.add(KnowledgeBaseRecord(
        name="product_info_kb",
        config={
            "name": "product_info_kb", "path": "/tmp/does-not-matter-here",
            "type": "local_folder", "description": "Product manuals and FAQs",
        },
    ))
    db_session.add(KnowledgeBaseRecord(
        name="undescribed_kb",
        config={"name": "undescribed_kb", "path": "/tmp/does-not-matter-here", "type": "local_folder"},
    ))
    db_session.commit()

    result = _with_knowledge_base_catalog(db_session, "Requirements here.")
    assert "- product_info_kb (type: local_folder): Product manuals and FAQs" in result
    # No description -> no trailing colon, rather than an empty one.
    assert "- undescribed_kb (type: local_folder)\n" in result + "\n"


def test_model_catalog_prompt_hides_embedding_tier():
    """The Solution Architect picks an agent's chat model from this text; an
    embedding entry offered there would produce a team that cannot answer."""
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)

    with Session() as db:
        upsert_entry(db, "openai:gpt-4o-mini", display_name="Quick Assistant", tier="fast")
        upsert_entry(db, "openai:text-embedding-3-small", display_name="Embeddings", tier="embedding")

        with_catalog = _with_model_catalog(db, "Requirements text")

    assert "openai:gpt-4o-mini" in with_catalog
    assert "text-embedding-3-small" not in with_catalog
