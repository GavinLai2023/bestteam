"""Typed skill/KB dependency records for published workflow versions (P1-04).

`record_version_dependencies` materializes, at deploy, one row per skill/standalone
KB a version depends on; `workflows_referencing` answers the reverse "which deployed
teams' current version depend on this resource?" that the skill/KB delete guard uses.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import (
    KnowledgeBaseRecord,
    SkillRecord,
    WorkflowDependency,
    WorkflowRecord,
    WorkflowVersion,
)


def _referenced_names(raw: Any) -> tuple[set[str], set[str]]:
    """(skill names, tool names) referenced by raw["agents"], defensively --
    mirrors the loader's `list(refs or [])` normalization; skips malformed rows."""
    skills: set[str] = set()
    tools: set[str] = set()
    agents = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(agents, list):
        return skills, tools
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for field, sink in (("skills", skills), ("tools", tools)):
            try:
                sink.update(agent.get(field) or [])
            except TypeError:
                continue
    return skills, tools


def record_version_dependencies(
    db: Session, *, version_id: int, org_id: Optional[int], raw: dict[str, Any]
) -> None:
    """Insert one WorkflowDependency row per skill and per standalone KB `raw`
    references. Resolves resource_id the way the loader resolves names: an org
    skill shadows a same-named platform built-in (org_id IS NULL); KBs are
    org-scoped. A tool name that is not a standalone KB (built-in tool, email
    tool, or inline KB) is not a KB dependency and is skipped. Does NOT commit --
    the caller owns the transaction."""
    skill_names, tool_names = _referenced_names(raw)

    skill_ids: dict[str, int] = {}
    if skill_names:
        rows = (
            db.query(SkillRecord.name, SkillRecord.id, SkillRecord.org_id)
            .filter(
                SkillRecord.name.in_(skill_names),
                or_(SkillRecord.org_id == org_id, SkillRecord.org_id.is_(None)),
            )
            .all()
        )
        # Platform built-ins (org_id IS NULL, sort key False) first so an org's
        # own row (sort key True) overwrites on a name clash.
        for name, sid, _row_org in sorted(rows, key=lambda r: r[2] is not None):
            skill_ids[name] = sid

    kb_ids: dict[str, int] = {}
    if tool_names:
        for name, kid in db.query(KnowledgeBaseRecord.name, KnowledgeBaseRecord.id).filter(
            KnowledgeBaseRecord.name.in_(tool_names),
            KnowledgeBaseRecord.org_id == org_id,
        ):
            kb_ids[name] = kid

    for name in sorted(skill_names):
        db.add(WorkflowDependency(
            workflow_version_id=version_id, resource_kind="skill",
            resource_name=name, resource_id=skill_ids.get(name),
        ))
    for name in sorted(tool_names):
        if name in kb_ids:
            db.add(WorkflowDependency(
                workflow_version_id=version_id, resource_kind="knowledge_base",
                resource_name=name, resource_id=kb_ids[name],
            ))


def workflows_referencing(db: Session, *, kind: str, resource_id: int) -> list[str]:
    """Names of `deployed` workflows whose CURRENT version depends on the resource
    with id `resource_id` (kind = "skill" | "knowledge_base"). Matches by stable
    id, so a platform built-in skill's referencers across every org are found
    without an all-orgs name scan; the current_version_id join reproduces
    "current deployed config only"."""
    q = (
        db.query(WorkflowRecord.name)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRecord.current_version_id)
        .join(WorkflowDependency, WorkflowDependency.workflow_version_id == WorkflowVersion.id)
        .filter(
            WorkflowRecord.status == "deployed",
            WorkflowDependency.resource_kind == kind,
            WorkflowDependency.resource_id == resource_id,
        )
    )
    return sorted({name for (name,) in q})
