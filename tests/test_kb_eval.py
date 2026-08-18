"""Tests for the retrieval-quality evaluation harness (`core/kb_eval.py`)."""
from pathlib import Path

import pytest

pytest.importorskip("rank_bm25")

from bestteam.core.kb_eval import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    EvalQuery,
    evaluate,
    load_queries,
    mrr,
    rank_of,
    recall_at_k,
)
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase, _Chunk  # noqa: E402
from bestteam.exceptions import ConfigurationError  # noqa: E402

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


class _StubKB:
    """Returns a fixed chunk list, so a metric can be checked against an
    exact retrieval result without indexing anything."""

    def __init__(self, *chunks):
        self._chunks = list(chunks)

    def search(self, query, top_k=None):
        return self._chunks[:top_k] if top_k else self._chunks


def test_substring_hit_only_counts_inside_the_expected_document():
    """The substring check exists to catch a chunking change that separates
    an answer from the words that found it, so it is scoped to the expected
    document. Another document that happens to quote the same fact is a
    retrieval miss, not partial credit."""
    query = EvalQuery(
        query="refund window",
        expected_source="refund_policy.md",
        expected_substring="30 days",
    )

    elsewhere = _StubKB(
        _Chunk(source="warranty.md", text="A repair takes 30 days."),
        _Chunk(source="refund_policy.md", text="See the returns page."),
    )
    outcome = evaluate(elsewhere, [query], top_k=3).outcomes[0]
    assert outcome.rank == 2, "the expected document WAS retrieved"
    assert outcome.substring_hit is False

    # The same substring in the expected document's own chunk does count.
    here = _StubKB(
        _Chunk(source="warranty.md", text="A repair takes 30 days."),
        _Chunk(source="refund_policy.md", text="Returns are refunded in 30 days."),
    )
    assert evaluate(here, [query], top_k=3).outcomes[0].substring_hit is True


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


@pytest.mark.parametrize("top_k", [0, -1])
def test_evaluate_rejects_non_positive_top_k(top_k):
    """`top_k=0` would retrieve the KB's default cutoff (`top_k or default`)
    yet be scored as recall@0 -- a report that is internally inconsistent.
    Refuse it before any retrieval, for the CLI's `--top-k 0` too."""
    kb = LocalFolderKnowledgeBase.from_chunks("kb", [_Chunk("a.md", "alpha beta")])
    with pytest.raises(ConfigurationError, match="top_k"):
        evaluate(kb, [EvalQuery(query="alpha", expected_source="a.md", kind="lexical")], top_k=top_k)
