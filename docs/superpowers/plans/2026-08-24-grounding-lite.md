# Grounding-lite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent with a knowledge-base tool makes its first model call with `tool_choice="required"` on every team mode, and its final text's `[source: …]` tags are checked against the citations its own searches returned, recorded as one `grounding_checked` trace event.

**Architecture:** A pure checker in `src/bestteam/core/grounding.py` (regex over the final text, set comparison against the citation labels the tool reported). The knowledge-base tool reports its full citation list through the existing `report_trace` side channel; the adapter's `_run_agent` accumulates those per turn, forces the first call when a knowledge-base tool is bound, and emits the event. Backend needs nothing (events persist generically); the frontend gains one label and one summary line.

**Tech Stack:** Python 3.10, LangChain fake chat models for tests, pytest; React/Vite/Vitest for the one frontend line.

**Spec:** `docs/superpowers/specs/2026-08-24-grounding-lite-design.md`

## Global Constraints

- Force `tool_choice="required"` only when a knowledge-base tool (a callable with `__bestteam_tool_kind__ == "knowledge_base"`) is bound — never for any other tool. `_make_delegate_tool` and the manager path are unchanged.
- The `grounding_checked` event carries counts and citation labels only: no chunk text, no query, no model name. `unverified` ≤ 10 entries, each ≤ 200 characters.
- The `tool_completed` event of a knowledge-base tool is unchanged: the new `citations` report field is **not** copied into it.
- The check records; it never alters, retries, blocks or flags the answer.
- No new configuration (no env var, no per-agent switch, no schema change).
- Code comments in English; prose (docs, CHANGELOG) in British English.
- Run Python through the project venv: `./.venv/Scripts/python.exe` on Windows.
- Every new test file needs a `pytestmark` (`unit` here).

---

## File structure

| File | Responsibility |
|---|---|
| `src/bestteam/core/grounding.py` (create) | `check_grounding()` and `GroundingResult` — pure, engine-free |
| `tests/test_grounding.py` (create) | unit tests for the checker |
| `src/bestteam/core/knowledge_base.py` (modify, `make_knowledge_base_tool`) | report `citations` in full |
| `tests/test_knowledge_base.py` (modify) | `citations` full while `sources` capped |
| `src/bestteam/adapters/langgraph_adapter.py` (modify) | `_has_knowledge_base_tool`, forcing in `_agent_node`, accumulation and emission in `_run_agent` |
| `src/bestteam/core/trace.py` (modify docstring) | document the event type |
| `tests/test_trace_granularity.py` (modify) | event emission, ordering, counts, forcing |
| `ui/frontend/src/lib/traceEvents.ts` + `traceEvents.test.ts` (modify) | label + summary line |
| `docs/KNOWLEDGE_BASES.md`, `src/bestteam/CLAUDE.md`, `CLAUDE.md`, `docs/STATUS.md`, `CHANGELOG.md` (modify) | documentation |

---

### Task 1: The checker (`core/grounding.py`)

**Files:**
- Create: `src/bestteam/core/grounding.py`
- Test: `tests/test_grounding.py`

**Interfaces:**
- Consumes: nothing from the repo.
- Produces: `check_grounding(text: str, citations: Sequence[str], *, searches: int, hit_count: int) -> GroundingResult`; `GroundingResult(searches, hit_count, cited, verified, unverified)` frozen dataclass with `as_trace_data() -> Dict[str, Any]` returning `{"searches", "hit_count", "cited", "verified", "unverified"}`; module constants `CITATION_TAG`, `MAX_UNVERIFIED = 10`, `MAX_LABEL_CHARS = 200`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_grounding.py
import pytest

from bestteam.core.grounding import (
    MAX_LABEL_CHARS,
    MAX_UNVERIFIED,
    GroundingResult,
    check_grounding,
)

pytestmark = pytest.mark.unit


def test_exact_label_is_verified():
    result = check_grounding(
        "Refunds take 14 days [source: handbook.pdf, p.3 § Refunds].",
        ["handbook.pdf, p.3 § Refunds", "policies.md"],
        searches=1,
        hit_count=2,
    )
    assert result == GroundingResult(searches=1, hit_count=2, cited=1, verified=1, unverified=[])


