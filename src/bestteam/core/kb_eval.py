"""Retrieval-quality evaluation for knowledge bases.

A knowledge base's retrieval quality is otherwise judged by whoever last
tried a query and thought the answer looked reasonable. This module turns
that into a number: run a fixed set of queries whose right answer is known
against a `KnowledgeBase`, and report how often the expected document came
back and how highly it ranked.

The metrics are deliberately the cheap, standard ones, computed **per source
document** rather than per chunk -- a knowledge base that returns three
chunks of the right document has answered the question, whichever chunk of
it ranked first. Because each query in the golden set has exactly one
relevant document, recall@k here is the same quantity as hit@k: did the one
right document appear in the top k results.

`evaluate()` consumes `KnowledgeBase.search()` (structured chunks), never the
formatted `query()` string, so a change to the citation format can't quietly
change a score.

The golden set lives in `tests/fixtures/kb_eval/`; `scripts/kb_eval.py` is
the command-line front end. See `docs/KNOWLEDGE_BASES.md`, "Evaluating
retrieval".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import yaml

from ..exceptions import ConfigurationError

# The smoke-test configuration, shared by the CLI's defaults and the tests so
# a documented number can't drift away from the one actually measured. The
# golden documents are short (300-600 characters), so a production-sized
# 1000-character chunk would make most of them a single chunk and stop
# exercising chunking at all.
DEFAULT_TOP_K = 3
DEFAULT_CHUNK_SIZE = 300
DEFAULT_CHUNK_OVERLAP = 50

# `lexical`/`paraphrase` classify the bundled easy set; the last four
# classify the hard set's failure modes (tests/fixtures/kb_eval_hard/
# queries.yaml documents each). `report_for` + the CLI group by kind
# generically, so a kind is just a label with a documented meaning.
_KINDS = ("lexical", "paraphrase", "table", "long", "distractor", "crosslingual")


@dataclass(frozen=True)
class EvalQuery:
    """One graded question: what to ask, and which document answers it.

    `kind` records why the query is in the set -- `lexical` shares wording
    with its document (keyword search is expected to find it), `paraphrase`
    deliberately shares none (BM25 alone is expected to miss it, so the set
    can show what embeddings add). `expected_substring` is an optional
    stricter check: a concrete fact that must appear in a retrieved chunk of
    `expected_source` itself, which catches a chunking regression that splits
    the answer away from the words that retrieved it.
    """

    query: str
    expected_source: str
    kind: str = "lexical"
    expected_substring: Optional[str] = None


@dataclass(frozen=True)
class QueryOutcome:
    """What one query actually retrieved."""

    query: EvalQuery
    #: Source documents, best first, de-duplicated -- several chunks of the
    #: same document count once, at the position of the best of them.
    sources: List[str]
    #: 1-based position of `query.expected_source` in `sources`, or None if
    #: it never appeared.
    rank: Optional[int]
    #: Whether `expected_substring` appeared in a retrieved chunk OF THE
    #: EXPECTED DOCUMENT; None when the query declares no substring. A chunk
    #: of another document containing the same words is not a hit.
    substring_hit: Optional[bool]


@dataclass(frozen=True)
class EvalReport:
    """Aggregate scores plus the per-query outcomes behind them."""

    top_k: int
    outcomes: List[QueryOutcome]
    recall_at_k: float
    mrr: float
    hit_at_1: float
    #: Mean over the queries that declare an `expected_substring`; None when
    #: no query in the set declares one.
    substring_hit_at_k: Optional[float]


def rank_of(sources: Sequence[str], expected: str) -> Optional[int]:
    """The 1-based position of `expected` in `sources`, or None if absent."""
    for position, source in enumerate(sources, start=1):
        if source == expected:
            return position
    return None


def recall_at_k(ranks: Sequence[Optional[int]], k: int) -> float:
    """Fraction of queries whose expected document landed within the top `k`.

    With one relevant document per query this is also hit@k. An empty set of
    queries scores 0.0 rather than raising.
    """
    if not ranks:
        return 0.0
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def mrr(ranks: Sequence[Optional[int]]) -> float:
    """Mean reciprocal rank: a hit at position 1 scores 1.0, at position 2
    scores 0.5, a miss scores 0. Rewards ranking the right document highly,
    not merely returning it somewhere."""
    if not ranks:
        return 0.0
    return sum(1 / rank for rank in ranks if rank is not None) / len(ranks)


def load_queries(path: str | Path) -> List[EvalQuery]:
    """Read a golden set's `queries.yaml`.

    Shape: a top-level `queries:` list, each entry `{query, expected_source,
    kind?, expected_substring?}`. Anything else raises `ConfigurationError`
    naming the offending entry -- a typo in a golden set should fail the run
    that reads it, not silently score zero.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Could not read query set '{path}': {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("queries"), list):
        raise ConfigurationError(
            f"Query set '{path}' must be a mapping with a 'queries' list."
        )

    queries: List[EvalQuery] = []
    for index, entry in enumerate(raw["queries"], start=1):
        where = f"Query set '{path}', entry {index}"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{where}: expected a mapping, got {type(entry).__name__}.")
        for field in ("query", "expected_source"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ConfigurationError(f"{where}: '{field}' must be a non-empty string.")
        kind = entry.get("kind", "lexical")
        if kind not in _KINDS:
            raise ConfigurationError(
                f"{where}: 'kind' must be one of {', '.join(_KINDS)} (got {kind!r})."
            )
        substring = entry.get("expected_substring")
        if substring is not None and not isinstance(substring, str):
            raise ConfigurationError(f"{where}: 'expected_substring' must be a string.")
        queries.append(EvalQuery(
            query=entry["query"],
            expected_source=entry["expected_source"],
            kind=kind,
            expected_substring=substring,
        ))
    return queries


def report_for(outcomes: Sequence[QueryOutcome], top_k: int) -> EvalReport:
    """Aggregate already-collected outcomes. Separate from `evaluate()` so a
    caller can score a subset -- the CLI reports the lexical and paraphrase
    halves of a golden set side by side without retrieving anything twice."""
    ranks = [outcome.rank for outcome in outcomes]
    graded = [outcome.substring_hit for outcome in outcomes if outcome.substring_hit is not None]
    return EvalReport(
        top_k=top_k,
        outcomes=list(outcomes),
        recall_at_k=recall_at_k(ranks, top_k),
        mrr=mrr(ranks),
        hit_at_1=recall_at_k(ranks, 1),
        substring_hit_at_k=(sum(graded) / len(graded)) if graded else None,
    )


def evaluate(kb, queries: Sequence[EvalQuery], top_k: int = DEFAULT_TOP_K) -> EvalReport:
    """Run every query against `kb` and score the results.

    `kb` is any `KnowledgeBase` -- only `search(query, top_k)` is used, so a
    `local_folder`, `vector` or `hybrid` base (with or without reranking and
    query expansion) is measured the same way.
    """
    # Every built-in `search()` treats a falsy cutoff as "use my default", so
    # `top_k=0` would retrieve five chunks and then be scored as recall@0 --
    # a report that contradicts itself. Refuse it before any retrieval; the
    # CLI's `--top-k` reaches here too.
    if top_k < 1:
        raise ConfigurationError(f"top_k must be at least 1, got {top_k}.")
    outcomes: List[QueryOutcome] = []
    for query in queries:
        chunks = kb.search(query.query, top_k)
        sources: List[str] = []
        for chunk in chunks:
            if chunk.source not in sources:
                sources.append(chunk.source)
        substring_hit = None
        if query.expected_substring is not None:
            # Only the expected document's own chunks can satisfy this. A
            # chunk of some other document that happens to contain the same
            # words is a retrieval miss, not a partial credit -- scoring it
            # as a hit would decouple the metric from the chunking regression
            # it exists to catch.
            substring_hit = any(
                chunk.source == query.expected_source
                and query.expected_substring in chunk.text
                for chunk in chunks
            )
        outcomes.append(QueryOutcome(
            query=query,
            sources=sources,
            rank=rank_of(sources, query.expected_source),
            substring_hit=substring_hit,
        ))

    return report_for(outcomes, top_k)
