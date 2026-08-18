#!/usr/bin/env python
"""Measure a knowledge base's retrieval quality against a golden query set.

Run the bundled golden set through the default BM25 knowledge base:

    python scripts/kb_eval.py

Compare retrieval types on the same questions -- `local_folder` is keyword
search only, so the paraphrase queries are the headroom a real embedding
model should close:

    python scripts/kb_eval.py --type hybrid --embedding-model fake:32
    python scripts/kb_eval.py --type hybrid \\
        --embedding-model openai:text-embedding-3-small

`fake:` specs cost nothing but are deterministic noise -- they prove the
harness runs end to end, never that retrieval improved. Only a real
embedding/rerank model says anything about quality.

Point it at your own documents with `--docs`/`--queries`; see
docs/KNOWLEDGE_BASES.md, "Evaluating retrieval", for the query-set format.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

from bestteam.core.kb_eval import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    EvalReport,
    evaluate,
    load_queries,
    report_for,
)
from bestteam.core.loader import _KNOWLEDGE_BASE_TYPES
from bestteam.exceptions import BestTeamError, ConfigurationError

_GOLDEN_SET = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "kb_eval"


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--docs", type=Path, default=_GOLDEN_SET / "docs",
                        help="folder of documents to index (default: the bundled golden set)")
    parser.add_argument("--queries", type=Path, default=_GOLDEN_SET / "queries.yaml",
                        help="YAML query set (default: the bundled golden set)")
    parser.add_argument("--type", default="local_folder", choices=sorted(_KNOWLEDGE_BASE_TYPES),
                        help="knowledge base type (default: local_folder)")
    parser.add_argument("--embedding-model",
                        help="embedding spec, required by --type vector/hybrid "
                             "(e.g. fake:32, openai:text-embedding-3-small)")
    parser.add_argument("--rerank-model",
                        help="reranker spec (e.g. fake:, cross-encoder:<model-name>)")
    parser.add_argument("--expansion-model",
                        help="chat model spec used to expand each query into alternatives")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON instead of a table")
    return parser.parse_args(argv)


def _check_embedding_flag(args) -> None:
    """Reject a --type/--embedding-model combination the constructor would
    only reject as a raw TypeError. Asked of the constructor itself rather
    than a hard-coded list of types, so a knowledge base type added later
    cannot quietly reintroduce the traceback."""
    parameters = inspect.signature(_KNOWLEDGE_BASE_TYPES[args.type]).parameters
    accepted = "embedding_model" in parameters
    required = accepted and parameters["embedding_model"].default is inspect.Parameter.empty

    if required and not args.embedding_model:
        raise ConfigurationError(
            f"--type {args.type} requires --embedding-model "
            "(e.g. fake:32 for a $0 dry run, or openai:text-embedding-3-small)."
        )
    if args.embedding_model and not accepted:
        embedding_types = sorted(
            name for name, cls in _KNOWLEDGE_BASE_TYPES.items()
            if "embedding_model" in inspect.signature(cls).parameters
        )
        raise ConfigurationError(
            f"--embedding-model is not used by --type {args.type}; it is only "
            f"valid with --type {' or '.join(embedding_types)}."
        )


def _build_kb(args):
    kwargs = {
        "chunk_size": args.chunk_size,
        "chunk_overlap": args.chunk_overlap,
        "top_k": args.top_k,
    }
    if args.embedding_model:
        kwargs["embedding_model"] = args.embedding_model
    if args.rerank_model:
        kwargs["rerank_model"] = args.rerank_model
    if args.expansion_model:
        kwargs["query_expansion_model"] = args.expansion_model
    return _KNOWLEDGE_BASE_TYPES[args.type]("eval", args.docs, **kwargs)


#: Width of each score column, wide enough for the widest header
#: ("paraphrase(NN)") so the table stays aligned.
_COLUMN = 15


def _percent(value):
    return "-" if value is None else f"{value:.1%}"


def _row(label: str, key: str, reports) -> str:
    cells = "".join(_percent(getattr(report, key)).rjust(_COLUMN) for report in reports)
    return f"  {label:<18}{cells}"


def _print_table(args, report: EvalReport, by_kind) -> None:
    print(f"Knowledge base : {args.type}")
    print(f"Documents      : {args.docs}")
    print(f"Query set      : {args.queries}  ({len(report.outcomes)} queries)")
    print(f"Retrieval      : top_k={args.top_k}, chunk_size={args.chunk_size}, "
          f"chunk_overlap={args.chunk_overlap}")
    if args.embedding_model:
        print(f"Embeddings     : {args.embedding_model}")
    if args.rerank_model:
        print(f"Reranker       : {args.rerank_model}")
    if args.expansion_model:
        print(f"Expansion      : {args.expansion_model}")
    print()

    kinds = sorted(by_kind)
    reports = [report] + [by_kind[kind] for kind in kinds]
    headers = ["all"] + [f"{kind}({len(by_kind[kind].outcomes)})" for kind in kinds]
    print("  " + "metric".ljust(18) + "".join(header.rjust(_COLUMN) for header in headers))
    print(_row(f"recall@{args.top_k}", "recall_at_k", reports))
    print(_row("MRR", "mrr", reports))
    print(_row("hit@1", "hit_at_1", reports))
    print(_row(f"substring hit@{args.top_k}", "substring_hit_at_k", reports))
    print()

    imperfect = [outcome for outcome in report.outcomes if outcome.rank != 1]
    if not imperfect:
        print("Every query ranked its expected document first.")
        return
    print(f"Did not rank the expected document first ({len(imperfect)}):")
    for outcome in imperfect:
        rank = "not retrieved" if outcome.rank is None else f"rank {outcome.rank}"
        print(f"  [{outcome.query.kind}] {outcome.query.query}")
        print(f"      want {outcome.query.expected_source} ({rank}); "
              f"got {', '.join(outcome.sources) or '(nothing)'}")


def _as_json(args, report: EvalReport, by_kind) -> str:
    def scores(one: EvalReport):
        return {
            "queries": len(one.outcomes),
            "recall_at_k": one.recall_at_k,
            "mrr": one.mrr,
            "hit_at_1": one.hit_at_1,
            "substring_hit_at_k": one.substring_hit_at_k,
        }

    return json.dumps({
        "type": args.type,
        "docs": str(args.docs),
        "queries": str(args.queries),
        "top_k": args.top_k,
        "chunk_size": args.chunk_size,
        "chunk_overlap": args.chunk_overlap,
        "embedding_model": args.embedding_model,
        "rerank_model": args.rerank_model,
        "expansion_model": args.expansion_model,
        "overall": scores(report),
        "by_kind": {kind: scores(one) for kind, one in sorted(by_kind.items())},
        "results": [
            {
                "query": outcome.query.query,
                "kind": outcome.query.kind,
                "expected_source": outcome.query.expected_source,
                "rank": outcome.rank,
                "sources": outcome.sources,
                "substring_hit": outcome.substring_hit,
            }
            for outcome in report.outcomes
        ],
    }, ensure_ascii=False, indent=2)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        _check_embedding_flag(args)
        queries = load_queries(args.queries)
        report = evaluate(_build_kb(args), queries, args.top_k)
    except BestTeamError as exc:
        print(f"kb_eval: {exc}", file=sys.stderr)
        return 2

    kinds = {outcome.query.kind for outcome in report.outcomes}
    by_kind = {
        kind: report_for([o for o in report.outcomes if o.query.kind == kind], args.top_k)
        for kind in kinds
    }
    if args.json:
        print(_as_json(args, report, by_kind))
    else:
        _print_table(args, report, by_kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
