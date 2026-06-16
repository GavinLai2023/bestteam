"""Shared helper for loading SkillRecords from the database as SkillSpec instances."""

from __future__ import annotations

from typing import Dict

from sqlalchemy.orm import Session

from bestteam import SkillSpec

from .db.models import SkillRecord


def load_skills(db: Session) -> Dict[str, SkillSpec]:
    """Return all SkillRecords as a name→SkillSpec mapping for use as `extra_skills`."""
    return {
        r.name: SkillSpec.model_validate({**r.config, "name": r.name})
        for r in db.query(SkillRecord).all()
    }
