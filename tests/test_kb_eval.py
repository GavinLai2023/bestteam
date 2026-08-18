"""Tests for the retrieval-quality evaluation harness (`core/kb_eval.py`)."""
from pathlib import Path

import pytest

pytest.importorskip("rank_bm25")

from bestteam.core.kb_eval import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    evaluate,
    load_queries,
    mrr,
    rank_of,
    recall_at_k,
)
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase  # noqa: E402

pytestmark = pytest.mark.unit

GOLDEN_SET = Path(__file__).parent / "fixtures" / "kb_eval"
GOLDEN_DOCS = GOLDEN_SET / "docs"
GOLDEN_QUERIES = GOLDEN_SET / "queries.yaml"


def _golden_kb(cls=LocalFolderKnowledgeBase, **kwargs):
    return cls(
        "golden",
        GOLDEN_DOCS,
        chunk_size=DEFAULT_CHUNK_SIZE,
        chunk_overlap=DEFAULT_CHUNK_OVERLAP,
        top_k=DEFAULT_TOP_K,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Metric math
# ---------------------------------------------------------------------------

def test_metric_math_on_synthetic_results():
    """The three metrics are pure functions of where the expected source
    landed, so they can be checked without retrieving anything."""
    assert rank_of(["a.md", "b.md", "c.md"], "b.md") == 2
    assert rank_of(["a.md", "b.md"], "z.md") is None
    # First occurrence wins when a source contributed several chunks.
    assert rank_of(["a.md", "b.md", "a.md"], "a.md") == 1

    ranks = [1, 2, 3, None]
    assert recall_at_k(ranks, 3) == pytest.approx(0.75)
    assert recall_at_k(ranks, 1) == pytest.approx(0.25)
    assert mrr(ranks) == pytest.approx((1 + 0.5 + 1 / 3) / 4)

    # A perfect run and a total miss are the two ends of the scale.
    assert recall_at_k([1, 1, 1], 3) == pytest.approx(1.0)
    assert mrr([1, 1, 1]) == pytest.approx(1.0)
    assert recall_at_k([None, None], 3) == pytest.approx(0.0)
    assert mrr([None, None]) == pytest.approx(0.0)

    # No queries at all is 0.0, not a ZeroDivisionError.
    assert recall_at_k([], 3) == pytest.approx(0.0)
    assert mrr([]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The golden set itself
# ---------------------------------------------------------------------------

def test_golden_set_is_well_formed():
    queries = load_queries(GOLDEN_QUERIES)
    docs = {p.name: p.read_text(encoding="utf-8") for p in GOLDEN_DOCS.iterdir()}

    assert len(docs) == 10
    for name, text in docs.items():
        assert 300 <= len(text) <= 600, f"{name} is {len(text)} characters"

    assert len(queries) == 20
    kinds = [q.kind for q in queries]
    assert kinds.count("lexical") == 16
    assert kinds.count("paraphrase") == 4

    for query in queries:
        assert query.query.strip()
        assert query.expected_source in docs, query.expected_source
        if query.expected_substring is not None:
            assert query.expected_substring in docs[query.expected_source], query.query

    # Every document is the answer to at least one query.
    assert {q.expected_source for q in queries} == set(docs)


# ---------------------------------------------------------------------------
# Retrieval baselines
# ---------------------------------------------------------------------------

def test_local_folder_baseline_on_golden_set():
    """BM25 keyword search must find the right document for the lexical
    queries. The paraphrase queries share no significant terms with their
    target, so BM25 misses them by design -- hence the headroom below 1.0."""
    queries = load_queries(GOLDEN_QUERIES)
    report = evaluate(_golden_kb(), queries, top_k=DEFAULT_TOP_K)

    assert report.recall_at_k >= 0.8, report.recall_at_k
    assert report.mrr >= 0.7, report.mrr
    assert len(report.outcomes) == len(queries)
    # Every lexical query is expected to put its document first.
    lexical_misses = [o.query.query for o in report.outcomes
                      if o.query.kind == "lexical" and o.rank != 1]
    assert lexical_misses == []


def test_hybrid_with_fake_embeddings_runs_and_reports_all_metrics():
    """Smoke only: `fake:` embeddings are deterministic noise, so no quality
    claim is made -- this checks the harness drives a hybrid knowledge base
    end to end and fills in every metric."""
    pytest.importorskip("numpy")
    from bestteam.core.hybrid_knowledge_base import HybridKnowledgeBase

    queries = load_queries(GOLDEN_QUERIES)
    kb = _golden_kb(HybridKnowledgeBase, embedding_model="fake:32")
    report = evaluate(kb, queries, top_k=DEFAULT_TOP_K)

    assert report.top_k == DEFAULT_TOP_K
    assert len(report.outcomes) == len(queries)
    for value in (report.recall_at_k, report.mrr, report.hit_at_1,
                  report.substring_hit_at_k):
        assert 0.0 <= value <= 1.0
    for outcome in report.outcomes:
        assert outcome.sources, outcome.query.query
        assert outcome.substring_hit in (True, False)
