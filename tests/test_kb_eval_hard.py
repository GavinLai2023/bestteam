"""The hard golden set (`tests/fixtures/kb_eval_hard/`) and its $0 baselines.

The bundled set in `tests/fixtures/kb_eval/` is the easy floor: short
single-topic documents where BM25 already ranks every lexical query first.
This set holds the failure modes real customer corpora have -- answers inside
CSV tables, facts buried late in long documents, near-identical sibling
documents, and Chinese questions over English-only documents. BM25's scores
here are documented as a baseline; the real-model floors live in
`tests/test_kb_eval_live.py`.
"""
from pathlib import Path

import pytest

pytest.importorskip("rank_bm25")

from bestteam.core.kb_eval import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    evaluate,
    load_queries,
    report_for,
)
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase  # noqa: E402

pytestmark = pytest.mark.unit

HARD_SET = Path(__file__).parent / "fixtures" / "kb_eval_hard"
HARD_DOCS = HARD_SET / "docs"
HARD_QUERIES = HARD_SET / "queries.yaml"

def _report(queries=None):
    kb = LocalFolderKnowledgeBase(
        "hard",
        HARD_DOCS,
        chunk_size=DEFAULT_CHUNK_SIZE,
        chunk_overlap=DEFAULT_CHUNK_OVERLAP,
        top_k=DEFAULT_TOP_K,
    )
    return evaluate(kb, queries or load_queries(HARD_QUERIES), DEFAULT_TOP_K)


def _kind(report, kind):
    return report_for(
        [o for o in report.outcomes if o.query.kind == kind], report.top_k
    )


def test_hard_set_is_well_formed():
    queries = load_queries(HARD_QUERIES)
    docs = {p.name: p.read_text(encoding="utf-8") for p in HARD_DOCS.iterdir()}

    assert len(docs) == 11
    assert len(queries) == 17
    kinds = [q.kind for q in queries]
    assert kinds.count("table") == 4
    assert kinds.count("long") == 4
    assert kinds.count("distractor") == 5
    assert kinds.count("crosslingual") == 4

    for query in queries:
        assert query.query.strip()
        assert query.expected_source in docs, query.expected_source
        if query.expected_substring is not None:
            assert query.expected_substring in docs[query.expected_source], query.query

    # Every document is the answer to at least one query -- the distractor
    # siblings included, each queried via its one distinguishing detail.
    assert {q.expected_source for q in queries} == set(docs)

    # The `long` documents must actually be long -- an edit that trims one
    # below this stops exercising chunk competition and should be noticed.
    # The Chinese bound is lower because CJK text carries the same content in
    # far fewer characters (and chunks are measured in characters).
    for name, floor in (("employee_handbook.md", 2800), ("员工手册.md", 1400)):
        assert len(docs[name]) >= floor, f"{name} is only {len(docs[name])} characters"

    # A crosslingual query must share no token with its (English) document:
    # no Latin letters or digits in the query, so BM25 scores 0 by
    # construction and the kind stays what it claims to be.
    for query in queries:
        if query.kind == "crosslingual":
            assert not any(c.isascii() and c.isalnum() for c in query.query), query.query


def test_bm25_baseline_on_hard_set():
    """What keyword search alone delivers on the hard cases, pinned so a
    chunking or tokenizer regression shows up at $0. Crosslingual is 0 by
    construction (no shared tokens); the other kinds are where table-aware
    chunking and CJK bigrams earn their keep."""
    report = _report()

    assert _kind(report, "crosslingual").recall_at_k == 0.0

    table = _kind(report, "table")
    long_ = _kind(report, "long")
    distractor = _kind(report, "distractor")
    assert table.recall_at_k == 1.0, [o.sources for o in table.outcomes]
    assert long_.recall_at_k == 1.0, [o.sources for o in long_.outcomes]
    assert distractor.recall_at_k == 1.0, [o.sources for o in distractor.outcomes]

    # Retrieving the right sibling somewhere in the top 3 is not enough for
    # the distractor kind -- the query names the one distinguishing detail,
    # so the right sibling must be first.
    assert distractor.hit_at_1 == 1.0, [
        (o.query.query, o.sources) for o in distractor.outcomes
    ]

    # The buried facts must arrive in the retrieved chunks themselves, not
    # just somewhere in the right document.
    assert report.substring_hit_at_k is not None
    graded = [o for o in report.outcomes if o.substring_hit is False and o.query.kind != "crosslingual"]
    assert graded == [], [(o.query.query, o.sources) for o in graded]
