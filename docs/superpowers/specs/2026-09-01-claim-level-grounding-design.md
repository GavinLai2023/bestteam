# Claim-level grounding (per-turn) — design

**Date**: 2026-09-01
**Status**: approved (brainstormed with the user; five rulings below)
**Predecessor**: `2026-08-24` grounding-lite (PR #89) — citation-set checking +
`observe|retry|refuse` policies.

## Problem

Grounding-lite's pass bar is *"at least one citation, every citation returned
by this turn's searches"*. It cannot tell whether the cited passages actually
support the answer's conclusions: an answer with five business claims and one
real-but-irrelevant citation at the end passes. For high-risk / compliance
knowledge QA this is the platform's top gap (Aug-31 assessment: the only
P0/P1).

## User rulings (2026-09-01)

1. **Mechanism**: LLM grader — one extra model call that both splits the
   answer into claims and judges each against the evidence. Not NLI, not
   hybrid.
2. **Layer**: per-turn, at the existing `grounding_checked` site in
   `_run_agent`. Pipeline-final-output checking is deferred.
3. **Failure semantics**: `retry` rewrites *removing or re-grounding* the
   unsupported claims (named in the instruction); `refuse` refuses whole only
   after the rewritten answer still fails. `observe` records only.
4. **Grader model**: defaults to the agent's own model; optional
   `grounding_model` override (e.g. a cheaper model as grader).
5. **Activation**: new orthogonal field `Agent.grounding_level:
   "citation" | "claim"`, default `"citation"` — existing configs are
   byte-for-byte unchanged. `grounding_policy` keeps deciding what happens on
   failure; `grounding_level` decides how deep the check goes.

## Design

### Configuration (`core/agent.py`, loader, `core/specification.py`)

- `Agent.grounding_level: str = "citation"`, validated in `__post_init__`
  against `GROUNDING_LEVELS = ("citation", "claim")` (same shape as
  `grounding_policy`).
- `Agent.grounding_model: ModelSpec | None = None` — used only when
  `grounding_level == "claim"`; `None` means the agent's own model. Resolved
  through the adapter's `_resolve_model`, so `fake:` works. No validation
  beyond what `_resolve_model` does (same as `Agent.model`).
- Both are **inert for an agent without a knowledge-base tool** — the same
  documented semantics `grounding_policy` already has. (Deviation from the
  presented design, which proposed a `ConfigurationError`: consistency with
  the existing field wins, and the check is per-adapter knowledge.)
- Loader: nothing to do — `_build_agent` passes the spec dict to
  `Agent(**spec)`, so the YAML keys flow through and unknown-value errors come
  from `Agent.__post_init__` as `ConfigurationError`.
- `AgentSpec` (`core/specification.py`): add `grounding_level: Optional[str]`
  and `grounding_model: Optional[str]`, emitted by `to_raw()` only when set —
  mirroring `grounding_policy`. Not exposed in the Team Builder wizard UI
  (v1 is a YAML/admin-level feature, like `grounding_policy` today).

### Grader (`core/grounding.py`)

New pure function + result type:

```python
@dataclass(frozen=True)
class ClaimGrading:
    claims: int              # factual claims the grader extracted
    supported: int           # of those, judged supported by the evidence
    unsupported: List[str]   # the rest, bounded (MAX_UNVERIFIED × MAX_LABEL_CHARS)

    @property
    def passes(self) -> bool:  # no unsupported claims (zero claims passes)
        ...

def grade_claims(text, evidence, model) -> Tuple[Optional[ClaimGrading], Any]
```

The second element is the raw model response (or `None` when the invoke
itself raised) — returned so the caller can meter a billed-but-unparseable
call, exactly `expand_query`'s shape.

- **Not** `with_structured_output`: `fake:` models don't support it (would
  make the feature untestable at $0) and DeepSeek-style thinking models 400 on
  the forced `tool_choice` behind `function_calling`. Instead: one plain
  `model.invoke()` on a prompt carrying the answer and the evidence, asking
  for a single JSON object `{"claims": [{"text": ..., "supported": true},
  ...]}` — the same plain-invoke-and-parse shape as
  `query_expansion.expand_query`. Parsing is tolerant (strips code fences,
  finds the outermost JSON object); malformed entries are skipped; the claim
  list is capped at 20.
- **Zero claims passes.** An honest "the knowledge base does not contain the
  answer" has no factual claims to support and must not be refused.
- The grader prompt instructs: extract only factual/business assertions
  (numbers, dates, policies, conditions); quote each claim's text from the
  answer verbatim; judge `supported` strictly against the evidence text only.
- **`None` = the check was not performed** (invoke raised, or the response was
  unparseable). The caller falls back to citation-level — a grader failure is
  never a reason to retry or refuse (the query-expansion / rerank fail-soft
  precedent). The function never raises.

