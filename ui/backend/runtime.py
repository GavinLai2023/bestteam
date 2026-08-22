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
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from bestteam import MemoryManager, Pipeline, SqliteBM25Memory
from bestteam.adapters.langgraph_adapter import STREAM_RESET
from bestteam.core.trace import TraceEvent

from . import error_reporting

from .automation_results import (
    CONFIRMED_DRAFT_OUTCOMES,
    RESULT_TYPE_BATCH_MARKER,
    already_drafted_uids,
    normalize_run_result,
)
from .db.inbox_events import (
    EVENT_CLAIMED,
    claimed_events,
    complete_events,
    release_events,
)
from .db.models import InboxEvent, Run, TraceEventRecord
from .db.usage import record_usage
from .registry import RunRegistry
from .share_transcript import record_share_reply

_logger = logging.getLogger(__name__)

registry = RunRegistry()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-run")


# Token deltas are coalesced before they become WebSocket frames: one frame
# per token is wasteful on a public surface and jitters badly on a phone.
#
# Both thresholds are evaluated when a delta ARRIVES, never on a timer -- so
# the honest statement is "a delta flushes the buffer once either 40
# characters have accumulated or 80 ms have passed since the last flush",
# not "the buffer is flushed within 80 ms". In practice the time threshold is
# the one that fires: at a typical 30 tokens/second, ~3 tokens cross 80 ms
# well before 10 of them cross 40 characters, which is what keeps a real
# stream smooth rather than arriving in 40-character steps. What the absence
# of a timer costs is the tail: if the provider stalls mid-reply, up to 39
# characters sit unshown until the next delta. A timer thread was considered
# and rejected for that -- a second thread per run, publishing across a
# lock, to reveal a sub-word tail during a pause when the run's own
# `run_completed`/event flush already bounds it (Codex review finding).
_TOKEN_FLUSH_CHARS = 40
_TOKEN_FLUSH_SECONDS = 0.08


