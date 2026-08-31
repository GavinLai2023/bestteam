"""Release gate: retrieval quality under a REAL embedding model.

`tests/test_kb_eval.py` proves the harness and pins the BM25 baseline at $0;
nothing in CI ever measures what the `vector`/`hybrid` types actually deliver
under the production default embedding model. This file is that measurement:
the bundled golden set (10 documents EN/ZH, 16 lexical + 4 paraphrase
queries) run against `openai:text-embedding-3-small` -- the model
`BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL` documents as the deployment default.

The paraphrase queries are the whole point. They share no significant term
with their documents, so BM25 scores exactly 0 on them by construction --
these thresholds are the proof that a real embedding model closes that gap,
which the fake:-embedding smoke tests structurally cannot show.

Run it by hand before a release (CI never needs an API key -- the module
skips without the opt-in or the key):

    $env:BESTTEAM_LIVE_EVAL = "1"
    .venv/Scripts/python -m pytest tests/test_kb_eval_live.py -m optional

The opt-in is deliberate belt-and-braces. Skipping on a missing API key alone
was not enough: a developer who exports `OPENAI_API_KEY` for the dev backend
was silently enrolling every local `-m "not e2e"` sweep into the paid gates --
this one plus `test_kb_eval_rerank_live.py`, which was 5m46s of a 10m53s run
on its own. CI has neither the key nor the opt-in, so nothing there changes.

Cost: well under $0.01. Chunk embeddings persist in the gitignored
`.bestteam_cache/kb_eval_live.json`, so re-runs pay only the 20 query
embeddings per knowledge-base type.

Measured 2026-08-26 (text-embedding-3-small), thresholds set below with
margin for provider drift:

    vector  recall@3 1.00 overall / 1.00 lexical / 1.00 paraphrase, MRR 0.975
    hybrid  recall@3 1.00 overall / 1.00 lexical / 1.00 paraphrase, MRR 0.925

Hybrid's paraphrase hit@1 was only 0.25 -- the BM25 leg outranks the vector
leg on an RRF tie (a documented side effect, `core/CLAUDE.md`) -- which is
why the gate holds recall@3, not hit@1.

The HARD set (`tests/fixtures/kb_eval_hard/` -- answers in CSV tables, facts
buried in long documents, near-identical sibling documents, Chinese queries
over English-only documents) is gated here too. Measured 2026-08-26, both
vector and hybrid scored recall@3 1.00 on every kind. Two shapes worth
knowing from that measurement, neither a failure:

  - vector ranked the ENGLISH handbook first for two Chinese long-document
    queries (the EN/ZH handbook pair is itself a cross-language distractor);
    the right document was rank 2, inside recall@3.
  - hybrid's crosslingual hit@1 was 0.00 -- the same BM25-leg tie preference
    as the paraphrase queries above -- with every right document at rank 2-3.
"""
import os
from pathlib import Path

import pytest

if not os.environ.get("BESTTEAM_LIVE_EVAL"):
    pytest.skip(
        "BESTTEAM_LIVE_EVAL is not set -- this is a by-hand release gate that "
        "spends real provider quota. Set BESTTEAM_LIVE_EVAL=1 to run it.",
        allow_module_level=True,
    )

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")
pytest.importorskip("langchain_openai")

