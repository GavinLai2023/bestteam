"""Shared run-execution plumbing for `main.py` (`/api/runs`) and `builder.py`
(the Team Builder Wizard's sandbox test runs, Phase 2).

Split into its own module so the two router modules don't import from each
other.
"""

from __future__ import annotations

import asyncio
import dataclasses
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from bestteam import Workflow

from .db.usage import record_usage
from .registry import RunRegistry

registry = RunRegistry()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-run")


def run_in_background(
    run_id: str, workflow: Workflow, input: str, loop: asyncio.AbstractEventLoop, engine: Optional[Engine] = None
) -> None:
    """Drain `Workflow.stream()` on a worker thread and relay each event back
    onto the asyncio loop so WebSocket subscribers see it as it happens.

    If `engine` is given, each `agent_completed` event's per-model-call
    `usage` entries (see `core/trace.py`) are persisted as `usage_records`
    (Phase 3) -- a fresh `Session` is opened on `engine` since this runs on a
    worker thread. `engine` is the request's `db.get_bind()`, so tests that
    override `get_db` with an in-memory database see usage records there too.
    """
    db = Session(engine) if engine is not None else None
    try:
        for event in workflow.stream(input):
            payload = dataclasses.asdict(event)
            loop.call_soon_threadsafe(registry.publish, run_id, payload)
            if db is not None and event.type == "agent_completed":
                for entry in event.usage:
                    record_usage(
                        db,
                        run_id=run_id,
                        agent=event.agent,
                        model=entry.get("model"),
                        input_tokens=entry.get("input_tokens", 0),
                        output_tokens=entry.get("output_tokens", 0),
                    )
    finally:
        if db is not None:
            db.close()
