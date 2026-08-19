"""Deploy primitive: publish a PipelineRecord's config as an immutable version.

`PipelineRecord` is the stable team head (unique `(org_id, name)`);
`pipeline_versions` is its append-only history. Deploy appends a version, moves
`current_version_id`, and keeps `config` as a mirror of the current version.
Skill dependencies are content-version-pinned during the same transaction. Callers hold
`component_mutation_lock` and own the commit."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .dependencies import record_version_dependencies
from .models import PipelineRecord, PipelineVersion


def publish_pipeline_version(
    db: Session,
    *,
    org_id: Optional[int],
    name: str,
    config: dict[str, Any],
    pipeline_id: Optional[int] = None,
    created_by: Optional[str] = None,
    owner_principal_id: Optional[str] = None,
) -> tuple[PipelineRecord, PipelineVersion]:
    """Publish `config` as the next immutable version of a team head, moving its
    current-version pointer. Returns `(record, version)`; does NOT commit.

    `pipeline_id` given and found *within `org_id`* -> that existing head
    (rename-safe: `record.name = name`). Otherwise resolve-or-create the head by
    `(org_id, name)` -- so a stale session pointer (deleted team), or one that
    names another org's pipeline, recreates cleanly in the caller's own org, and
    two sessions deploying the same name converge on one head. The lookup is
    org-scoped so this primitive can never rename/redeploy another org's record.

    `created_by` (a username) is purely an informational audit label on the
    immutable `PipelineVersion` snapshot -- same role as `SkillVersion.created_by`.
    `owner_principal_id` is the *authorization* value written to
    `PipelineRecord.created_by` (My Teams / run-ownership filtering): it must be
    the creator's immutable `User.principal_id`, never their username, since
    usernames are reusable after account deletion and a username-keyed
    comparison would let a newly created same-named account see/run the
    deleted account's personal pipelines."""
    record: Optional[PipelineRecord] = None
    if pipeline_id is not None:
        record = (
            db.query(PipelineRecord)
            .filter_by(id=pipeline_id, org_id=org_id)
            .one_or_none()
        )
    if record is not None:
        record.name = name
        record.config = config
        record.status = "deployed"
        if owner_principal_id is not None:
            record.created_by = owner_principal_id
    else:
        record = (
            db.query(PipelineRecord).filter_by(name=name, org_id=org_id).one_or_none()
        )
        if record is None:
            record = PipelineRecord(
                name=name, config=config, status="deployed", org_id=org_id, created_by=owner_principal_id
            )
            db.add(record)
        else:
            record.config = config
            record.status = "deployed"
            if owner_principal_id is not None:
                record.created_by = owner_principal_id

    # NB: record.config and version.config below share ONE dict object. That is
    # safe only because deploy never mutates config in place -- the next deploy
    # rebinds record.config to a fresh object, leaving prior versions frozen. Do
    # not mutate record.config / version.config in place, or you corrupt history.
    db.flush()  # need record.id
    next_number = (
        db.query(func.max(PipelineVersion.version_number))
        .filter_by(pipeline_id=record.id)
        .scalar()
        or 0
    ) + 1
    version = PipelineVersion(
        pipeline_id=record.id,
        version_number=next_number,
        config=config,
        created_by=created_by,
    )
    db.add(version)
    db.flush()  # need version.id
    record.current_version_id = version.id
    record_version_dependencies(db, version_id=version.id, org_id=org_id, raw=config)
    return record, version


def current_version_id(db: Session, org_id: Optional[int], name: str) -> Optional[int]:
    """The `current_version_id` of a deployed team by `(org_id, name)`, else None."""
    record = (
        db.query(PipelineRecord)
        .filter_by(org_id=org_id, name=name, status="deployed")
        .one_or_none()
    )
    return record.current_version_id if record else None
