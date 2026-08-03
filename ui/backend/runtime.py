"""Shared run-execution plumbing for `main.py` (`/api/runs`) and `builder.py`
(the Team Builder Wizard's sandbox test runs, Phase 2).

Split into its own module so the two router modules don't import from each
other.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from bestteam import MemoryManager, SqliteBM25Memory, Workflow
from bestteam.core.trace import TraceEvent

from .automation_results import RESULT_TYPE_BATCH_MARKER, normalize_run_result
from .db.models import Run, TraceEventRecord
from .db.usage import record_usage
from .registry import RunRegistry

_logger = logging.getLogger(__name__)

registry = RunRegistry()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-run")

# A property-maintenance-batch run's agent output is derived from customer
# email content (the envelope's free-text `extracted`/`missing_information`/
# `risk_reasons` fields can quote it directly) -- the same trust boundary that
# already redacts tool_completed data for email_find/email_read/
# email_draft_reply (adapters/langgraph_adapter.py) applies here too, so this
# placeholder replaces the raw agent_completed/run_completed text before it's
# ever published live or persisted to trace_events/runs.output (Codex review
# finding). The structured result stays fully available via
# automation_item_results -- normalize_run_result still gets the real text,
# just never the trace/output columns.
_PM_TRACE_REDACTED = "[redacted -- Property Maintenance Inbox response; see automation results for this run]"

# A node's own buffered events (see adapters/langgraph_adapter.py) -- these
# describe paid work already done by the time any of them is yielded, so a
# cancellation check must be deferred across all of them until that node's
# own agent_completed (which carries its usage). Checking cancellation is
# safe for every OTHER event type -- see the checkpoint below.
_BUFFERED_NODE_EVENT_TYPES = frozenset(
    {
        "agent_started",
        "agent_progress",
        "tool_started",
        "tool_completed",
        "delegation_started",
        "delegation_completed",
        "subagent_started",
        "subagent_completed",
    }
)


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


def _safe_record_trace_event(db: Session, *, run_id: str, seq: int, event: TraceEvent) -> None:
    """Persist one TraceEvent as a `trace_events` row, isolating failures from
    run status -- same rationale as `_safe_record_usage`. `data` is always
    JSON-encoded (even a plain string) so the read side has one consistent
    `json.loads()` path regardless of the event's `data` shape."""
    try:
        db.add(
            TraceEventRecord(
                run_id=run_id,
                seq=seq,
                type=event.type,
                agent=event.agent,
                data=json.dumps(event.data),
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 -- trace persistence must never break a run
        _logger.warning("Trace event persistence failed for run %s; run unaffected", run_id, exc_info=True)
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
    seq = 0
    # Declared here (not inside the streaming loop below) so every terminal
    # path -- the loop's own run_completed/run_failed, _mark_cancelled, and
    # the outer except-Exception fallback -- can see whatever trace evidence
    # was collected before the run ended, even a cancellation or crash that
    # happened before a single tool_completed event was ever yielded.
    confirmed_draft_message_ids: set[str] = set()
    failed_tool_message_ids: set[str] = set()

    def _maybe_normalize(raw_output_override: Optional[str] = None) -> None:
        # Property Maintenance Inbox (and any future vertical using the same
        # envelope contract): turn this triggered run's output into immutable,
        # queryable automation_item_results rows. A no-op for any run whose
        # output isn't one of these envelopes -- see automation_results.py.
        # Called from every terminal path (completed/failed/cancelled/crashed)
        # so a batch that never reaches a parseable envelope still gets
        # synthetic per-UID error rows instead of silently disappearing from
        # Needs-attention (spec 10.1; Codex review finding). `raw_output_override`
        # carries the real (unredacted) agent text for a property-maintenance
        # run whose `run_row.output` has already been overwritten with
        # `_PM_TRACE_REDACTED` by the time this runs -- normalization still
        # needs the real JSON even though nothing else does.
        if run_row is not None and run_row.trigger_context is not None:
            normalize_run_result(
                db,
                run_row,
                confirmed_draft_message_ids=confirmed_draft_message_ids,
                failed_tool_message_ids=failed_tool_message_ids,
                raw_output_override=raw_output_override,
            )

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
        is_pm_contract_run = bool(
            run_row is not None
            and run_row.trigger_context
            and run_row.trigger_context.get("result_contract") == RESULT_TYPE_BATCH_MARKER
        )
        # The SDK has no notion of "queued" (it only starts once `.stream()`
        # begins), so this bookend is synthesized here -- published to the
        # live registry the same way every other event is, so a WS
        # subscriber's replay log agrees with the persisted trace_events
        # history below (review finding P3: this used to be persisted only,
        # leaving the live log starting at run_started).
        run_queued_event = TraceEvent(type="run_queued", workflow=getattr(workflow, "name", ""), data=None)
        registry.publish(run_id, dataclasses.asdict(run_queued_event))
        if db is not None:
            _safe_record_trace_event(db, run_id=run_id, seq=seq, event=run_queued_event)
            seq += 1
        def _mark_cancelled() -> None:
            # Cooperative cancellation only, never a forceful thread kill (not
            # safely possible mid-`workflow.stream()`) -- this only runs
            # between yielded events, so it can't cut off a node already in
            # flight (see registry.py::request_cancel).
            nonlocal seq, terminal_seen
            cancelled = TraceEvent(
                type="run_cancelled", workflow=getattr(workflow, "name", ""), data="Run was cancelled."
            )
            # Commit + normalize BEFORE publish/trace-record (not after, as
            # this used to do) -- same ordering rationale as the streaming
            # loop's run_completed/run_failed branch: a live subscriber that
            # refetches automation-results the instant it observes the
            # terminal event must never be able to race ahead of these rows
            # (Codex review finding).
            if db is not None and run_row is not None:
                run_row.status = "cancelled"
                run_row.output = cancelled.data
                db.commit()
                _maybe_normalize()
            registry.publish(run_id, dataclasses.asdict(cancelled))
            if db is not None:
                _safe_record_trace_event(db, run_id=run_id, seq=seq, event=cancelled)
                seq += 1
            terminal_seen = True

        if registry.cancel_requested(run_id):
            # Already cancelled before this worker even started (e.g. queued
            # behind other runs on the thread pool) -- skip streaming entirely.
            _mark_cancelled()
        else:
            stream_iter = workflow.stream(input, user_id=user_id, memory=memory)
            for event in stream_iter:
                raw_run_completed_output: Optional[str] = None
                if is_pm_contract_run and event.type in ("agent_completed", "run_completed"):
                    # `run_completed.data` is the same raw agent text as
                    # `agent_completed.data` (core/workflow.py's `last_output`)
                    # -- normalize_run_result still needs the real JSON, so
                    # it's captured here before the event itself is redacted
                    # for everyone else (live subscribers, trace_events,
                    # runs.output).
                    if event.type == "run_completed":
                        raw_run_completed_output = event.data
                    event.data = _PM_TRACE_REDACTED
                payload = dataclasses.asdict(event)
                if (
                    event.type == "tool_completed"
                    and isinstance(event.data, dict)
                    and event.data.get("tool") == "email_draft_reply"
                    and event.data.get("success")
                    and event.data.get("outcome") == "draft_created"
                ):
                    # Ground truth for automation_results.py's normalization:
                    # a model's envelope can CLAIM action.draft_created for any
                    # message id, but only a real, successfully-executed
                    # email_draft_reply tool call for that id counts -- see
                    # adapters/langgraph_adapter.py's redacted tool_completed data.
                    message_id = event.data.get("message_id")
                    if message_id:
                        confirmed_draft_message_ids.add(message_id)
                if (
                    event.type == "tool_completed"
                    and isinstance(event.data, dict)
                    and event.data.get("tool") in ("email_read", "email_draft_reply")
                    and (
                        event.data.get("success") is False
                        or event.data.get("outcome") in ("not_found", "out_of_batch")
                    )
                ):
                    # Same trust boundary as confirmed_draft_message_ids above, for
                    # the opposite direction: a model's envelope can just as easily
                    # under-report a tool failure for a message id as it can
                    # over-claim a draft, so this run's own trace -- not the
                    # model's self-report -- is what forces needs_attention for
                    # that UID (spec 9.5 "Tool failure -> needs_attention: yes").
                    # A `not_found`/`out_of_batch` outcome is reported as
                    # `success: True` by the adapter (the call itself didn't
                    # raise), but it's just as much a rejected/unresolved
                    # action on that message id as a raised exception, so it
                    # gets the same forced escalation (Codex review finding).
                    message_id = event.data.get("message_id")
                    if message_id:
                        failed_tool_message_ids.add(message_id)
                if event.type in ("run_completed", "run_failed"):
                    if run_row is not None:
                        run_row.status = "completed" if event.type == "run_completed" else "failed"
                        run_row.output = event.data  # already redacted above for a PM-contract run
                        # Committed, and normalization run, BEFORE both
                        # `terminal_seen = True` below and the terminal event's
                        # publish further down -- the former so a commit
                        # failure still lets the except-Exception fallback
                        # publish a real run_failed event instead of silently
                        # leaving the run "stuck" from a live subscriber's
                        # perspective (Codex review finding), the latter so a
                        # WS subscriber that refetches automation-results the
                        # instant it observes run_completed/run_failed can
                        # never race ahead of these rows.
                        db.commit()
                        _maybe_normalize(raw_run_completed_output)
                    terminal_seen = True
                registry.publish(run_id, payload)
                if db is not None:
                    _safe_record_trace_event(db, run_id=run_id, seq=seq, event=event)
                    seq += 1
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
                # Skip the cancellation check only for a node's own buffered
                # granular events (tool_started/tool_completed/etc., flushed
                # together right before that node's agent_completed -- see
                # adapters/langgraph_adapter.py). The paid model/tool calls
                # they describe already happened, so stopping between them and
                # their agent_completed would silently drop that node's usage
                # from usage_records (review finding P1). Every other event --
                # notably run_started, reached before any node has started --
                # is a safe boundary and must still be checked, or a
                # cancellation already known at that point (e.g. requested
                # during compile/memory-recall, before run_started was even
                # yielded) goes unhonored for a whole avoidable extra paid
                # agent turn (round-2 review P1).
                if (
                    not terminal_seen
                    and event.type not in _BUFFERED_NODE_EVENT_TYPES
                    and registry.cancel_requested(run_id)
                ):
                    stream_iter.close()
                    _mark_cancelled()
                    break
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
            failed_event = TraceEvent(type="run_failed", workflow=getattr(workflow, "name", ""), data=message)
            # Persist status + normalize BEFORE publish/trace-record (not
            # after, as this used to do) -- same ordering rationale as the
            # streaming loop's terminal branch and _mark_cancelled: a live
            # subscriber that refetches automation-results the instant it
            # observes run_failed must never be able to race ahead of these
            # rows (Codex review finding). Still best-effort (swallowed on
            # failure below) so the terminal event further down -- the hard
            # CR-003 guarantee -- always gets published even if this fails.
            if db is not None and run_row is not None:
                try:
                    db.rollback()
                    run_row.status = "failed"
                    run_row.output = message
                    db.add(run_row)
                    db.commit()
                    # A triggered run that fails before workflow.stream() ever
                    # yields an event (e.g. a compile failure) previously
                    # skipped normalization entirely, so its UID batch just
                    # disappeared from Needs-attention instead of getting the
                    # synthetic error rows spec 10.1 requires (Codex review
                    # finding).
                    _maybe_normalize()
                except Exception:  # noqa: BLE001
                    _logger.warning("Could not persist failed status for run %s", run_id)
            registry.publish(run_id, dataclasses.asdict(failed_event))
            if db is not None:
                # `_safe_record_trace_event` rolls back internally on failure, so
                # it self-heals a session left poisoned by whatever raised above.
                _safe_record_trace_event(db, run_id=run_id, seq=seq, event=failed_event)
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
