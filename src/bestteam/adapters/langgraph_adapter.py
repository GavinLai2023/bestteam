from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, Iterator, Tuple, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from ..core.agent import Agent
from ..core.team import CollaborationMode, Team
from ..core.trace import TraceEvent
from ..core.workflow import Workflow, WorkflowResult
from ..exceptions import BestTeamError, ConfigurationError, EngineError
from .base import EngineAdapter


class _TeamState(TypedDict):
    input: str
    context: str
    # Annotated with a dict-union reducer: parallel agents in the same
    # superstep each write their own {name: text} entry, and LangGraph merges
    # them instead of raising a concurrent-update error. Sequential agents run
    # in separate supersteps, where the same reducer just accumulates in order.
    contributions: Annotated[Dict[str, str], operator.or_]
    output: str


def _resolve_model(model: Any) -> BaseChatModel:
    """Accept either a ready-made chat model or a provider model name string.

    Customers who already have a `BaseChatModel` (fake, fine-tuned, custom
    routing, ...) can pass it directly. String names are resolved lazily via
    `langchain.chat_models.init_chat_model`, kept as an optional dependency so
    the core SDK doesn't force a provider choice on anyone.
    """
    if isinstance(model, BaseChatModel):
        return model
    if isinstance(model, str):
        # "fake:<canned response>" lets customers dry-run a pipeline's wiring
        # from plain YAML — no provider package or API key required, $0 cost.
        if model.startswith("fake:"):
            from langchain_core.language_models.fake_chat_models import FakeListChatModel

            return FakeListChatModel(responses=[model[len("fake:") :]])
        try:
            from langchain.chat_models import init_chat_model
        except ImportError as exc:
            raise ConfigurationError(
                "Resolving a model from a string name requires the optional "
                "'langchain' package (pip install langchain). Alternatively, "
                "pass a BaseChatModel instance directly as Agent(model=...)."
            ) from exc
        return init_chat_model(model)
    raise ConfigurationError(
        f"Unsupported model spec {model!r} on agent: pass a provider model "
        "name (str) or a langchain BaseChatModel instance."
    )


def _agent_node(agent: Agent, *, propagate_context: bool):
    """Build a LangGraph node function that runs a single agent.

    `propagate_context` controls whether this agent's output becomes the
    shared context/output for whatever runs next. Sequential agents propagate
    (each hands off to the next); parallel agents don't (they all see the same
    incoming context and only contribute to the final aggregation).
    """
    if agent.model is None:
        raise ConfigurationError(f"Agent '{agent.name}' has no model configured")

    def node(state: _TeamState) -> Dict[str, Any]:
        model = _resolve_model(agent.model)
        if agent.tools:
            try:
                model = model.bind_tools(list(agent.tools))
            except NotImplementedError:
                pass  # model doesn't support tool calling (e.g. FakeListChatModel in tests)

        messages = [
            SystemMessage(content=agent.system_prompt()),
            HumanMessage(content=state["context"] or state["input"]),
        ]
        response = model.invoke(messages)
        text = response.content if hasattr(response, "content") else str(response)

        update: Dict[str, Any] = {"contributions": {agent.name: text}}
        if propagate_context:
            update["context"] = text
            update["output"] = text
        return update

    return node


def _initial_state(input: str) -> _TeamState:
    return {"input": input, "context": "", "contributions": {}, "output": ""}


def _passthrough_node(_state: _TeamState) -> Dict[str, Any]:
    """No-op fan-out node: gives parallel branches a single, named entry point."""
    return {}


def _aggregate_node(state: _TeamState) -> Dict[str, Any]:
    """Combine a parallel team's per-agent contributions into one team output."""
    contributions = state.get("contributions", {})
    merged = "\n\n".join(f"[{name}]\n{text}" for name, text in contributions.items())
    return {"output": merged, "context": merged}


class LangGraphAdapter(EngineAdapter):
    """Default engine adapter, built on top of LangGraph's StateGraph.

    Supports SEQUENTIAL and PARALLEL collaboration modes today. HIERARCHICAL
    and DEBATE are on the roadmap — they raise NotImplementedError with a
    clear message rather than silently behaving like SEQUENTIAL.
    """

    def compile(self, workflow: Workflow) -> Any:
        graph = StateGraph(_TeamState)
        previous_exit = START

        for team in workflow.steps:
            entry, exit_ = self._wire_team(graph, team)
            graph.add_edge(previous_exit, entry)
            previous_exit = exit_

        graph.add_edge(previous_exit, END)
        return graph.compile()

    def _wire_team(self, graph: StateGraph, team: Team) -> Tuple[str, str]:
        if team.mode == CollaborationMode.SEQUENTIAL:
            return self._wire_sequential(graph, team)
        if team.mode == CollaborationMode.PARALLEL:
            return self._wire_parallel(graph, team)
        raise NotImplementedError(
            f"Collaboration mode '{team.mode.value}' is not implemented yet "
            f"(team '{team.name}'). SEQUENTIAL and PARALLEL are available; "
            "HIERARCHICAL and DEBATE are on the roadmap."
        )

    def _wire_sequential(self, graph: StateGraph, team: Team) -> Tuple[str, str]:
        node_names = []
        for agent in team.agents:
            node_name = f"{team.name}.{agent.name}"
            graph.add_node(node_name, _agent_node(agent, propagate_context=True))
            node_names.append(node_name)

        for current, nxt in zip(node_names, node_names[1:]):
            graph.add_edge(current, nxt)

        return node_names[0], node_names[-1]

    def _wire_parallel(self, graph: StateGraph, team: Team) -> Tuple[str, str]:
        entry_name = f"{team.name}.__fan_out__"
        exit_name = f"{team.name}.__aggregate__"

        graph.add_node(entry_name, _passthrough_node)
        graph.add_node(exit_name, _aggregate_node)

        for agent in team.agents:
            node_name = f"{team.name}.{agent.name}"
            graph.add_node(node_name, _agent_node(agent, propagate_context=False))
            graph.add_edge(entry_name, node_name)
            graph.add_edge(node_name, exit_name)

        return entry_name, exit_name

    def execute(self, compiled: Any, input: str) -> WorkflowResult:
        try:
            final_state = compiled.invoke(_initial_state(input))
        except BestTeamError:
            # Already a framework-level error (e.g. ConfigurationError raised
            # lazily during model resolution) — surface it as-is rather than
            # masking it behind a generic "engine execution failed".
            raise
        except Exception as exc:
            raise EngineError(f"Workflow execution failed: {exc}") from exc

        steps = [
            {"agent": name, "output": text}
            for name, text in final_state.get("contributions", {}).items()
        ]
        return WorkflowResult(
            output=final_state.get("output", ""),
            steps=steps,
            raw=final_state,
        )

    def to_mermaid(self, compiled: Any) -> str:
        return compiled.get_graph().draw_mermaid()

    def stream(self, compiled: Any, input: str) -> Iterator[TraceEvent]:
        """Yield an `agent_completed` TraceEvent each time a node finishes.

        Built on LangGraph's `stream_mode="updates"`, which yields exactly the
        partial state each node returned — so `contributions` always holds
        just that node's own entry, never a merged view of the whole run.
        """
        try:
            for update in compiled.stream(_initial_state(input), stream_mode="updates"):
                for partial in update.values():
                    if not isinstance(partial, dict):
                        continue
                    for agent_name, text in partial.get("contributions", {}).items():
                        yield TraceEvent(type="agent_completed", workflow="", agent=agent_name, data=text)
        except BestTeamError:
            raise
        except Exception as exc:
            raise EngineError(f"Workflow execution failed: {exc}") from exc
