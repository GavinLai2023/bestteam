"""Tests for the local-folder knowledge base."""
import io
from unittest.mock import patch

import pytest

from bestteam.core.knowledge_base import (
    LocalFolderKnowledgeBase,
    _chunk_document,
    _chunk_text,
    _has_extractable_text,
    _load_document_chunks,
    make_knowledge_base_tool,
)
from bestteam.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------

def test_chunk_text_empty_string():
    assert _chunk_text("", chunk_size=100, chunk_overlap=10) == []


def test_chunk_text_short_text_is_single_chunk():
    text = "short text"
    assert _chunk_text(text, chunk_size=100, chunk_overlap=10) == [text]


def test_chunk_text_long_text_produces_overlapping_chunks():
    text = "".join(str(i % 10) for i in range(250))
    chunks = _chunk_text(text, chunk_size=100, chunk_overlap=20)
    assert len(chunks) > 1
    assert chunks[0][-10:] in chunks[1]


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


def test_chunk_text_markdown_heading_never_stranded_without_body(tmp_path=None):
    text = (
        "## Refunds\n"
        + "Refunds are issued within 7 days of the request. " * 3
        + "\n\n## Shipping\n"
        + "Orders ship within two business days of confirmation. " * 3
    )
    chunks = _chunk_text(text, chunk_size=80, chunk_overlap=0, suffix=".md")
    for chunk in chunks:
        stripped = chunk.strip()
        if stripped.startswith("## "):
            # a chunk containing a heading must carry more than just the heading
            assert len(stripped) > len("## Refunds") + 5 or stripped not in ("## Refunds", "## Shipping"), (
                f"heading stranded without body content: {chunk!r}"
            )


def test_chunk_text_markdown_overlap_is_applied_at_chunk_boundaries():
    text = (
        "## Refunds\n"
        + "Refunds are issued within 7 days of the request. " * 3
        + "\n\n## Shipping\n"
        + "Orders ship within two business days of confirmation. " * 3
    )
    chunks = _chunk_text(text, chunk_size=80, chunk_overlap=20, suffix=".md")
    assert len(chunks) > 1
    # at least one adjacent pair shares overlapping trailing/leading text
    assert any(chunks[i][-10:] in chunks[i + 1] for i in range(len(chunks) - 1))


def test_chunk_text_overlap_never_exceeds_chunk_size():
    text = " ".join(f"word{i}" for i in range(1, 60))
    chunks = _chunk_text(text, chunk_size=50, chunk_overlap=15)
    assert len(chunks) > 1
    assert all(len(chunk) <= 50 for chunk in chunks)


def test_xml_chunks_split_on_element_boundaries():
    text = (
        "[XML: catalog.xml]\n"
        "<catalog>\n"
        '  <book id="bk101"> Widgets Explained\n'
        '  <book id="bk102"> Gadgets Explained\n'
        '  <book id="bk103"> Gizmos Explained'
    )
    chunks = _chunk_document("catalog.xml", text, chunk_size=60, chunk_overlap=0, suffix=".xml")
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 60
        for line in chunk.text.splitlines():
            if line.strip():
                assert line.lstrip().startswith("<") or line.startswith("[XML:")


def test_xml_oversized_leaf_falls_back_without_cutting_words():
    # A single leaf element too large to fit in one chunk even under its
    # ancestor path -- there is no deeper structure to split on, so this must
    # fall back to _DEFAULT_SEPARATORS. That fallback can't preserve a "<tag>"
    # prefix on every resulting line once it's forced down to word-level
    # splitting, so the guarantee this test checks is the honest one: no word
    # gets cut in half and no chunk exceeds chunk_size.
    long_title = " ".join(f"word{i}" for i in range(1, 30))
    text = (
        "[XML: catalog.xml]\n"
        "<catalog>\n"
        '  <book id="bk101">\n'
        f"    <title> {long_title}"
    )
    chunks = _chunk_document("catalog.xml", text, chunk_size=50, chunk_overlap=0, suffix=".xml")
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 50 for chunk in chunks)
    words = set(" ".join(chunk.text for chunk in chunks).split())
    assert {w for w in words if w.startswith("word")} == {f"word{i}" for i in range(1, 30)}


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
    with pytest.warns(UserWarning, match="Unsupported file type"):
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


def test_skips_a_mis_encoded_document_instead_of_ingesting_mojibake(tmp_path):
    # No monkeypatching: this is a real GBK-encoded file, the kind an
    # operator actually drops into a knowledge folder. Phase 4b's byte
    # refactor briefly made `parse_file` lenient, which turned this from
    # 'skipped with a warning' into 'silently indexed as replacement
    # characters' -- searchable nonsense nobody was told about.
    (tmp_path / "good.txt").write_text("Apples are great fruit.", encoding="utf-8")
    (tmp_path / "legacy.txt").write_bytes("你好，这是一份旧文档。".encode("gbk"))

    with pytest.warns(UserWarning, match="legacy.txt"):
        kb = LocalFolderKnowledgeBase("kb", tmp_path)

    sources = {chunk.source for chunk in kb._chunks}
    assert "good.txt" in sources
    assert "legacy.txt" not in sources
    # And nothing partially-decoded leaked into the index.
    assert not any("�" in chunk.text for chunk in kb._chunks)


