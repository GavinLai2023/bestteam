# Grounding policy: observe | retry | refuse

**Date:** 2026-08-26
**Status:** approved (direction approved by the user 2026-08-26; detail
decisions delegated)

## Problem

Grounding-lite (`core/grounding.py`, spec `2026-08-24-grounding-lite-design.md`)
records whether a knowledge-base agent's `[source: …]` tags match what its own
searches returned — and deliberately never acts on the result. For ordinary
internal assistants that is the right trade. For high-risk answers (prices,
policy, compliance, contracts) a customer needs the platform to *do* something
when an answer is ungrounded, not just log it.

## Decision

A per-agent, opt-in `grounding_policy` with three values. The default,
`observe`, is byte-for-byte today's behaviour — the "checked, never enforced"
ruling stays the default and this feature is the documented, opt-in extension
of it.

| Policy | On a failed check |
|---|---|
| `observe` (default) | Record only — exactly today. |
| `retry` | One corrective model call, then return the retried answer even if it still fails. |
| `refuse` | Same single corrective call; if the retried answer still fails, return a fixed refusal text instead of the answer. |

**The bar** (an answer *passes* when): `cited > 0` **and** every cited label
verified (`unverified` empty). Anything else — no tags at all, or any
fabricated tag — fails. This includes the zero-hit turn: with `refuse`, an
agent whose searches found nothing cannot produce a passing answer and the
customer gets the refusal text, which is precisely the wanted behaviour for a
high-risk agent. (A grader that could tell an honest "the KB doesn't cover
this" from a fabricated confident answer stays deferred, as in the
grounding-lite spec.)

**The retry** is one additional model call on the same conversation — the
search results are already in the turn's `ToolMessage`s, so the model needs no
new searches, only a rewrite instruction (`GROUNDING_RETRY_INSTRUCTION`,
appended as a `HumanMessage`): cite only returned sources, or say the
knowledge base does not contain the answer. A retry response that asks for
more tools instead of answering counts as a failed retry (no second loop —
one retry, bounded). The retry call is metered (`_record_usage`) and respects
cancellation (`_call` already checks).

**The refusal text** is a fixed constant (`GROUNDING_REFUSAL_TEXT` in
`core/grounding.py`) so a refused answer is deterministic and recognisable
downstream.

## Where it lives

- `Agent.grounding_policy: str = "observe"` — validated in `__post_init__`
  against `GROUNDING_POLICIES`, so the loader's `Agent(**spec)` pass-through
  validates YAML for free (`ConfigurationError` naming the valid values).
  Inert for an agent without a knowledge-base tool, like the forcing itself.
- Enforcement in `adapters/langgraph_adapter.py::_run_agent`, at the existing
  grounding block — the one place all agent paths (SEQUENTIAL/PARALLEL
  members, HIERARCHICAL managers and subordinates) share.
- `AgentSpec.grounding_policy: Optional[str]` in `core/specification.py`, kept
  by `to_raw()` when set — a loader-level field like `skills`, not a
  wizard-only presentation field. The wizard does not set it; round-tripping
  an existing pipeline must not drop it.

## Trace event

`grounding_checked` keeps today's five fields. With `observe` the payload is
**byte-identical to today** (no new keys — dashboards and tests keyed on the
exact dict keep passing). With `retry`/`refuse` the event describes the
**final** answer's check and adds: `policy`, `retried` (bool), `refused`
(bool). One event per turn, as today.

## Streaming

A streaming agent has already forwarded the failing answer's text live. On
retry, `STREAM_RESET` is sent to the token sink before the corrective call
(the consumer discards the shown text; the retry streams fresh). On refusal,
`STREAM_RESET` is sent and the refusal text is returned unstreamed — the
authoritative `run_completed` carries it, per the streaming contract.

## Out of scope (deliberate)

- Claim-level entailment / grader models (deferred in grounding-lite; still
  deferred).
- A pipeline-level global grounding pass over the final output.
- Naming a *specific* KB the agent must use (`required` still forces "some
  tool"; the DeepSeek fallback keeps working — a model that refuses forced
  tool_choice degrades to unforced, unchanged).
- More than one retry, retry budgets, or per-policy configuration knobs.

## Tests

Loader/Agent validation; the pass/fail bar (`GroundingResult.passes`);
adapter behaviour for each policy via the existing `_FakeToolCallingChatModel`
+ `_stub_knowledge_base_tool` scaffolding (observe unchanged and payload
byte-identical, retry success, retry still-failing, refuse success passthrough,
refuse fallthrough to refusal text, corrective message content, STREAM_RESET
on the streaming path); `AgentSpec` round-trip.
