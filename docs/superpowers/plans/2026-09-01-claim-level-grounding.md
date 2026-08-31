# Claim-level Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opt-in per-agent claim-level grounding: an LLM grader splits a KB agent's answer into factual claims and judges each against the turn's own search results; `retry` rewrites removing unsupported claims, `refuse` refuses only after the rewrite still fails.

**Architecture:** A new pure grader (`grade_claims`) in `core/grounding.py` (plain invoke + tolerant JSON parse — NOT `with_structured_output`, which `fake:` models lack and DeepSeek-style models 400 on). The adapter's existing grounding site in `_run_agent` collects the KB tool results' text as evidence, calls the grader when `Agent.grounding_level == "claim"` and the citation check passed, and feeds a claim-specific retry instruction into the existing retry/refuse/STREAM_RESET machinery. Two new `Agent` fields; default behaviour byte-identical.

**Tech Stack:** Python (stdlib json/dataclasses), LangChain fakes for $0 tests, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-claim-level-grounding-design.md`

## Global Constraints

- Default configs must stay byte-for-byte unchanged: `grounding_level: "citation"` (default) emits exactly today's `grounding_checked` payload.
- The grader never raises and never blocks: any grader failure falls back to citation-level (fail-soft, like query expansion / rerank).
- A billed-but-unparseable grader call is still metered (mirror `expand_query`'s `(result, response)` shape).
- Code comments in English. British spelling in prose docs.
- Every new test file needs a `pytestmark` (this plan only touches existing test files, which have one).
- Run tests through `./.venv/Scripts/python.exe -m pytest`.

---

### Task 1: `ClaimGrading` + `grade_claims` in `core/grounding.py`

**Files:**
- Modify: `src/bestteam/core/grounding.py`
- Test: `tests/test_grounding.py`

**Interfaces:**
- Consumes: `fusion._parse_expansion(content) -> Optional[dict]` (existing tolerant JSON extractor; shared the way `_MARKDOWN_HEADING_RE` is shared from `file_parser`).
- Produces (used by Task 3):
  - `GROUNDING_LEVELS = ("citation", "claim")`
  - `MAX_CLAIMS = 20`
  - `@dataclass(frozen=True) ClaimGrading(claims: int, supported: int, unsupported: List[str])` with property `passes: bool` (True iff `unsupported` is empty — zero claims passes)
  - `grade_claims(text: str, evidence: Sequence[str], model: Any) -> Tuple[Optional[ClaimGrading], Optional[Any]]` — `(grading, raw_response)`; `(None, None)` when the invoke raised, `(None, response)` when it returned but nothing parsed.
  - `claim_retry_instruction(unsupported: Sequence[str]) -> str` — `GROUNDING_RETRY_INSTRUCTION` plus the unsupported-claims list and the delete-or-reground instruction.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_grounding.py`:

