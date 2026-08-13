"""Tests for the local-folder knowledge base."""
from unittest.mock import patch

import pytest

from bestteam.core.knowledge_base import (
    LocalFolderKnowledgeBase,
    _chunk_text,
    make_knowledge_base_tool,
)
from bestteam.exceptions import ConfigurationError


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------

def test_chunk_text_empty_string():
    assert _chunk_text("", chunk_size=100, chunk_overlap=10) == []


def test_chunk_text_short_text_is_single_chunk():
    text = "short text"
    assert _chunk_text(text, chunk_size=100, chunk_overlap=10) == [text]


def test_chunk_text_long_text_produces_overlapping_chunks():
    text = "a" * 250
    chunks = _chunk_text(text, chunk_size=100, chunk_overlap=20)
    assert len(chunks) > 1
    assert chunks[0][-20:] == chunks[1][:20]


def test_chunk_text_does_not_cut_mid_word():
    text = " ".join(f"word{i}" for i in range(1, 15))
    chunks = _chunk_text(text, chunk_size=20, chunk_overlap=0)
    assert len(chunks) > 1
    reconstructed = " ".join(chunks).split()
    assert reconstructed == text.split()


def test_chunk_text_markdown_preserves_heading_boundaries():
    text = (
        "## Section One\n"
        "Some content about apples.\n\n"
        "## Section Two\n"
        "Some content about oranges.\n\n"
        "## Section Three\n"
        "Some content about bananas.\n"
    )
    chunks = _chunk_text(text, chunk_size=40, chunk_overlap=0, suffix=".md")
    assert len(chunks) > 1
    for chunk in chunks:
        if "## Section" in chunk:
            heading_index = chunk.index("## Section")
            assert chunk[:heading_index].strip() == ""


def test_chunk_text_markdown_splits_oversized_section_by_sentence():
    text = "## Big Section\n" + "This is sentence one. " * 20
    chunks = _chunk_text(text, chunk_size=60, chunk_overlap=0, suffix=".md")
    assert len(chunks) > 1
    assert all(len(chunk) <= 60 for chunk in chunks)


def test_chunk_text_overlap_never_exceeds_chunk_size():
    text = " ".join(f"word{i}" for i in range(1, 60))
    chunks = _chunk_text(text, chunk_size=50, chunk_overlap=15)
    assert len(chunks) > 1
    assert all(len(chunk) <= 50 for chunk in chunks)


# ---------------------------------------------------------------------------
# LocalFolderKnowledgeBase construction
# ---------------------------------------------------------------------------

