from ui.backend.db.database import make_engine, init_db, session_factory
from ui.backend.db.models import WorkflowRecord, WorkflowVersion
from ui.backend.db.workflows import publish_workflow_version, current_version_id


def _db():
    engine = make_engine(":memory:")
    init_db(engine)
    return session_factory(engine)()


def test_first_deploy_creates_v1_and_sets_pointer():
    db = _db()
    record, version = publish_workflow_version(db, org_id=1, name="wf", config={"name": "wf", "v": 1})
    db.commit()
    assert version.version_number == 1
    assert record.current_version_id == version.id
    assert record.config == {"name": "wf", "v": 1}


def test_redeploy_appends_v2_moves_pointer_and_keeps_v1_immutable():
    db = _db()
    record, v1 = publish_workflow_version(db, org_id=1, name="wf", config={"v": 1})
    db.commit()
    v1_id = v1.id
    record2, v2 = publish_workflow_version(db, org_id=1, name="wf", config={"v": 2})
    db.commit()
    assert record2.id == record.id                 # same team head
    assert v2.version_number == 2
    assert record2.current_version_id == v2.id     # pointer moved
    frozen = db.get(WorkflowVersion, v1_id)
    assert frozen.config == {"v": 1}               # v1 untouched
    assert record2.config == {"v": 2}              # mirror is current


def test_redeploy_by_workflow_id_renames_head_in_place():
    db = _db()
    record, _ = publish_workflow_version(db, org_id=1, name="old", config={"v": 1})
    db.commit()
    head_id = record.id
    record2, v2 = publish_workflow_version(
        db, org_id=1, name="new", config={"v": 2}, workflow_id=head_id
    )
    db.commit()
    assert record2.id == head_id
    assert record2.name == "new"
    assert v2.version_number == 2


def test_stale_workflow_id_falls_back_to_resolve_or_create_by_name():
    db = _db()
    record, v2 = publish_workflow_version(
        db, org_id=1, name="wf", config={"v": 1}, workflow_id=999  # nonexistent
    )
    db.commit()
    assert record.id is not None
    assert record.name == "wf"
    assert v2.version_number == 1


def test_current_version_id_returns_pointer_for_deployed_only():
    db = _db()
    record, version = publish_workflow_version(db, org_id=1, name="wf", config={"v": 1})
    db.commit()
    assert current_version_id(db, 1, "wf") == version.id
    assert current_version_id(db, 1, "absent") is None
