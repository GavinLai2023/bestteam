"""Shared run-execution plumbing for `main.py` (`/api/runs`) and `builder.py`
(the Team Builder Wizard's sandbox test runs, Phase 2).

Split into its own module so the two router modules don't import from each
other.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from bestteam import MemoryManager, SqliteBM25Memory, Workflow

from .db.usage import record_usage
from .registry import RunRegistry

_logger = logging.getLogger(__name__)

registry = RunRegistry()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-run")


def _make_memory() -> Optional[MemoryManager]:
    """Build a per-user `MemoryManager` from env, or None when memory is disabled.

    Called on the worker thread that runs the workflow so the underlying
    SQLite connection stays thread-local (`SqliteBM25Memory` opens its
    connection in `__init__`).

    - `BESTTEAM_MEMORY_DB` unset/empty -> disabled (returns None; runs behave
      exactly as before).
    - set -> a `SqliteBM25Memory` at that path; `BESTTEAM_MEMORY_MODEL`, if
      set, enables semantic/procedural extraction via one LLM call per run.
    """
    db_path = os.environ.get("BESTTEAM_MEMORY_DB", "").strip()
    if not db_path:
        return None
    try:
        store = SqliteBM25Memory(db_path)
    except Exception as exc:  # noqa: BLE001 — memory must never break a run
        _logger.warning("Memory disabled: could not open store at %r: %s", db_path, exc)
        return None
    extraction_model = os.environ.get("BESTTEAM_MEMORY_MODEL", "").strip() or None
    return MemoryManager(store, extraction_model=extraction_model)


def run_in_background(
    run_id: str,
    workflow: Workflow,
    input: str,
    engine: Optional[Engine] = None,
    user_id: Optional[str] = None,
) -> None:
    """Drain `Workflow.stream()` on a worker thread and publish each event to
    the registry (thread-safe) so WebSocket subscribers see it as it happens.

    If `engine` is given, each `agent_completed` event's per-model-call
    `usage` entries (see `core/trace.py`) are persisted as `usage_records`
    (Phase 3) -- a fresh `Session` is opened on `engine` since this runs on a
    worker thread. `engine` is the request's `db.get_bind()`, so tests that
    override `get_db` with an in-memory database see usage records there too.

    If `user_id` is given and memory is enabled (`BESTTEAM_MEMORY_DB`), the
    run recalls that user's memory into every agent's prompt and records the
    run afterward (see `core/memory.py`). Memory is built here, on the worker
    thread, so its SQLite connection is thread-local.
    """
    db = Session(engine) if engine is not None else None
    memory = _make_memory() if user_id else None
    try:
        for event in workflow.stream(input, user_id=user_id, memory=memory):
            payload = dataclasses.asdict(event)
            registry.publish(run_id, payload)
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
