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
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

from ..exceptions import ConfigurationError


_MAX_RERANK_CANDIDATE_K = 100


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


def _resolve_candidate_k(candidate_k: Optional[int], top_k: int) -> int:
    """`None` defaults to `top_k * 4`; the result is always clamped into
    `[top_k, _MAX_RERANK_CANDIDATE_K]` when `top_k` itself fits that range.
    `_MAX_RERANK_CANDIDATE_K` is the outer, non-negotiable bound (it caps how
    many texts ever reach the reranker), so it is applied last -- if `top_k`
    alone exceeds it, the result is the cap, not `top_k` (the pool can end up
    smaller than `top_k`, same as any other cost-bounded truncation). Pure
    clamp, never raises: a caller that wants "reject bad config" (knowledge
    bases, YAML-authored) checks the caller-supplied value against these same
    bounds itself and raises before calling this; a caller that wants "clamp
    silently" (`MemoryManager`, env-authored) just uses this function's
    return value directly."""
    if candidate_k is None:
        candidate_k = top_k * 4
    return min(max(top_k, candidate_k), _MAX_RERANK_CANDIDATE_K)


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
        if spec.startswith("cross-encoder:"):
            with _cache_lock:
                if spec not in _reranker_cache:
                    _reranker_cache[spec] = _CrossEncoderReranker(spec[len("cross-encoder:") :])
                return _reranker_cache[spec]
        raise ConfigurationError(
            f"Unsupported reranker spec {spec!r}: use 'fake:' or "
            "'cross-encoder:<model-name>'."
        )
    raise ConfigurationError(
        f"Unsupported reranker spec {spec!r}: pass a reranker model spec "
        "(str) or a Reranker instance."
    )
