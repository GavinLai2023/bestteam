from __future__ import annotations

import logging
import operator
import time
from typing import Annotated, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from ..core.agent import Agent
from ..core.team import CollaborationMode, Team
from ..core.trace import TraceEvent
from ..core.workflow import Workflow, WorkflowResult
from ..exceptions import BestTeamError, ConfigurationError, EngineError
from .base import EngineAdapter

_logger = logging.getLogger(__name__)

# Upper bound on how many times an agent node will execute tool calls and
# re-invoke the model in a single turn. Guards against a model that keeps
# requesting tools and never settles on a final answer.
_MAX_TOOL_ITERATIONS = 5


def _tool_loop_exhausted_notice(agent_name: str) -> str:
    """Output returned when an agent uses up `_MAX_TOOL_ITERATIONS` without ever
    settling on a text answer. The final tool-calling response's `content` is an
    empty string, so returning it would make an exhausted run look like a silent
    empty success (CR-011). This explicit notice surfaces the truncation in the
    agent's output and the resulting `agent_completed` trace event instead."""
    return (
        f"[Agent '{agent_name}' stopped after {_MAX_TOOL_ITERATIONS} tool "
        "iterations without producing a final answer.]"
    )


class _TeamState(TypedDict):
    input: str
    context: str
    # Annotated with a dict-union reducer: parallel agents in the same
    # superstep each write their own {name: text} entry, and LangGraph merges
    # them instead of raising a concurrent-update error. Sequential agents run
    # in separate supersteps, where the same reducer just accumulates in order.
    contributions: Annotated[Dict[str, str], operator.or_]
    # Same shape/reducer as `contributions`, but each entry is the list of
    # per-model-call usage_metadata dicts recorded while that agent ran (see
    # `_record_usage`). Empty for `fake:` models, which don't report usage.
    usage: Annotated[Dict[str, List[Dict[str, Any]]], operator.or_]
    # Same shape/reducer as `contributions`/`usage`: each entry is the list of
    # granular TraceEvents (agent_started/tool_started/.../delegation_*)
    # recorded while that agent (or manager) ran, in order. Flushed by
    # `LangGraphAdapter.stream()` just before the node's `agent_completed`.
    trace_events: Annotated[Dict[str, List[TraceEvent]], operator.or_]
    output: str
    # Recalled per-user memory for this run, injected into each agent's system
    # prompt (see core/memory.py). Plain (no reducer): nodes only read it, and
    # it's set once by `_initial_state` and never written by a node.
    memory_preamble: str


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


def _model_spec(agent: Agent) -> str:
    """A best-effort spec string identifying `agent.model`, for usage attribution.

    String specs (e.g. "openai:gpt-4o-mini" or "fake:hi") are used as-is, since
    those are exactly the keys a `model_catalog` entry is looked up by. A
    pre-built `BaseChatModel` instance has no such spec, so fall back to
    whatever model name it reports (or its class name).
    """
    if isinstance(agent.model, str):
        return agent.model
    return getattr(agent.model, "model_name", None) or getattr(agent.model, "model", None) or type(agent.model).__name__


def _summarize(value: Any, limit: int = 200) -> str:
    """Business-safe, length-bounded stringification for trace event `data`.

    Used for tool results, delegated tasks, and subordinate outputs -- things
    that can be arbitrarily long or (for a real tool) carry more detail than a
    monitoring UI should render. Never used for chain-of-thought or raw
    exception text, which never enter trace events at all.
    """
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


# Email tools carry customer/tenant content (subject lines, body snippets,
# drafted reply text) that a generic 200-char `_summarize()` would still leak
# into the trace_events table and any log/UI that renders it. Special-cased
# below so the *content* of a mailbox never reaches a trace event -- only
# success/failure and counts/ids, derived from the call's own arguments
# rather than parsed out of the tool's return text (robust to the return
# text's exact wording changing). See docs/superpowers/specs/
# 2026-08-02-property-maintenance-inbox-phase-1-development-plan.md section 15.2.
_EMAIL_TOOLS_NEEDING_REDACTION = frozenset({"email_find", "email_read", "email_draft_reply"})