```python
# ---------------------------------------------------------------------------
# Claim-level grading (grade_claims): one plain LLM call splits the answer
# into factual claims and judges each against the turn's search results.
# ---------------------------------------------------------------------------

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from bestteam.core.grounding import (
    MAX_CLAIMS,
    ClaimGrading,
    claim_retry_instruction,
    grade_claims,
    GROUNDING_RETRY_INSTRUCTION,
)


def _grader(response_text):
    return FakeListChatModel(responses=[response_text])


_EVIDENCE = ["[source: handbook.pdf, p.3 § Refunds]\nRefunds are processed within 14 days."]


def test_grade_claims_parses_a_clean_json_response():
    grading, response = grade_claims(
        "Refunds take 14 days.",
        _EVIDENCE,
        _grader('{"claims": [{"text": "Refunds take 14 days.", "supported": true}]}'),
    )
    assert grading == ClaimGrading(claims=1, supported=1, unsupported=[])
    assert grading.passes is True
    assert response is not None


def test_grade_claims_reports_unsupported_claims():
    grading, _ = grade_claims(
        "Refunds take 14 days. Shipping is free.",
        _EVIDENCE,
        _grader(
            '{"claims": [{"text": "Refunds take 14 days.", "supported": true},'
            ' {"text": "Shipping is free.", "supported": false}]}'
        ),
    )
    assert grading.claims == 2
    assert grading.supported == 1
    assert grading.unsupported == ["Shipping is free."]
    assert grading.passes is False


def test_grade_claims_tolerates_code_fences_and_prose():
    grading, _ = grade_claims(
        "Answer.",
        _EVIDENCE,
        _grader('Here you go:\n```json\n{"claims": []}\n```'),
    )
    assert grading == ClaimGrading(claims=0, supported=0, unsupported=[])
    assert grading.passes is True, "an answer with no factual claims passes"


def test_grade_claims_skips_malformed_entries():
    grading, _ = grade_claims(
        "Answer.",
        _EVIDENCE,
        _grader(
            '{"claims": ["not a dict", {"supported": true}, {"text": "", "supported": false},'
            ' {"text": "Real claim.", "supported": "yes"}, {"text": "Good.", "supported": true}]}'
        ),
    )
    assert grading == ClaimGrading(claims=1, supported=1, unsupported=[])


def test_grade_claims_caps_the_claim_list():
    entries = ", ".join(f'{{"text": "c{i}", "supported": false}}' for i in range(30))
    grading, _ = grade_claims("Answer.", _EVIDENCE, _grader(f'{{"claims": [{entries}]}}'))
    assert grading.claims == MAX_CLAIMS == 20
    assert len(grading.unsupported) == 10, "unsupported reuses the MAX_UNVERIFIED bound"


def test_grade_claims_truncates_long_claim_texts():
    long_claim = "x" * 500
    grading, _ = grade_claims(
        "Answer.",
        _EVIDENCE,
        _grader(f'{{"claims": [{{"text": "{long_claim}", "supported": false}}]}}'),
    )
    assert grading.unsupported == ["x" * 200]


def test_grade_claims_unparseable_response_returns_none_with_the_response():
    grading, response = grade_claims("Answer.", _EVIDENCE, _grader("I cannot answer that."))
    assert grading is None
    assert response is not None, "the call was billed, so the caller must be able to meter it"


def test_grade_claims_invoke_error_returns_none_none():
    class _Boom:
        def invoke(self, messages):
            raise RuntimeError("provider down")

    grading, response = grade_claims("Answer.", _EVIDENCE, _Boom())
    assert grading is None
    assert response is None


def test_grade_claims_non_string_text_is_treated_as_empty():
    grading, _ = grade_claims(
        [{"type": "text", "text": "blocks"}],
        _EVIDENCE,
        _grader('{"claims": []}'),
    )
    assert grading == ClaimGrading(claims=0, supported=0, unsupported=[])


def test_claim_retry_instruction_names_the_unsupported_claims():
    instruction = claim_retry_instruction(["Shipping is free.", "Returns cost nothing."])
    assert instruction.startswith(GROUNDING_RETRY_INSTRUCTION)
    assert "Shipping is free." in instruction
    assert "Returns cost nothing." in instruction
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_grounding.py -v -k "grade_claims or claim_retry"`
Expected: FAIL with `ImportError` (cannot import `grade_claims` etc.)

- [ ] **Step 3: Implement** — append to `src/bestteam/core/grounding.py` (and extend its imports: `json` is not needed — parsing is delegated; add `Optional, Tuple` to the typing import):

