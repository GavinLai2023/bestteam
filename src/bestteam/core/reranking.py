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