# `message_id` is a model-controlled tool-call argument (not our own deterministic
# text), so it needs the same length bound `_summarize()` gives everything else --
# otherwise a model could smuggle an arbitrarily long/injected string into the
# trace via this one field.
_MESSAGE_ID_TRACE_CHARS = 64

# Mirrors tools/email_client.py's `_OUT_OF_BATCH` sentinel (a UID-scoped run's
# email_read/email_draft_reply refuse any id outside the poller-detected batch).
# Not imported directly to avoid the adapters layer depending on a specific
# tools implementation -- see src/bestteam/tools/CLAUDE.md.
_OUT_OF_BATCH_TEXT = "That message isn't part of this batch of new mail."


def _bounded_message_id(call_args: Dict[str, Any]) -> str:
    raw = str(call_args.get("message_id", ""))
    return raw if len(raw) <= _MESSAGE_ID_TRACE_CHARS else f"{raw[:_MESSAGE_ID_TRACE_CHARS]}…"


def _redacted_email_tool_data(tool_name: str, call_args: Dict[str, Any], result: Any) -> Dict[str, Any]:
    """Business-safe `tool_completed` data for an email tool: never the
    subject/body/draft text, only outcome + a length-bounded message id.

    `outcome` lets a caller (e.g. `automation_results.py`) distinguish a real
    confirmed action (`"draft_created"`) from a call the tool itself refused
    or that found nothing -- the result text alone is not enough to tell,
    since a scoped tool's rejection text doesn't start with the same prefix
    as its "not found" text.
    """
    text = str(result)
    if tool_name == "email_find":
        if text.startswith("Found "):
            count = text.split(" ", 2)[1]
            return {"summary": f"Found {count} message(s)."}
        return {"summary": "No messages found."}

    message_id = _bounded_message_id(call_args)
    if text == _OUT_OF_BATCH_TEXT:
        return {
            "summary": f"Rejected: message '{message_id}' is outside this run's batch.",
            "message_id": message_id,
            "outcome": "out_of_batch",
        }
    if text.startswith("No message found"):
        return {
            "summary": f"No message found for id '{message_id}'.",
            "message_id": message_id,
            "outcome": "not_found",
        }
    if tool_name == "email_read":
        return {"summary": f"Read message '{message_id}'.", "message_id": message_id, "outcome": "read"}
    # email_draft_reply: both backends' success text starts with "Draft reply"
    # (see tools/email_client.py's Graph/IMAP `draft_reply` implementations).
    if text.startswith("Draft reply"):
        return {
            "summary": f"Draft reply saved for message '{message_id}'.",
            "message_id": message_id,
            "outcome": "draft_created",
        }
    return {
        "summary": f"Draft reply attempt for message '{message_id}' did not complete.",
        "message_id": message_id,
        "outcome": "unknown",
    }


