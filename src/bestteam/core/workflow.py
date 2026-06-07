from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator, List, Optional, Sequence

from ..exceptions import BestTeamError, ConfigurationError
from .team import Team
from .trace import TraceEvent

if TYPE_CHECKING:
    from ..adapters.base import EngineAdapter


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

    def run(self, input: str) -> WorkflowResult:
        """Execute the workflow end-to-end and return a normalized result."""
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)
        return self._adapter.execute(self._compiled, input)

    def stream(self, input: str) -> Iterator[TraceEvent]:
        """Run the workflow while yielding live TraceEvents — what the
        monitoring UI subscribes to.

        Wraps the adapter's raw event stream with run-level bookends
        (`run_started`/`run_completed`/`run_failed`) and stamps every event
        with this workflow's name. A failure surfaces as a terminal
        `run_failed` event rather than an exception breaking the generator
        mid-iteration — simpler for consumers like a WebSocket handler to relay.
        """
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)

        yield TraceEvent(type="run_started", workflow=self.name, data=input)

        last_output = ""
        try:
            for event in self._adapter.stream(self._compiled, input):
                event = dataclasses.replace(event, workflow=self.name)
                if event.type == "agent_completed":
                    last_output = event.data
                yield event
        except BestTeamError as exc:
            yield TraceEvent(type="run_failed", workflow=self.name, data=str(exc))
            return

        yield TraceEvent(type="run_completed", workflow=self.name, data=last_output)

    def visualize(self) -> str:
        """Render the compiled graph as Mermaid markup (for the future CLI/UI)."""
        if self._compiled is None:
            self._compiled = self._adapter.compile(self)
        return self._adapter.to_mermaid(self._compiled)
