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


class _MarkerEmbedding(Embeddings):
    """Embeds independent of the literal words the BM25 leg matches on, so a
    chunk can be a strong keyword match yet score low on the vector leg --
    used to verify `score_threshold` gates only the vector leg (a chunk
    below the cosine cutoff can still surface via a BM25 match)."""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return [1.0, 0.0] if "VECMATCH" in text else [0.0, 1.0]


def test_hybrid_score_threshold_only_gates_vector_leg(tmp_path):
    # doc0 is a strong BM25 match for "widget" but, via _MarkerEmbedding,
    # orthogonal (cosine 0) to the query -- score_threshold=0.5 filters it
    # out of the vector leg entirely, yet it must still surface via BM25.
    docs = (
        "widget widget widget installation and assembly instructions VECMATCH",
        "unrelated filler content about nothing relevant",
    )
    kb = _kb_with_docs(
        tmp_path, *docs, top_k=2, embedding_model=_MarkerEmbedding(), score_threshold=0.5
    )
    result = kb.query("widget")
    assert "doc0.txt" in result


def test_hybrid_kb_query_expansion_recovers_chunk_literal_query_misses(tmp_path):
    # "sprocket" embeds as the zero vector on both concept dimensions, so
    # the vector leg's cosine scores are uniformly 0 across every chunk --
    # not just doc1 -- and argsort's tie-break deterministically favors the
    # highest chunk index among ties. doc2 (a decoy, unrelated to either
    # concept) is placed last precisely so it -- not doc1 -- absorbs that
    # tie-break noise for the plain "sprocket" query. The expansion variant
    # "refund" matches doc1 on BOTH legs (doc1's text literally contains
    # "refund" for the BM25 leg, and embeds as the refund-concept vector for
    # the vector leg), giving doc1 two RRF votes -- reliably outranking the
    # decoy's single tie-break vote regardless of dict/list insertion order.
    docs = (
        "shipping info only",
        "if unsatisfied, refund or money back guaranteed",
        "unrelated filler text about nothing relevant",
    )
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