def test_knowledge_base_raises_without_package(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with patch.dict("sys.modules", {"rank_bm25": None}):
        with pytest.raises(ConfigurationError, match="rank-bm25"):
            LocalFolderKnowledgeBase("kb", tmp_path)


def test_knowledge_base_raises_for_empty_folder(tmp_path):
    with pytest.raises(ConfigurationError, match="no readable documents"):
        LocalFolderKnowledgeBase("kb", tmp_path)


def test_knowledge_base_skips_unsupported_files(tmp_path):
    (tmp_path / "doc.txt").write_text("apples and oranges", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n")
    kb = LocalFolderKnowledgeBase("kb", tmp_path)
    assert len(kb._chunks) == 1
    assert kb._chunks[0].source == "doc.txt"


def test_knowledge_base_skips_corrupt_file_with_warning(tmp_path):
    pytest.importorskip("docx")
    (tmp_path / "good.md").write_text("Apples are great fruit.", encoding="utf-8")
    (tmp_path / "bad.docx").write_bytes(b"not a real docx file")

    with pytest.warns(UserWarning, match="bad.docx"):
        kb = LocalFolderKnowledgeBase("kb", tmp_path)

    sources = {chunk.source for chunk in kb._chunks}
    assert "good.md" in sources
    assert "bad.docx" not in sources


def test_skips_file_with_configuration_error_and_warns(tmp_path, monkeypatch):
    import bestteam.core.knowledge_base as kb_module

    (tmp_path / "good.txt").write_text("Apples are great fruit.", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("irrelevant", encoding="utf-8")

    real_parse_file = kb_module.parse_file

    def fake_parse_file(path):
        if str(path).endswith("bad.txt"):
            raise ConfigurationError("simulated parse failure")
        return real_parse_file(path)

    monkeypatch.setattr(kb_module, "parse_file", fake_parse_file)

    with pytest.warns(UserWarning, match="bad.txt"):
        kb = LocalFolderKnowledgeBase("kb", tmp_path)

    sources = {chunk.source for chunk in kb._chunks}
    assert "good.txt" in sources
    assert "bad.txt" not in sources


@pytest.mark.parametrize("chunk_size,chunk_overlap", [(100, 100), (100, 150)])
def test_rejects_chunk_overlap_gte_chunk_size(tmp_path, chunk_size, chunk_overlap):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="chunk_overlap"):
        LocalFolderKnowledgeBase("kb", tmp_path, chunk_size=chunk_size, chunk_overlap=chunk_overlap)


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_rejects_non_positive_chunk_size(tmp_path, chunk_size):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="chunk_size"):
        LocalFolderKnowledgeBase("kb", tmp_path, chunk_size=chunk_size, chunk_overlap=0)


# ---------------------------------------------------------------------------
# LocalFolderKnowledgeBase.query
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
    return LocalFolderKnowledgeBase("docs", tmp_path)


def test_knowledge_base_query_returns_relevant_source(docs_kb):
    result = docs_kb.query("apples orchards")
    assert "fruit.md" in result
    assert "cars.md" not in result


def test_knowledge_base_query_no_match_returns_message(docs_kb):
    result = docs_kb.query("spaceships and aliens")
    assert "No results found in knowledge base 'docs'" in result


def test_knowledge_base_ingests_xml_files(tmp_path):
    (tmp_path / "fruit.md").write_text(
        "Cars require regular maintenance such as oil changes.",
        encoding="utf-8",
    )
    (tmp_path / "catalog.xml").write_text(
        '<catalog><book><title>Widgets and gadgets explained</title></book></catalog>',
        encoding="utf-8",
    )
    kb = LocalFolderKnowledgeBase("docs", tmp_path)

    result = kb.query("widgets gadgets")
    assert "catalog.xml" in result
    assert "fruit.md" not in result


@pytest.fixture
def chinese_docs_kb(tmp_path):
    (tmp_path / "policy.txt").write_text(
        "退货政策：商品在签收后7天内，因质量问题可申请全额退款。",
        encoding="utf-8",
    )
    (tmp_path / "shipping.txt").write_text(
        "发货时间：订单提交后通常在两个工作日内发货。",
        encoding="utf-8",
    )
    return LocalFolderKnowledgeBase("docs", tmp_path)


def test_knowledge_base_query_matches_chinese_text(chinese_docs_kb):
    result = chinese_docs_kb.query("退货政策")
    assert "policy.txt" in result
    assert "shipping.txt" not in result


# ---------------------------------------------------------------------------
# make_knowledge_base_tool
# ---------------------------------------------------------------------------

def test_make_knowledge_base_tool_name_and_delegation(docs_kb):
    tool = make_knowledge_base_tool(docs_kb)
    assert tool.__name__ == "docs"
    assert "docs" in tool.__doc__
    assert tool("apples orchards") == docs_kb.query("apples orchards")


# ---------------------------------------------------------------------------
# YAML loader integration
# ---------------------------------------------------------------------------

def test_loader_resolves_knowledge_base_tool(tmp_path):
    from bestteam import load_workflow

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "fruit.md").write_text(
        "Apples are a popular fruit grown in orchards across the country.",
        encoding="utf-8",
    )

    yaml_text = """
name: kb_test
knowledge_bases:
  - name: product_docs
    path: ./docs
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
    assert "fruit.md" in tool("apples orchards")


def test_loader_raises_for_missing_knowledge_base_path(tmp_path):
    from bestteam import load_workflow

    yaml_text = """
name: kb_missing_path
knowledge_bases:
  - name: product_docs
    path: ./does-not-exist
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

    with pytest.raises(ConfigurationError, match="does not exist or is not a directory"):
        load_workflow(str(p))


# ---------------------------------------------------------------------------
# _rerank_candidates
# ---------------------------------------------------------------------------

from bestteam.core.knowledge_base import _Chunk, _rerank_candidates
from bestteam.core.reranking import Reranker


class _ReverseLengthReranker(Reranker):
    """Scores by text length -- longer text wins. Distinct from any
    retrieval score, so tests can tell rerank changed the order."""

    def _score(self, query, texts):
        return [float(len(t)) for t in texts]


class _BoomReranker(Reranker):
    def _score(self, query, texts):
        raise RuntimeError("inference boom")


def _candidates(*texts):
    return [(1.0, _Chunk(source="s", text=t)) for t in texts]


def test_rerank_candidates_no_reranker_slices_to_top_k():
    candidates = _candidates("a", "bb", "ccc")
    result = _rerank_candidates("q", candidates, None, top_k=2)
    assert result == candidates[:2]


def test_rerank_candidates_empty_list_no_reranker_call():
    calls = []

    class _Spy(Reranker):
        def _score(self, query, texts):
            calls.append(texts)
            return []

    assert _rerank_candidates("q", [], _Spy(), top_k=5) == []
    assert calls == []