def _record_usage(agent: Agent, response: Any, usage_sink: Optional[List[Dict[str, Any]]]) -> None:
    """Append `response.usage_metadata` (if any) to `usage_sink`, tagged with `agent`'s model spec."""
    if usage_sink is None:
        return
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return
    usage_sink.append(
        {
            "model": _model_spec(agent),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
    )


def _run_agent(
    agent: Agent,
    input_text: str,
    *,
    extra_tools: Sequence[Callable[..., Any]] = (),
    extra_system_prompt: str = "",
    require_tool_use_on_first_call: bool = False,
    usage_sink: Optional[List[Dict[str, Any]]] = None,
    on_event: Optional[Callable[[TraceEvent], None]] = None,
) -> str:
    """Run one agent's full tool-calling turn on `input_text`, returning its final text.

    Shared by `_agent_node` (SEQUENTIAL/PARALLEL team members) and
    `_make_delegate_tool` (a HIERARCHICAL manager's subordinates), so both
    paths get identical tool-calling behavior, including the iteration guard.
    `extra_tools` lets a manager additionally bind its subordinates'
    `delegate_to_<name>` tools alongside the agent's own `tools`.
    `extra_system_prompt`, if non-empty, is appended (separated by a blank
    line) after `agent.system_prompt()` -- used by `_hierarchical_node` to
    give a manager delegation guidance without changing `Agent.system_prompt()`
    itself, which every agent type uses. `require_tool_use_on_first_call`, if
    True and tools are bound, forces the very first model call to use
    `tool_choice="required"` -- a real model can otherwise just ignore prompt
    text and answer directly, so `_hierarchical_node` uses this to make a
    manager's first turn always delegate rather than merely suggesting it
    should. Later iterations in this same call always use the unforced
    binding, so the agent can still settle on a final text answer once it has
    gathered what it needs. If `usage_sink` is given, each model invocation's
    `usage_metadata` (when reported) is appended to it for usage metering.
    If `on_event` is given, it's called with each granular `TraceEvent`
    (agent_started/tool_started/tool_completed/agent_progress) as this turn
    progresses -- `LangGraphAdapter.stream()` buffers these per-node and
    flushes them just before that node's `agent_completed`.
    """

    def _emit(event_type: str, data: Any = None) -> None:
        if on_event is not None:
            on_event(TraceEvent(type=event_type, workflow="", agent=agent.name, data=data))

    model = _resolve_model(agent.model)
    all_tools = [*agent.tools, *extra_tools]
    tools_by_name = {fn.__name__: fn for fn in all_tools}
    first_call_model = model
    if all_tools:
        try:
            model = model.bind_tools(all_tools)
            first_call_model = (
                model.bind_tools(all_tools, tool_choice="required") if require_tool_use_on_first_call else model
            )
        except NotImplementedError:
            pass  # model doesn't support tool calling (e.g. FakeListChatModel in tests)

    system_prompt = agent.system_prompt()
    if extra_system_prompt:
        system_prompt = f"{system_prompt}\n\n{extra_system_prompt}"

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=input_text),
    ]
    _emit("agent_started", {"role": agent.role, "goal": agent.goal})
    response = first_call_model.invoke(messages)
    _record_usage(agent, response, usage_sink)

    for i in range(_MAX_TOOL_ITERATIONS):
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            break
        messages.append(response)
        for call in tool_calls:
            tool_fn = tools_by_name.get(call["name"])
            if tool_fn is None:
                result = f"Error: unknown tool '{call['name']}'"
            else:
                _emit("tool_started", {"tool": call["name"]})
                start = time.monotonic()
                try:
                    result = tool_fn(**call["args"])
                except Exception as exc:
                    _logger.warning(
                        "Tool call to '%s' failed for agent '%s': %s", call["name"], agent.name, exc, exc_info=True
                    )
                    result = f"Error calling tool '{call['name']}': {exc}"
                    failure_data: Dict[str, Any] = {
                        "tool": call["name"],
                        "success": False,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                        "summary": "Tool call failed",
                    }
                    if call["name"] in ("email_read", "email_draft_reply"):
                        # Retain the bounded message id on failure too, same as a
                        # successful call's redacted data -- otherwise a failed
                        # email_read/email_draft_reply can't be correlated back to
                        # its UID downstream (automation_results.py's per-UID
                        # needs_attention enforcement, Codex review finding).
                        failure_data["message_id"] = _bounded_message_id(call["args"])
                    _emit("tool_completed", failure_data)
                else:
                    extra_data = (
                        _redacted_email_tool_data(call["name"], call["args"], result)
                        if call["name"] in _EMAIL_TOOLS_NEEDING_REDACTION
                        else {"summary": _summarize(result)}
                    )
                    _emit(
                        "tool_completed",
                        {
                            "tool": call["name"],
                            "success": True,
                            "duration_ms": int((time.monotonic() - start) * 1000),
                            **extra_data,
                        },
                    )
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        _emit("agent_progress", {"note": f"iteration {i + 1} of {_MAX_TOOL_ITERATIONS}"})
        response = model.invoke(messages)
        _record_usage(agent, response, usage_sink)

    if getattr(response, "tool_calls", None):
        # The loop ran out while the model was still asking for tools, so it
        # never produced a text answer -- `response.content` is empty here.
        return _tool_loop_exhausted_notice(agent.name)
    return response.content if hasattr(response, "content") else str(response)


