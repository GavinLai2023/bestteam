import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine
from ui.backend.db.database import session_factory
from ui.backend.db.dependencies import record_version_dependencies, workflows_referencing
from ui.backend.db.models import KnowledgeBaseRecord, SkillRecord, SkillVersion, WorkflowDependency
from ui.backend.db.skills import publish_skill_version
from ui.backend.db.workflows import publish_workflow_version
from ui.backend.skills import load_skills


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    session = session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _config(agents):
    return {"name": "wf", "agents": agents}


def test_records_skill_and_standalone_kb_deps(db):
    db.add(SkillRecord(id=1, name="greet", org_id=7, config={}))
    db.add(KnowledgeBaseRecord(id=2, name="returns_policy", org_id=7, config={}))
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["greet"],
                         "tools": ["returns_policy", "http_get"]}]),
    )
    db.commit()
    deps = {
        (d.resource_kind, d.resource_name, d.resource_id)
        for d in db.query(WorkflowDependency).filter_by(workflow_version_id=version.id)
    }
    # http_get is a built-in tool, not a standalone KB -> no row.
    assert deps == {("skill", "greet", 1), ("knowledge_base", "returns_policy", 2)}
    skill_dep = db.query(WorkflowDependency).filter_by(resource_kind="skill").one()
    assert db.get(SkillVersion, skill_dep.resource_version_id).config == {}


def test_org_skill_shadows_platform_builtin(db):
    db.add(SkillRecord(id=1, name="triage", org_id=None, config={}))  # platform built-in
    db.add(SkillRecord(id=2, name="triage", org_id=7, config={}))     # org override
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).one()
    assert (row.resource_kind, row.resource_id) == ("skill", 2)  # org row wins


def test_platform_builtin_skill_resolves_for_org_workflow(db):
    db.add(SkillRecord(id=5, name="triage", org_id=None, config={}))
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).one()
    assert row.resource_id == 5


def test_inline_kb_is_not_a_standalone_dependency(db):
    config = {
        "name": "wf",
        "knowledge_bases": [{"name": "faq", "path": "./faq"}],
        "agents": [{"name": "a", "skills": [], "tools": ["faq"]}],
    }
    _rec, version = publish_workflow_version(db, org_id=7, name="wf", config=config)
    db.commit()
    assert db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).count() == 0


def test_inline_kb_shadows_same_named_standalone_kb(db):
    # A standalone KB and an inline KB share the name "faq". At runtime the
    # inline KB wins (the loader builds inline KBs into tool_lookup after the
    # standalone ones), so the workflow does NOT depend on the standalone KB:
    # no knowledge_base dep row, and the standalone KB stays deletable.
    db.add(KnowledgeBaseRecord(id=3, name="faq", org_id=7, config={}))
    db.commit()
    config = {
        "name": "wf",
        "knowledge_bases": [{"name": "faq", "path": "./faq"}],
        "agents": [{"name": "a", "skills": [], "tools": ["faq"]}],
    }
    _rec, version = publish_workflow_version(db, org_id=7, name="wf", config=config)
    db.commit()
    kb_rows = db.query(WorkflowDependency).filter_by(
        workflow_version_id=version.id, resource_kind="knowledge_base"
    ).all()
    assert kb_rows == []
    assert workflows_referencing(db, kind="knowledge_base", resource_id=3) == []


def test_mixed_type_refs_are_dropped_not_fatal(db):
    # A legacy config with a non-string ref (["greet", 1]) must not break the
    # sorted() walk in record_version_dependencies; the string ref still records.
    db.add(SkillRecord(id=1, name="greet", org_id=7, config={}))
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["greet", 1], "tools": [2]}]),
    )
    db.commit()
    rows = {
        (d.resource_kind, d.resource_name, d.resource_id)
        for d in db.query(WorkflowDependency).filter_by(workflow_version_id=version.id)
    }
    assert rows == {("skill", "greet", 1)}


def test_post_deploy_org_override_does_not_rewrite_pinned_dependency(db):
    platform, _ = publish_skill_version(
        db, org_id=None, name="triage",
        config={"name": "triage", "instructions": "platform", "tools": []},
    )
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).one()
    pinned_version_id = row.resource_version_id

    org_skill, _ = publish_skill_version(
        db, org_id=7, name="triage",
        config={"name": "triage", "instructions": "org override", "tools": []},
    )
    db.commit()

    db.refresh(row)
    assert (row.resource_id, row.resource_version_id) == (platform.id, pinned_version_id)
    assert load_skills(db, 7, workflow_version_id=version.id)["triage"].instructions == "platform"
    assert workflows_referencing(db, kind="skill", resource_id=platform.id) == ["wf"]
    assert workflows_referencing(db, kind="skill", resource_id=org_skill.id) == []


def test_redeploy_after_org_override_pins_override(db):
    platform, _ = publish_skill_version(
        db, org_id=None, name="triage",
        config={"name": "triage", "instructions": "platform", "tools": []},
    )
    db.commit()
    publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    org_skill, org_version = publish_skill_version(
        db, org_id=7, name="triage",
        config={"name": "triage", "instructions": "org", "tools": []},
    )
    db.commit()
    _head, v2 = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=v2.id).one()
    assert (row.resource_id, row.resource_version_id) == (org_skill.id, org_version.id)
    assert workflows_referencing(db, kind="skill", resource_id=platform.id) == []
    assert workflows_referencing(db, kind="skill", resource_id=org_skill.id) == ["wf"]


def test_workflows_referencing_matches_current_version_only(db):
    db.add(SkillRecord(id=1, name="greet", org_id=7, config={}))
    db.commit()
    publish_workflow_version(
        db, org_id=7, name="team-a",
        config=_config([{"name": "a", "skills": ["greet"], "tools": []}]),
    )
    db.commit()
    assert workflows_referencing(db, kind="skill", resource_id=1) == ["team-a"]
    assert workflows_referencing(db, kind="skill", resource_id=999) == []
