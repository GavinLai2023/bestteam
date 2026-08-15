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
        # numpy is checked before rank_bm25: rank_bm25 imports numpy
        # internally, so if rank_bm25 hasn't been imported yet in this
        # process and numpy is unavailable, `import rank_bm25` fails too --
        # checking numpy first ensures a numpy-only failure is reported as
        # "numpy", not misattributed to "rank-bm25".
        try:
            import numpy as np
        except ImportError as exc:
            raise ConfigurationError(
                "Hybrid knowledge bases require the 'numpy' package. "
                "Install it with: pip install 'bestteam[tools-rag-vector]'"
            ) from exc
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ConfigurationError(
                "Hybrid knowledge bases require the 'rank-bm25' package. "
                "Install it with: pip install 'bestteam[tools-rag]'"
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
