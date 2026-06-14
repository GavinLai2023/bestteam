"""FastAPI backend for the bestteam runtime monitoring dashboard.

Pure wrapper: every endpoint is a thin shim over the bestteam SDK
(`load_workflow`, `Workflow.run/.stream/.visualize`). The only thing this
layer adds is turning a blocking, synchronous SDK into something a browser
can subscribe to live — an in-memory run registry plus a thread-pool bridge
from LangGraph's blocking `.stream()` generator into asyncio.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from bestteam import Workflow, load_workflow
from bestteam.core.loader import _build_workflow
from bestteam.exceptions import BestTeamError

from . import auth
from .auth_api import get_current_user, router as auth_router
from .builder import router as builder_router
from .crud import router as crud_router
from .db.models import User, WorkflowRecord
from .db_session import SessionLocal, get_db
from .runtime import _executor, registry, run_in_background

WORKFLOWS_DIR = Path(__file__).parent / "workflows"

if os.environ.get("BESTTEAM_ENV") == "production" and auth.SECRET_KEY == auth._DEFAULT_SECRET_KEY:
    raise RuntimeError("BESTTEAM_SECRET_KEY must be set when BESTTEAM_ENV=production")

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

_workflow_cache: Dict[str, Tuple[Workflow, Any]] = {}


class RunRequest(BaseModel):
    workflow: str
    input: str


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
    if db is not None:
        record = db.query(WorkflowRecord).filter_by(name=name).one_or_none()
    else:
        with SessionLocal() as session:
            record = session.query(WorkflowRecord).filter_by(name=name).one_or_none()

    if record is not None:
        cache_key: Any = ("db", record.updated_at)
        cached = _workflow_cache.get(name)
        if cached is None or cached[1] != cache_key:
            try:
                workflow = _build_workflow(record.config, source=WORKFLOWS_DIR / f"{name}.yaml", extra_tools={})
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

    loop = asyncio.get_running_loop()
    loop.run_in_executor(_executor, run_in_background, run.id, workflow, req.input, loop, db.get_bind())

    return {"run_id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(get_current_user)):
    run = registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    return dataclasses.asdict(run)


@app.websocket("/api/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str):
    """Replays any events already produced, then relays new ones live until
    the run reaches a terminal state (run_completed / run_failed)."""
    run = registry.get(run_id)
    if run is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    queue = registry.subscribe(run_id)
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event["type"] in ("run_completed", "run_failed"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run_id, queue)