def test_whitespace_differences_do_not_make_a_label_unverified():
    result = check_grounding(
        "See [source:  handbook.pdf,  p.3 §  Refunds ].",
        ["handbook.pdf, p.3 § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.cited == 1
    assert result.verified == 1
    assert result.unverified == []


def test_filename_only_tag_is_verified_when_that_document_was_returned():
    result = check_grounding(
        "As the handbook says [source: handbook.pdf].",
        ["handbook.pdf, p.3 § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.verified == 1
    assert result.unverified == []


def test_tag_with_an_unreturned_page_is_unverified():
    result = check_grounding(
        "See [source: handbook.pdf, p.99].",
        ["handbook.pdf, p.3 § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.cited == 1
    assert result.verified == 0
    assert result.unverified == ["handbook.pdf, p.99"]


def test_tag_with_an_unreturned_heading_is_unverified():
    result = check_grounding(
        "See [source: policies.md § Holidays].",
        ["policies.md § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.unverified == ["policies.md § Holidays"]


def test_filename_only_tag_for_a_document_never_returned_is_unverified():
    result = check_grounding(
        "See [source: invented.pdf].",
        ["handbook.pdf, p.3"],
        searches=1,
        hit_count=1,
    )
    assert result.unverified == ["invented.pdf"]


def test_filename_match_is_case_sensitive():
    result = check_grounding("See [source: Handbook.pdf].", ["handbook.pdf, p.3"], searches=1, hit_count=1)
    assert result.unverified == ["Handbook.pdf"]


def test_repeated_label_counts_once_and_keeps_first_appearance_order():
    text = (
        "A [source: b.md, p.2]. B [source: a.md]. C [source: b.md, p.2]. D [source: c.md]."
    )
    result = check_grounding(text, ["a.md"], searches=1, hit_count=1)
    assert result.cited == 3
    assert result.verified == 1
    assert result.unverified == ["b.md, p.2", "c.md"]


def test_no_tags_is_zero_cited_even_with_hits():
    result = check_grounding("Plain answer.", ["handbook.pdf"], searches=1, hit_count=1)
    assert result == GroundingResult(searches=1, hit_count=1, cited=0, verified=0, unverified=[])


def test_empty_text_and_no_citations_is_valid():
    result = check_grounding("", [], searches=0, hit_count=0)
    assert result == GroundingResult(searches=0, hit_count=0, cited=0, verified=0, unverified=[])


def test_tags_with_no_search_are_all_unverified():
    result = check_grounding("[source: a.md] [source: b.md]", [], searches=0, hit_count=0)
    assert result.cited == 2
    assert result.verified == 0
    assert result.unverified == ["a.md", "b.md"]


def test_empty_tag_is_ignored():
    result = check_grounding("[source: ] [source:   ]", ["a.md"], searches=1, hit_count=1)
    assert result.cited == 0
    assert result.unverified == []


def test_unverified_is_capped_and_each_label_truncated():
    labels = [f"doc{i}.pdf, p.{i}" for i in range(15)]
    long_label = "x" * 500
    text = " ".join(f"[source: {label}]" for label in [*labels, long_label])
    result = check_grounding(text, [], searches=0, hit_count=0)
    assert result.cited == 16
    assert len(result.unverified) == MAX_UNVERIFIED == 10
    assert result.unverified == labels[:10]

    only_long = check_grounding(f"[source: {long_label}]", [], searches=0, hit_count=0)
    assert only_long.unverified == ["x" * MAX_LABEL_CHARS]
    assert MAX_LABEL_CHARS == 200


def test_as_trace_data_shape():
    result = check_grounding("[source: a.md] [source: z.md]", ["a.md"], searches=2, hit_count=5)
    assert result.as_trace_data() == {
        "searches": 2,
        "hit_count": 5,
        "cited": 2,
        "verified": 1,
        "unverified": ["z.md"],
    }
    # A fresh list each time -- callers must not be able to mutate the result.
    assert result.as_trace_data()["unverified"] is not result.unverified
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_grounding.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bestteam.core.grounding'`

- [ ] **Step 3: Write the module**

```python
# src/bestteam/core/grounding.py
"""Grounding-lite: does an agent's answer cite what its searches returned?

A knowledge-base tool hands the model excerpts tagged ``[source: <label>]``
and asks it to cite them with the same tag. Nothing stops a model from
writing a tag the search never returned -- a plausible filename, a page
number that does not exist. This module compares the tags in an agent's
final text with the citation labels its own knowledge-base searches produced
during the same turn, and reports the counts. It records; it never changes
the answer. The adapter (``adapters/langgraph_adapter.py``) runs it once per
turn for an agent that has a knowledge-base tool bound and emits the result
as a ``grounding_checked`` trace event.

Verification rule: a tag is verified when its label equals a returned
citation exactly (after whitespace normalisation), or when the tag names
only a filename and that filename is the document of some returned citation.
A tag carrying a page or heading that matches no returned citation is
unverified -- a fabricated locator is precisely what this exists to show.
Filenames are case-sensitive, so the comparison is too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

#: The tag the tool tells the model to quote (see
#: ``knowledge_base.format_results``): ``[source: handbook.pdf, p.3 § Refunds]``.
CITATION_TAG = re.compile(r"\[source:\s*([^\]]*?)\s*\]")

#: Bounds on what the event records. An unverified label is model-written
#: text, so it gets the same length bound a traced query has, and a list of
#: them must not turn one event into a wall.
MAX_UNVERIFIED = 10
MAX_LABEL_CHARS = 200

# What ``knowledge_base._citation`` appends after the filename: a page
# (``, p.3``) and/or a heading (`` § Refunds``). A label with neither is a
# bare filename.
_LOCATOR_MARKERS = (", p.", " § ")


def _normalise(label: str) -> str:
    """Collapse internal whitespace and strip the ends -- applied to both sides."""
    return " ".join(label.split())


def _filename(label: str) -> str:
    """The document part of a citation label: everything before the first locator."""
    cut = len(label)
    for marker in _LOCATOR_MARKERS:
        index = label.find(marker)
        if index != -1 and index < cut:
            cut = index
    return label[:cut]


@dataclass(frozen=True)
class GroundingResult:
    """What one agent turn's citations looked like against its own searches."""

    #: Knowledge-base tool calls that completed this turn.
    searches: int
    #: Passages those searches returned, summed over the calls.
    hit_count: int
    #: Distinct ``[source: …]`` labels in the final text.
    cited: int
    #: Of those, labels the searches actually returned.
    verified: int
    #: The rest, in order of first appearance, bounded.
    unverified: List[str]

    def as_trace_data(self) -> Dict[str, Any]:
        """The ``grounding_checked`` event's ``data`` -- a fresh dict, fresh list."""
        return {
            "searches": self.searches,
            "hit_count": self.hit_count,
            "cited": self.cited,
            "verified": self.verified,
            "unverified": list(self.unverified),
        }


def check_grounding(
    text: str,
    citations: Sequence[str],
    *,
    searches: int,
    hit_count: int,
) -> GroundingResult:
    """Compare the ``[source: …]`` tags in ``text`` with ``citations``.

    ``citations`` is every label the agent's knowledge-base searches returned
    this turn, in full (not the bounded ``sources`` a trace event keeps).
    ``searches`` and ``hit_count`` are carried through unchanged so the
    result is the whole story of the turn in one object.
    """
    returned = {_normalise(citation) for citation in citations}
    returned_files = {_filename(citation) for citation in returned}

    # dict.fromkeys de-duplicates while keeping first-appearance order.
    labels = [
        label
        for label in dict.fromkeys(_normalise(match) for match in CITATION_TAG.findall(text or ""))
        if label
    ]

    verified = 0
    unverified: List[str] = []
    for label in labels:
        filename_only = _filename(label) == label
        if label in returned or (filename_only and label in returned_files):
            verified += 1
        else:
            unverified.append(label[:MAX_LABEL_CHARS])

    return GroundingResult(
        searches=searches,
        hit_count=hit_count,
        cited=len(labels),
        verified=verified,
        unverified=unverified[:MAX_UNVERIFIED],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_grounding.py -q`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/grounding.py tests/test_grounding.py
git commit -m "feat(core): grounding checker — [source: …] tags against the turn's own citations"
```

---

### Task 2: The knowledge-base tool reports its full citation list

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py` (`make_knowledge_base_tool`, the `report_trace(...)` call inside `_tool`, around line 1113)
- Test: `tests/test_knowledge_base.py` (append near `test_make_knowledge_base_tool_name_and_delegation`, line ~484)

**Interfaces:**
- Consumes: `report_trace(**fields)` from `core/tool_context.py` (no-op outside a run); `tool_call_context()` from the same module, used in the test to read what the tool reported.
- Produces: the tool's `ToolCallContext.trace` gains `citations: List[str]` — one `_citation(chunk)` string per returned chunk, in rank order, **unbounded and not de-duplicated** (`sources` stays de-duplicated and capped at `_MAX_TRACE_SOURCES = 10`). Task 3's adapter reads `tool_ctx.trace["citations"]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_knowledge_base.py`:

```python
def test_tool_reports_every_citation_while_sources_stay_capped(tmp_path):
    """The adapter's grounding check needs every label the model was shown,
    not the ten the trace keeps -- otherwise a top_k above 10 would make a
    correctly cited passage look fabricated."""
    from bestteam.core.knowledge_base import _MAX_TRACE_SOURCES, _Chunk
    from bestteam.core.tool_context import tool_call_context

    chunks = [
        _Chunk(source=f"doc{i}.md", text=f"apples orchard harvest {i}", page=None, heading=None)
        for i in range(_MAX_TRACE_SOURCES + 3)
    ]
    kb = LocalFolderKnowledgeBase.from_chunks("docs", chunks, top_k=_MAX_TRACE_SOURCES + 3)
    tool = make_knowledge_base_tool(kb)

    with tool_call_context() as ctx:
        tool("apples orchard harvest")

    assert len(ctx.trace["sources"]) == _MAX_TRACE_SOURCES
    assert len(ctx.trace["citations"]) == _MAX_TRACE_SOURCES + 3
    assert ctx.trace["citations"][: _MAX_TRACE_SOURCES] == ctx.trace["sources"]
```

Check `_Chunk`'s constructor first: `grep -n "class _Chunk" -A 12 src/bestteam/core/knowledge_base.py`. It is a `NamedTuple` with more fields than the four above (`chunk_id`, `document_id`, `ingestion_job_id` were added in PR #86); if those have no defaults, pass `chunk_id=None, document_id=None, ingestion_job_id=None` explicitly. Look at how the existing `from_chunks` tests around line 322 build chunks and copy that form.

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_knowledge_base.py::test_tool_reports_every_citation_while_sources_stay_capped -q`
Expected: FAIL — `KeyError: 'citations'`

- [ ] **Step 3: Report the field**

In `make_knowledge_base_tool._tool`, the `report_trace(...)` call currently reads:

```python
        report_trace(
            query=bounded_query,
            hit_count=len(chunks),
            sources=sources,
            # Which generation of the collection answered ...
            ingestion_job_id=getattr(kb, "ingestion_job_id", None),
            hits=[_trace_hit(hit) for hit in hits[:_MAX_TRACE_SOURCES]],
            summary=(...),
        )
```

Add one field (keep everything else as it is):

```python
            # Every label the model was shown, in rank order and unbounded --
            # for the adapter's grounding check (core/grounding.py), which
            # must not mistake the eleventh passage's citation for an
            # invented one. `_kb_tool_trace_data` copies named fields only,
            # so this never reaches the trace event.
            citations=[_citation(chunk) for chunk in chunks],
```

- [ ] **Step 4: Run the test and the file**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_knowledge_base.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "feat(kb): tool reports every citation label for the grounding check"
```

---

### Task 3: Adapter — force the first call for KB agents and emit `grounding_checked`

**Files:**
- Modify: `src/bestteam/adapters/langgraph_adapter.py` — `_run_agent` (signature at ~480; the KB branch in the tool loop at ~739; the final return at ~784), `_agent_node` (~852), and a new helper beside `_kb_tool_trace_data` (~440)
- Modify: `src/bestteam/core/trace.py` docstring (line ~19-25, the `tool_completed` sentence)
- Test: `tests/test_trace_granularity.py` (append)

**Interfaces:**
- Consumes: `check_grounding` / `GroundingResult` from Task 1; `tool_ctx.trace["citations"]` and `tool_ctx.trace["hit_count"]` from Task 2.
- Produces: `_has_knowledge_base_tool(agent: Agent) -> bool`; a `grounding_checked` `TraceEvent` (`agent=<name>`, `data=GroundingResult.as_trace_data()`), emitted through `_emit` in `_run_agent` after the loop and before the final `return response.content …`, only when `_has_knowledge_base_tool(agent)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trace_granularity.py` (the file already imports `AIMessage`, `Agent`, `CollaborationMode`, `Team`, `Pipeline` and defines `_FakeToolCallingChatModel`):

```python
# ---------------------------------------------------------------------------
# Grounding-lite (core/grounding.py): a knowledge-base agent searches first,
# and its [source: …] tags are checked against what its searches returned.
# ---------------------------------------------------------------------------

from bestteam.core.tool_context import report_trace


def _stub_knowledge_base_tool(citations):
    """A tool shaped like `make_knowledge_base_tool`'s: the marker the adapter
    dispatches on, and a `report_trace` call with the fields the real tool
    reports -- so these tests need no documents on disk."""

    def product_docs(query: str) -> str:
        report_trace(
            query=query,
            hit_count=len(citations),
            sources=list(dict.fromkeys(citations))[:10],
            citations=list(citations),
            summary=f"{len(citations)} result(s)",
        )
        return "\n".join(f"[source: {c}]\nexcerpt" for c in citations)

    product_docs.__bestteam_tool_kind__ = "knowledge_base"
    return product_docs


class _RecordingToolCallingChatModel(_FakeToolCallingChatModel):
    """Records every `tool_choice` passed to `bind_tools`."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        calls = getattr(self, "bind_tools_calls", None) or []
        calls.append(tool_choice)
        object.__setattr__(self, "bind_tools_calls", calls)
        return self


def _single_agent_pipeline(agent):
    return Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])


def test_knowledge_base_agent_emits_grounding_checked_between_tool_completed_and_agent_completed():
    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "product_docs", "args": {"query": "refunds"}, "id": "call_1"}],
            ),
            AIMessage(
                content="Refunds take 14 days [source: handbook.pdf, p.3 § Refunds]. "
                "Holidays are 25 days [source: handbook.pdf, p.99]."
            ),
        ]
    )
    agent = Agent(
        name="a",
        role="role-a",
        goal="goal-a",
        model=model,
        tools=[_stub_knowledge_base_tool(["handbook.pdf, p.3 § Refunds", "policies.md"])],
    )

    events = list(_single_agent_pipeline(agent).stream("what is the refund window?"))
    types = [e.type for e in events]

    assert types.index("tool_completed") < types.index("grounding_checked") < types.index("agent_completed")
    grounding = next(e for e in events if e.type == "grounding_checked")
    assert grounding.agent == "a"
    assert grounding.data == {
        "searches": 1,
        "hit_count": 2,
        "cited": 2,
        "verified": 1,
        "unverified": ["handbook.pdf, p.99"],
    }
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert "citations" not in tool_completed.data
    assert tool_completed.data["sources"] == ["handbook.pdf, p.3 § Refunds", "policies.md"]


def test_agent_without_a_knowledge_base_tool_emits_no_grounding_event():
    def echo_tool(text: str) -> str:
        return f"echoed: {text}"

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "call_1"}]),
            AIMessage(content="Done [source: made-up.pdf]."),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[echo_tool])

    events = list(_single_agent_pipeline(agent).stream("do the thing"))

    assert "grounding_checked" not in [e.type for e in events]


def test_knowledge_base_agent_that_never_searches_reports_zero_searches_and_all_tags_unverified():
    model = _FakeToolCallingChatModel(responses=[AIMessage(content="It is 14 days [source: handbook.pdf].")])
    agent = Agent(
        name="a",
        role="role-a",
        goal="goal-a",
        model=model,
        tools=[_stub_knowledge_base_tool(["handbook.pdf, p.3"])],
    )

    events = list(_single_agent_pipeline(agent).stream("refund window?"))
    grounding = next(e for e in events if e.type == "grounding_checked")

    assert grounding.data == {
        "searches": 0,
        "hit_count": 0,
        "cited": 1,
        "verified": 0,
        "unverified": ["handbook.pdf"],
    }


def test_two_searches_accumulate_citations_and_hit_counts():
    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "product_docs", "args": {"query": "refunds"}, "id": "call_1"},
                    {"name": "product_docs", "args": {"query": "holidays"}, "id": "call_2"},
                ],
            ),
            AIMessage(content="[source: a.md] [source: b.md] [source: c.md]"),
        ]
    )
    # One tool, two calls: both return the same two labels, so hit_count sums
    # to 4 while the distinct labels stay two.
    agent = Agent(
        name="a",
        role="role-a",
        goal="goal-a",
        model=model,
        tools=[_stub_knowledge_base_tool(["a.md", "b.md"])],
    )

    events = list(_single_agent_pipeline(agent).stream("q"))
    grounding = next(e for e in events if e.type == "grounding_checked")

    assert grounding.data["searches"] == 2
    assert grounding.data["hit_count"] == 4
    assert grounding.data["cited"] == 3
    assert grounding.data["verified"] == 2
    assert grounding.data["unverified"] == ["c.md"]


def test_sequential_knowledge_base_agent_forces_tool_choice_on_first_call():
    model = _RecordingToolCallingChatModel(responses=[AIMessage(content="answer")])
    agent = Agent(
        name="a",
        role="role-a",
        goal="goal-a",
        model=model,
        tools=[_stub_knowledge_base_tool(["handbook.pdf"])],
    )

    _single_agent_pipeline(agent).run("q")

    assert "required" in model.bind_tools_calls


def test_sequential_agent_with_only_an_ordinary_tool_is_not_forced():
    def echo_tool(text: str) -> str:
        return text

    model = _RecordingToolCallingChatModel(responses=[AIMessage(content="answer")])
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[echo_tool])

    _single_agent_pipeline(agent).run("q")

    assert model.bind_tools_calls == [None]


def test_parallel_knowledge_base_agent_is_forced_and_checked_too():
    model = _RecordingToolCallingChatModel(responses=[AIMessage(content="answer [source: handbook.pdf]")])
    kb_agent = Agent(
        name="kb",
        role="role-kb",
        goal="goal-kb",
        model=model,
        tools=[_stub_knowledge_base_tool(["handbook.pdf"])],
    )
    other = _agent("other", "other output")
    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[kb_agent, other], mode=CollaborationMode.PARALLEL)],
    )

    events = list(pipeline.stream("q"))

    assert "required" in model.bind_tools_calls
    grounding = [e for e in events if e.type == "grounding_checked"]
    assert [e.agent for e in grounding] == ["kb"]
    assert grounding[0].data["cited"] == 1
    assert grounding[0].data["verified"] == 0  # the model never searched, so the tag is unverified
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_trace_granularity.py -q -k "grounding or forces or forced or not_forced"`
Expected: the first four fail with `StopIteration` (no `grounding_checked` event); `test_sequential_knowledge_base_agent_forces_tool_choice_on_first_call` and the parallel one fail on `"required" in …`; `test_agent_without_a_knowledge_base_tool_emits_no_grounding_event` and `test_sequential_agent_with_only_an_ordinary_tool_is_not_forced` already pass (that is fine — they guard the boundary).

- [ ] **Step 3: Implement in the adapter**

3a. Import, at the top of `src/bestteam/adapters/langgraph_adapter.py` beside the other `..core` imports:

```python
from ..core.grounding import check_grounding
```

3b. Add the helper directly after `_kb_tool_trace_data`:

```python
def _has_knowledge_base_tool(agent: Agent) -> bool:
    """True when one of the agent's own tools is a knowledge base (the marker
    `make_knowledge_base_tool` sets). Decides two things for the agent's turn:
    its first model call is forced to use a tool, and its final text gets a
    grounding check (core/grounding.py)."""
    return any(getattr(fn, "__bestteam_tool_kind__", None) == "knowledge_base" for fn in agent.tools)
```

3c. In `_run_agent`, right after `tools_by_name = {fn.__name__: fn for fn in all_tools}`, add the per-turn accumulators:

```python
    # Grounding-lite (core/grounding.py): what this turn's knowledge-base
    # searches returned, so the final text's [source: …] tags can be checked
    # against them. Only ever read when the agent has a knowledge-base tool.
    kb_searches = 0
    kb_hit_count = 0
    kb_citations: List[str] = []
```

3d. In the tool loop's success branch, the existing

```python
                    elif getattr(tool_fn, "__bestteam_tool_kind__", None) == "knowledge_base":
                        extra_data = _kb_tool_trace_data(tool_ctx.trace)
```

becomes

```python
                    elif getattr(tool_fn, "__bestteam_tool_kind__", None) == "knowledge_base":
                        extra_data = _kb_tool_trace_data(tool_ctx.trace)
                        kb_searches += 1
                        kb_hit_count += int(tool_ctx.trace.get("hit_count") or 0)
                        kb_citations.extend(tool_ctx.trace.get("citations") or ())
```

(A failed call takes the `except` branch above and counts for nothing.)

3e. The end of `_run_agent` currently reads:

```python
    if getattr(response, "tool_calls", None):
        # The loop ran out while the model was still asking for tools ...
        return _tool_loop_exhausted_notice(agent.name)
    return response.content if hasattr(response, "content") else str(response)
```

Replace the last line with:

```python
    text = response.content if hasattr(response, "content") else str(response)
    if _has_knowledge_base_tool(agent):
        # Grounding-lite: record how the answer's citations compare with what
        # this turn's searches returned. Recorded, never acted on -- the text
        # is returned unchanged. Not emitted on the early returns above (a
        # stop, an exhausted loop): those turns produced no answer to check.
        _emit(
            "grounding_checked",
            check_grounding(text, kb_citations, searches=kb_searches, hit_count=kb_hit_count).as_trace_data(),
        )
    return text
```

3f. In `_agent_node`'s `node`, the `_run_agent(...)` call gains one keyword argument (place it after `extra_system_prompt=`):

```python
            require_tool_use_on_first_call=_has_knowledge_base_tool(agent),
```

and add to the `_agent_node` docstring, after the `streams` paragraph:

```
    An agent with a knowledge-base tool has its first model call forced to
    use a tool (`require_tool_use_on_first_call`), the same insurance the
    hierarchical paths carry: the tool's docstring asks the model to search
    before answering, and a real model can ignore that. Any other tool set
    keeps the unforced first call. The forcing has `_first_call`'s fallback
    for a provider that rejects it.
```

3g. Update the `_run_agent` docstring sentence that starts "`require_tool_use_on_first_call`, if True and tools are bound, forces the very first model call to use `tool_choice=\"required\"` … so `_hierarchical_node` uses this to make a manager's first turn always delegate" — after "…rather than merely suggesting it should." add: "`_agent_node` uses it for an agent that carries a knowledge-base tool, for the same reason."

3h. `src/bestteam/core/trace.py` docstring: after the `tool_completed` description (the sentence ending "never body text)," and before `"delegation_started"`), insert:

```
    "grounding_checked" (`data` = {"searches": int, "hit_count": int,
    "cited": int, "verified": int, "unverified": List[str]} -- emitted once
    per turn of an agent that has a knowledge-base tool bound, after its last
    tool event and before its `agent_completed`: how many `[source: …]` tags
    its final text carries, how many name a citation its own searches
    returned, and the ones that do not (≤10, each ≤200 chars); citation
    labels only, never text -- see `core/grounding.py`),
```

- [ ] **Step 4: Run the adapter-facing suites**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_trace_granularity.py tests/test_hierarchical_team.py tests/test_pipeline.py tests/test_streaming.py tests/test_diagnostic_trace.py tests/test_usage_metering.py tests/test_knowledge_base.py -q`
Expected: all pass. If a hierarchical test now sees an extra `grounding_checked` event in an exact event-list assertion, that subordinate carries a KB-marked tool — check whether the test's tool is genuinely marked (`__bestteam_tool_kind__`); an unmarked stub tool must not trigger the event, so a failure there means the helper is checking the wrong thing, not that the test needs changing.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/adapters/langgraph_adapter.py src/bestteam/core/trace.py tests/test_trace_granularity.py
git commit -m "feat(adapter): KB agents search first, and their citations are checked (grounding_checked)"
```

---

### Task 4: Frontend — label and summary line for `grounding_checked`

**Files:**
- Modify: `ui/frontend/src/lib/traceEvents.ts` (`EVENT_LABELS` map, ~line 55; `renderEventData` switch, ~line 90)
- Test: `ui/frontend/src/lib/traceEvents.test.ts` (append a `describe`)

**Interfaces:**
- Consumes: the event `data` shape from Task 3: `{searches, hit_count, cited, verified, unverified: string[]}`.
- Produces: nothing downstream.

- [ ] **Step 1: Write the failing tests**

Append to `ui/frontend/src/lib/traceEvents.test.ts`:

```ts
// Grounding-lite (core/grounding.py): one event per knowledge-base agent
// turn, saying whether its [source: …] tags name passages it retrieved.
describe('grounding_checked', () => {
  it('has a technical label', () => {
    expect(EVENT_LABELS.grounding_checked).toBe('📎 grounding checked')
  })

  it('summarises the counts and lists the unverified labels', () => {
    expect(
      renderEventData({
        type: 'grounding_checked',
        agent: 'a',
        data: { searches: 1, hit_count: 3, cited: 2, verified: 1, unverified: ['handbook.pdf, p.99'] },
      }),
    ).toBe('1 search · 3 passages · 2 cited · 1 verified — unverified: handbook.pdf, p.99')
  })

  it('omits the unverified clause when every citation was verified', () => {
    expect(
      renderEventData({
        type: 'grounding_checked',
        agent: 'a',
        data: { searches: 2, hit_count: 4, cited: 2, verified: 2, unverified: [] },
      }),
    ).toBe('2 searches · 4 passages · 2 cited · 2 verified')
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run (from `ui/frontend`): `npx vitest run src/lib/traceEvents.test.ts`
Expected: FAIL — label undefined; `renderEventData` returns the JSON dump.

- [ ] **Step 3: Implement**

In `EVENT_LABELS`, after `memory_failed`:

```ts
  // Grounding-lite (core/grounding.py): a knowledge-base agent's citations
  // checked against its own searches.
  grounding_checked: '📎 grounding checked',
```

In `renderEventData`'s switch, before `case 'agent_prompt':`:

```ts
    case 'grounding_checked': {
      const searches = (data.searches as number | undefined) ?? 0
      const parts = [
        `${searches} ${searches === 1 ? 'search' : 'searches'}`,
        `${(data.hit_count as number | undefined) ?? 0} passages`,
        `${(data.cited as number | undefined) ?? 0} cited`,
        `${(data.verified as number | undefined) ?? 0} verified`,
      ]
      const unverified = (data.unverified as string[] | undefined) ?? []
      const line = parts.join(' · ')
      return unverified.length > 0 ? `${line} — unverified: ${unverified.join(', ')}` : line
    }
```

- [ ] **Step 4: Run tests, lint and build**

Run (from `ui/frontend`): `npx vitest run src/lib/traceEvents.test.ts && npm run lint && npm run build`
Expected: tests pass, lint clean, build succeeds.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/lib/traceEvents.ts ui/frontend/src/lib/traceEvents.test.ts
git commit -m "feat(frontend): label and summary line for grounding_checked trace events"
```

---

### Task 5: Documentation

**Files:**
- Modify: `docs/KNOWLEDGE_BASES.md` ("How agents use a knowledge base", line ~422, and "Known limitations", ~966)
- Modify: `src/bestteam/CLAUDE.md` (the "Both hierarchical paths force `tool_choice`" paragraph, ~line 94)
- Modify: `CLAUDE.md` (the "Knowledge bases have no external vector store" bullet, line ~97)
- Modify: `docs/STATUS.md` (top of `## Done`, line 9)
- Modify: `CHANGELOG.md` (`## [Unreleased]` → `### Added`, first bullet)

**Interfaces:** none — prose only. British spelling.

- [ ] **Step 1: `docs/KNOWLEDGE_BASES.md`**

In "How agents use a knowledge base", after the paragraph ending "…dispatches by name when the model calls one.", add:

```markdown
Two things hold the agent to the knowledge base rather than merely offering
it (grounding-lite, 2026-08-24). First, an agent that carries a knowledge-base
tool makes its **first model call with `tool_choice="required"`** on every
team mode — the same insurance a hierarchical manager and a delegated
specialist already had — so the model searches before it answers instead of
answering from what it remembers. A provider that rejects a forced
`tool_choice` (DeepSeek's thinking mode does) gets the unforced call instead,
so the worst case is today's behaviour, never a failed run. An agent whose
only tools are `web_search`, `calculator` and the like is not forced. Second,
when the turn ends, the `[source: …]` tags in the agent's final text are
**checked against the citations its own searches returned** and the result is
recorded as one `grounding_checked` trace event:

```json
{
  "searches": 1,
  "hit_count": 3,
  "cited": 2,
  "verified": 1,
  "unverified": ["handbook.pdf, p.99"]
}
```

A tag is verified when it equals a returned citation (whitespace aside), or
when it names only a filename and that document was among the hits. A tag
with a page or heading the search never returned is unverified — a fabricated
locator is exactly what this shows. The event carries counts and citation
labels only (at most ten unverified labels, each at most 200 characters), and
it **records rather than acts**: the answer is returned unchanged, nothing is
retried or refused. A knowledge-base agent that never searched (`searches:
0`) has every tag unverified. A hierarchical manager without a knowledge base
of its own is not checked — its specialists are.
```

In "Known limitations", add a bullet:

```markdown
- **Grounding is checked, not enforced.** `grounding_checked` says whether an
  answer's citations name passages that were retrieved; it does not say the
  passage supports the claim, and an unverified citation changes nothing
  about the run. Regenerating or refusing an ungrounded answer, and any
  answer-level evaluation, are not built.
```

- [ ] **Step 2: `src/bestteam/CLAUDE.md`**

Replace the opening sentence of the paragraph "Both hierarchical paths force `tool_choice=\"required\"` on an agent's **first** call — a manager's, so it delegates rather than answering from its own guesswork, and a tool-carrying subordinate's, so it actually consults the tool it was delegated to use." with:

```markdown
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
```

Keep the rest of the paragraph (the DeepSeek fallback sentences) as it is.

- [ ] **Step 3: `CLAUDE.md`**

In the "Knowledge bases have no external vector store, no DMS connectors." bullet, append one sentence at the end (after the "Embedding token counts are *estimated*…" sentence):

```markdown
  **Grounding is lite**: a knowledge-base agent's first model call is forced
  to use a tool and its answer's `[source: …]` tags are checked against its
  own hits (`grounding_checked` event, `core/grounding.py`) — recorded, never
  enforced; no grader model, no retry, no answer-level evaluation.
```

- [ ] **Step 4: `docs/STATUS.md`**

Insert at the top of `## Done` (before the "A trace's knowledge-base ids keep resolving" entry):

```markdown
- **Grounding-lite** (2026-08-24, spec
  `docs/superpowers/specs/2026-08-24-grounding-lite-design.md`). The
  2026-08-24 external review's second P0: a SEQUENTIAL/PARALLEL agent was
  merely *offered* its knowledge base. Now an agent with a knowledge-base tool
  bound (`_has_knowledge_base_tool`) has its first model call forced to
  `tool_choice="required"` on every mode, with `_first_call`'s existing
  refusal fallback, and its final text's `[source: …]` tags are compared with
  the citation labels its own searches returned (`core/grounding.py`;
  the tool reports them in full via `report_trace(citations=…)`, the trace
  event still keeps ten `sources`). One `grounding_checked` event per turn:
  `searches`, `hit_count`, `cited`, `verified`, `unverified` (≤10, ≤200
  chars each). Verified = exact label, or a bare filename among the hits; a
  page/heading the search never returned is unverified. Recorded, never
  acted on — no retry, no refusal, no `needs_attention`, no schema. Rulings:
  force only for a knowledge-base tool (not any tool); `required`, not a
  named tool; check in the adapter so SDK users get it; no read surface
  beyond the technical trace (`📎 grounding checked` label). Deferred:
  regenerating/refusing an ungrounded answer, a grader model, answer-level
  evaluation.
```

- [ ] **Step 5: `CHANGELOG.md`**

Under `## [Unreleased]` → `### Added`, insert as the first bullet:

```markdown
- **A knowledge-base agent searches before it answers, and its citations
  are checked** — an agent that has a knowledge base is now made to use a
  tool on its first model call (as a team manager already was), and each of
  its turns ends with a `grounding_checked` trace event: how many
  `[source: …]` tags the answer carries, how many name a passage the agent
  actually retrieved, and which do not. Nothing is retried or refused; the
  event is there to be read. No configuration, no migration.
```

- [ ] **Step 6: Verify and commit**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_grounding.py tests/test_trace_granularity.py -q` (a docs-only task, but the suite must still be green at the commit).

```bash
git add docs/KNOWLEDGE_BASES.md src/bestteam/CLAUDE.md CLAUDE.md docs/STATUS.md CHANGELOG.md
git commit -m "docs: grounding-lite — forced first search and the grounding_checked event"
```

---

## Self-review

- **Spec coverage.** §1 search first → Task 3 (3b, 3f). §2 checker → Task 1. §2 "citations in full, not copied into the event" → Task 2 + Task 3's `"citations" not in tool_completed.data` assertion. §3 event, position, early returns → Task 3 (3c–3e, 3h). §4 backend: nothing, by design. §5 frontend → Task 4. §6 docs → Task 5. §7 tests → Tasks 1–4.
- **Placeholders.** None; every step carries its code.
- **Type consistency.** `check_grounding(text, citations, *, searches, hit_count)` and `GroundingResult.as_trace_data()` are named identically in Tasks 1 and 3; `_has_knowledge_base_tool(agent)` in Task 3 and the docs; `report_trace(citations=[...])` in Tasks 2 and 3; the event data keys `searches/hit_count/cited/verified/unverified` in Tasks 1, 3, 4 and 5.
