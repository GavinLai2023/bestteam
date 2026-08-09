"""Builder session state-machine API (Phase 2).

Thin wrappers over `ui/backend/db/builder_sessions.py` plus the Phase 0.5
generation/validation helpers (`bestteam.generate_requirements`,
`bestteam.generate_specification`, `bestteam.validate_specification`). Maps
onto the six-stage methodology in docs/team_builder_methodology.md:
intent -> requirements -> spec -> solution -> testing -> deployed.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from bestteam import Specification, generate_requirements, generate_specification, validate_specification
from bestteam.adapters.langgraph_adapter import _resolve_model
from bestteam.core.knowledge_base import make_knowledge_base_tool
from bestteam.core.loader import _build_knowledge_base
from bestteam.core.requirements import Requirements
from bestteam.exceptions import BestTeamError, ConfigurationError

from .auth_api import get_current_org, get_current_user
from .db.builder_sessions import append_feedback, create_session, delete_session, get_session, list_sessions, update_session
from .db.model_catalog import list_entries, to_prompt_text
from .deploy_validation import validate_agent_models
from .db.models import BuilderSession, KnowledgeBaseRecord, Organization, User, WorkflowRecord, iso_utc
from .db.workflows import publish_workflow_version
from .db_session import get_db
from .component_lock import component_mutation_lock
from .knowledge_bases import (
    check_path_traversal,
    checked_contained_cache_path,
    contain_kb_config_for_load,
    ensure_contained_cache_path_for_source,
    ensure_workflow_cache_paths_for_source,
    kb_name_collisions,
    load_knowledge_base_tools,
    resolve_kb_upload_path,
)
from .db.email_credentials import get_email_credentials
from .email_tools import load_email_tools, spec_uses_email
from .runtime import _executor, registry, run_in_background
from .skills import load_skills

router = APIRouter(prefix="/api/builder/sessions", tags=["builder"], dependencies=[Depends(get_current_user)])

logger = logging.getLogger(__name__)

_SESSIONS_DIR = Path(__file__).parent / "data" / "builder_sessions"

_T = TypeVar("_T")


def _call_model(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a `generate_*`/`_resolve_model` call, translating failures into HTTP errors.

    `BestTeamError` (e.g. an unresolvable model spec, or a Solution Architect
    that couldn't self-correct) becomes a 400 with the original message; any
    other exception (e.g. a real provider call failing without an API key)
    becomes a 502 -- it's the model provider, not the request, that's at fault.
    """
    try:
        return fn(*args, **kwargs)
    except BestTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Model call failed: {exc}") from exc


def _session_to_dict(
    session: BuilderSession, db: Optional[Session] = None, org_id: Optional[int] = None
) -> Dict[str, Any]:
    # `uses_email` tells the wizard UI whether the built team needs a mailbox
    # (so it shows the connect-mailbox step). Computed only when a spec exists
    # and the caller passes its org context; defaults False otherwise.
    uses_email = False
    if db is not None and org_id is not None and session.specification_json:
        spec_raw = session.specification_json
        workflow_version_id = None
        # Once deployed, capability metadata must describe the exact live team,
        # including its pinned skills. The session spec can be stale after an
        # Advanced-page redeploy, and current skill heads can move independently.
        if session.status == "deployed" and session.workflow_id is not None:
            record = (
                db.query(WorkflowRecord)
                .filter_by(id=session.workflow_id, org_id=org_id, status="deployed")
                .one_or_none()
            )
            if record is not None:
                spec_raw = record.config
                workflow_version_id = record.current_version_id
        uses_email = spec_uses_email(
            db,
            spec_raw,
            org_id,
            workflow_version_id=workflow_version_id,
        )
    return {
        "id": session.id,
        "intent_text": session.intent_text,
        "as_is_text": session.as_is_text,
        "requirements_json": session.requirements_json,
        "specification_json": session.specification_json,
        "status": session.status,
        "workflow_id": session.workflow_id,
        "feedback_history": session.feedback_history,
        "uses_email": uses_email,
        "created_at": iso_utc(session.created_at),
        "updated_at": iso_utc(session.updated_at),
    }


