# Pluggable Rerank for Knowledge Bases and Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in cross-encoder rerank stage to both knowledge base
types (`local_folder`, `vector`) and to `MemoryManager`'s recall, byte-for-
byte unchanged when unconfigured.

**Architecture:** A new `core/reranking.py` module provides a `Reranker` ABC
and `resolve_reranker(spec)` (mirrors `core/embeddings.py::
resolve_embedding_model`'s spec-string convention: `"fake:"` for $0
deterministic tests, `"cross-encoder:<model-name>"` for a real local
`sentence-transformers.CrossEncoder`, process-level cached). Both KB classes
fetch `candidate_k` candidates instead of `top_k`, rerank, then truncate.
`MemoryManager._fused_search` fetches `candidate_k` per query variant, fuses
via the existing RRF, caps to `candidate_k`, reranks against the literal
query only, then re-fuses the pre-rerank and rerank orderings via a
*weighted* RRF (`weights=(1.0, 8.0)`) so the reranker's signal isn't diluted
by the existing recency/hybrid signal. KB fails hard at construction
(`ConfigurationError`, matches `embedding_model`); Memory fails soft, lazily,
per `MemoryManager` instance (matches `query_expansion_model`). Both fall
back to pre-rerank ordering on a query-time inference error.

**Tech Stack:** Python 3.10+, `sentence-transformers` (new optional dep, via
a new `bestteam[tools-rerank]` extra), existing `rank-bm25`/`numpy` already
in play. No new backend/DB/API dependency.

## Global Constraints

- Full spec: `docs/superpowers/specs/2026-08-12-pluggable-rerank-design.md`
  — every task below implements a section of it; consult it for the "why"
  behind any decision that looks arbitrary here.
- **Unconfigured = byte-identical.** Every existing test must keep passing
  unmodified. `rerank_model=None` (the default everywhere) must produce
  identical output to pre-change code, at every layer.
- `resolve_reranker()` never decides fail-hard vs. fail-soft — it always
  raises `ConfigurationError` uniformly on a bad spec/missing dependency.
  Callers (KB constructors vs. `MemoryManager._get_reranker`) decide.
- `_MAX_RERANK_CANDIDATE_K = 100`; `_RERANK_RRF_WEIGHT = 8.0` — both are
  internal constants in this pass, not customer-facing config. `8.0` (not the
  originally-planned `2.0`) is deliberate: see Task 11's implementation for
  the break-even math showing `2.0` barely nudges the fused order, while a
  much larger weight (~15-40+) would make rerank order win almost
  unconditionally and quietly defeat the point of re-fusing with the
  pre-rerank order at all.
- Rerank is always scored against the **literal, unexpanded** query — never
  a query-expansion variant.
- New dependency: `sentence-transformers>=2.2` behind `bestteam[tools-rerank]`
  in `pyproject.toml` — **not** added to the aggregate `tools` extra (it
  pulls in PyTorch; stays an explicit, deliberate opt-in like the others but
  not bundled with the lighter tools).
- Test env: `sentence-transformers` is not installed in the dev venv by
  default — any test that exercises a real `CrossEncoder` load must use
  `pytest.importorskip("sentence_transformers")` and mock `CrossEncoder`
  itself (never download a real model in tests), matching
  `test_knowledge_base_skips_corrupt_file_with_warning`'s
  `pytest.importorskip("docx")` precedent.
- Run tests with `.\.venv\Scripts\python.exe -m pytest <path> -v` (Windows
  venv, per root `CLAUDE.md`).

---

### Task 1: `core/reranking.py` — `Reranker` ABC, contract validation, `_FakeReranker`

**Files:**
- Create: `src/bestteam/core/reranking.py`
- Test: `tests/test_reranking.py` (new)

**Interfaces:**
- Produces: `Reranker` (ABC, public method `score(query: str, texts:
  Sequence[str]) -> List[float]`, abstract `_score(query, texts) ->
  Sequence[float]` for subclasses), `_FakeReranker` (concrete, `-abs(len(text)
  - len(query))` scoring), `_RerankScoringError` (module-private
  `RuntimeError` subclass).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reranking.py
"""Tests for pluggable rerank (core/reranking.py)."""
import pytest

from bestteam.core.reranking import Reranker, _FakeReranker, _RerankScoringError


class _StubReranker(Reranker):
    def __init__(self, scores):
        self._scores = scores

    def _score(self, query, texts):
        return self._scores


def test_score_empty_texts_returns_empty_without_calling_score_impl():
    calls = []

    class _SpyReranker(Reranker):
        def _score(self, query, texts):
            calls.append(texts)
            return []

    assert _SpyReranker().score("q", []) == []
    assert calls == []


def test_score_rejects_count_mismatch():
    reranker = _StubReranker([1.0, 2.0])  # 2 scores for 3 texts
    with pytest.raises(_RerankScoringError, match="2 scores for 3 texts"):
        reranker.score("q", ["a", "b", "c"])


def test_score_rejects_non_finite():
    reranker = _StubReranker([1.0, float("nan")])
    with pytest.raises(_RerankScoringError, match="non-finite"):
        reranker.score("q", ["a", "b"])

    reranker = _StubReranker([1.0, float("inf")])
    with pytest.raises(_RerankScoringError, match="non-finite"):
        reranker.score("q", ["a", "b"])


def test_score_returns_floats():
    reranker = _StubReranker([1, 2])  # ints, must coerce to float
    scores = reranker.score("q", ["a", "b"])
    assert scores == [1.0, 2.0]
    assert all(isinstance(s, float) for s in scores)


def test_fake_reranker_scores_by_length_distance():
    reranker = _FakeReranker()
    scores = reranker.score("abc", ["abc", "abcdefgh", "ab"])
    # Exact match (len 3 vs query len 3) scores highest (0); farther lengths score lower.
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]


def test_fake_reranker_does_not_call_model_for_empty_input():
    assert _FakeReranker().score("q", []) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bestteam.core.reranking'`

- [ ] **Step 3: Write the implementation**

```python
# src/bestteam/core/reranking.py
"""Pluggable rerank for knowledge bases and per-user memory.

Mirrors `core/embeddings.py::resolve_embedding_model`'s shape: a spec string
("fake:" or "cross-encoder:<model-name>") or a live `Reranker` instance,
resolved via `resolve_reranker()`. This module never decides fail-hard vs.
fail-soft on a resolution failure -- callers do (knowledge bases raise at
construction; `MemoryManager` catches and disables rerank for that run). See
`docs/superpowers/specs/2026-08-12-pluggable-rerank-design.md`.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import List, Sequence


class _RerankScoringError(RuntimeError):
    """A `Reranker` subclass violated the `score()` contract (wrong count,
    non-finite value). Callers catch this alongside any other scoring
    exception (e.g. a model inference crash) with one try/except -- it is
    not part of the public `bestteam.exceptions` hierarchy."""


class Reranker(ABC):
    """Scores (query, text) pairs for relevance -- higher is more relevant.

    `score()` is the public entry point every caller uses: it validates
    `_score()`'s output (right count, all finite) so a caller only needs one
    try/except regardless of which failure mode fired -- a contract
    violation and a model inference crash look the same from the outside.
    """

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        if not texts:
            return []
        raw = list(self._score(query, list(texts)))
        if len(raw) != len(texts):
            raise _RerankScoringError(
                f"reranker returned {len(raw)} scores for {len(texts)} texts"
            )
        scores = [float(s) for s in raw]
        if any(not math.isfinite(s) for s in scores):
            raise _RerankScoringError("reranker returned a non-finite score")
        return scores

    @abstractmethod
    def _score(self, query: str, texts: List[str]) -> Sequence[float]:
        """Return one relevance score per text. Do not validate count or
        finiteness here -- `score()` already does."""


class _FakeReranker(Reranker):
    """Deterministic, $0, no model download: scores by
    `-abs(len(text) - len(query))`. Deliberately unrelated to BM25/cosine
    scoring, so a test can assert reranking actually changed the order
    rather than coincidentally reproducing retrieval order."""

    def _score(self, query: str, texts: List[str]) -> Sequence[float]:
        return [-abs(len(text) - len(query)) for text in texts]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/reranking.py tests/test_reranking.py
git commit -m "feat(reranking): add Reranker ABC with contract-validating score()"
```

