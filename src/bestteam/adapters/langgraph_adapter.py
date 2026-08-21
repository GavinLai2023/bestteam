from __future__ import annotations

import logging
import operator
import time
from typing import TYPE_CHECKING, Annotated, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from ..core.agent import Agent
from ..core.team import CollaborationMode, Team
from ..core.tool_context import tool_call_context
from ..core.trace import TraceEvent
from ..core.pipeline import Pipeline, PipelineResult
from ..exceptions import BestTeamError, ConfigurationError, EngineError
from .base import EngineAdapter

if TYPE_CHECKING:
    from ..core.requirements import Requirements
    from ..core.specification import Specification

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
    # Admin diagnostic re-run (see core/trace.py): when True, `_run_agent`
    # additionally emits `agent_prompt`/`model_turn` events and adds
    # `args`/`result` to the tool events. Plain field, same lifecycle as
    # `memory_preamble` -- read by nodes, set once by `_initial_state`, so the
    # cached compiled graph needs no recompile to switch it on.
    diagnostic: bool


def _fake_architect_specification() -> "Specification":
    from ..core.specification import AgentSpec, PipelineSpec, Specification, TeamSpec

    return Specification(
        name="e2e_support_team",
        display_name="Support Team (E2E)",
        agents=[
            AgentSpec(
                name="support_agent",
                role="Customer Support Specialist",
                goal="Answer customer questions clearly and politely.",
                backstory="A friendly, patient support assistant.",
                model="fake:Thanks for reaching out! Here's how I can help.",
            ),
        ],
        teams=[TeamSpec(name="support_team", mode="sequential", agents=["support_agent"])],
        pipeline=PipelineSpec(steps=["support_team"]),
    )


def _fake_architect_requirements() -> "Requirements":
    from ..core.requirements import Requirements

    return Requirements(
        summary="Customers need faster, friendlier email support.",
        pain_points=["Replies take too long."],
        goals=["Answer common questions quickly."],
        success_criteria=["Customers get a reply within minutes."],
        constraints=["Must stay professional and on-topic."],
        clarifying_questions=[],
    )