def test_has_extractable_text_ignores_parser_headers():
    # Every parser prefixes its output with bracketed header lines we generate
    # ourselves, so "the parsed string is non-empty" is not the same question
    # as "the document had any text in it".
    assert not _has_extractable_text("[PDF: scan.pdf — 3 page(s)]\n")
    assert not _has_extractable_text("[Word: empty.docx]\n")
    assert not _has_extractable_text("[Word: tables.docx]\n\n[Table 1]\n")
    assert not _has_extractable_text("[Excel: book.xlsx]\n\n[Sheet: Sheet1]\n")
    assert not _has_extractable_text("[XML: empty.xml]")
    assert not _has_extractable_text("")

    assert _has_extractable_text("[PDF: report.pdf — 1 page(s)]\n\nQ3 revenue rose.")
    assert _has_extractable_text("[Sheet: Sheet1]\nName,Price\nWidget,10")
    assert _has_extractable_text("a document with no parser header at all")


def test_load_document_chunks_warns_on_unsupported_and_empty_documents(tmp_path):
    pypdf = pytest.importorskip("pypdf")

    (tmp_path / "good.txt").write_text("Apples are great fruit.", encoding="utf-8")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\r\n")
    # A blank page stands in for a scanned one: pypdf extracts no text, so the
    # parser returns nothing but its own "[PDF: ...]" header line.
    buffer = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    (tmp_path / "scan.pdf").write_bytes(buffer.getvalue())

    with pytest.warns(UserWarning) as recorded:
        chunks = _load_document_chunks(tmp_path, chunk_size=1000, chunk_overlap=100)

    messages = [str(warning.message) for warning in recorded]
    assert any("photo.png" in m and "Unsupported file type" in m for m in messages)
    assert any("scan.pdf" in m and "OCR" in m for m in messages)
    # Only the readable document is indexed -- in particular the scanned PDF
    # never becomes a chunk holding nothing but its own header line.
    assert {chunk.source for chunk in chunks} == {"good.txt"}


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


def test_init_raises_rank_bm25_missing_before_chunk_param_validation(tmp_path):
    """Regression test: __init__ checks for rank_bm25 BEFORE validating chunk params.

    When both rank_bm25 is missing AND chunk_size/overlap are invalid, the
    rank_bm25 error must be raised first (fail fast), not the chunk param error.
    """
    (tmp_path / "doc.txt").write_text("hello world", encoding="utf-8")
    with patch.dict("sys.modules", {"rank_bm25": None}):
        # Both rank_bm25 is missing AND chunk_size is invalid
        with pytest.raises(ConfigurationError, match="rank-bm25"):
            LocalFolderKnowledgeBase("kb", tmp_path, chunk_size=0, chunk_overlap=0)


# ---------------------------------------------------------------------------
# LocalFolderKnowledgeBase.from_chunks
# ---------------------------------------------------------------------------

def test_from_chunks_builds_queryable_kb():
    from bestteam.core.knowledge_base import _Chunk

    chunks = [
        _Chunk(source="a.txt", text="Refunds are allowed within 30 days of purchase."),
        _Chunk(source="b.txt", text="Our office hours are 9am to 5pm on weekdays."),
    ]
    kb = LocalFolderKnowledgeBase.from_chunks("policies", chunks, top_k=1)
    result = kb.query("refunds allowed")
    assert "30 days" in result
    assert "[source: a.txt]" in result


def test_from_chunks_empty_list_raises_configuration_error():
    with pytest.raises(ConfigurationError, match="no readable documents"):
        LocalFolderKnowledgeBase.from_chunks("empty_kb", [])


def test_from_chunks_and_init_produce_identical_query_results(tmp_path):
    (tmp_path / "doc.txt").write_text(
        "Refunds are allowed within 30 days of purchase.", encoding="utf-8"
    )
    from_path = LocalFolderKnowledgeBase("kb", tmp_path, top_k=1)

    from bestteam.core.knowledge_base import _Chunk
    chunks = [_Chunk(source="doc.txt", text="Refunds are allowed within 30 days of purchase.")]
    from_chunks = LocalFolderKnowledgeBase.from_chunks("kb", chunks, top_k=1)

    assert from_path.query("purchase") == from_chunks.query("purchase")


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


