"""FastAPI backend for the bestteam runtime monitoring dashboard.

Pure wrapper: every endpoint is a thin shim over the bestteam SDK
(`load_workflow`, `Workflow.run/.stream/.visualize`). The only thing this
layer adds is turning a blocking, synchronous SDK into something a browser
can subscribe to live — an in-memory run registry plus a thread-pool bridge
from LangGraph's blocking `.stream()` generator into asyncio.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from bestteam import Workflow, load_workflow
from bestteam.core.loader import _build_workflow
from bestteam.exceptions import BestTeamError

from . import auth
from .auth import AuthError, decode_access_token
from .auth_api import get_current_user, router as auth_router
from .builder import router as builder_router
from .crud import router as crud_router
from .db.models import KnowledgeBaseRecord, SkillRecord, User, WorkflowRecord
from .db.users import get_user_by_username
from .db_session import SessionLocal, get_db
from .knowledge_bases import load_knowledge_base_tools
from .runtime import _executor, registry, run_in_background
from .skills import load_skills

WORKFLOWS_DIR = Path(__file__).parent / "workflows"

if auth.SECRET_KEY == auth._DEFAULT_SECRET_KEY:
    raise RuntimeError(
        "BESTTEAM_SECRET_KEY is unset (using the insecure dev default). "
        "Set BESTTEAM_SECRET_KEY to a long random value before starting this service "
        "-- generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

_default_cors_origins = "http://localhost:5173,http://127.0.0.1:5173"
_cors_origins = [o.strip() for o in os.environ.get("BESTTEAM_CORS_ORIGINS", _default_cors_origins).split(",") if o.strip()]

app = FastAPI(title="bestteam monitoring dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(builder_router)
app.include_router(crud_router)

logger = logging.getLogger("bestteam.api")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for anything not already turned into an HTTPException by a
    route handler (BestTeamError/ValidationError/KeyError/TypeError are
    handled inline and never reach here). Logs the full traceback
    server-side and returns a generic, non-leaking 500 to the client."""
    logger.exception("Unhandled exception in %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


_workflow_cache: Dict[str, Tuple[Workflow, Any]] = {}


class RunRequest(BaseModel):
    workflow: str
    input: str


def _dependency_freshness(db: Session) -> Tuple[Optional[Any], Optional[Any]]:
    """Max `updated_at` across all SkillRecords and all KnowledgeBaseRecords,
    folded into a cached Workflow's cache key so editing either invalidates
    any already-cached workflow that might depend on them.

    Deliberately global rather than scoped to only the names a given
    workflow references -- see
    docs/superpowers/specs/2026-06-22-code-review-fixes-design.md, "Design
    > A" for why."""
    skills_max = db.query(func.max(SkillRecord.updated_at)).scalar()
    kb_max = db.query(func.max(KnowledgeBaseRecord.updated_at)).scalar()
    return (skills_max, kb_max)


def _get_workflow(name: str, db: Optional[Session] = None) -> Workflow:
    """Load and cache a workflow by name (Workflow already memoizes its own
    compiled graph, so repeat runs of the same workflow stay cheap).

    A `WorkflowRecord` in the database (e.g. one deployed via the Team
    Builder Wizard, or edited through the `/api/config` CRUD API) takes
    priority over a YAML file of the same name, and is cached on its
    `updated_at`. Otherwise falls back to `WORKFLOWS_DIR/<name>.yaml`, cached
    by the file's mtime, so editing a workflow file on disk is picked up on
    the next request.

    `db` is the request's `get_db`-provided session, if available, so this
    sees the same data as the `/api/builder` and `/api/config` routers
    (including in tests, which override `get_db`); if omitted, a one-off
    session against the module-level engine is used."""
    source = WORKFLOWS_DIR / f"{name}.yaml"
    if db is not None:
        record = db.query(WorkflowRecord).filter_by(name=name).one_or_none()
        dependency_freshness = _dependency_freshness(db) if record is not None else None
    else:
        with SessionLocal() as session:
            record = session.query(WorkflowRecord).filter_by(name=name).one_or_none()
            dependency_freshness = _dependency_freshness(session) if record is not None else None

    if record is not None:
        cache_key: Any = ("db", record.updated_at, *dependency_freshness)
        cached = _workflow_cache.get(name)
        if cached is None or cached[1] != cache_key:
            # Only load skills and build standalone KB tools (which may
            # re-chunk files and, for type: vector, call a paid embedding
            # model) on a cache miss -- not on every request.
            if db is not None:
                skill_lookup = load_skills(db)
                kb_tools = load_knowledge_base_tools(db, record.config, source)
            else:
                with SessionLocal() as session:
                    skill_lookup = load_skills(session)
                    kb_tools = load_knowledge_base_tools(session, record.config, source)
            try:
                workflow = _build_workflow(
                    record.config,
                    source=source,
                    extra_tools=kb_tools,
                    extra_skills=skill_lookup,
                )
            except (KeyError, TypeError, BestTeamError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _workflow_cache[name] = (workflow, cache_key)
        return _workflow_cache[name][0]

    path = WORKFLOWS_DIR / f"{name}.yaml"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Unknown workflow '{name}'")

    cache_key = ("file", path.stat().st_mtime)
    cached = _workflow_cache.get(name)
    if cached is None or cached[1] != cache_key:
        try:
            _workflow_cache[name] = (load_workflow(path), cache_key)
        except BestTeamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _workflow_cache[name][0]


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/workflows")
def list_workflows(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    db_names = {row.name for row in db.query(WorkflowRecord.name).all()}
    yaml_names = {p.stem for p in WORKFLOWS_DIR.glob("*.yaml")}
    return {"workflows": sorted(db_names | yaml_names)}


@app.get("/api/workflows/{name}/graph")
def workflow_graph(name: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    workflow = _get_workflow(name, db)
    try:
        return {"mermaid": workflow.visualize()}
    except BestTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs")
async def create_run(req: RunRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    workflow = _get_workflow(req.workflow, db)
    run = registry.create(req.workflow, req.input)

    _executor.submit(run_in_background, run.id, workflow, req.input, db.get_bind())

    return {"run_id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(get_current_user)):
    run = registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    return dataclasses.asdict(run)


@app.websocket("/api/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str, token: Optional[str] = None, db: Session = Depends(get_db)):
    """Replays any events already produced, then relays new ones live until
    the run reaches a terminal state (run_completed / run_failed).

    Requires the same bearer token used for REST routes, passed as a
    `?token=` query parameter -- browsers can't set custom headers when
    opening a WebSocket, so this can't reuse the HTTPBearer-based
    get_current_user dependency directly."""
    if token is None:
        await websocket.close(code=4401)
        return
    try:
        username = decode_access_token(token)
    except AuthError:
        await websocket.close(code=4401)
        return
    if get_user_by_username(db, username) is None:
        await websocket.close(code=4401)
        return

    run = registry.get(run_id)
    if run is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    subscriber_queue = registry.subscribe(run_id)
    try:
        while True:
            event = await subscriber_queue.get()
            await websocket.send_json(event)
            if event["type"] in ("run_completed", "run_failed"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run_id, subscriber_queue)
