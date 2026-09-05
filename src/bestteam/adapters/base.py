from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

if TYPE_CHECKING:
    from ..core.trace import TraceEvent
    from ..core.pipeline import Pipeline, PipelineResult


class EngineAdapter(ABC):
    """Translates business-facing config (Agent/Team/Pipeline) into a runnable
    engine-specific graph, and normalizes results back into a PipelineResult.

    This is the seam that lets bestteam swap LangGraph for CrewAI — or any
    other orchestration engine — without customer code ever noticing.
    """

    @abstractmethod
    def compile(self, pipeline: "Pipeline") -> Any:
        """Build and return an executable representation of the pipeline."""

    @abstractmethod
    def execute(
        self, compiled: Any, input: str, memory_preamble: str = "", diagnostic: bool = False
    ) -> "PipelineResult":
        """Run the compiled pipeline against `input` and normalize the result.

        `memory_preamble`, if non-empty, is recalled per-user memory injected
        into each agent's system prompt for this run (see `core/memory.py`).
        `diagnostic` asks the engine to also surface what a normal trace leaves
        out (prompts, model turns, tool args/results) -- see `core/trace.py`.
        """

    @abstractmethod
    def to_mermaid(self, compiled: Any) -> str:
        """Render the compiled graph as Mermaid markup, for the CLI/UI to display."""

    @abstractmethod
    def stream(
        self,
        compiled: Any,
        input: str,
        memory_preamble: str = "",
        diagnostic: bool = False,
        *,
        on_token: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_live_event: Optional[Callable[["TraceEvent"], None]] = None,
    ) -> Iterator["TraceEvent"]:
        """Yield TraceEvents live as the pipeline executes, for monitoring/observability.

        `memory_preamble` and `diagnostic` behave as in `execute`.

        `on_token`, if given, receives the text deltas of the one agent whose
        output is the run's answer, as they are produced -- a side channel for
        engines that can stream, since this iterator itself may only yield at
        coarse boundaries. Deltas are not TraceEvents and must never be
        persisted. `should_cancel`, if given, is polled between deltas so a
        long reply can be stopped mid-generation.

        `on_live_event`, if given, receives an `agent_working` TraceEvent the
        moment an agent (or a delegated subordinate) starts or a subordinate
        finishes -- the same side-channel argument as `on_token`: this
        iterator only yields at coarse boundaries. Never yielded, never
        persisted (see core/trace.py).

        An adapter whose engine cannot stream may ignore all three: the caller
        falls back to the progress events this iterator already yields.
        """
