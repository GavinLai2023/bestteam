"""Builder session state-machine API (Phase 2).

Thin wrappers over `ui/backend/db/builder_sessions.py` plus the Phase 0.5
generation/validation helpers (`bestteam.generate_requirements`,
`bestteam.generate_specification`, `bestteam.validate_specification`). Maps
onto the six-stage methodology in docs/team_builder_methodology.md:
intent -> requirements -> spec -> solution -> testing -> deployed.
"""

from __future__ import annotations

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

from .auth_api import get_current_user
from .db.builder_sessions import append_feedback, create_session, get_session, list_sessions, update_session
from .db.model_catalog import list_entries, to_prompt_text
from .db.models import BuilderSession, KnowledgeBaseRecord, WorkflowRecord
from .db_session import get_db
from .knowledge_bases import load_knowledge_base_tools
from .runtime import _executor, registry, run_in_background
from .skills import load_skills

router = APIRouter(prefix="/api/builder/sessions", tags=["builder"], dependencies=[Depends(get_current_user)])

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


def _session_to_dict(session: BuilderSession) -> Dict[str, Any]:
    return {
        "id": session.id,
        "intent_text": session.intent_text,
        "as_is_text": session.as_is_text,
        "requirements_json": session.requirements_json,
        "specification_json": session.specification_json,
        "status": session.status,
        "feedback_history": session.feedback_history,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _get_session_or_404(db: Session, session_id: str) -> BuilderSession:
    session = get_session(db, session_id)
    if session is None:
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


def _with_skill_catalog(db: Session, text: str) -> str:
    """Append available skills (if any) so the Solution Architect can assign
    them to agents by name, parallel to `_with_model_catalog`."""
    skills = load_skills(db)
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


def _with_knowledge_base_catalog(db: Session, text: str) -> str:
    """Append available standalone knowledge bases (if any) so the Solution
    Architect can reference them by name, parallel to `_with_skill_catalog`.

    Returns `text` unchanged when none exist, so the architect sees no
    "Available knowledge bases" section and has nothing to draw a name
    from -- combined with `_ARCHITECT_SYSTEM_PROMPT`'s instruction not to
    invent one, this is what stops it from fabricating a `path`.
    """
    records = db.query(KnowledgeBaseRecord).all()
    if not records:
        return text
    lines = ["", "", "Available knowledge bases (reference by name in an agent's tools, do not redeclare):"]
    for record in records:
        kb_type = record.config.get("type", "local_folder")
        lines.append(f"- {record.name} (type: {kb_type})")
    return text + "\n".join(lines)


def _all_knowledge_base_tools(db: Session, source: Path) -> Dict[str, Any]:
    """Build a tool for every standalone knowledge base in the database.

    Used before a Specification exists yet (at generation time), when we
    don't yet know which knowledge bases the architect's agents will
    reference -- unlike `load_knowledge_base_tools`, which filters to a
    known `raw` config's referenced names.
    """
    records = db.query(KnowledgeBaseRecord).all()
    tools: Dict[str, Any] = {}
    for record in records:
        kb = _build_knowledge_base(record.config, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools


def _validate_spec_payload(
    db: Session, payload: Dict[str, Any], source: Path, extra_skills: Optional[Dict[str, Any]] = None
) -> Specification:
    try:
        spec = Specification.model_validate(payload)
        extra_tools = load_knowledge_base_tools(db, spec.to_raw(), source)
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
def create_builder_session(req: CreateSessionRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Stage 1 (Intent): start a session with the customer's free-text intent/as-is."""
    session = create_session(db, intent_text=req.intent_text, as_is_text=req.as_is_text)
    return _session_to_dict(session)


@router.get("")
def list_builder_sessions(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """List builder sessions (most recent first), for an "AI teams I've
    built" list page. A session with status 'deployed' has a live
    WorkflowRecord matching specification_json['name']."""
    return {"sessions": [_session_to_dict(s) for s in list_sessions(db)]}


@router.get("/{session_id}")
def get_builder_session(session_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return _session_to_dict(_get_session_or_404(db, session_id))


@router.post("/{session_id}/requirements")
def submit_requirements(session_id: str, req: RequirementsRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Stage 2 (Requirements): generate (via `model`) or confirm (via `requirements`)
    a structured requirements summary."""
    session = _get_session_or_404(db, session_id)

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
    return _session_to_dict(session)


@router.post("/{session_id}/specification")
def submit_specification(session_id: str, req: SpecificationRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Stage 3 (Specification): generate (via `model`) or accept (via `specification`)
    a team design, validated through `_build_workflow` before it's stored."""
    session = _get_session_or_404(db, session_id)
    source = _source_for(session_id)

    if req.specification is not None:
        spec = _validate_spec_payload(db, req.specification, source, extra_skills=load_skills(db))
    elif req.model is not None:
        requirements_text = _requirements_text(session)
        if req.feedback:
            requirements_text += f"\n\nCustomer feedback on the previous design:\n{req.feedback}"
        requirements_text = _with_model_catalog(db, requirements_text)
        requirements_text = _with_skill_catalog(db, requirements_text)
        requirements_text = _with_knowledge_base_catalog(db, requirements_text)
        chat_model = _call_model(_resolve_model, req.model)
        spec = _call_model(
            generate_specification,
            chat_model,
            requirements_text,
            source=source,
            extra_tools=_all_knowledge_base_tools(db, source),
            extra_skills=load_skills(db),
        )
    else:
        raise HTTPException(status_code=400, detail="Provide either 'specification' or 'model'")

    if req.feedback:
        append_feedback(db, session_id, {"stage": "specification", "note": req.feedback})

    session = update_session(db, session_id, specification_json=spec.model_dump(), status="spec")
    return _session_to_dict(session)


@router.post("/{session_id}/solution")
def submit_solution_feedback(session_id: str, req: SolutionRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Stage 4 (Solution): refine the Specification with customer feedback,
    either by re-running the Solution Architect (`model`) or accepting a
    manually-edited Specification (`specification`)."""
    session = _get_session_or_404(db, session_id)
    source = _source_for(session_id)

    if req.specification is not None:
        spec = _validate_spec_payload(db, req.specification, source, extra_skills=load_skills(db))
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
        requirements_text = _with_skill_catalog(db, requirements_text)
        requirements_text = _with_knowledge_base_catalog(db, requirements_text)
        chat_model = _call_model(_resolve_model, req.model)
        spec = _call_model(
            generate_specification,
            chat_model,
            requirements_text,
            source=source,
            extra_tools=_all_knowledge_base_tools(db, source),
            extra_skills=load_skills(db),
        )
    else:
        raise HTTPException(status_code=400, detail="Provide either 'specification' or 'model'")

    append_feedback(db, session_id, {"stage": "solution", "note": req.feedback})
    session = update_session(db, session_id, specification_json=spec.model_dump(), status="solution")
    return _session_to_dict(session)


@router.post("/{session_id}/test-runs")
async def create_test_run(session_id: str, req: TestRunRequest, db: Session = Depends(get_db)) -> Dict[str, str]:
    """Stage 5 (Testing): run the validated Specification in the sandbox via
    the same `Workflow.stream()`/`RunRegistry` machinery as `/api/runs`."""
    session = _get_session_or_404(db, session_id)
    if session.specification_json is None:
        raise HTTPException(status_code=400, detail="Generate a specification before testing")

    spec = Specification.model_validate(session.specification_json)
    source = _source_for(session_id)
    extra_tools = load_knowledge_base_tools(db, spec.to_raw(), source)
    try:
        workflow = validate_specification(spec, source=source, extra_tools=extra_tools, extra_skills=load_skills(db))
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    update_session(db, session_id, status="testing")

    run = registry.create(spec.name, req.input)
    _executor.submit(run_in_background, run.id, workflow, req.input, db.get_bind())
    return {"run_id": run.id}


@router.post("/{session_id}/deploy")
def deploy_session(session_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Stage 6 (Deployment): persist the validated Specification as a
    `WorkflowRecord` (`status=deployed`) so `_get_workflow()` picks it up."""
    session = _get_session_or_404(db, session_id)
    if session.specification_json is None:
        raise HTTPException(status_code=400, detail="Generate a specification before deploying")

    spec = Specification.model_validate(session.specification_json)
    source = _source_for(session_id)
    extra_tools = load_knowledge_base_tools(db, spec.to_raw(), source)
    try:
        validate_specification(spec, source=source, extra_tools=extra_tools, extra_skills=load_skills(db))
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw = spec.to_raw()
    record = db.query(WorkflowRecord).filter_by(name=spec.name).one_or_none()
    if record is None:
        record = WorkflowRecord(name=spec.name, config=raw, status="deployed")
        db.add(record)
    else:
        record.config = raw
        record.status = "deployed"
    db.commit()

    session = update_session(db, session_id, status="deployed")
    return _session_to_dict(session)
