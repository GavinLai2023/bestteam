"""Tests for the vector knowledge base."""
import json
from typing import List
from unittest.mock import patch

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings

from bestteam.core.embeddings import resolve_embedding_model as _resolve_embedding_model
from bestteam.core.vector_knowledge_base import VectorKnowledgeBase
from bestteam.exceptions import ConfigurationError


# ---------------------------------------------------------------------------
# A small embedding model with semantically meaningful vectors, for testing
# relevance ranking. DeterministicFakeEmbedding's hash-based vectors aren't
# suitable for asserting *which* document should rank higher.
# ---------------------------------------------------------------------------

_VOCAB = ["fruit", "apple", "orchard", "car", "engine", "tire"]


class _KeywordEmbedding(Embeddings):
    """Embeds text as a one-hot-ish vector over a small fixed vocabulary."""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        lowered = text.lower()
        return [1.0 if word in lowered else 0.0 for word in _VOCAB]


class _BrokenEmbedding(Embeddings):
    """Returns the wrong number of vectors."""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return []

    def embed_query(self, text: str) -> List[float]:
        return [1.0, 0.0]


# ---------------------------------------------------------------------------
# _resolve_embedding_model
# ---------------------------------------------------------------------------

def test_resolve_embedding_model_fake_default_dim():
    model = _resolve_embedding_model("fake:")
    assert isinstance(model, DeterministicFakeEmbedding)
    assert len(model.embed_query("hello")) == 32


def test_resolve_embedding_model_fake_custom_dim():
    model = _resolve_embedding_model("fake:8")
    assert isinstance(model, DeterministicFakeEmbedding)
    assert len(model.embed_query("hello")) == 8


def test_resolve_embedding_model_fake_invalid_dim():
    with pytest.raises(ConfigurationError, match="fake:"):
        _resolve_embedding_model("fake:abc")


def test_resolve_embedding_model_passthrough_instance():
    embedding = _KeywordEmbedding()
    assert _resolve_embedding_model(embedding) is embedding


def test_resolve_embedding_model_missing_langchain_for_provider_string():
    with patch.dict("sys.modules", {"langchain.embeddings": None, "langchain": None}):
        with pytest.raises(ConfigurationError, match="langchain"):
            _resolve_embedding_model("openai:text-embedding-3-small")


def test_resolve_embedding_model_invalid_spec():
    with pytest.raises(ConfigurationError, match="Unsupported embedding model spec"):
        _resolve_embedding_model(123)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------

def test_vector_knowledge_base_raises_without_numpy(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with patch.dict("sys.modules", {"numpy": None}):
        with pytest.raises(ConfigurationError, match="numpy"):
            VectorKnowledgeBase("kb", tmp_path, embedding_model="fake:8")


def test_vector_knowledge_base_raises_for_empty_folder(tmp_path):
    with pytest.raises(ConfigurationError, match="no readable documents"):
        VectorKnowledgeBase("kb", tmp_path, embedding_model="fake:8")


@pytest.mark.parametrize("chunk_size,chunk_overlap", [(100, 100), (100, 150)])
def test_rejects_chunk_overlap_gte_chunk_size(tmp_path, chunk_size, chunk_overlap):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="chunk_overlap"):
        VectorKnowledgeBase(
            "kb", tmp_path, embedding_model="fake:8",
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        )


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_rejects_non_positive_chunk_size(tmp_path, chunk_size):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="chunk_size"):
        VectorKnowledgeBase(
            "kb", tmp_path, embedding_model="fake:8",
            chunk_size=chunk_size, chunk_overlap=0,
        )


def test_vector_kb_raises_if_embedding_count_mismatches(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="embedding model returned"):
        VectorKnowledgeBase("kb", tmp_path, embedding_model=_BrokenEmbedding())


# ---------------------------------------------------------------------------
# Query relevance / score_threshold / top_k edge cases
# ---------------------------------------------------------------------------

@pytest.fixture
def docs_kb(tmp_path):
    (tmp_path / "fruit.md").write_text(
        "Apples are a popular fruit grown in orchards across the country.",
        encoding="utf-8",
    )
    (tmp_path / "cars.md").write_text(
        "Cars require regular maintenance such as oil changes and tire rotations.",
        encoding="utf-8",
    )
    return VectorKnowledgeBase("docs", tmp_path, embedding_model=_KeywordEmbedding())


