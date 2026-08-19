from __future__ import annotations

import math
import time
from typing import Any, Callable, List, Optional, Sequence

from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings

from ..exceptions import ConfigurationError
from .text_tokenize import _CJK_RUN_RE
from .tool_context import add_usage

_DEFAULT_FAKE_EMBEDDING_DIM = 32

# 100 chunks of ~1000 characters is roughly 25k tokens -- far below any
# provider's per-request limit, and small enough that a failed batch is cheap
# to redo. Deliberately not a parameter: no caller has a reason to differ.
_EMBED_BATCH_SIZE = 100
_EMBED_ATTEMPTS = 3


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


def embed_documents_in_batches(
    embeddings: Embeddings,
    texts: Sequence[str],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> List[List[float]]:
    """Embed `texts` in batches of `_EMBED_BATCH_SIZE`, retrying a failed batch.

    A provider client already splits one `embed_documents` call into several
    requests internally, but to the *caller* that is still one all-or-nothing
    Python call: a hiccup on the 37th request throws away the 36 that were
    computed and paid for. Batching here makes a failure cost one batch, and
    **only the failing batch is retried** -- up to `_EMBED_ATTEMPTS` attempts,
    backing off 1s then 2s, after which the original exception is re-raised
    for the caller to report. Any exception retries: classifying provider
    exceptions would mean tracking every provider's error taxonomy, so an
    authentication failure waits 3 seconds before surfacing, which is an
    acceptable price for not silently giving up on a rate limit.

    A batch that comes back with the wrong number of vectors raises
    `ConfigurationError` immediately, without retrying -- a deterministic
    answer will not change on a second ask, and a model whose response is the
    wrong shape is a misconfiguration like any other in this module.

    `sleep` is injectable so tests need not actually wait. Empty input makes
    no provider call at all.
    """
    vectors: List[List[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = list(texts[start : start + _EMBED_BATCH_SIZE])
        for attempt in range(1, _EMBED_ATTEMPTS + 1):
            try:
                result = embeddings.embed_documents(batch)
            except Exception:
                if attempt == _EMBED_ATTEMPTS:
                    raise
                sleep(float(attempt))  # 1.0s, then 2.0s
            else:
                break
        if len(result) != len(batch):
            raise ConfigurationError(
                f"embedding model returned {len(result)} vectors for {len(batch)} texts"
            )
        vectors.extend(result)
    return vectors


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


def estimate_embedding_tokens(text: str) -> int:
    """Estimate how many tokens embedding `text` will be billed for.

    LangChain's `Embeddings` interface returns vectors and nothing else --
    no provider reports token usage for an embedding call the way a chat
    model does -- so a metered embedding row has to estimate. The ratio used
    here is one token per CJK character plus one per four other characters,
    which is roughly where the mainstream BPE tokenizers land for each
    script. Expect the estimate to be within about **±30%** of a provider's
    own count: enough to keep a spend cap honest, not enough to reconcile
    against a bill. Kana and Hangul are inside `_CJK_RUN_RE`'s ranges too, so
    they are counted per character, which is roughly where Japanese and Korean
    land as well.

    Deterministic and dependency-free (no `tiktoken`), and it reuses
    `text_tokenize._CJK_RUN_RE` so "CJK" means exactly what the BM25
    tokenizer already means by it.
    """
    if not text:
        return 0
    cjk_chars = sum(len(run) for run in _CJK_RUN_RE.findall(text))
    return cjk_chars + math.ceil((len(text) - cjk_chars) / 4)


def billable_spec(model: Any) -> Optional[str]:
    """The catalog spec an embedding call should be metered against, or None
    when there is nothing billable to meter: a live `Embeddings` instance has
    no spec string for `model_catalog` to price, and a `"fake:"` spec is $0 by
    construction. Shared by the query-time path (below) and the backend's
    ingestion metering (`ui/backend/ingestion.py`) so both agree on what
    counts as spend."""
    if isinstance(model, str) and not model.startswith("fake:"):
        return model
    return None


def report_query_embedding_usage(spec: Optional[str], query_text: str) -> None:
    """Meter one `embed_query` call on `spec` into the active tool-call
    context (`core/tool_context.py`), from which the adapter's tool loop
    forwards it onto the agent's `agent_completed` event. `spec` is a
    `billable_spec()` result, so None means "nothing to bill"; a no-op
    outside a run either way."""
    if spec is None:
        return
    add_usage(
        {
            "model": spec,
            "input_tokens": estimate_embedding_tokens(query_text),
            "output_tokens": 0,
        }
    )