---

### Task 2: `resolve_reranker()` — instance passthrough, `"fake:"`, invalid-spec rejection

**Files:**
- Modify: `src/bestteam/core/reranking.py`
- Test: `tests/test_reranking.py`

**Interfaces:**
- Consumes: `Reranker`, `_FakeReranker` (Task 1).
- Produces: `resolve_reranker(spec: Any) -> Reranker`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_reranking.py
from bestteam.core.reranking import resolve_reranker
from bestteam.exceptions import ConfigurationError


def test_resolve_reranker_passthrough_instance():
    reranker = _FakeReranker()
    assert resolve_reranker(reranker) is reranker


def test_resolve_reranker_fake_spec():
    reranker = resolve_reranker("fake:")
    assert isinstance(reranker, _FakeReranker)


def test_resolve_reranker_unrecognized_string_spec():
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        resolve_reranker("openai:gpt-4o-mini")


def test_resolve_reranker_invalid_type():
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        resolve_reranker(123)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v -k resolve_reranker`
Expected: FAIL — `ImportError: cannot import name 'resolve_reranker'`

- [ ] **Step 3: Write the implementation**

```python
# Add to src/bestteam/core/reranking.py, after _FakeReranker
from typing import Any

from ..exceptions import ConfigurationError


def resolve_reranker(spec: Any) -> Reranker:
    """Accept a live `Reranker`, `"fake:"` (deterministic, $0), or
    `"cross-encoder:<model-name>"` (added in the next task). Raises
    `ConfigurationError` uniformly on a bad spec -- this function does not
    decide what a caller does with that failure."""
    if isinstance(spec, Reranker):
        return spec
    if isinstance(spec, str):
        if spec.startswith("fake:"):
            return _FakeReranker()
        raise ConfigurationError(
            f"Unsupported reranker spec {spec!r}: use 'fake:' or "
            "'cross-encoder:<model-name>'."
        )
    raise ConfigurationError(
        f"Unsupported reranker spec {spec!r}: pass a reranker model spec "
        "(str) or a Reranker instance."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/reranking.py tests/test_reranking.py
git commit -m "feat(reranking): add resolve_reranker() for fake:/instance specs"
```

---

### Task 3: `"cross-encoder:"` spec + process-level cache

**Files:**
- Modify: `src/bestteam/core/reranking.py`
- Modify: `pyproject.toml` — add `tools-rerank` extra (bundled into this task
  since the test suite needs it documented to explain the `importorskip`)
- Test: `tests/test_reranking.py`

**Interfaces:**
- Consumes: `resolve_reranker` (Task 2).
- Produces: `_CrossEncoderReranker`, module-level `_reranker_cache` +
  `_cache_lock`; `resolve_reranker()` now also accepts `"cross-encoder:..."`.

- [ ] **Step 1: Add the `tools-rerank` extra**

In `pyproject.toml`, after the `tools-rag-vector` line:

```toml
tools-rag-vector = ["numpy>=1.24"]
tools-rerank = ["sentence-transformers>=2.2"]
```

(Deliberately not added to the aggregate `tools` extra — it pulls in
PyTorch, a much heavier dependency than the other `tools-*` extras.)

- [ ] **Step 2: Write the failing tests**

```python
# Append to tests/test_reranking.py
from unittest.mock import MagicMock, patch


def test_resolve_reranker_missing_sentence_transformers():
    with patch.dict("sys.modules", {"sentence_transformers": None}):
        with pytest.raises(ConfigurationError, match="sentence-transformers"):
            resolve_reranker("cross-encoder:some-model")


def test_resolve_reranker_cross_encoder_caches_across_calls():
    pytest.importorskip("sentence_transformers")
    with patch("sentence_transformers.CrossEncoder") as mock_cls:
        mock_cls.return_value = MagicMock()
        first = resolve_reranker("cross-encoder:test-model")
        second = resolve_reranker("cross-encoder:test-model")
    assert first is second
    mock_cls.assert_called_once_with("test-model")


def test_resolve_reranker_cross_encoder_different_specs_not_shared():
    pytest.importorskip("sentence_transformers")
    with patch("sentence_transformers.CrossEncoder") as mock_cls:
        mock_cls.side_effect = lambda name: MagicMock(name=name)
        first = resolve_reranker("cross-encoder:model-a")
        second = resolve_reranker("cross-encoder:model-b")
    assert first is not second
    assert mock_cls.call_count == 2


def test_cross_encoder_reranker_scores_via_predict():
    pytest.importorskip("sentence_transformers")
    with patch("sentence_transformers.CrossEncoder") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.predict.return_value = [0.9, 0.1]
        mock_cls.return_value = mock_instance
        reranker = resolve_reranker("cross-encoder:unique-model-for-this-test")
        scores = reranker.score("q", ["a", "b"])
    assert scores == [0.9, 0.1]
    mock_instance.predict.assert_called_once_with([("q", "a"), ("q", "b")])
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v -k cross_encoder`
Expected: FAIL — `ConfigurationError: Unsupported reranker spec 'cross-encoder:...'`

- [ ] **Step 4: Write the implementation**

```python
# Add to src/bestteam/core/reranking.py
import threading
from typing import Dict

_cache_lock = threading.Lock()
_reranker_cache: Dict[str, Reranker] = {}  # successful resolutions only


class _CrossEncoderReranker(Reranker):
    """Wraps `sentence_transformers.CrossEncoder`. Constructed once per
    unique model spec and cached at process (module) scope by
    `resolve_reranker` -- a cross-encoder is a real local model with load
    cost, and `MemoryManager` is rebuilt fresh every run (see the design
    spec's "Key facts"), so per-instance caching alone wouldn't survive
    across runs."""

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ConfigurationError(
                "Cross-encoder reranking requires the 'sentence-transformers' "
                "package. Install it with: pip install 'bestteam[tools-rerank]'"
            ) from exc
        self._model = CrossEncoder(model_name)

    def _score(self, query: str, texts: List[str]) -> Sequence[float]:
        return self._model.predict([(query, text) for text in texts])
```

Then update `resolve_reranker`, inserting a new branch **before** the final
`raise ConfigurationError(f"Unsupported reranker spec ...")` for a string:

```python
        if spec.startswith("cross-encoder:"):
            with _cache_lock:
                if spec not in _reranker_cache:
                    _reranker_cache[spec] = _CrossEncoderReranker(spec[len("cross-encoder:") :])
                return _reranker_cache[spec]
```

(The lock guards only the "check cache, else construct" critical section —
never held during a `.score()` call, so concurrent recalls/queries against
an already-cached model aren't serialized. A construction failure inside the
`with` block propagates normally without populating the cache — every
subsequent call retries construction; see the design spec's "Deferred"
section for why v1 doesn't distinguish permanent vs. transient failures.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v`
Expected: 14 PASS

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/reranking.py pyproject.toml tests/test_reranking.py
git commit -m "feat(reranking): add cross-encoder backend with process-level cache"
```

---

### Task 4: `_resolve_candidate_k()` shared default/clamp helper

**Files:**
- Modify: `src/bestteam/core/reranking.py`
- Test: `tests/test_reranking.py`

**Interfaces:**
- Produces: `_MAX_RERANK_CANDIDATE_K = 100`, `_resolve_candidate_k(candidate_k:
  Optional[int], top_k: int) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_reranking.py
from bestteam.core.reranking import _MAX_RERANK_CANDIDATE_K, _resolve_candidate_k


def test_resolve_candidate_k_none_defaults_to_four_times_top_k():
    assert _resolve_candidate_k(None, top_k=5) == 20


def test_resolve_candidate_k_clamps_below_top_k_up_to_top_k():
    assert _resolve_candidate_k(2, top_k=5) == 5


def test_resolve_candidate_k_clamps_above_max_down_to_max():
    assert _resolve_candidate_k(500, top_k=5) == _MAX_RERANK_CANDIDATE_K


def test_resolve_candidate_k_passthrough_within_bounds():
    assert _resolve_candidate_k(30, top_k=5) == 30


def test_resolve_candidate_k_default_also_clamped_to_max():
    # top_k=30 -> default would be 120, clamped to 100
    assert _resolve_candidate_k(None, top_k=30) == _MAX_RERANK_CANDIDATE_K
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v -k resolve_candidate_k`
Expected: FAIL — `ImportError: cannot import name '_resolve_candidate_k'`

- [ ] **Step 3: Write the implementation**

```python
# Add to src/bestteam/core/reranking.py (near the top, with other module constants)
from typing import Optional

_MAX_RERANK_CANDIDATE_K = 100


def _resolve_candidate_k(candidate_k: Optional[int], top_k: int) -> int:
    """`None` defaults to `top_k * 4`; the result is always clamped into
    `[top_k, _MAX_RERANK_CANDIDATE_K]`. Pure clamp, never raises: a caller
    that wants "reject bad config" (knowledge bases, YAML-authored) checks
    the caller-supplied value against these same bounds itself and raises
    before calling this; a caller that wants "clamp silently"
    (`MemoryManager`, env-authored) just uses this function's return value
    directly."""
    if candidate_k is None:
        candidate_k = top_k * 4
    return max(top_k, min(candidate_k, _MAX_RERANK_CANDIDATE_K))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_reranking.py -v`
Expected: 19 PASS

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/reranking.py tests/test_reranking.py
git commit -m "feat(reranking): add _resolve_candidate_k default/clamp helper"
```

---

### Task 5: `core/knowledge_base.py` — shared `_rerank_candidates()` helper

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py`
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `Reranker` (Task 1).
- Produces: `_rerank_candidates(query: str, candidates: List[tuple[float,
  _Chunk]], reranker: Optional[Reranker], top_k: int) -> List[tuple[float,
  _Chunk]]` — used by both `LocalFolderKnowledgeBase` (Task 6) and
  `VectorKnowledgeBase` (Task 7).

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_knowledge_base.py
from bestteam.core.knowledge_base import _Chunk, _rerank_candidates
from bestteam.core.reranking import Reranker


class _ReverseLengthReranker(Reranker):
    """Scores by text length -- longer text wins. Distinct from any
    retrieval score, so tests can tell rerank changed the order."""

    def _score(self, query, texts):
        return [float(len(t)) for t in texts]


class _BoomReranker(Reranker):
    def _score(self, query, texts):
        raise RuntimeError("inference boom")


def _candidates(*texts):
    return [(1.0, _Chunk(source="s", text=t)) for t in texts]


def test_rerank_candidates_no_reranker_slices_to_top_k():
    candidates = _candidates("a", "bb", "ccc")
    result = _rerank_candidates("q", candidates, None, top_k=2)
    assert result == candidates[:2]


def test_rerank_candidates_empty_list_no_reranker_call():
    calls = []

    class _Spy(Reranker):
        def _score(self, query, texts):
            calls.append(texts)
            return []

    assert _rerank_candidates("q", [], _Spy(), top_k=5) == []
    assert calls == []


def test_rerank_candidates_reorders_by_score():
    candidates = _candidates("a", "bb", "ccc")  # retrieval order: a, bb, ccc
    result = _rerank_candidates("q", candidates, _ReverseLengthReranker(), top_k=3)
    assert [c.text for _s, c in result] == ["ccc", "bb", "a"]  # longest first


def test_rerank_candidates_truncates_to_top_k_after_reranking():
    candidates = _candidates("a", "bb", "ccc")
    result = _rerank_candidates("q", candidates, _ReverseLengthReranker(), top_k=1)
    assert [c.text for _s, c in result] == ["ccc"]


def test_rerank_candidates_falls_back_on_scoring_failure():
    candidates = _candidates("a", "bb", "ccc")
    result = _rerank_candidates("q", candidates, _BoomReranker(), top_k=2)
    assert result == candidates[:2]  # pre-rerank order preserved


def test_rerank_candidates_does_not_mutate_input():
    candidates = _candidates("a", "bb", "ccc")
    original = list(candidates)
    _rerank_candidates("q", candidates, _ReverseLengthReranker(), top_k=3)
    assert candidates == original
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -v -k rerank_candidates`
Expected: FAIL — `ImportError: cannot import name '_rerank_candidates'`

- [ ] **Step 3: Write the implementation**

In `src/bestteam/core/knowledge_base.py`, add near the top (after existing
imports) and after the `_Chunk` definition:

```python
import logging
# (add alongside the existing `import warnings`)

from .reranking import Reranker

_logger = logging.getLogger(__name__)
```

Then, after `_load_document_chunks`:

```python
def _rerank_candidates(
    query: str,
    candidates: List["tuple[float, _Chunk]"],
    reranker: Optional[Reranker],
    top_k: int,
) -> List["tuple[float, _Chunk]"]:
    """Never mutates `candidates`. `candidates` is already sorted by
    retrieval score and sliced to `candidate_k` by the caller. Empty input
    or no reranker configured is a pure slice -- no model call, no logging.
    Any exception during scoring (including a `_RerankScoringError` contract
    violation) falls back to the pre-rerank `candidates[:top_k]` slice,
    logged as a warning: rerank is a quality layer, never a reason the
    knowledge base query itself fails."""
    if reranker is None or not candidates:
        return candidates[:top_k]
    try:
        rerank_scores = reranker.score(query, [chunk.text for _score, chunk in candidates])
    except Exception:
        _logger.warning("Rerank failed; falling back to retrieval order", exc_info=True)
        return candidates[:top_k]
    order = sorted(range(len(candidates)), key=lambda i: (-rerank_scores[i], i))
    return [candidates[i] for i in order[:top_k]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -v`
Expected: all PASS (existing tests unaffected, 6 new ones pass)

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "feat(kb): add shared _rerank_candidates helper"
```

---

### Task 6: `LocalFolderKnowledgeBase` — wire in `rerank_model`/`candidate_k`

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py`
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `_rerank_candidates` (Task 5), `resolve_reranker`,
  `_resolve_candidate_k`, `_MAX_RERANK_CANDIDATE_K` (Tasks 2-4).
- Produces: `LocalFolderKnowledgeBase(..., rerank_model=None,
  candidate_k=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_knowledge_base.py
def _kb_with_docs(tmp_path, *texts, **kwargs):
    for i, text in enumerate(texts):
        (tmp_path / f"doc{i}.txt").write_text(text, encoding="utf-8")
    return LocalFolderKnowledgeBase("kb", tmp_path, **kwargs)


def test_local_folder_kb_rerank_unset_is_byte_identical(tmp_path):
    plain = _kb_with_docs(tmp_path, "apples and oranges", "cars and trucks", top_k=2)
    result_a = plain.query("apples")
    result_b = plain.query("apples")
    assert result_a == result_b  # deterministic, unaffected by the new code path


def test_local_folder_kb_rerank_changes_result_order(tmp_path):
    # Both docs share the term "fruit" so BM25 keeps both; fake reranker
    # (scores by length-distance to the query) prefers whichever is closer
    # in length to the query text.
    kb = _kb_with_docs(
        tmp_path,
        "fruit " * 1,       # short
        "fruit " * 20,      # long
        top_k=1,
        candidate_k=2,
        rerank_model="fake:",
    )
    result = kb.query("fruit")
    assert "doc0.txt" in result  # the short doc, closest in length to "fruit"


def test_local_folder_kb_candidate_k_rejects_below_top_k(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="candidate_k"):
        LocalFolderKnowledgeBase("kb", tmp_path, top_k=5, candidate_k=2, rerank_model="fake:")


def test_local_folder_kb_candidate_k_rejects_above_max(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="candidate_k"):
        LocalFolderKnowledgeBase("kb", tmp_path, top_k=5, candidate_k=500, rerank_model="fake:")


def test_local_folder_kb_bad_rerank_spec_raises_at_construction(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        LocalFolderKnowledgeBase("kb", tmp_path, rerank_model="not-a-real-spec")


def test_local_folder_kb_rerank_inference_failure_falls_back(tmp_path, monkeypatch):
    kb = _kb_with_docs(tmp_path, "apples and oranges", top_k=1, rerank_model="fake:")

    def boom(self, query, texts):
        raise RuntimeError("boom")

    monkeypatch.setattr(kb._reranker.__class__, "_score", boom)
    result = kb.query("apples")
    assert "doc0.txt" in result  # still returns the retrieval-order result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -v -k "rerank or candidate_k"`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'rerank_model'`

- [ ] **Step 3: Write the implementation**

In `src/bestteam/core/knowledge_base.py`, add the import:

```python
from .reranking import (
    _MAX_RERANK_CANDIDATE_K,
    _resolve_candidate_k,
    Reranker,
    resolve_reranker,
)
```

Modify `LocalFolderKnowledgeBase.__init__`:

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
    ) -> None:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ConfigurationError(
                "Knowledge bases require the 'rank-bm25' package. "
                "Install it with: pip install 'bestteam[tools-rag]'"
            ) from exc

        _validate_chunk_params(name, chunk_size, chunk_overlap)

        self.name = name
        self.path = Path(path)
        self.default_top_k = top_k
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)

        self._chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        ...  # unchanged from here
```

(`Any` needs to be added to the `typing` import at the top of the file
alongside `Callable, List, NamedTuple, Optional`.)

Modify `query()`:

```python
    def query(self, query: str, top_k: Optional[int] = None) -> str:
        top_k = top_k or self.default_top_k
        query_tokens = tokenize(query)
        query_terms = significant_terms(query_tokens)
        scores = self._bm25.get_scores(query_tokens)

        matches = [
            (len(query_terms & chunk_terms), score, chunk)
            for score, chunk, chunk_terms in zip(scores, self._chunks, self._chunk_terms)
            if query_terms & chunk_terms
        ]
        matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
        fetch_k = self._candidate_k if self._reranker is not None else top_k
        results = [(score, chunk) for _overlap, score, chunk in matches[:fetch_k]]
        results = _rerank_candidates(query, results, self._reranker, top_k)

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"
        ...  # formatting loop unchanged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "feat(kb): wire rerank_model/candidate_k into LocalFolderKnowledgeBase"
```

---

### Task 7: `VectorKnowledgeBase` — wire in `rerank_model`/`candidate_k`

**Files:**
- Modify: `src/bestteam/core/vector_knowledge_base.py`
- Test: `tests/test_vector_knowledge_base.py`

**Interfaces:**
- Consumes: `_rerank_candidates` (from `knowledge_base.py`, Task 5),
  `resolve_reranker`, `_resolve_candidate_k`, `_MAX_RERANK_CANDIDATE_K`
  (Tasks 2-4).
- Produces: `VectorKnowledgeBase(..., rerank_model=None, candidate_k=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_vector_knowledge_base.py
from bestteam.core.reranking import _FakeReranker


def _vector_kb_with_docs(tmp_path, *texts, **kwargs):
    for i, text in enumerate(texts):
        (tmp_path / f"doc{i}.txt").write_text(text, encoding="utf-8")
    kwargs.setdefault("embedding_model", "fake:8")
    return VectorKnowledgeBase("kb", tmp_path, **kwargs)


def test_vector_kb_rerank_unset_is_byte_identical(tmp_path):
    kb = _vector_kb_with_docs(tmp_path, "apples and oranges", "cars and trucks", top_k=2)
    assert kb.query("apples") == kb.query("apples")


def test_vector_kb_rerank_changes_result_order(tmp_path):
    kb = _vector_kb_with_docs(
        tmp_path,
        "fruit " * 1,
        "fruit " * 20,
        top_k=1,
        candidate_k=2,
        rerank_model="fake:",
        embedding_model=_KeywordEmbedding(),  # both docs equally "relevant" by cosine
    )
    result = kb.query("fruit")
    assert "doc0.txt" in result  # closest in length to the query


def test_vector_kb_score_threshold_applies_before_rerank(tmp_path):
    # score_threshold filters on the ORIGINAL cosine score, before rerank
    # ever sees the candidate pool.
    kb = _vector_kb_with_docs(
        tmp_path,
        "car engine",
        "irrelevant text about nothing shared",
        top_k=2,
        candidate_k=2,
        rerank_model="fake:",
        embedding_model=_KeywordEmbedding(),
        score_threshold=0.5,
    )
    result = kb.query("car engine")
    assert "doc0.txt" in result
    assert "doc1.txt" not in result  # excluded by score_threshold, never reaches rerank


def test_vector_kb_candidate_k_rejects_below_top_k(tmp_path):
    with pytest.raises(ConfigurationError, match="candidate_k"):
        _vector_kb_with_docs(tmp_path, "hello world", top_k=5, candidate_k=2, rerank_model="fake:")


def test_vector_kb_bad_rerank_spec_raises_at_construction(tmp_path):
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        _vector_kb_with_docs(tmp_path, "hello world", rerank_model="not-a-real-spec")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_vector_knowledge_base.py -v -k rerank`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'rerank_model'`

- [ ] **Step 3: Write the implementation**

In `src/bestteam/core/vector_knowledge_base.py`:

```python
from .knowledge_base import (
    KnowledgeBase,
    _load_document_chunks,
    _rerank_candidates,
    _validate_chunk_params,
)
from .reranking import _MAX_RERANK_CANDIDATE_K, _resolve_candidate_k, resolve_reranker
```

Modify `VectorKnowledgeBase.__init__`:

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
    ) -> None:
        try:
            import numpy as np
        except ImportError as exc:
            raise ConfigurationError(
                "Vector knowledge bases require the 'numpy' package. "
                "Install it with: pip install 'bestteam[tools-rag-vector]'"
            ) from exc

        _validate_chunk_params(name, chunk_size, chunk_overlap)

        self.name = name
        self.path = Path(path)
        self.default_top_k = top_k
        self.score_threshold = score_threshold
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)

        self._chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        ...  # unchanged from here
```

Modify `query()`:

```python
    def query(self, query: str, top_k: Optional[int] = None) -> str:
        import numpy as np

        top_k = top_k or self.default_top_k

        query_vec = np.array(self._embeddings.embed_query(query), dtype=np.float64)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm

        scores = self._matrix @ query_vec

        fetch_k = self._candidate_k if self._reranker is not None else top_k
        k = min(fetch_k, len(scores))
        top_indices = np.argsort(scores)[::-1][:k]

        results = [(scores[i], self._chunks[i]) for i in top_indices]

        if self.score_threshold is not None:
            results = [(s, c) for s, c in results if s >= self.score_threshold]

        results = _rerank_candidates(query, results, self._reranker, top_k)

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"
        ...  # formatting loop unchanged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_vector_knowledge_base.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/vector_knowledge_base.py tests/test_vector_knowledge_base.py
git commit -m "feat(kb): wire rerank_model/candidate_k into VectorKnowledgeBase"
```

---

### Task 8: `KnowledgeBaseSpec` + YAML round-trip

**Files:**
- Modify: `src/bestteam/core/specification.py`
- Test: `tests/test_specification.py`

**Interfaces:**
- Produces: `KnowledgeBaseSpec.rerank_model: Optional[str]`,
  `.candidate_k: Optional[int]`, emitted by `to_raw()` for **both**
  `local_folder` and `vector` (unlike the vector-only fields).

- [ ] **Step 1: Write the failing test**

Check the existing `KnowledgeBaseSpec` tests in `tests/test_specification.py`
first (`grep -n "KnowledgeBaseSpec" tests/test_specification.py`) to match
the file's existing style, then add:

```python
def test_knowledge_base_spec_rerank_fields_emitted_for_local_folder():
    spec = KnowledgeBaseSpec(
        name="kb", path="./docs", rerank_model="fake:", candidate_k=20
    )
    raw = spec.to_raw()
    assert raw["rerank_model"] == "fake:"
    assert raw["candidate_k"] == 20


def test_knowledge_base_spec_rerank_fields_emitted_for_vector():
    spec = KnowledgeBaseSpec(
        name="kb", path="./docs", type="vector", embedding_model="fake:8",
        rerank_model="fake:", candidate_k=20,
    )
    raw = spec.to_raw()
    assert raw["rerank_model"] == "fake:"
    assert raw["candidate_k"] == 20


def test_knowledge_base_spec_rerank_fields_omitted_when_unset():
    spec = KnowledgeBaseSpec(name="kb", path="./docs")
    raw = spec.to_raw()
    assert "rerank_model" not in raw
    assert "candidate_k" not in raw
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_specification.py -v -k rerank`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'rerank_model'`

- [ ] **Step 3: Write the implementation**

In `src/bestteam/core/specification.py`, modify `KnowledgeBaseSpec`:

```python
class KnowledgeBaseSpec(BaseModel):
    """Mirrors a `knowledge_bases:` entry in the loader's raw dict (see `core/loader.py`).

    `embedding_model`/`score_threshold`/`cache_path` only apply to
    `type: vector` -- `to_raw()` omits them for `local_folder` so an
    architect that sets them on the wrong type doesn't trigger an unexpected
    `TypeError` from the knowledge base constructor. `rerank_model`/
    `candidate_k` apply to BOTH types (rerank is retrieval-method-agnostic)
    and are always emitted when set.
    """

    name: str
    path: str
    type: str = "local_folder"
    chunk_size: int = 1000
    chunk_overlap: int = 100
    top_k: int = 5
    embedding_model: Optional[str] = None
    score_threshold: Optional[float] = None
    cache_path: Optional[str] = None
    rerank_model: Optional[str] = None
    candidate_k: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _name_is_valid_tool_identifier(cls, v: str) -> str:
        _validate_tool_name(v)
        return v

    def to_raw(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k": self.top_k,
        }
        if self.rerank_model is not None:
            raw["rerank_model"] = self.rerank_model
        if self.candidate_k is not None:
            raw["candidate_k"] = self.candidate_k
        if self.type == "vector":
            if self.embedding_model is not None:
                raw["embedding_model"] = self.embedding_model
            if self.score_threshold is not None:
                raw["score_threshold"] = self.score_threshold
            if self.cache_path is not None:
                raw["cache_path"] = self.cache_path
        return raw
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_specification.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/specification.py tests/test_specification.py
git commit -m "feat(kb): add rerank_model/candidate_k to KnowledgeBaseSpec"
```

---

### Task 9: `core/memory.py` — weighted `_reciprocal_rank_fusion`

**Files:**
- Modify: `src/bestteam/core/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Produces: `_reciprocal_rank_fusion(*ranked_id_lists, k=60, weights=None)`
  — `weights=None` (default) is byte-identical to today's unweighted
  behavior at both existing call sites (hybrid BM25+vector fusion,
  query-expansion fusion).

- [ ] **Step 1: Write the failing tests**

Check existing `_reciprocal_rank_fusion` tests first
(`grep -n "_reciprocal_rank_fusion" tests/test_memory.py`), then add:

```python
def test_reciprocal_rank_fusion_weights_default_matches_unweighted():
    unweighted = _reciprocal_rank_fusion(["a", "b"], ["b", "a"])
    explicit = _reciprocal_rank_fusion(["a", "b"], ["b", "a"], weights=[1.0, 1.0])
    assert unweighted == explicit


def test_reciprocal_rank_fusion_weighted_favors_higher_weight_list():
    # List A ranks "winner" #1, list B ranks "steady" #5 in BOTH lists.
    # Unweighted, "steady" can outscore "winner" (verified in the design
    # spec's math); weighting list-2 (the "winner" signal) at 2x should let
    # it win instead.
    list_a = ["steady", "x2", "x3", "x4", "x5"]  # steady at rank 1 here too
    list_b = ["winner", "x2", "x3", "x4", "steady"]  # winner at rank 1, steady at rank 5

    unweighted = _reciprocal_rank_fusion(list_a, list_b)
    assert unweighted["steady"] > unweighted["winner"]  # confirms the dilution problem

    weighted = _reciprocal_rank_fusion(list_a, list_b, weights=(1.0, 2.0))
    assert weighted["winner"] > weighted["steady"]  # weighting fixes it
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -v -k reciprocal_rank_fusion`
Expected: FAIL — `TypeError: _reciprocal_rank_fusion() got an unexpected keyword argument 'weights'`

- [ ] **Step 3: Write the implementation**

In `src/bestteam/core/memory.py`, modify `_reciprocal_rank_fusion`:

```python
def _reciprocal_rank_fusion(
    *ranked_id_lists: Sequence[str], k: int = 60, weights: Optional[Sequence[float]] = None
) -> Dict[str, float]:
    """Merge ranked-id lists into one fused score per id: the sum, across
    every list an id appears in, of ``weight / (k + rank)`` (1-based rank).
    Standard Reciprocal Rank Fusion -- rank-based, so it needs no score
    calibration between signals on different scales (BM25 vs. cosine vs. a
    cross-encoder's raw logits). `weights` defaults to `1.0` per list
    (today's behavior, unchanged); the rerank combination step (see
    `_fused_search`) passes an explicit weight to keep the reranker's signal
    from being diluted by the pre-rerank ordering."""
    resolved_weights = weights if weights is not None else [1.0] * len(ranked_id_lists)
    scores: Dict[str, float] = {}
    for weight, ranked_ids in zip(resolved_weights, ranked_id_lists):
        for rank, record_id in enumerate(ranked_ids, start=1):
            scores[record_id] = scores.get(record_id, 0.0) + weight / (k + rank)
    return scores
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -v`
Expected: all PASS (existing `_reciprocal_rank_fusion` tests unaffected)

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/memory.py tests/test_memory.py
git commit -m "feat(memory): support weighted _reciprocal_rank_fusion"
```

---

### Task 10: `MemoryManager` — `rerank_model`/`rerank_candidate_k` + lazy `_get_reranker()`

**Files:**
- Modify: `src/bestteam/core/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `resolve_reranker`, `_resolve_candidate_k`, `Reranker` (Tasks
  2-4).
- Produces: `MemoryManager(..., rerank_model=None, rerank_candidate_k=None)`,
  `MemoryManager._get_reranker() -> Optional[Reranker]` (lazy, resolved
  once, cached on `self`, never raises).

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_memory.py
def test_memory_manager_rerank_disabled_by_default():
    store = SqliteBM25Memory(":memory:")
    mgr = MemoryManager(store)
    assert mgr._get_reranker() is None


def test_memory_manager_fake_rerank_resolves():
    store = SqliteBM25Memory(":memory:")
    mgr = MemoryManager(store, rerank_model="fake:")
    reranker = mgr._get_reranker()
    assert reranker is not None
    assert reranker.score("q", ["a", "bb"]) == [-1.0, -2.0]


def test_memory_manager_bad_rerank_spec_disables_rerank_not_construction():
    store = SqliteBM25Memory(":memory:")
    mgr = MemoryManager(store, rerank_model="not-a-real-spec")  # must not raise
    assert mgr._get_reranker() is None  # degrades silently


def test_memory_manager_reranker_resolved_once(monkeypatch):
    calls = []
    import bestteam.core.memory as memory_module

    real_resolve = memory_module.resolve_reranker

    def spy_resolve(spec):
        calls.append(spec)
        return real_resolve(spec)

    monkeypatch.setattr(memory_module, "resolve_reranker", spy_resolve)
    store = SqliteBM25Memory(":memory:")
    mgr = MemoryManager(store, rerank_model="fake:")
    mgr._get_reranker()
    mgr._get_reranker()
    assert len(calls) == 1  # resolved once per MemoryManager, not per call


def test_memory_manager_rerank_candidate_k_defaults_from_top_k():
    store = SqliteBM25Memory(":memory:")
    mgr = MemoryManager(store, top_k=5)
    assert mgr.rerank_candidate_k == 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -v -k "rerank_disabled or fake_rerank or bad_rerank or reranker_resolved or candidate_k_defaults"`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'rerank_model'`

- [ ] **Step 3: Write the implementation**

In `src/bestteam/core/memory.py`, add the import (near the existing
`from .embeddings import normalize_rows, resolve_embedding_model`):

```python
from .reranking import Reranker, _resolve_candidate_k, resolve_reranker
```

Modify `MemoryManager.__init__` (add two new parameters at the end of the
signature, alongside the existing `query_expansion_*` ones):

```python
    def __init__(
        self,
        store: Memory,
        extraction_model: Any = None,
        top_k: int = 5,
        org_id: Optional[int] = None,
        principal_id: Optional[str] = None,
        workflow_id: Optional[int] = None,
        run_id: Optional[str] = None,
        workflow_version_id: Optional[int] = None,
        recall_max_candidates: Optional[int] = None,
        max_episodic_per_user: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
        rerank_model: Any = None,
        rerank_candidate_k: Optional[int] = None,
    ) -> None:
        self.store = store
        self.extraction_model = extraction_model
        self.top_k = top_k
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count
        # Rerank (opt-in): resolved LAZILY on first use (`_get_reranker`),
        # never at construction -- unlike the store's `embedding_model`
        # (eager, fail-hard), every other MemoryManager-level model knob
        # (extraction_model, query_expansion_model) is lazy + fail-soft, and
        # rerank follows that same convention since it lives at this layer
        # too. `rerank_candidate_k` is resolved eagerly here (pure
        # arithmetic, no model call) via the same helper the KB side uses.
        self.rerank_model = rerank_model
        self.rerank_candidate_k = _resolve_candidate_k(rerank_candidate_k, top_k)
        self._reranker: Optional[Reranker] = None
        self._reranker_resolve_attempted = False
        ...  # the rest of __init__ (org_id, principal_id, etc.) unchanged
```

Add a new method, near `_expand_query`:

```python
    def _get_reranker(self) -> Optional[Reranker]:
        """Lazily resolve `rerank_model` on first use, cached on `self` for
        this manager's lifetime (one resolution attempt per run, since a new
        `MemoryManager` is built per run -- see `ui/backend/runtime.py::
        _make_memory`). NEVER raises: a bad spec/missing dependency degrades
        to "rerank disabled for this run", exactly like `_expand_query`'s
        `query_expansion_model` handling, not the store's eager
        `embedding_model` handling."""
        if not self._reranker_resolve_attempted:
            self._reranker_resolve_attempted = True
            if self.rerank_model is not None:
                try:
                    self._reranker = resolve_reranker(self.rerank_model)
                except Exception:
                    _logger.warning(
                        "Memory rerank disabled: could not resolve reranker %r",
                        self.rerank_model,
                        exc_info=True,
                    )
        return self._reranker
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/memory.py tests/test_memory.py
git commit -m "feat(memory): add MemoryManager.rerank_model with lazy fail-soft resolution"
```

---

### Task 11: `_fused_search()` rewrite — rerank the fused candidate pool

**Files:**
- Modify: `src/bestteam/core/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `_reciprocal_rank_fusion(weights=)` (Task 9), `_get_reranker()`
  (Task 10).
- Produces: `_fused_search()` unchanged signature, new internal behavior;
  `_RERANK_RRF_WEIGHT = 8.0` module constant.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_memory.py
def _manager_with_records(records, **manager_kwargs):
    """records: list of (type, content) tuples, all for user 'u'."""
    store = SqliteBM25Memory(":memory:")
    for type_, content in records:
        store.add("u", type_, content)
    return MemoryManager(store, **manager_kwargs)


def test_fused_search_single_query_matches_pre_change_behavior():
    # No query expansion, no rerank: _fused_search's single-query path must
    # still return the plain BM25 order (this is the len(queries)==1
    # special case being removed -- must remain equivalent via RRF-of-one).
    mgr = _manager_with_records([
        (SEMANTIC, "likes apples"),
        (SEMANTIC, "likes oranges and apples too"),
    ])
    results = mgr._fused_search("u", ["apples"], types=[SEMANTIC], top_k=5)
    assert len(results) == 2  # both share "apples"; order matches BM25-only ranking


def test_fused_search_rerank_scores_literal_query_only(monkeypatch):
    # `_fused_search` receives `queries` already assembled (by `recall()`,
    # via `_expand_query` -- untouched by this task) as
    # [literal_query, *expansions]. This test calls `_fused_search` directly
    # with a literal + a fake "expansion" already in the list, the same
    # shape `recall()` would pass, to isolate: does rerank scoring ever see
    # anything but `queries[0]`?
    seen_queries = []

    class _SpyReranker(Reranker):
        def _score(self, query, texts):
            seen_queries.append(query)
            return [0.0] * len(texts)

    mgr = _manager_with_records(
        [(SEMANTIC, "prefers bullet points"), (SEMANTIC, "likes concise answers")],
        rerank_model="fake:",
    )
    monkeypatch.setattr(mgr, "_get_reranker", lambda: _SpyReranker())

    mgr._fused_search("u", ["original query", "a totally different expansion"], types=[SEMANTIC], top_k=5)
    assert seen_queries == ["original query"]  # never the expansion variant


def test_fused_search_candidate_pool_capped_at_rerank_candidate_k():
    scored_batches = []

    class _SpyReranker(Reranker):
        def _score(self, query, texts):
            scored_batches.append(len(texts))
            return [0.0] * len(texts)

    mgr = _manager_with_records(
        [(SEMANTIC, f"fact number {i}") for i in range(30)],
        rerank_model="fake:",
        rerank_candidate_k=10,
    )
    mgr._reranker = _SpyReranker()
    mgr._reranker_resolve_attempted = True

    # 4 query variants, each capable of returning up to rerank_candidate_k=10
    # results -- the reranker must still only ever see <= 10 candidates.
    mgr._fused_search(
        "u",
        ["fact", "number", "fact number", "figures"],
        types=[SEMANTIC],
        top_k=5,
    )
    assert scored_batches and max(scored_batches) <= 10


def test_fused_search_rerank_changes_final_order():
    mgr = _manager_with_records(
        [(SEMANTIC, "x"), (SEMANTIC, "matching query text exactly")],
        rerank_model="fake:",  # scores by length-distance to the query
        rerank_candidate_k=10,
    )
    results = mgr._fused_search("u", ["matching query text exactly"], types=[SEMANTIC], top_k=1)
    assert results[0].content == "matching query text exactly"


def test_fused_search_rerank_failure_falls_back(monkeypatch):
    class _BoomReranker(Reranker):
        def _score(self, query, texts):
            raise RuntimeError("boom")

    mgr = _manager_with_records([(SEMANTIC, "a"), (SEMANTIC, "b")], rerank_model="fake:")
    monkeypatch.setattr(mgr, "_get_reranker", lambda: _BoomReranker())

    results = mgr._fused_search("u", ["a"], types=[SEMANTIC], top_k=5)
    assert len(results) >= 1  # recall still works, just without rerank benefit


def test_fused_search_rerank_unset_byte_identical_to_no_rerank():
    records = [(SEMANTIC, "likes apples"), (SEMANTIC, "likes oranges and apples too")]
    plain = _manager_with_records(records)
    with_none = _manager_with_records(records, rerank_model=None)
    assert (
        plain._fused_search("u", ["apples"], types=[SEMANTIC], top_k=5)
        == with_none._fused_search("u", ["apples"], types=[SEMANTIC], top_k=5)
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -v -k fused_search`
Expected: some FAIL (rerank tests error/assert-fail since `_fused_search`
doesn't consult `_get_reranker()` yet; `test_fused_search_single_query_matches_pre_change_behavior`
and `test_fused_search_rerank_unset_byte_identical_to_no_rerank` PASS already
against the old implementation — that's fine, they're the regression guard)

- [ ] **Step 3: Write the implementation**

In `src/bestteam/core/memory.py`, add the module constant near
`_DEFAULT_RECENCY_HALF_LIFE_DAYS`:

```python
# Weight given to the reranker's own ranking when re-fusing it with the
# pre-rerank (hybrid/recency-aware) ranking in `_fused_search`. >1 so the
# cross-encoder's signal isn't diluted by equal-weight RRF -- verified
# numerically (see the design spec) that unweighted RRF can let a
# mid-ranked-on-both-signals candidate beat the reranker's clear #1 pick.
#
# 8.0, not 2.0: hand-derived break-even math (k=60, RRF's rank-based
# 1/(k+rank) formula) shows weight=2.0 still loses to a
# consistent-on-both-signals candidate across most of the realistic
# pre-rerank-rank-gap range -- it barely nudges the fused order. Pushing the
# weight past ~15-40 (depending on candidate_k) instead makes the reranker's
# order win almost unconditionally, which quietly defeats the point of
# re-fusing with the pre-rerank order at all (a continuous cross-encoder
# score essentially never ties, so the pre-rerank signal would then only
# ever break a tie that doesn't happen). 8.0 is a deliberate middle point:
# it meaningfully corrects the fused order when the two signals roughly
# agree or diverge modestly, but still lets a very consistent pre-rerank
# candidate win over the reranker's pick on a WIDE disagreement (e.g.
# pre-rerank rank ~20+ vs rank ~1) -- treated as a legitimate hedge on
# strong signal conflict, not a bug. Internal constant for v1; revisit once
# there's eval data to tune against.
_RERANK_RRF_WEIGHT = 8.0
```

Replace `_fused_search` entirely:

```python
    def _fused_search(
        self, user_id: str, queries: List[str], types: Sequence[str], **kwargs: Any
    ) -> List["MemoryRecord"]:
        """Search once per query in `queries` and fuse the ranked id lists
        with `_reciprocal_rank_fusion` (rank-based, so it composes cleanly on
        top of each per-query search's own BM25/vector/recency-decay
        ranking). With a single query this reproduces the pre-rerank ranking
        exactly (RRF over one list preserves relative order). When rerank is
        configured (`_get_reranker()` returns non-None): each query variant
        fetches `rerank_candidate_k` results instead of `top_k` (so a deep
        enough pool survives fusion), the fused pool is capped to
        `rerank_candidate_k` BEFORE scoring (bounding the cross-encoder call
        regardless of how many query variants query expansion produced), the
        capped pool is scored against `queries[0]` ONLY (the literal
        original query -- an expansion variant is never used for scoring,
        only for widening recall), and the pre-rerank and rerank orderings
        are re-fused via a weighted RRF (`_RERANK_RRF_WEIGHT`) so the
        reranker's signal isn't diluted by the existing recency/hybrid
        ordering. A rerank-time failure (bad `score()` contract, model
        inference error) logs a warning and falls back to the pre-rerank
        `candidates[:top_k]` -- recall must never fail because of rerank."""
        top_k = kwargs.get("top_k", self.top_k)
        reranker = self._get_reranker()
        fetch_k = self.rerank_candidate_k if reranker is not None else top_k

        ranked_lists: List[List[str]] = []
        by_id: Dict[str, "MemoryRecord"] = {}
        for q in queries:
            results = self.store.search(user_id, q, types=types, **{**kwargs, "top_k": fetch_k})
            ranked_lists.append([r.id for r in results])
            for r in results:
                by_id.setdefault(r.id, r)

        fused = _reciprocal_rank_fusion(*ranked_lists)
        pre_rerank_ranked_ids = [
            record_id
            for record_id, _score in sorted(fused.items(), key=lambda pair: pair[1], reverse=True)
        ]

        if reranker is None:
            return [by_id[record_id] for record_id in pre_rerank_ranked_ids[:top_k]]

        candidate_ids = pre_rerank_ranked_ids[: self.rerank_candidate_k]
        candidates = [by_id[record_id] for record_id in candidate_ids]
        try:
            scores = reranker.score(queries[0], [c.content for c in candidates])
            rerank_ranked_ids = [
                candidate_ids[i]
                for i, _score in sorted(enumerate(scores), key=lambda pair: (-pair[1], pair[0]))
            ]
        except Exception:
            _logger.warning("Memory rerank failed; using pre-rerank order", exc_info=True)
            return candidates[:top_k]

        final = _reciprocal_rank_fusion(
            candidate_ids, rerank_ranked_ids, weights=(1.0, _RERANK_RRF_WEIGHT)
        )
        final_ranked = sorted(final.items(), key=lambda pair: pair[1], reverse=True)
        return [by_id[record_id] for record_id, _score in final_ranked[:top_k]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory.py tests/test_memory_backend.py tests/test_memory_integration.py -v`
Expected: all PASS (including every pre-existing test — this confirms the
`len(queries) == 1` removal is truly equivalent)

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/memory.py tests/test_memory.py
git commit -m "feat(memory): rerank the fused recall candidate pool with weighted RRF"
```

---

### Task 12: `recall()` per-scope `top_k` regression test

**Files:**
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `MemoryManager.recall()` (unchanged call sites into
  `_fused_search`, Task 11).

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_memory.py
def test_recall_two_scopes_each_independently_respect_top_k_with_rerank():
    store = SqliteBM25Memory(":memory:")
    for i in range(5):
        store.add("u", SEMANTIC, f"semantic fact about apples {i}")
    for i in range(5):
        store.add("u", EPISODIC, f"episodic note about apples {i}")
    mgr = MemoryManager(store, top_k=2, rerank_model="fake:")

    result = mgr.recall("u", "apples")
    assert result.count <= 4  # 2 semantic + 2 episodic/procedural, never more
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -v -k recall_two_scopes`
Expected: PASS already (Task 11's `_fused_search` already caps correctly per
scope — this step is a regression guard, not new behavior; if it fails,
Task 11's implementation has a bug to fix before continuing)

- [ ] **Step 3: Commit**

```bash
git add tests/test_memory.py
git commit -m "test(memory): guard per-scope top_k with rerank enabled"
```

---

### Task 13: `ui/backend/runtime.py` — env var wiring

**Files:**
- Modify: `ui/backend/runtime.py`
- Test: `tests/test_memory_backend.py`

**Interfaces:**
- Consumes: `MemoryManager(rerank_model=, rerank_candidate_k=)` (Task 10).
- Produces: `_make_memory()` reads `BESTTEAM_MEMORY_RERANK_MODEL`,
  `BESTTEAM_MEMORY_RERANK_CANDIDATE_K`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_memory_backend.py
def test_make_memory_rerank_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_RERANK_MODEL", raising=False)

    mgr = _make_memory()
    assert mgr.rerank_model is None


def test_make_memory_wires_rerank_model_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_RERANK_MODEL", "fake:")

    mgr = _make_memory()
    assert mgr.rerank_model == "fake:"


def test_make_memory_wires_rerank_candidate_k_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_RERANK_CANDIDATE_K", "30")

    mgr = _make_memory()
    assert mgr.rerank_candidate_k == 30


def test_make_memory_bad_rerank_spec_does_not_disable_memory(monkeypatch, tmp_path):
    # Contrast with test_make_memory_bad_embedding_spec_disables_memory_entirely:
    # rerank_model is resolved lazily (like query_expansion_model), not
    # eagerly at store construction, so a bad spec never disables memory.
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_RERANK_MODEL", "not-a-real-provider:whatever")

    mgr = _make_memory()
    assert mgr is not None
    assert mgr._get_reranker() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory_backend.py -v -k rerank`
Expected: FAIL — `assert mgr.rerank_model is None` raises `AttributeError`
(or the env var is silently ignored) since `_make_memory` doesn't read it yet

- [ ] **Step 3: Write the implementation**

In `ui/backend/runtime.py`, inside `_make_memory()`, alongside the existing
`query_expansion_model`/`query_expansion_count` reads:

```python
    query_expansion_model = os.environ.get("BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL", "").strip() or None
    query_expansion_count = _env_int("BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT", 3, min_value=None)
    rerank_model = os.environ.get("BESTTEAM_MEMORY_RERANK_MODEL", "").strip() or None
    rerank_candidate_k = _env_int("BESTTEAM_MEMORY_RERANK_CANDIDATE_K", None)
    return MemoryManager(
        store,
        extraction_model=extraction_model,
        ...  # existing kwargs unchanged
        query_expansion_model=query_expansion_model,
        query_expansion_count=query_expansion_count,
        rerank_model=rerank_model,
        rerank_candidate_k=rerank_candidate_k,
        ...  # remaining existing kwargs unchanged
    )
```

(Read the current `_make_memory` body first — `.\.venv\Scripts\python.exe -c
"import inspect, ui.backend.runtime as m; print(inspect.getsource(m._make_memory))"`
— to place these lines next to the existing `query_expansion_*` block rather
than guessing the exact surrounding line numbers, since Tasks 1-12 don't
touch this file and it may have shifted.)

Also update the `_make_memory` docstring to mention the two new env vars,
mirroring the existing `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL` paragraph:

```python
    - `BESTTEAM_MEMORY_RERANK_MODEL`, if set, enables a cross-encoder rerank
      pass over the fused recall candidates for both scopes (same spec
      convention -- `"fake:"` for $0 tests, `"cross-encoder:<model-name>"`
      for a real local model via `sentence-transformers`).
      `BESTTEAM_MEMORY_RERANK_CANDIDATE_K` (default `top_k * 4`, clamped)
      tunes how many fused candidates reach the reranker. Like
      `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL`, this is resolved lazily and a
      bad spec never disables memory -- it just disables rerank for that
      run. See `core/memory.py`'s `_fused_search` and
      `docs/superpowers/specs/2026-08-12-pluggable-rerank-design.md`.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_memory_backend.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add ui/backend/runtime.py tests/test_memory_backend.py
git commit -m "feat(memory): wire BESTTEAM_MEMORY_RERANK_MODEL/CANDIDATE_K env vars"
```

---

### Task 14: Docs + full-suite + end-to-end smoke verification

**Files:**
- Modify: `src/bestteam/core/CLAUDE.md`
- Modify: `ui/backend/CLAUDE.md`

**Interfaces:** none (documentation + verification only).

- [ ] **Step 1: Update `src/bestteam/core/CLAUDE.md`**

In the "Known limitation: vector knowledge base retrieval is single-stage"
section, add a paragraph documenting that reranking is now available (both
KB types), referencing `rerank_model`/`candidate_k`, `"fake:"`/
`"cross-encoder:<model-name>"` specs, and the `bestteam[tools-rerank]`
extra — and note the limitation paragraph now reads "no query
rewriting/expansion for KB (see Memory's for a possible pattern to mirror);
reranking is available, opt-in" instead of "no ... reranking".

In the "Known limitations (per-user memory)" section, add a paragraph next
to the existing query-expansion write-up documenting `rerank_model`/
`rerank_candidate_k`, the weighted-RRF re-fusion with `_RERANK_RRF_WEIGHT`,
literal-query-only scoring, the lazy/fail-soft resolution shape (mirrors
`query_expansion_model`), and explicitly note the two v1-deferred items from
the design spec (no failure-caching differentiation, no inference-time
lock) so a future reader doesn't rediscover and re-litigate those decisions.

- [ ] **Step 2: Update `ui/backend/CLAUDE.md`**

Next to the existing `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL`/
`BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT` paragraph in "Per-user memory", add
a short paragraph for `BESTTEAM_MEMORY_RERANK_MODEL`/
`BESTTEAM_MEMORY_RERANK_CANDIDATE_K`, same style (env var, default, failure
shape, one-line pointer to `core/CLAUDE.md` for the full design).

- [ ] **Step 3: Run the full test suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all PASS, zero regressions

- [ ] **Step 4: End-to-end smoke test (no real model download)**

```bash
.\.venv\Scripts\python.exe -c "
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase
from bestteam.core.vector_knowledge_base import VectorKnowledgeBase
import tempfile, pathlib

with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d)
    (p / 'short.txt').write_text('fruit')
    (p / 'long.txt').write_text('fruit ' * 30)

    kb = LocalFolderKnowledgeBase('kb', p, top_k=1, candidate_k=2, rerank_model='fake:')
    print('local_folder rerank result:')
    print(kb.query('fruit'))

    vkb = VectorKnowledgeBase('vkb', p, embedding_model='fake:8', top_k=1, candidate_k=2, rerank_model='fake:')
    print('vector rerank result:')
    print(vkb.query('fruit'))
"
```

Expected: both print a formatted result whose `[source: ...]` names the
document whose length is closest to the query `"fruit"` (i.e. `short.txt`),
confirming the fake reranker's length-distance scoring actually drove the
final choice end-to-end, through the real constructor/query() code path
(not just the unit-level `_rerank_candidates`/`_fused_search` tests).

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/CLAUDE.md ui/backend/CLAUDE.md
git commit -m "docs(kb,memory): document pluggable rerank"
```
