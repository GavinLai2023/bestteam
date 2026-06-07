from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from ..core.trace import TraceEvent
    from ..core.workflow import Workflow, WorkflowResult


class EngineAdapter(ABC):
    """Translates business-facing config (Agent/Team/Workflow) into a runnable
    engine-specific graph, and normalizes results back into a WorkflowResult.

    This is the seam that lets bestteam swap LangGraph for CrewAI — or any
    other orchestration engine — without customer code ever noticing.
    """

    @abstractmethod
    def compile(self, workflow: "Workflow") -> Any:
        """Build and return an executable representation of the workflow."""

    @abstractmethod
    def execute(self, compiled: Any, input: str) -> "WorkflowResult":
        """Run the compiled workflow against `input` and normalize the result."""

    @abstractmethod
    def to_mermaid(self, compiled: Any) -> str:
        """Render the compiled graph as Mermaid markup, for the CLI/UI to display."""

    @abstractmethod
    def stream(self, compiled: Any, input: str) -> Iterator["TraceEvent"]:
        """Yield TraceEvents live as the workflow executes, for monitoring/observability."""
