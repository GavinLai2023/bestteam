import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine
from ui.backend.db.database import session_factory
from ui.backend.db.dependencies import (
    reconcile_skill_dependencies,
    record_version_dependencies,
    workflows_referencing,
)
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


def test_reconcile_repoints_dep_to_post_deploy_org_override(db):
    # Deploy referencing "triage" when only the platform built-in (id 1) exists,
    # then the org creates its own "triage" (id 2). The runtime now prefers the
    # org skill, so reconcile must move the dep row off the shadowed built-in --
    # otherwise the delete guard would allow deleting the in-use org skill and
    # keep blocking the now-unused built-in.
    db.add(SkillRecord(id=1, name="triage", org_id=None, config={}))  # platform built-in
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).one()
    assert row.resource_id == 1  # resolved to the built-in at deploy

    db.add(SkillRecord(id=2, name="triage", org_id=7, config={}))  # org override
    db.commit()
    reconcile_skill_dependencies(db, org_id=7, name="triage")
    db.commit()

    assert workflows_referencing(db, kind="skill", resource_id=2) == ["wf"]  # org skill: in use
    assert workflows_referencing(db, kind="skill", resource_id=1) == []      # built-in: freed


def test_reconcile_ignores_other_orgs_and_names(db):
    # Reconcile is scoped to (org_id, name): a same-named skill in another org
    # and a different-named skill in the same org must be untouched.
    db.add(SkillRecord(id=1, name="triage", org_id=None, config={}))
    db.commit()
    _rec, version = publish_workflow_version(
        db, org_id=7, name="wf",
        config=_config([{"name": "a", "skills": ["triage"], "tools": []}]),
    )
    db.commit()
    # An org override in a DIFFERENT org (8) exists; reconciling org 7 must not
    # touch org 7's row on account of it, and there's no org-7 override yet.
    db.add(SkillRecord(id=9, name="triage", org_id=8, config={}))
    db.commit()
    reconcile_skill_dependencies(db, org_id=7, name="triage")
    db.commit()
    row = db.query(WorkflowDependency).filter_by(workflow_version_id=version.id).one()
    assert row.resource_id == 1  # still the built-in (no org-7 override)


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