class _FakeArchitectStructuredResult:
    """Returned by `_FakeArchitectChatModel.with_structured_output(...).invoke(...)`."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def invoke(self, messages: Any) -> Any:
        return self._value


class _FakeArchitectChatModel(FakeListChatModel):
    """A deterministic, $0 stand-in for E2E tests that is a full drop-in
    chat model (ordinary `.invoke()` works, so it's safe to also run as a
    deployed agent's model -- see the design doc) that ADDITIONALLY
    supports `with_structured_output()` for the two schemas the Team
    Builder wizard needs. Not listed in `DEFAULT_MODEL_CATALOG`; only
    reachable by resolving the `fake-architect:` spec string directly.

    Note the asymmetry vs. `fake:` in `deploy_validation.validate_agent_models`:
    both spec prefixes are exempted from the model-catalog check there, but
    an unauthorized/malformed request using `fake:` fails loudly at deploy or
    first run with a customer-facing `ConfigurationError` (`fake:` models
    don't support `with_structured_output`), whereas one using
    `fake-architect:` would succeed silently and return a plausible-looking
    but entirely canned team. Not a security regression -- same reachability
    as the pre-existing `fake:` exemption, no privilege escalation, and
    admin-only catalog CRUD keeps it out of the real UI -- just worth being
    explicit about here.
    """

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeArchitectStructuredResult:
        from ..core.requirements import Requirements
        from ..core.specification import Specification

        if schema is Requirements:
            return _FakeArchitectStructuredResult(_fake_architect_requirements())
        if schema is Specification:
            return _FakeArchitectStructuredResult(_fake_architect_specification())
        raise NotImplementedError(
            f"fake-architect: has no canned response for schema {schema!r}"
        )


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
            return FakeListChatModel(responses=[model[len("fake:") :]])
        # "fake-architect:<name>" is like "fake:" but additionally supports
        # `with_structured_output()` for the Team Builder wizard's Requirements/
        # Specification schemas -- see `_FakeArchitectChatModel`.
        if model.startswith("fake-architect:"):
            return _FakeArchitectChatModel(responses=[model[len("fake-architect:") :] or "OK, done."])
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


# Upper bound on each string field of a diagnostic-only event (`agent_prompt`,
# `model_turn`, `tool_completed.result`). Generous on purpose -- the point of
# a diagnostic run is to show what the model actually saw -- but still a bound,
# since every one of these lands in a `trace_events` row.
_MAX_DIAGNOSTIC_CHARS = 20_000


def _diagnostic_text(value: Any) -> str:
    """Stringify a diagnostic payload, capped at `_MAX_DIAGNOSTIC_CHARS`."""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= _MAX_DIAGNOSTIC_CHARS:
        return text
    return f"{text[:_MAX_DIAGNOSTIC_CHARS]}…[truncated]"


def _model_turn_data(turn: int, response: Any) -> Dict[str, Any]:
    """`model_turn` data for a diagnostic run: the model's text and the tool
    calls it asked for. Never the provider's call ids. An email tool's args are
    model-authored mail content (a message id, a draft body) and are dropped
    here for the same reason `_redacted_email_tool_data` exists."""
    tool_calls = []
    for call in getattr(response, "tool_calls", None) or []:
        name = call.get("name")
        tool_calls.append(
            {"name": name, "args": None if name in _EMAIL_TOOLS_NEEDING_REDACTION else call.get("args")}
        )
    content = getattr(response, "content", response)
    return {"turn": turn, "content": _diagnostic_text(content), "tool_calls": tool_calls}


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
_EMAIL_TOOLS_NEEDING_REDACTION = frozenset(
    {"email_find", "email_read", "email_read_attachment", "email_draft_reply"}
)

# `message_id` is a model-controlled tool-call argument (not our own deterministic
# text), so it needs the same length bound `_summarize()` gives everything else --
# otherwise a model could smuggle an arbitrarily long/injected string into the
# trace via this one field.
_MESSAGE_ID_TRACE_CHARS = 64

# Mirrors tools/email_client.py's `_OUT_OF_BATCH` sentinel (a UID-scoped run's
# email_read/email_read_attachment/email_draft_reply refuse any id outside the
# poller-detected batch).
# Not imported directly to avoid the adapters layer depending on a specific
# tools implementation -- see src/bestteam/tools/CLAUDE.md.
_OUT_OF_BATCH_TEXT = "That message isn't part of this batch of new mail."


def _bounded_message_id(call_args: Dict[str, Any]) -> str:
    # Strip to match the email tools' own normalization (email_client.py's
    # `_read_impl`/`_draft_impl` call `.strip()` before touching the mailbox).
    # Without this, a model calling with " 42 " records that unstripped id in
    # trace evidence, while automation_results.py compares the envelope's
    # stripped "42" against it -- a real draft goes unrecognized as confirmed,
    # risking a duplicate draft on retry (Codex review finding).
    raw = str(call_args.get("message_id", "")).strip()
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
    if tool_name == "email_read_attachment":
        # The result IS the extracted attachment text -- the most sender-
        # controlled string the toolkit produces -- so nothing derived from it
        # goes into the trace. The filename is left out for the same reason:
        # the sender chose it and it carries no length bound of its own.
        # A single outcome, because every other path this tool takes (an
        # unsupported type, a parser that gave up) is reported in a sentence
        # that embeds that same filename, and matching on those sentences
        # would hand the sender a say in which outcome gets recorded.
        # One caveat this does not escape: the shared out-of-batch and
        # "No message found" checks above run before this branch and match on
        # the result text too, so an attachment whose extracted text starts
        # with either sentinel is recorded under that outcome instead. The
        # direction is safe -- both force needs_attention downstream, so a
        # sender can add noise, never suppress an escalation -- but the
        # outcome is not derived from the arguments alone the way the comment
        # at the top of this function describes.
        return {
            "summary": f"Read an attachment on message '{message_id}'.",
            "message_id": message_id,
            "outcome": "attachment_read",
        }
    # email_draft_reply: both backends' success text starts with "Draft reply"
    # (see tools/email_client.py's Graph/IMAP `draft_reply` implementations).
    if text.startswith("Draft reply"):
        return {
            "summary": f"Draft reply saved for message '{message_id}'.",
            "message_id": message_id,
            "outcome": "draft_created",
        }
    # The idempotency guard found this message's source key already in Drafts
    # and skipped the APPEND. A draft exists, so this counts as confirmed for
    # retry exclusion exactly like `draft_created` -- but it is reported
    # distinctly so the trace never claims a write that did not happen.
    if text.startswith("A draft reply for this message already exists"):
        return {
            "summary": f"Draft reply already existed for message '{message_id}'.",
            "message_id": message_id,
            "outcome": "draft_exists",
        }
    return {
        "summary": f"Draft reply attempt for message '{message_id}' did not complete.",
        "message_id": message_id,
        "outcome": "unknown",
    }


def _kb_tool_trace_data(trace: Dict[str, Any]) -> Dict[str, Any]:
    """Business-safe `tool_completed` data for a knowledge base tool: the
    query, how many chunks matched and which documents they came from --
    never a line of the documents themselves, which `_summarize()` would put
    straight into the `trace_events` table and any UI rendering it.

    Built from what the tool reported through `core/tool_context.py` rather
    than parsed out of its return text. A tool that reported nothing (a
    custom `KnowledgeBase` wrapper that never calls `report_trace`) still
    gets an event of the same shape, just an uninformative one.
    """
    return {
        "summary": trace.get("summary") or "Knowledge base searched.",
        "query": trace.get("query", ""),
        "hit_count": trace.get("hit_count", 0),
        "sources": list(trace.get("sources") or []),
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
    diagnostic: bool = False,
) -> str:
    """Run one agent's full tool-calling turn on `input_text`, returning its final text.

    `diagnostic` (an admin diagnostic re-run, see `core/trace.py`) additionally
    emits `agent_prompt` (the exact system prompt and input) and one
    `model_turn` per model call, and adds `args` to `tool_started` / `result`
    to `tool_completed` -- except for the email tools, which keep their
    redaction on every path. With it off, the event stream is unchanged.

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
            on_event(TraceEvent(type=event_type, pipeline="", agent=agent.name, data=data))

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
    if diagnostic:
        _emit(
            "agent_prompt",
            {"system_prompt": _diagnostic_text(system_prompt), "input": _diagnostic_text(input_text)},
        )
    response = first_call_model.invoke(messages)
    _record_usage(agent, response, usage_sink)
    if diagnostic:
        _emit("model_turn", _model_turn_data(1, response))

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
                # An email tool's args/result are mail content -- redacted on
                # every path, diagnostic or not (see _EMAIL_TOOLS_NEEDING_REDACTION).
                reveal = diagnostic and call["name"] not in _EMAIL_TOOLS_NEEDING_REDACTION
                started_data: Dict[str, Any] = {"tool": call["name"]}
                if reveal:
                    started_data["args"] = call["args"]
                _emit("tool_started", started_data)
                start = time.monotonic()
                try:
                    with tool_call_context() as tool_ctx:
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
                    if reveal:
                        # What the model is about to read (the ToolMessage
                        # below) -- the raw exception text stays out of the
                        # business-safe `summary` as before.
                        failure_data["result"] = _diagnostic_text(result)
                    if call["name"] in ("email_read", "email_read_attachment", "email_draft_reply"):
                        # Retain the bounded message id on failure too, same as a
                        # successful call's redacted data -- otherwise a failed
                        # email_read/email_read_attachment/email_draft_reply can't
                        # be correlated back to its UID downstream
                        # (automation_results.py's per-UID needs_attention
                        # enforcement, Codex review finding).
                        failure_data["message_id"] = _bounded_message_id(call["args"])
                    _emit("tool_completed", failure_data)
                else:
                    if call["name"] in _EMAIL_TOOLS_NEEDING_REDACTION:
                        extra_data = _redacted_email_tool_data(call["name"], call["args"], result)
                    elif getattr(tool_fn, "__bestteam_tool_kind__", None) == "knowledge_base":
                        extra_data = _kb_tool_trace_data(tool_ctx.trace)
                    else:
                        extra_data = {"summary": _summarize(result)}
                    if reveal:
                        extra_data["result"] = _diagnostic_text(result)
                    _emit(
                        "tool_completed",
                        {
                            "tool": call["name"],
                            "success": True,
                            "duration_ms": int((time.monotonic() - start) * 1000),
                            **extra_data,
                        },
                    )
                if usage_sink is not None:
                    # LLM/embedding calls the tool made internally (a knowledge
                    # base's query embedding and its query-expansion call) ride
                    # the agent's own usage list, so they reach `usage_records`
                    # through the same `agent_completed.usage` path as a model
                    # call -- no new event field, no backend change. Drained on
                    # the failure path too: the paid call already happened.
                    usage_sink.extend(tool_ctx.usage)
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        _emit("agent_progress", {"note": f"iteration {i + 1} of {_MAX_TOOL_ITERATIONS}"})
        response = model.invoke(messages)
        _record_usage(agent, response, usage_sink)
        if diagnostic:
            _emit("model_turn", _model_turn_data(i + 2, response))

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
    diagnostic: bool = False,
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
                    pipeline="",
                    agent=manager_name,
                    data={"to": agent.name, "task_summary": _summarize(task)},
                )
            )
            on_event(
                TraceEvent(
                    type="subagent_started",
                    pipeline="",
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
            diagnostic=diagnostic,
        )
        if on_event is not None:
            on_event(
                TraceEvent(
                    type="subagent_completed",
                    pipeline="",
                    agent=agent.name,
                    data={"success": True, "summary": _summarize(result)},
                )
            )
            on_event(
                TraceEvent(
                    type="delegation_completed",
                    pipeline="",
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
            diagnostic=state.get("diagnostic", False),
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
        diagnostic = state.get("diagnostic", False)
        # Subordinates get the recalled user memory (like sequential/parallel
        # agents) but not the manager's delegation guidance, which is manager-only.
        delegate_tools = [
            _make_delegate_tool(
                agent,
                usage_sink=usage_sink,
                extra_system_prompt=preamble,
                on_event=sub_events.append,
                manager_name=manager.name,
                diagnostic=diagnostic,
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
            diagnostic=diagnostic,
        )
        return {
            "contributions": {manager.name: text},
            "usage": {manager.name: usage_sink},
            "trace_events": {manager.name: sub_events},
            "context": text,
            "output": text,
        }

    return node


def _initial_state(input: str, memory_preamble: str = "", diagnostic: bool = False) -> _TeamState:
    return {
        "input": input,
        "context": "",
        "contributions": {},
        "usage": {},
        "trace_events": {},
        "output": "",
        "memory_preamble": memory_preamble,
        "diagnostic": diagnostic,
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

    def compile(self, pipeline: Pipeline) -> Any:
        graph = StateGraph(_TeamState)
        previous_exit = START

        for team in pipeline.steps:
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

    def execute(
        self, compiled: Any, input: str, memory_preamble: str = "", diagnostic: bool = False
    ) -> PipelineResult:
        try:
            final_state = compiled.invoke(_initial_state(input, memory_preamble, diagnostic))
        except BestTeamError:
            # Already a framework-level error (e.g. ConfigurationError raised
            # lazily during model resolution) — surface it as-is rather than
            # masking it behind a generic "engine execution failed".
            raise
        except Exception as exc:
            raise EngineError(f"Pipeline execution failed: {exc}") from exc

        steps = [
            {"agent": name, "output": text}
            for name, text in final_state.get("contributions", {}).items()
        ]
        return PipelineResult(
            output=final_state.get("output", ""),
            steps=steps,
            raw=final_state,
        )

    def to_mermaid(self, compiled: Any) -> str:
        return compiled.get_graph().draw_mermaid()

    def stream(
        self, compiled: Any, input: str, memory_preamble: str = "", diagnostic: bool = False
    ) -> Iterator[TraceEvent]:
        """Yield an `agent_completed` TraceEvent each time a node finishes.

        Built on LangGraph's `stream_mode="updates"`, which yields exactly the
        partial state each node returned — so `contributions` always holds
        just that node's own entry, never a merged view of the whole run.
        """
        try:
            for update in compiled.stream(
                _initial_state(input, memory_preamble, diagnostic), stream_mode="updates"
            ):
                for partial in update.values():
                    if not isinstance(partial, dict):
                        continue
                    usage_by_agent = partial.get("usage", {})
                    events_by_agent = partial.get("trace_events", {})
                    for agent_name, text in partial.get("contributions", {}).items():
                        yield from events_by_agent.get(agent_name, [])
                        yield TraceEvent(
                            type="agent_completed",
                            pipeline="",
                            agent=agent_name,
                            data=text,
                            usage=usage_by_agent.get(agent_name, []),
                        )
        except BestTeamError:
            raise
        except Exception as exc:
            raise EngineError(f"Pipeline execution failed: {exc}") from exc
