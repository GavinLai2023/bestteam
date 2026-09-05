from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator, List, Optional, Sequence

from ..exceptions import BestTeamError, ConfigurationError
from .team import Team
from .trace import TraceEvent

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..adapters.base import EngineAdapter
    from .memory import MemoryManager, MemoryOutcome, RecallResult


def _safe_recall(
    memory: "Optional[MemoryManager]", user_id: Optional[str], input: str
) -> "RecallResult":
    """Recall memory best-effort, returning a `RecallResult` whose ``ok`` is False
    only when recall raised. Recall is a read on an optional, opt-in subsystem;
    like record_run (the write), it must never break a run, so any failure
    degrades to an empty result rather than propagating as run_failed.

    A custom manager (or subclass) that overrides only `recall_preamble` is
    honored — the richer `recall()` is used just for the stock `MemoryManager`,
    so a legacy `recall_preamble` override isn't silently bypassed (review r4 #4)."""
    from .memory import MemoryManager, RecallResult

    if not memory:
        return RecallResult(preamble="", count=0)
    try:
        overridden = getattr(type(memory), "recall_preamble", None)
        if overridden is not None and overridden is not MemoryManager.recall_preamble:
            preamble = memory.recall_preamble(user_id, input)
            return RecallResult(preamble=preamble, count=1 if preamble else 0)
        return memory.recall(user_id, input)
    except Exception:  # noqa: BLE001 -- memory must never break a run
        _logger.exception("Memory recall failed; run proceeds without recalled memory")
        return RecallResult(preamble="", count=0, ok=False)


@dataclass
class PipelineResult:
    """Normalized output of a pipeline run, independent of the underlying engine.

    Whatever engine produced it (LangGraph today, possibly CrewAI tomorrow),
    customer code only ever sees this shape.
    """

    output: str
    steps: List[dict] = field(default_factory=list)
    raw: Any = None
    # Per-user memory instrumentation for this run (SP-3), when memory was active;
    # both None when memory was disabled. Give `run()` callers the same visibility
    # `stream()` exposes via events: `memory` is the recording outcome
    # (`ok=False` = a recording failure), `recall` the recall outcome
    # (`count`=records drawn, `ok=False` = a recall failure) — reviews r4 #3 / r6 #3.
    memory: "Optional[MemoryOutcome]" = None
    recall: "Optional[RecallResult]" = None