def _get_session_or_404(db: Session, session_id: str, org_id: Optional[int] = None) -> BuilderSession:
    """Fetch a session, org-scoped: another org's session is a 404 (existence
    is not revealed). Every subresource route flows through this, so the
    ownership check covers requirements/specification/solution/test-runs/
    deploy in one place."""
    session = get_session(db, session_id)
    if session is None or session.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Unknown builder session '{session_id}'")
    return session


def _source_for(session_id: str) -> Path:
    """A per-session workspace directory, used as `_build_workflow`'s `source`
    (relative knowledge-base paths, default workflow name)."""
    workspace = _SESSIONS_DIR / session_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace / "workflow.yaml"


def _requirements_text(session: BuilderSession) -> str:
    if session.requirements_json:
        return Requirements.model_validate(session.requirements_json).to_prompt()
    parts = [session.intent_text]
    if session.as_is_text:
        parts.append(f"Current process:\n{session.as_is_text}")
    return "\n\n".join(part for part in parts if part)


def _with_model_catalog(db: Session, text: str) -> str:
    """Append the `model_catalog` (if any) so the Solution Architect picks
    `AgentSpec.model` specs by role complexity, per
    docs/team_builder_methodology.md's Phase 3."""
    catalog_text = to_prompt_text(list_entries(db))
    return f"{text}\n\n{catalog_text}" if catalog_text else text


def _with_skill_catalog(db: Session, text: str, org_id: Optional[int] = None) -> str:
    """Append the skills visible to `org_id` (its own + platform built-ins)
    so the Solution Architect can assign them to agents by name, parallel to
    `_with_model_catalog`."""
    skills = load_skills(db, org_id)
    if not skills:
        return text
    lines = ["", "", "Available skills (from the platform's skill library):"]
    for spec in skills.values():
        tools_note = f" (tools: {', '.join(spec.tools)})" if spec.tools else ""
        desc = spec.description if spec.description else (
            spec.instructions[:80] + "..." if len(spec.instructions) > 80 else spec.instructions
        )
        lines.append(f"- {spec.name}: {desc}{tools_note}")
    return text + "\n".join(lines)


def _with_knowledge_base_catalog(db: Session, text: str, org_id: Optional[int] = None) -> str:
    """Append available standalone knowledge bases (if any) so the Solution
    Architect can reference them by name, parallel to `_with_skill_catalog`.

    Returns `text` unchanged when none exist, so the architect sees no
    "Available knowledge bases" section and has nothing to draw a name
    from -- combined with `_ARCHITECT_SYSTEM_PROMPT`'s instruction not to
    invent one, this is what stops it from fabricating a `path`.
    """
    records = db.query(KnowledgeBaseRecord).filter(KnowledgeBaseRecord.org_id == org_id).all()
    if not records:
        return text
    lines = ["", "", "Available knowledge bases (reference by name in an agent's tools, do not redeclare):"]
    for record in records:
        kb_type = record.config.get("type", "local_folder")
        lines.append(f"- {record.name} (type: {kb_type})")
    return text + "\n".join(lines)


