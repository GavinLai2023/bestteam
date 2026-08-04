"""Immutable skill versioning and workflow pinning unit tests."""

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import SkillVersion, WorkflowDependency
from ui.backend.db.skills import publish_skill_version
from ui.backend.db.workflows import publish_workflow_version
from ui.backend.skills import load_skills


def _db():
    engine = make_engine(":memory:")
    init_db(engine)
    return session_factory(engine)()


def _workflow_config():
    return {
        "name": "team",
        "agents": [{"name": "a", "skills": ["playbook"], "tools": []}],
    }


def test_skill_save_appends_and_preserves_prior_config():
    db = _db()
    record, v1 = publish_skill_version(
        db,
        org_id=7,
        name="playbook",
        config={"name": "playbook", "instructions": "v1", "tools": []},
    )
    db.commit()
    v1_id = v1.id

    same_record, v2 = publish_skill_version(
        db,
        org_id=7,
        name="playbook",
        config={"name": "playbook", "instructions": "v2", "tools": []},
    )
    db.commit()

    assert same_record.id == record.id
    assert (v1.version_number, v2.version_number) == (1, 2)
    assert same_record.current_version_id == v2.id
    assert db.get(SkillVersion, v1_id).config["instructions"] == "v1"
    assert same_record.config["instructions"] == "v2"


def test_deployed_workflow_loads_pinned_skill_after_head_edit():
    db = _db()
    _record, skill_v1 = publish_skill_version(
        db,
        org_id=7,
        name="playbook",
        config={"name": "playbook", "instructions": "stable v1", "tools": []},
    )
    _head, workflow_v1 = publish_workflow_version(
        db, org_id=7, name="team", config=_workflow_config()
    )
    db.commit()

    publish_skill_version(
        db,
        org_id=7,
        name="playbook",
        config={"name": "playbook", "instructions": "new v2", "tools": []},
    )
    db.commit()

    dependency = db.query(WorkflowDependency).filter_by(
        workflow_version_id=workflow_v1.id,
        resource_kind="skill",
    ).one()
    assert dependency.resource_version_id == skill_v1.id
    assert load_skills(
        db, 7, workflow_version_id=workflow_v1.id
    )["playbook"].instructions == "stable v1"
    assert load_skills(db, 7)["playbook"].instructions == "new v2"


def test_redeploy_is_explicit_upgrade_to_current_skill_version():
    db = _db()
    publish_skill_version(
        db,
        org_id=7,
        name="playbook",
        config={"name": "playbook", "instructions": "v1", "tools": []},
    )
    _head, workflow_v1 = publish_workflow_version(
        db, org_id=7, name="team", config=_workflow_config()
    )
    db.commit()
    _record, skill_v2 = publish_skill_version(
        db,
        org_id=7,
        name="playbook",
        config={"name": "playbook", "instructions": "v2", "tools": []},
    )
    _head, workflow_v2 = publish_workflow_version(
        db, org_id=7, name="team", config=_workflow_config()
    )
    db.commit()

    assert load_skills(db, 7, workflow_version_id=workflow_v1.id)["playbook"].instructions == "v1"
    assert load_skills(db, 7, workflow_version_id=workflow_v2.id)["playbook"].instructions == "v2"
    dep_v2 = db.query(WorkflowDependency).filter_by(
        workflow_version_id=workflow_v2.id,
        resource_kind="skill",
    ).one()
    assert dep_v2.resource_version_id == skill_v2.id


def test_missing_pinned_version_never_falls_forward_to_mutable_head():
    db = _db()
    publish_skill_version(
        db,
        org_id=7,
        name="playbook",
        config={"name": "playbook", "instructions": "head", "tools": []},
    )
    _head, workflow_version = publish_workflow_version(
        db, org_id=7, name="team", config=_workflow_config()
    )
    db.commit()
    dependency = db.query(WorkflowDependency).filter_by(
        workflow_version_id=workflow_version.id,
        resource_kind="skill",
    ).one()
    dependency.resource_version_id = 999_999
    db.commit()

    # Corruption must fail closed at workflow validation (unknown skill), not
    # silently execute whatever mutable content happens to be current.
    assert load_skills(db, 7, workflow_version_id=workflow_version.id) == {}