def test_knowledge_base_passes_file_suffix_to_the_chunker(tmp_path, monkeypatch):
    import bestteam.core.knowledge_base as kb_module

    (tmp_path / "guide.md").write_text("# Heading\ncontent", encoding="utf-8")
    (tmp_path / "catalog.xml").write_text("<a>text</a>", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("plain text", encoding="utf-8")

    seen_suffixes = {}
    real_chunk_document = kb_module._chunk_document

    def spy_chunk_document(source, text, chunk_size, chunk_overlap, suffix=""):
        seen_suffixes[suffix] = seen_suffixes.get(suffix, 0) + 1
        return real_chunk_document(source, text, chunk_size, chunk_overlap, suffix=suffix)

    monkeypatch.setattr(kb_module, "_chunk_document", spy_chunk_document)

    LocalFolderKnowledgeBase("docs", tmp_path)

    assert seen_suffixes.get(".md", 0) >= 1
    assert seen_suffixes.get(".xml", 0) >= 1
    assert seen_suffixes.get(".txt", 0) >= 1


def test_knowledge_base_markdown_chunking_respects_headings(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## Refunds\n"
        + "Refunds are issued within 7 days of the request. " * 3
        + "\n\n## Shipping\n"
        + "Orders ship within two business days of confirmation. " * 3,
        encoding="utf-8",
    )
    kb = LocalFolderKnowledgeBase("docs", tmp_path, chunk_size=80, chunk_overlap=0)
    result = kb.query("refund request")
    assert "guide.md" in result
    assert "Refunds" in result


def test_knowledge_base_xml_chunking_respects_element_boundaries(tmp_path):
    (tmp_path / "catalog.xml").write_text(
        "<catalog>"
        '<book id="bk101"><title>Widgets Explained</title></book>'
        '<book id="bk102"><title>Gadgets Explained</title></book>'
        '<book id="bk103"><title>Completely unrelated automotive repair manual</title></book>'
        "</catalog>",
        encoding="utf-8",
    )
    kb = LocalFolderKnowledgeBase("docs", tmp_path, chunk_size=80, chunk_overlap=0)
    result = kb.query("automotive repair manual")
    assert "catalog.xml" in result
    assert "automotive repair manual" in result


def test_knowledge_base_query_matches_inflected_english(tmp_path):
    """Stemming means a singular query reaches a document that only ever uses
    the plural."""
    (tmp_path / "refunds.txt").write_text(
        "Refunds are issued within 30 days.",
        encoding="utf-8",
    )
    (tmp_path / "shipping.txt").write_text(
        "Orders leave the warehouse within two business days.",
        encoding="utf-8",
    )
    kb = LocalFolderKnowledgeBase("docs", tmp_path)

    result = kb.query("refund")

    assert "refunds.txt" in result
    assert "shipping.txt" not in result


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
# _rrf_retrieve / _query_variants
# ---------------------------------------------------------------------------

from bestteam.core.knowledge_base import _query_variants, _rrf_retrieve


def test_rrf_retrieve_single_variant_single_leg_preserves_leg_order():
    def leg(query_text, fetch_k):
        return [3, 1, 2][:fetch_k]

    result = _rrf_retrieve(["q"], [leg], fetch_k=3)
    assert result == [3, 1, 2]


def test_rrf_retrieve_fuses_across_legs():
    def leg_a(query_text, fetch_k):
        return [1, 2][:fetch_k]

    def leg_b(query_text, fetch_k):
        return [2, 1][:fetch_k]

    result = _rrf_retrieve(["q"], [leg_a, leg_b], fetch_k=2)
    # Both indices appear at rank 1 once and rank 2 once -- tied, both present.
    assert set(result) == {1, 2}


def test_rrf_retrieve_fuses_across_variants():
    calls = []

    def leg(query_text, fetch_k):
        calls.append(query_text)
        return {"q": [1], "alt": [2]}.get(query_text, [])

    result = _rrf_retrieve(["q", "alt"], [leg], fetch_k=1)
    assert calls == ["q", "alt"]
    assert set(result) == {1, 2}


def test_rrf_retrieve_empty_legs_returns_empty():
    assert _rrf_retrieve(["q"], [lambda q, k: []], fetch_k=5) == []


def test_query_variants_no_expansion_model_returns_just_the_query():
    assert _query_variants("refund", None, 3) == ["refund"]


def test_query_variants_expansion_adds_alternatives():
    variants = _query_variants(
        "refund", 'fake:{"queries": ["money back"]}', 3
    )
    assert variants == ["refund", "money back"]


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


# ---------------------------------------------------------------------------
# LocalFolderKnowledgeBase query expansion
# ---------------------------------------------------------------------------

def test_local_folder_kb_query_expansion_unset_is_byte_identical(tmp_path):
    kb = _kb_with_docs(tmp_path, "apples and oranges", "cars and trucks", top_k=2)
    assert kb.query("apples") == kb.query("apples")


def test_local_folder_kb_query_expansion_recovers_chunk_literal_query_misses(tmp_path):
    # "sprocket" shares zero significant terms with either doc, so plain BM25
    # (query_expansion unset) returns nothing. The expansion variant "widget"
    # matches doc0 -- proving fusion recovers a chunk the literal query alone
    # could never surface.
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = _kb_with_docs(plain_dir, "widget assembly instructions", "gadget repair guide")
    plain_result = plain.query("sprocket")
    assert "No results found" in plain_result

    expanded_dir = tmp_path / "expanded"
    expanded_dir.mkdir()
    expanded = _kb_with_docs(
        expanded_dir,
        "widget assembly instructions",
        "gadget repair guide",
        query_expansion_model='fake:{"queries": ["widget"]}',
    )
    expanded_result = expanded.query("sprocket")
    assert "doc0.txt" in expanded_result


def test_local_folder_kb_query_expansion_disabled_when_count_zero(tmp_path):
    kb = _kb_with_docs(
        tmp_path,
        "widget assembly instructions",
        query_expansion_model='fake:{"queries": ["widget"]}',
        query_expansion_count=0,
    )
    assert "No results found" in kb.query("sprocket")


def test_local_folder_kb_bad_query_expansion_spec_degrades_gracefully(tmp_path):
    kb = _kb_with_docs(
        tmp_path, "apples and oranges", query_expansion_model="not-a-real-spec"
    )
    # Never raises; falls back to the literal query.
    result = kb.query("apples")
    assert "doc0.txt" in result


# ---------------------------------------------------------------------------
# Chunk metadata (page/heading), citations, and the single formatter (P0-3)
# ---------------------------------------------------------------------------

from bestteam.core.knowledge_base import _chunk_document, _citation, format_results


def test_chunk_two_field_construction_still_works():
    # `page`/`heading` default to None, so every pre-existing two-argument
    # construction (and every `from_chunks` caller) keeps working untouched.
    chunk = _Chunk(source="a.txt", text="body")
    assert chunk.page is None
    assert chunk.heading is None
    assert LocalFolderKnowledgeBase.from_chunks("kb", [chunk], top_k=1).query("body")


def test_pdf_pages_become_separate_chunks_with_page_numbers():
    text = (
        "[PDF: manual.pdf — 3 page(s)]\n"
        "Refunds are allowed within 30 days.\f"
        "Shipping is free above fifty pounds.\f"
        "Warranty claims need the receipt."
    )
    chunks = _chunk_document("manual.pdf", text, chunk_size=1000, chunk_overlap=0, suffix=".pdf")

    assert [chunk.page for chunk in chunks] == [1, 2, 3]
    assert "Refunds" in chunks[0].text
    assert "Warranty" in chunks[2].text
    # A chunk never straddles a page boundary, which is what makes `p.N` exact.
    assert all("\f" not in chunk.text for chunk in chunks)


def test_markdown_chunks_carry_section_heading():
    text = (
        "## Refunds\n"
        + "Refunds are issued within seven days of the request. " * 3
        + "\n\n## Shipping\n"
        + "Orders ship within two business days of confirmation. " * 3
    )
    chunks = _chunk_document("guide.md", text, chunk_size=120, chunk_overlap=0, suffix=".md")

    headings = [chunk.heading for chunk in chunks]
    assert "Refunds" in headings
    assert "Shipping" in headings
    # Heading is an approximation only Markdown and XML claim -- plain text does not.
    assert all(chunk.heading is None for chunk in _chunk_document(
        "notes.txt", "## Refunds\nplain text", chunk_size=1000, chunk_overlap=0, suffix=".txt"
    ))


# The parsed form of a small decision tree, exactly as `_parse_xml_bytes`
# renders it: one element per line, two-space indent per level, an element's
# own text after its tag.
_FLOW_XML = (
    "[XML: refund_flow.xml]\n"
    '<process name="Refund handling">\n'
    '  <step id="1"> Customer submits refund request\n'
    '  <decision id="2" question="Is the order within 30 days?">\n'
    '    <branch answer="Yes">\n'
    '      <decision id="3" question="Is the item unopened?">\n'
    '        <branch answer="Yes">\n'
    '          <step id="4"> Full refund to original payment method\n'
    '        <branch answer="No">\n'
    '          <step id="5"> Offer store credit only (15% restocking fee)\n'
    '    <branch answer="No">\n'
    '      <step id="6"> Reject: outside refund window; escalate to supervisor\n'
    '  <step id="7"> End'
)


def test_xml_document_that_fits_is_one_chunk_unchanged():
    chunks = _chunk_document("refund_flow.xml", _FLOW_XML, chunk_size=1000, chunk_overlap=100, suffix=".xml")

    assert [chunk.text for chunk in chunks] == [_FLOW_XML]
    assert chunks[0].heading is None


def test_xml_sub_chunks_repeat_their_ancestor_path():
    chunks = _chunk_document("refund_flow.xml", _FLOW_XML, chunk_size=400, chunk_overlap=0, suffix=".xml")
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 400 for chunk in chunks)

    # The "No" branch of decision 3 was split away from its decision; the
    # chunk that carries it must still say which question it answers, and
    # which outer branch it sits under -- the whole path, not just the parent.
    store_credit = next(chunk for chunk in chunks if "store credit" in chunk.text)
    lines = store_credit.text.splitlines()
    assert lines[0] == '<process name="Refund handling">'
    assert '  <decision id="2" question="Is the order within 30 days?">' in lines
    assert '    <branch answer="Yes">' in lines
    assert '      <decision id="3" question="Is the item unopened?">' in lines
    # Ancestors come before the content they introduce, in document order.
    assert lines.index('      <decision id="3" question="Is the item unopened?">') < lines.index(
        '        <branch answer="No">'
    )