class _TokenSink:
    """Coalesces one run's token deltas into `reply_delta` events.

    Called synchronously from the worker thread, inside the final agent's
    model loop (see
    docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md).
    Publishes through `registry.publish_transient`, so nothing here is
    recorded, replayed or persisted -- the authoritative reply is still the
    one `run_completed` carries and `record_share_reply` stores.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._buffer: List[str] = []
        self._pending = 0
        self._last_flush = time.monotonic()
        # The FIRST flush of a reply waits for the character threshold, never
        # the time one. A tool-capable agent can emit a short preamble ("Let
        # me check the pricing handbook") and only then decide to call a tool;
        # `STREAM_RESET` retracts that from the screen, but it cannot retract
        # bytes already sent over the visitor's socket (Codex review finding).
        # Holding the first flush to 40 characters means a short preamble --
        # the common shape -- never crosses the wire at all. A longer one
        # still can; see docs/STATUS.md, Known issues.
        self._flushed_any = False

    def __call__(self, delta: str) -> None:
        if delta == STREAM_RESET:
            # The text so far belonged to what turned out to be a tool call.
            self._buffer.clear()
            self._pending = 0
            # Back to "nothing sent yet": whatever follows is a fresh reply and
            # gets the same first-flush protection.
            self._flushed_any = False
            registry.publish_transient(self._run_id, {"type": "reply_reset", "data": None})
            return
        self._buffer.append(delta)
        self._pending += len(delta)
        if self._pending >= _TOKEN_FLUSH_CHARS or (
            self._flushed_any and time.monotonic() - self._last_flush >= _TOKEN_FLUSH_SECONDS
        ):
            self.flush()

    def flush(self) -> None:
        """Publish whatever is buffered. A no-op when there is nothing."""
        self._last_flush = time.monotonic()
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._pending = 0
        self._flushed_any = True
        registry.publish_transient(self._run_id, {"type": "reply_delta", "data": text})

INTERRUPTED_RUN_MESSAGE = (
    "The run was interrupted by a server restart before it finished."
)


def fail_interrupted_runs(engine: Engine, *, max_event_attempts: int) -> int:
    """Resolve every `running` row to `failed` and return how many.

    Called once from `main.py::_lifespan`, beside
    `ingestion.fail_interrupted_jobs`. The run executor lives in this process,
    so a row still `running` when the app starts belongs to a worker that no
    longer exists and will never reach a terminal event. Left alone it is
    permanent: the Activity page shows it running forever and its retry path
    (gated on `failed`) never appears.

    A dead run gets the same infrastructure-class treatment
    `email_trigger._release_stale_run` gives a hung one: mark the row failed,
    hand back the inbox events it had claimed (`release_events` -- pending
    again, or dead-lettered once `max_event_attempts` is used up, so the next
    poll reprocesses them instead of leaving them `claimed` by a run that will
    never finish), and normalise a declared maintenance batch so it does not
    vanish from Needs-attention. Duplicate drafts on reprocessing are guarded
    where `_release_stale_run` relies on it too: `email_draft_reply` checks the
    Drafts folder for the message's source key before APPEND. Usage rows and
    trace events are left as they are.
    """
    swept = 0
    with Session(engine) as db:
        dead = db.query(Run).filter(Run.status == "running").all()
        for run_row in dead:
            run_row.status = "failed"
            run_row.output = INTERRUPTED_RUN_MESSAGE
            dead_lettered = release_events(
                db, run_row.id, max_attempts=max_event_attempts, error=INTERRUPTED_RUN_MESSAGE
            )
            if dead_lettered:
                _logger.warning(
                    "Run %s was interrupted by a restart; %s of its messages have used up their "
                    "attempts and were dead-lettered", run_row.id, dead_lettered,
                )
            swept += 1
        _release_orphaned_claims(db, max_event_attempts=max_event_attempts)
        db.commit()
        for run_row in dead:
            normalize_run_result(db, run_row)  # never raises; no-op unless a declared batch
    return swept


def _release_orphaned_claims(db: Session, *, max_event_attempts: int) -> None:
    """Hand back every claim no live worker can possibly own.

    Runs after the loop above, and every `claimed` row that survives it is
    orphaned **by definition**: the run executor is per-process, so nothing
    in this fresh process owns one, and each `running` run's claims were just
    released. Two kinds reach here, and neither is visible to a
    `Run.status == "running"` query:

    - a claim committed by `_start_triggered_run` before the `runs` row was
      written (it commits the claim on its own so a build failure can release
      it penalty-free, then builds the pipeline, then inserts the row), so
      `run_id` names a row that does not exist;
    - a claim outstanding on a run that already reached a terminal status,
      because `complete_events` runs on the worker thread after that commit.

    Left alone, either stays `claimed` forever -- invisible to `claim_events`
    and to `has_pending_events` alike, so the mail is never processed and
    nothing anywhere reports it.

    Deliberately not a lease with a periodic scavenger: the process boundary
    IS the lease here, and `email_trigger._release_stale_run` already covers
    the other case, a run that is alive but hung.
    """
    orphaned = db.execute(
        select(InboxEvent.run_id)
        .where(InboxEvent.status == EVENT_CLAIMED, InboxEvent.run_id.isnot(None))
        .distinct()
    ).scalars().all()
    for run_id in orphaned:
        outstanding = len(claimed_events(db, run_id))
        dead_lettered = release_events(
            db, run_id, max_attempts=max_event_attempts, error=INTERRUPTED_RUN_MESSAGE
        )
        _logger.warning(
            "Released %s orphaned inbox claim(s) left by run %s; %s were dead-lettered",
            outstanding, run_id, dead_lettered,
        )

# A property-maintenance-batch run's agent output is derived from customer
# email content (the envelope's free-text `extracted`/`missing_information`/
# `risk_reasons` fields can quote it directly) -- the same trust boundary that
# already redacts tool_completed data for email_find/email_read/
# email_read_attachment/email_draft_reply (adapters/langgraph_adapter.py)
# applies here too, so this placeholder replaces the raw agent_completed/
# run_completed text before it's ever published live or persisted to
# trace_events/runs.output (Codex review finding). The structured result
# stays fully available via
# automation_item_results -- normalize_run_result still gets the real text,
# just never the trace/output columns.
_PM_TRACE_REDACTED = "[redacted -- Property Maintenance Inbox response; see automation results for this run]"

# If a declared maintenance pipeline uses HIERARCHICAL mode, the manager's
# delegate/subordinate exchange carries the same customer-email-derived text
# as agent_completed/run_completed (delegation_started/subagent_started's
# `task_summary` is the manager's own free-text hand-off, and
# subagent_completed/delegation_completed's `summary` is the subordinate's
# raw output) -- all four must be redacted for a PM-contract run too, or that
# content leaks around the boundary above (Codex review finding).
_PM_REDACTED_EVENT_TYPES = frozenset(
    {
        "agent_completed",
        "run_completed",
        "subagent_started",
        "subagent_completed",
        "delegation_started",
        "delegation_completed",
    }
)


def _is_delegate_tool_completed(event: TraceEvent) -> bool:
    """True for the MANAGER's own `tool_completed` event from calling a
    `delegate_to_<name>` tool (`adapters/langgraph_adapter.py`'s generic
    tool-calling loop, not the `on_event`-driven subagent_completed/
    delegation_completed events above) -- its `summary` is the same raw
    subordinate output `_PM_REDACTED_EVENT_TYPES` already redacts, just
    reaching the trace through a second, separate event (Codex review
    finding)."""
    return (
        event.type == "tool_completed"
        and isinstance(event.data, dict)
        and str(event.data.get("tool", "")).startswith("delegate_to_")
    )


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


_TRIGGER_RUN_FAILED_MESSAGE = (
    "The last automatic run didn't finish successfully. Automatic runs will keep "
    "trying when new mail arrives -- open the run for details."
)


def _apply_trigger_health(db: Session, trigger, run_status: str) -> None:
    """Fold a terminal run status into the trigger's alert state.

    Additive: the caller's existing `last_error`/`last_error_kind` writes stay
    exactly where they were -- those drive the dashboard's error surface and
    are pinned by Phase 0's tests. This adds the *telling someone* half.

    Late import for the same reason as `get_email_trigger` below: `email_trigger`
    imports this module, so a module-level import would be circular.
    """
    from . import trigger_health
    from .db.notifications import create_notification

    outcome = (
        trigger_health.OUTCOME_PIPELINE_OK
        if run_status == "completed"
        else trigger_health.OUTCOME_PIPELINE
    )
    decision = trigger_health.evaluate(
        outcome=outcome,
        consecutive_faults=trigger.consecutive_faults or 0,
        alerted_fingerprint=trigger.alerted_fingerprint,
        threshold=trigger_health.alert_threshold(),
    )
    trigger.consecutive_faults = decision.consecutive_faults
    trigger.alerted_fingerprint = decision.alerted_fingerprint
    if decision.notification is not None:
        draft = decision.notification
        create_notification(
            db, org_id=trigger.org_id, kind=draft.kind, severity=draft.severity,
            title=draft.title, body=draft.body, fingerprint=draft.fingerprint,
        )


def _safe_record_trigger_health(db: Session, run_row) -> None:
    """Reflect an autonomous email run's outcome on its org's `EmailTrigger`.

    Before this, nothing ever wrote a *workflow* fault back from the run side:
    `_start_triggered_run` clears `last_error` when it dispatches, and only the
    poller's own mailbox check could set one. So an org whose team failed on
    every single run kept showing a healthy, "Active" trigger indefinitely,
    with the failures visible only to someone who opened the run list
    (Phase 0, item 0.5).

    A `workflow`-kind fault is sticky by existing convention -- it persists
    across empty polls and clears only on a real success -- so a successful run
    clears it here, while a `mailbox`-kind fault is left strictly alone: it is
    owned by the connectivity check, and a workflow outcome says nothing about
    whether the mailbox is reachable.

    Isolated like `_safe_record_usage`: a health write must never flip an
    otherwise-successful run to failed.
    """
    trigger_context = getattr(run_row, "trigger_context", None) or {}
    if trigger_context.get("trigger_type") != "email" or run_row.org_id is None:
        return
    try:
        # Late import: email_trigger imports this module, so a module-level
        # import here would be circular.
        from .db.email_triggers import get_email_trigger

        trigger = get_email_trigger(db, run_row.org_id)
        if trigger is None:
            return
        # A finishing run is not necessarily the run this trigger is waiting
        # on. The stale-run watchdog releases a wedged run's overlap guard and
        # lets a new run start while the old one is still executing, so the
        # old one's outcome arrives late and stale -- applying it would let an
        # abandoned run's failure overwrite health the current run just
        # established, or an old success clear a current failure. `None` means
        # no run is being tracked (a pre-watchdog or freshly-enabled trigger),
        # which stays permissive.
        if trigger.last_run_id is not None and trigger.last_run_id != run_row.id:
            return
        if run_row.status in ("failed", "cancelled"):
            trigger.last_error = _TRIGGER_RUN_FAILED_MESSAGE
            trigger.last_error_kind = "workflow"
        elif run_row.status == "completed":
            if trigger.last_error_kind == "workflow":
                trigger.last_error = None
                trigger.last_error_kind = None
        else:
            # Not a terminal status: nothing is known yet, so neither the error
            # surface nor the alert state should move.
            return
        # Alerting runs for every terminal outcome, including a completed run
        # that had no `workflow` error to clear -- the success still ends a
        # fault streak and can clear a timeout alert.
        _apply_trigger_health(db, trigger, run_row.status)
        db.commit()
    except Exception:  # noqa: BLE001 -- health reporting must never break a run
        _logger.warning(
            "Trigger health update failed for run %s; run unaffected", run_row.id, exc_info=True
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _safe_complete_inbox_events(db: Session, run_row) -> None:
    """Give this run's claimed inbox events their terminal status.

    Everything that reaches here has actually executed the model, which is what
    makes "terminal here => workflow-class" sound: the two infrastructure-class
    paths (a failed dispatch, and the stale-run watchdog) never get this far and
    release their events at the site instead.

    A failed run still leaves real drafts behind for the messages it got
    through, so `already_drafted_uids` -- Phase 0's union of trace evidence, the
    X-BestTeam-Source-Key mailbox scan and automation_item_results -- decides
    which are done. Reprocessing one of those would create a second draft, since
    `email_draft_reply` has no dedup of its own. The rest are terminal and wait
    for the human retry, which is today's product behaviour for a run whose
    model ran and failed.

    Isolated like `_safe_record_usage`: bookkeeping must never flip an otherwise
    successful run to failed.
    """
    trigger_context = getattr(run_row, "trigger_context", None) or {}
    if trigger_context.get("trigger_type") != "email" or run_row.org_id is None:
        return
    try:
        if run_row.status == "completed":
            done = {str(u) for u in (trigger_context.get("uids") or [])}
            error = None
        else:
            done = {str(u) for u in already_drafted_uids(db, run_row)}
            error = _TRIGGER_RUN_FAILED_MESSAGE
        complete_events(db, run_row.id, done_external_ids=done, error=error)
        db.commit()
    except Exception:  # noqa: BLE001 -- bookkeeping must never break a run
        _logger.warning(
            "Inbox event completion failed for run %s; run unaffected",
            run_row.id, exc_info=True,
        )
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


_SHARE_REPLY_MAX_ATTEMPTS = 3
_SHARE_REPLY_RETRY_DELAY_SECONDS = 0.05


def _safe_record_share_reply(db: Optional[Session], run_row: Run, output: Optional[str]) -> None:
    """Append a share-chat run's assistant reply, isolating failures from the
    run's own terminal handling -- same rationale as `_safe_record_usage`.

    This used to be called unguarded, before `terminal_seen = True` and before
    `registry.publish`, so an `append_message` failure (a
    `(share_session_id, turn_number)` collision, a transient DB error) aborted
    the whole terminal branch: the terminal event never reached the live WS
    subscriber and the outer handler then treated the run as crashed and
    recorded a SECOND reply for it (final whole-branch review I5).

    A single attempt still left a gap: a transient DB failure here (nothing
    else in this function's own history has ever raised for a non-transient
    reason) left the session's last message an unanswered user turn with no
    way to recover -- every later send is rejected by `_has_pending_turn`
    forever, since nothing retries or repairs the write (Codex review
    finding). Retries a few times, with a brief pause to let a transient
    fault (e.g. momentary SQLite lock contention) clear, before giving up and
    logging loudly enough for an operator to notice and repair by hand.
    """
    for attempt in range(1, _SHARE_REPLY_MAX_ATTEMPTS + 1):
        try:
            record_share_reply(db, run_row, output)
            return
        except Exception:  # noqa: BLE001 -- a transcript write must never break a run
            _logger.warning(
                "Share reply recording failed for run %s (attempt %d/%d); run unaffected",
                run_row.id,
                attempt,
                _SHARE_REPLY_MAX_ATTEMPTS,
                exc_info=True,
            )
            try:
                if db is not None:
                    db.rollback()
            except Exception:  # noqa: BLE001
                pass
            if attempt < _SHARE_REPLY_MAX_ATTEMPTS:
                time.sleep(_SHARE_REPLY_RETRY_DELAY_SECONDS)
    _logger.error(
        "Share reply recording permanently failed for run %s after %d attempts; "
        "the visitor session will appear to have a pending turn until an "
        "operator repairs it",
        run_row.id,
        _SHARE_REPLY_MAX_ATTEMPTS,
    )


def _env_int(name: str, default: Optional[int], *, min_value: Optional[int] = 1) -> Optional[int]:
    """Int env config, or `default` when unset/blank/invalid. `min_value` (1 by
    default, matching every other caller's "positive-int" knobs) also falls
    back to `default` when the parsed value is below it. Pass `min_value=None`
    for a knob whose own consumer treats a non-positive value as meaningful
    (e.g. `query_expansion_count`'s documented `<=0` = disabled contract) --
    without this, `0`/negative silently couldn't reach that consumer at all."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("Ignoring non-integer %s=%r; using %r", name, raw, default)
        return default
    if min_value is not None and value < min_value:
        return default
    return value


def _make_memory(
    org_id: Optional[int] = None,
    *,
    principal_id: Optional[str] = None,
    run_id: Optional[str] = None,
    pipeline_version_id: Optional[int] = None,
    pipeline_id: Optional[int] = None,
) -> Optional[MemoryManager]:
    """Build a per-user `MemoryManager` from env, or None when memory is disabled.

    Called on the worker thread that runs the pipeline so the underlying
    SQLite connection stays thread-local (`SqliteBM25Memory` opens its
    connection in `__init__`).

    - `BESTTEAM_MEMORY_DB` unset/empty -> disabled (returns None; runs behave
      exactly as before).
    - set -> a `SqliteBM25Memory` at that path; `BESTTEAM_MEMORY_MODEL`, if
      set, enables semantic/procedural extraction via one LLM call per run.
    - `BESTTEAM_MEMORY_EMBEDDING_MODEL`, if set, enables hybrid (BM25 +
      vector, RRF-fused) recall with type-aware recency decay -- same spec
      convention as the vector knowledge base (`"fake:<dim>"` for $0 tests,
      or a provider string like `"openai:text-embedding-3-small"`). Unset ->
      pure-BM25 recall, byte-for-byte unchanged. `BESTTEAM_MEMORY_RECENCY_
      HALF_LIFE_DAYS` tunes the decay applied to EPISODIC/PROCEDURAL hits
      (SEMANTIC never decays); only meaningful when an embedding model is
      also set. A misconfigured embedding spec disables memory entirely,
      like a bad `BESTTEAM_MEMORY_DB` path (caught below).
    - `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL`, if set, enables query expansion:
      one extra LLM call per recall rewrites the query into up to
      `BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT` (default 3) alternative
      phrasings, each searched and fused via RRF alongside the literal query.
      Unlike `BESTTEAM_MEMORY_EMBEDDING_MODEL` (eagerly resolved at store
      construction, so a bad spec disables memory entirely), this is resolved
      lazily per-call inside its own try/except -- a bad spec never disables
      memory, it just makes that call's expansion silently no-op (same
      failure shape as `BESTTEAM_MEMORY_MODEL`/extraction). Unset -> recall is
      byte-for-byte unchanged.
    - `BESTTEAM_MEMORY_RERANK_MODEL`, if set, enables a cross-encoder rerank
      pass over the fused recall candidates for both scopes (same spec
      convention -- `"fake:"` for $0 tests, `"cross-encoder:<model-name>"`
      for a real local model via `sentence-transformers`).
      `BESTTEAM_MEMORY_RERANK_CANDIDATE_K` (default `top_k * 4`, clamped)
      tunes how many fused candidates reach the reranker. Like
      `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL`, this is resolved lazily and a
      bad spec never disables memory -- it just disables rerank for that
      run. See `core/memory.py`'s `_fused_search` and
      `docs/superpowers/specs/2026-08-12-pluggable-rerank-design.md`.

    `org_id` scopes every recall/record to the run's organization (SP-2), so a
    run only ever sees and writes its own org's memory. `pipeline_id` (the
    deployed team's stable `PipelineRecord.id`) additionally scopes
    episodic/procedural recall/writes to the current pipeline -- semantic facts
    stay org-wide regardless (see `core/memory.py::MemoryManager.recall`).
    """
    db_path = os.environ.get("BESTTEAM_MEMORY_DB", "").strip()
    if not db_path:
        return None
    embedding_model = os.environ.get("BESTTEAM_MEMORY_EMBEDDING_MODEL", "").strip() or None
    recency_half_life_days = _env_int("BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS", 14)
    try:
        store = SqliteBM25Memory(
            db_path,
            embedding_model=embedding_model,
            recency_half_life_days=recency_half_life_days,
        )
    except Exception as exc:  # noqa: BLE001 — memory must never break a run
        _logger.warning("Memory disabled: could not open store at %r: %s", db_path, exc)
        return None
    extraction_model = os.environ.get("BESTTEAM_MEMORY_MODEL", "").strip() or None
    query_expansion_model = os.environ.get("BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL", "").strip() or None
    rerank_model = os.environ.get("BESTTEAM_MEMORY_RERANK_MODEL", "").strip() or None
    rerank_candidate_k = _env_int("BESTTEAM_MEMORY_RERANK_CANDIDATE_K", None)
    return MemoryManager(
        store,
        extraction_model=extraction_model,
        org_id=org_id,
        principal_id=principal_id,
        pipeline_id=pipeline_id,
        run_id=run_id,
        pipeline_version_id=pipeline_version_id,
        # SP-4: production recall is bounded by default (M-09); episodic retention
        # is opt-in (M-07, destructive so default unbounded).
        recall_max_candidates=_env_int("BESTTEAM_MEMORY_RECALL_MAX_CANDIDATES", 1000),
        max_episodic_per_user=_env_int("BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER", None),
        query_expansion_model=query_expansion_model,
        # min_value=None: 0/negative must reach MemoryManager, which treats
        # <=0 as "expansion disabled" -- the default min_value=1 would instead
        # silently substitute the default count, defeating that setting.
        query_expansion_count=_env_int("BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT", 3, min_value=None),
        rerank_model=rerank_model,
        rerank_candidate_k=rerank_candidate_k,
    )


def run_in_background(
    run_id: str,
    pipeline: Pipeline,
    input: str,
    engine: Optional[Engine] = None,
    user_id: Optional[str] = None,
    org_id: Optional[int] = None,
    principal_id: Optional[str] = None,
    username: Optional[str] = None,
    pipeline_version_id: Optional[int] = None,
    pipeline_id: Optional[int] = None,
    diagnostic: bool = False,
) -> None:
    """Drain `Pipeline.stream()` on a worker thread and publish each event to
    the registry (thread-safe) so WebSocket subscribers see it as it happens.

    `diagnostic` is forwarded to `Pipeline.stream` (an admin's diagnostic
    re-run, `main.py::diagnose_run`): the extra events it produces are
    persisted and published like any other -- nothing else here changes.

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
            pipeline_version_id=pipeline_version_id,
            pipeline_id=pipeline_id,
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
            # Same set of terminal paths, same ordering guarantee: the run's
            # own status is already committed by the time this runs.
            _safe_record_trigger_health(db, run_row)
            _safe_complete_inbox_events(db, run_row)

    def _maybe_record_share_reply(output: Optional[str]) -> None:
        # Share-chat turns (share_chat.py) are regular runs stamped with
        # trigger_context["share_session_id"] -- append the assistant's
        # reply (or record_share_reply's own friendly fallback) so the
        # visitor's chat page sees an answer and share_chat.py's
        # "last message is unanswered" guard never wedges the session shut.
        # No-op for every other run (see record_share_reply's own guard).
        # Always via _safe_record_share_reply: this is a secondary write, and
        # a failure here must never abort the terminal branch it sits in
        # (final whole-branch review I5).
        if run_row is not None:
            _safe_record_share_reply(db, run_row, output)

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
                    pipeline=getattr(pipeline, "name", ""),
                    input=input,
                    org_id=org_id,
                    username=username,
                    pipeline_version_id=pipeline_version_id,
                )
                db.add(run_row)
            else:
                # A caller (the autonomous trigger) already persisted this row
                # before dispatch as a durable activity record; keep it.
                run_row.pipeline = getattr(pipeline, "name", "") or run_row.pipeline
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
        run_queued_event = TraceEvent(type="run_queued", pipeline=getattr(pipeline, "name", ""), data=None)
        registry.publish(run_id, dataclasses.asdict(run_queued_event))
        if db is not None:
            _safe_record_trace_event(db, run_id=run_id, seq=seq, event=run_queued_event)
            seq += 1
        def _mark_cancelled() -> None:
            # Cooperative cancellation only, never a forceful thread kill (not
            # safely possible mid-`pipeline.stream()`) -- this only runs
            # between yielded events, so it can't cut off a node already in
            # flight (see registry.py::request_cancel).
            nonlocal seq, terminal_seen
            cancelled = TraceEvent(
                type="run_cancelled", pipeline=getattr(pipeline, "name", ""), data="Run was cancelled."
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
                _maybe_record_share_reply("This conversation was stopped before a reply was ready.")
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
            # Share-chat turns are the only consumer of token streaming today:
            # the monitor page has no UI for deltas, and pushing thousands of
            # unhandled events per run through an authenticated WebSocket
            # would buy nothing. One condition moves it later (see
            # docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md).
            share_session_id = (
                (run_row.trigger_context or {}).get("share_session_id") if run_row is not None else None
            )
            token_sink = _TokenSink(run_id) if share_session_id is not None else None
            stream_iter = pipeline.stream(
                input,
                user_id=user_id,
                memory=memory,
                diagnostic=diagnostic,
                on_token=token_sink,
                should_cancel=(lambda: registry.cancel_requested(run_id)) if token_sink else None,
            )
            for event in stream_iter:
                if token_sink is not None:
                    # Any buffered delta must reach the visitor before the
                    # event that supersedes it (`agent_completed`, then
                    # `run_completed` with the authoritative text). A no-op
                    # when the buffer is empty, which is every non-final event.
                    token_sink.flush()
                raw_run_completed_output: Optional[str] = None
                if is_pm_contract_run and (
                    event.type in _PM_REDACTED_EVENT_TYPES or _is_delegate_tool_completed(event)
                ):
                    # `run_completed.data` is the same raw agent text as
                    # `agent_completed.data` (core/pipeline.py's `last_output`)
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
                    and event.data.get("outcome") in CONFIRMED_DRAFT_OUTCOMES
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
                    and event.data.get("tool") in (
                        "email_read", "email_read_attachment", "email_draft_reply",
                    )
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
                    if event.type == "run_failed":
                        # The pipeline failed without raising (a provider or
                        # BestTeamError the SDK already turned into an event):
                        # still worth an operator's attention. Ids only -- the
                        # reason (`event.data`, an exception's text) can quote
                        # a prompt or a model's output, and is in the run's
                        # persisted trace on-box; see error_reporting.py.
                        error_reporting.report_message(
                            f"Run failed: {getattr(pipeline, 'name', '')}",
                            run_id=run_id,
                            pipeline=getattr(pipeline, "name", ""),
                        )
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
                        _maybe_record_share_reply(
                            run_row.output if event.type == "run_completed" else None
                        )
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
                if db is not None and event.type in ("memory_recorded", "memory_recalled", "memory_failed"):
                    # Meter the memory extraction/query-expansion LLM calls; both
                    # bypass the adapter's usage path, so each arrives on its own
                    # memory event (M-04). `memory_failed` is shared between a
                    # record failure (data="record") and a recall failure
                    # (data="recall") that may still carry billable expansion
                    # usage, so the agent label is picked per-event rather than
                    # hardcoded -- a recall-side call must never be mis-attributed
                    # as extraction. Each side attaches usage to exactly one event
                    # of its own (recorded/recalled, or failed when that side's
                    # only write/search failed), so this never double-counts
                    # (review r6 #1, extended to the recall side).
                    is_recall_side = event.type == "memory_recalled" or (
                        event.type == "memory_failed" and event.data == "recall"
                    )
                    agent = "memory:query_expansion" if is_recall_side else "memory:extraction"
                    for entry in event.usage:
                        _safe_record_usage(
                            db,
                            run_id=run_id,
                            agent=agent,
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
                #
                # Exception: when memory is active, `run_started` is ALSO
                # skipped -- it's immediately followed by exactly one
                # `memory_recalled`/`memory_failed` event, which is the sole
                # carrier of the (already-paid-for) query-expansion usage. A
                # cancellation discovered right after `run_started` would call
                # `stream_iter.close()` before that event is ever pulled from
                # the generator, silently dropping billable usage. Deferring
                # the check by that one event still stops before any agent
                # runs (memory_recalled/failed always precedes agent_started),
                # so the "avoidable paid agent turn" guarantee above is intact.
                if (
                    not terminal_seen
                    and event.type not in _BUFFERED_NODE_EVENT_TYPES
                    and not (event.type == "run_started" and memory is not None)
                    and registry.cancel_requested(run_id)
                ):
                    stream_iter.close()
                    _mark_cancelled()
                    break
    except Exception as exc:  # noqa: BLE001 -- any worker failure must still yield a terminal event
        # Pipeline.stream() compiles before its own BestTeamError handler, so a
        # compile failure (e.g. an unsupported collaboration mode) escapes as an
        # exception rather than a run_failed event. Without this catch-all the
        # run would stay "running" forever and subscribers would never see a
        # terminal event (CR-003). The message is sanitized; the real traceback
        # is logged server-side only. Only synthesize run_failed if no terminal
        # event was already published, so a post-completion failure (e.g. usage
        # recording) can't flip a completed run to failed.
        _logger.exception("Run %s failed on the worker thread", run_id)
        error_reporting.report_exception(exc, run_id=run_id, pipeline=getattr(pipeline, "name", ""))
        if not terminal_seen:
            message = "The run failed due to an internal error."
            failed_event = TraceEvent(type="run_failed", pipeline=getattr(pipeline, "name", ""), data=message)
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
                    # A triggered run that fails before pipeline.stream() ever
                    # yields an event (e.g. a compile failure) previously
                    # skipped normalization entirely, so its UID batch just
                    # disappeared from Needs-attention instead of getting the
                    # synthetic error rows spec 10.1 requires (Codex review
                    # finding).
                    _maybe_normalize()
                    _maybe_record_share_reply(None)
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
