from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Iterator

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
        self, compiled: Any, input: str, memory_preamble: str = "", diagnostic: bool = False
    ) -> Iterator["TraceEvent"]:
        """Yield TraceEvents live as the pipeline executes, for monitoring/observability.

        `memory_preamble` and `diagnostic` behave as in `execute`.
        """
