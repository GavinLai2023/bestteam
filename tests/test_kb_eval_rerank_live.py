"""Release gate: reranking under a REAL cross-encoder must not hurt retrieval.

Reranking has only ever been exercised with the `fake:` reranker, which
proves the plumbing and nothing about quality. This file scores the hybrid
type with a real `sentence_transformers.CrossEncoder` on both golden sets
(`tests/fixtures/kb_eval/` and `kb_eval_hard/`) and holds two lines:

  1. the reranked ranking still meets the same recall@3 release floors as
     the unreranked one (`tests/test_kb_eval_live.py`), and
  2. reranking never costs more than one query of overall recall against
     the unreranked baseline computed in the same run.

The model must be multilingual because the golden sets are EN/ZH -- an
English-only cross-encoder (e.g. ms-marco-MiniLM) would misrank every
Chinese query. Multilingual is necessary but not sufficient: the first
candidate measured here, `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`,
FAILED this gate (2026-08-26) -- on English paraphrase queries it scored
the parallel *Chinese* document above the English original and pushed the
right document out of the top 3 entirely (recall@3 1.00 -> 0.90). That is
exactly the mistake this gate exists to catch in a deployment's
`BESTTEAM_KB_DEFAULT_RERANK_MODEL`; `BAAI/bge-reranker-base` passed
everything and is the recommendation.

A silently broken reranker CANNOT pass this file: rerank failures are
fail-soft (retrieval order is kept, `rerank_score` stays None), so
`test_reranker_actually_ran` asserts scores are present -- on every query
of both sets, since a failure on one long or Chinese pair leaves only that
query unreranked and the floors have the slack to pass anyway.

Run it by hand before a release (CI never needs it -- the module skips
without the opt-in, an API key, or the `tools-rerank` extra):

    $env:BESTTEAM_LIVE_EVAL = "1"
    .venv/Scripts/python -m pytest tests/test_kb_eval_rerank_live.py -m optional

Both this file and `test_kb_eval_live.py` are release gates you run by hand,
so they need an explicit opt-in as well as an API key -- `BESTTEAM_LIVE_EVAL`.
Skipping on a missing key alone was not enough: a developer who exports
`OPENAI_API_KEY` for the dev backend enrols every local `-m "not e2e"` sweep
into a paid, model-downloading gate, and this file alone was 5m46s of a 10m53s
run (53%) that way. CI has neither the key nor the extra, so nothing there
changes.

First run downloads the cross-encoder (~1.1 GB, cached by Hugging Face
after that); inference is local and $0, the only API spend is the query
embeddings (well under $0.01, chunk embeddings come from the shared
`.bestteam_cache/kb_eval_live.json`).

Measured 2026-08-26 (text-embedding-3-small + BAAI/bge-reranker-base),
floors inherited from `test_kb_eval_live.py`:

    golden  recall@3 1.00, MRR 0.950, hit@1 0.90   (unreranked MRR 0.925)
    hard    recall@3 1.00, MRR 0.941, hit@1 0.88   -- and crosslingual
            hit@1 went 0.00 -> 1.00: the reranker undoes the BM25-leg RRF
            tie preference that buries the vector leg's crosslingual wins.
"""
import os
from pathlib import Path

import pytest

# Checked before the importorskips below, so a default run does not even pay
# `sentence_transformers` (which drags in transformers and torch) to find out
# it is going to skip.
if not os.environ.get("BESTTEAM_LIVE_EVAL"):
    pytest.skip(
        "BESTTEAM_LIVE_EVAL is not set -- this is a by-hand release gate that "
        "spends real provider quota and downloads a ~1.1 GB cross-encoder. "
        "Set BESTTEAM_LIVE_EVAL=1 to run it.",
        allow_module_level=True,
    )

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")
pytest.importorskip("langchain_openai")
pytest.importorskip("sentence_transformers")

if not os.environ.get("OPENAI_API_KEY"):
    pytest.skip(
        "OPENAI_API_KEY is not set -- the rerank gate scores a hybrid base, "
        "so it needs a real embedding provider (cost: under $0.01)",
        allow_module_level=True,
    )

from bestteam.core.hybrid_knowledge_base import HybridKnowledgeBase  # noqa: E402
from bestteam.core.kb_eval import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    EvalReport,
    evaluate,
    load_queries,
    report_for,
)

pytestmark = [pytest.mark.optional, pytest.mark.slow]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_SET = Path(__file__).parent / "fixtures" / "kb_eval"
_HARD_SET = Path(__file__).parent / "fixtures" / "kb_eval_hard"
_EMBEDDING_MODEL = "openai:text-embedding-3-small"
#: Multilingual on purpose, and gate-validated -- see the module docstring
#: (mmarco-mMiniLMv2 failed this gate). If a deployment picks a different
#: default cross-encoder, point this at it and re-run.
_RERANK_MODEL = "cross-encoder:BAAI/bge-reranker-base"
_CACHE_PATH = _REPO_ROOT / ".bestteam_cache" / "kb_eval_live.json"

