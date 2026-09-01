from __future__ import annotations

import logging
import operator
import time
from typing import TYPE_CHECKING, Annotated, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from ..core.agent import Agent
from ..core.grounding import (
    GROUNDING_REFUSAL_TEXT,
    GROUNDING_RETRY_INSTRUCTION,
    check_grounding,
    claim_retry_instruction,
    grade_claims,
)
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
#
# Ten rather than five because search-read-search-again is the ordinary shape
# of a research turn, so five was reached by legitimate work and not only by a
# runaway model. An agent with no tools never enters the loop and one with a
# simple task settles in a round or two, so the higher ceiling costs nothing in
# the common case -- it only widens the worst case, which the wrap-up call
# below now ends with an answer rather than a discarded turn.
_MAX_TOOL_ITERATIONS = 10

# Sent as the wrap-up call's final message. The model has to be told the budget
# is gone, or it answers with another tool request and the turn is wasted twice.
_WRAP_UP_INSTRUCTION = (
    "You have used every tool call available for this turn. Do not request any "
    "more tools. Answer now with what you have gathered so far, and say plainly "
    "which parts are missing or unverified."
)


def _tool_loop_exhausted_notice(agent_name: str) -> str:
    """Output returned when an agent uses up `_MAX_TOOL_ITERATIONS` and the
    wrap-up call still produces no text. The final tool-calling response's
    `content` is an empty string, so returning it would make an exhausted run
    look like a silent empty success (CR-011). This explicit notice surfaces the
    truncation in the agent's output and the resulting `agent_completed` trace
    event instead."""
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
    # Optional per-run side channel for token streaming (see
    # docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md). Plain
    # fields, no reducer: set once by `_initial_state`, only ever read by
    # nodes. They hold callables rather than data because `compile()`'s result
    # is cached and reused across runs, so a per-run sink baked into a node
    # closure would leak into the next run.
    on_token: Optional[Callable[[str], None]]
    should_cancel: Optional[Callable[[], bool]]


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


_FAKE_INTERVIEW_MARKER = "[interview me]"
_FAKE_ANSWERS_HEADER = "The customer was asked these clarifying questions:"


def _fake_architect_requirements(prompt_text: str) -> "Requirements":
    from ..core.requirements import Requirements

    base = Requirements(
        summary="Customers need faster, friendlier email support.",
        pain_points=["Replies take too long."],
        goals=["Answer common questions quickly."],
        success_criteria=["Customers get a reply within minutes."],
        constraints=["Must stay professional and on-topic."],
        clarifying_questions=[],
    )
    if _FAKE_ANSWERS_HEADER in prompt_text:
        # Deterministic "folding": each answered pair lands in constraints
        # verbatim, each unanswered one becomes a fixed assumption -- so an
        # E2E test can assert the round-trip on the Confirm page. Answers come
        # from a textarea, so an "A: " line runs until the next "Q: " line (or
        # the end of the answers block), not just to its own line break.
        block = prompt_text.split(_FAKE_ANSWERS_HEADER, 1)[1].split("\n\n", 1)[0]
        collected: list[str] = []
        answer_lines: Any = None
        for line in block.splitlines():
            if line.startswith("Q: "):
                if answer_lines is not None:
                    collected.append("\n".join(answer_lines))
                answer_lines = None
            elif line.startswith("A: "):
                answer_lines = [line[len("A: "):]]
            elif answer_lines is not None:
                answer_lines.append(line)
        if answer_lines is not None:
            collected.append("\n".join(answer_lines))
        for text in collected:
            if text.startswith("(not answered"):
                base.constraints.append("Assumed: replies can go out within one business day.")
            else:
                base.constraints.append(f"The customer clarified: {text}")
        return base
    if _FAKE_INTERVIEW_MARKER in prompt_text:
        base.clarifying_questions = [
            "How many emails do you receive per day?",
            "Which mailbox provider do you use?",
        ]
    return base


