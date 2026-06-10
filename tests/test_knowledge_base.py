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