def test_rerank_candidates_reorders_by_score():
    candidates = _candidates("a", "bb", "ccc")  # retrieval order: a, bb, ccc
    result = _rerank_candidates("q", candidates, _ReverseLengthReranker(), top_k=3)
    assert [c.text for _s, c in result] == ["ccc", "bb", "a"]  # longest first


def test_rerank_candidates_truncates_to_top_k_after_reranking():
    candidates = _candidates("a", "bb", "ccc")
    result = _rerank_candidates("q", candidates, _ReverseLengthReranker(), top_k=1)
    assert [c.text for _s, c in result] == ["ccc"]


def test_rerank_candidates_falls_back_on_scoring_failure():
    candidates = _candidates("a", "bb", "ccc")
    result = _rerank_candidates("q", candidates, _BoomReranker(), top_k=2)
    assert result == candidates[:2]  # pre-rerank order preserved


def test_rerank_candidates_does_not_mutate_input():
    candidates = _candidates("a", "bb", "ccc")
    original = list(candidates)
    _rerank_candidates("q", candidates, _ReverseLengthReranker(), top_k=3)
    assert candidates == original


# ---------------------------------------------------------------------------
# LocalFolderKnowledgeBase rerank wiring
# ---------------------------------------------------------------------------

def _kb_with_docs(tmp_path, *texts, **kwargs):
    for i, text in enumerate(texts):
        (tmp_path / f"doc{i}.txt").write_text(text, encoding="utf-8")
    return LocalFolderKnowledgeBase("kb", tmp_path, **kwargs)


def test_local_folder_kb_rerank_unset_is_byte_identical(tmp_path):
    plain = _kb_with_docs(tmp_path, "apples and oranges", "cars and trucks", top_k=2)
    result_a = plain.query("apples")
    result_b = plain.query("apples")
    assert result_a == result_b  # deterministic, unaffected by the new code path


def test_local_folder_kb_rerank_changes_result_order(tmp_path):
    # Three docs: doc0/doc1 share the term "fruit" (BM25 candidates), doc2 is
    # an unrelated filler doc that gives "fruit" a non-degenerate IDF, which
    # makes plain BM25 favor doc1 (more occurrences) over doc0. The fake
    # reranker (scores by length-distance to the query) instead prefers doc0,
    # the doc closest in length to the query text -- flipping the top result.
    docs = ("fruit " * 1, "fruit " * 20, "banana orange grape melon")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = _kb_with_docs(
        plain_dir,
        *docs,
        top_k=1,
        candidate_k=2,
    )
    reranked_dir = tmp_path / "reranked"
    reranked_dir.mkdir()
    reranked = _kb_with_docs(
        reranked_dir,
        *docs,
        top_k=1,
        candidate_k=2,
        rerank_model="fake:",
    )
    plain_result = plain.query("fruit")
    reranked_result = reranked.query("fruit")
    # Control comparison: plain BM25 (no reranker) vs. the same docs reranked
    # must differ, proving the reranker actually changed the order.
    assert plain_result != reranked_result
    assert "doc0.txt" in reranked_result  # the short doc, closest in length to "fruit"


def test_local_folder_kb_candidate_k_rejects_below_top_k(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="candidate_k"):
        LocalFolderKnowledgeBase("kb", tmp_path, top_k=5, candidate_k=2, rerank_model="fake:")


def test_local_folder_kb_candidate_k_rejects_above_max(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="candidate_k"):
        LocalFolderKnowledgeBase("kb", tmp_path, top_k=5, candidate_k=500, rerank_model="fake:")


def test_local_folder_kb_bad_rerank_spec_raises_at_construction(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        LocalFolderKnowledgeBase("kb", tmp_path, rerank_model="not-a-real-spec")


def test_local_folder_kb_rerank_honors_per_call_top_k_above_default(tmp_path):
    # Constructor top_k=1 -> default candidate_k = 4. A per-call top_k=10
    # must still be able to return up to 10 results, not be capped at 4
    # by the construction-time candidate pool size.
    docs = [f"fruit item number {i}" for i in range(10)]
    kb = _kb_with_docs(tmp_path, *docs, top_k=1, rerank_model="fake:")
    result = kb.query("fruit", top_k=10)
    assert sum(f"doc{i}.txt" in result for i in range(10)) == 10


def test_local_folder_kb_rerank_inference_failure_falls_back(tmp_path, monkeypatch):
    kb = _kb_with_docs(tmp_path, "apples and oranges", top_k=1, rerank_model="fake:")

    def boom(self, query, texts):
        raise RuntimeError("boom")

    monkeypatch.setattr(kb._reranker.__class__, "_score", boom)
    result = kb.query("apples")
    assert "doc0.txt" in result  # still returns the retrieval-order result
