from __future__ import annotations

from typing import Any

from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings

from ..exceptions import ConfigurationError

_DEFAULT_FAKE_EMBEDDING_DIM = 32


def resolve_embedding_model(model: Any) -> Embeddings:
    """Accept either a ready-made embeddings model or a provider model spec string.

    Mirrors `_resolve_model()` in the LangGraph adapter: customers with a
    `langchain_core.embeddings.Embeddings` instance (real, fake, or a custom
    test double) can pass it directly. String specs are resolved lazily.

    Shared by `core/vector_knowledge_base.py` and `core/memory.py`'s hybrid
    recall, so both features use one embedding-model-resolution
    implementation (mirrors `core/text_tokenize.py`'s extraction for the
    shared BM25 tokenizer).
    """
    if isinstance(model, Embeddings):
        return model
    if isinstance(model, str):
        # "fake:<dim>" (dim optional) lets customers dry-run embeddings from
        # plain config — no provider package or API key required, $0 cost.
        if model.startswith("fake:"):
            dim_str = model[len("fake:") :]
            if dim_str:
                try:
                    dim = int(dim_str)
                except ValueError as exc:
                    raise ConfigurationError(
                        f"Invalid 'fake:' embedding spec {model!r}: dimension "
                        "must be an integer, e.g. 'fake:32'."
                    ) from exc
                if dim <= 0:
                    raise ConfigurationError(
                        f"Invalid 'fake:' embedding spec {model!r}: dimension "
                        "must be positive."
                    )
            else:
                dim = _DEFAULT_FAKE_EMBEDDING_DIM
            return DeterministicFakeEmbedding(size=dim)
        try:
            from langchain.embeddings import init_embeddings
        except ImportError as exc:
            raise ConfigurationError(
                "Resolving an embedding model from a string name requires the "
                "optional 'langchain' package (pip install langchain). "
                "Alternatively, pass an Embeddings instance directly."
            ) from exc
        return init_embeddings(model)
    raise ConfigurationError(
        f"Unsupported embedding model spec {model!r}: pass a provider "
        "embedding model name (str) or a langchain Embeddings instance."
    )


def normalize_rows(matrix: Any) -> Any:
    """L2-normalize each row; rows with zero norm are left as all-zeros.

    A zero vector naturally yields a cosine similarity of 0 against any
    query, which is the desired "no signal" behavior rather than a
    division-by-zero NaN.
    """
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe_norms = np.where(norms == 0, 1.0, norms)
    return matrix / safe_norms
