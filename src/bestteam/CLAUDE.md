# bestteam — `src/bestteam/` (SDK core)

Directory-scoped notes for the SDK layer — `Agent`/`Team`/`Pipeline` and the
LangGraph adapter. See the root `CLAUDE.md` for project overview,
architecture, and commands. For the Specification/Requirements
structured-output stages and knowledge bases, see
`src/bestteam/core/CLAUDE.md`; for built-in tools, see
`src/bestteam/tools/CLAUDE.md`.

## SDK layer

`Agent`/`Team`/`Pipeline` are business-facing dataclasses, decoupled from the
engine via an `EngineAdapter` ABC (`adapters/base.py`). The only
implementation today is `LangGraphAdapter` (`adapters/langgraph_adapter.py`);
the seam exists so a CrewAI (or other) adapter could be added later without
touching the public API. Pipelines are declarative YAML (parsed by
`core/loader.py`) — see `ui/backend/pipelines/*.yaml` for examples of
sequential and parallel collaboration modes.

## Key design decisions worth knowing

- **State reducer for parallel agents**: `_TeamState.contributions` uses
  `Annotated[Dict[str, str], operator.or_]` so concurrent node writes merge
  instead of raising `InvalidUpdateError`.
- **`fake:<response>` model spec**: a custom convenience added to
  `_resolve_model()` so YAML pipelines can declare zero-cost,
  deterministic dry-run models (`FakeListChatModel` under the hood) without
  needing real API keys. Use this for demos/tests — see
  `ui/backend/pipelines/*.yaml`.
- **`diagnostic` is a run-level switch, not a config**: `Pipeline.run/stream(
  ..., diagnostic=True)` travels as `_TeamState.diagnostic` (plain field, set
  once by `_initial_state`, like `memory_preamble`) into `_run_agent`, which
  then also emits `agent_prompt`/`model_turn` and adds `args`/`result` to the
  tool events -- see `core/trace.py` for the shapes. Default off keeps the
  event stream byte-identical; the email tools stay redacted either way. The
  backend's admin diagnostic re-run (`ui/backend/CLAUDE.md`) is its only
  caller today.
- **Preserve `BestTeamError` subtypes through adapter boundaries**: always
  `except BestTeamError: raise` *before* a broad `except Exception` in
  adapter code, otherwise `ConfigurationError`/etc. get masked as a generic
  `EngineError` and the real cause is lost.
- **Token streaming is a side channel, not an event type.**
  `Pipeline.stream()` / `EngineAdapter.stream()` take optional
  `on_token: Callable[[str], None]` and `should_cancel: Callable[[], bool]`.
  They have to be a side channel because `LangGraphAdapter.stream()` is built
  on `stream_mode="updates"` and therefore only yields at *node* boundaries --
  nothing on that path can reach a subscriber while the reply is still being
  written. Deltas are not TraceEvents and must never be persisted; the
  authoritative answer is still `run_completed`'s data. Both default to None,
  which is byte-for-byte today's behaviour. Spec:
  `docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md`.
  Four rules in that design are load-bearing:
  - **Exactly one agent per pipeline streams** -- the one whose text IS the
    run's output. `compile()` decides it at wiring time (`streams_final` →
    `_agent_node(..., streams=)`): the last agent of the last SEQUENTIAL team,
    a HIERARCHICAL manager, and **none at all** for a PARALLEL final team,
    whose output is `_aggregate_node`'s join of several contributions,
    produced with no model call. A HIERARCHICAL manager's *subordinates* never
    stream: a delegate's answer is working material, not the reply.
  - **The callables travel in `_TeamState`, not in a node closure**, because
    `compile()`'s result is cached and reused across runs -- a per-run sink
    baked into a closure would leak into the next run. Plain fields, no
    reducer, same lifecycle as `memory_preamble`.
  - **`_should_stream` refuses to stream a model whose usage would be lost.**
    A model class declaring a `stream_usage` field (ChatOpenAI and family) is
    bound with it so the aggregated chunk still carries `usage_metadata`; a
    langchain fake reports no usage on any path, so it streams freely (which
    is what makes this testable at $0); anything else falls back to plain
    `invoke()`. An unstreamed reply is better than an unmetered one.
  - **Streaming an agent and being able to stop it are different questions.**
    `forward_text` (only the wired agent's text may reach the consumer) is
    separate from `stream_call` (any agent in a run that supplied a cancel
    check consumes its call in chunks and discards the text) -- otherwise an
    earlier agent blocks in `invoke()` and Stop is unresponsive for its whole
    paid turn. Cancellation also travels into a HIERARCHICAL delegation; the
    token sink deliberately does not.
  - **Cancellation adds no terminal path.** `should_cancel` is polled between
    deltas, and again before a tool batch, before each individual call in it,
    and before any new provider request; when it trips, `_run_agent` stops iterating and returns what it
    has, the node finishes early, and the caller's existing between-events
    cancellation handling does the rest. The cost, accepted and documented:
    the provider's usage arrives in a final chunk a cancelled stream never
    reads, so that one call goes unmetered.

## Known limitation: CrewAI adapter, DEBATE mode, deployment templates

CrewAI adapter, DEBATE collaboration mode, and deployment templates are
planned on the roadmap, not started. HIERARCHICAL *is* implemented: the
`manager` agent gets a `delegate_to_<name>(task)` tool per subordinate, bound
alongside its own `tools` and run through the same tool-calling loop as
SEQUENTIAL/PARALLEL agents — see `_hierarchical_node` in
`adapters/langgraph_adapter.py`.

Three paths force `tool_choice="required"` on an agent's **first** call — a
hierarchical manager's, so it delegates rather than answering from its own
guesswork; a tool-carrying subordinate's, so it actually consults the tool it
was delegated to use; and, on every mode, an agent that carries a
knowledge-base tool (`_has_knowledge_base_tool`, keyed on the
`__bestteam_tool_kind__ == "knowledge_base"` marker), so it searches before it
answers. The same turn ends with a `grounding_checked` event from
`core/grounding.py`: the final text's `[source: …]` tags compared with the
citation labels the turn's own searches returned (the tool reports them in
full through `report_trace(citations=…)`; the trace event keeps only the
bounded `sources`). Recorded, never acted on.

That forcing is insurance, not a requirement:
`_first_call` catches a provider that rejects it (DeepSeek's thinking mode
returns `400 Thinking mode does not support this tool_choice`, which arrives at
call time, not at bind time) and retries once on the unforced binding. Without
that fallback a whole hierarchical team was dead on its first call — the
guidance is still in the system prompt, so the worst case is a manager that
answers directly instead of delegating. `core/_structured_output.py` handles
the identical refusal on the structured-output path; the two are the same
provider behaviour met in two places.