# The same calibrated floors as tests/test_kb_eval_live.py -- reranking has
# to clear the bar retrieval already clears without it.
_FLOORS = {"lexical": 0.9375, "paraphrase": 0.75}
_HARD_FLOORS = {"table": 0.75, "long": 0.75, "distractor": 0.80, "crosslingual": 0.75}
_OVERALL_FLOOR = 0.90


def _reports(golden_set: Path):
    """(unreranked report, reranked report, the reranked base, its queries).

    Both reports come from the same documents and queries, so the not-worse
    comparison is same-run, same-embeddings; the base and queries come back
    so `test_reranker_actually_ran` can re-ask each one for its hits (a
    report keeps sources and ranks, not `rerank_score`)."""
    def build(**extra):
        return HybridKnowledgeBase(
            "rerank_live",
            golden_set / "docs",
            embedding_model=_EMBEDDING_MODEL,
            chunk_size=DEFAULT_CHUNK_SIZE,
            chunk_overlap=DEFAULT_CHUNK_OVERLAP,
            top_k=DEFAULT_TOP_K,
            cache_path=_CACHE_PATH,
            **extra,
        )
    queries = load_queries(golden_set / "queries.yaml")
    reranked = build(rerank_model=_RERANK_MODEL)
    return (
        evaluate(build(), queries, DEFAULT_TOP_K),
        evaluate(reranked, queries, DEFAULT_TOP_K),
        reranked,
        queries,
    )


@pytest.fixture(scope="module")
def golden_reports():
    return _reports(_GOLDEN_SET)


@pytest.fixture(scope="module")
def hard_reports():
    return _reports(_HARD_SET)


def _kind(report: EvalReport, kind: str) -> EvalReport:
    return report_for(
        [o for o in report.outcomes if o.query.kind == kind], report.top_k
    )


def _misses(report: EvalReport) -> str:
    lines = [
        f"  [{o.query.kind}] {o.query.query!r}: want {o.query.expected_source}, "
        f"got {', '.join(o.sources) or '(nothing)'}"
        for o in report.outcomes
        if o.rank is None or o.rank > report.top_k
    ]
    return "\n".join(lines) or "  (all queries retrieved their document)"


@pytest.mark.parametrize("which", ["golden_reports", "hard_reports"])
def test_reranker_actually_ran(which, request):
    """Guards the guard: a rerank-time failure keeps retrieval order and
    leaves `rerank_score` None (fail-soft by design), which would let every
    floor test below pass without the cross-encoder ever scoring a pair.

    Checked on **every gated query of both sets**, not one smoke query: a
    scoring failure the cross-encoder only hits on a particular pair (a long
    document, a Chinese query) leaves exactly that query unreranked, and the
    floors have enough slack to pass anyway — so a single English lookup
    proves nothing about the queries the gate is actually scoring.
    """
    _, _, kb, queries = request.getfixturevalue(which)
    unscored = []
    for query in queries:
        hits = kb.search_hits(query.query, DEFAULT_TOP_K)
        if not hits or any(hit.rerank_score is None for hit in hits):
            unscored.append(query.query)
    assert not unscored, (
        "rerank_score is None (or nothing was retrieved) for these queries -- "
        "the cross-encoder failed on them and retrieval order was kept, so "
        "the floors below are not measuring reranking for them:\n  "
        + "\n  ".join(repr(q) for q in unscored)
    )


def _assert_floors(report: EvalReport, kind_floors, label: str) -> None:
    failures = []
    if report.recall_at_k < _OVERALL_FLOOR:
        failures.append(f"overall {report.recall_at_k:.2f} (floor {_OVERALL_FLOOR})")
    for kind, floor in kind_floors.items():
        recall = _kind(report, kind).recall_at_k
        if recall < floor:
            failures.append(f"{kind} {recall:.2f} (floor {floor})")
    assert not failures, (
        f"{label}: reranked recall@{report.top_k} below the release floor: "
        f"{'; '.join(failures)}.\nMissed:\n{_misses(report)}"
    )


def test_reranked_meets_release_floors(golden_reports):
    _assert_floors(golden_reports[1], _FLOORS, "golden set")


def test_reranked_meets_hard_set_floors(hard_reports):
    """The distractor bucket is where a cross-encoder earns its keep (it
    reads the query against the actual chunk text, so near-identical
    siblings separate); crosslingual is where a monolingual one would sink."""
    _assert_floors(hard_reports[1], _HARD_FLOORS, "hard set")


@pytest.mark.parametrize("which", ["golden_reports", "hard_reports"])
def test_reranking_is_not_worse_than_retrieval(which, request):
    """The opt-in's contract: switching reranking on must not lose more
    than one query of overall recall against the same run's unreranked
    ranking. (One query of slack, not zero, for the same reason the floors
    have slack -- a single provider-side embedding wobble should not read
    as a rerank regression.)"""
    unreranked, reranked, _, _ = request.getfixturevalue(which)
    slack = 1 / len(unreranked.outcomes)
    assert reranked.recall_at_k >= unreranked.recall_at_k - slack, (
        f"reranking LOWERED recall@{reranked.top_k}: "
        f"{unreranked.recall_at_k:.2f} -> {reranked.recall_at_k:.2f}.\n"
        f"Missed after reranking:\n{_misses(reranked)}"
    )