def test_vector_kb_query_returns_relevant_source(docs_kb):
    result = docs_kb.query("apple orchard fruit", top_k=1)
    assert "fruit.md" in result
    assert "cars.md" not in result


def test_vector_kb_score_threshold_filters_low_scores(docs_kb):
    result = docs_kb.query("apple orchard fruit", top_k=1)
    assert "fruit.md" in result

    docs_kb.score_threshold = 1.5  # unattainable cosine similarity
    result = docs_kb.query("apple orchard fruit")
    assert "No results found in knowledge base 'docs'" in result


def test_vector_kb_score_threshold_none_returns_top_k_regardless(tmp_path):
    (tmp_path / "fruit.md").write_text(
        "Apples are a popular fruit grown in orchards across the country.",
        encoding="utf-8",
    )
    (tmp_path / "cars.md").write_text(
        "Cars require regular maintenance such as oil changes and tire rotations.",
        encoding="utf-8",
    )
    kb = VectorKnowledgeBase("docs", tmp_path, embedding_model=_KeywordEmbedding(), top_k=2)
    # A query with zero overlap with the vocabulary still returns top_k results.
    result = kb.query("completely unrelated topic")
    assert "Knowledge base 'docs' results for" in result
    assert "fruit.md" in result
    assert "cars.md" in result


def test_vector_kb_top_k_larger_than_chunk_count(tmp_path):
    (tmp_path / "doc.md").write_text("Apples are a fruit.", encoding="utf-8")
    kb = VectorKnowledgeBase("docs", tmp_path, embedding_model=_KeywordEmbedding(), top_k=100)
    result = kb.query("apple fruit")
    assert "doc.md" in result


# ---------------------------------------------------------------------------
# make_knowledge_base_tool
# ---------------------------------------------------------------------------

def test_make_knowledge_base_tool_name_and_delegation_for_vector_kb(docs_kb):
    from bestteam.core.knowledge_base import make_knowledge_base_tool

    tool = make_knowledge_base_tool(docs_kb)
    assert tool.__name__ == "docs"
    assert "docs" in tool.__doc__
    assert tool("apple orchard fruit") == docs_kb.query("apple orchard fruit")


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------