def _all_knowledge_base_tools(db: Session, source: Path, org_id: Optional[int] = None) -> Dict[str, Any]:
    """Build a tool for every one of `org_id`'s standalone knowledge bases.

    Used before a Specification exists yet (at generation time), when we
    don't yet know which knowledge bases the architect's agents will
    reference -- unlike `load_knowledge_base_tools`, which filters to a
    known `raw` config's referenced names.
    """
    records = db.query(KnowledgeBaseRecord).filter(KnowledgeBaseRecord.org_id == org_id).all()
    tools: Dict[str, Any] = {}
    for record in records:
        config = resolve_kb_upload_path(contain_kb_config_for_load(record.config))
        ensure_contained_cache_path_for_source(config, source)
        kb = _build_knowledge_base(config, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools


def _reject_unsafe_kb_paths(spec: Specification) -> None:
    """Constrain a spec's KB paths in place before it is built or stored (CR-001).

    Building a vector KB calls `_save_embedding_cache` at construction time, so
    this must run before `validate_specification`/`_build_workflow`. Rejects
    absolute/`..` paths and rewrites each `cache_path` to an app-owned
    `_kb_cache/<filename>`, mutating the spec's KnowledgeBaseSpec objects so the
    contained value is what gets built, stored, and later deployed. Shared by
    every builder boundary that builds or stores a caller-influenced spec:
    specification submit/refine, test-run, and deploy.
    """
    for kb in spec.knowledge_bases:
        if isinstance(kb.path, str):
            check_path_traversal(kb.path)
        if isinstance(kb.cache_path, str):
            kb.cache_path = checked_contained_cache_path(kb.cache_path)


def _prepare_generated_specification(spec: Specification, source: Path) -> None:
    """Contain an untrusted model candidate before SDK validation builds it."""
    try:
        _reject_unsafe_kb_paths(spec)
        ensure_workflow_cache_paths_for_source(spec.to_raw(), source)
    except HTTPException as exc:
        # ``generate_specification`` treats ConfigurationError as feedback for
        # the architect and, after retries, `_call_model` returns it as a 400.
        # Do not let a candidate reach vector-KB construction first (CR-001).
        raise ConfigurationError(str(exc.detail)) from exc


def _validate_spec_payload(
    db: Session,
    payload: Dict[str, Any],
    source: Path,
    extra_skills: Optional[Dict[str, Any]] = None,
    org_id: Optional[int] = None,
) -> Specification:
    try:
        spec = Specification.model_validate(payload)
        _reject_unsafe_kb_paths(spec)
        ensure_workflow_cache_paths_for_source(spec.to_raw(), source)
        extra_tools = {
            **load_knowledge_base_tools(db, spec.to_raw(), source, org_id=org_id),
            **(load_email_tools(db, org_id) if org_id is not None else {}),
        }
        validate_specification(spec, source=source, extra_tools=extra_tools, extra_skills=extra_skills or {})
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return spec


class CreateSessionRequest(BaseModel):
    intent_text: str = ""
    as_is_text: str = ""


class RequirementsRequest(BaseModel):
    requirements: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    feedback: Optional[str] = None


class SpecificationRequest(BaseModel):
    specification: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    feedback: Optional[str] = None


class SolutionRequest(BaseModel):
    feedback: str
    model: Optional[str] = None
    specification: Optional[Dict[str, Any]] = None


class TestRunRequest(BaseModel):
    input: str


@router.post("")
def create_builder_session(
    req: CreateSessionRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Stage 1 (Intent): start a session with the customer's free-text intent/as-is."""
    session = create_session(db, intent_text=req.intent_text, as_is_text=req.as_is_text, org_id=org.id)
    return _session_to_dict(session, db, org.id)


def _synthetic_session_for_workflow(
    record: WorkflowRecord, db: Session, org_id: int
) -> Dict[str, Any]:
    """A My-teams card for a workflow deployed without ever going through the
    wizard (e.g. via the admin Advanced/CRUD page) -- so every deployed team
    is visible there, not just wizard-built ones. `id` stays `None`: there is
    no `BuilderSession` to resume into, so the frontend routes a click
    straight to Run a Team instead of a wizard page."""
    return {
        "id": None,
        "intent_text": record.name,
        "as_is_text": None,
        "requirements_json": None,
        "specification_json": record.config,
        "status": "deployed",
        "workflow_id": record.id,
        "feedback_history": [],
        "uses_email": spec_uses_email(
            db,
            record.config,
            org_id,
            workflow_version_id=record.current_version_id,
        ),
        "created_at": iso_utc(record.created_at),
        "updated_at": iso_utc(record.updated_at),
    }


@router.get("")
def list_builder_sessions(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """List the current user's own builder sessions (most recent first), for
    an "AI teams I've built" list page. A session with status 'deployed' has
    a live WorkflowRecord matching specification_json['name']. A deployed
    workflow with no backing session at all (deployed straight through the
    admin Advanced/CRUD page) gets a synthetic entry too -- but only when its
    `created_by` matches this user's immutable principal_id (never username --
    see WorkflowRecord.created_by), so My Teams shows exactly the teams this
    person built, not every workflow anyone in the org can run (that broader
    "org can run" list is /api/workflows, which treats an unowned workflow as
    an admin-shared template)."""
    sessions = list_sessions(db, org_id=org.id)
    session_dicts = [_session_to_dict(s, db, org.id) for s in sessions]

    session_workflow_ids = {s.workflow_id for s in sessions if s.workflow_id is not None}
    orphan_workflows = (
        db.query(WorkflowRecord)
        .filter(WorkflowRecord.org_id == org.id, WorkflowRecord.status == "deployed")
        .filter(WorkflowRecord.id.notin_(session_workflow_ids))
        .filter(WorkflowRecord.created_by == user.principal_id)
        .all()
    )
    session_dicts.extend(
        _synthetic_session_for_workflow(r, db, org.id) for r in orphan_workflows
    )
    # Both source queries have different ordering semantics; enforce the API's
    # most-recent-first contract after combining them, with stable tie-breakers.
    session_dicts.sort(
        key=lambda item: (
            item["updated_at"],
            item["id"] or "",
            item["workflow_id"] or 0,
        ),
        reverse=True,
    )
    return {"sessions": session_dicts}


@router.get("/{session_id}")
def get_builder_session(
    session_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    return _session_to_dict(_get_session_or_404(db, session_id, org.id), db, org.id)


@router.delete("/{session_id}", status_code=204)
def delete_builder_session(
    session_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> None:
    """Delete a session that was never deployed (`workflow_id IS NULL`) --
    the "abandoned draft" case. A session that has ever gone live has no
    delete path here (see docs/superpowers/specs/2026-07-31-draft-session-deletion-design.md);
    the frontend never offers this for a `workflow_id`-linked session, but
    this guard holds even if the route is called directly."""
    session = _get_session_or_404(db, session_id, org.id)
    if session.workflow_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This team is live -- it can't be deleted from here yet.",
        )
    delete_session(db, session_id)
    try:
        shutil.rmtree(_SESSIONS_DIR / session_id)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning(
            "Failed to remove workspace directory for deleted session %s", session_id, exc_info=True
        )


@router.post("/{session_id}/requirements")
def submit_requirements(
    session_id: str,
    req: RequirementsRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Stage 2 (Requirements): generate (via `model`) or confirm (via `requirements`)
    a structured requirements summary."""
    session = _get_session_or_404(db, session_id, org.id)

    if req.requirements is not None:
        try:
            requirements = Requirements.model_validate(req.requirements)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif req.model is not None:
        chat_model = _call_model(_resolve_model, req.model)
        requirements = _call_model(
            generate_requirements, chat_model, session.intent_text, session.as_is_text, feedback=req.feedback
        )
    else:
        raise HTTPException(status_code=400, detail="Provide either 'requirements' or 'model'")

    if req.feedback:
        append_feedback(db, session_id, {"stage": "requirements", "note": req.feedback})

    session = update_session(db, session_id, requirements_json=requirements.model_dump(), status="requirements")
    return _session_to_dict(session, db, org.id)


@router.post("/{session_id}/specification")
def submit_specification(
    session_id: str,
    req: SpecificationRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Stage 3 (Specification): generate (via `model`) or accept (via `specification`)
    a team design, validated through `_build_workflow` before it's stored."""
    session = _get_session_or_404(db, session_id, org.id)
    source = _source_for(session_id)

    if req.specification is not None:
        spec = _validate_spec_payload(
            db, req.specification, source, extra_skills=load_skills(db, org.id), org_id=org.id
        )
    elif req.model is not None:
        requirements_text = _requirements_text(session)
        if req.feedback:
            requirements_text += f"\n\nCustomer feedback on the previous design:\n{req.feedback}"
        requirements_text = _with_model_catalog(db, requirements_text)
        requirements_text = _with_skill_catalog(db, requirements_text, org.id)
        requirements_text = _with_knowledge_base_catalog(db, requirements_text, org.id)
        chat_model = _call_model(_resolve_model, req.model)
        spec = _call_model(
            generate_specification,
            chat_model,
            requirements_text,
            source=source,
            extra_tools=_all_knowledge_base_tools(db, source, org.id),
            extra_skills=load_skills(db, org.id),
            pre_validate=lambda candidate: _prepare_generated_specification(candidate, source),
        )
    else:
        raise HTTPException(status_code=400, detail="Provide either 'specification' or 'model'")

    if req.feedback:
        append_feedback(db, session_id, {"stage": "specification", "note": req.feedback})

    # Contain KB paths on the stored spec so a model-generated spec (which the
    # user-dict path already contains in _validate_spec_payload) can't persist an
    # uncontained cache_path that test-run/deploy would later build (CR-001).
    _prepare_generated_specification(spec, source)
    session = update_session(db, session_id, specification_json=spec.model_dump(), status="spec")
    return _session_to_dict(session, db, org.id)


@router.post("/{session_id}/solution")
def submit_solution_feedback(
    session_id: str,
    req: SolutionRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Stage 4 (Solution): refine the Specification with customer feedback,
    either by re-running the Solution Architect (`model`) or accepting a
    manually-edited Specification (`specification`)."""
    session = _get_session_or_404(db, session_id, org.id)
    source = _source_for(session_id)

    if req.specification is not None:
        spec = _validate_spec_payload(
            db, req.specification, source, extra_skills=load_skills(db, org.id), org_id=org.id
        )
    elif req.model is not None:
        if session.specification_json is None:
            raise HTTPException(status_code=400, detail="Generate a specification before requesting refinements")
        current = Specification.model_validate(session.specification_json)
        requirements_text = (
            f"{_requirements_text(session)}\n\n"
            f"The current team design is:\n{current.model_dump_json()}\n\n"
            f"Customer feedback on this design:\n{req.feedback}"
        )
        requirements_text = _with_model_catalog(db, requirements_text)
        requirements_text = _with_skill_catalog(db, requirements_text, org.id)
        requirements_text = _with_knowledge_base_catalog(db, requirements_text, org.id)
        chat_model = _call_model(_resolve_model, req.model)
        spec = _call_model(
            generate_specification,
            chat_model,
            requirements_text,
            source=source,
            extra_tools=_all_knowledge_base_tools(db, source, org.id),
            extra_skills=load_skills(db, org.id),
            pre_validate=lambda candidate: _prepare_generated_specification(candidate, source),
        )
    else:
        raise HTTPException(status_code=400, detail="Provide either 'specification' or 'model'")

    append_feedback(db, session_id, {"stage": "solution", "note": req.feedback})
    _prepare_generated_specification(spec, source)  # contain the stored spec's KB paths (CR-001)
    session = update_session(db, session_id, specification_json=spec.model_dump(), status="solution")
    return _session_to_dict(session, db, org.id)


@router.post("/{session_id}/test-runs")
async def create_test_run(
    session_id: str,
    req: TestRunRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> Dict[str, str]:
    """Stage 5 (Testing): run the validated Specification in the sandbox via
    the same `Workflow.stream()`/`RunRegistry` machinery as `/api/runs`."""
    session = _get_session_or_404(db, session_id, org.id)
    if session.specification_json is None:
        raise HTTPException(status_code=400, detail="Generate a specification before testing")

    spec = Specification.model_validate(session.specification_json)
    _reject_unsafe_kb_paths(spec)  # CR-001: guard the stored spec before it is built
    source = _source_for(session_id)
    ensure_workflow_cache_paths_for_source(spec.to_raw(), source)
    extra_tools = {
        **load_knowledge_base_tools(db, spec.to_raw(), source, org_id=org.id),
        **load_email_tools(db, org.id),
    }
    try:
        workflow = validate_specification(
            spec, source=source, extra_tools=extra_tools, extra_skills=load_skills(db, org.id)
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    update_session(db, session_id, status="testing")

    # Sandbox runs carry the org (so their streams are org-guarded like real
    # runs) and record who started them (CR-032), but no user_id -- test runs
    # never touch per-user memory.
    run = registry.create(spec.name, req.input, org_id=org.id, username=user.username)
    _executor.submit(
        run_in_background,
        run.id,
        workflow,
        req.input,
        engine=db.get_bind(),
        org_id=org.id,
        username=user.username,
    )
    return {"run_id": run.id}


@router.post("/{session_id}/deploy")
def deploy_session(
    session_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Stage 6 (Deployment): publish the validated Specification as a new
    immutable version of a `WorkflowRecord` team head (`status=deployed`) so
    `_get_workflow()` picks it up, and link the session to that head
    (`session.workflow_id`) so a redeploy versions the same team (P1-02).
    The version publish and the session update share a single commit (P1-14)."""
    session = _get_session_or_404(db, session_id, org.id)
    if session.specification_json is None:
        raise HTTPException(status_code=400, detail="Generate a specification before deploying")

    spec = Specification.model_validate(session.specification_json)
    _reject_unsafe_kb_paths(spec)  # CR-001: guard the stored spec before it is built/persisted
    kb_collisions = kb_name_collisions(db, org.id, spec.to_raw())
    if kb_collisions:
        raise HTTPException(
            status_code=400,
            detail=(
                "A knowledge base can't reuse a built-in tool name: "
                + ", ".join(kb_collisions)
                + ". Rename the knowledge base."
            ),
        )
    source = _source_for(session_id)
    ensure_workflow_cache_paths_for_source(spec.to_raw(), source)
    # Serialize dependency resolution + the deployed write against a concurrent
    # component delete (F3): either the delete's scan sees this workflow, or this
    # deploy's resolution fails because the resource was already removed.
    with component_mutation_lock:
        # Resolve capability and publish dependencies from one skill-head
        # snapshot. Otherwise an admin edit between this gate and publication
        # could pin an email skill after a no-mailbox check had already passed.
        if spec_uses_email(db, session.specification_json, org.id) and (
            get_email_credentials(db, org.id) is None
        ):
            raise HTTPException(
                status_code=400,
                detail="This team works in your email, so connect a mailbox before going live.",
            )
        extra_tools = {
            **load_knowledge_base_tools(db, spec.to_raw(), source, org_id=org.id),
            **load_email_tools(db, org.id),
        }
        try:
            validate_specification(
                spec, source=source, extra_tools=extra_tools, extra_skills=load_skills(db, org.id)
            )
        except ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw = spec.to_raw()
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
        # spec.to_raw() deliberately omits display_name/friendly_description
        # (it matches the engine loader's minimal shape, see
        # test_to_raw_strips_friendly_fields_and_matches_loader_shape) -- but
        # Activity's run cards read a team's customer-facing name from this
        # exact persisted config (main.py's team_display_name), so merge it
        # back in for what actually gets persisted, without touching
        # to_raw()'s own contract. The loader ignores unknown keys.
        for team_raw, team_spec in zip(raw.get("teams", []), spec.teams):
            if team_spec.display_name:
                team_raw["display_name"] = team_spec.display_name
            if team_spec.friendly_description:
                team_raw["friendly_description"] = team_spec.friendly_description

        record, _version = publish_workflow_version(
            db,
            org_id=org.id,
            name=spec.name,
            config=raw,
            workflow_id=session.workflow_id,
            created_by=user.username,
            owner_principal_id=user.principal_id,
        )
        session = update_session(
            db, session_id, status="deployed", workflow_id=record.id
        )
    return _session_to_dict(session, db, org.id)