class _FakeArchitectStructuredResult:
    """Returned by `_FakeArchitectChatModel.with_structured_output(...).invoke(...)`."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def invoke(self, messages: Any) -> Any:
        if callable(self._value):
            text = "\n".join(str(getattr(m, "content", m)) for m in messages)
            return self._value(text)
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
            return _FakeArchitectStructuredResult(_fake_architect_requirements)
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
    return _spec_string(agent.model)


def _spec_string(model: Any) -> str:
    """A best-effort spec string for any model value, for usage attribution."""
    if isinstance(model, str):
        return model
    return getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__


# Emitted through `on_token` when a model call that had already produced text
# turns out to be a tool call after all: the consumer must discard what it has
# shown. A NUL-prefixed sentinel cannot collide with model output, and one
# callback stays a smaller interface change than two (see
# docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md).
STREAM_RESET = "\x00bestteam:reset"


def _supports_stream_usage(model: Any) -> bool:
    """True if this model reports token usage while streaming.

    ChatOpenAI and family declare a `stream_usage` field; binding it makes the
    aggregated chunk carry `usage_metadata`, so metering is unchanged. Binding
    it on a model that does NOT declare it would push an unexpected kwarg down
    into its `_stream()`, so this is checked before the bind, on the resolved
    model rather than on a `RunnableBinding` wrapper.
    """
    return "stream_usage" in getattr(type(model), "model_fields", {})


def _should_stream(model: BaseChatModel) -> bool:
    """Whether this model's calls may be streamed.

    Streaming a billable model that does not report usage while streaming
    would silently stop metering the largest call in the run, so it is
    refused -- an unstreamed reply is better than an unmetered one. A fake
    reports no usage on any path, so streaming it loses nothing; that is also
    what makes this feature testable at zero cost.
    """
    return _supports_stream_usage(model) or isinstance(
        model, (FakeListChatModel, FakeMessagesListChatModel)
    )


def _chunk_text(chunk: Any) -> str:
    """The plain text of one streamed chunk.

    `content` is a string for every provider we support today; the list form
    (content blocks) is handled so a provider that returns one degrades to its
    text parts rather than to `str(list)` on a visitor's screen. Deliberately
    avoids `BaseMessage.text`, which is a method in langchain-core 0.3 and a
    property in 1.x.
    """
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


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


def _diagnostic_args(value: Any) -> Any:
    """Size-bound a tool call's args for a diagnostic event: every string in
    the (possibly nested) JSON structure goes through `_diagnostic_text`, so a
    model-authored 100k-character argument can't produce an unbounded
    `tool_started`/`model_turn` row. Shape and non-string values are kept."""
    if isinstance(value, str):
        return _diagnostic_text(value)
    if isinstance(value, dict):
        return {k: _diagnostic_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_diagnostic_args(v) for v in value]
    return value


def _model_turn_data(turn: int, response: Any) -> Dict[str, Any]:
    """`model_turn` data for a diagnostic run: the model's text and the tool
    calls it asked for. Never the provider's call ids. An email tool's args are
    model-authored mail content (a message id, a draft body) and are dropped
    here for the same reason `_redacted_email_tool_data` exists."""
    tool_calls = []
    for call in getattr(response, "tool_calls", None) or []:
        name = call.get("name")
        tool_calls.append(
            {
                "name": name,
                "args": None if name in _EMAIL_TOOLS_NEEDING_REDACTION else _diagnostic_args(call.get("args")),
            }
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
        # Which ingestion generation answered (None for a folder-built KB),
        # and per hit the chunk/document row and the scores behind its rank
        # -- the tool bounds the list and never puts chunk text in it.
        "ingestion_job_id": trace.get("ingestion_job_id"),
        "hits": list(trace.get("hits") or []),
    }


def _has_knowledge_base_tool(agent: Agent) -> bool:
    """True when one of the agent's own tools is a knowledge base (the marker
    `make_knowledge_base_tool` sets). Decides two things for the agent's turn:
    its first model call is forced to use a tool, and its final text gets a
    grounding check (core/grounding.py)."""
    return any(getattr(fn, "__bestteam_tool_kind__", None) == "knowledge_base" for fn in agent.tools)


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
    streams: bool = False,
    on_token: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
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
    should. `_agent_node` uses it for an agent that carries a knowledge-base
    tool, for the same reason. Later iterations in this same call always use
    the unforced binding, so the agent can still settle on a final text answer once it has
    gathered what it needs. A model that refuses the forcing outright (see
    `_first_call`) falls back to the unforced binding rather than failing. If `usage_sink` is given, each model invocation's
    `usage_metadata` (when reported) is appended to it for usage metering.
    If `on_event` is given, it's called with each granular `TraceEvent`
    (agent_started/tool_started/tool_completed/agent_progress) as this turn
    progresses -- `LangGraphAdapter.stream()` buffers these per-node and
    flushes them just before that node's `agent_completed`.

    `streams` (set at wiring time for the one agent whose text IS the run's
    output) plus an `on_token` sink makes each model call stream, with every
    text delta handed to the sink as it arrives. This is a side channel out of
    the node on purpose: `LangGraphAdapter.stream()` only yields at node
    boundaries, so nothing on that path can reach a subscriber while the reply
    is still being written. `should_cancel`, if given, is polled between
    deltas so a long reply can be stopped mid-generation rather than merely
    ignored. Streaming is refused for a model whose usage would be lost (see
    `_should_stream`); with `streams=False` or no sink, behaviour here is
    identical to a plain `invoke()`.
    """

    def _emit(event_type: str, data: Any = None) -> None:
        if on_event is not None:
            on_event(TraceEvent(type=event_type, pipeline="", agent=agent.name, data=data))

    model = _resolve_model(agent.model)
    raw_model = model
    all_tools = [*agent.tools, *extra_tools]
    tools_by_name = {fn.__name__: fn for fn in all_tools}
    # Grounding-lite (core/grounding.py): what this turn's knowledge-base
    # searches returned, so the final text's [source: …] tags can be checked
    # against them. Only ever read when the agent has a knowledge-base tool.
    kb_searches = 0
    kb_hit_count = 0
    kb_citations: List[str] = []
    kb_documents: List[str] = []
    # The KB tool results' text, verbatim -- the claim grader's evidence
    # (grounding_level: "claim"). Same material as the turn's ToolMessages.
    kb_result_texts: List[str] = []
    first_call_model = model
    forced_first_call = False
    if all_tools:
        try:
            model = model.bind_tools(all_tools)
            first_call_model = (
                model.bind_tools(all_tools, tool_choice="required") if require_tool_use_on_first_call else model
            )
            forced_first_call = require_tool_use_on_first_call
        except NotImplementedError:
            pass  # model doesn't support tool calling (e.g. FakeListChatModel in tests)

    # Two separate questions. `forward_text` is "may this agent's text reach
    # the visitor" -- true only for the one agent wired to stream.
    # `stream_call` is "must this call be interruptible", which is true for
    # every agent in a run that supplied a cancel check: `invoke()` blocks
    # until the whole paid generation finishes, so Stop would sit unresponsive
    # through an earlier agent's entire turn (Codex review finding). A
    # non-forwarding agent still consumes its chunks; it just drops the text.
    forward_text = streams and on_token is not None
    stream_call = (forward_text or should_cancel is not None) and _should_stream(raw_model)
    # The wrap-up call deliberately keeps the pre-`bind_tools` model: its whole
    # purpose is a turn the model cannot answer with another tool request.
    wrapup_model = raw_model
    if stream_call and _supports_stream_usage(raw_model):
        # Bound after bind_tools: `.bind()` on a RunnableBinding merges kwargs,
        # so both bindings survive. Without this the aggregated chunk carries
        # no `usage_metadata` and the run's largest call goes unmetered.
        model = model.bind(stream_usage=True)
        first_call_model = first_call_model.bind(stream_usage=True)
        wrapup_model = wrapup_model.bind(stream_usage=True)

    def _call(bound_model: Any, msgs: List[Any]) -> Any:
        """One model call -- streamed with deltas, or a plain `invoke`.

        The streamed branch accumulates chunks into a message equivalent to
        what `invoke()` would have returned (tool calls merged, usage attached),
        so everything downstream of this function is unaware of the difference.
        """
        if should_cancel is not None and should_cancel():
            # Never open a NEW provider request after a stop. Without this the
            # next call in the tool loop is dispatched -- billable, and Stop
            # then waits on that provider's first chunk before it can take
            # effect (Codex review finding). An empty response settles the
            # loop: no tool calls, so the agent returns what it has.
            return AIMessage(content="")
        if not stream_call:
            return bound_model.invoke(msgs)
        full = None
        emitted = False
        tool_call_seen = False
        for chunk in bound_model.stream(msgs):
            full = chunk if full is None else full + chunk
            if getattr(chunk, "tool_call_chunks", None) and not tool_call_seen:
                tool_call_seen = True
                if emitted and on_token is not None:
                    # Text already went out for what turns out to be a tool
                    # call -- tell the consumer to discard it. Providers
                    # normally emit tool calls from the first chunk, so this is
                    # insurance rather than a common path.
                    on_token(STREAM_RESET)
            if forward_text and on_token is not None and not tool_call_seen:
                text = _chunk_text(chunk)
                if text:
                    on_token(text)
                    emitted = True
            if should_cancel is not None and should_cancel():
                # Stop generating rather than merely ignoring the result. The
                # node then finishes early and the caller's own cancellation
                # handling does the rest -- no new terminal path. The
                # provider's usage arrives in a final chunk this never reads,
                # so a cancelled call goes unmetered: a bounded, deliberate
                # cost, since draining the stream would spend exactly the
                # tokens we are stopping.
                break
        return full if full is not None else bound_model.invoke(msgs)

    def _first_call(msgs: List[Any]) -> Any:
        """The opening call, dropping a forced `tool_choice` if it is refused.

        Some reasoning/"thinking mode" models -- DeepSeek's, for one -- reject
        a forced `tool_choice` outright with a 400, and it arrives when the
        call is made rather than when the tools are bound. Left unhandled it
        fails the run on its very first call, so a whole HIERARCHICAL team is
        unusable on such a model: both forcing sites are on that path (a
        manager's first turn, and a delegated subordinate that carries tools).
        `core/_structured_output.invoke_structured` already meets the same
        refusal on the structured-output path; this is the tool-calling one.

        Only the forcing is dropped -- the delegation guidance is still in the
        system prompt, so a manager that heeds it still delegates and one that
        does not answers directly. A weaker first turn, but a turn. The retry
        is keyed on the provider's own wording so an unrelated failure is not
        quietly paid for twice, and a rejected request is billed for nothing,
        so the second call is the only one that costs anything.
        """
        try:
            return _call(first_call_model, msgs)
        except Exception as exc:
            if not forced_first_call or "tool_choice" not in str(exc).lower():
                raise
            _logger.info(
                "Agent '%s': model refused a forced tool_choice, retrying without it (%s)",
                agent.name,
                exc,
            )
            return _call(model, msgs)

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
    response = _first_call(messages)
    _record_usage(agent, response, usage_sink)
    model_turns = 1
    if diagnostic:
        _emit("model_turn", _model_turn_data(model_turns, response))

    for i in range(_MAX_TOOL_ITERATIONS):
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            break
        if should_cancel is not None and should_cancel():
            # Cancellation observed while THIS response was streaming. Stopping
            # the model call is not enough: a tool call has side effects, and
            # running one after the visitor pressed Stop -- then calling the
            # model again on its result -- is exactly what a stop must prevent
            # (Codex review finding). The empty return is deliberate: the
            # partial text has already been shown live, and returning it here
            # would PERSIST it as this agent's `agent_completed` output --
            # recording a stopped agent as one that completed with a partial
            # reply, which the live-only contract for streamed text forbids.
            # `usage_sink` is untouched, so calls already paid for are still
            # metered.
            return ""
        messages.append(response)
        for call in tool_calls:
            if should_cancel is not None and should_cancel():
                # A stop that lands while an earlier call in this batch is
                # running must abandon the rest of it: each one is its own
                # side effect (Codex review finding). Returning rather than
                # breaking matters -- a break would fall through to another
                # model call on a half-answered batch. Empty for the same
                # reason as the guard above: a stopped agent must not be
                # recorded as having completed with partial output.
                return ""
            tool_fn = tools_by_name.get(call["name"])
            if tool_fn is None:
                result = f"Error: unknown tool '{call['name']}'"
            else:
                # An email tool's args/result are mail content -- redacted on
                # every path, diagnostic or not (see _EMAIL_TOOLS_NEEDING_REDACTION).
                reveal = diagnostic and call["name"] not in _EMAIL_TOOLS_NEEDING_REDACTION
                started_data: Dict[str, Any] = {"tool": call["name"]}
                if reveal:
                    started_data["args"] = _diagnostic_args(call["args"])
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
                        kb_searches += 1
                        kb_hit_count += int(tool_ctx.trace.get("hit_count") or 0)
                        kb_citations.extend(tool_ctx.trace.get("citations") or ())
                        kb_documents.extend(tool_ctx.trace.get("citation_documents") or ())
                        kb_result_texts.append(str(result))
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
        response = _call(model, messages)
        _record_usage(agent, response, usage_sink)
        model_turns += 1
        if diagnostic:
            _emit("model_turn", _model_turn_data(model_turns, response))

    if should_cancel is not None and should_cancel():
        # A stop reached this turn, including one that only interrupted the
        # chunk loop. Whatever streamed has already been shown live; returning
        # it here would persist it as this agent's `agent_completed` output
        # and record a stopped agent as one that completed with a partial
        # reply (Codex review finding). `usage_sink` is untouched, so calls
        # already paid for are still metered.
        return ""
    if getattr(response, "tool_calls", None):
        # The loop ran out while the model was still asking for tools, so it
        # never produced a text answer -- `response.content` is empty here.
        # Everything the tools returned is still in `messages`, though, and it
        # was paid for: one more call with no tools bound turns that material
        # into the answer instead of discarding the whole turn.
        #
        # `response` itself is deliberately NOT appended. It carries tool calls
        # with no matching results, and providers reject that conversation
        # shape. `messages` already ends with the last iteration's ToolMessages,
        # so appending the instruction alone leaves it valid.
        #
        # The `should_cancel` guard directly above still stands between a stop
        # and this call, so a stopped run never opens it.
        messages.append(HumanMessage(content=_WRAP_UP_INSTRUCTION))
        response = _call(wrapup_model, messages)
        _record_usage(agent, response, usage_sink)
        model_turns += 1
        if diagnostic:
            _emit("model_turn", _model_turn_data(model_turns, response))
        if not str(getattr(response, "content", "") or "").strip():
            return _tool_loop_exhausted_notice(agent.name)
    text = response.content if hasattr(response, "content") else str(response)
    if _has_knowledge_base_tool(agent):
        # Grounding-lite: how does the answer's citations compare with what
        # this turn's searches returned? With the default `observe` policy the
        # result is recorded and the text returned unchanged -- and the event
        # payload is byte-identical to the pre-policy one. Not emitted on the
        # early returns above (a stop, an exhausted loop): those turns
        # produced no answer to check.
        result = check_grounding(
            text, kb_citations, documents=kb_documents, searches=kb_searches, hit_count=kb_hit_count
        )
        policy = agent.grounding_policy
        claim_level = agent.grounding_level == "claim"
        grading = None
        claim_check_error = False
        grader_model = None

        def _grade(answer_text: str) -> None:
            # One grader call: claim split + per-claim verdict against this
            # turn's own search results. `None` grading = the check was not
            # performed (invoke failed or unparseable) -- fail-soft to the
            # citation-level result, never a reason to retry or refuse. The
            # call is billed either way, so usage is metered either way.
            nonlocal grading, claim_check_error, grader_model
            if should_cancel is not None and should_cancel():
                # Same contract as `_call`: never open a NEW provider request
                # after a stop, and the guard before the grounding block is not
                # enough on its own -- `should_cancel` reads a flag another
                # thread sets, so a stop can land in the instant between the
                # two (Codex review finding). Not a `claim_check_error`: the
                # check did not fail, the turn ended, and the guard below
                # discards this text anyway.
                return
            if grader_model is None:
                try:
                    grader_model = (
                        _resolve_model(agent.grounding_model) if agent.grounding_model is not None else raw_model
                    )
                except Exception:  # noqa: BLE001 -- deliberate fail-soft, incl. ConfigurationError:
                    # a bad grader spec degrades the CHECK, it must never fail
                    # the RUN (rerank/expansion precedent). Not the
                    # BestTeamError-masking case -- nothing is re-raised as
                    # EngineError here.
                    _logger.warning(
                        "Agent '%s': claim grader model could not be resolved; falling back to citation-level",
                        agent.name,
                        exc_info=True,
                    )
                    grading = None
                    claim_check_error = True
                    return
            grading, grader_response = grade_claims(answer_text, kb_result_texts, grader_model)
            claim_check_error = grading is None
            usage = getattr(grader_response, "usage_metadata", None)
            if usage and usage_sink is not None:
                usage_sink.append(
                    {
                        "model": _spec_string(agent.grounding_model)
                        if agent.grounding_model is not None
                        else _model_spec(agent),
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                    }
                )

        if claim_level and result.passes:
            _grade(text)

        def _passes() -> bool:
            return result.passes and (grading is None or grading.passes)

        retried = False
        refused = False
        if policy in ("retry", "refuse") and not _passes():
            # One corrective call on the same conversation: the search results
            # are already in this turn's ToolMessages, so the model needs a
            # rewrite instruction, not new searches. Exactly one retry -- no
            # loop. The failing text may already have streamed to a viewer,
            # so the sink is told to discard it before fresh text arrives.
            # A claim-level failure names the unsupported claims so the model
            # deletes or re-grounds exactly those, keeping the rest.
            if forward_text and on_token is not None:
                on_token(STREAM_RESET)
            instruction = (
                claim_retry_instruction(grading.unsupported)
                if result.passes and grading is not None and not grading.passes
                else GROUNDING_RETRY_INSTRUCTION
            )
            messages.append(response)
            messages.append(HumanMessage(content=instruction))
            retry_response = _call(model, messages)
            _record_usage(agent, retry_response, usage_sink)
            model_turns += 1
            if diagnostic:
                _emit("model_turn", _model_turn_data(model_turns, retry_response))
            if should_cancel is not None and should_cancel():
                # Same contract as the guards above: a stopped agent must not
                # be recorded as having completed with partial output.
                return ""
            retried = True
            if not getattr(retry_response, "tool_calls", None):
                text = retry_response.content if hasattr(retry_response, "content") else str(retry_response)
                result = check_grounding(
                    text,
                    kb_citations,
                    documents=kb_documents,
                    searches=kb_searches,
                    hit_count=kb_hit_count,
                )
                grading = None
                claim_check_error = False
                if claim_level and result.passes:
                    _grade(text)
            # else: a retry that asks for more tools instead of answering is a
            # failed retry -- the original text, result and grading stand.
            if policy == "refuse" and not _passes():
                # The viewer must not keep a retried-but-still-ungrounded
                # answer either; the authoritative refusal rides run_completed.
                refused = True
                if forward_text and on_token is not None:
                    on_token(STREAM_RESET)
                text = GROUNDING_REFUSAL_TEXT
        if should_cancel is not None and should_cancel():
            # A stop that landed while the grader was in flight. `grade_claims`
            # invokes in one go -- unlike the agent's own calls it cannot be
            # broken off mid-generation, an accepted cost bounded by the guard
            # inside `_grade`, which keeps a stopped turn from starting one at
            # all. What must not follow is persisting the answer it graded, so
            # this mirrors the guards above: no output, no event.
            return ""
        data = result.as_trace_data()
        if policy != "observe":
            data.update(policy=policy, retried=retried, refused=refused)
        if claim_level:
            # Only the opt-in level adds keys -- the default payload stays
            # byte-identical (the observe invariant, extended to the level).
            data["level"] = "claim"
            if grading is not None:
                data["claims"] = grading.claims
                data["claims_supported"] = grading.supported
                data["unsupported_claims"] = list(grading.unsupported)
            elif claim_check_error:
                data["claim_check_error"] = True
            # A citation-check failure means the grader never ran: no claim
            # keys at all, distinguishable by their absence.
        _emit("grounding_checked", data)
    return text