```python
#: How deep the grounding check goes (`Agent.grounding_level`). `citation`
#: is the pre-existing set-membership check; `claim` additionally has an LLM
#: grader split the answer into factual claims and judge each against the
#: turn's own search results.
GROUNDING_LEVELS = ("citation", "claim")

#: Bound on how many grader-reported claims are read; anything past it is
#: model output nobody asked for, not evidence.
MAX_CLAIMS = 20

_CLAIM_GRADER_SYSTEM_PROMPT = (
    "You are a strict fact-checking grader. Extract every factual or business "
    "assertion from the ANSWER (numbers, dates, durations, policies, "
    "conditions, capabilities stated as fact), quoting each claim's text from "
    "the answer. Judge each claim ONLY against the EVIDENCE text: supported "
    "means the evidence states it or directly implies it. Respond with ONLY a "
    'JSON object of the form {"claims": [{"text": "...", "supported": true}]}. '
    "Use an empty list if the answer makes no factual claims (for example, it "
    "only says the knowledge base has no answer). No prose outside the JSON."
)


def claim_retry_instruction(unsupported: Sequence[str]) -> str:
    """The corrective instruction for a turn that failed at claim level: the
    citation instruction plus the grader-named claims to delete or re-ground.
    The rest of the answer is explicitly kept -- the customer gets the
    verifiable part, not an empty hand."""
    claims = "\n".join(f"- {claim}" for claim in unsupported)
    return (
        f"{GROUNDING_RETRY_INSTRUCTION}\n\n"
        "These statements in your previous answer are NOT supported by the "
        f"search results:\n{claims}\n"
        "Remove each of them, or rewrite it to state only what the search "
        "results support. Keep the rest of the answer."
    )


@dataclass(frozen=True)
class ClaimGrading:
    """What the grader made of one answer against one turn's evidence."""

    #: Factual claims the grader extracted (bounded by MAX_CLAIMS).
    claims: int
    #: Of those, judged supported by the evidence.
    supported: int
    #: The rest, bounded like `GroundingResult.unverified`.
    unsupported: List[str]

    @property
    def passes(self) -> bool:
        """No unsupported claims. Zero claims passes: an honest "the knowledge
        base does not contain the answer" has nothing to support and must not
        be refused."""
        return not self.unsupported


def grade_claims(
    text: str, evidence: Sequence[str], model: Any
) -> "Tuple[Optional[ClaimGrading], Optional[Any]]":
    """One LLM call that splits `text` into factual claims and judges each
    against `evidence` (the turn's knowledge-base tool results, verbatim).

    Returns `(grading, raw_response)`. NEVER raises: an invoke failure returns
    `(None, None)`; a response that parses to nothing usable returns
    `(None, response)` -- the call was billed either way, so the caller can
    still meter it (`expand_query`'s shape). `None` grading means the check
    was not performed and the caller falls back to citation-level -- a grader
    failure is never a reason to retry or refuse.

    Deliberately NOT `with_structured_output`: `fake:` models don't support
    it (which would make this untestable at $0), and reasoning-mode models
    reject the forced `tool_choice` behind `function_calling` -- a plain
    invoke plus tolerant JSON parsing sidesteps both, exactly like
    `fusion.expand_query`.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # Shared deliberately, like `_MARKDOWN_HEADING_RE` from `file_parser`:
    # one definition of "tolerant JSON reply parsing", not two drifting ones.
    from .fusion import _parse_expansion

    haystack = text if isinstance(text, str) else ""
    evidence_block = "\n\n---\n\n".join(evidence) if evidence else "(no search results)"
    try:
        response = model.invoke(
            [
                SystemMessage(content=_CLAIM_GRADER_SYSTEM_PROMPT),
                HumanMessage(content=f"EVIDENCE:\n{evidence_block}\n\nANSWER:\n{haystack}"),
            ]
        )
    except Exception:  # noqa: BLE001 -- no call succeeded, nothing billable
        _logger.warning("Claim grading call failed; falling back to citation-level", exc_info=True)
        return None, None

    content = response.content if hasattr(response, "content") else str(response)
    parsed = _parse_expansion(content if isinstance(content, str) else "")
    raw_claims = parsed.get("claims") if parsed else None
    if not isinstance(raw_claims, list):
        _logger.warning("Claim grading response had no usable 'claims' list; falling back to citation-level")
        return None, response

    claims = 0
    supported = 0
    unsupported: List[str] = []
    for entry in raw_claims:
        if claims >= MAX_CLAIMS:
            break
        if not isinstance(entry, dict):
            continue
        claim_text = entry.get("text")
        verdict = entry.get("supported")
        if not isinstance(claim_text, str) or not claim_text.strip() or not isinstance(verdict, bool):
            continue
        claims += 1
        if verdict:
            supported += 1
        else:
            unsupported.append(" ".join(claim_text.split())[:MAX_LABEL_CHARS])
    return ClaimGrading(claims=claims, supported=supported, unsupported=unsupported[:MAX_UNVERIFIED]), response
```

