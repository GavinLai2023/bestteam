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
    from .memory import MemoryManager, RecallResult


def _safe_recall(memory: "Optional[MemoryManager]", user_id: Optional[str], input: str) -> "RecallResult":
    """Recall memory best-effort, returning the preamble plus recalled count.
    Recall is a read on an optional, opt-in subsystem; like record_run (the
    write), it must never break a run, so any failure degrades to an empty
    result (the run proceeds without recalled memory) rather than propagating
    and surfacing as run_failed."""
    from .memory import RecallResult

    if not memory:
        return RecallResult(preamble="", count=0)
    try:
        return memory.recall(user_id, input)
    except Exception:  # noqa: BLE001 -- memory must never break a run
        _logger.exception("Memory recall failed; run proceeds without recalled memory")
        return RecallResult(preamble="", count=0)


@dataclass
class WorkflowResult:
    """Normalized output of a workflow run, independent of the underlying engine.

    Whatever engine produced it (LangGraph today, possibly CrewAI tomorrow),
    customer code only ever sees this shape.
    """

    output: str
    steps: List[dict] = field(default_factory=list)
    raw: Any = None


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

        preamble = _safe_recall(memory, user_id, input).preamble
        result = self._adapter.execute(self._compiled, input, memory_preamble=preamble)
        if memory:
            # Best-effort, exactly like stream(): the workflow has already
            # completed, so a memory-recording failure must not raise here and
            # make a completed, side-effecting run look failed to the caller.
            try:
                memory.record_run(user_id, input, result.output)
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
        into the run before the first event and the run is recorded after
        `run_completed` (not on the `run_failed` path). Both default to None →
        no memory, current behavior unchanged.
        """
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)

        recall = _safe_recall(memory, user_id, input)
        preamble = recall.preamble

        yield TraceEvent(type="run_started", workflow=self.name, data=input)

        # Observability (M-05): surface that recall drew N records into this run.
        if memory and recall.count:
            yield TraceEvent(type="memory_recalled", workflow=self.name, data=recall.count)

        last_output = ""
        try:
            for event in self._adapter.stream(self._compiled, input, memory_preamble=preamble):
                event = dataclasses.replace(event, workflow=self.name)
                if event.type == "agent_completed":
                    last_output = event.data
                yield event
        except BestTeamError as exc:
            yield TraceEvent(type="run_failed", workflow=self.name, data=str(exc))
            return

        yield TraceEvent(type="run_completed", workflow=self.name, data=last_output)

        if memory:
            # Best-effort: the run has already completed and been reported, so a
            # memory-recording failure must not turn a completed run into a
            # failed one (it would otherwise raise here, after run_completed).
            try:
                outcome = memory.record_run(user_id, input, last_output)
                if outcome.recorded:
                    # Observability (M-05) + metering (M-04): report what was
                    # written and carry the extraction call's token usage so the
                    # backend can persist a usage_records row for it.
                    yield TraceEvent(
                        type="memory_recorded",
                        workflow=self.name,
                        data=", ".join(outcome.recorded),
                        usage=[outcome.extraction_usage] if outcome.extraction_usage else [],
                    )
            except Exception:  # noqa: BLE001 -- memory must never break a run
                _logger.exception("Memory recording failed after run_completed; run stays completed")

    def visualize(self) -> str:
        """Render the compiled graph as Mermaid markup (for the future CLI/UI)."""
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)
        return self._adapter.to_mermaid(self._compiled)
