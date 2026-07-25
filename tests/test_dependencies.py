import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine
from ui.backend.db.database import session_factory
from ui.backend.db.dependencies import record_version_dependencies, workflows_referencing
from ui.backend.db.models import KnowledgeBaseRecord, SkillRecord, WorkflowDependency
from ui.backend.db.workflows import publish_workflow_version


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