Also add at module top (near the imports): `import logging` and `_logger = logging.getLogger(__name__)` (the module currently has no logger), and extend the `typing` import with `Optional, Tuple`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_grounding.py -v`
Expected: ALL PASS (new and pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/grounding.py tests/test_grounding.py
git commit -m "feat(grounding): claim grader -- grade_claims + ClaimGrading"
```

---

### Task 2: `Agent.grounding_level` / `Agent.grounding_model` + `AgentSpec` round-trip

**Files:**
- Modify: `src/bestteam/core/agent.py`
- Modify: `src/bestteam/core/specification.py` (mirror the `grounding_policy` lines at `specification.py:96` and `:108-109`)
- Test: `tests/test_agent.py`, `tests/test_grounding.py`, `tests/test_specification.py`

**Interfaces:**
- Consumes: `GROUNDING_LEVELS` from Task 1.
- Produces (used by Task 3): `Agent.grounding_level: str = "citation"`, `Agent.grounding_model: ModelSpec | None = None`. YAML keys `grounding_level:` / `grounding_model:` flow through `_build_agent`'s `Agent(**spec)` untouched — no loader change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py`:

```python
def test_agent_grounding_level_defaults_to_citation():
    agent = Agent(name="bot", role="Helper", goal="do things")
    assert agent.grounding_level == "citation"
    assert agent.grounding_model is None


def test_agent_rejects_an_unknown_grounding_level():
    with pytest.raises(ConfigurationError) as exc:
        Agent(name="bot", role="Helper", goal="do things", grounding_level="entailment")
    message = str(exc.value)
    assert "grounding_level" in message
    assert "citation" in message and "claim" in message
```

Append to `tests/test_grounding.py` (uses the existing `_pipeline_yaml` helper):

```python
def test_yaml_grounding_level_and_model_reach_the_agent(tmp_path):
    from bestteam.core.loader import load_pipeline

    path = tmp_path / "p.yaml"
    path.write_text(
        _pipeline_yaml('grounding_level: claim\n    grounding_model: "fake:ok"'),
        encoding="utf-8",
    )

    agent = load_pipeline(path).steps[0].agents[0]
    assert agent.grounding_level == "claim"
    assert agent.grounding_model == "fake:ok"


def test_yaml_unknown_grounding_level_is_a_configuration_error(tmp_path):
    from bestteam.core.loader import load_pipeline
    from bestteam.exceptions import ConfigurationError

    path = tmp_path / "p.yaml"
    path.write_text(_pipeline_yaml("grounding_level: strict"), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="grounding_level"):
        load_pipeline(path)
```

Append to `tests/test_specification.py` (match its local style for constructing an `AgentSpec`; the assertions that matter):

```python
def test_agent_spec_round_trips_grounding_level_and_model():
    from bestteam.core.specification import AgentSpec

    spec = AgentSpec(
        name="a",
        role="Helper",
        goal="Answer",
        grounding_level="claim",
        grounding_model="fake:ok",
    )
    raw = spec.to_raw()
    assert raw["grounding_level"] == "claim"
    assert raw["grounding_model"] == "fake:ok"

    default = AgentSpec(name="a", role="Helper", goal="Answer").to_raw()
    assert "grounding_level" not in default
    assert "grounding_model" not in default
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_grounding.py tests/test_specification.py -v -k "grounding_level or grounding_model or round_trips_grounding"`
Expected: FAIL (`Agent` has no field `grounding_level`; `AgentSpec` rejects unknown kwargs).

- [ ] **Step 3: Implement**

`src/bestteam/core/agent.py` — extend the import and the dataclass:

```python
from .grounding import GROUNDING_LEVELS, GROUNDING_POLICIES
```

```python
    # What to do when this agent's answer fails the grounding check
    # (core/grounding.py). Inert for an agent without a knowledge-base tool.
    grounding_policy: str = "observe"
    # How deep that check goes: "citation" (set membership over returned
    # citations -- the default and the pre-existing behaviour) or "claim"
    # (an additional LLM grader judges each factual claim against the turn's
    # search results). Inert without a knowledge-base tool, like the policy.
    grounding_level: str = "citation"
    # Model for the claim grader; None means the agent's own model. Only read
    # when grounding_level == "claim".
    grounding_model: ModelSpec | None = None
```