if not os.environ.get("OPENAI_API_KEY"):
    pytest.skip(
        "OPENAI_API_KEY is not set -- the live retrieval gate needs a real "
        "embedding provider (cost: under $0.01)",
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
from bestteam.core.vector_knowledge_base import VectorKnowledgeBase  # noqa: E402

pytestmark = pytest.mark.optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_SET = Path(__file__).parent / "fixtures" / "kb_eval"
_HARD_SET = Path(__file__).parent / "fixtures" / "kb_eval_hard"
_EMBEDDING_MODEL = "openai:text-embedding-3-small"
_CACHE_PATH = _REPO_ROOT / ".bestteam_cache" / "kb_eval_live.json"

# recall@3 floors, calibrated from the measured 1.00s above with one query of
# slack per bucket -- a regression that loses a single paraphrase query out
# of four still fails, provider-side drift on one lexical query does not.
_OVERALL_FLOOR = 0.90     # 18 of 20
_LEXICAL_FLOOR = 0.9375   # 15 of 16
_PARAPHRASE_FLOOR = 0.75  # 3 of 4

# Hard-set recall@3 floors, same one-query-of-slack calibration from the
# measured 1.00s. `crosslingual` is the load-bearing bucket (BM25 scores 0 on
# it by construction, so only the embedding model holds it up).
_HARD_OVERALL_FLOOR = 0.90        # 16 of 17
_HARD_KIND_FLOORS = {
    "table": 0.75,         # 3 of 4
    "long": 0.75,          # 3 of 4
    "distractor": 0.80,    # 4 of 5
    "crosslingual": 0.75,  # 3 of 4
}


def _report(kb_cls, golden_set: Path = _GOLDEN_SET) -> EvalReport:
    kb = kb_cls(
        "golden_live",
        golden_set / "docs",
        embedding_model=_EMBEDDING_MODEL,
        chunk_size=DEFAULT_CHUNK_SIZE,
        chunk_overlap=DEFAULT_CHUNK_OVERLAP,
        top_k=DEFAULT_TOP_K,
        cache_path=_CACHE_PATH,
    )
    return evaluate(kb, load_queries(golden_set / "queries.yaml"), DEFAULT_TOP_K)


@pytest.fixture(scope="module")
def vector_report() -> EvalReport:
    return _report(VectorKnowledgeBase)


@pytest.fixture(scope="module")
def hybrid_report() -> EvalReport:
    return _report(HybridKnowledgeBase)


@pytest.fixture(scope="module")
def hard_vector_report() -> EvalReport:
    return _report(VectorKnowledgeBase, _HARD_SET)


@pytest.fixture(scope="module")
def hard_hybrid_report() -> EvalReport:
    return _report(HybridKnowledgeBase, _HARD_SET)


def _kind(report: EvalReport, kind: str) -> EvalReport:
    return report_for(
        [o for o in report.outcomes if o.query.kind == kind], report.top_k
    )


def _misses(report: EvalReport) -> str:
    """Every query whose expected document was not retrieved, for the
    assertion message -- recall@3 alone doesn't say WHICH query regressed."""
    lines = [
        f"  [{o.query.kind}] {o.query.query!r}: want {o.query.expected_source}, "
        f"got {', '.join(o.sources) or '(nothing)'}"
        for o in report.outcomes
        if o.rank is None or o.rank > report.top_k
    ]
    return "\n".join(lines) or "  (all queries retrieved their document)"


def _assert_floors(report: EvalReport, label: str) -> None:
    overall = report.recall_at_k
    lexical = _kind(report, "lexical").recall_at_k
    paraphrase = _kind(report, "paraphrase").recall_at_k
    assert (
        overall >= _OVERALL_FLOOR
        and lexical >= _LEXICAL_FLOOR
        and paraphrase >= _PARAPHRASE_FLOOR
    ), (
        f"{label} recall@{report.top_k} below the release floor: "
        f"overall {overall:.2f} (floor {_OVERALL_FLOOR}), "
        f"lexical {lexical:.2f} (floor {_LEXICAL_FLOOR}), "
        f"paraphrase {paraphrase:.2f} (floor {_PARAPHRASE_FLOOR}).\n"
        f"Missed:\n{_misses(report)}"
    )


def test_vector_meets_release_floors(vector_report):
    _assert_floors(vector_report, "vector")


def test_vector_recovers_paraphrase_queries(vector_report):
    """The reason this file exists: the queries BM25 misses by construction
    must be recovered by a real embedding model."""
    paraphrase = _kind(vector_report, "paraphrase")
    assert paraphrase.recall_at_k >= _PARAPHRASE_FLOOR, (
        f"vector retrieval no longer closes the paraphrase gap "
        f"(recall@{paraphrase.top_k} {paraphrase.recall_at_k:.2f}).\n"
        f"Missed:\n{_misses(paraphrase)}"
    )


def test_hybrid_meets_release_floors(hybrid_report):
    """Hybrid must keep BOTH legs' strengths: BM25's lexical scores AND the
    vector leg's paraphrase recovery must survive RRF fusion."""
    _assert_floors(hybrid_report, "hybrid")


def _assert_hard_floors(report: EvalReport, label: str) -> None:
    failures = []
    if report.recall_at_k < _HARD_OVERALL_FLOOR:
        failures.append(f"overall {report.recall_at_k:.2f} (floor {_HARD_OVERALL_FLOOR})")
    for kind, floor in _HARD_KIND_FLOORS.items():
        recall = _kind(report, kind).recall_at_k
        if recall < floor:
            failures.append(f"{kind} {recall:.2f} (floor {floor})")
    assert not failures, (
        f"{label} recall@{report.top_k} on the HARD set below the release "
        f"floor: {'; '.join(failures)}.\nMissed:\n{_misses(report)}"
    )


def test_vector_meets_hard_set_floors(hard_vector_report):
    """The hard set's failure modes -- table cells, buried facts, sibling
    distractors, crosslingual -- under the production default embedding
    model. Crosslingual is the bucket only the embedding model can hold."""
    _assert_hard_floors(hard_vector_report, "vector")


def test_hybrid_meets_hard_set_floors(hard_hybrid_report):
    """RRF fusion must not surrender what either leg wins on the hard set --
    in particular the vector leg's crosslingual recovery (where the BM25 leg
    contributes only noise) has to survive fusion into the top 3."""
    _assert_hard_floors(hard_hybrid_report, "hybrid")
