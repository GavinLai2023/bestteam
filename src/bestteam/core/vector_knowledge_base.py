from __future__ import annotations

import hashlib
import json
import os
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..exceptions import ConfigurationError
from .embeddings import normalize_rows, resolve_embedding_model
from .knowledge_base import KnowledgeBase, _load_document_chunks, _validate_chunk_params


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

        self._chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        if not self._chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents in {self.path}"
            )

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

    def query(self, query: str, top_k: Optional[int] = None) -> str:
        import numpy as np

        # Mirrors LocalFolderKnowledgeBase.query()'s `top_k or self.default_top_k`:
        # a caller-supplied top_k=0 falls back to default_top_k (intentional parity).
        top_k = top_k or self.default_top_k

        query_vec = np.array(self._embeddings.embed_query(query), dtype=np.float64)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm

        scores = self._matrix @ query_vec

        k = min(top_k, len(scores))
        top_indices = np.argsort(scores)[::-1][:k]

        results = [(scores[i], self._chunks[i]) for i in top_indices]

        if self.score_threshold is not None:
            results = [(s, c) for s, c in results if s >= self.score_threshold]

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"

        lines = [f"Knowledge base '{self.name}' results for: {query}\n"]
        for i, (_score, chunk) in enumerate(results, 1):
            lines.append(f"{i}. [source: {chunk.source}]")
            lines.append(chunk.text.strip())
            lines.append("")
        return "\n".join(lines)