def test_vector_kb_cache_writes_file_and_skips_recompute(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text("Apples are a fruit.", encoding="utf-8")

    cache_path = tmp_path / "cache.json"
    kb1 = VectorKnowledgeBase("docs", docs_dir, embedding_model="fake:8", cache_path=cache_path)
    assert cache_path.exists()

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["model"] == "fake:8"
    assert len(data["entries"]) == len(kb1._chunks)

    def _boom(self, texts):
        raise AssertionError("embed_documents should not be called when cache hits")

    monkeypatch.setattr(DeterministicFakeEmbedding, "embed_documents", _boom)

    kb2 = VectorKnowledgeBase("docs", docs_dir, embedding_model="fake:8", cache_path=cache_path)
    assert kb2.query("apples") == kb1.query("apples")


def test_vector_kb_cache_partial_reembed_on_new_chunk(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "fruit.md").write_text("Apples are a fruit.", encoding="utf-8")

    cache_path = tmp_path / "cache.json"
    VectorKnowledgeBase("docs", docs_dir, embedding_model="fake:8", cache_path=cache_path)

    (docs_dir / "cars.md").write_text("Cars need oil changes.", encoding="utf-8")

    calls = []
    original = DeterministicFakeEmbedding.embed_documents

    def _tracking(self, texts):
        calls.append(list(texts))
        return original(self, texts)

    monkeypatch.setattr(DeterministicFakeEmbedding, "embed_documents", _tracking)

    VectorKnowledgeBase("docs", docs_dir, embedding_model="fake:8", cache_path=cache_path)

    assert len(calls) == 1
    assert calls[0] == ["Cars need oil changes."]


def test_vector_kb_cache_invalidated_on_model_change(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text("Apples are a fruit.", encoding="utf-8")

    cache_path = tmp_path / "cache.json"
    VectorKnowledgeBase("docs", docs_dir, embedding_model="fake:8", cache_path=cache_path)

    kb2 = VectorKnowledgeBase("docs", docs_dir, embedding_model="fake:16", cache_path=cache_path)
    assert kb2._matrix.shape[1] == 16

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["model"] == "fake:16"


def test_vector_kb_cache_handles_corrupt_file(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text("Apples are a fruit.", encoding="utf-8")

    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not valid json", encoding="utf-8")

    kb = VectorKnowledgeBase("docs", docs_dir, embedding_model="fake:8", cache_path=cache_path)
    assert "doc.md" in kb.query("apples")

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["model"] == "fake:8"


def test_vector_kb_cache_skipped_for_embeddings_instance(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text("Apples are a fruit.", encoding="utf-8")

    cache_path = tmp_path / "cache.json"
    with pytest.warns(UserWarning, match="cach"):
        kb = VectorKnowledgeBase(
            "docs", docs_dir, embedding_model=_KeywordEmbedding(), cache_path=cache_path
        )
    assert "doc.md" in kb.query("apple fruit")
    assert not cache_path.exists()


# ---------------------------------------------------------------------------
# YAML loader integration
# ---------------------------------------------------------------------------

def test_loader_resolves_vector_knowledge_base(tmp_path):
    from bestteam import load_workflow

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "fruit.md").write_text(
        "Apples are a popular fruit grown in orchards across the country.",
        encoding="utf-8",
    )

    yaml_text = """
name: vector_kb_test
knowledge_bases:
  - name: product_docs
    type: vector
    path: ./docs
    embedding_model: "fake:8"
agents:
  - name: helper
    role: Helper
    goal: Answer questions using the product docs
    model: "fake:done"
    tools: [product_docs]
teams:
  - name: team1
    agents: [helper]
    mode: sequential
workflow:
  steps: [team1]
"""
    p = tmp_path / "workflow.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    wf = load_workflow(str(p))
    agent = wf.steps[0].agents[0]

    assert len(agent.tools) == 1
    tool = agent.tools[0]
    assert tool.__name__ == "product_docs"
    assert "Knowledge base 'product_docs' results for" in tool("apples orchards")


def test_loader_unknown_knowledge_base_type_raises(tmp_path):
    from bestteam import load_workflow

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "fruit.md").write_text("Apples are a fruit.", encoding="utf-8")

    yaml_text = """
name: bad_type_test
knowledge_bases:
  - name: product_docs
    type: bogus
    path: ./docs
agents:
  - name: helper
    role: Helper
    goal: Answer questions
    model: "fake:done"
    tools: [product_docs]
teams:
  - name: team1
    agents: [helper]
    mode: sequential
workflow:
  steps: [team1]
"""
    p = tmp_path / "workflow.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unknown type 'bogus'.*Valid types"):
        load_workflow(str(p))


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


def test_vector_kb_rerank_honors_per_call_top_k_above_default(tmp_path):
    # Constructor top_k=1 -> default candidate_k = 4. A per-call top_k=10
    # must still be able to return up to 10 results, not be capped at 4
    # by the construction-time candidate pool size.
    docs = ["fruit"] * 10
    kb = _vector_kb_with_docs(
        tmp_path,
        *docs,
        top_k=1,
        rerank_model="fake:",
        embedding_model=_KeywordEmbedding(),  # all docs tie on cosine score
    )
    result = kb.query("fruit", top_k=10)
    assert sum(f"doc{i}.txt" in result for i in range(10)) == 10


def test_vector_kb_candidate_k_rejects_below_top_k(tmp_path):
    with pytest.raises(ConfigurationError, match="candidate_k"):
        _vector_kb_with_docs(tmp_path, "hello world", top_k=5, candidate_k=2, rerank_model="fake:")


def test_vector_kb_bad_rerank_spec_raises_at_construction(tmp_path):
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        _vector_kb_with_docs(tmp_path, "hello world", rerank_model="not-a-real-spec")


def test_loader_resolves_vector_kb_cache_path_relative_to_workflow(tmp_path):
    from bestteam import load_workflow

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "fruit.md").write_text("Apples are a fruit.", encoding="utf-8")

    yaml_text = """
name: vector_kb_cache_test
knowledge_bases:
  - name: product_docs
    type: vector
    path: ./docs
    embedding_model: "fake:8"
    cache_path: ./.cache/embeddings.json
agents:
  - name: helper
    role: Helper
    goal: Answer questions using the product docs
    model: "fake:done"
    tools: [product_docs]
teams:
  - name: team1
    agents: [helper]
    mode: sequential
workflow:
  steps: [team1]
"""
    p = tmp_path / "workflow.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    load_workflow(str(p))

    assert (tmp_path / ".cache" / "embeddings.json").exists()
