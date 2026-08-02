"""Immutable skill-version persistence primitives."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import SkillRecord, SkillVersion


def ensure_skill_version(db: Session, record: SkillRecord) -> SkillVersion:
    """Return ``record``'s current immutable version, creating a legacy v1.

    Normal writes go through :func:`publish_skill_version`. The lazy path keeps
    direct ORM inserts, old tests, and databases bootstrapped with ``create_all``
    before Alembic migration safe without weakening deploy-time pinning.
    """
    if record.current_version_id is not None:
        version = db.get(SkillVersion, record.current_version_id)
        if version is not None and version.skill_id == record.id:
            return version

    next_number = (
        db.query(func.max(SkillVersion.version_number))
        .filter(SkillVersion.skill_id == record.id)
        .scalar()
        or 0
    ) + 1
    version = SkillVersion(
        skill_id=record.id,
        version_number=next_number,
        config=dict(record.config),
    )
    db.add(version)
    db.flush()
    record.current_version_id = version.id
    return version


def publish_skill_version(
    db: Session,
    *,
    org_id: Optional[int],
    name: str,
    config: dict[str, Any],
    created_by: Optional[str] = None,
) -> tuple[SkillRecord, SkillVersion]:
    """Append a new immutable version and move the skill head; does not commit."""
    record = db.query(SkillRecord).filter_by(name=name, org_id=org_id).one_or_none()
    if record is None:
        record = SkillRecord(name=name, org_id=org_id, config=config)
        db.add(record)
        db.flush()
        next_number = 1
    else:
        # Ensure legacy data receives v1 before an edit becomes v2.
        current = ensure_skill_version(db, record)
        next_number = current.version_number + 1
        record.config = config

    version = SkillVersion(
        skill_id=record.id,
        version_number=next_number,
        config=config,
        created_by=created_by,
    )
    db.add(version)
    db.flush()
    record.current_version_id = version.id
    return record, version
