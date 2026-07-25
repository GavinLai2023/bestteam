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
    """Insert one WorkflowDependency row per skill and per standalone KB `raw`
    references. Resolves resource_id the way the loader resolves names: an org
    skill shadows a same-named platform built-in (org_id IS NULL); KBs are
    org-scoped. A tool name that is not a standalone KB (built-in tool, email
    tool, or inline KB -- including one whose name collides with a standalone
    KB, since the inline KB shadows it at runtime) is not a KB dependency and is
    skipped. Does NOT commit -- the caller owns the transaction."""
    skill_names, tool_names = _referenced_names(raw)
    kb_tool_names = tool_names - _inline_kb_names(raw)

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
    if kb_tool_names:
        for name, kid in db.query(KnowledgeBaseRecord.name, KnowledgeBaseRecord.id).filter(
            KnowledgeBaseRecord.name.in_(kb_tool_names),
            KnowledgeBaseRecord.org_id == org_id,
        ):
            kb_ids[name] = kid

    for name in sorted(skill_names):
        db.add(WorkflowDependency(
            workflow_version_id=version_id, resource_kind="skill",
            resource_name=name, resource_id=skill_ids.get(name),
        ))
    for name in sorted(kb_tool_names):
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


def _resolve_skill_id(db: Session, *, org_id: Optional[int], name: str) -> Optional[int]:
    """The skill id `load_skills(org_id)` resolves for `name`: the org's own row
    shadows a same-named platform built-in (org_id IS NULL); None if neither."""
    if org_id is not None:
        row = (
            db.query(SkillRecord.id)
            .filter(SkillRecord.name == name, SkillRecord.org_id == org_id)
            .first()
        )
        if row is not None:
            return row[0]
    row = (
        db.query(SkillRecord.id)
        .filter(SkillRecord.name == name, SkillRecord.org_id.is_(None))
        .first()
    )
    return row[0] if row is not None else None


def reconcile_skill_dependencies(db: Session, *, org_id: int, name: str) -> None:
    """Re-point `org_id`'s deployed workflows' current-version skill dep rows for
    `name` to the id resolved now. Called when an org skill is created/updated:
    a workflow deployed against a platform built-in recorded that built-in's id,
    but once the org creates a same-named skill the runtime prefers the org row,
    so the recorded id (and thus the delete guard) would otherwise track the
    stale, shadowed skill. Must run under `component_mutation_lock` so it can't
    interleave with a concurrent deploy's own dependency recording. Does NOT
    commit -- the caller owns the transaction."""
    resolved = _resolve_skill_id(db, org_id=org_id, name=name)
    version_ids = [
        vid
        for (vid,) in db.query(WorkflowRecord.current_version_id).filter(
            WorkflowRecord.org_id == org_id,
            WorkflowRecord.status == "deployed",
            WorkflowRecord.current_version_id.isnot(None),
        )
    ]
    if not version_ids:
        return
    (
        db.query(WorkflowDependency)
        .filter(
            WorkflowDependency.workflow_version_id.in_(version_ids),
            WorkflowDependency.resource_kind == "skill",
            WorkflowDependency.resource_name == name,
        )
        .update({WorkflowDependency.resource_id: resolved}, synchronize_session=False)
    )