And in `__post_init__`, after the `grounding_policy` check:

```python
        if self.grounding_level not in GROUNDING_LEVELS:
            valid = ", ".join(GROUNDING_LEVELS)
            raise ConfigurationError(
                f"Agent '{self.name}' has unknown grounding_level "
                f"'{self.grounding_level}'. Valid values: {valid}"
            )
```

`src/bestteam/core/specification.py` — next to `grounding_policy: Optional[str] = None` add:

```python
    grounding_level: Optional[str] = None
    grounding_model: Optional[str] = None
```

and next to the `to_raw()` emission of `grounding_policy` add:

```python
        if self.grounding_level:
            raw["grounding_level"] = self.grounding_level
        if self.grounding_model:
            raw["grounding_model"] = self.grounding_model
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_grounding.py tests/test_specification.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/agent.py src/bestteam/core/specification.py tests/test_agent.py tests/test_grounding.py tests/test_specification.py
git commit -m "feat(grounding): Agent.grounding_level + grounding_model config fields"
```

---

### Task 3: Adapter wiring — evidence collection, combined bar, claim retry, trace, metering

**Files:**
- Modify: `src/bestteam/adapters/langgraph_adapter.py` (the grounding block at ~`langgraph_adapter.py:882-937`, the KB branch at ~`:816-821`, and a small spec helper near `_model_spec` at `:253`)
- Test: `tests/test_trace_granularity.py` (new section after the existing policy tests)

**Interfaces:**
- Consumes: `grade_claims`, `claim_retry_instruction`, `ClaimGrading` (Task 1); `Agent.grounding_level` / `grounding_model` (Task 2); existing `check_grounding`, `GROUNDING_RETRY_INSTRUCTION`, `GROUNDING_REFUSAL_TEXT`, `STREAM_RESET`, `_resolve_model`, `_model_spec`.
- Produces: `grounding_checked.data` claim-level keys (`level`, `claims`, `claims_supported`, `unsupported_claims`, `claim_check_error`) per the spec; grader usage entries in `usage_sink`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_trace_granularity.py` after the retry-instruction test (~line 1246):

```python
# ---------------------------------------------------------------------------
# Claim-level grounding (grounding_level: "claim"): an LLM grader judges each
# factual claim against the turn's own search results, on top of the citation
# check. Grader failure falls back to citation-level -- never a refusal.
# ---------------------------------------------------------------------------

from bestteam.core.grounding import claim_retry_instruction  # noqa: E402

_SUPPORTED_JSON = AIMessage(
    content='{"claims": [{"text": "Refunds take 14 days.", "supported": true}]}'
)
_UNSUPPORTED_JSON = AIMessage(
    content='{"claims": [{"text": "Refunds take 14 days.", "supported": true},'
    ' {"text": "Shipping is free.", "supported": false}]}'
)


def _claim_agent(model, policy, grounding_model=None):
    return Agent(
        name="a",
        role="role-a",
        goal="goal-a",
        model=model,
        tools=[_stub_knowledge_base_tool(["handbook.pdf, p.3 § Refunds"])],
        grounding_policy=policy,
        grounding_level="claim",
        grounding_model=grounding_model,
    )


def _run_claim(policy, responses, grounding_model=None):
    model = _FakeToolCallingChatModel(responses=responses)
    agent = _claim_agent(model, policy, grounding_model)
    events = list(_single_agent_pipeline(agent).stream("refund window?"))
    final = next(e for e in events if e.type == "agent_completed").data
    grounding = next(e for e in events if e.type == "grounding_checked").data
    return final, grounding


