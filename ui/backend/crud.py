"""CRUD API for `knowledge_bases`/`skills`/`workflows` + the model catalog.

The "advanced view" referenced by docs/team_builder_methodology.md's Phase 2:
an operator-facing fine-tuning surface for already-deployed configs, reusing
the same `*Spec.to_raw()` / `_build_workflow()` validation as the wizard.

A `workflows` entry is a complete, self-contained `Specification.to_raw()`
dict -- it carries its own `agents:` and `teams:` inline -- and is validated
as a whole via `_build_workflow`, exactly like the wizard's Specification
stage. `knowledge_bases` and `skills` are the only standalone records a
workflow resolves by name (via `knowledge_bases.py::load_knowledge_base_tools`
and `skills.py::load_skills`).

Standalone `agents`/`teams` CRUD used to live here too, but nothing ever
consumed those records: `_build_workflow` takes only `extra_tools`/
`extra_skills`, so they could never reach a running workflow. Both tables
were empty in every deployment, so the routes were removed; the models
remain in `db/models.py`.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict, Optional, Type

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from bestteam import KnowledgeBaseSpec, SkillSpec
from bestteam.core.loader import _build_workflow
from bestteam.exceptions import BestTeamError
from bestteam.tools import REGISTRY

from .auth_api import get_current_admin, get_current_user
from .db.model_catalog import delete_entry, get_entry, list_entries, upsert_entry
from .deploy_validation import find_email_egress_conflicts, validate_agent_models
from .db.models import (
    BuilderSession,
    IngestionJob,
    KnowledgeBaseRecord,
    Organization,
    Run,
    ShareLink,
    ShareMessage,
    ShareSession,
    SkillRecord,
    SkillVersion,
    User,
    WorkflowDependency,
    WorkflowRecord,
    WorkflowVersion,
    iso_utc,
)
from .db.dependencies import workflows_referencing
from .db.share_links import count_active_share_links
from .db.skills import publish_skill_version
from .db.orgs import get_org_by_name, list_orgs
from .db.workflows import publish_workflow_version
from .email_tools import load_email_tools, resolve_agent_tool_sets
from .db_session import get_db
from .ingestion import job_status_payload
from .knowledge_bases import (
    _invalidate_workflow_cache,
    _reject_builtin_kb_name,
    check_path_traversal,
    checked_contained_cache_path,
    delete_knowledge_base,
    kb_name_collisions,
    load_knowledge_base_tools,
    upload_knowledge_base,
)
from .component_lock import component_mutation_lock
from .skills import DEFAULT_SKILLS, load_skills

# Seeded platform built-in skills can't be deleted -- bundled YAML workflows may
# depend on them (F4). Names only; the check applies to the platform tier.
_BUILTIN_SKILL_NAMES = frozenset(s.name for s in DEFAULT_SKILLS)

# Used as `_build_workflow`'s `source` for relative knowledge-base paths and
# the default workflow name -- same directory as the YAML demo workflows.
_WORKFLOWS_DIR = Path(__file__).parent / "workflows"

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(get_current_admin)])


@router.get("/orgs")
def list_organizations(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
    """Organizations, for the Advanced page's org selector.

    Read-only on purpose: orgs are provisioned out-of-band by the operator
    CLI (`python -m ui.backend.admin create-org`), so this adds no
    self-service provisioning surface -- it only lets an admin target an
    existing org on the `?org=`-scoped item routes below.
    """
    return [{"name": org.name, "display_name": org.display_name} for org in list_orgs(db)]


@router.get("/tools")
def list_tools() -> list[Dict[str, Any]]:
    """The built-in tools an agent or skill can reference by name.

    Read-only: tools are Python functions in `bestteam.tools.REGISTRY`, not
    config rows. Their docstrings are already written as the LLM-facing tool
    description (that's what the loader passes to the model), so they serve
    as the reference text here too.
    """
    return [
        {"name": name, "description": inspect.getdoc(fn) or ""}
        for name, fn in sorted(REGISTRY.items())
    ]


def _validate_kb_paths(kb_config: Dict[str, Any]) -> None:
    """Constrain caller-supplied KB paths to application-owned roots (CR-001).

    The `/api/config/knowledge_bases` and inline-`knowledge_bases` API
    boundaries accept caller-supplied `path`/`cache_path` strings. An absolute
    or `..`-traversing `cache_path` is a server-file *write* primitive (the
    vector KB's `_save_embedding_cache()` does `os.replace(tmp, cache_path)`).
    Reject `..`/absolute with a clear 400, then rewrite `cache_path` in place to
    an app-owned `_kb_cache/<filename>` -- so even a clean relative or Windows
    rooted-relative value can only ever write inside that subdir, never over a
    workflow YAML or outside the app roots. Absolute `local_folder` `path`s stay
    allowed (the documented "point at a folder you manage yourself" feature).
    """
    path = kb_config.get("path")
    if isinstance(path, str):
        check_path_traversal(path)
    cache_path = kb_config.get("cache_path")
    if isinstance(cache_path, str):
        kb_config["cache_path"] = checked_contained_cache_path(cache_path)


def _resolve_org_id(db: Session, org: Optional[str], *, allow_platform: bool) -> Optional[int]:
    """Resolve an admin request's `?org=<name>` to an org id.

    Omitted `org` means the platform tier (org_id NULL) where allowed
    (skills' built-ins); otherwise it's an error -- admin mutations must
    target an explicit org so a multi-org operator can't edit the wrong
    customer by accident.
    """
    if org is None or org == "":
        if allow_platform:
            return None
        raise HTTPException(
            status_code=422,
            detail="Query parameter 'org' is required (the organization the item belongs to)",
        )
    record = get_org_by_name(db, org)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown organization '{org}'")
    return record.id


def _org_name_map(db: Session) -> Dict[int, str]:
    return {o.id: o.name for o in db.query(Organization).all()}


def _skill_version_number(db: Session, item: SkillRecord) -> Optional[int]:
    if item.current_version_id is None:
        return None
    version = db.get(SkillVersion, item.current_version_id)
    return version.version_number if version is not None else None


def _make_component_router(name: str, record_cls: Type, spec_cls: Type[BaseModel]) -> APIRouter:
    sub = APIRouter(prefix=f"/{name}")
    # Skills have a platform tier (org_id NULL = built-ins visible to every
    # org), so `?org=` may be omitted there; everything else requires it.
    allow_platform = name == "skills"

    @sub.get("")
    def list_items(
        org: Optional[str] = Query(None), db: Session = Depends(get_db)
    ) -> list[Dict[str, Any]]:
        query = db.query(record_cls)
        if org is not None:
            query = query.filter(record_cls.org_id == _resolve_org_id(db, org, allow_platform=False))
        items = query.order_by(record_cls.name).all()
        org_names = _org_name_map(db)
        return [
            {
                "name": item.name,
                "org": org_names.get(item.org_id),
                "config": item.config,
                **(
                    {
                        "version": _skill_version_number(db, item)
                    }
                    if name == "skills" else {}
                ),
            }
            for item in items
        ]

    @sub.get("/{item_name}")
    def get_item(
        item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        org_id = _resolve_org_id(db, org, allow_platform=allow_platform)
        item = db.query(record_cls).filter_by(name=item_name, org_id=org_id).one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown {name[:-1]} '{item_name}'")
        payload = {"name": item.name, "org": org, "config": item.config}
        if name == "skills":
            payload["version"] = _skill_version_number(db, item)
        return payload

    @sub.put("/{item_name}")
    def upsert_item(
        item_name: str,
        config: Dict[str, Any] = Body(...),
        org: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin: User = Depends(get_current_admin),
    ) -> Dict[str, Any]:
        org_id = _resolve_org_id(db, org, allow_platform=allow_platform)
        if name == "knowledge_bases":
            _validate_kb_paths(config)
            _reject_builtin_kb_name(item_name)
        try:
            spec = spec_cls.model_validate({**config, "name": item_name})
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw = spec.to_raw()
        # Skill saves append immutable versions and are serialized with workflow
        # deploy so the deploy pins either the old or new version atomically.
        # Creating an org override deliberately does not rewrite existing team
        # dependencies: only a future redeploy opts that team into the override.
        with component_mutation_lock:
            item = db.query(record_cls).filter_by(name=item_name, org_id=org_id).one_or_none()
            if name == "skills":
                item, version = publish_skill_version(
                    db,
                    org_id=org_id,
                    name=item_name,
                    config=raw,
                    created_by=admin.username,
                )
            else:
                version = None
                if item is None:
                    item = record_cls(name=item_name, config=raw, org_id=org_id)
                    db.add(item)
                else:
                    item.config = raw
            db.commit()
        if name in ("knowledge_bases", "skills"):
            _invalidate_workflow_cache()
        return {
            "name": item_name,
            "org": org,
            "config": raw,
            **({"version": version.version_number} if version is not None else {}),
        }

    @sub.delete("/{item_name}", status_code=204)
    def delete_item(
        item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
    ) -> Response:
        org_id = _resolve_org_id(db, org, allow_platform=allow_platform)
        if name == "knowledge_bases":
            # A KB delete additionally interlocks with in-flight ingestion, so
            # it owns the whole sequence (see knowledge_bases.py). It takes
            # `component_mutation_lock` itself and that lock is NOT reentrant,
            # so this must return before the `with` below -- not run inside it.
            delete_knowledge_base(db, org_id, item_name)
            return Response(status_code=204)
        # Serialize the reference scan + delete against concurrent deploys so a
        # deploy can't add a reference between the scan and the commit (F3).
        with component_mutation_lock:
            item = db.query(record_cls).filter_by(name=item_name, org_id=org_id).one_or_none()
            if item is None:
                raise HTTPException(status_code=404, detail=f"Unknown {name[:-1]} '{item_name}'")
            # A seeded platform built-in skill can't be deleted -- bundled YAML
            # workflows may depend on it (F4). Platform tier only (org_id None).
            if name == "skills" and org_id is None and item_name in _BUILTIN_SKILL_NAMES:
                raise HTTPException(
                    status_code=409,
                    detail=f"'{item_name}' is a built-in skill and can't be deleted.",
                )
            if name == "skills":
                used_by = workflows_referencing(db, kind="skill", resource_id=item.id)
                if used_by:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Can't delete '{item_name}': it's used by deployed team(s): "
                            + ", ".join(used_by)
                            + ". Update or remove those teams first."
                        ),
                    )
                # Retain immutable version snapshots for superseded workflow
                # provenance while detaching them from the deleted library head.
                db.query(SkillVersion).filter_by(skill_id=item.id).update(
                    {SkillVersion.skill_id: None}, synchronize_session=False
                )
                item.current_version_id = None
                db.flush()
            db.delete(item)
            db.commit()
        if name == "skills":
            _invalidate_workflow_cache()
        return Response(status_code=204)

    if name == "skills":
        @sub.get("/{item_name}/versions")
        def list_skill_versions(
            item_name: str,
            org: Optional[str] = Query(None),
            db: Session = Depends(get_db),
        ) -> list[Dict[str, Any]]:
            """Immutable version history for an active skill library head."""
            org_id = _resolve_org_id(db, org, allow_platform=True)
            item = db.query(SkillRecord).filter_by(
                name=item_name, org_id=org_id
            ).one_or_none()
            if item is None:
                raise HTTPException(status_code=404, detail=f"Unknown skill '{item_name}'")
            versions = (
                db.query(SkillVersion)
                .filter_by(skill_id=item.id)
                .order_by(SkillVersion.version_number.desc())
                .all()
            )
            return [
                {
                    "version": version.version_number,
                    "config": version.config,
                    "created_by": version.created_by,
                    "created_at": iso_utc(version.created_at),
                    "current": version.id == item.current_version_id,
                }
                for version in versions
            ]

    return sub


router.include_router(_make_component_router("knowledge_bases", KnowledgeBaseRecord, KnowledgeBaseSpec))
router.include_router(_make_component_router("skills", SkillRecord, SkillSpec))


@router.post("/knowledge_bases/{item_name}/upload")
def upload_knowledge_base_files(
    item_name: str,
    files: list[UploadFile] = File(...),
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    top_k: int = 5,
    org: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    return upload_knowledge_base(
        db, org_id, item_name, files,
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, top_k=top_k,
        created_by=admin.username,
    )


@router.get("/knowledge_bases/{item_name}/ingestion-jobs/{job_id}")
def get_ingestion_job_status(
    item_name: str,
    job_id: int,
    org: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    kb = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge_base '{item_name}'")
    job = db.query(IngestionJob).filter_by(id=job_id, kb_id=kb.id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown ingestion job")
    return job_status_payload(db, job)


_workflows = APIRouter(prefix="/workflows")


@_workflows.get("")
def list_workflow_configs(
    org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> list[Dict[str, Any]]:
    query = db.query(WorkflowRecord)
    if org is not None:
        query = query.filter(WorkflowRecord.org_id == _resolve_org_id(db, org, allow_platform=False))
    items = query.order_by(WorkflowRecord.name).all()
    org_names = _org_name_map(db)
    return [
        {"name": item.name, "org": org_names.get(item.org_id), "status": item.status, "config": item.config}
        for item in items
    ]


@_workflows.get("/{item_name}")
def get_workflow_config(
    item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    item = db.query(WorkflowRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow '{item_name}'")
    return {"name": item.name, "org": org, "status": item.status, "config": item.config}


@_workflows.put("/{item_name}")
def upsert_workflow_config(
    item_name: str,
    config: Dict[str, Any] = Body(...),
    org: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    raw = {**config, "name": item_name}
    # Serialize dependency resolution + the deployed write against a concurrent
    # component delete (F3): either the delete's scan sees this workflow, or this
    # deploy's resolution fails because the resource was already removed.
    with component_mutation_lock:
        try:
            kb_collisions = kb_name_collisions(db, org_id, raw)
            if kb_collisions:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "A knowledge base can't reuse a built-in tool name: "
                        + ", ".join(kb_collisions)
                        + ". Rename the knowledge base."
                    ),
                )
            for kb_config in raw.get("knowledge_bases", []) or []:
                if isinstance(kb_config, dict):
                    _validate_kb_paths(kb_config)
            source = _WORKFLOWS_DIR / f"{item_name}.yaml"
            # Dependencies resolve within the workflow's own org (+ built-in skills).
            extra_tools = {
                **load_knowledge_base_tools(db, raw, source, org_id=org_id),
                **(load_email_tools(db, org_id) if org_id is not None else {}),
            }
            _build_workflow(raw, source=source, extra_tools=extra_tools, extra_skills=load_skills(db, org_id))
        except (KeyError, TypeError, BestTeamError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        model_problems = validate_agent_models(raw, {e.spec for e in list_entries(db)})
        if model_problems:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This team can't be deployed: "
                    + "; ".join(model_problems)
                    + ". Pick a model from the catalog."
                ),
            )

        if org_id is not None:
            egress_problems = find_email_egress_conflicts(
                resolve_agent_tool_sets(db, raw, org_id)
            )
            if egress_problems:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This team can't be deployed: "
                        + "; ".join(egress_problems)
                        + ". Remove the web-access tool, or move that work to a workflow with no mailbox access."
                    ),
                )

        # Attribute an admin-side deploy to the org's own member (schema
        # guarantees at most one) so it shows up on that member's My Teams
        # page instead of being permanently invisible there. Ownership binds
        # to the member's immutable principal_id, never their username (see
        # WorkflowRecord.created_by).
        member = db.query(User).filter_by(org_id=org_id).one_or_none()
        item, _version = publish_workflow_version(
            db, org_id=org_id, name=item_name, config=raw,
            created_by=member.username if member else None,
            owner_principal_id=member.principal_id if member else None,
        )
        db.commit()
        status = item.status
    return {"name": item_name, "org": org, "status": status, "config": raw}


@_workflows.delete("/{item_name}", status_code=204)
def delete_workflow_config(
    item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> Response:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    # Serialize with publish (which appends versions + moves the pointer under the
    # same lock) so a delete can't interleave with a concurrent version publish.
    with component_mutation_lock:
        item = db.query(WorkflowRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown workflow '{item_name}'")
        # Preserve run provenance: refuse to delete while any Run references one
        # of this head's versions, since deletion removes the version history
        # (FK enforcement is off -> no DB cascade) and would leave those runs
        # pointing at rows that no longer exist. A never-run workflow deletes
        # cleanly, taking its (unreferenced) version history with it so no
        # orphaned workflow_versions rows survive the deleted head.
        run_refs = (
            db.query(Run)
            .filter(Run.workflow_version_id.in_(
                db.query(WorkflowVersion.id).filter_by(workflow_id=item.id)
            ))
            .count()
        )
        if run_refs:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Can't delete '{item_name}': {run_refs} run(s) recorded a version "
                    "of it. Deleting would orphan their provenance."
                ),
            )
        active_share_links = count_active_share_links(db, item.id)
        if active_share_links:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Can't delete '{item_name}': {active_share_links} active share "
                    "link(s) point at it. Revoke them first."
                ),
            )
        # Every share_links row still pointing at this workflow is revoked
        # (the guard above already refused if any are active) -- delete them
        # and their dependent sessions/messages too, or they become
        # permanent orphans (FK enforcement is off, so nothing does this for
        # us). SQLite's INTEGER PRIMARY KEY is not AUTOINCREMENT here, so a
        # later workflow reusing this same id could otherwise silently
        # inherit another team's old share links and visitor transcripts
        # (Codex review finding).
        revoked_link_ids = [
            link_id for (link_id,) in db.query(ShareLink.id).filter_by(workflow_id=item.id)
        ]
        if revoked_link_ids:
            session_ids = [
                session_id
                for (session_id,) in db.query(ShareSession.id).filter(
                    ShareSession.share_link_id.in_(revoked_link_ids)
                )
            ]
            if session_ids:
                db.query(ShareMessage).filter(
                    ShareMessage.share_session_id.in_(session_ids)
                ).delete(synchronize_session=False)
                db.query(ShareSession).filter(
                    ShareSession.id.in_(session_ids)
                ).delete(synchronize_session=False)
            db.query(ShareLink).filter(
                ShareLink.id.in_(revoked_link_ids)
            ).delete(synchronize_session=False)
        # Detach any builder sessions that deployed this head so none is left
        # pointing at a deleted workflow. A nulled workflow_id self-heals: the
        # session's next deploy resolve-or-creates a fresh head by (org_id, name)
        # (publish_workflow_version treats a stale/absent workflow_id as a miss).
        db.query(BuilderSession).filter_by(workflow_id=item.id).update(
            {BuilderSession.workflow_id: None}
        )
        version_ids = [
            v for (v,) in db.query(WorkflowVersion.id).filter_by(workflow_id=item.id)
        ]
        if version_ids:
            db.query(WorkflowDependency).filter(
                WorkflowDependency.workflow_version_id.in_(version_ids)
            ).delete(synchronize_session=False)
        db.query(WorkflowVersion).filter_by(workflow_id=item.id).delete()
        db.delete(item)
        db.commit()
    return Response(status_code=204)


router.include_router(_workflows)


class ModelCatalogEntrySpec(BaseModel):
    """Field shape for a `model_catalog` entry (Phase 3)."""

    display_name: str
    description: str = ""
    tier: str = "balanced"
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0


def _model_catalog_entry_to_dict(entry) -> Dict[str, Any]:
    return {
        "spec": entry.spec,
        "display_name": entry.display_name,
        "description": entry.description,
        "tier": entry.tier,
        "input_price_per_1k": entry.input_price_per_1k,
        "output_price_per_1k": entry.output_price_per_1k,
    }


_model_catalog = APIRouter(prefix="/model-catalog")


@_model_catalog.get("")
def list_model_catalog(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
    return [_model_catalog_entry_to_dict(entry) for entry in list_entries(db)]


@_model_catalog.get("/{spec:path}")
def get_model_catalog_entry(spec: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    entry = get_entry(db, spec)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown model catalog entry '{spec}'")
    return _model_catalog_entry_to_dict(entry)


@_model_catalog.put("/{spec:path}")
def upsert_model_catalog_entry(spec: str, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        item = ModelCatalogEntrySpec.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entry = upsert_entry(db, spec, **item.model_dump())
    return _model_catalog_entry_to_dict(entry)


@_model_catalog.delete("/{spec:path}", status_code=204)
def delete_model_catalog_entry(spec: str, db: Session = Depends(get_db)) -> Response:
    if not delete_entry(db, spec):
        raise HTTPException(status_code=404, detail=f"Unknown model catalog entry '{spec}'")
    return Response(status_code=204)


router.include_router(_model_catalog)


# Read-only model-catalog access for ANY authenticated user (not just admins).
# The Team Builder wizard runs as an org member and needs the catalog to pick a
# real model; without it the frontend falls back to a fake model and team
# generation fails. Kept separate from the admin-gated `router` above -- this
# exposes the list only, no CRUD.
public_router = APIRouter(prefix="/api", tags=["model-catalog"], dependencies=[Depends(get_current_user)])


@public_router.get("/model-catalog")
def list_model_catalog_public(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
    return [_model_catalog_entry_to_dict(entry) for entry in list_entries(db)]