def test_xml_sub_chunks_never_cut_an_element_line():
    chunks = _chunk_document("refund_flow.xml", _FLOW_XML, chunk_size=300, chunk_overlap=100, suffix=".xml")
    assert len(chunks) > 1
    original_lines = set(_FLOW_XML.splitlines())
    for chunk in chunks:
        for line in chunk.text.splitlines():
            assert line in original_lines, f"line cut or altered: {line!r}"


def test_xml_sub_chunk_heading_is_its_nearest_ancestor_element():
    chunks = _chunk_document("refund_flow.xml", _FLOW_XML, chunk_size=300, chunk_overlap=0, suffix=".xml")

    store_credit = next(chunk for chunk in chunks if "store credit" in chunk.text)
    assert store_credit.heading == 'decision id="3" question="Is the item unopened?"'
    assert all(chunk.heading is not None for chunk in chunks)
    assert all(len(chunk.heading) <= 80 for chunk in chunks)


def test_xml_splits_descend_below_the_top_level():
    # One root, one depth-1 element, many depth-2 leaves: the only useful
    # boundaries are two levels down. Every chunk must still be whole lines.
    leaves = "".join(f'    <item id="{i}"> Leaf number {i} has a label\n' for i in range(40))
    text = "[XML: deep.xml]\n<root>\n  <group name=\"all\">\n" + leaves.rstrip("\n")
    chunks = _chunk_document("deep.xml", text, chunk_size=200, chunk_overlap=0, suffix=".xml")

    assert len(chunks) > 3
    original_lines = set(text.splitlines())
    for chunk in chunks:
        assert len(chunk.text) <= 200
        lines = chunk.text.splitlines()
        assert all(line in original_lines for line in lines)
        assert lines[0] == "<root>" or lines[0].startswith("[XML:")
        assert '  <group name="all">' in lines
        assert chunk.heading == 'group name="all"'


