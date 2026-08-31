# bestteam — `src/bestteam/` (SDK core)

`Agent`/`Team`/`Pipeline` and the LangGraph adapter. Root `CLAUDE.md` for the
overview; `core/CLAUDE.md` for Specification/Requirements, knowledge bases and
memory; `tools/CLAUDE.md` for built-in tools.

## Key design decisions

- **State reducer for parallel agents**: `_TeamState.contributions` uses
  `Annotated[Dict[str, str], operator.or_]` so concurrent node writes merge
  instead of raising `InvalidUpdateError`.
- **`fake:<response>` model spec**: a custom convenience in `_resolve_model()` so
  YAML pipelines can declare zero-cost deterministic dry-run models
  (`FakeListChatModel`) with no API keys.
- **`diagnostic` is a run-level switch, not a config**:
  `Pipeline.run/stream(..., diagnostic=True)` travels as `_TeamState.diagnostic`
  (a plain field set once by `_initial_state`, like `memory_preamble`) into
  `_run_agent`, which then also emits `agent_prompt`/`model_turn` and adds
  `args`/`result` to tool events. Default off keeps the event stream
  byte-identical; **the email tools stay redacted either way.**
- ⚠️ **Preserve `BestTeamError` subtypes through adapter boundaries**: always
  `except BestTeamError: raise` *before* a broad `except Exception` in adapter
  code, or `ConfigurationError`/etc. get masked as a generic `EngineError` and the
  real cause is lost.

## Token streaming is a side channel, not an event type

`Pipeline.stream()` / `EngineAdapter.stream()` take optional
`on_token: Callable[[str], None]` and `should_cancel: Callable[[], bool]`.

⚠️ **They have to be a side channel** because `LangGraphAdapter.stream()` is built
on `stream_mode="updates"` and therefore only yields at *node* boundaries —
nothing on that path can reach a subscriber while the reply is still being
written. **Deltas are not TraceEvents and must never be persisted**; the
authoritative answer is still `run_completed`'s data. Both default to None, which
is byte-for-byte today's behaviour. Spec:
`2026-08-23-share-chat-streaming-design.md`.

Five rules in that design are load-bearing:

- **Exactly one agent per pipeline streams** — the one whose text IS the run's
  output. `compile()` decides at wiring time (`streams_final` →
  `_agent_node(..., streams=)`): the last agent of the last SEQUENTIAL team, a
  HIERARCHICAL manager, and **none at all** for a PARALLEL final team, whose
  output is `_aggregate_node`'s join produced with no model call. A HIERARCHICAL
  manager's *subordinates* never stream: a delegate's answer is working material,
  not the reply.
- **The callables travel in `_TeamState`, not in a node closure**, because
  `compile()`'s result is cached and reused across runs — a per-run sink baked
  into a closure would leak into the next run. Plain fields, no reducer, same
  lifecycle as `memory_preamble`.
- **`_should_stream` refuses to stream a model whose usage would be lost.** A
  model class declaring a `stream_usage` field (ChatOpenAI and family) is bound
  with it so the aggregated chunk still carries `usage_metadata`; a langchain fake
  reports no usage on any path, so it streams freely (which is what makes this
  testable at $0); anything else falls back to plain `invoke()`. **An unstreamed
  reply is better than an unmetered one.**
- **Streaming an agent and being able to stop it are different questions.**
  `forward_text` (only the wired agent's text may reach the consumer) is separate
  from `stream_call` (any agent in a run that supplied a cancel check consumes its
  call in chunks and discards the text) — otherwise an earlier agent blocks in
  `invoke()` and Stop is unresponsive for its whole paid turn. Cancellation also
  travels into a HIERARCHICAL delegation; **the token sink deliberately does
  not.**
- **Cancellation adds no terminal path.** `should_cancel` is polled between
  deltas, and again before a tool batch, before each call in it, and before any
  new provider request; when it trips, `_run_agent` stops iterating and returns
  what it has, the node finishes early, and the caller's existing between-events
  handling does the rest. ⚠️ **Accepted cost: the provider's usage arrives in a
  final chunk a cancelled stream never reads, so that one call goes unmetered.**

## Collaboration modes

HIERARCHICAL **is** implemented: the `manager` agent gets a
`delegate_to_<name>(task)` tool per subordinate, bound alongside its own `tools`
and run through the same tool-calling loop as SEQUENTIAL/PARALLEL agents
(`_hierarchical_node`). **CrewAI adapter, DEBATE mode and deployment templates are
planned, not started.**

## Forced `tool_choice` and its fallback

Three paths force `tool_choice="required"` on an agent's **first** call:

1. a hierarchical manager's, so it delegates rather than answering from guesswork;
2. a tool-carrying subordinate's, so it actually consults the tool it was
   delegated to use;
3. on every mode, an agent carrying a knowledge-base tool
   (`_has_knowledge_base_tool`, keyed on the
   `__bestteam_tool_kind__ == "knowledge_base"` marker), so it searches before it
   answers.

That turn ends with a `grounding_checked` event (`core/grounding.py`): the final
text's `[source: …]` tags compared with the citation labels the turn's own
searches returned (the tool reports them in full via
`report_trace(citations=…, citation_documents=…)`; the trace event keeps only
the bounded `sources`). **Both comparisons are set membership over reported
fields — the check never splits a label apart**, because under `refuse` a
misparse is a wrong refusal, not trace noise.
**Recorded — and, only under an opt-in `Agent.grounding_policy`, acted on.**
`observe` (default) keeps the event payload byte-identical to the
pre-policy shape (no `policy`/`retried`/`refused` keys — consumers keyed on
the exact dict rely on that). `retry` makes exactly one corrective call on
the same conversation (the hits are already in the ToolMessages — no new
searches; a retry answering with tool calls counts as failed). `refuse`
retries once, then returns `GROUNDING_REFUSAL_TEXT` — which, on a turn whose
searches found nothing, is every turn: refuse deliberately refuses what it
cannot verify. Streaming: the failing text already reached the viewer, so
`STREAM_RESET` precedes the corrected stream (and a refusal, which is never
streamed — it rides `run_completed`).

**`Agent.grounding_level: claim` (opt-in, orthogonal to the policy)** adds
`grade_claims` after a passing citation check: one plain-invoke LLM call
(deliberately NOT `with_structured_output` — `fake:` can't, reasoning models
400 on its forced `tool_choice`) judging each factual claim against the
turn's KB tool results (`kb_result_texts`). Combined bar = citation check ∧
no unsupported claims; zero claims passes. `grounding_model` overrides the
grader (default: the agent's own model); its usage is metered under the
grader's spec. ⚠️ **Grader failure is fail-soft** — bad spec, invoke error or
unparseable JSON degrades to the citation-level result (`claim_check_error`
in the trace), never a retry/refusal. A claim-level failure's retry
instruction names the unsupported claims (`claim_retry_instruction`). Claim
keys ride `grounding_checked` only at claim level, so the default payload
stays byte-identical. Bounds: one retry, two grader calls per turn.

⚠️ **That forcing is insurance, not a requirement.** `_first_call` catches a
provider that rejects it — DeepSeek's thinking mode returns
`400 Thinking mode does not support this tool_choice`, **which arrives at call
time, not at bind time** — and retries once on the unforced binding. Without that
fallback a whole hierarchical team was dead on its first call. The guidance is
still in the system prompt, so the worst case is a manager that answers directly
instead of delegating. `core/_structured_output.py` handles the identical refusal
on the structured-output path; the two are the same provider behaviour met in two
places.
