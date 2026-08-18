from __future__ import annotations

import hashlib
import json
import os
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..exceptions import ConfigurationError
from .embeddings import (
    billable_spec,
    embed_documents_in_batches,
    normalize_rows,
    report_query_embedding_usage,
    resolve_embedding_model,
)
from .knowledge_base import (
    KnowledgeBase,
    _Chunk,
    _load_document_chunks,
    _query_variants,
    _rerank_candidates,
    _rerank_fetch_k,
    _rrf_retrieve,
    _validate_chunk_params,
)
from .reranking import _MAX_RERANK_CANDIDATE_K, _resolve_candidate_k, resolve_reranker


def _require_numpy():
    """Check that numpy is available and return the np module.

    Raises ConfigurationError if the package is not installed.
    This check must run early to avoid expensive operations (like file parsing)
    before discovering a missing dependency.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise ConfigurationError(
            "Vector knowledge bases require the 'numpy' package. "
            "Install it with: pip install 'bestteam[tools-rag-vector]'"
        ) from exc
    return np


def _chunk_cache_key(model_spec: str, text: str) -> str:
    return hashlib.sha256(f"{model_spec}\x00{text}".encode("utf-8")).hexdigest()


def _load_embedding_cache(cache_path: Path, model_spec: str) -> Dict[str, List[float]]:
    """Return {chunk_hash: vector}, or {} if missing/corrupt/model mismatch."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if data.get("model") != model_spec:
        return {}  # different embedding model -> incompatible vector space
    return data.get("entries", {})


def _save_embedding_cache(cache_path: Path, model_spec: str, entries: Dict[str, List[float]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"model": model_spec, "entries": entries}), encoding="utf-8")
    os.replace(tmp, cache_path)  # atomic on Windows and POSIX