def test_xml_deep_path_is_capped_so_content_keeps_at_least_half_the_chunk():
    # Six levels deep, the full path alone is ~270 characters: repeated whole,
    # it would leave a 300-character chunk a few dozen characters of content
    # and shred every leaf. Outermost ancestors are dropped first, the
    # nearest ones kept, and no element line is ever cut.
    text = (
        "[XML: refund_flow.xml]\n"
        '<process name="Refund handling">\n'
        '  <decision id="2" question="Is the order within 30 days?">\n'
        '    <branch answer="Yes">\n'
        '      <decision id="3" question="Is the item unopened?">\n'
        '        <branch answer="Yes">\n'
        '          <decision id="4" question="Amount greater than $500?">\n'
        '            <branch answer="Yes">\n'
        '              <step id="5"> Manager approval required before refund is issued\n'
        '              <step id="6"> Full refund to original payment method\n'
        '            <branch answer="No">\n'
        '              <step id="7"> Full refund to original payment method\n'
        '        <branch answer="No">\n'
        '          <step id="8"> Offer store credit only (15% restocking fee)\n'
        '    <branch answer="No">\n'
        '      <step id="9"> Reject: outside refund window\n'
        '  <step id="10"> End'
    )
    chunks = _chunk_document("refund_flow.xml", text, chunk_size=300, chunk_overlap=0, suffix=".xml")

    original_lines = set(text.splitlines())
    for chunk in chunks:
        assert len(chunk.text) <= 300
        for line in chunk.text.splitlines():
            assert line in original_lines, f"line cut or altered: {line!r}"
    approval = next(chunk for chunk in chunks if "Manager approval" in chunk.text)
    lines = approval.text.splitlines()
    # The nearest ancestors survive even though the root had to go.
    assert '          <decision id="4" question="Amount greater than $500?">' in lines
    assert '            <branch answer="Yes">' in lines
    assert approval.heading in (
        'decision id="4" question="Amount greater than $500?"',
        'branch answer="Yes"',
    )


def test_xml_ancestor_dropped_from_the_path_is_still_indexed_as_content():
    # A narrow, deep tree: the root's own text is on its opening line and
    # nowhere else. Its descendants' path is too long to keep the root, so
    # the root must be emitted as content in its own right, or a search for
    # "IMPORTANT ROOT TEXT" finds nothing (Codex review, P1).
    root = "<root> IMPORTANT ROOT TEXT about widgets"
    leaves = "".join(f'        <leaf id="{i}"> Leaf number {i} has a label\n' for i in range(12))
    text = "[XML: deep.xml]\n" + root + "\n  <a>\n    <b>\n      <c>\n" + leaves.rstrip("\n")
    chunks = _chunk_document("deep.xml", text, chunk_size=120, chunk_overlap=0, suffix=".xml")

    assert all(len(chunk.text) <= 120 for chunk in chunks)
    assert any(root in chunk.text.split("\n") for chunk in chunks)
    # And the cap still holds: no leaf chunk carries the root.
    leaf_chunks = [chunk for chunk in chunks if "Leaf number" in chunk.text]
    assert leaf_chunks and all(root not in chunk.text for chunk in leaf_chunks)