class Pipeline:
    """Top-level entry point: chains teams into a runnable business pipeline."""

    def __init__(
        self,
        name: str,
        steps: Sequence[Team],
        adapter: Optional["EngineAdapter"] = None,
    ) -> None:
        if not name:
            raise ConfigurationError("Pipeline.name is required")
        if not steps:
            raise ConfigurationError(f"Pipeline '{name}' needs at least one step")

        self.name = name
        self.steps: List[Team] = list(steps)
        self._adapter = adapter or self._default_adapter()
        self._compiled: Any = None

    @staticmethod
    def _default_adapter() -> "EngineAdapter":
        from ..adapters.langgraph_adapter import LangGraphAdapter

        return LangGraphAdapter()

    def run(
        self,
        input: str,
        *,
        user_id: Optional[str] = None,
        memory: Optional["MemoryManager"] = None,
        diagnostic: bool = False,
    ) -> PipelineResult:
        """Execute the pipeline end-to-end and return a normalized result.

        When both `user_id` and `memory` are given, per-user memory is recalled
        into every agent's system prompt before the run and the run is recorded
        afterward (see `core/memory.py`). Both default to None → no memory,
        current behavior unchanged. `diagnostic` is forwarded to the adapter
        (see `stream`).
        """
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)

        recall_result = _safe_recall(memory, user_id, input)
        result = self._adapter.execute(
            self._compiled, input, memory_preamble=recall_result.preamble, diagnostic=diagnostic
        )
        if memory:
            # Surface recall + recording instrumentation for parity with stream()
            # (reviews r4 #3 / r6 #3). Both stay None when memory is disabled.
            result.recall = recall_result
            # Best-effort: a recording failure must not raise here and make a
            # completed, side-effecting run look failed to the caller.
            try:
                result.memory = memory.record_run(user_id, input, result.output)
            except Exception:  # noqa: BLE001 -- memory must never break a run
                _logger.exception("Memory recording failed after run; run stays completed")
        return result

    def stream(
        self,
        input: str,
        *,
        user_id: Optional[str] = None,
        memory: Optional["MemoryManager"] = None,
        diagnostic: bool = False,
        on_token: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_live_event: Optional[Callable[["TraceEvent"], None]] = None,
    ) -> Iterator[TraceEvent]:
        """Run the pipeline while yielding live TraceEvents — what the
        monitoring UI subscribes to.

        `on_token`, if given, receives the text deltas of the one agent whose
        output is the run's answer, as the model produces them. Deltas are not
        TraceEvents: they are a live-only side channel and must never be
        persisted -- the authoritative answer is `run_completed`'s data.
        `should_cancel` is polled between deltas so a long reply can be
        stopped mid-generation rather than merely ignored. Both default to
        None → no streaming, current behavior unchanged.

        `on_live_event`, if given, receives an `agent_working` TraceEvent as
        each agent starts (and as a delegated subordinate starts/finishes),
        before the node's own events reach this iterator. Live-only: never
        yielded here, never persisted. Default None → unchanged.

        `diagnostic=True` (an admin's diagnostic re-run) makes the adapter also
        emit the prompts, model turns and tool args/results a normal trace
        leaves out -- see `core/trace.py`. Default off: the stream is unchanged.

        Wraps the adapter's raw event stream with run-level bookends
        (`run_started`/`run_completed`/`run_failed`) and stamps every event
        with this pipeline's name. A failure surfaces as a terminal
        `run_failed` event rather than an exception breaking the generator
        mid-iteration — simpler for consumers like a WebSocket handler to relay.

        When both `user_id` and `memory` are given, per-user memory is recalled
        into the run before the first event and recorded *before* `run_completed`
        (never on the `run_failed` path), so `run_completed` is the final event
        and the memory events reach a consumer that stops on the terminal event
        (review r4 #2). Recording stays best-effort. Both default to None → no
        memory, current behavior unchanged.
        """
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)

        recall_result = _safe_recall(memory, user_id, input)

        yield TraceEvent(type="run_started", pipeline=self.name, data=input)

        # Observability (M-05): for an active-memory run, always surface the recall
        # attempt — a count (0 when nothing matched, distinguishing it from memory
        # being disabled) or a sanitized failure marker.
        if memory:
            # The query-expansion call's usage (SP-3-style metering) is billable
            # even when the recall that follows it fails, so it rides whichever
            # of these two events is emitted -- mirrors how `memory_recorded`/
            # `memory_failed` carry the extraction call's usage below.
            expansion_usage = [recall_result.expansion_usage] if recall_result.expansion_usage else []
            if recall_result.ok:
                yield TraceEvent(
                    type="memory_recalled",
                    pipeline=self.name,
                    data=recall_result.count,
                    usage=expansion_usage,
                )
            else:
                yield TraceEvent(
                    type="memory_failed", pipeline=self.name, data="recall", usage=expansion_usage
                )

        last_output = ""
        try:
            # Passed only when actually in use. The two arguments are part of
            # the `EngineAdapter` ABC, but sending them unconditionally would
            # raise TypeError on an adapter written against the older
            # signature -- and this is a documented extension seam, so an
            # ordinary non-streaming run must keep working through one
            # (Codex review finding).
            streaming_kwargs = {}
            if on_token is not None:
                streaming_kwargs["on_token"] = on_token
            if should_cancel is not None:
                streaming_kwargs["should_cancel"] = should_cancel
            if on_live_event is not None:
                streaming_kwargs["on_live_event"] = on_live_event
            for event in self._adapter.stream(
                self._compiled,
                input,
                memory_preamble=recall_result.preamble,
                diagnostic=diagnostic,
                **streaming_kwargs,
            ):
                event = dataclasses.replace(event, pipeline=self.name)
                if event.type == "agent_completed":
                    last_output = event.data
                yield event
        except BestTeamError as exc:
            yield TraceEvent(type="run_failed", pipeline=self.name, data=str(exc))
            return

        # The business run is complete: emit the terminal event NOW, before the
        # optional memory recording. Recording (which may include a slow/hung
        # extraction LLM call) must never delay or wedge a finished run, so it
        # runs AFTER `run_completed` (review r7 — this dissolves the need for a
        # timeout on the extraction). The backend still meters/records these
        # post-terminal events because it drains the whole event stream; a live
        # WebSocket that stops on `run_completed` won't display them, but no
        # durable billing/provenance data depends on that. Recording stays
        # best-effort: a failure is a sanitized `memory_failed`, never `run_failed`.
        yield TraceEvent(type="run_completed", pipeline=self.name, data=last_output)

        if memory:
            from .memory import MemoryOutcome

            try:
                outcome = memory.record_run(user_id, input, last_output)
            except Exception:  # noqa: BLE001 -- memory must never break a run
                _logger.exception("Memory recording failed; run already completed")
                yield TraceEvent(type="memory_failed", pipeline=self.name, data="record")
            else:
                # A legacy manager may return None (recorded successfully, no
                # structured outcome) -- that is NOT a failure (review r5 #3).
                if isinstance(outcome, MemoryOutcome):
                    # The extraction call's usage must be metered even if every
                    # write failed (the paid call still happened, M-04 / review
                    # r6 #1). Carry it on whichever event is emitted, exactly once.
                    usage = [outcome.extraction_usage] if outcome.extraction_usage else []
                    if outcome.recorded:
                        # Observability (M-05) + metering (M-04): report what was
                        # written and carry the extraction usage for the backend.
                        yield TraceEvent(
                            type="memory_recorded",
                            pipeline=self.name,
                            data=", ".join(outcome.recorded),
                            usage=usage,
                        )
                        usage = []  # already carried by memory_recorded
                    if not outcome.ok:
                        # A partial/total write failure is observable; when no
                        # write succeeded it carries the (still-billable) usage.
                        yield TraceEvent(
                            type="memory_failed", pipeline=self.name, data="record", usage=usage
                        )

    def visualize(self) -> str:
        """Render the compiled graph as Mermaid markup (for the future CLI/UI)."""
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)
        return self._adapter.to_mermaid(self._compiled)
