"""Deploy primitive: publish a WorkflowRecord's config as an immutable version.

`WorkflowRecord` is the stable team head (unique `(org_id, name)`);
`workflow_versions` is its append-only history. Deploy appends a version, moves
`current_version_id`, and keeps `config` as a mirror of the current version so
every reader stays name-based and unchanged (P1-01/02/03). Callers hold
`component_mutation_lock` and own the commit."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import WorkflowRecord, WorkflowVersion


def publish_workflow_version(
    db: Session,
    *,
    org_id: Optional[int],
    name: str,
    config: dict[str, Any],
    workflow_id: Optional[int] = None,
    created_by: Optional[str] = None,
) -> tuple[WorkflowRecord, WorkflowVersion]:
    """Publish `config` as the next immutable version of a team head, moving its
    current-version pointer. Returns `(record, version)`; does NOT commit.

    `workflow_id` given and found -> that existing head (rename-safe:
    `record.name = name`). Otherwise resolve-or-create the head by
    `(org_id, name)` -- so a stale session pointer (deleted team) recreates
    cleanly, and two sessions deploying the same name converge on one head."""
    record: Optional[WorkflowRecord] = None
    if workflow_id is not None:
        record = db.get(WorkflowRecord, workflow_id)
    if record is not None:
        record.name = name
        record.config = config
        record.status = "deployed"
    else:
        record = (
            db.query(WorkflowRecord).filter_by(name=name, org_id=org_id).one_or_none()
        )
        if record is None:
            record = WorkflowRecord(name=name, config=config, status="deployed", org_id=org_id)
            db.add(record)
        else:
            record.config = config
            record.status = "deployed"

    db.flush()  # need record.id
    next_number = (
        db.query(func.max(WorkflowVersion.version_number))
        .filter_by(workflow_id=record.id)
        .scalar()
        or 0
    ) + 1
    version = WorkflowVersion(
        workflow_id=record.id,
        version_number=next_number,
        config=config,
        created_by=created_by,
    )
    db.add(version)
    db.flush()  # need version.id
    record.current_version_id = version.id
    return record, version


def current_version_id(db: Session, org_id: Optional[int], name: str) -> Optional[int]:
    """The `current_version_id` of a deployed team by `(org_id, name)`, else None."""
    record = (
        db.query(WorkflowRecord)
        .filter_by(org_id=org_id, name=name, status="deployed")
        .one_or_none()
    )
    return record.current_version_id if record else None