def test_xml_parent_tail_text_stays_with_the_parent_not_the_preceding_child():
    # Mixed content: `<root><label>…</label>root description<note/></root>`.
    # The renderer puts the tail at the child's depth as a text line; it is
    # the root's text, and must not be packed, prefixed or cited under
    # `<label>` just because it follows it (Codex review, P2).
    items = "".join(f'    <item id="{i}"> Item number {i} with some words\n' for i in range(10))
    text = (
        "[XML: mixed.xml]\n"
        "<root>\n"
        "  <label>\n"
        + items
        + "  root description after the label\n"
        "  <note> trailing note"
    )
    chunks = _chunk_document("mixed.xml", text, chunk_size=200, chunk_overlap=0, suffix=".xml")

    tail_chunk = next(chunk for chunk in chunks if "root description after the label" in chunk.text)
    assert tail_chunk.heading == "root"
    assert "  <label>" not in tail_chunk.text.split("\n")


def test_xml_ancestor_path_longer_than_chunk_size_degrades_to_plain_split():
    long_attr = "x" * 150
    text = (
        "[XML: wide.xml]\n"
        f'<root note="{long_attr}">\n'
        + "".join(f'  <item id="{i}"> Leaf {i}\n' for i in range(20)).rstrip("\n")
    )
    chunks = _chunk_document("wide.xml", text, chunk_size=120, chunk_overlap=0, suffix=".xml")

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 120 for chunk in chunks)
    assert any("Leaf 19" in chunk.text for chunk in chunks)


def test_knowledge_base_xml_tree_query_returns_branch_with_its_question(tmp_path):
    (tmp_path / "refund_flow.xml").write_text(
        '<process name="Refund handling">'
        '<decision id="2" question="Is the order within 30 days?">'
        '<branch answer="Yes">'
        '<decision id="3" question="Is the item unopened?">'
        '<branch answer="Yes"><step id="4">Full refund to original payment method</step></branch>'
        '<branch answer="No"><step id="5">Offer store credit only (15% restocking fee)</step></branch>'
        "</decision></branch>"
        '<branch answer="No"><step id="6">Reject: outside refund window; escalate to supervisor</step></branch>'
        "</decision></process>",
        encoding="utf-8",
    )
    kb = LocalFolderKnowledgeBase("flows", tmp_path, chunk_size=300, chunk_overlap=0, top_k=1)

    result = kb.query("store credit restocking fee")
    assert "store credit" in result
    assert "Is the item unopened?" in result
    assert '[source: refund_flow.xml § decision id="3" question="Is the item unopened?"]' in result


def test_format_results_unchanged_without_metadata():
    chunks = [_Chunk(source="a.txt", text="Refunds are allowed."), _Chunk(source="b.txt", text="Office hours.")]

    assert format_results("docs", "refunds", chunks) == (
        "Knowledge base 'docs' results for: refunds\n\n"
        "1. [source: a.txt]\n"
        "Refunds are allowed.\n\n"
        "2. [source: b.txt]\n"
        "Office hours.\n"
    )
    assert format_results("docs", "refunds", []) == (
        "No results found in knowledge base 'docs' for: refunds"
    )


def test_format_results_cites_page_and_heading():
    assert _citation(_Chunk(source="handbook.pdf", text="x", page=3)) == "handbook.pdf, p.3"
    assert _citation(_Chunk(source="guide.md", text="x", heading="Refunds")) == "guide.md § Refunds"
    assert _citation(
        _Chunk(source="handbook.pdf", text="x", page=3, heading="Refunds")
    ) == "handbook.pdf, p.3 § Refunds"

    rendered = format_results("docs", "refunds", [_Chunk(source="handbook.pdf", text="body", page=3)])
    assert "1. [source: handbook.pdf, p.3]" in rendered


@pytest.mark.parametrize("kb_type", ["local_folder", "vector", "hybrid"])
def test_search_returns_chunks_and_query_formats_them(tmp_path, kb_type):
    pytest.importorskip("numpy")
    from bestteam.core.hybrid_knowledge_base import HybridKnowledgeBase
    from bestteam.core.vector_knowledge_base import VectorKnowledgeBase

    (tmp_path / "policy.md").write_text(
        "## Refunds\nRefunds are allowed within 30 days of purchase.", encoding="utf-8"
    )
    if kb_type == "local_folder":
        kb = LocalFolderKnowledgeBase("docs", tmp_path, top_k=1)
    elif kb_type == "vector":
        kb = VectorKnowledgeBase("docs", tmp_path, embedding_model="fake:8", top_k=1)
    else:
        kb = HybridKnowledgeBase("docs", tmp_path, embedding_model="fake:8", top_k=1)

    chunks = kb.search("refunds")
    assert chunks and all(isinstance(chunk, _Chunk) for chunk in chunks)
    # One formatter, three retrieval methods: `query()` is the base class's.
    assert kb.query("refunds") == format_results("docs", "refunds", chunks)
    assert "§ Refunds" in kb.query("refunds")