def _make_delegate_tool(
    agent: Agent,
    *,
    usage_sink: Optional[List[Dict[str, Any]]] = None,
    extra_system_prompt: str = "",
    on_event: Optional[Callable[[TraceEvent], None]] = None,
    manager_name: str = "",
) -> Callable[[str], str]:
    """Wrap a subordinate agent as a `delegate_to_<name>(task)` tool for a manager.

    Calling the tool runs the subordinate's full tool-calling turn on `task`
    (via `_run_agent`) and returns its final text, so a manager's tool-calling
    loop can treat "delegate to a teammate" exactly like any other tool call.
    If the subordinate has its own `tools` (e.g. a knowledge base), its first
    call is also forced to use one of them (`require_tool_use_on_first_call`)
    -- otherwise a real model can just answer from its own guesswork instead
    of actually consulting the tool it was delegated to use. `usage_sink`, if
    given, collects this subordinate's usage alongside the manager's own, so
    a hierarchical turn's total usage is reported in one place.
    `extra_system_prompt` (e.g. recalled user memory) is forwarded to the
    subordinate's run so delegates get the same memory preamble as
    sequential/parallel agents.
    `on_event`, if given, receives delegation_started/subagent_started (before)
    and subagent_completed/delegation_completed (after) around the subordinate's
    own run, tagging the manager-side events with `manager_name` and the
    subordinate-side events with the subordinate's own name.
    """

    def delegate(task: str) -> str:
        _logger.info("Manager delegated to '%s': %s", agent.name, task[:200])
        if on_event is not None:
            on_event(
                TraceEvent(
                    type="delegation_started",
                    workflow="",
                    agent=manager_name,
                    data={"to": agent.name, "task_summary": _summarize(task)},
                )
            )
            on_event(
                TraceEvent(
                    type="subagent_started",
                    workflow="",
                    agent=agent.name,
                    data={"task_summary": _summarize(task)},
                )
            )
        result = _run_agent(
            agent,
            task,
            extra_system_prompt=extra_system_prompt,
            require_tool_use_on_first_call=bool(agent.tools),
            usage_sink=usage_sink,
            on_event=on_event,
        )
        if on_event is not None:
            on_event(
                TraceEvent(
                    type="subagent_completed",
                    workflow="",
                    agent=agent.name,
                    data={"success": True, "summary": _summarize(result)},
                )
            )
            on_event(
                TraceEvent(
                    type="delegation_completed",
                    workflow="",
                    agent=manager_name,
                    data={"to": agent.name, "summary": _summarize(result)},
                )
            )
        return result

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
        usage_sink: List[Dict[str, Any]] = []
        sub_events: List[TraceEvent] = []
        text = _run_agent(
            agent,
            state["context"] or state["input"],
            extra_system_prompt=state.get("memory_preamble", ""),
            usage_sink=usage_sink,
            on_event=sub_events.append,
        )

        update: Dict[str, Any] = {
            "contributions": {agent.name: text},
            "usage": {agent.name: usage_sink},
            "trace_events": {agent.name: sub_events},
        }
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
    it would call a knowledge-base or web-search tool. The manager also gets
    explicit delegation guidance appended to its system prompt (via
    `extra_system_prompt`) naming each subordinate and their
    `delegate_to_<name>` tool. Guidance text alone is only a suggestion a
    real model can ignore, so the manager's first call also forces
    `tool_choice="required"` (via `require_tool_use_on_first_call`) --
    without this, a real LLM can just answer directly and never call a
    delegate tool at all.
    """
    manager = team.manager
    if manager is None:
        raise ConfigurationError(
            f"Team '{team.name}' uses hierarchical mode and requires a 'manager' agent"
        )
    for member in (manager, *team.agents):
        if member.model is None:
            raise ConfigurationError(f"Agent '{member.name}' has no model configured")

    guidance_lines = [
        "You manage a team of specialists. For any part of the request that "
        "falls within a specialist's domain below, delegate that sub-task to "
        "them using their tool rather than answering it yourself. If the "
        "request touches more than one specialist's domain, delegate to "
        "EVERY relevant specialist (not just one) before composing your "
        "final answer -- a request can need input from several of them at "
        "once:",
    ]
    for agent in team.agents:
        guidance_lines.append(
            f"- {agent.name} ({agent.role}, goal: {agent.goal}): "
            f"call delegate_to_{agent.name}(task) to delegate to them."
        )
    delegation_guidance = "\n".join(guidance_lines)

    def node(state: _TeamState) -> Dict[str, Any]:
        usage_sink: List[Dict[str, Any]] = []
        sub_events: List[TraceEvent] = []
        preamble = state.get("memory_preamble", "")
        # Subordinates get the recalled user memory (like sequential/parallel
        # agents) but not the manager's delegation guidance, which is manager-only.
        delegate_tools = [
            _make_delegate_tool(
                agent,
                usage_sink=usage_sink,
                extra_system_prompt=preamble,
                on_event=sub_events.append,
                manager_name=manager.name,
            )
            for agent in team.agents
        ]
        extra_system_prompt = f"{preamble}\n\n{delegation_guidance}" if preamble else delegation_guidance
        text = _run_agent(
            manager,
            state["context"] or state["input"],
            extra_tools=delegate_tools,
            extra_system_prompt=extra_system_prompt,
            require_tool_use_on_first_call=True,
            usage_sink=usage_sink,
            on_event=sub_events.append,
        )
        return {
            "contributions": {manager.name: text},
            "usage": {manager.name: usage_sink},
            "trace_events": {manager.name: sub_events},
            "context": text,
            "output": text,
        }

    return node


def _initial_state(input: str, memory_preamble: str = "") -> _TeamState:
    return {
        "input": input,
        "context": "",
        "contributions": {},
        "usage": {},
        "trace_events": {},
        "output": "",
        "memory_preamble": memory_preamble,
    }


def _passthrough_node(_state: _TeamState) -> Dict[str, Any]:
    """No-op fan-out node: gives parallel branches a single, named entry point."""
    return {}


def _aggregate_node(team: Team) -> Callable[[_TeamState], Dict[str, Any]]:
    """Build a parallel team's aggregation node.

    `contributions` is a run-global dict, so by the time this runs it also holds
    earlier teams' entries. Aggregating only *this* team's agents (in declared
    order) keeps a later parallel team's output from being contaminated by
    unrelated prior steps (CR-004). The global contributions dict -- and thus
    the run's full step history in `execute()` -- is left as-is, per the
    deferred history redesign.
    """
    agent_names = [agent.name for agent in team.agents]

    def node(state: _TeamState) -> Dict[str, Any]:
        contributions = state.get("contributions", {})
        merged = "\n\n".join(
            f"[{name}]\n{contributions[name]}" for name in agent_names if name in contributions
        )
        return {"output": merged, "context": merged}

    return node


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
        graph.add_node(exit_name, _aggregate_node(team))

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

    def execute(self, compiled: Any, input: str, memory_preamble: str = "") -> WorkflowResult:
        try:
            final_state = compiled.invoke(_initial_state(input, memory_preamble))
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

    def stream(self, compiled: Any, input: str, memory_preamble: str = "") -> Iterator[TraceEvent]:
        """Yield an `agent_completed` TraceEvent each time a node finishes.

        Built on LangGraph's `stream_mode="updates"`, which yields exactly the
        partial state each node returned — so `contributions` always holds
        just that node's own entry, never a merged view of the whole run.
        """
        try:
            for update in compiled.stream(_initial_state(input, memory_preamble), stream_mode="updates"):
                for partial in update.values():
                    if not isinstance(partial, dict):
                        continue
                    usage_by_agent = partial.get("usage", {})
                    events_by_agent = partial.get("trace_events", {})
                    for agent_name, text in partial.get("contributions", {}).items():
                        yield from events_by_agent.get(agent_name, [])
                        yield TraceEvent(
                            type="agent_completed",
                            workflow="",
                            agent=agent_name,
                            data=text,
                            usage=usage_by_agent.get(agent_name, []),
                        )
        except BestTeamError:
            raise
        except Exception as exc:
            raise EngineError(f"Workflow execution failed: {exc}") from exc
