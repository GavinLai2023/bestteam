"""Builder session state-machine API (Phase 2).

Thin wrappers over `ui/backend/db/builder_sessions.py` plus the Phase 0.5
generation/validation helpers (`bestteam.generate_requirements`,
`bestteam.generate_specification`, `bestteam.validate_specification`). Maps
onto the six-stage methodology in docs/team_builder_methodology.md:
intent -> requirements -> spec -> solution -> testing -> deployed.
"""

from __future__ import annotations

import inspect
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from bestteam import (
    KnowledgeBaseSpec,
    Specification,
    generate_requirements,
    generate_specification,
    validate_specification,
)
from bestteam.adapters.langgraph_adapter import _resolve_model
from bestteam.core.knowledge_base import make_knowledge_base_tool
from bestteam.core.requirements import QuestionAnswer, Requirements
from bestteam.exceptions import BestTeamError, ConfigurationError
from bestteam.tools import REGISTRY

from .auth_api import get_current_org, get_current_user
from .db.builder_sessions import append_feedback, create_session, delete_session, get_session, list_sessions, update_session
from .db.model_catalog import list_chat_entries, to_prompt_text
from .deploy_validation import (
    LOCAL_FILE_TOOL_NAMES,
    find_email_egress_conflicts,
    find_local_file_tools,
    validate_agent_models,
)
from .db.models import BuilderSession, KnowledgeBaseRecord, Organization, PipelineRecord, User, iso_utc
from .db.pipelines import publish_pipeline_version
from .db_session import get_db
from .component_lock import component_mutation_lock
from .knowledge_bases import (
    check_path_traversal,
    checked_contained_cache_path,
    ensure_pipeline_cache_paths_for_source,
    kb_name_collisions,
    load_knowledge_base_tools,
    resolve_knowledge_base,
)
from .db.email_credentials import get_email_credentials
from .email_tools import load_email_tools, resolve_agent_tool_sets, spec_uses_email
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
    # A paused team is still listed here -- My Teams is where it gets switched
    # back on -- so the card needs to know. True for anything not deployed:
    # a draft is not paused, it has simply never run.
    active = True
    if db is not None and org_id is not None and session.specification_json:
        spec_raw = session.specification_json
        pipeline_version_id = None
        # Once deployed, capability metadata must describe the exact live team,
        # including its pinned skills. The session spec can be stale after an
        # Advanced-page redeploy, and current skill heads can move independently.
        if session.status == "deployed" and session.pipeline_id is not None:
            record = (
                db.query(PipelineRecord)
                .filter_by(id=session.pipeline_id, org_id=org_id, status="deployed")
                .one_or_none()
            )
            if record is not None:
                spec_raw = record.config
                pipeline_version_id = record.current_version_id
                active = record.active
        uses_email = spec_uses_email(
            db,
            spec_raw,
            org_id,
            pipeline_version_id=pipeline_version_id,
        )
    return {
        "id": session.id,
        "intent_text": session.intent_text,
        "as_is_text": session.as_is_text,
        "requirements_json": session.requirements_json,
        "specification_json": session.specification_json,
        "status": session.status,
        "pipeline_id": session.pipeline_id,
        "feedback_history": session.feedback_history,
        "uses_email": uses_email,
        "active": active,
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
    """A per-session workspace directory, used as `_build_pipeline`'s `source`
    (relative knowledge-base paths, default pipeline name)."""
    workspace = _SESSIONS_DIR / session_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace / "pipeline.yaml"


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
    catalog_text = to_prompt_text(list_chat_entries(db))
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


def _with_tool_catalog(text: str) -> str:
    """Append the built-in tools an agent can be given, parallel to
    `_with_skill_catalog`.

    `_ARCHITECT_SYSTEM_PROMPT` tells the architect to choose tools "from the
    ones available to the pipeline", but the list was never in the prompt: the
    only way to discover a name was to guess a wrong one and read the real ones
    back out of `_build_agent`'s error on a retry. A design with no tools at
    all is valid, so the usual outcome was silent -- a research team with no
    way to reach the web, answering from the model's own weights.

    Needs no database, unlike the other three catalogs: `REGISTRY` is a
    module-level dict of Python functions, identical for every org. Each tool's
    docstring is already written as the LLM-facing description (the loader
    passes it to the model verbatim), so its first line is the summary here.
    """
    lines = ["", "", "Available built-in tools (add the exact name to an agent's `tools` list):"]
    for name, fn in sorted(REGISTRY.items()):
        if name in LOCAL_FILE_TOOL_NAMES:
            # Refused at deploy (`find_local_file_tools`), so naming it here
            # would only have the architect design a team the gate rejects.
            continue
        summary = (inspect.getdoc(fn) or "").strip().splitlines()
        lines.append(f"- {name}: {summary[0]}" if summary else f"- {name}")
    # Naming the email tools is what makes this reachable. Before this catalog
    # existed the architect could not guess them, so it never built the
    # combination `find_email_egress_conflicts` refuses at deploy time.
    lines.append(
        "Never give one team both an email_* tool and web_search or http_get: that "
        "combination is refused at deployment, because anything an agent reads out of "
        "the mailbox reaches the agent that can send it back out."
    )
    return text + "\n".join(lines)


def _with_knowledge_base_catalog(
    db: Session, text: str, org_id: Optional[int] = None, names: Optional[set[str]] = None
) -> str:
    """Append available standalone knowledge bases (if any) so the Solution
    Architect can reference them by name, parallel to `_with_skill_catalog`.

    Returns `text` unchanged when none exist, so the architect sees no
    "Available knowledge bases" section and has nothing to draw a name
    from -- combined with `_ARCHITECT_SYSTEM_PROMPT`'s instruction not to
    invent one, this is what stops it from fabricating a `path`.

    `names` restricts the listing to knowledge bases that actually built
    (`_all_knowledge_base_tools`'s keys). A KB whose ingestion failed has no
    tool, so naming it here would invite the architect to reference something
    no agent could ever call. `None` lists every record, for callers that
    aren't building tools at all.
    """
    records = db.query(KnowledgeBaseRecord).filter(KnowledgeBaseRecord.org_id == org_id).all()
    if names is not None:
        records = [r for r in records if r.name in names]
    if not records:
        return text
    lines = ["", "", "Available knowledge bases (reference by name in an agent's tools, do not redeclare):"]
    for record in records:
        kb_type = record.config.get("type", "local_folder")
        # The uploader's own sentence about the documents -- without it the
        # architect has only a name to decide which agent needs which
        # collection. Omitted entirely when there isn't one, rather than
        # printing a dangling colon.
        description = record.config.get("description")
        described = f": {description}" if description else ""
        lines.append(f"- {record.name} (type: {kb_type}){described}")
    return text + "\n".join(lines)


def _all_knowledge_base_tools(db: Session, source: Path, org_id: Optional[int] = None) -> Dict[str, Any]:
    """Build a tool for every one of `org_id`'s standalone knowledge bases.

    Used before a Specification exists yet (at generation time), when we
    don't yet know which knowledge bases the architect's agents will
    reference -- unlike `load_knowledge_base_tools`, which filters to a
    known `raw` config's referenced names.

    A knowledge base that can't be resolved (its ingestion failed, or hasn't
    completed yet) is skipped rather than raised: this runs for every one of
    the org's knowledge bases at generation time, so one customer's
    unparseable upload used to 4xx spec generation for the whole org. The
    pipeline-build path (`load_knowledge_base_tools`) still fails closed --
    there a broken KB is one an agent actually references.
    """
    records = db.query(KnowledgeBaseRecord).filter(KnowledgeBaseRecord.org_id == org_id).all()
    tools: Dict[str, Any] = {}
    for record in records:
        try:
            kb = resolve_knowledge_base(db, record, source)
        except ConfigurationError as exc:
            logger.warning("Skipping knowledge base '%s' in the wizard catalog: %s", record.name, exc)
            continue
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools


def _reject_unsafe_kb_paths(spec: Specification) -> None:
    """Constrain a spec's KB paths in place before it is built or stored (CR-001).

    Building a vector KB calls `_save_embedding_cache` at construction time, so
    this must run before `validate_specification`/`_build_pipeline`. Rejects
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


def _reject_fabricated_knowledge_bases(
    spec: Specification, existing: Optional[List[KnowledgeBaseSpec]] = None
) -> None:
    """Refuse an inline `knowledge_bases:` entry the architect introduced itself.

    A wizard session's workspace (`_source_for`) only ever holds `pipeline.yaml`
    -- nothing puts document folders there -- so a relative KB path the architect
    invents can never resolve. The loader catches that, but only as a missing
    directory ("path does not exist or is not a directory: <workspace>/<name>").
    The retry loop hands that message back as self-correction feedback, and the
    architect has no way to create a directory on the server -- so it resubmits
    the same design until every attempt is spent, and the customer sees a server
    path. Name the remedy instead: reference an existing knowledge base by name,
    or design without one. The prompt already says this; this enforces it.

    `existing` is the design being refined, whose entries came through the
    caller-supplied-spec boundary (`_validate_spec_payload`) and already built
    once -- an absolute `path` is legitimate there (`check_path_traversal`
    refuses only `..`). Preserving one of those is not a fabrication; changing
    its path, or adding an entry that wasn't there, is.
    """
    kept = {(kb.name, kb.path) for kb in existing or []}
    fabricated = [kb for kb in spec.knowledge_bases if (kb.name, kb.path) not in kept]
    if not fabricated:
        return
    names = ", ".join(f"'{kb.name}'" for kb in fabricated)
    raise ConfigurationError(
        f"A specification cannot declare its own knowledge bases ({names}). "
        "Use a knowledge base only by adding its exact name to an agent's "
        "tools list, and only if it appears in the available knowledge bases "
        "listed in the input. Remove the knowledge_bases entry; if none of the "
        "available knowledge bases fits, design the team without one."
    )


def _prepare_architect_candidate(
    spec: Specification, source: Path, existing: Optional[List[KnowledgeBaseSpec]] = None
) -> None:
    """`pre_validate` for a model-generated candidate: reject what the architect
    may not declare at all, then contain what it may."""
    _reject_fabricated_knowledge_bases(spec, existing)
    _prepare_generated_specification(spec, source)


def _prepare_generated_specification(spec: Specification, source: Path) -> None:
    """Contain an untrusted model candidate before SDK validation builds it."""
    try:
        _reject_unsafe_kb_paths(spec)
        ensure_pipeline_cache_paths_for_source(spec.to_raw(), source)
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
        ensure_pipeline_cache_paths_for_source(spec.to_raw(), source)
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
    # Answers to the stored requirements' clarifying_questions, paired.
    # Blank answers are deliberate ("skip"): the analyst records assumptions.
    answers: Optional[List[QuestionAnswer]] = None


class SpecificationRequest(BaseModel):
    specification: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    feedback: Optional[str] = None


class SolutionRequest(BaseModel):
    feedback: str
    model: Optional[str] = None
    specification: Optional[Dict[str, Any]] = None


class RefineRequest(BaseModel):
    """The Confirm page's one action: the customer's edited understanding
    (`requirements`) plus whatever they described in words (`feedback`)."""

    requirements: Optional[Dict[str, Any]] = None
    feedback: str = ""
    model: str
    # Non-blank answers to open clarifying questions; blanks are filtered out
    # here (no skip button on Confirm -- an unanswered question stays open).
    answers: Optional[List[QuestionAnswer]] = None


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


def _synthetic_session_for_pipeline(
    record: PipelineRecord, db: Session, org_id: int
) -> Dict[str, Any]:
    """A My-teams card for a pipeline deployed without ever going through the
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
        "pipeline_id": record.id,
        "feedback_history": [],
        "uses_email": spec_uses_email(
            db,
            record.config,
            org_id,
            pipeline_version_id=record.current_version_id,
        ),
        "active": record.active,
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
    a live PipelineRecord matching specification_json['name']. A deployed
    pipeline with no backing session at all (deployed straight through the
    admin Advanced/CRUD page) gets a synthetic entry too -- but only when its
    `created_by` matches this user's immutable principal_id (never username --
    see PipelineRecord.created_by), so My Teams shows exactly the teams this
    person built, not every pipeline anyone in the org can run (that broader
    "org can run" list is /api/pipelines, which treats an unowned pipeline as
    an admin-shared template)."""
    sessions = list_sessions(db, org_id=org.id)
    session_dicts = [_session_to_dict(s, db, org.id) for s in sessions]

    session_pipeline_ids = {s.pipeline_id for s in sessions if s.pipeline_id is not None}
    orphan_pipelines = (
        db.query(PipelineRecord)
        .filter(PipelineRecord.org_id == org.id, PipelineRecord.status == "deployed")
        .filter(PipelineRecord.id.notin_(session_pipeline_ids))
        .filter(PipelineRecord.created_by == user.principal_id)
        .all()
    )
    session_dicts.extend(
        _synthetic_session_for_pipeline(r, db, org.id) for r in orphan_pipelines
    )
    # Both source queries have different ordering semantics; enforce the API's
    # most-recent-first contract after combining them, with stable tie-breakers.
    session_dicts.sort(
        key=lambda item: (
            item["updated_at"],
            item["id"] or "",
            item["pipeline_id"] or 0,
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
    """Delete a session that was never deployed (`pipeline_id IS NULL`) --
    the "abandoned draft" case. A session that has ever gone live has no
    delete path here (see docs/superpowers/specs/2026-07-31-draft-session-deletion-design.md);
    the frontend never offers this for a `pipeline_id`-linked session, but
    this guard holds even if the route is called directly."""
    session = _get_session_or_404(db, session_id, org.id)
    if session.pipeline_id is not None:
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


def _redesign_specification(
    db: Session,
    org_id: int,
    source: Path,
    chat_model: Any,
    requirements_text: str,
    current: Specification,
    feedback: str,
) -> Specification:
    """Re-run the Solution Architect over a design that already exists.

    `current` goes into the prompt so a refinement is incremental: without it
    each round rebuilds the team from the requirements alone and the
    adjustments earlier rounds made drift away.
    """
    prompt = f"{requirements_text}\n\nThe current team design is:\n{current.model_dump_json()}"
    if feedback.strip():
        prompt += f"\n\nCustomer feedback on this design:\n{feedback}"
    # Build the tools first: the catalog only names knowledge bases that
    # actually resolved, so the architect can't reference a broken one.
    kb_tools = _all_knowledge_base_tools(db, source, org_id)
    prompt = _with_model_catalog(db, prompt)
    prompt = _with_tool_catalog(prompt)
    prompt = _with_skill_catalog(db, prompt, org_id)
    prompt = _with_knowledge_base_catalog(db, prompt, org_id, names=set(kb_tools))
    return _call_model(
        generate_specification,
        chat_model,
        prompt,
        source=source,
        extra_tools=kb_tools,
        extra_skills=load_skills(db, org_id),
        # `current`'s own inline knowledge bases are not fabrications -- see
        # `_reject_fabricated_knowledge_bases`.
        pre_validate=lambda candidate: _prepare_architect_candidate(
            candidate, source, current.knowledge_bases
        ),
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

    if req.answers is not None:
        # The Questions step: fold the paired answers into the stored
        # understanding. A blank answer is a deliberate skip -- the analyst
        # records the assumption it made instead (see core/requirements.py).
        if req.requirements is not None:
            raise HTTPException(status_code=400, detail="Provide either 'answers' or 'requirements', not both")
        if req.model is None:
            raise HTTPException(status_code=400, detail="Answering clarifying questions needs a 'model'")
        if session.requirements_json is None:
            raise HTTPException(status_code=400, detail="There are no clarifying questions to answer yet")
        current = Requirements.model_validate(session.requirements_json)
        if not current.clarifying_questions:
            raise HTTPException(status_code=400, detail="There are no clarifying questions to answer yet")
        # The contract is the full paired batch: an empty, partial, stale or
        # unrelated list would let the history record a "skip" that never
        # showed the analyst any question (Codex review finding).
        if sorted(qa.question for qa in req.answers) != sorted(current.clarifying_questions):
            raise HTTPException(
                status_code=400,
                detail="Answers must cover exactly the current clarifying questions",
            )
        chat_model = _call_model(_resolve_model, req.model)
        requirements = _call_model(
            generate_requirements,
            chat_model,
            session.intent_text,
            session.as_is_text,
            current=current,
            answers=req.answers,
        )
        append_feedback(
            db,
            session_id,
            {
                "stage": "clarifying",
                "answers": [qa.model_dump() for qa in req.answers],
                "skipped": all(not qa.answer.strip() for qa in req.answers),
            },
        )
    elif req.requirements is not None:
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
    a team design, validated through `_build_pipeline` before it's stored."""
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
        # Build the tools first: the catalog only names knowledge bases that
        # actually resolved, so the architect can't reference a broken one.
        kb_tools = _all_knowledge_base_tools(db, source, org.id)
        requirements_text = _with_model_catalog(db, requirements_text)
        requirements_text = _with_tool_catalog(requirements_text)
        requirements_text = _with_skill_catalog(db, requirements_text, org.id)
        requirements_text = _with_knowledge_base_catalog(db, requirements_text, org.id, names=set(kb_tools))
        chat_model = _call_model(_resolve_model, req.model)
        spec = _call_model(
            generate_specification,
            chat_model,
            requirements_text,
            source=source,
            extra_tools=kb_tools,
            extra_skills=load_skills(db, org.id),
            pre_validate=lambda candidate: _prepare_architect_candidate(candidate, source),
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
        # Resolving the model spec is cheap/local (constructs a client, no API
        # call) -- do it even on the no-feedback path below so an invalid pick
        # still 400s clearly instead of silently landing on every agent.
        chat_model = _call_model(_resolve_model, req.model)
        if req.feedback.strip():
            spec = _redesign_specification(
                db, org.id, source, chat_model, _requirements_text(session), current, req.feedback
            )
        else:
            # No described change -- the customer is only switching which
            # assistant their team uses (the wizard's feedback box is
            # optional). Keep the current design as-is rather than spending an
            # architect call and risking unrequested drift; only the model
            # pin below applies.
            spec = current
        # `req.model` here is the customer's own pick from the wizard's
        # "Which assistant should your team use?" control -- generation above
        # (when it ran) only used it to run the architect. Left alone, the
        # architect assigns each agent a model by its own role/cost judgement,
        # which can (and did, per customer report) end up different from what
        # the customer just explicitly chose. Pin every agent to that choice
        # so the picker's wording matches what actually gets deployed; this
        # never touches `req.specification` (a customer-submitted spec
        # already has whatever per-agent models they put in it).
        for agent in spec.agents:
            agent.model = req.model
    else:
        raise HTTPException(status_code=400, detail="Provide either 'specification' or 'model'")

    if req.feedback.strip():
        append_feedback(db, session_id, {"stage": "solution", "note": req.feedback})
    _prepare_generated_specification(spec, source)  # contain the stored spec's KB paths (CR-001)
    session = update_session(db, session_id, specification_json=spec.model_dump(), status="solution")
    return _session_to_dict(session, db, org.id)


@router.post("/{session_id}/refine")
def refine_team(
    session_id: str,
    req: RefineRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Update the understanding and the team together, in one call.

    The wizard used to expose these as two buttons, and a customer who used
    only the first one saved an understanding their team had never seen --
    with nothing on screen saying so. Here the two stages are one action: the
    analyst runs only when there is something described in words for it to
    interpret, and the architect always runs, because the customer pressed a
    button that says the team will be updated.

    Nothing is persisted until both stages succeed, so a failed redesign
    cannot leave the understanding ahead of the team.
    """
    session = _get_session_or_404(db, session_id, org.id)
    if session.specification_json is None:
        raise HTTPException(status_code=400, detail="Generate a specification before requesting refinements")

    if req.requirements is not None:
        try:
            requirements = Requirements.model_validate(req.requirements)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif session.requirements_json is not None:
        requirements = Requirements.model_validate(session.requirements_json)
    else:
        requirements = None

    chat_model = _call_model(_resolve_model, req.model)

    answered = [qa for qa in (req.answers or []) if qa.answer.strip()]
    if req.feedback.strip() or answered:
        # `current` is the customer's edited draft, not the stored copy --
        # otherwise the round their edit triggered overwrites that edit.
        requirements = _call_model(
            generate_requirements,
            chat_model,
            session.intent_text,
            session.as_is_text,
            current=requirements,
            answers=answered or None,
            feedback=req.feedback,
        )

    source = _source_for(session_id)
    requirements_text = requirements.to_prompt() if requirements is not None else _requirements_text(session)
    spec = _redesign_specification(
        db,
        org.id,
        source,
        chat_model,
        requirements_text,
        Specification.model_validate(session.specification_json),
        req.feedback,
    )
    # Same pin as `submit_solution_feedback` -- see its comment. Keeping it
    # here means moving the Confirm page onto this endpoint changes which
    # models the customer's agents run on in no way at all.
    for agent in spec.agents:
        agent.model = req.model
    _prepare_generated_specification(spec, source)  # contain the stored spec's KB paths (CR-001)

    if req.feedback.strip():
        append_feedback(db, session_id, {"stage": "solution", "note": req.feedback})
    if answered:
        append_feedback(
            db,
            session_id,
            {
                "stage": "clarifying",
                "answers": [qa.model_dump() for qa in answered],
                "skipped": False,
            },
        )
    fields: Dict[str, Any] = {"specification_json": spec.model_dump(), "status": "solution"}
    if requirements is not None:
        fields["requirements_json"] = requirements.model_dump()
    session = update_session(db, session_id, **fields)
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
    the same `Pipeline.stream()`/`RunRegistry` machinery as `/api/runs`."""
    session = _get_session_or_404(db, session_id, org.id)
    if session.specification_json is None:
        raise HTTPException(status_code=400, detail="Generate a specification before testing")

    spec = Specification.model_validate(session.specification_json)
    _reject_unsafe_kb_paths(spec)  # CR-001: guard the stored spec before it is built
    source = _source_for(session_id)
    ensure_pipeline_cache_paths_for_source(spec.to_raw(), source)
    extra_tools = {
        **load_knowledge_base_tools(db, spec.to_raw(), source, org_id=org.id),
        **load_email_tools(db, org.id),
    }
    try:
        pipeline = validate_specification(
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
        pipeline,
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
    immutable version of a `PipelineRecord` team head (`status=deployed`) so
    `_get_pipeline()` picks it up, and link the session to that head
    (`session.pipeline_id`) so a redeploy versions the same team (P1-02).
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
    ensure_pipeline_cache_paths_for_source(spec.to_raw(), source)
    # Serialize dependency resolution + the deployed write against a concurrent
    # component delete (F3): either the delete's scan sees this pipeline, or this
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
        model_problems = validate_agent_models(raw, {e.spec for e in list_chat_entries(db)})
        if model_problems:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This team can't be deployed: "
                    + "; ".join(model_problems)
                    + ". Pick a model from the catalog."
                ),
            )
        agent_tool_sets = resolve_agent_tool_sets(db, raw, org.id)
        egress_problems = find_email_egress_conflicts(agent_tool_sets)
        if egress_problems:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This team can't be deployed: "
                    + "; ".join(egress_problems)
                    + ". Remove the web-access tool, or move that work to a pipeline with no mailbox access."
                ),
            )
        local_file_problems = find_local_file_tools(agent_tool_sets)
        if local_file_problems:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This team can't be deployed: "
                    + "; ".join(local_file_problems)
                    + ". Upload the documents to a knowledge base instead."
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
        # Same for each agent's own display_name: the customer's run-detail
        # view narrates a run step by step and reads these out of the persisted
        # config (main.py's list_pipelines). Only display_name -- nothing reads
        # an agent's friendly_description outside the wizard's own session.
        for agent_raw, agent_spec in zip(raw.get("agents", []), spec.agents):
            if agent_spec.display_name:
                agent_raw["display_name"] = agent_spec.display_name

        record, _version = publish_pipeline_version(
            db,
            org_id=org.id,
            name=spec.name,
            config=raw,
            pipeline_id=session.pipeline_id,
            created_by=user.username,
            owner_principal_id=user.principal_id,
        )
        session = update_session(
            db, session_id, status="deployed", pipeline_id=record.id
        )
    return _session_to_dict(session, db, org.id)