def _make_delegate_tool(
    agent: Agent,
    *,
    usage_sink: Optional[List[Dict[str, Any]]] = None,
    extra_system_prompt: str = "",
    on_event: Optional[Callable[[TraceEvent], None]] = None,
    manager_name: str = "",
    diagnostic: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,
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
            # Cancellation follows the delegation; the token sink deliberately
            # does not. A subordinate's answer is working material, not the
            # reply -- but it can call side-effecting tools and burn model
            # turns, so a stop has to reach it (Codex review finding).
            should_cancel=should_cancel,
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


def _agent_node(agent: Agent, *, propagate_context: bool, streams: bool = False):
    """Build a LangGraph node function that runs a single agent.

    `propagate_context` controls whether this agent's output becomes the
    shared context/output for whatever runs next. Sequential agents propagate
    (each hands off to the next); parallel agents don't (they all see the same
    incoming context and only contribute to the final aggregation).

    `streams` marks the one agent per pipeline whose text IS the run's output,
    so its model calls are streamed token by token when the run supplies a
    sink (see `_run_agent` and `LangGraphAdapter.compile`).

    An agent with a knowledge-base tool has its first model call forced to
    use a tool (`require_tool_use_on_first_call`), the same insurance the
    hierarchical paths carry: the tool's docstring asks the model to search
    before answering, and a real model can ignore that. Any other tool set
    keeps the unforced first call. The forcing has `_first_call`'s fallback
    for a provider that rejects it.
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
            require_tool_use_on_first_call=_has_knowledge_base_tool(agent),
            usage_sink=usage_sink,
            on_event=sub_events.append,
            diagnostic=state.get("diagnostic", False),
            streams=streams,
            on_token=state.get("on_token"),
            should_cancel=state.get("should_cancel"),
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


def _hierarchical_node(team: Team, *, streams: bool = False):
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
    delegate tool at all. It is insurance, not a requirement: a model that
    rejects the forcing (DeepSeek's thinking mode does) gets the unforced
    binding instead, so the guidance goes back to being a suggestion rather
    than taking the whole team down with it.
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
                should_cancel=state.get("should_cancel"),
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
            # The manager's final text is the run's output, so it is the one
            # that streams. The delegate tools above deliberately get no sink:
            # a subordinate's answer is working material, not the reply.
            streams=streams,
            on_token=state.get("on_token"),
            should_cancel=state.get("should_cancel"),
        )
        return {
            "contributions": {manager.name: text},
            "usage": {manager.name: usage_sink},
            "trace_events": {manager.name: sub_events},
            "context": text,
            "output": text,
        }

    return node


def _initial_state(
    input: str,
    memory_preamble: str = "",
    diagnostic: bool = False,
    on_token: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> _TeamState:
    return {
        "input": input,
        "context": "",
        "contributions": {},
        "usage": {},
        "trace_events": {},
        "output": "",
        "memory_preamble": memory_preamble,
        "diagnostic": diagnostic,
        "on_token": on_token,
        "should_cancel": should_cancel,
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

        # Exactly one agent per pipeline streams: the one whose text IS the
        # run's output. Decided here, at wiring time, so no node has to work
        # out at runtime whether it happens to be last.
        teams = list(pipeline.steps)
        for index, team in enumerate(teams):
            entry, exit_ = self._wire_team(graph, team, streams_final=index == len(teams) - 1)
            graph.add_edge(previous_exit, entry)
            previous_exit = exit_

        graph.add_edge(previous_exit, END)
        return graph.compile()

    def _wire_team(self, graph: StateGraph, team: Team, streams_final: bool = False) -> Tuple[str, str]:
        if team.mode == CollaborationMode.SEQUENTIAL:
            return self._wire_sequential(graph, team, streams_final)
        if team.mode == CollaborationMode.PARALLEL:
            return self._wire_parallel(graph, team, streams_final)
        if team.mode == CollaborationMode.HIERARCHICAL:
            return self._wire_hierarchical(graph, team, streams_final)
        raise NotImplementedError(
            f"Collaboration mode '{team.mode.value}' is not implemented yet "
            f"(team '{team.name}'). SEQUENTIAL, PARALLEL, and HIERARCHICAL are "
            "available; DEBATE is on the roadmap."
        )

    def _wire_sequential(
        self, graph: StateGraph, team: Team, streams_final: bool = False
    ) -> Tuple[str, str]:
        node_names = []
        for position, agent in enumerate(team.agents):
            node_name = f"{team.name}.{agent.name}"
            graph.add_node(
                node_name,
                _agent_node(
                    agent,
                    propagate_context=True,
                    streams=streams_final and position == len(team.agents) - 1,
                ),
            )
            node_names.append(node_name)

        for current, nxt in zip(node_names, node_names[1:]):
            graph.add_edge(current, nxt)

        return node_names[0], node_names[-1]

    def _wire_parallel(
        self, graph: StateGraph, team: Team, streams_final: bool = False
    ) -> Tuple[str, str]:
        # `streams_final` is deliberately unused: a parallel team's output is
        # `_aggregate_node`'s join of several contributions, produced with no
        # model call at all, so there is no single reply to stream.
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

    def _wire_hierarchical(
        self, graph: StateGraph, team: Team, streams_final: bool = False
    ) -> Tuple[str, str]:
        manager = team.manager
        if manager is None:
            raise ConfigurationError(
                f"Team '{team.name}' uses hierarchical mode and requires a 'manager' agent"
            )
        node_name = f"{team.name}.{manager.name}"
        graph.add_node(node_name, _hierarchical_node(team, streams=streams_final))
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
        self,
        compiled: Any,
        input: str,
        memory_preamble: str = "",
        diagnostic: bool = False,
        *,
        on_token: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Iterator[TraceEvent]:
        """Yield an `agent_completed` TraceEvent each time a node finishes.

        Built on LangGraph's `stream_mode="updates"`, which yields exactly the
        partial state each node returned — so `contributions` always holds
        just that node's own entry, never a merged view of the whole run.

        `on_token`, if given, receives the final agent's text deltas as they
        are produced. That is a side channel out of the node on purpose: this
        generator only yields at node boundaries, so nothing on this path can
        reach a subscriber while the reply is still being written. Deltas are
        NOT TraceEvents and are never persisted. `should_cancel` is polled
        between deltas so a long reply can be stopped mid-generation.
        """
        try:
            for update in compiled.stream(
                _initial_state(input, memory_preamble, diagnostic, on_token, should_cancel),
                stream_mode="updates",
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
