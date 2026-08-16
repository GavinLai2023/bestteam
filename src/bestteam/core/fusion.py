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
