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
from typing import Any, Optional

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from bestteam import MemoryManager, SqliteBM25Memory, Workflow
from bestteam.core.trace import TraceEvent

from .db.models import Run
from .db.usage import record_usage
from .registry import RunRegistry

_logger = logging.getLogger(__name__)

registry = RunRegistry()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-run")


def _safe_record_usage(db: Session, **kwargs: Any) -> None:
    """Persist one usage entry, isolating failures from run status (review r5 #1).

    Usage metering is auxiliary: a `usage_records` write failing (DB error) must
    NOT propagate and flip an otherwise-successful run to `run_failed`. Log,
    roll back the poisoned transaction, and continue.
    """
    try:
        record_usage(db, **kwargs)
    except Exception:  # noqa: BLE001 -- metering must never break a run
        _logger.warning("Usage recording failed for run %s; run unaffected", kwargs.get("run_id"), exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    """Positive-int env config, or `default` when unset/blank/invalid/non-positive."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("Ignoring non-integer %s=%r; using %r", name, raw, default)
        return default
    return value if value > 0 else default


def _make_memory(
    org_id: Optional[int] = None,
    *,
    principal_id: Optional[str] = None,
    run_id: Optional[str] = None,
    workflow_version_id: Optional[int] = None,
) -> Optional[MemoryManager]:
    """Build a per-user `MemoryManager` from env, or None when memory is disabled.

    Called on the worker thread that runs the workflow so the underlying
    SQLite connection stays thread-local (`SqliteBM25Memory` opens its
    connection in `__init__`).

    - `BESTTEAM_MEMORY_DB` unset/empty -> disabled (returns None; runs behave
      exactly as before).
    - set -> a `SqliteBM25Memory` at that path; `BESTTEAM_MEMORY_MODEL`, if
      set, enables semantic/procedural extraction via one LLM call per run.

    `org_id` scopes every recall/record to the run's organization (SP-2), so a
    run only ever sees and writes its own org's memory.
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
    return MemoryManager(
        store,
        extraction_model=extraction_model,
        org_id=org_id,
        principal_id=principal_id,
        run_id=run_id,
        workflow_version_id=workflow_version_id,
        # SP-4: production recall is bounded by default (M-09); episodic retention
        # is opt-in (M-07, destructive so default unbounded).
        recall_max_candidates=_env_int("BESTTEAM_MEMORY_RECALL_MAX_CANDIDATES", 1000),
        max_episodic_per_user=_env_int("BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER", None),
    )


def run_in_background(
    run_id: str,
    workflow: Workflow,
    input: str,
    engine: Optional[Engine] = None,
    user_id: Optional[str] = None,
    org_id: Optional[int] = None,
    principal_id: Optional[str] = None,
    username: Optional[str] = None,
    workflow_version_id: Optional[int] = None,
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
    thread, so its SQLite connection is thread-local. `principal_id` (the
    caller's immutable `users.principal_id`) scopes recall/writes to the account
    instance, so a recreated same-username account can't recall the deleted
    account's memory (deletion-lifecycle).

    `username` records who started the run on the persisted row (CR-032,
    audit); it is separate from `user_id` so builder sandbox runs can keep
    the initiator without touching per-user memory.
    """
    db = Session(engine) if engine is not None else None
    run_row: Optional[Run] = None
    memory = (
        _make_memory(
            org_id,
            principal_id=principal_id,
            run_id=run_id,
            workflow_version_id=workflow_version_id,
        )
        if user_id
        else None
    )
    terminal_seen = False
    try:
        if db is not None:
            # Persist the run up front so usage_records/trace_events foreign
            # keys reference a real `runs` row rather than a phantom id
            # (CR-012). Committed before any usage record so the FK target
            # always exists; its terminal status/output are updated below. This
            # sits inside the try so a persistence failure still yields a
            # terminal event instead of leaving the run stuck "running" (CR-003).
            run_row = db.get(Run, run_id)
            if run_row is None:
                run_row = Run(
                    id=run_id,
                    workflow=getattr(workflow, "name", ""),
                    input=input,
                    org_id=org_id,
                    username=username,
                    workflow_version_id=workflow_version_id,
                )
                db.add(run_row)
            else:
                # A caller (the autonomous trigger) already persisted this row
                # before dispatch as a durable activity record; keep it.
                run_row.workflow = getattr(workflow, "name", "") or run_row.workflow
            db.commit()
        for event in workflow.stream(input, user_id=user_id, memory=memory):
            payload = dataclasses.asdict(event)
            registry.publish(run_id, payload)
            if event.type in ("run_completed", "run_failed"):
                terminal_seen = True
                if run_row is not None:
                    run_row.status = "completed" if event.type == "run_completed" else "failed"
                    run_row.output = event.data
                    db.commit()
            if db is not None and event.type == "agent_completed":
                for entry in event.usage:
                    _safe_record_usage(
                        db,
                        run_id=run_id,
                        agent=event.agent,
                        model=entry.get("model"),
                        input_tokens=entry.get("input_tokens", 0),
                        output_tokens=entry.get("output_tokens", 0),
                        org_id=org_id,
                    )
            if db is not None and event.type in ("memory_recorded", "memory_failed"):
                # Meter the memory extraction LLM call (M-04); it bypasses the
                # adapter's usage path, so it arrives on the memory event. The SDK
                # attaches the usage to exactly one event (recorded, or failed when
                # every write failed), so this never double-counts (review r6 #1).
                for entry in event.usage:
                    _safe_record_usage(
                        db,
                        run_id=run_id,
                        agent="memory:extraction",
                        model=entry.get("model"),
                        input_tokens=entry.get("input_tokens", 0),
                        output_tokens=entry.get("output_tokens", 0),
                        org_id=org_id,
                    )
    except Exception:  # noqa: BLE001 -- any worker failure must still yield a terminal event
        # Workflow.stream() compiles before its own BestTeamError handler, so a
        # compile failure (e.g. an unsupported collaboration mode) escapes as an
        # exception rather than a run_failed event. Without this catch-all the
        # run would stay "running" forever and subscribers would never see a
        # terminal event (CR-003). The message is sanitized; the real traceback
        # is logged server-side only. Only synthesize run_failed if no terminal
        # event was already published, so a post-completion failure (e.g. usage
        # recording) can't flip a completed run to failed.
        _logger.exception("Run %s failed on the worker thread", run_id)
        if not terminal_seen:
            message = "The run failed due to an internal error."
            registry.publish(
                run_id,
                dataclasses.asdict(
                    TraceEvent(
                        type="run_failed",
                        workflow=getattr(workflow, "name", ""),
                        data=message,
                    )
                ),
            )
            # Best-effort: the terminal event above is the hard CR-003
            # guarantee; recording the failed status must never re-raise (the
            # up-front persist may itself have failed, leaving the session
            # needing a rollback), so a DB error here is logged and swallowed.
            if db is not None and run_row is not None:
                try:
                    db.rollback()
                    run_row.status = "failed"
                    run_row.output = message
                    db.add(run_row)
                    db.commit()
                except Exception:  # noqa: BLE001
                    _logger.warning("Could not persist failed status for run %s", run_id)
    finally:
        if db is not None:
            db.close()
        if memory is not None:
            # Built per run on this worker thread; close its SQLite connection
            # now instead of leaving it to GC (M-03). Best-effort like every
            # other memory operation: a custom store whose close() raises must
            # not escape the worker as an unobserved Future exception (the run
            # already completed and published its terminal event).
            try:
                memory.close()
            except Exception:  # noqa: BLE001 -- memory must never break a run
                _logger.warning("Closing per-run memory store failed", exc_info=True)
