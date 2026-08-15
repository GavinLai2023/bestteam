# KB hybrid retrieval + query expansion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `type: "hybrid"` knowledge base (BM25 + vector fused via
Reciprocal Rank Fusion) and add opt-in LLM query expansion
(`query_expansion_model`/`query_expansion_count`) to all three knowledge base
types (`local_folder`, `vector`, `hybrid`).

**Architecture:** Extract `core/memory.py`'s proven RRF-fusion and
query-expansion primitives into a new shared `core/fusion.py` module, add two
small generic helpers (`_rrf_retrieve`, `_query_variants`) to
`core/knowledge_base.py`, refactor `LocalFolderKnowledgeBase`/
`VectorKnowledgeBase` to route their existing single-method retrieval through
those helpers (provably byte-identical when expansion is unset), then add a
new `HybridKnowledgeBase` that reuses the same helpers with two legs (BM25 +
vector) instead of one.

**Tech Stack:** Python, `rank-bm25`, `numpy`, `langchain_core` (for the
query-expansion LLM call, resolved via the existing `_resolve_model`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-15-kb-hybrid-retrieval-design.md`

## Global Constraints

- Every new knob (`query_expansion_model`, `query_expansion_count`) must be
  **opt-in and fail-soft at query time**: unset, a bad model spec, an invoke
  error, or an unparseable response must never raise — they degrade to
  `variants = [query]` (see spec "Key facts").
- Missing `rank-bm25`/`numpy` for `type: "hybrid"` raises `ConfigurationError`
  at construction (fail-hard), matching the existing per-type guards.
- `_rerank_candidates`/`_rerank_fetch_k`/`_resolve_candidate_k` from
  `core/reranking.py` are reused **unmodified** — do not change their
  signatures or behavior.
- `tests/test_memory.py`'s `from bestteam.core.memory import
  _reciprocal_rank_fusion` import and `mgr._expand_query(...)` method calls
  must keep working, byte-identical, after the extraction into
  `core/fusion.py`.
- Unset `query_expansion_model`/`rerank_model` on `local_folder`/`vector`
  must produce **byte-for-byte identical** `query()` output to the
  pre-refactor code — this is the regression contract every existing test in
  `tests/test_knowledge_base.py`/`tests/test_vector_knowledge_base.py` must
  keep passing, unmodified, after the refactor.
- No new pyproject extra, no new `.env.example` entries, no chunk-level
  citation metadata, no per-leg RRF weighting, no weighted re-fusion after
  rerank for KB — all explicitly deferred in the spec.

---

## Task 1: Extract shared `core/fusion.py`

**Files:**
- Create: `src/bestteam/core/fusion.py`
- Test: `tests/test_fusion.py`

**Interfaces:**
- Produces: `reciprocal_rank_fusion(*ranked_id_lists: Sequence[Any], k: int = 60, weights: Optional[Sequence[float]] = None) -> Dict[Any, float]`
- Produces: `expand_query(model_spec: Any, query: str, count: int) -> tuple[List[str], Optional[Any]]`
  (returns `(expansions, raw_response_or_None)` — the raw response is handed
  back so a caller that wants usage metering, i.e. `MemoryManager`, can
  extract it; a caller that doesn't just discards it)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fusion.py`:

```python
"""Tests for the shared RRF-fusion and query-expansion primitives."""
from unittest.mock import patch

import pytest

from bestteam.core.fusion import expand_query, reciprocal_rank_fusion

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion
# ---------------------------------------------------------------------------

def test_reciprocal_rank_fusion_combines_two_lists():
    scores = reciprocal_rank_fusion(["a", "b", "c"], ["c", "a"])
    assert scores["a"] > scores["b"]
    assert scores["c"] > scores["b"]


def test_reciprocal_rank_fusion_respects_custom_k():
    assert reciprocal_rank_fusion(["a"], k=1)["a"] == pytest.approx(1 / 2)


def test_reciprocal_rank_fusion_empty_lists():
    assert reciprocal_rank_fusion([], []) == {}


def test_reciprocal_rank_fusion_weights_default_matches_unweighted():
    unweighted = reciprocal_rank_fusion(["a", "b"], ["b", "a"])
    explicit = reciprocal_rank_fusion(["a", "b"], ["b", "a"], weights=[1.0, 1.0])
    assert unweighted == explicit


def test_reciprocal_rank_fusion_weighted_favors_higher_weight_list():
    list_a = ["x", "y"]
    list_b = ["y", "x"]
    unweighted = reciprocal_rank_fusion(list_a, list_b)
    assert unweighted["x"] == pytest.approx(unweighted["y"])
    weighted = reciprocal_rank_fusion(list_a, list_b, weights=(1.0, 30.0))
    assert weighted["y"] > weighted["x"]


def test_reciprocal_rank_fusion_accepts_non_string_ids():
    # A knowledge base fuses by integer chunk index, not a string id.
    scores = reciprocal_rank_fusion([0, 2], [2, 0])
    assert scores[0] == pytest.approx(scores[2])
    assert set(scores) == {0, 2}


def test_reciprocal_rank_fusion_single_list_preserves_order():
    # Proof that routing a single-query, single-leg retrieval path through
    # fusion is order-preserving (the invariant the KB refactor depends on).
    fused = reciprocal_rank_fusion([5, 1, 9, 2])
    ranked = [idx for idx, _score in sorted(fused.items(), key=lambda p: p[1], reverse=True)]
    assert ranked == [5, 1, 9, 2]


# ---------------------------------------------------------------------------
# expand_query
# ---------------------------------------------------------------------------

def test_expand_query_unset_model_returns_empty():
    expansions, response = expand_query(None, "refund", 3)
    assert expansions == []
    assert response is None


def test_expand_query_count_zero_returns_empty():
    expansions, response = expand_query("fake:{\"queries\": [\"alt\"]}", "refund", 0)
    assert expansions == []
    assert response is None


def test_expand_query_parses_alternatives():
    expansions, response = expand_query(
        'fake:{"queries": ["money back", "reimbursement"]}', "refund", 3
    )
    assert expansions == ["money back", "reimbursement"]
    assert response is not None


def test_expand_query_dedupes_against_original_and_caps_count():
    canned = '{"queries": ["Refund", "refund ", "alt one", "alt one", "alt two", "alt three"]}'
    expansions, _response = expand_query(f"fake:{canned}", "refund", 2)
    assert expansions == ["alt one", "alt two"]


def test_expand_query_unparseable_response_degrades_gracefully():
    expansions, response = expand_query("fake:sorry, not JSON", "refund", 3)
    assert expansions == []
    # The call happened (a caller may still want to meter it), just nothing parsed.
    assert response is not None


def test_expand_query_invoke_error_returns_none_response():
    with patch(
        "bestteam.adapters.langgraph_adapter._resolve_model",
        side_effect=RuntimeError("boom"),
    ):
        expansions, response = expand_query("fake:ignored", "refund", 3)
    assert expansions == []
    assert response is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_fusion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bestteam.core.fusion'`

- [ ] **Step 3: Write `src/bestteam/core/fusion.py`**

```python
"""Shared retrieval primitives: Reciprocal Rank Fusion and LLM query
expansion.

Used by both `core/memory.py` (per-user memory hybrid/query-expansion
recall) and the knowledge base modules (`core/knowledge_base.py`,
`core/vector_knowledge_base.py`, `core/hybrid_knowledge_base.py`) so both
subsystems share one tested implementation instead of diverging copies. See
`docs/superpowers/specs/2026-08-15-kb-hybrid-retrieval-design.md`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

_logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    *ranked_id_lists: Sequence[Any], k: int = 60, weights: Optional[Sequence[float]] = None
) -> Dict[Any, float]:
    """Merge ranked-id lists into one fused score per id: the sum, across
    every list an id appears in, of ``weight / (k + rank)`` (1-based rank).
    Standard Reciprocal Rank Fusion -- rank-based, so it needs no score
    calibration between signals on different scales (BM25 vs. cosine vs. a
    cross-encoder's raw logits). `weights` defaults to `1.0` per list. Ids
    may be any hashable type -- a memory record's string id, or a knowledge
    base chunk's integer index."""
    resolved_weights = weights if weights is not None else [1.0] * len(ranked_id_lists)
    scores: Dict[Any, float] = {}
    for weight, ranked_ids in zip(resolved_weights, ranked_id_lists):
        for rank, record_id in enumerate(ranked_ids, start=1):
            scores[record_id] = scores.get(record_id, 0.0) + weight / (k + rank)
    return scores


def _parse_expansion(content: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of the expansion model's JSON reply. Tolerates
    surrounding prose/code fences by extracting the first ``{...}`` span.
    Returns None if nothing parseable is found."""
    if not content:
        return None
    text = content.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


_QUERY_EXPANSION_SYSTEM_PROMPT = (
    "You rewrite a search query into alternative phrasings that might match "
    "a differently-worded stored note/document with the same meaning "
    "(synonyms, rephrasing, a more/less formal register). Respond with ONLY "
    'a JSON object of the form {{"queries": ["...", "..."]}} containing up '
    "to {n} alternative phrasings of the given query -- NOT the original "
    "query itself, NOT an answer to it, NOT a follow-up question. Use an "
    "empty list if no useful alternative phrasing exists. No prose outside "
    "the JSON."
)


def expand_query(model_spec: Any, query: str, count: int) -> "tuple[List[str], Optional[Any]]":
    """Best-effort: up to `count` alternative phrasings of `query` from
    `model_spec`, for MultiQueryRetriever-style fused retrieval, plus the raw
    model response object (or None if no call was made / it didn't succeed)
    so a caller that wants usage metering can extract it itself -- this
    function never meters anything. NEVER raises -- any failure (no model
    configured, count<=0, invoke error, unparseable response) returns
    ``([], None)`` on invoke failure, or ``([], response)`` when the call
    succeeded but nothing parsed, so a caller always has a safe fallback to
    the literal query alone."""
    if model_spec is None or count <= 0:
        return [], None
    from langchain_core.messages import HumanMessage, SystemMessage

    # Same resolver as extraction, so "fake:" specs stay $0 in tests.
    from ..adapters.langgraph_adapter import _resolve_model

    try:
        model = _resolve_model(model_spec)
        response = model.invoke(
            [
                SystemMessage(content=_QUERY_EXPANSION_SYSTEM_PROMPT.format(n=count)),
                HumanMessage(content=f"Query: {query}"),
            ]
        )
    except Exception as exc:  # noqa: BLE001 -- no call succeeded, nothing billable
        _logger.warning(
            "Query expansion failed, falling back to the original query only: %s",
            exc,
            exc_info=True,
        )
        return [], None

    try:
        content = response.content if hasattr(response, "content") else str(response)
        parsed = _parse_expansion(content)
    except Exception as exc:  # noqa: BLE001 -- parse failure, the call still happened
        _logger.warning(
            "Query expansion response parse failed, falling back to the "
            "original query only: %s",
            exc,
            exc_info=True,
        )
        return [], response
    if not parsed or not isinstance(parsed.get("queries"), list):
        return [], response

    seen = {query.strip().lower()}
    expansions: List[str] = []
    for item in parsed["queries"]:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        expansions.append(text)
        if len(expansions) >= count:
            break
    return expansions, response
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_fusion.py -v`
Expected: PASS (all tests green)

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/fusion.py tests/test_fusion.py
git commit -m "feat(core): extract RRF fusion and query expansion into shared fusion.py"
```

---

## Task 2: Route `core/memory.py` through `core/fusion.py`

**Files:**
- Modify: `src/bestteam/core/memory.py:126-142` (remove `_reciprocal_rank_fusion` def, import alias instead), `:1114-1122` (remove `_QUERY_EXPANSION_SYSTEM_PROMPT`, now in `fusion.py`), `:1309-1373` (`_expand_query` becomes a thin wrapper)
- Test: `tests/test_memory.py` (existing — must pass unchanged, no edits)

**Interfaces:**
- Consumes: `reciprocal_rank_fusion`, `expand_query` from `bestteam.core.fusion` (Task 1)
- Produces: `bestteam.core.memory._reciprocal_rank_fusion` (import alias, same
  name/signature as before — `tests/test_memory.py:16` imports this directly)
- Produces: `MemoryManager._expand_query(self, query: str) -> tuple[List[str], Optional[Dict[str, Any]]]` (unchanged signature/behavior)

- [ ] **Step 1: Confirm the pre-refactor baseline passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_memory.py -v`
Expected: PASS (this is the baseline the refactor must not break)

- [ ] **Step 2: Add the import alias and remove the moved code**

In `src/bestteam/core/memory.py`, near the top (after the existing
`from .reranking import ...` import), add:

```python
from .fusion import expand_query as _expand_query_impl
from .fusion import reciprocal_rank_fusion as _reciprocal_rank_fusion
```

Delete the `_reciprocal_rank_fusion` function definition (the block starting
`def _reciprocal_rank_fusion(` through its closing `return scores`, currently
lines 126-142) — the import alias above replaces it, with the exact same
name so `tests/test_memory.py`'s `from bestteam.core.memory import
_reciprocal_rank_fusion` keeps resolving to a callable with identical
behavior.

Delete the `_QUERY_EXPANSION_SYSTEM_PROMPT` constant (currently lines
1114-1122) — it now lives in `fusion.py` and nothing in `memory.py` needs it
directly after Step 3.

- [ ] **Step 3: Rewrite `MemoryManager._expand_query` as a thin wrapper**

Replace the existing `_expand_query` method body (currently
`core/memory.py:1309-1373`) with:

```python
    def _expand_query(self, query: str) -> "tuple[List[str], Optional[Dict[str, Any]]]":
        """Best-effort: up to `query_expansion_count` alternative phrasings of
        `query` from `query_expansion_model`, for MultiQueryRetriever-style
        fused recall, plus that call's token-usage entry (None if unreported,
        e.g. a `fake:` model). NEVER raises -- any failure (no model
        configured, count<=0, invoke error, unparseable response) returns
        ``([], None)``, so `recall()` always has a safe fallback to the
        literal query alone and never bills for a call that didn't happen.
        Thin wrapper around `fusion.expand_query()`: adds this manager's own
        usage-metering on top of the shared expansion logic."""
        expansions, response = _expand_query_impl(
            self.query_expansion_model, query, self.query_expansion_count
        )
        usage = self._usage_entry(response, self.query_expansion_model)
        return expansions, usage
```

This preserves the exact original contract: when `query_expansion_model` is
`None` or `query_expansion_count <= 0`, `_expand_query_impl` returns
`([], None)`, and `self._usage_entry(None, ...)` returns `None` (its first
line is `usage = getattr(response, "usage_metadata", None)`, which is `None`
for a `None` response) — so the early-return path still yields `([], None)`,
identical to before.

- [ ] **Step 4: Run the tests to verify nothing broke**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_memory.py tests\test_memory_backend.py tests\test_memory_integration.py -v`
Expected: PASS, unchanged (no test file edits) — this is the regression
proof for the extraction.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/memory.py
git commit -m "refactor(core): route memory.py's RRF fusion and query expansion through fusion.py"
```

---

## Task 3: Shared `_rrf_retrieve`/`_query_variants` helpers in `core/knowledge_base.py`

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py` (add two new module-level
  functions, plus one new import line)
- Test: `tests/test_knowledge_base.py` (add a new section)

**Interfaces:**
- Consumes: `reciprocal_rank_fusion`, `expand_query` from `bestteam.core.fusion` (Task 1)
- Produces: `_rrf_retrieve(query_variants: List[str], legs: List[Callable[[str, int], List[int]]], fetch_k: int) -> List[int]`
- Produces: `_query_variants(query: str, query_expansion_model: Any, query_expansion_count: int) -> List[str]`
  (both imported by `vector_knowledge_base.py` in Task 5 and
  `hybrid_knowledge_base.py` in Task 6, the same way those modules already
  import `_load_document_chunks`/`_rerank_candidates`/`_rerank_fetch_k`/
  `_validate_chunk_params`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_knowledge_base.py` (new section, after the existing
`_rerank_candidates` section, before "LocalFolderKnowledgeBase rerank
wiring"):

```python
# ---------------------------------------------------------------------------
# _rrf_retrieve / _query_variants
# ---------------------------------------------------------------------------

from bestteam.core.knowledge_base import _query_variants, _rrf_retrieve


def test_rrf_retrieve_single_variant_single_leg_preserves_leg_order():
    def leg(query_text, fetch_k):
        return [3, 1, 2][:fetch_k]

    result = _rrf_retrieve(["q"], [leg], fetch_k=3)
    assert result == [3, 1, 2]


def test_rrf_retrieve_fuses_across_legs():
    def leg_a(query_text, fetch_k):
        return [1, 2][:fetch_k]

    def leg_b(query_text, fetch_k):
        return [2, 1][:fetch_k]

    result = _rrf_retrieve(["q"], [leg_a, leg_b], fetch_k=2)
    # Both indices appear at rank 1 once and rank 2 once -- tied, both present.
    assert set(result) == {1, 2}


def test_rrf_retrieve_fuses_across_variants():
    calls = []

    def leg(query_text, fetch_k):
        calls.append(query_text)
        return {"q": [1], "alt": [2]}.get(query_text, [])

    result = _rrf_retrieve(["q", "alt"], [leg], fetch_k=1)
    assert calls == ["q", "alt"]
    assert set(result) == {1, 2}


def test_rrf_retrieve_empty_legs_returns_empty():
    assert _rrf_retrieve(["q"], [lambda q, k: []], fetch_k=5) == []


def test_query_variants_no_expansion_model_returns_just_the_query():
    assert _query_variants("refund", None, 3) == ["refund"]


def test_query_variants_expansion_adds_alternatives():
    variants = _query_variants(
        "refund", 'fake:{"queries": ["money back"]}', 3
    )
    assert variants == ["refund", "money back"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py -k "rrf_retrieve or query_variants" -v`
Expected: FAIL with `ImportError: cannot import name '_query_variants'`

- [ ] **Step 3: Implement the helpers**

In `src/bestteam/core/knowledge_base.py`, add this import alongside the
existing `from .reranking import (...)` block:

```python
from .fusion import expand_query, reciprocal_rank_fusion
```

Add the two functions after `_load_document_chunks` and before
`_rerank_fetch_k`:

```python
def _rrf_retrieve(
    query_variants: List[str],
    legs: List[Callable[[str, int], List[int]]],
    fetch_k: int,
) -> List[int]:
    """Run every (query_variant, leg) pair -- each leg returns a ranked list
    of chunk indices capped at fetch_k -- and fuse ALL resulting lists
    (variants x legs) with one unweighted `reciprocal_rank_fusion` call.
    Returns chunk indices ordered by fused score, descending. A leg is a
    closure over one KB's own chunks/index (BM25 scoring, or cosine
    scoring); parameterizing on `(query_text, fetch_k) -> List[int]` lets
    one function serve local_folder (1 leg), vector (1 leg), and hybrid (2
    legs) identically. With exactly one variant and one leg this reproduces
    that leg's own order exactly (RRF over a single ranked list is
    order-preserving -- see the design spec's "Key facts")."""
    ranked_lists = [leg(variant, fetch_k) for variant in query_variants for leg in legs]
    fused = reciprocal_rank_fusion(*ranked_lists)
    return [idx for idx, _score in sorted(fused.items(), key=lambda pair: pair[1], reverse=True)]


def _query_variants(query: str, query_expansion_model: Any, query_expansion_count: int) -> List[str]:
    """`[query]` plus up to `query_expansion_count` LLM-generated alternative
    phrasings. Expansion failures/unset already degrade to `[]` inside
    `expand_query`, so this never raises and never returns an empty list."""
    expansions, _response = expand_query(query_expansion_model, query, query_expansion_count)
    return [query] + expansions
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py -v`
Expected: PASS (all tests, including the pre-existing ones — nothing else
was touched yet)

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "feat(core): add shared _rrf_retrieve/_query_variants helpers to knowledge_base.py"
```

---

## Task 4: `LocalFolderKnowledgeBase` gains query expansion

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py:57-122` (`LocalFolderKnowledgeBase.__init__` and `query()`)
- Test: `tests/test_knowledge_base.py` (add a new section; existing tests must keep passing unmodified)

**Interfaces:**
- Consumes: `_rrf_retrieve`, `_query_variants` (Task 3), `_rerank_fetch_k`,
  `_rerank_candidates` (existing, unmodified)
- Produces: `LocalFolderKnowledgeBase(..., query_expansion_model: Any = None, query_expansion_count: int = 3)`
- Produces: `LocalFolderKnowledgeBase._bm25_leg(self, query_text: str, fetch_k: int) -> List[int]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_knowledge_base.py`, after the existing "LocalFolderKnowledgeBase rerank wiring" section:

```python
# ---------------------------------------------------------------------------
# LocalFolderKnowledgeBase query expansion
# ---------------------------------------------------------------------------

def test_local_folder_kb_query_expansion_unset_is_byte_identical(tmp_path):
    kb = _kb_with_docs(tmp_path, "apples and oranges", "cars and trucks", top_k=2)
    assert kb.query("apples") == kb.query("apples")


def test_local_folder_kb_query_expansion_recovers_chunk_literal_query_misses(tmp_path):
    # "sprocket" shares zero significant terms with either doc, so plain BM25
    # (query_expansion unset) returns nothing. The expansion variant "widget"
    # matches doc0 -- proving fusion recovers a chunk the literal query alone
    # could never surface.
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = _kb_with_docs(plain_dir, "widget assembly instructions", "gadget repair guide")
    plain_result = plain.query("sprocket")
    assert "No results found" in plain_result

    expanded_dir = tmp_path / "expanded"
    expanded_dir.mkdir()
    expanded = _kb_with_docs(
        expanded_dir,
        "widget assembly instructions",
        "gadget repair guide",
        query_expansion_model='fake:{"queries": ["widget"]}',
    )
    expanded_result = expanded.query("sprocket")
    assert "doc0.txt" in expanded_result


def test_local_folder_kb_query_expansion_disabled_when_count_zero(tmp_path):
    kb = _kb_with_docs(
        tmp_path,
        "widget assembly instructions",
        query_expansion_model='fake:{"queries": ["widget"]}',
        query_expansion_count=0,
    )
    assert "No results found" in kb.query("sprocket")


def test_local_folder_kb_bad_query_expansion_spec_degrades_gracefully(tmp_path):
    kb = _kb_with_docs(
        tmp_path, "apples and oranges", query_expansion_model="not-a-real-spec"
    )
    # Never raises; falls back to the literal query.
    result = kb.query("apples")
    assert "doc0.txt" in result
```

(`_kb_with_docs` already exists in this file, from the rerank-wiring
section — it forwards `**kwargs` to `LocalFolderKnowledgeBase`, so no
change is needed there.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py -k "query_expansion" -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'query_expansion_model'`

- [ ] **Step 3: Implement the refactor**

In `src/bestteam/core/knowledge_base.py`, change
`LocalFolderKnowledgeBase.__init__`'s signature (currently lines 57-66) to
add the two new parameters at the end:

```python
    def __init__(
        self,
        name: str,
        path: str | Path,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        top_k: int = 5,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> None:
```

Inside the constructor body, after `self._candidate_k = _resolve_candidate_k(candidate_k, top_k)`, add:

```python
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count
```

Replace the entire `query()` method (currently lines 98-122) with:

```python
    def _bm25_leg(self, query_text: str, fetch_k: int) -> List[int]:
        """Identical scoring logic to the pre-refactor `query()` body,
        returning chunk indices instead of `(score, chunk)` tuples."""
        query_tokens = tokenize(query_text)
        query_terms = significant_terms(query_tokens)
        scores = self._bm25.get_scores(query_tokens)

        matches = [
            (len(query_terms & chunk_terms), score, idx)
            for idx, (score, chunk_terms) in enumerate(zip(scores, self._chunk_terms))
            if query_terms & chunk_terms
        ]
        matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
        return [idx for _overlap, _score, idx in matches[:fetch_k]]

    def query(self, query: str, top_k: Optional[int] = None) -> str:
        top_k = top_k or self.default_top_k
        variants = _query_variants(query, self.query_expansion_model, self.query_expansion_count)
        fetch_k = _rerank_fetch_k(top_k, self._candidate_k, self._reranker)
        ranked_indices = _rrf_retrieve(variants, [self._bm25_leg], fetch_k)
        results = [(float(-i), self._chunks[idx]) for i, idx in enumerate(ranked_indices[:fetch_k])]
        results = _rerank_candidates(query, results, self._reranker, top_k)

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"

        lines = [f"Knowledge base '{self.name}' results for: {query}\n"]
        for i, (_score, chunk) in enumerate(results, 1):
            lines.append(f"{i}. [source: {chunk.source}]")
            lines.append(chunk.text.strip())
            lines.append("")
        return "\n".join(lines)
```

Note: the final slice uses `fetch_k`, not a separately-recomputed
`candidate_k` — `fetch_k` is exactly what the pre-refactor code sliced
`matches` to before calling `_rerank_candidates`, and `_rrf_retrieve` can
return **more** than `fetch_k` unique indices once multiple query variants
are fused (each leg call is independently capped at `fetch_k`, but their
union can exceed it), so re-slicing to `fetch_k` here is required to match
`_rerank_fetch_k`'s documented contract (see
`test_local_folder_kb_rerank_honors_per_call_top_k_above_default`, which
this must keep passing).

The synthetic `float(-i)` "score" is never read numerically — verified in
the spec's "Key facts": `_rerank_candidates` only touches candidate *order*
and the reranker's own scores, never the retrieval score half of the tuple.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py -v`
Expected: PASS — **every** test in this file, including all pre-existing
chunking and rerank-wiring tests, unmodified. This is the byte-identical
regression proof.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "feat(core): add query expansion to LocalFolderKnowledgeBase"
```

---

## Task 5: `VectorKnowledgeBase` gains query expansion

**Files:**
- Modify: `src/bestteam/core/vector_knowledge_base.py`
- Test: `tests/test_vector_knowledge_base.py` (add a new section; existing tests must keep passing unmodified)

**Interfaces:**
- Consumes: `_rrf_retrieve`, `_query_variants` from `bestteam.core.knowledge_base` (Task 3)
- Produces: `VectorKnowledgeBase(..., query_expansion_model: Any = None, query_expansion_count: int = 3)`
- Produces: `VectorKnowledgeBase._vector_leg(self, query_text: str, fetch_k: int) -> List[int]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_vector_knowledge_base.py` (find/add a `_kb_with_docs`-style
helper if none exists yet, or construct directly as the existing tests in
this file do — check the file for the established pattern before adding
new tests; existing tests build documents via `(tmp_path / "doc.txt").write_text(...)`
then construct `VectorKnowledgeBase(...)` directly):

```python
# ---------------------------------------------------------------------------
# VectorKnowledgeBase query expansion
# ---------------------------------------------------------------------------

class _VocabEmbedding(Embeddings):
    """One-hot over a 2-word vocab: presence of "gadget"/"widget" only."""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        lowered = text.lower()
        return [1.0 if "gadget" in lowered else 0.0, 1.0 if "widget" in lowered else 0.0]


def test_vector_kb_query_expansion_unset_is_byte_identical(tmp_path):
    (tmp_path / "doc0.txt").write_text("gadget notes", encoding="utf-8")
    kb = VectorKnowledgeBase("kb", tmp_path, embedding_model=_VocabEmbedding(), top_k=1)
    assert kb.query("gadget") == kb.query("gadget")


def test_vector_kb_query_expansion_recovers_chunk_literal_query_misses(tmp_path):
    # score_threshold=0.5 filters out anything below a clean cosine match.
    # The literal query "sprocket" is outside the 2-word vocab (embeds to the
    # zero vector, cosine similarity 0 with everything -> filtered out
    # entirely). The expansion variant "widget" matches doc0 exactly
    # (similarity 1.0), which only fusion with the expansion leg can surface.
    (tmp_path / "doc0.txt").write_text("widget notes", encoding="utf-8")
    plain = VectorKnowledgeBase(
        "kb", tmp_path, embedding_model=_VocabEmbedding(), top_k=1, score_threshold=0.5
    )
    assert "No results found" in plain.query("sprocket")

    expanded_dir = tmp_path / "expanded"
    expanded_dir.mkdir()
    (expanded_dir / "doc0.txt").write_text("widget notes", encoding="utf-8")
    expanded = VectorKnowledgeBase(
        "kb", expanded_dir, embedding_model=_VocabEmbedding(), top_k=1,
        score_threshold=0.5, query_expansion_model='fake:{"queries": ["widget"]}',
    )
    assert "doc0.txt" in expanded.query("sprocket")


def test_vector_kb_query_expansion_disabled_when_count_zero(tmp_path):
    (tmp_path / "doc0.txt").write_text("widget notes", encoding="utf-8")
    kb = VectorKnowledgeBase(
        "kb", tmp_path, embedding_model=_VocabEmbedding(), top_k=1, score_threshold=0.5,
        query_expansion_model='fake:{"queries": ["widget"]}', query_expansion_count=0,
    )
    assert "No results found" in kb.query("sprocket")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_vector_knowledge_base.py -k "query_expansion" -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'query_expansion_model'`

- [ ] **Step 3: Implement the refactor**

In `src/bestteam/core/vector_knowledge_base.py`, update the import from
`.knowledge_base`:

```python
from .knowledge_base import (
    KnowledgeBase,
    _load_document_chunks,
    _query_variants,
    _rerank_candidates,
    _rerank_fetch_k,
    _rrf_retrieve,
    _validate_chunk_params,
)
```

Change `VectorKnowledgeBase.__init__`'s signature (currently lines 54-66) to
add the two new parameters at the end:

```python
    def __init__(
        self,
        name: str,
        path: str | Path,
        embedding_model: Any,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        cache_path: Optional[str | Path] = None,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> None:
```

Inside the constructor body, after `self._candidate_k = _resolve_candidate_k(candidate_k, top_k)`, add:

```python
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count
```

Replace the entire `query()` method (currently lines 139-172) with:

```python
    def _vector_leg(self, query_text: str, fetch_k: int) -> List[int]:
        import numpy as np

        query_vec = np.array(self._embeddings.embed_query(query_text), dtype=np.float64)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm

        scores = self._matrix @ query_vec
        k = min(fetch_k, len(scores))
        top_indices = np.argsort(scores)[::-1][:k]

        indices = [int(i) for i in top_indices]
        if self.score_threshold is not None:
            indices = [i for i in indices if scores[i] >= self.score_threshold]
        return indices

    def query(self, query: str, top_k: Optional[int] = None) -> str:
        # Mirrors LocalFolderKnowledgeBase.query()'s `top_k or self.default_top_k`:
        # a caller-supplied top_k=0 falls back to default_top_k (intentional parity).
        top_k = top_k or self.default_top_k

        variants = _query_variants(query, self.query_expansion_model, self.query_expansion_count)
        fetch_k = _rerank_fetch_k(top_k, self._candidate_k, self._reranker)
        ranked_indices = _rrf_retrieve(variants, [self._vector_leg], fetch_k)
        results = [(float(-i), self._chunks[idx]) for i, idx in enumerate(ranked_indices[:fetch_k])]
        results = _rerank_candidates(query, results, self._reranker, top_k)

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"

        lines = [f"Knowledge base '{self.name}' results for: {query}\n"]
        for i, (_score, chunk) in enumerate(results, 1):
            lines.append(f"{i}. [source: {chunk.source}]")
            lines.append(chunk.text.strip())
            lines.append("")
        return "\n".join(lines)
```

`score_threshold` filtering now happens **inside** `_vector_leg`, per call,
before its ranked list reaches fusion — same semantics as today (filter
happens before `_rerank_candidates` either way), just moved from `query()`'s
body into the leg closure so it applies per-variant.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_vector_knowledge_base.py -v`
Expected: PASS — every test in this file, including all pre-existing ones,
unmodified. This is the byte-identical regression proof.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/vector_knowledge_base.py tests/test_vector_knowledge_base.py
git commit -m "feat(core): add query expansion to VectorKnowledgeBase"
```

---

## Task 6: New `HybridKnowledgeBase`

**Files:**
- Create: `src/bestteam/core/hybrid_knowledge_base.py`
- Test: `tests/test_hybrid_knowledge_base.py`

**Interfaces:**
- Consumes: `_load_document_chunks`, `_query_variants`, `_rerank_candidates`,
  `_rerank_fetch_k`, `_rrf_retrieve`, `_validate_chunk_params` from
  `bestteam.core.knowledge_base` (Task 3); `_chunk_cache_key`,
  `_load_embedding_cache`, `_save_embedding_cache` from
  `bestteam.core.vector_knowledge_base` (existing, unmodified)
- Produces: `HybridKnowledgeBase` (implements the `KnowledgeBase` ABC,
  `type: "hybrid"` — wired into the loader in Task 7)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hybrid_knowledge_base.py`:

```python
"""Tests for the hybrid (BM25 + vector, RRF-fused) knowledge base."""
from typing import List
from unittest.mock import patch

import pytest
from langchain_core.embeddings import Embeddings

from bestteam.core.hybrid_knowledge_base import HybridKnowledgeBase
from bestteam.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


class _ConceptEmbedding(Embeddings):
    """Embeds by [refund-concept, shipping-concept] presence -- a chunk
    saying "money back" scores highly on the refund concept despite sharing
    zero literal words with the query "refund", modeling real semantic
    search (the motivating example in core/CLAUDE.md)."""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        lowered = text.lower()
        refund = 1.0 if ("refund" in lowered or "money back" in lowered) else 0.0
        shipping = 1.0 if "shipping" in lowered else 0.0
        return [refund, shipping]


def _kb_with_docs(tmp_path, *texts, **kwargs):
    for i, text in enumerate(texts):
        (tmp_path / f"doc{i}.txt").write_text(text, encoding="utf-8")
    kwargs.setdefault("embedding_model", _ConceptEmbedding())
    return HybridKnowledgeBase("kb", tmp_path, **kwargs)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------

def test_hybrid_kb_raises_without_bm25(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with patch.dict("sys.modules", {"rank_bm25": None}):
        with pytest.raises(ConfigurationError, match="rank-bm25"):
            HybridKnowledgeBase("kb", tmp_path, embedding_model=_ConceptEmbedding())


def test_hybrid_kb_raises_without_numpy(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with patch.dict("sys.modules", {"numpy": None}):
        with pytest.raises(ConfigurationError, match="numpy"):
            HybridKnowledgeBase("kb", tmp_path, embedding_model=_ConceptEmbedding())


def test_hybrid_kb_raises_for_empty_folder(tmp_path):
    with pytest.raises(ConfigurationError, match="no readable documents"):
        HybridKnowledgeBase("kb", tmp_path, embedding_model=_ConceptEmbedding())


def test_hybrid_kb_candidate_k_rejects_below_top_k(tmp_path):
    with pytest.raises(ConfigurationError, match="candidate_k"):
        _kb_with_docs(tmp_path, "hello world", top_k=5, candidate_k=2)


def test_hybrid_kb_candidate_k_rejects_above_max(tmp_path):
    with pytest.raises(ConfigurationError, match="candidate_k"):
        _kb_with_docs(tmp_path, "hello world", top_k=5, candidate_k=500)


# ---------------------------------------------------------------------------
# Fusion recovery: hybrid finds what BM25-only cannot
# ---------------------------------------------------------------------------

def test_hybrid_recovers_semantically_relevant_chunk_bm25_would_miss(tmp_path):
    docs = (
        "Our shipping policy covers two-day delivery for all orders.",
        "If you are not satisfied, we offer money back within 30 days.",
        "General terms and conditions apply to all purchases.",
    )

    from bestteam.core.knowledge_base import LocalFolderKnowledgeBase

    bm25_only_dir = tmp_path / "bm25_only"
    bm25_only_dir.mkdir()
    for i, text in enumerate(docs):
        (bm25_only_dir / f"doc{i}.txt").write_text(text, encoding="utf-8")
    bm25_only = LocalFolderKnowledgeBase("kb", bm25_only_dir, top_k=1)
    assert "No results found" in bm25_only.query("refund")

    hybrid_dir = tmp_path / "hybrid"
    hybrid_dir.mkdir()
    hybrid = _kb_with_docs(hybrid_dir, *docs, top_k=1)
    result = hybrid.query("refund")
    assert "doc1.txt" in result


# ---------------------------------------------------------------------------
# Rerank / query expansion integration
# ---------------------------------------------------------------------------

def test_hybrid_kb_rerank_unset_is_byte_identical(tmp_path):
    kb = _kb_with_docs(tmp_path, "shipping info", "money back guarantee", top_k=2)
    assert kb.query("shipping") == kb.query("shipping")


def test_hybrid_kb_rerank_changes_result_order(tmp_path):
    docs = ("fruit " * 1, "fruit " * 20, "banana orange grape melon")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = _kb_with_docs(plain_dir, *docs, top_k=1, candidate_k=2)
    reranked_dir = tmp_path / "reranked"
    reranked_dir.mkdir()
    reranked = _kb_with_docs(reranked_dir, *docs, top_k=1, candidate_k=2, rerank_model="fake:")
    assert plain.query("fruit") != reranked.query("fruit")


def test_hybrid_kb_query_expansion_recovers_chunk_literal_query_misses(tmp_path):
    # "sprocket" matches neither the BM25 leg (no shared significant terms)
    # nor the vector leg (zero-vector embedding, similarity 0 with either
    # concept dimension) for doc1 ("money back"). The expansion variant
    # "refund" matches doc1 on both legs.
    docs = ("shipping info only", "if unsatisfied, money back guaranteed")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = _kb_with_docs(plain_dir, *docs, top_k=1)
    assert "doc1.txt" not in plain.query("sprocket")

    expanded_dir = tmp_path / "expanded"
    expanded_dir.mkdir()
    expanded = _kb_with_docs(
        expanded_dir, *docs, top_k=1, query_expansion_model='fake:{"queries": ["refund"]}'
    )
    assert "doc1.txt" in expanded.query("sprocket")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_hybrid_knowledge_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bestteam.core.hybrid_knowledge_base'`

- [ ] **Step 3: Implement `src/bestteam/core/hybrid_knowledge_base.py`**

```python
"""Hybrid BM25 + vector knowledge base: fuses both retrieval methods via
Reciprocal Rank Fusion so results neither method alone would surface (e.g. a
semantically relevant chunk with zero keyword overlap) can still appear. See
`docs/superpowers/specs/2026-08-15-kb-hybrid-retrieval-design.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from ..exceptions import ConfigurationError
from .embeddings import normalize_rows, resolve_embedding_model
from .knowledge_base import (
    KnowledgeBase,
    _load_document_chunks,
    _query_variants,
    _rerank_candidates,
    _rerank_fetch_k,
    _rrf_retrieve,
    _validate_chunk_params,
)
from .reranking import _MAX_RERANK_CANDIDATE_K, _resolve_candidate_k, resolve_reranker
from .text_tokenize import significant_terms, tokenize
from .vector_knowledge_base import _chunk_cache_key, _load_embedding_cache, _save_embedding_cache


class HybridKnowledgeBase(KnowledgeBase):
    """A knowledge base backed by a folder of documents, indexed with BOTH
    BM25 keyword search and embeddings, fused via Reciprocal Rank Fusion.

    Recovers chunks a single retrieval method would miss (e.g. a
    semantically relevant chunk with zero keyword overlap with the query, or
    a keyword match an embedding model scores as only weakly similar) --
    reranking (opt-in) can only re-order what a retrieval pass already
    surfaced, so widening that pass matters more here than for either
    single-method type.
    """

    def __init__(
        self,
        name: str,
        path: str | Path,
        embedding_model: Any,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        cache_path: Optional[str | Path] = None,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
    ) -> None:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ConfigurationError(
                "Hybrid knowledge bases require the 'rank-bm25' package. "
                "Install it with: pip install 'bestteam[tools-rag]'"
            ) from exc
        try:
            import numpy as np
        except ImportError as exc:
            raise ConfigurationError(
                "Hybrid knowledge bases require the 'numpy' package. "
                "Install it with: pip install 'bestteam[tools-rag-vector]'"
            ) from exc

        _validate_chunk_params(name, chunk_size, chunk_overlap)

        self.name = name
        self.path = Path(path)
        self.default_top_k = top_k
        self.score_threshold = score_threshold
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)

        self._chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        if not self._chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents in {self.path}"
            )

        self._chunk_tokens = [tokenize(chunk.text) for chunk in self._chunks]
        self._chunk_terms = [significant_terms(tokens) for tokens in self._chunk_tokens]
        self._bm25 = BM25Okapi(self._chunk_tokens)

        self._embeddings = resolve_embedding_model(embedding_model)
        vectors = self._embed_chunks(embedding_model, cache_path)
        if not vectors or len(vectors) != len(self._chunks):
            raise ConfigurationError(
                f"Knowledge base '{name}': embedding model returned "
                f"{len(vectors)} vectors for {len(self._chunks)} chunks"
            )
        matrix = np.array(vectors, dtype=np.float64)
        self._matrix = normalize_rows(matrix)

    def _embed_chunks(self, embedding_model: Any, cache_path: Optional[str | Path]) -> List[List[float]]:
        # Identical to VectorKnowledgeBase._embed_chunks -- same cache
        # helpers, same behavior, so hybrid gets the same cache_path support.
        import warnings

        texts = [c.text for c in self._chunks]

        if cache_path is None:
            return self._embeddings.embed_documents(texts)

        if not isinstance(embedding_model, str):
            warnings.warn(
                f"Knowledge base '{self.name}': cache_path is set but "
                "embedding_model is not a string spec, so it has no stable "
                "cache key — caching is skipped.",
                stacklevel=3,
            )
            return self._embeddings.embed_documents(texts)

        cache_path = Path(cache_path)
        model_spec = embedding_model
        cache = _load_embedding_cache(cache_path, model_spec)

        keys = [_chunk_cache_key(model_spec, text) for text in texts]
        missing = [i for i, key in enumerate(keys) if key not in cache]

        if missing:
            new_vectors = self._embeddings.embed_documents([texts[i] for i in missing])
            for i, vector in zip(missing, new_vectors):
                cache[keys[i]] = vector
            _save_embedding_cache(cache_path, model_spec, cache)
        elif not cache_path.exists():
            _save_embedding_cache(cache_path, model_spec, cache)

        return [cache[key] for key in keys]

    def _bm25_leg(self, query_text: str, fetch_k: int) -> List[int]:
        query_tokens = tokenize(query_text)
        query_terms = significant_terms(query_tokens)
        scores = self._bm25.get_scores(query_tokens)

        matches = [
            (len(query_terms & chunk_terms), score, idx)
            for idx, (score, chunk_terms) in enumerate(zip(scores, self._chunk_terms))
            if query_terms & chunk_terms
        ]
        matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
        return [idx for _overlap, _score, idx in matches[:fetch_k]]

    def _vector_leg(self, query_text: str, fetch_k: int) -> List[int]:
        import numpy as np

        query_vec = np.array(self._embeddings.embed_query(query_text), dtype=np.float64)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm

        scores = self._matrix @ query_vec
        k = min(fetch_k, len(scores))
        top_indices = np.argsort(scores)[::-1][:k]

        indices = [int(i) for i in top_indices]
        if self.score_threshold is not None:
            indices = [i for i in indices if scores[i] >= self.score_threshold]
        return indices

    def query(self, query: str, top_k: Optional[int] = None) -> str:
        top_k = top_k or self.default_top_k
        variants = _query_variants(query, self.query_expansion_model, self.query_expansion_count)
        fetch_k = _rerank_fetch_k(top_k, self._candidate_k, self._reranker)
        ranked_indices = _rrf_retrieve(variants, [self._bm25_leg, self._vector_leg], fetch_k)
        results = [(float(-i), self._chunks[idx]) for i, idx in enumerate(ranked_indices[:fetch_k])]
        results = _rerank_candidates(query, results, self._reranker, top_k)

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"

        lines = [f"Knowledge base '{self.name}' results for: {query}\n"]
        for i, (_score, chunk) in enumerate(results, 1):
            lines.append(f"{i}. [source: {chunk.source}]")
            lines.append(chunk.text.strip())
            lines.append("")
        return "\n".join(lines)
```

Two legs, equal-weighted in the RRF fusion (no per-leg weight knob — see the
design spec's "Deferred"). Both indexes are built over the identical
`self._chunks`, so fusing the BM25-leg and vector-leg ranked-index lists
needs no id scheme beyond that shared array position.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_hybrid_knowledge_base.py -v`
Expected: PASS (all tests). If a specific assertion's exact wording doesn't
match your tokenizer's actual `significant_terms` behavior (e.g. a chosen
test word turns out not to be treated as significant), adjust that test's
vocabulary word and re-run — the fixture design (asymmetric "one query has
zero overlap on a leg, the other has a clean match") is what matters, not
the specific words chosen.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/hybrid_knowledge_base.py tests/test_hybrid_knowledge_base.py
git commit -m "feat(core): add HybridKnowledgeBase (BM25 + vector, RRF-fused)"
```

---

## Task 7: Wire `hybrid` into the loader and `KnowledgeBaseSpec`

**Files:**
- Modify: `src/bestteam/core/loader.py:11-19` (import + `_KNOWLEDGE_BASE_TYPES`)
- Modify: `src/bestteam/core/specification.py:145-194` (`KnowledgeBaseSpec`)
- Test: `tests/test_specification.py` (add new tests)
- Test: `tests/test_crud_api.py` (add one smoke test)

**Interfaces:**
- Consumes: `HybridKnowledgeBase` from `bestteam.core.hybrid_knowledge_base` (Task 6)
- Produces: `_KNOWLEDGE_BASE_TYPES["hybrid"]` resolvable by `core/loader.py::_build_knowledge_base`
- Produces: `KnowledgeBaseSpec.query_expansion_model: Optional[str] = None`, `KnowledgeBaseSpec.query_expansion_count: int = 3`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_specification.py`, after `test_knowledge_base_spec_rerank_fields_omitted_when_unset`:

```python
def test_knowledge_base_spec_query_expansion_emitted_for_every_type():
    for kb_type, extra in [("local_folder", {}), ("vector", {"embedding_model": "fake:8"}), ("hybrid", {"embedding_model": "fake:8"})]:
        spec = KnowledgeBaseSpec(
            name="kb", path="./docs", type=kb_type,
            query_expansion_model="fake:{}", query_expansion_count=2, **extra,
        )
        raw = spec.to_raw()
        assert raw["query_expansion_model"] == "fake:{}"
        assert raw["query_expansion_count"] == 2


def test_knowledge_base_spec_query_expansion_defaults():
    spec = KnowledgeBaseSpec(name="kb", path="./docs")
    raw = spec.to_raw()
    assert raw["query_expansion_model"] is None
    assert raw["query_expansion_count"] == 3


def test_knowledge_base_spec_hybrid_emits_vector_only_fields():
    spec = KnowledgeBaseSpec(
        name="kb", path="./docs", type="hybrid",
        embedding_model="fake:8", score_threshold=0.5, cache_path="./cache.json",
    )
    raw = spec.to_raw()
    assert raw["embedding_model"] == "fake:8"
    assert raw["score_threshold"] == 0.5
    assert raw["cache_path"] == "./cache.json"


def test_validate_specification_accepts_hybrid_knowledge_base(tmp_path):
    from bestteam.core.specification import AgentSpec, Specification, TeamSpec, WorkflowSpec
    from bestteam.core.specification import KnowledgeBaseSpec as KBSpec

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.txt").write_text("hello world", encoding="utf-8")

    spec = Specification(
        name="wf",
        knowledge_bases=[
            KBSpec(name="kb", path=str(docs_dir), type="hybrid", embedding_model="fake:8")
        ],
        agents=[AgentSpec(name="a", role="r", goal="g", model="fake:done", tools=["kb"])],
        teams=[TeamSpec(name="t", agents=["a"])],
        workflow=WorkflowSpec(steps=["t"]),
    )

    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml")
    assert workflow.name == "wf"
```

(If `validate_specification`/`Specification`/`AgentSpec`/`TeamSpec`/
`WorkflowSpec` aren't already imported at the top of
`tests/test_specification.py`, check the existing imports first — this file
already has tests like `test_validate_specification_rejects_hierarchical_team_without_manager`
using the same helpers, so they should already be in scope; only add
imports that are actually missing.)

Add to `tests/test_crud_api.py`, after `test_knowledge_base_put_omits_vector_only_fields_for_local_folder`:

```python
def test_knowledge_base_put_allows_hybrid_type_with_vector_fields(client):
    config = {"path": "./docs", "type": "hybrid", "embedding_model": "fake:8"}

    resp = client.put("/api/config/knowledge_bases/docs?org=default", json=config)

    assert resp.status_code == 200
    assert resp.json()["config"]["embedding_model"] == "fake:8"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_specification.py tests\test_crud_api.py -k "hybrid or query_expansion" -v`
Expected: FAIL — `test_knowledge_base_spec_query_expansion_*` fail with
`TypeError`/`KeyError` (fields don't exist yet); the `hybrid` tests fail with
`ConfigurationError: unknown type 'hybrid'`.

- [ ] **Step 3: Wire the loader**

In `src/bestteam/core/loader.py`, add the import and dict entry:

```python
from .hybrid_knowledge_base import HybridKnowledgeBase
from .knowledge_base import KnowledgeBase, LocalFolderKnowledgeBase, make_knowledge_base_tool
from .team import CollaborationMode, Team
from .vector_knowledge_base import VectorKnowledgeBase
from .workflow import Workflow

_KNOWLEDGE_BASE_TYPES = {
    "local_folder": LocalFolderKnowledgeBase,
    "vector": VectorKnowledgeBase,
    "hybrid": HybridKnowledgeBase,
}
```

(Keep the existing import ordering/alphabetization style already in this
file — insert `from .hybrid_knowledge_base import HybridKnowledgeBase` where
it sorts alphabetically among the existing `from .` imports.)

- [ ] **Step 4: Wire `KnowledgeBaseSpec`**

In `src/bestteam/core/specification.py`, add the two new fields to
`KnowledgeBaseSpec` (after `candidate_k`):

```python
    rerank_model: Optional[str] = None
    candidate_k: Optional[int] = None
    query_expansion_model: Optional[str] = None
    query_expansion_count: int = 3
```

Replace `to_raw()` (currently lines 174-194) with:

```python
    def to_raw(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k": self.top_k,
            "query_expansion_model": self.query_expansion_model,
            "query_expansion_count": self.query_expansion_count,
        }
        if self.rerank_model is not None:
            raw["rerank_model"] = self.rerank_model
        if self.candidate_k is not None:
            raw["candidate_k"] = self.candidate_k
        if self.type in ("vector", "hybrid"):
            if self.embedding_model is not None:
                raw["embedding_model"] = self.embedding_model
            if self.score_threshold is not None:
                raw["score_threshold"] = self.score_threshold
            if self.cache_path is not None:
                raw["cache_path"] = self.cache_path
        return raw
```

`query_expansion_model`/`query_expansion_count` are always emitted (unlike
`embedding_model` et al.) because they're valid constructor kwargs for
**every** KB type — `LocalFolderKnowledgeBase`/`VectorKnowledgeBase`/
`HybridKnowledgeBase` all now accept them (Tasks 4-6), and their defaults
(`None`, `3`) match what an omitted kwarg would resolve to, so always
emitting them is behavior-neutral.

Also update the class's docstring (currently lines 145-154) to mention the
`hybrid` type and that query expansion applies to all three — this is a
one-line docstring edit, not a behavior change:

```python
class KnowledgeBaseSpec(BaseModel):
    """Mirrors a `knowledge_bases:` entry in the loader's raw dict (see `core/loader.py`).

    `embedding_model`/`score_threshold`/`cache_path` only apply to
    `type: vector` or `type: hybrid` -- `to_raw()` omits them for
    `local_folder` so an architect that sets them on the wrong type doesn't
    trigger an unexpected `TypeError` from the knowledge base constructor.
    `rerank_model`/`candidate_k`/`query_expansion_model`/
    `query_expansion_count` apply to ALL THREE types (retrieval-method-agnostic).
    """
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_specification.py tests\test_crud_api.py tests\test_loader.py -v`
Expected: PASS (all tests in these files, including pre-existing ones).

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/loader.py src/bestteam/core/specification.py tests/test_specification.py tests/test_crud_api.py
git commit -m "feat(core): wire hybrid knowledge base type and query expansion into loader/specification"
```

---

## Task 8: Docs and demo workflow

**Files:**
- Modify: `src/bestteam/core/CLAUDE.md`
- Create: `ui/backend/workflows/hybrid_knowledge_base_demo.yaml`

**Interfaces:**
- Consumes: `HybridKnowledgeBase` (Task 6), `ui/backend/workflows/kb_docs/`
  (existing fixture docs, already used by `vector_knowledge_base_demo.yaml`)

- [ ] **Step 1: Add the demo workflow**

Create `ui/backend/workflows/hybrid_knowledge_base_demo.yaml`:

```yaml
# Single-agent support workflow backed by a hybrid (BM25 + vector) knowledge
# base. "support_agent" can call the "company_docs" tool, which fuses BM25
# keyword search with cosine-similarity search via Reciprocal Rank Fusion --
# so it can find a document via a shared keyword OR a semantically related
# phrase with no shared keywords, whichever method's index has it.
# Uses "fake:" specs for both the model and the embeddings so the dashboard
# demo runs with zero cost / no API key.

name: hybrid_knowledge_base_demo

knowledge_bases:
  - name: company_docs
    type: hybrid
    path: ./kb_docs
    embedding_model: "fake:8"

agents:
  - name: support_agent
    role: Customer Support Specialist
    goal: Answer customer questions using the company's policy documents
    backstory: Always checks the knowledge base before answering
    model: "fake:Based on our policy documents, here is the answer to your question."
    tools: [company_docs]

teams:
  - name: support_team
    agents: [support_agent]
    mode: sequential

workflow:
  steps: [support_team]
```

- [ ] **Step 2: Verify it loads and runs**

Run: `.\.venv\Scripts\python.exe -m bestteam run ui\backend\workflows\hybrid_knowledge_base_demo.yaml "What is your shipping policy?"`
Expected: Completes without error, printing the fake model's canned response
(confirms `load_workflow` resolves `type: hybrid` and the tool call
succeeds end-to-end).

- [ ] **Step 3: Update `src/bestteam/core/CLAUDE.md`**

In the "Knowledge bases" section (around the existing bullet list of KB
`type:`s), add a third bullet after the existing `vector` one:

```markdown
- `hybrid` (`core/hybrid_knowledge_base.py`): indexes chunks with BOTH BM25
  and embeddings, fusing the two rankings via Reciprocal Rank Fusion so a
  chunk either method alone would miss (e.g. a semantically relevant chunk
  with zero keyword overlap with the query) can still surface. Requires
  both `pip install 'bestteam[tools-rag,tools-rag-vector]'`. See
  `ui/backend/workflows/hybrid_knowledge_base_demo.yaml`.
```

Replace the "Known limitation: vector knowledge base retrieval is
single-stage" section's opening paragraph (the part that says "no query
rewriting/expansion for KB (see Memory's `query_expansion_model`, below, for
a possible pattern to mirror)") with a paragraph documenting that this is
now implemented:

```markdown
## Query expansion (opt-in, all three KB types)

All three KB types accept `query_expansion_model`/`query_expansion_count`
(same spec-string convention and MultiQueryRetriever-style behavior as
Memory's `query_expansion_model` -- see "Per-user memory", below): when set,
`query()` rewrites the query into up to `query_expansion_count` alternative
phrasings via one LLM call, searches with the literal query plus every
alternative, and fuses the per-variant ranked results with Reciprocal Rank
Fusion (`core/fusion.py`, shared with Memory) before slicing to `top_k`.
Unset (the default) -> `query()` is byte-for-byte unchanged. A bad spec /
invoke error / unparseable response degrades to searching the literal query
alone -- a query never fails because expansion failed. **This call's cost is
unmetered**: KB tools run inside the agent's generic tool-calling loop
(`adapters/langgraph_adapter.py`), which has no hook to report a nested LLM
call's token usage back to the backend (unlike Memory's recall, which runs
at the `Workflow.stream()` orchestration layer) -- the same pre-existing gap
`VectorKnowledgeBase`'s embedding calls already have. See
`docs/superpowers/specs/2026-08-15-kb-hybrid-retrieval-design.md`.
```

Leave the rest of the "Known limitation" section (chunking, no external
vector store, no DMS connector, reranking paragraph) untouched — only the
query-expansion sentence changes, since that limitation is now closed.

- [ ] **Step 4: Update the root `CLAUDE.md`'s known-limitations bullet**

In `C:\Projects\MyBestTeam\CLAUDE.md`, find the bullet:

```markdown
- **Vector knowledge base retrieval has no query rewriting/expansion, no
  external vector store, no DMS connectors** (reranking is available,
  opt-in, for both knowledge base types via `rerank_model`/`candidate_k` —
  `core/reranking.py`) — see `src/bestteam/core/CLAUDE.md`.
```

Replace it with:

```markdown
- **Knowledge bases have no external vector store, no DMS connectors.**
  Three types are supported: `local_folder` (BM25), `vector` (cosine), and
  `hybrid` (BM25 + vector, RRF-fused). All three support opt-in query
  expansion (`query_expansion_model`/`query_expansion_count`, MultiQueryRetriever-style,
  unmetered) and opt-in reranking (`rerank_model`/`candidate_k`) —
  see `src/bestteam/core/CLAUDE.md`.
```

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/CLAUDE.md CLAUDE.md ui/backend/workflows/hybrid_knowledge_base_demo.yaml
git commit -m "docs(core): document hybrid KB type and query expansion; add demo workflow"
```

---

## Task 9: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: PASS, zero failures, zero new skips beyond pre-existing
`optional`-marked ones (e.g. `sentence-transformers` not installed).

- [ ] **Step 2: Run the KB-specific and memory-specific files explicitly, verbose**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_fusion.py tests\test_knowledge_base.py tests\test_vector_knowledge_base.py tests\test_hybrid_knowledge_base.py tests\test_memory.py tests\test_memory_backend.py tests\test_memory_integration.py tests\test_specification.py tests\test_crud_api.py tests\test_loader.py -v`
Expected: PASS, all green — this is the consolidated regression + new-feature
proof for everything this plan touched.

- [ ] **Step 3: Re-run the end-to-end hybrid demo**

Run: `.\.venv\Scripts\python.exe -m bestteam run ui\backend\workflows\hybrid_knowledge_base_demo.yaml "What is your shipping policy?"`
Expected: Completes without error.

- [ ] **Step 4: Re-run the Mermaid graph command as a loader sanity check**

Run: `.\.venv\Scripts\python.exe -m bestteam graph ui\backend\workflows\hybrid_knowledge_base_demo.yaml`
Expected: Renders without error (confirms `type: hybrid` doesn't break the
`graph` command's own `load_workflow()` call).

- [ ] **Step 5: Report status**

No commit for this task (verification-only) — if anything fails, return to
the relevant task above, fix it there, and re-run this task's steps from the
top.