### Adapter flow (`langgraph_adapter._run_agent`)

Alongside `kb_citations`/`kb_documents`, collect `kb_result_texts:
List[str]` — the `str(result)` of each successful KB tool call (the same text
that becomes the ToolMessage). That is the grader's evidence; zero extra
collection cost.

At the existing grounding site:

1. Run `check_grounding` (unchanged).
2. If `grounding_level == "claim"` **and** the citation check passed, call
   `grade_claims(text, kb_result_texts, grader_model)`. The combined bar:
   `passes = result.passes and (grading is None or grading.passes)`.
   (Citation check failed → the grader is not called; the citation failure
   already triggers the policy and its retry instruction covers rewriting.)
3. On failure under `retry`/`refuse`: one corrective call, as today. When the
   failure includes unsupported claims, the retry instruction is
   `GROUNDING_RETRY_INSTRUCTION` plus a rendered list of the unsupported
   claims and the instruction to delete them or rewrite them to match the
   search results, keeping the rest of the answer.
4. The retried answer goes through the same combined bar (citation check, then
   grader when level is `claim` and citations pass). `refuse` returns
   `GROUNDING_REFUSAL_TEXT` only when the retried answer still fails.
5. Streaming: the existing `STREAM_RESET` machinery is reused unchanged; no
   new terminal path.

Bounds per turn: at most one retry (unchanged), at most two grader calls.

### Metering

The grader call's `usage_metadata` is appended to `usage_sink` tagged with the
grader's model spec (the override's spec when set, else the agent's) — the
same `{model, input_tokens, output_tokens}` entry shape as `_record_usage`.
Captured even when the response is unparseable (the call was billed). `fake:`
models report no usage, as everywhere else. No customer-facing surface shows
the grader model or its cost (existing ruling).

### Trace (`grounding_checked`)

- `grounding_level: "citation"` (default): payload byte-identical to today —
  the `observe` invariant holds.
- `grounding_level: "claim"`: `data` additionally carries `level: "claim"`
  and, when the grader ran, `claims`, `claims_supported`,
  `unsupported_claims` (bounded list, `MAX_UNVERIFIED` entries of
  `MAX_LABEL_CHARS`); when the grader failed, `claim_check_error: true`
  instead. When the citation check failed and the grader never ran, the claim
  keys are simply absent.

### Testing (all `fake:`, $0, deterministic)

New test file (with `pytestmark`) covering:

- `grade_claims` unit tests: parse a canned JSON response; fenced JSON; claims
  cap; malformed entries skipped; unparseable → `None`; invoke error → `None`;
  zero claims passes.
- Adapter integration via `fake:` agent + `fake:` grader (canned JSON as the
  fake's scripted response): all-supported passes; unsupported claim +
  `retry` triggers the extended instruction and a rewrite; still-failing +
  `refuse` returns the refusal text; grader failure falls back to
  citation-level (no retry); `observe` + claim level records only.
- Trace shape: citation-level payload byte-identical; claim-level keys as
  specified.
- Metering: grader usage lands in the usage sink with the right model tag.
- Config: `Agent` field validation; YAML round-trip through the loader;
  `AgentSpec.to_raw()` emission.

The scripted-responses detail: `fake:` resolves to `FakeListChatModel`, whose
responses cycle; the agent's own calls and the grader's calls draw from
*different* model instances (the grader resolves its own), so a test can
script each side independently — the grader model is scripted with grading
JSON, the agent model with the answer (and its rewrite).

### Documentation

- `src/bestteam/CLAUDE.md` — extend the grounding paragraph (level field,
  grader, combined bar; keep it short).
- `docs/KNOWLEDGE_BASES.md` — the full reference.
- Root `CLAUDE.md` known-limitations bullet: grounding is now *citation-level
  by default, claim-level opt-in per agent* — still not entailment-verified
  evidence spans.
- `docs/STATUS.md` — move the item.

## Explicitly out of scope (YAGNI)

- Pipeline final-output-layer checking (multi-agent aggregation drift).
- NLI / local entailment models.
- Team Builder wizard UI exposure.
- Dedicated numeric/date/range consistency checkers.
- Claim-to-evidence span alignment beyond the grader's judgment.
- Answer-level live eval metrics (belongs to the eval-gate direction).

## Risks

- **Grader quality is model-dependent.** A weak grader model produces false
  unsupported verdicts; under `refuse` that means wrong refusals. Mitigation:
  default is the agent's own model, `observe` + `claim` lets an operator
  watch the false-positive rate before enabling `retry`/`refuse`; the
  `unsupported_claims` trace list makes misjudgments auditable.
- **Cost**: one extra model call per KB-agent turn (two on retry), carrying
  the evidence text as input tokens. Opt-in per agent, so nothing changes
  until a customer asks for it.