def test_tool_docstring_states_description_and_citation_instruction(tmp_path):
    (tmp_path / "policy.txt").write_text("Refunds are allowed.", encoding="utf-8")
    kb = LocalFolderKnowledgeBase("docs", tmp_path, description="Our refund and shipping policies")

    doc = make_knowledge_base_tool(kb).__doc__
    assert "Search the 'docs' knowledge base: Our refund and shipping policies." in doc
    assert "Use it whenever the question may be answered by these documents." in doc
    assert '"[source: handbook.pdf, p.3]"' in doc
    assert "cite it with that same [source: …] tag" in doc


def test_tool_docstring_without_description_is_generic(tmp_path):
    (tmp_path / "policy.txt").write_text("Refunds are allowed.", encoding="utf-8")
    kb = LocalFolderKnowledgeBase("docs", tmp_path)

    doc = make_knowledge_base_tool(kb).__doc__
    assert "Search the 'docs' knowledge base. Use it whenever" in doc
    assert kb.description is None


# ---------------------------------------------------------------------------
# estimate_embedding_tokens
# ---------------------------------------------------------------------------

def test_estimate_embedding_tokens_counts_cjk_per_char_and_latin_per_4():
    from bestteam.core.embeddings import estimate_embedding_tokens

    assert estimate_embedding_tokens("") == 0
    # Latin: one token per four characters, rounded up.
    assert estimate_embedding_tokens("abcd") == 1
    assert estimate_embedding_tokens("abcde") == 2
    # CJK: one token per character, because there are no word boundaries to
    # merge across.
    assert estimate_embedding_tokens("退货政策") == 4
    # Kana and Hangul are inside `_CJK_RUN_RE`'s ranges too (P1-1), so they are
    # counted per character rather than per four -- which is roughly where
    # Japanese and Korean land.
    assert estimate_embedding_tokens("ひらがな") == 4
    assert estimate_embedding_tokens("한글") == 2
    # Mixed: 4 CJK characters + " refunds" (8 characters -> 2).
    assert estimate_embedding_tokens("退货政策 refunds") == 6


# ---------------------------------------------------------------------------
# Tabular chunking: .xlsx/.xlsm sheets and .docx tables (P1-5)
# ---------------------------------------------------------------------------

def _sheet_rows(count: int) -> str:
    """CSV-style body rows shaped the way `_parse_excel_bytes` renders them."""
    return "\n".join(f"north,widget-{i:03d},{i}" for i in range(count))


def test_xlsx_long_sheet_repeats_sheet_marker_and_header_row_in_every_chunk():
    text = "[Excel: sales.xlsx]\n\n[Sheet: Q1]\nregion,product,units\n" + _sheet_rows(40)

    chunks = _chunk_document("sales.xlsx", text, chunk_size=200, chunk_overlap=0, suffix=".xlsx")

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("[Sheet: Q1]\nregion,product,units\n")
        assert chunk.heading == "Sheet: Q1"
    # No body row is lost or duplicated across the chunks: with
    # `chunk_overlap=0`, the chunk bodies -- each chunk stripped of its
    # repeated marker and header prefix -- rejoin to exactly the input rows.
    body_rows = [
        row for chunk in chunks for row in chunk.text.split("\n", 2)[2].split("\n") if row
    ]
    assert body_rows == _sheet_rows(40).split("\n")


def test_xlsx_blank_leading_row_is_skipped_when_choosing_the_header_row():
    """Many workbooks put a spacer or title row above their headers, which
    `read_only` openpyxl renders as `,,`. Repeating *that* in every chunk
    would make the repeated header say nothing at all."""
    text = "[Excel: sales.xlsx]\n\n[Sheet: Data]\n,,\nregion,product,units\n" + _sheet_rows(40)

    chunks = _chunk_document("sales.xlsx", text, chunk_size=200, chunk_overlap=0, suffix=".xlsx")

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("[Sheet: Data]\nregion,product,units\n")
        assert chunk.heading == "Sheet: Data"


def test_xlsx_comma_only_sheet_yields_no_chunk():
    """A formatted-but-empty sheet parses to rows of bare commas, which
    survive `.strip()` -- but there is nothing in them to index."""
    text = "[Excel: sales.xlsx]\n\n[Sheet: Data]\n,,\n,,"

    chunks = _chunk_document("sales.xlsx", text, chunk_size=200, chunk_overlap=0, suffix=".xlsx")

    assert chunks == []


def test_xlsx_small_sheet_is_a_single_chunk_without_duplication():
    text = "[Excel: sales.xlsx]\n\n[Sheet: Q1]\nregion,product,units\n" + _sheet_rows(3)

    chunks = _chunk_document("sales.xlsx", text, chunk_size=1000, chunk_overlap=0, suffix=".xlsx")

    assert len(chunks) == 1
    assert chunks[0].heading == "Sheet: Q1"
    assert chunks[0].text.count("[Sheet: Q1]") == 1
    assert chunks[0].text.count("region,product,units") == 1


