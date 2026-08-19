"""Typed skill/KB dependency records for published pipeline versions (P1-04).

`record_version_dependencies` materializes, at deploy, one row per skill/standalone
KB a version depends on. Skill rows pin immutable content versions;
`pipelines_referencing` answers the reverse query used by delete guards.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import (
    KnowledgeBaseRecord,
    PipelineDependency,
    PipelineRecord,
    PipelineVersion,
    SkillRecord,
)
from .skills import ensure_skill_version


def _referenced_names(raw: Any) -> tuple[set[str], set[str]]:
    """(skill names, tool names) referenced by raw["agents"], defensively --
    mirrors the loader's `list(refs or [])` normalization; skips malformed rows.
    Only string references are kept: a legacy config with a non-string entry
    (e.g. ``skills: ["ok", 1]``) must not poison the set and later break the
    `sorted()` walk with a str/int comparison TypeError."""
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
                sink.update(x for x in (agent.get(field) or []) if isinstance(x, str))
            except TypeError:
                continue
    return skills, tools


def _inline_kb_names(raw: Any) -> set[str]:
    """Names of knowledge bases defined inline in `raw["knowledge_bases"]`. At
    runtime an inline KB shadows a same-named standalone KB (the loader builds
    inline KBs into `tool_lookup` after the standalone ones), so a tool name
    satisfied by an inline KB is NOT a standalone-KB dependency."""
    kbs = raw.get("knowledge_bases") if isinstance(raw, dict) else None
    if not isinstance(kbs, list):
        return set()
    return {
        kb["name"]
        for kb in kbs
        if isinstance(kb, dict) and isinstance(kb.get("name"), str)
    }


def record_version_dependencies(
    db: Session, *, version_id: int, org_id: Optional[int], raw: dict[str, Any]
) -> None:
    """Insert one PipelineDependency row per skill and per standalone KB `raw`
    references. Resolves resource_id the way the loader resolves names and pins
    the current immutable SkillVersion: an org
    skill shadows a same-named platform built-in (org_id IS NULL); KBs are
    org-scoped. A tool name that is not a standalone KB (built-in tool, email
    tool, or inline KB -- including one whose name collides with a standalone
    KB, since the inline KB shadows it at runtime) is not a KB dependency and is
    skipped. Does NOT commit -- the caller owns the transaction."""
    skill_names, tool_names = _referenced_names(raw)
    kb_tool_names = tool_names - _inline_kb_names(raw)

    skill_records: dict[str, SkillRecord] = {}
    if skill_names:
        rows = (
            db.query(SkillRecord)
            .filter(
                SkillRecord.name.in_(skill_names),
                or_(SkillRecord.org_id == org_id, SkillRecord.org_id.is_(None)),
            )
            .all()
        )
        # Platform built-ins (org_id IS NULL, sort key False) first so an org's
        # own row (sort key True) overwrites on a name clash.
        for record in sorted(rows, key=lambda r: r.org_id is not None):
            skill_records[record.name] = record

    kb_ids: dict[str, int] = {}
    if kb_tool_names:
        for name, kid in db.query(KnowledgeBaseRecord.name, KnowledgeBaseRecord.id).filter(
            KnowledgeBaseRecord.name.in_(kb_tool_names),
            KnowledgeBaseRecord.org_id == org_id,
        ):
            kb_ids[name] = kid

    for name in sorted(skill_names):
        skill = skill_records.get(name)
        skill_version = ensure_skill_version(db, skill) if skill is not None else None
        db.add(PipelineDependency(
            pipeline_version_id=version_id, resource_kind="skill",
            resource_name=name,
            resource_id=skill.id if skill is not None else None,
            resource_version_id=skill_version.id if skill_version is not None else None,
        ))
    for name in sorted(kb_tool_names):
        if name in kb_ids:
            db.add(PipelineDependency(
                pipeline_version_id=version_id, resource_kind="knowledge_base",
                resource_name=name, resource_id=kb_ids[name],
            ))


def pipelines_referencing(db: Session, *, kind: str, resource_id: int) -> list[str]:
    """Names of `deployed` pipelines whose CURRENT version depends on the resource
    with id `resource_id` (kind = "skill" | "knowledge_base"). Matches by stable
    id, so a platform built-in skill's referencers across every org are found
    without an all-orgs name scan; the current_version_id join reproduces
    "current deployed config only"."""
    q = (
        db.query(PipelineRecord.name)
        .join(PipelineVersion, PipelineVersion.id == PipelineRecord.current_version_id)
        .join(PipelineDependency, PipelineDependency.pipeline_version_id == PipelineVersion.id)
        .filter(
            PipelineRecord.status == "deployed",
            PipelineDependency.resource_kind == kind,
            PipelineDependency.resource_id == resource_id,
        )
    )
    return sorted({name for (name,) in q})