def test_claim_level_all_supported_passes():
    # The grader defaults to the agent's own model, so its call draws the next
    # scripted response: [tool call, answer, grader JSON].
    final, grounding = _run_claim("refuse", [_TOOL_CALL, AIMessage(content=_CITED), _SUPPORTED_JSON])
    assert final == _CITED
    assert grounding["level"] == "claim"
    assert grounding["claims"] == 1
    assert grounding["claims_supported"] == 1
    assert grounding["unsupported_claims"] == []
    assert grounding["refused"] is False


def test_claim_level_observe_records_unsupported_claims_without_retry():
    answer = AIMessage(content=_CITED + " Shipping is free.")
    final, grounding = _run_claim("observe", [_TOOL_CALL, answer, _UNSUPPORTED_JSON])
    assert final == _CITED + " Shipping is free.", "observe must not retry"
    assert grounding["level"] == "claim"
    assert grounding["unsupported_claims"] == ["Shipping is free."]
    assert "policy" not in grounding, "observe keeps the policy keys out, as at citation level"


def test_claim_level_retry_rewrites_and_passes():
    failing = AIMessage(content=_CITED + " Shipping is free.")
    final, grounding = _run_claim(
        "retry",
        # tool call, failing answer, failing grade, rewrite, passing grade
        [_TOOL_CALL, failing, _UNSUPPORTED_JSON, AIMessage(content=_CITED), _SUPPORTED_JSON],
    )
    assert final == _CITED
    assert grounding["retried"] is True
    assert grounding["refused"] is False
    assert grounding["unsupported_claims"] == []


def test_claim_level_refuse_refuses_when_the_rewrite_still_fails():
    failing = AIMessage(content=_CITED + " Shipping is free.")
    final, grounding = _run_claim(
        "refuse",
        [_TOOL_CALL, failing, _UNSUPPORTED_JSON, failing, _UNSUPPORTED_JSON],
    )
    assert final == GROUNDING_REFUSAL_TEXT
    assert grounding["refused"] is True
    assert grounding["unsupported_claims"] == ["Shipping is free."]


def test_claim_level_retry_instruction_names_the_unsupported_claims():
    failing = AIMessage(content=_CITED + " Shipping is free.")
    model = _MessageRecordingModel(
        responses=[_TOOL_CALL, failing, _UNSUPPORTED_JSON, AIMessage(content=_CITED), _SUPPORTED_JSON]
    )
    list(_single_agent_pipeline(_claim_agent(model, "retry")).stream("refund window?"))
    retry_call = model.recorded_calls[-2]  # -1 is the grader's second call
    assert retry_call[-1].content == claim_retry_instruction(["Shipping is free."])
    assert retry_call[-1].type == "human"


def test_claim_level_grader_failure_falls_back_to_citation_level():
    # The grader's scripted response parses to nothing -- the check was not
    # performed, so a citation-passing answer must pass and NOT be refused.
    final, grounding = _run_claim(
        "refuse", [_TOOL_CALL, AIMessage(content=_CITED), AIMessage(content="not json")]
    )
    assert final == _CITED
    assert grounding["level"] == "claim"
    assert grounding["claim_check_error"] is True
    assert "claims" not in grounding
    assert grounding["refused"] is False


def test_claim_level_grader_is_not_called_when_the_citation_check_fails():
    # Citation check fails -> the grader must not burn a call; the retry uses
    # the plain citation instruction; the rewrite passes citations and then
    # gets graded. Scripted: tool call, uncited answer, rewrite, grader JSON.
    model = _MessageRecordingModel(
        responses=[_TOOL_CALL, AIMessage(content="Uncited."), AIMessage(content=_CITED), _SUPPORTED_JSON]
    )
    events = list(_single_agent_pipeline(_claim_agent(model, "retry")).stream("refund window?"))
    grounding = next(e for e in events if e.type == "grounding_checked").data
    assert grounding["claims"] == 1
    retry_call = model.recorded_calls[2]
    assert retry_call[-1].content == GROUNDING_RETRY_INSTRUCTION
    assert grounding["retried"] is True