def test_xlsx_multiple_sheets_each_carry_their_own_heading():
    text = (
        "[Excel: sales.xlsx]\n\n"
        "[Sheet: Q1]\nregion,product,units\n" + _sheet_rows(30) + "\n\n"
        "[Sheet: Q2]\nregion,product,units\n" + _sheet_rows(30)
    )

    chunks = _chunk_document("sales.xlsx", text, chunk_size=200, chunk_overlap=0, suffix=".xlsx")

    headings = {chunk.heading for chunk in chunks}
    assert headings == {"Sheet: Q1", "Sheet: Q2"}
    # A sheet's chunks carry that sheet's marker and nobody else's.
    for chunk in chunks:
        marker = f"[{chunk.heading}]"
        assert chunk.text.startswith(marker)
        assert chunk.text.count("[Sheet: ") == 1


def test_docx_body_chunks_normally_and_table_chunks_repeat_the_header():
    text = (
        "[Word: report.docx]\n"
        + "Quarterly trading was steady across every region. " * 6
        + "\n\n[Table 1]\nname,role\n"
        + "\n".join(f"person-{i:03d},analyst" for i in range(40))
    )

    chunks = _chunk_document("report.docx", text, chunk_size=200, chunk_overlap=0, suffix=".docx")

    body_chunks = [chunk for chunk in chunks if chunk.heading is None]
    table_chunks = [chunk for chunk in chunks if chunk.heading == "Table 1"]
    assert body_chunks and len(table_chunks) > 1
    assert any("Quarterly trading" in chunk.text for chunk in body_chunks)
    assert all("[Table 1]" not in chunk.text for chunk in body_chunks)
    for chunk in table_chunks:
        assert chunk.text.startswith("[Table 1]\nname,role\n")


def test_header_row_longer_than_chunk_size_does_not_crash_and_still_sets_heading():
    header_row = ",".join(f"column-{i:03d}" for i in range(12))
    assert len(header_row) > 60
    text = "[Excel: wide.xlsx]\n\n[Sheet: Q1]\n" + header_row + "\n" + _sheet_rows(20)

    chunks = _chunk_document("wide.xlsx", text, chunk_size=60, chunk_overlap=0, suffix=".xlsx")

    assert len(chunks) > 1
    assert all(chunk.heading == "Sheet: Q1" for chunk in chunks)
    assert all(len(chunk.text) <= 60 for chunk in chunks)


def test_table_heading_is_capped_at_80_chars():
    from bestteam.core.knowledge_base import _MAX_HEADING_CHARS

    sheet_name = "Q" * 120
    text = f"[Excel: wide.xlsx]\n\n[Sheet: {sheet_name}]\nregion,units\n" + _sheet_rows(30)

    chunks = _chunk_document("wide.xlsx", text, chunk_size=200, chunk_overlap=0, suffix=".xlsx")

    assert chunks
    for chunk in chunks:
        assert len(chunk.heading) == _MAX_HEADING_CHARS
        assert chunk.heading == f"Sheet: {sheet_name}"[:_MAX_HEADING_CHARS]


@pytest.mark.parametrize("chunk_size", [60, 120, 200, 500])
@pytest.mark.parametrize("chunk_overlap", [0, 30])
def test_table_chunks_never_exceed_chunk_size(chunk_size, chunk_overlap):
    text = (
        "[Excel: sales.xlsx]\n\n[Sheet: Q1]\nregion,product,units\n"
        + _sheet_rows(60)
        + "\n\n[Sheet: Q2]\nregion,product,units\n"
        + _sheet_rows(60)
    )

    chunks = _chunk_document(
        "sales.xlsx", text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, suffix=".xlsx"
    )

    assert chunks
    assert all(len(chunk.text) <= chunk_size for chunk in chunks)


def test_an_empty_sheet_or_table_block_produces_no_chunk():
    """Empty trailing sheets are ubiquitous in real workbooks. A block that is
    its marker line and nothing else is exactly the content-free chunk P0-6 set
    out to stop indexing -- it matches no query and reports no problem."""
    text = (
        "[Excel: sales.xlsx]\n\n"
        "[Sheet: Q1]\nregion,units\n" + _sheet_rows(3) + "\n\n"
        "[Sheet: Sheet2]\n\n"
        "[Sheet: Sheet3]"
    )

    chunks = _chunk_document("sales.xlsx", text, chunk_size=1000, chunk_overlap=0, suffix=".xlsx")

    assert [chunk.heading for chunk in chunks] == ["Sheet: Q1"]

    # The same holds for a Word table that parsed to its marker alone: the
    # body paragraph is still chunked, the empty table contributes nothing.
    docx_chunks = _chunk_document(
        "report.docx",
        "[Word: report.docx]\nBody text.\n\n[Table 1]",
        chunk_size=1000,
        chunk_overlap=0,
        suffix=".docx",
    )
    assert [(chunk.text, chunk.heading) for chunk in docx_chunks] == [
        ("[Word: report.docx]\nBody text.", None)
    ]


def test_format_results_cites_sheet_heading():
    chunk = _Chunk(source="sales.xlsx", text="[Sheet: Q1]\nregion,units\nnorth,12", heading="Sheet: Q1")

    assert _citation(chunk) == "sales.xlsx § Sheet: Q1"
    assert "1. [source: sales.xlsx § Sheet: Q1]" in format_results("docs", "units", [chunk])
