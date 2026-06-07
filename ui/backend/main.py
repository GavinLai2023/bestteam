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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bestteam import Workflow, load_workflow
from bestteam.exceptions import BestTeamError

from .registry import RunRegistry

WORKFLOWS_DIR = Path(__file__).parent / "workflows"

app = FastAPI(title="bestteam monitoring dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

registry = RunRegistry()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-run")
_workflow_cache: Dict[str, Workflow] = {}


class RunRequest(BaseModel):
    workflow: str
    input: str


def _get_workflow(name: str) -> Workflow:
    """Load and cache a workflow by name (Workflow already memoizes its own
    compiled graph, so repeat runs of the same workflow stay cheap)."""
    if name not in _workflow_cache:
        path = WORKFLOWS_DIR / f"{name}.yaml"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Unknown workflow '{name}'")
        try:
            _workflow_cache[name] = load_workflow(path)
        except BestTeamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _workflow_cache[name]


def _run_in_background(run_id: str, workflow: Workflow, input: str, loop: asyncio.AbstractEventLoop) -> None:
    """Drains Workflow.stream() on a worker thread — LangGraph's underlying
    .stream() is a blocking generator — and relays each TraceEvent back onto
    the asyncio loop so WebSocket subscribers see it as it happens."""
    for event in workflow.stream(input):
        payload = dataclasses.asdict(event)
        loop.call_soon_threadsafe(registry.publish, run_id, payload)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/workflows")
def list_workflows():
    return {"workflows": sorted(p.stem for p in WORKFLOWS_DIR.glob("*.yaml"))}


@app.get("/api/workflows/{name}/graph")
def workflow_graph(name: str):
    workflow = _get_workflow(name)
    try:
        return {"mermaid": workflow.visualize()}
    except BestTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs")
async def create_run(req: RunRequest):
    workflow = _get_workflow(req.workflow)
    run = registry.create(req.workflow, req.input)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(_executor, _run_in_background, run.id, workflow, req.input, loop)

    return {"run_id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
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