def test_claim_level_uses_the_grounding_model_override_and_meters_it():
    # Override grader: the agent's model scripts only its own two calls; the
    # grader draws from its own FakeListChatModel. FakeListChatModel reports
    # no usage, so metering is asserted structurally: the run completes and
    # the grader call happened (grading keys present).
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    grader = FakeListChatModel(responses=['{"claims": [{"text": "Refunds take 14 days.", "supported": true}]}'])
    final, grounding = _run_claim("refuse", [_TOOL_CALL, AIMessage(content=_CITED)], grounding_model=grader)
    assert final == _CITED
    assert grounding["claims"] == 1
    assert grounding["refused"] is False


def test_citation_level_payload_is_byte_identical_with_claim_machinery_present():
    # The default level must keep the exact pre-claim payload -- no level key.
    final, grounding = _run_policy(
        "observe",
        [_TOOL_CALL, AIMessage(content=_CITED)],
    )
    assert "level" not in grounding
    assert "claims" not in grounding
    assert "claim_check_error" not in grounding


def test_claim_level_grader_usage_is_metered_with_the_grader_model_spec():
    # A grader response carrying usage_metadata must land in the usage sink
    # tagged with the grader model's spec (its class name for an instance with
    # no model attribute). FakeMessagesListChatModel returns the scripted
    # message as-is, usage_metadata included, and IS a BaseChatModel so
    # `_resolve_model` accepts it.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    from bestteam.adapters.langgraph_adapter import _run_agent

    grader = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"claims": []}',
                usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            )
        ]
    )
    model = _FakeToolCallingChatModel(responses=[_TOOL_CALL, AIMessage(content=_CITED)])
    agent = Agent(
        name="a",
        role="role-a",
        goal="goal-a",
        model=model,
        tools=[_stub_knowledge_base_tool(["handbook.pdf, p.3 § Refunds"])],
        grounding_level="claim",
        grounding_model=grader,
    )
    sink = []
    _run_agent(agent, "refund window?", usage_sink=sink)
    grader_entries = [u for u in sink if u["model"] == "FakeMessagesListChatModel"]
    assert grader_entries == [
        {"model": "FakeMessagesListChatModel", "input_tokens": 7, "output_tokens": 3}
    ]


