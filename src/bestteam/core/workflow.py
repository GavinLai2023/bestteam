from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator, List, Optional, Sequence

from ..exceptions import BestTeamError, ConfigurationError
from .team import Team
from .trace import TraceEvent

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..adapters.base import EngineAdapter
    from .memory import MemoryManager, MemoryOutcome, RecallResult


def _safe_recall(
    memory: "Optional[MemoryManager]", user_id: Optional[str], input: str
) -> "tuple[RecallResult, bool]":
    """Recall memory best-effort, returning ``(result, ok)`` where ``ok`` is False
    only when recall raised. Recall is a read on an optional, opt-in subsystem;
    like record_run (the write), it must never break a run, so any failure
    degrades to an empty result rather than propagating as run_failed.

    A custom manager (or subclass) that overrides only `recall_preamble` is
    honored — the richer `recall()` is used just for the stock `MemoryManager`,
    so a legacy `recall_preamble` override isn't silently bypassed (review r4 #4)."""
    from .memory import MemoryManager, RecallResult

    if not memory:
        return RecallResult(preamble="", count=0), True
    try:
        overridden = getattr(type(memory), "recall_preamble", None)
        if overridden is not None and overridden is not MemoryManager.recall_preamble:
            preamble = memory.recall_preamble(user_id, input)
            return RecallResult(preamble=preamble, count=1 if preamble else 0), True
        return memory.recall(user_id, input), True
    except Exception:  # noqa: BLE001 -- memory must never break a run
        _logger.exception("Memory recall failed; run proceeds without recalled memory")
        return RecallResult(preamble="", count=0), False


@dataclass
class WorkflowResult:
    """Normalized output of a workflow run, independent of the underlying engine.

    Whatever engine produced it (LangGraph today, possibly CrewAI tomorrow),
    customer code only ever sees this shape.
    """

    output: str
    steps: List[dict] = field(default_factory=list)
    raw: Any = None
    # Per-user memory recording outcome for this run (SP-3), when memory was
    # active; None otherwise. Gives `run()` callers the same instrumentation
    # `stream()` exposes via `memory_recorded` events (review r4 #3).
    memory: "Optional[MemoryOutcome]" = None


class Workflow:
    """Top-level entry point: chains teams into a runnable business pipeline."""

    def __init__(
        self,
        name: str,
        steps: Sequence[Team],
        adapter: Optional["EngineAdapter"] = None,
    ) -> None:
        if not name:
            raise ConfigurationError("Workflow.name is required")
        if not steps:
            raise ConfigurationError(f"Workflow '{name}' needs at least one step")

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
    ) -> WorkflowResult:
        """Execute the workflow end-to-end and return a normalized result.

        When both `user_id` and `memory` are given, per-user memory is recalled
        into every agent's system prompt before the run and the run is recorded
        afterward (see `core/memory.py`). Both default to None → no memory,
        current behavior unchanged.
        """
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)

        recall_result, _ok = _safe_recall(memory, user_id, input)
        result = self._adapter.execute(self._compiled, input, memory_preamble=recall_result.preamble)
        if memory:
            # Best-effort, exactly like stream(): the workflow has already
            # completed, so a memory-recording failure must not raise here and
            # make a completed, side-effecting run look failed to the caller. The
            # outcome is surfaced on the result for instrumentation parity (#3).
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
    ) -> Iterator[TraceEvent]:
        """Run the workflow while yielding live TraceEvents — what the
        monitoring UI subscribes to.

        Wraps the adapter's raw event stream with run-level bookends
        (`run_started`/`run_completed`/`run_failed`) and stamps every event
        with this workflow's name. A failure surfaces as a terminal
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

        recall_result, recall_ok = _safe_recall(memory, user_id, input)

        yield TraceEvent(type="run_started", workflow=self.name, data=input)

        # Observability (M-05): for an active-memory run, always surface the recall
        # attempt — a count (0 when nothing matched, distinguishing it from memory
        # being disabled) or a sanitized failure marker.
        if memory:
            if recall_ok:
                yield TraceEvent(type="memory_recalled", workflow=self.name, data=recall_result.count)
            else:
                yield TraceEvent(type="memory_failed", workflow=self.name, data="recall")

        last_output = ""
        try:
            for event in self._adapter.stream(
                self._compiled, input, memory_preamble=recall_result.preamble
            ):
                event = dataclasses.replace(event, workflow=self.name)
                if event.type == "agent_completed":
                    last_output = event.data
                yield event
        except BestTeamError as exc:
            yield TraceEvent(type="run_failed", workflow=self.name, data=str(exc))
            return

        # Record BEFORE the terminal event so the memory events (and the backend's
        # usage persistence) land before a consumer stops on `run_completed`, and
        # before the run becomes eligible for registry eviction (review r4 #2).
        # Still best-effort: a recording failure surfaces as a sanitized
        # `memory_failed` event, never as `run_failed`.
        if memory:
            from .memory import MemoryOutcome

            try:
                outcome = memory.record_run(user_id, input, last_output)
            except Exception:  # noqa: BLE001 -- memory must never break a run
                _logger.exception("Memory recording failed; run stays completed")
                yield TraceEvent(type="memory_failed", workflow=self.name, data="record")
            else:
                # A legacy manager may return None (recorded successfully, no
                # structured outcome) -- that is NOT a failure (review r5 #3).
                if isinstance(outcome, MemoryOutcome):
                    if outcome.recorded:
                        # Observability (M-05) + metering (M-04): report what was
                        # written and carry the extraction call's token usage so
                        # the backend can persist a usage_records row for it.
                        yield TraceEvent(
                            type="memory_recorded",
                            workflow=self.name,
                            data=", ".join(outcome.recorded),
                            usage=[outcome.extraction_usage] if outcome.extraction_usage else [],
                        )
                    if not outcome.ok:
                        # A partial/total write failure is observable alongside
                        # any writes that did succeed (review r5 #2).
                        yield TraceEvent(type="memory_failed", workflow=self.name, data="record")

        yield TraceEvent(type="run_completed", workflow=self.name, data=last_output)

    def visualize(self) -> str:
        """Render the compiled graph as Mermaid markup (for the future CLI/UI)."""
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)
        return self._adapter.to_mermaid(self._compiled)
