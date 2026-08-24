# Grounding-lite: a knowledge-base agent searches first, and its citations are checked against what it found

Date: 2026-08-24. Status: approved design (brainstorming; the user delegated
the design rulings — "start the grounding-lite spec, and then do the
execution" — so each ruling below is recorded with its reason).

## Context

An agent given a knowledge base is *offered* it, not held to it. The tool's
docstring says "use it whenever the question may be answered by these
documents … cite it with that same `[source: …]` tag", but on a SEQUENTIAL or
PARALLEL team nothing makes the model call the tool before answering, and
nothing checks that the `[source: …]` tags in its answer name passages the
search actually returned. Only the HIERARCHICAL path forces a first tool
call (`require_tool_use_on_first_call`, for a manager and for a delegated
subordinate that carries tools). The 2026-08-24 external review listed this
as its second P0: "RAG grounding is not a forced contract".

The full remedy — refusing or regenerating an ungrounded answer, a grader
model, answer-level evaluation — is deferred (see *Out of scope*). This is
the **lite** version, decided on 2026-08-24 after PR #88: two mechanical
guarantees that cost no extra model call.

1. **Search first.** An agent with a knowledge-base tool bound makes its
   first model call with `tool_choice="required"`, on every team mode.
2. **Check the citations.** When the agent's turn ends, every `[source: …]`
   tag in its final text is compared with the citations its own searches
   returned during that turn, and the result is recorded as one trace
   event. Nothing is changed or blocked; the run is annotated.

## Rulings

- **Force only when a knowledge-base tool is bound, not for any tool.**
  A sequential agent with only `web_search` or `calculator` keeps today's
  behaviour. The failure this guards against — "answered from guesswork
  instead of the documents" — is specific to a knowledge base; widening the
  forcing to every tool-carrying agent would change every existing team's
  first turn for no stated benefit. Cost if wrong: one more condition to
  drop later.
- **`tool_choice="required"`, not "call *this* tool".** Same forcing as the
  hierarchical paths, so the existing `_first_call` refusal fallback (a
  provider that 400s on a forced `tool_choice`, DeepSeek's thinking mode)
  covers it unchanged. An agent with a knowledge base *and* other tools may
  satisfy the forcing with a different tool; the grounding event then says
  `searches: 0`. Naming a specific tool has per-provider semantics and would
  need its own fallback; not worth it for the lite version.
- **The check lives in the adapter, not the backend.** `_run_agent` already
  holds the agent's final text and every tool call's `ToolCallContext`; a
  pure function in `core/grounding.py` does the comparison and the adapter
  emits the event. SDK and CLI users get the check too, and the backend
  needs no per-agent bookkeeping. It rides `_run_agent`, so a delegated
  subordinate with a knowledge base is checked on the hierarchical path as
  well; a manager without a knowledge base is not (its text is the
  subordinate's, re-told).
- **The event records; it does not act.** No retry, no refusal, no change
  to the answer, no `needs_attention`. "Lite" means an operator can see
  which runs cited passages that were never retrieved; what to do about it
  is the next decision, and it needs data first.
- **Verification rule.** A tag is *verified* when its label equals a
  returned citation exactly (after whitespace normalisation), or when the
  tag names only a filename and that filename is the document of some
  returned citation. A tag with a page or heading that matches no returned
  citation is *unverified* — a fabricated locator is exactly what this is
  for. Comparison is case-sensitive: filenames are.
- **No read surface beyond the trace.** The event renders in the technical
  trace on the admin/monitor pages through the existing generic path
  (`EVENT_LABELS[type] ?? type`); one label and one `renderEventData` case
  make it legible. No run-row column, no filter, no chart. The share page's
  visitor sees only a status word for any event type and this one falls to
  its default.
- **No new configuration.** Not a per-agent switch, not an env var. The
  forcing already has its fallback and the check costs nothing.

## 1. Search first (adapter)

`_agent_node` (SEQUENTIAL/PARALLEL members) passes
`require_tool_use_on_first_call=_has_knowledge_base_tool(agent)` to
`_run_agent`, where

```python
def _has_knowledge_base_tool(agent: Agent) -> bool:
    return any(getattr(fn, "__bestteam_tool_kind__", None) == "knowledge_base" for fn in agent.tools)
```

The marker is the one `make_knowledge_base_tool` already sets and the tool
loop already dispatches on. `_make_delegate_tool` keeps forcing on
`bool(agent.tools)` (unchanged). The manager keeps `True` (unchanged).
`_first_call`'s refusal fallback applies as-is.

Streaming is unaffected: the forced first call streams exactly as it does on
the hierarchical path today.

## 2. The check (`core/grounding.py`)

A new module with no engine dependency:

```python
CITATION_TAG = re.compile(r"\[source:\s*([^\]]+?)\s*\]")
MAX_UNVERIFIED = 10
MAX_LABEL_CHARS = 200

@dataclass(frozen=True)
class GroundingResult:
    searches: int          # knowledge-base tool calls that completed this turn
    hit_count: int         # passages those searches returned, summed
    cited: int             # distinct [source: …] labels in the final text
    verified: int          # of those, labels the searches returned
    unverified: List[str]  # the rest, <= MAX_UNVERIFIED, each <= MAX_LABEL_CHARS

    def as_trace_data(self) -> Dict[str, Any]: ...

def check_grounding(text: str, citations: Sequence[str], *, searches: int, hit_count: int) -> GroundingResult
```

- `citations` is every citation label the agent's knowledge-base searches
  returned this turn, in full — **not** the ≤10 `sources` the trace keeps.
  The tool reports it on a new `report_trace(citations=[...])` field that
  `_kb_tool_trace_data` does not copy into the event, so the trace event
  is unchanged and a `top_k` above 10 does not produce false unverified
  tags.
- Labels are normalised by collapsing internal whitespace and stripping the
  ends; the same normalisation applies to both sides.
- *Filename* of a label is the text before the first `, p.` or ` § `; a
  label is "filename only" when neither marker occurs.
- `cited` counts distinct normalised labels; a label cited three times is
  one entry.
- `unverified` is ordered by first appearance, capped at `MAX_UNVERIFIED =
  10`, each truncated to `MAX_LABEL_CHARS = 200` — the label is
  model-written text and gets the same bound as the query.
- `check_grounding("", [], searches=0, hit_count=0)` and an answer with no
  tags are valid: `cited = 0`, `verified = 0`, `unverified = []`.

## 3. The event (adapter)

In `_run_agent`, after the loop and before returning the final text, when
`_has_knowledge_base_tool(agent)` is true and the turn ended with text
(not a cancellation return, not the exhausted-loop notice — those return
early as today), emit:

```
grounding_checked  agent=<name>  data={
  "searches": 1, "hit_count": 3,
  "cited": 2, "verified": 1,
  "unverified": ["handbook.pdf, p.99"]
}
```

The adapter accumulates, per turn, the `citations` and `hit_count` reported
by each successful knowledge-base tool call (`tool_ctx.trace`), and counts
those calls as `searches`. A failed knowledge-base call (exception) counts
for nothing. The event is emitted on every path through `_run_agent` that
returns text — sequential, parallel, and a hierarchical subordinate — and
never for an agent without a knowledge-base tool. Position: after the last
`tool_completed`/`agent_progress` of the turn, before the node's
`agent_completed` (the adapter's per-node buffer already orders it so).

Business-safety: the event carries counts and citation labels. A label is a
filename plus page/heading — the same strings `sources` already records —
or, when unverified, model-written text bounded to 200 characters. No chunk
text, no query, no model name.

`core/trace.py`'s docstring gains the type. Diagnostic runs add nothing to
it.

## 4. Backend

Nothing to change in the runtime: `_safe_record_trace_event` persists every
event type generically, and the WebSocket publishes it. The share path
(`share_chat.py`) forwards only the type for non-terminal events, so a
visitor sees the default status word.

## 5. Frontend

`ui/frontend/src/lib/traceEvents.ts`:

- `EVENT_LABELS.grounding_checked = '📎 grounding checked'`
- `renderEventData` case: `1 search · 3 passages · 2 cited · 1 verified —
  unverified: handbook.pdf, p.99` (the `— unverified:` part only when the
  list is non-empty; `1 search` / `2 searches` pluralised).

Not added to `FRIENDLY_EVENT_TYPES` — a customer's friendly view filters it
out like every other intermediate event. English-only, like the other
technical labels.

## 6. Documentation

- `docs/KNOWLEDGE_BASES.md` "How agents use a knowledge base": the forced
  first call and the grounding check, with the event example.
- `src/bestteam/CLAUDE.md`: the forcing paragraph now covers the
  knowledge-base case on every mode; `core/grounding.py` noted.
- `CLAUDE.md` known-limitations bullet on knowledge bases: one sentence on
  what grounding-lite does and does not do.
- `docs/STATUS.md` Done entry; `CHANGELOG.md` `[Unreleased]` → Added.

## 7. Testing

- `tests/test_grounding.py` (unit): exact match; whitespace-normalised
  match; filename-only tag verified against a paged citation; paged tag
  with an unreturned page is unverified; heading tags; duplicates count
  once; cap at 10 and 200 chars; empty text; no citations returned with
  tags present → all unverified; `as_trace_data()` shape.
- `tests/test_trace_granularity.py` (unit, `_FakeToolCallingChatModel`):
  a sequential agent with a stub knowledge-base tool (a function carrying
  `__bestteam_tool_kind__ = "knowledge_base"` that calls `report_trace`)
  emits `grounding_checked` after `tool_completed` and before
  `agent_completed`, with the expected counts; an agent with an ordinary
  tool emits none; a knowledge-base agent whose model never calls the tool
  gets `searches: 0` and every tag unverified; the `tool_completed` event
  does not carry `citations`.
- Same file, `test_hierarchical_team.py` style: the sequential agent's
  first `bind_tools` sees `tool_choice="required"` when a knowledge-base
  tool is bound, and never sees a forced `tool_choice` for an ordinary
  tool.
- `tests/test_knowledge_base.py`: `make_knowledge_base_tool` reports
  `citations` in full for a `top_k` above `_MAX_TRACE_SOURCES` while
  `sources` stays capped.
- Frontend: `renderEventData` and `EVENT_LABELS` for the new event, in a
  new `traceEvents.test.ts` beside the module.

## Out of scope

- Regenerating, refusing or flagging an answer on an unverified citation.
- A grader model or any answer-level evaluation (the golden set measures
  retrieval only; see `docs/KNOWLEDGE_BASES.md` "What it does not measure").
- Forcing the *specific* knowledge-base tool, or forcing on later calls.
- A per-run grounding column, a runs-list filter, or a dashboard.
- Checking a manager's text against its subordinates' searches.
- Verifying that a cited passage *supports* the claim — this checks that
  the passage was retrieved, nothing more.
