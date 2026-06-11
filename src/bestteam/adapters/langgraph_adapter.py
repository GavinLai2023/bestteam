from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, Dict, Iterator, Sequence, Tuple, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from ..core.agent import Agent
from ..core.team import CollaborationMode, Team
from ..core.trace import TraceEvent
from ..core.workflow import Workflow, WorkflowResult
from ..exceptions import BestTeamError, ConfigurationError, EngineError
from .base import EngineAdapter


# Upper bound on how many times an agent node will execute tool calls and
# re-invoke the model in a single turn. Guards against a model that keeps
# requesting tools and never settles on a final answer.
_MAX_TOOL_ITERATIONS = 5


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


def _run_agent(agent: Agent, input_text: str, *, extra_tools: Sequence[Callable[..., Any]] = ()) -> str:
    """Run one agent's full tool-calling turn on `input_text`, returning its final text.

    Shared by `_agent_node` (SEQUENTIAL/PARALLEL team members) and
    `_make_delegate_tool` (a HIERARCHICAL manager's subordinates), so both
    paths get identical tool-calling behavior, including the iteration guard.
    `extra_tools` lets a manager additionally bind its subordinates'
    `delegate_to_<name>` tools alongside the agent's own `tools`.
    """
    model = _resolve_model(agent.model)
    all_tools = [*agent.tools, *extra_tools]
    tools_by_name = {fn.__name__: fn for fn in all_tools}
    if all_tools:
        try:
            model = model.bind_tools(all_tools)
        except NotImplementedError:
            pass  # model doesn't support tool calling (e.g. FakeListChatModel in tests)

    messages = [
        SystemMessage(content=agent.system_prompt()),
        HumanMessage(content=input_text),
    ]
    response = model.invoke(messages)

    for _ in range(_MAX_TOOL_ITERATIONS):
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            break
        messages.append(response)
        for call in tool_calls:
            tool_fn = tools_by_name.get(call["name"])
            if tool_fn is None:
                result = f"Error: unknown tool '{call['name']}'"
            else:
                try:
                    result = tool_fn(**call["args"])
                except Exception as exc:
                    result = f"Error calling tool '{call['name']}': {exc}"
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        response = model.invoke(messages)

    return response.content if hasattr(response, "content") else str(response)


def _make_delegate_tool(agent: Agent) -> Callable[[str], str]:
    """Wrap a subordinate agent as a `delegate_to_<name>(task)` tool for a manager.

    Calling the tool runs the subordinate's full tool-calling turn on `task`
    (via `_run_agent`) and returns its final text, so a manager's tool-calling
    loop can treat "delegate to a teammate" exactly like any other tool call.
    """

    def delegate(task: str) -> str:
        return _run_agent(agent, task)

    delegate.__name__ = f"delegate_to_{agent.name}"
    delegate.__doc__ = (
        f"Delegate a task to {agent.name}, a {agent.role} whose goal is: "
        f"{agent.goal}. Returns their response as text."
    )
    return delegate


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
        text = _run_agent(agent, state["context"] or state["input"])

        update: Dict[str, Any] = {"contributions": {agent.name: text}}
        if propagate_context:
            update["context"] = text
            update["output"] = text
        return update

    return node


def _hierarchical_node(team: Team):
    """Build a LangGraph node function for a HIERARCHICAL team's manager.

    The manager is run with one extra `delegate_to_<name>` tool per
    subordinate agent (see `_make_delegate_tool`), bound alongside its own
    `tools`. The existing tool-calling loop in `_run_agent` then lets the
    manager delegate to teammates and incorporate their answers, exactly like
    it would call a knowledge-base or web-search tool.
    """
    manager = team.manager
    if manager is None:
        raise ConfigurationError(
            f"Team '{team.name}' uses hierarchical mode and requires a 'manager' agent"
        )
    for member in (manager, *team.agents):
        if member.model is None:
            raise ConfigurationError(f"Agent '{member.name}' has no model configured")

    delegate_tools = [_make_delegate_tool(agent) for agent in team.agents]

    def node(state: _TeamState) -> Dict[str, Any]:
        text = _run_agent(manager, state["context"] or state["input"], extra_tools=delegate_tools)
        return {"contributions": {manager.name: text}, "context": text, "output": text}

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

    Supports SEQUENTIAL, PARALLEL, and HIERARCHICAL collaboration modes.
    DEBATE is on the roadmap — it raises NotImplementedError with a clear
    message rather than silently behaving like SEQUENTIAL.
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
        if team.mode == CollaborationMode.HIERARCHICAL:
            return self._wire_hierarchical(graph, team)
        raise NotImplementedError(
            f"Collaboration mode '{team.mode.value}' is not implemented yet "
            f"(team '{team.name}'). SEQUENTIAL, PARALLEL, and HIERARCHICAL are "
            "available; DEBATE is on the roadmap."
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

    def _wire_hierarchical(self, graph: StateGraph, team: Team) -> Tuple[str, str]:
        manager = team.manager
        if manager is None:
            raise ConfigurationError(
                f"Team '{team.name}' uses hierarchical mode and requires a 'manager' agent"
            )
        node_name = f"{team.name}.{manager.name}"
        graph.add_node(node_name, _hierarchical_node(team))
        return node_name, node_name

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