def test_claim_level_bad_grounding_model_spec_fails_soft_not_the_run():
    # `_resolve_model` raises ConfigurationError for an unresolvable spec; at
    # grading time that must degrade to citation-level, never fail the run.
    final, grounding = _run_claim(
        "refuse", [_TOOL_CALL, AIMessage(content=_CITED)], grounding_model=12345
    )
    assert final == _CITED
    assert grounding["claim_check_error"] is True
    assert grounding["refused"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_trace_granularity.py -v -k claim`
Expected: FAIL — `grounding_checked.data` has no `level` key (KeyError / assertion failures); the override test fails because the grader is never called.

- [ ] **Step 3: Implement** in `src/bestteam/adapters/langgraph_adapter.py`:

3a. Extend the grounding import (line 17):

```python
from ..core.grounding import (
    GROUNDING_REFUSAL_TEXT,
    GROUNDING_RETRY_INSTRUCTION,
    check_grounding,
    claim_retry_instruction,
    grade_claims,
)
```

3b. Near `_model_spec` (line ~253), add:

```python
def _spec_string(model: Any) -> str:
    """A best-effort spec string for any model value, for usage attribution."""
    if isinstance(model, str):
        return model
    return getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__
```

and rewrite `_model_spec`'s body to `return _spec_string(agent.model)` (behaviour unchanged).

3c. In `_run_agent`, next to `kb_citations`/`kb_documents` (line ~615):

```python
    kb_result_texts: List[str] = []
```

and in the KB tool success branch (after `kb_documents.extend(...)`, line ~821):

```python
                        kb_result_texts.append(str(result))
```

3d. Replace the grounding block (currently `result = check_grounding(...)` through `_emit("grounding_checked", data)`, lines ~889-936) with:

```python
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
            if grader_model is None:
                try:
                    grader_model = (
                        _resolve_model(agent.grounding_model) if agent.grounding_model is not None else raw_model
                    )
                except Exception:  # noqa: BLE001 -- deliberate fail-soft, incl. ConfigurationError:
                    # a bad grader spec degrades the CHECK, it must never fail the RUN
                    # (rerank/expansion precedent). Not the BestTeamError-masking case --
                    # nothing is re-raised as EngineError here.
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
                        "model": _spec_string(agent.grounding_model) if agent.grounding_model is not None else _model_spec(agent),
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
```

Note the pre-existing lines kept verbatim inside this block (`_record_usage`, cancel guard, `model_turns` accounting) — this is a rewrite of the block, not an adjacent-code cleanup.

- [ ] **Step 4: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_trace_granularity.py tests/test_grounding.py tests/test_streaming.py tests/test_run_cancellation.py -v`
Expected: ALL PASS — the pre-existing policy tests prove the citation-level path is unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/adapters/langgraph_adapter.py tests/test_trace_granularity.py
git commit -m "feat(grounding): claim-level grading wired into the agent turn"
```

---

### Task 4: Documentation

**Files:**
- Modify: `CLAUDE.md` (root — the Knowledge bases known-limitations bullet)
- Modify: `src/bestteam/CLAUDE.md` (the grounding paragraph in "Forced tool_choice and its fallback")
- Modify: `docs/KNOWLEDGE_BASES.md` (grounding section)
- Modify: `docs/STATUS.md` (move the item)

**Interfaces:** none — prose only. Keep the CLAUDE.md edits to a few lines each (vital-only rule).

- [ ] **Step 1: Root `CLAUDE.md`** — in the known-limitations Knowledge bases bullet, update the grounding clause to say: grounding is *checked*; enforcement is the opt-in per-agent `grounding_policy: observe|retry|refuse`, and depth is `grounding_level: citation|claim` (claim = one extra LLM-grader call per turn, default citation) — still not entailment-verified evidence spans.

- [ ] **Step 2: `src/bestteam/CLAUDE.md`** — extend the grounding paragraph with (concise): `grounding_level: claim` (opt-in) additionally runs `grade_claims` — one plain-invoke LLM call (deliberately not `with_structured_output`: `fake:` can't, reasoning models 400) judging each factual claim against the turn's KB tool results; combined bar = citation check ∧ no unsupported claims; grader failure fail-softs to citation level; `grounding_model` overrides the grader (default: the agent's own model); claim keys ride `grounding_checked` only at claim level, so the default payload stays byte-identical.

- [ ] **Step 3: `docs/KNOWLEDGE_BASES.md`** — find the grounding section (`Grep "grounding" docs/KNOWLEDGE_BASES.md`) and add a "Claim-level grounding" subsection: the two YAML keys with a snippet, the combined pass bar, retry/refuse semantics (rewrite deletes/re-grounds named claims; refuse only after the rewrite fails), fail-soft rule, metering (grader usage on `agent_completed.usage`, tagged with the grader model's spec), trace keys, and the recommended rollout (`observe` + `claim` first, watch `unsupported_claims` for grader false positives, then raise the policy).

- [ ] **Step 4: `docs/STATUS.md`** — add the shipped item under done / adjust known issues if it lists claim-level grounding as missing.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md src/bestteam/CLAUDE.md docs/KNOWLEDGE_BASES.md docs/STATUS.md
git commit -m "docs(grounding): claim-level grounding reference + invariants"
```

---

### Task 5: Full verification

- [ ] **Step 1: Full suite, serial** (what `backend-full` runs on main; catches ordering/isolation bugs):

Run: `./.venv/Scripts/python.exe -m pytest -m "not e2e"`
Expected: ALL PASS (baseline ~2,450 tests).

- [ ] **Step 2: Lint** (if the repo configures ruff/flake8 in `pyproject.toml` — check and run accordingly):

Run: `./.venv/Scripts/python.exe -m ruff check src tests` (skip if ruff is not a dev dependency)

- [ ] **Step 3: E2E** (SDK-only change, but the local gates rule applies; ports 8000/5173 must be free):

Run: `./.venv/Scripts/python.exe -m pytest tests/e2e -m e2e`
Expected: PASS. If the environment blocks (ports/npm), record the fact honestly in the final report instead of skipping silently.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/claim-level-grounding
gh pr create --title "feat(grounding): opt-in claim-level grounding (LLM grader)" --body "..."
```