class VectorKnowledgeBase(KnowledgeBase):
    """A knowledge base backed by a folder of documents, indexed with embeddings.

    Documents are parsed and chunked via the same pipeline as
    LocalFolderKnowledgeBase, embedded with a configurable embedding model,
    and searched with in-memory cosine similarity. No external vector store —
    suited to small/medium corpora where semantic (not just keyword) search
    is needed.
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
        description: Optional[str] = None,
    ) -> None:
        _require_numpy()
        _validate_chunk_params(name, chunk_size, chunk_overlap)
        self.path = Path(path)
        chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        if not chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents in {self.path}"
            )
        self._init_common(name, chunks, top_k, score_threshold, rerank_model, candidate_k,
                           query_expansion_model, query_expansion_count, description)
        self._embeddings = resolve_embedding_model(embedding_model)
        # Kept for metering only: the query-time `embed_query` call below is
        # billed against this spec, and there is nothing to bill when the
        # customer handed in a live model or a `"fake:"` spec.
        self._embedding_spec = billable_spec(embedding_model)
        try:
            vectors = self._embed_chunks(embedding_model, cache_path)
        except ValueError as exc:
            # A batch that came back the wrong size: `embed_documents_in_batches`
            # reports that as a plain ValueError, but this constructor has always
            # raised ConfigurationError for a mis-sized response (see
            # `_set_vectors`, which still guards the `from_chunks` path).
            raise ConfigurationError(f"Knowledge base '{name}': {exc}") from exc
        self._set_vectors(name, vectors)

    @classmethod
    def from_chunks(
        cls,
        name: str,
        chunks: List[_Chunk],
        vectors: List[List[float]],
        embedding_model: Any,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
        query_expansion_model: Any = None,
        query_expansion_count: int = 3,
        description: Optional[str] = None,
    ) -> "VectorKnowledgeBase":
        """Build directly from pre-parsed chunks and pre-computed vectors,
        skipping both the file-parsing pipeline and the embedding call for
        those chunks. `embedding_model` is still required and resolved
        (cheap, no API call) -- query() needs a live embeddings model to
        embed the QUERY text at search time, even though the document
        vectors themselves are supplied pre-computed. Used by the backend's
        DB-backed ingestion path (see ui/backend/knowledge_bases.py)."""
        self = cls.__new__(cls)
        self.path = None
        if not chunks:
            raise ConfigurationError(f"Knowledge base '{name}' has no readable documents")
        self._init_common(name, chunks, top_k, score_threshold, rerank_model, candidate_k,
                           query_expansion_model, query_expansion_count, description)
        self._embeddings = resolve_embedding_model(embedding_model)
        # Kept for metering only: the query-time `embed_query` call below is
        # billed against this spec, and there is nothing to bill when the
        # customer handed in a live model or a `"fake:"` spec.
        self._embedding_spec = billable_spec(embedding_model)
        self._set_vectors(name, vectors)
        return self

    def _init_common(
        self, name, chunks, top_k, score_threshold, rerank_model, candidate_k,
        query_expansion_model, query_expansion_count, description=None,
    ) -> None:
        _require_numpy()

        self.name = name
        self.description = description
        self.default_top_k = top_k
        self.score_threshold = score_threshold
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)
        self.query_expansion_model = query_expansion_model
        self.query_expansion_count = query_expansion_count
        # Defensive copy, matching LocalFolderKnowledgeBase._init_from_chunks:
        # a caller mutating the list it handed in must not reshape this KB's
        # index behind its (already-computed) vector matrix.
        self._chunks = list(chunks)

    def _set_vectors(self, name: str, vectors: List[List[float]]) -> None:
        import numpy as np

        if not vectors or len(vectors) != len(self._chunks):
            raise ConfigurationError(
                f"Knowledge base '{name}': embedding model returned "
                f"{len(vectors)} vectors for {len(self._chunks)} chunks"
            )
        matrix = np.array(vectors, dtype=np.float64)
        self._matrix = normalize_rows(matrix)

    def _embed_chunks(self, embedding_model: Any, cache_path: Optional[str | Path]) -> List[List[float]]:
        texts = [c.text for c in self._chunks]

        if cache_path is None:
            return embed_documents_in_batches(self._embeddings, texts)

        if not isinstance(embedding_model, str):
            warnings.warn(
                f"Knowledge base '{self.name}': cache_path is set but "
                "embedding_model is not a string spec, so it has no stable "
                "cache key — caching is skipped.",
                stacklevel=3,
            )
            return embed_documents_in_batches(self._embeddings, texts)

        cache_path = Path(cache_path)
        model_spec = embedding_model
        cache = _load_embedding_cache(cache_path, model_spec)

        keys = [_chunk_cache_key(model_spec, text) for text in texts]
        missing = [i for i, key in enumerate(keys) if key not in cache]

        if missing:
            new_vectors = embed_documents_in_batches(
                self._embeddings, [texts[i] for i in missing]
            )
            for i, vector in zip(missing, new_vectors):
                cache[keys[i]] = vector
            _save_embedding_cache(cache_path, model_spec, cache)
        elif not cache_path.exists():
            _save_embedding_cache(cache_path, model_spec, cache)

        return [cache[key] for key in keys]

    def _vector_leg(self, query_text: str, fetch_k: int) -> List[int]:
        import numpy as np

        query_vec = np.array(self._embeddings.embed_query(query_text), dtype=np.float64)
        report_query_embedding_usage(self._embedding_spec, query_text)
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

    def search(self, query: str, top_k: Optional[int] = None) -> List[_Chunk]:
        # Mirrors LocalFolderKnowledgeBase.search()'s `top_k or self.default_top_k`:
        # a caller-supplied top_k=0 falls back to default_top_k (intentional parity).
        top_k = top_k or self.default_top_k

        variants = _query_variants(query, self.query_expansion_model, self.query_expansion_count)
        fetch_k = _rerank_fetch_k(top_k, self._candidate_k, self._reranker)
        ranked_indices = _rrf_retrieve(variants, [self._vector_leg], fetch_k)
        # The score half of each tuple is a synthetic rank-derived placeholder,
        # not a real retrieval score -- RRF has already re-ordered by fused
        # rank, and _rerank_candidates only reads candidate order/chunk.text.
        results = [(float(-i), self._chunks[idx]) for i, idx in enumerate(ranked_indices[:fetch_k])]
        results = _rerank_candidates(query, results, self._reranker, top_k)
        return [chunk for _score, chunk in results]
