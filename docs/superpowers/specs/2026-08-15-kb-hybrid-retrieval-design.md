# KB hybrid retrieval + query expansion — design

Date: 2026-08-15
Status: design (ready for implementation)
Base: `main` @ `e9207a0` (after the team-sharing/continuous-chat and
e2e-tiering merges; independent of them — touches none of their files).

## Problem

`core/knowledge_base.py` (`LocalFolderKnowledgeBase`, BM25) and
`core/vector_knowledge_base.py` (`VectorKnowledgeBase`, cosine) are each a
single retrieval method: one KB config picks BM25 *or* vector, never both.
Reranking (`docs/superpowers/specs/2026-08-12-pluggable-rerank-design.md`) can
only re-order candidates that a single-method retrieval pass already
surfaced — a BM25 KB's rerank stage can never recover a chunk with zero
keyword overlap with the query, no matter how semantically relevant it is.
`core/memory.py`'s `MemoryManager` already solved this shape of problem for
per-user memory (hybrid BM25+vector fusion via `_reciprocal_rank_fusion`,
plus opt-in query expansion via `_expand_query`) — this design brings both
capabilities to knowledge bases, reusing Memory's proven algorithms rather
than inventing new ones, and dedupes the fusion/expansion logic into a
shared module both subsystems import from.

Scope, agreed with the user before writing this doc:
- Hybrid BM25+vector retrieval as a new `type: "hybrid"` knowledge base.
- Query expansion (`query_expansion_model`/`query_expansion_count`) added to
  **all three** KB types (`local_folder`, `vector`, `hybrid`), not just the
  new hybrid type — this requires refactoring the two existing types' `query()`
  bodies to route through a shared multi-query fusion helper, justified below.
- Explicitly **out of scope**: chunk-level citation metadata
  (`document_id`/`chunk_id`/page), a data-model rework, async ingestion, and
  KB-side usage metering for the new LLM call. These are separate, independent
  sub-projects the originating review also identified.

## Key facts (verified against source, this session)

- `_Chunk` (`core/knowledge_base.py:42`) is `NamedTuple(source, text)` — no
  id. `_load_document_chunks` (`core/knowledge_base.py:252`) is shared by
  both existing KB types (`vector_knowledge_base.py` imports it), producing
  one `List[_Chunk]` per KB instance from `_chunk_text`'s format-aware
  splitter. A single `HybridKnowledgeBase` instance can therefore build a
  BM25 index and an embedding matrix over the *same* `self._chunks` list —
  chunk identity for fusion is just its position in that list, so no
  `document_id`/`chunk_id` scheme is needed for this design.
- `reranking.py`'s `_rerank_fetch_k(top_k, candidate_k, reranker)`,
  `_rerank_candidates(query, candidates, reranker, top_k)`, and
  `_resolve_candidate_k(candidate_k, top_k)` are already generic over any
  `List[(score, _Chunk)]` — reused **unmodified** by the new hybrid type and
  by the refactored `local_folder`/`vector` query paths. `_rerank_candidates`
  takes pure reranker order for the final `top_k` (no re-fusion with
  pre-rerank order) — this design keeps that KB-side contract as-is; it does
  **not** adopt Memory's weighted-RRF-after-rerank trick (see "Deferred").
- `memory.py::_reciprocal_rank_fusion(*ranked_id_lists, k=60, weights=None)`
  (`core/memory.py:126`) is rank-based: fused score is `weight / (k + rank)`,
  strictly monotonic in rank. Fusing a **single** ranked list therefore
  reproduces that list's exact original order (no ties possible — rank is
  strictly increasing, `k`/`weight` are constants) — this is what makes
  routing `local_folder`/`vector`'s existing single-query, single-leg query
  path through the new shared multi-query/multi-leg helper *provably*
  behavior-preserving when query expansion is unset.
- `memory.py::MemoryManager._expand_query` (`core/memory.py:1309`) is a
  method with model-call + parsing + usage-metering (`_usage_entry`) all
  inline. The model-call + parsing half is what this design extracts into a
  free function; usage-metering stays `MemoryManager`-specific (KB does not
  meter — see next point).
- KB tools (`make_knowledge_base_tool`, `core/knowledge_base.py:309`) are
  plain single-argument functions invoked inside the agent's generic
  tool-calling loop (`adapters/langgraph_adapter.py`) — unlike Memory's
  recall, which runs at the `Workflow.stream()` orchestration layer with a
  `TraceEvent`/`memory_recalled` hook into `usage_records`, a KB tool has no
  mechanism to report a nested LLM call's token usage back to the backend.
  **A KB query-expansion call's cost will be unmetered/unbilled.** This is
  not a new gap: `VectorKnowledgeBase`'s embedding calls are already
  unmetered today for the identical reason. Documented, not fixed, in this
  design.
- `_KNOWLEDGE_BASE_TYPES` (`core/loader.py:16`) is a flat `{type_str: class}`
  dict; `_build_knowledge_base` (`core/loader.py:104`) pops `name`/`path`/`type`
  and splats the rest as constructor kwargs — adding `"hybrid"` needs no
  loader logic change beyond one new dict entry.
- `KnowledgeBaseSpec` (`core/specification.py:145`) gates
  `embedding_model`/`score_threshold`/`cache_path` to `type == "vector"` in
  `to_raw()`; this design changes that gate to `type in ("vector", "hybrid")`.
  `rerank_model`/`candidate_k` are already emitted for every type (no change
  needed there).
- `pyproject.toml` extras: `tools-rag = ["rank-bm25>=0.2.2"]`,
  `tools-rag-vector = ["numpy>=1.24"]` (lines 20-21). `hybrid` requires both;
  no new extra is added (see "Deferred") — `pip install
  'bestteam[tools-rag,tools-rag-vector]'` covers it.
- `tests/test_memory.py:16` imports `_reciprocal_rank_fusion` directly from
  `bestteam.core.memory` and calls `mgr._expand_query(...)` as a
  `MemoryManager` method (lines 642/654/668) — both must keep working
  unchanged after the extraction (see "Files").

## Approach

### 1. New shared module `core/fusion.py`

```python
def reciprocal_rank_fusion(
    *ranked_id_lists: Sequence[Any], k: int = 60, weights: Optional[Sequence[float]] = None
) -> Dict[Any, float]:
    """Moved verbatim from core/memory.py's _reciprocal_rank_fusion (same
    body, same semantics) -- now a shared primitive, id type widened from
    str to Any so a KB caller can fuse by integer chunk index instead of
    a string id."""


def expand_query(
    model_spec: Any, query: str, count: int
) -> "tuple[List[str], Optional[Any]]":
    """The model-call + parsing half of MemoryManager._expand_query,
    extracted as a free function: resolves model_spec via _resolve_model,
    invokes it with the existing _QUERY_EXPANSION_SYSTEM_PROMPT, parses up
    to `count` alternative phrasings. NEVER raises -- count<=0, no model
    configured, invoke error, or unparseable response all return ([], None).
    Returns the raw response object (or None) so a caller that wants usage
    metering (MemoryManager) can extract it; a caller that doesn't
    (knowledge bases) just discards it."""
```

`memory.py` changes to:
```python
from .fusion import expand_query as _expand_query_impl
from .fusion import reciprocal_rank_fusion as _reciprocal_rank_fusion
```
— the second import is a **name-preserving alias** specifically so
`tests/test_memory.py`'s `from bestteam.core.memory import
_reciprocal_rank_fusion` keeps working with zero test changes.
`MemoryManager._expand_query` becomes a thin wrapper: calls
`_expand_query_impl(self.query_expansion_model, query, self.query_expansion_count)`,
then applies its existing `_usage_entry(response, self.query_expansion_model)`
metering on the returned response. Behavior is unchanged; verified by running
the existing `test_memory.py`/`test_memory_backend.py`/`test_memory_integration.py`
suites, which already cover this method's contract.

### 2. Shared multi-query/multi-leg retrieval helper (`core/knowledge_base.py`)

```python
def _rrf_retrieve(
    query_variants: List[str],
    legs: List[Callable[[str, int], List[int]]],
    fetch_k: int,
) -> List[int]:
    """Run every (query_variant, leg) pair, each returning a ranked list of
    chunk indices capped at fetch_k; fuse ALL resulting lists (variants x
    legs) with one unweighted reciprocal_rank_fusion call; return chunk
    indices ordered by fused score, descending. A leg is a closure over one
    KB's own chunks/index (e.g. BM25 scoring, or cosine scoring) --
    parameterizing on `(query_text, fetch_k) -> List[int]` is what lets one
    function serve local_folder (1 leg), vector (1 leg), and hybrid (2 legs)
    identically. With exactly one variant and one leg this reproduces that
    leg's own order exactly (see "Key facts" -- RRF over a single list is
    order-preserving)."""
    ranked_lists = [leg(q, fetch_k) for q in query_variants for leg in legs]
    fused = reciprocal_rank_fusion(*ranked_lists)
    return [idx for idx, _score in sorted(fused.items(), key=lambda p: p[1], reverse=True)]


def _query_variants(
    query: str, query_expansion_model: Any, query_expansion_count: int
) -> List[str]:
    """[query] + expand_query(...)[0] -- expansion failures/unset already
    degrade to [] inside expand_query, so this never raises and never
    returns an empty list."""
```

Both are exported for `vector_knowledge_base.py` and the new
`hybrid_knowledge_base.py` to import, the same way those modules already
import `_load_document_chunks`/`_rerank_candidates`/`_rerank_fetch_k`/
`_validate_chunk_params` from `knowledge_base.py` today.

### 3. `LocalFolderKnowledgeBase` changes

Constructor gains `query_expansion_model: Any = None`,
`query_expansion_count: int = 3` (stored as-is; resolution is lazy, inside
`query()`, matching `expand_query`'s own fail-soft contract — no eager
validation needed since a bad spec/count already degrades to `[]`
internally).

The existing scoring body (BM25 `get_scores` + significant-term-overlap
filter + `(overlap, score)` sort) is extracted, unchanged, into a leg
closure:
```python
def _bm25_leg(self, query_text: str, fetch_k: int) -> List[int]:
    # identical logic to today's query(), returning chunk indices instead
    # of (score, chunk) tuples
```
`query()` becomes:
```python
def query(self, query: str, top_k: Optional[int] = None) -> str:
    top_k = top_k or self.default_top_k
    variants = _query_variants(query, self.query_expansion_model, self.query_expansion_count)
    fetch_k = _rerank_fetch_k(top_k, self._candidate_k, self._reranker)
    ranked_indices = _rrf_retrieve(variants, [self._bm25_leg], fetch_k)
    candidate_k = self._candidate_k if self._reranker is not None else top_k
    results = [(float(-i), self._chunks[idx]) for i, idx in enumerate(ranked_indices[:candidate_k])]
    results = _rerank_candidates(query, results, self._reranker, top_k)
    ... # formatting unchanged
```
The `score` half of each `(score, chunk)` tuple is now a synthetic
rank-derived placeholder (`_rerank_candidates`/formatting never surface it
numerically — verified in "Key facts" of the rerank design doc — so this is
safe). When `query_expansion_model` is unset, `variants == [query]`, one leg
→ `_rrf_retrieve` reproduces `_bm25_leg`'s own order exactly, and
`fetch_k`/`candidate_k` slicing is identical to today's — **byte-for-byte
unchanged output**, the same invariant every other opt-in KB feature in this
codebase already follows.

### 4. `VectorKnowledgeBase` changes (mirrors §3)

Gains the same two constructor params. The existing cosine-scoring +
`score_threshold` filter body becomes `_vector_leg(query_text, fetch_k) ->
List[int]` (threshold filtering happens inside the leg, before its ranked
list is handed to fusion — unaffected by expansion/fusion, same semantics as
today). `query()` follows the same `_query_variants` → `_rrf_retrieve` →
slice → `_rerank_candidates` shape as §3.

### 5. New `core/hybrid_knowledge_base.py::HybridKnowledgeBase`, `type: "hybrid"`

```python
class HybridKnowledgeBase(KnowledgeBase):
    def __init__(
        self, name, path, embedding_model,
        chunk_size=1000, chunk_overlap=100, top_k=5,
        score_threshold=None, cache_path=None,
        rerank_model=None, candidate_k=None,
        query_expansion_model=None, query_expansion_count=3,
    ) -> None:
        # requires BOTH rank_bm25 and numpy -- ConfigurationError naming
        # whichever's missing, same message shape as the two existing types
        ...
        self._chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        # BM25 index over self._chunks (same as LocalFolderKnowledgeBase.__init__)
        # embedding matrix over self._chunks (same as VectorKnowledgeBase.__init__)

    def _bm25_leg(self, query_text, fetch_k) -> List[int]: ...   # identical to §3's
    def _vector_leg(self, query_text, fetch_k) -> List[int]: ... # identical to §4's

    def query(self, query, top_k=None) -> str:
        top_k = top_k or self.default_top_k
        variants = _query_variants(query, self.query_expansion_model, self.query_expansion_count)
        fetch_k = _rerank_fetch_k(top_k, self._candidate_k, self._reranker)
        ranked_indices = _rrf_retrieve(variants, [self._bm25_leg, self._vector_leg], fetch_k)
        ...  # same slice/rerank/format shape as §3/§4
```
Two legs, equal-weighted in the RRF fusion (no per-leg weight knob — see
"Deferred"). Loads chunks once; both indexes are built over the identical
`self._chunks`, so fusing the BM25-leg and vector-leg ranked-index lists
needs no id scheme beyond that shared array position (see "Key facts").

### 6. Config/loader wiring

`core/loader.py`: `_KNOWLEDGE_BASE_TYPES["hybrid"] = HybridKnowledgeBase`.

`core/specification.py::KnowledgeBaseSpec` gains:
- `query_expansion_model: Optional[str] = None`
- `query_expansion_count: int = 3`

both always emitted in `to_raw()` (valid for every type, unlike
`embedding_model` et al.). The existing `if self.type == "vector":` gate
around `embedding_model`/`score_threshold`/`cache_path` becomes `if self.type
in ("vector", "hybrid"):`.

### 7. Error handling

| Failure | Behavior |
|---|---|
| Missing `rank-bm25`/`numpy` for `hybrid`, at construction | `ConfigurationError`, fail-hard (workflow load fails) — same as today's per-type guards |
| Bad `query_expansion_model` spec / invoke error / unparseable response, at query time | Silently degrades to `variants = [query]` (existing `expand_query` contract) — a query never fails because expansion failed |
| Cross-encoder inference error | Unchanged: falls back to pre-rerank `candidate_k` order (existing `_rerank_candidates` contract, untouched) |
| `candidate_k` invalid | Unchanged: `ConfigurationError` at load (YAML-authored, existing per-type check) |

### 8. Deferred (explicitly out of scope for this design)

- **Chunk-level citation metadata** (`document_id`/`chunk_id`/page/score).
  Sidestepped entirely here — fusion identity is the chunk's position in one
  shared `self._chunks` list, which only works because BM25 and vector
  indexes live in the *same* `HybridKnowledgeBase` instance. A future design
  that needs to fuse across independently-configured KB instances (or wants
  precise citations) will need real chunk ids; not needed for this design.
- **Per-leg RRF weighting** (trusting BM25 over vector, or vice versa).
  Equal weight for both legs, matching Memory's own default hybrid-fusion
  behavior (`_reciprocal_rank_fusion(bm25_ids, vector_ids)`, no explicit
  weights). No customer-facing knob added without eval data to tune it
  against — same reasoning the rerank design doc already applied to
  `_RERANK_RRF_WEIGHT`.
- **Weighted re-fusion after rerank** (Memory's
  pre-rerank-order-vs-rerank-order re-fusion trick). KB rerank keeps its
  existing, unmodified contract: pure reranker order wins for the final
  `top_k`. Not adopted here since it would mean modifying
  `_rerank_candidates`'s tested behavior for no KB-specific evidence of need.
- **KB-side usage metering** for the query-expansion LLM call. Documented
  as unmetered (see "Key facts"), not fixed — fixing it means giving KB
  tools a way to report nested-call usage back through the adapter's
  `TraceEvent`/`usage_records` pipeline, which is a separate, larger design.
- **New `tools-rag-hybrid` convenience pyproject extra.** `hybrid` requires
  installing both existing extras; not enough packaging friction yet to
  justify a third extra name.
- **`.env.example` entries.** No new backend env vars — every new knob here
  is YAML/`KnowledgeBaseSpec`-authored, not env-authored (KBs have never had
  an env-config path, unlike Memory).
- **Rerank score exposed to the caller/agent.** Formatted output shape is
  unchanged (`"N. [source: ...]\n<text>"`), matching the existing rerank
  design's same deferral.

## Files

- `src/bestteam/core/fusion.py` (new) — `reciprocal_rank_fusion()` (moved
  from `memory.py`), `expand_query()` (extracted from
  `MemoryManager._expand_query`).
- `src/bestteam/core/memory.py` — import `reciprocal_rank_fusion`/
  `expand_query` from `fusion.py` (name-preserving alias for the former, see
  §1); `MemoryManager._expand_query` becomes a thin wrapper.
- `src/bestteam/core/knowledge_base.py` — `_rrf_retrieve()`,
  `_query_variants()` (new, shared); `LocalFolderKnowledgeBase.__init__`
  (`query_expansion_model`/`query_expansion_count`), `_bm25_leg()`,
  `query()` rewrite.
- `src/bestteam/core/vector_knowledge_base.py` —
  `VectorKnowledgeBase.__init__` (same two new params), `_vector_leg()`,
  `query()` rewrite (reuses `_rrf_retrieve`/`_query_variants` from
  `knowledge_base.py`, same import pattern as today).
- `src/bestteam/core/hybrid_knowledge_base.py` (new) —
  `HybridKnowledgeBase`.
- `src/bestteam/core/loader.py` — `_KNOWLEDGE_BASE_TYPES["hybrid"]`.
- `src/bestteam/core/specification.py` — `KnowledgeBaseSpec.query_expansion_model`/
  `.query_expansion_count`; `to_raw()`'s vector-only gate widened to
  `("vector", "hybrid")`.
- `src/bestteam/core/CLAUDE.md` — document `hybrid` type, query expansion on
  all three types, and the unmetered-expansion-cost limitation (next to the
  existing "Known limitation: vector knowledge base retrieval is
  single-stage" paragraph — this closes the rest of it).
- Tests (new + extended):
  - `tests/test_fusion.py` (new, or extend an existing core-primitives test
    file) — `reciprocal_rank_fusion`/`expand_query` unit tests, migrated
    from/alongside whatever `test_memory.py` already covers for the moved
    function.
  - `tests/test_memory.py` — verify unchanged: `_reciprocal_rank_fusion`
    import, `mgr._expand_query(...)` calls, full existing suite green.
  - `tests/test_knowledge_base.py` — query-expansion-unset byte-identical
    regression; expansion actually changes results with a `fake:`-style
    deterministic expansion double; `_rrf_retrieve` single-list
    order-preservation unit test.
  - `tests/test_vector_knowledge_base.py` — same matrix, plus
    `score_threshold` still applies per-leg pre-fusion.
  - `tests/test_hybrid_knowledge_base.py` (new) — fusion correctness (a
    query engineered so BM25-only and vector-only would rank differently,
    assert hybrid recovers a vector-only-visible chunk BM25 alone would
    miss); `candidate_k` bounds; rerank integration (`fake:` reranker);
    query expansion integration; missing-dependency `ConfigurationError`
    (BM25 lib and numpy each independently, and both present but no
    embedding provider).
  - `tests/test_specification.py` — `KnowledgeBaseSpec` round-trips
    `query_expansion_model`/`query_expansion_count` for all three types;
    `hybrid` emits `embedding_model` etc. in `to_raw()`.
  - `tests/test_org_knowledge_bases.py` / `tests/test_crud_api.py` — a
    smoke check that `type: "hybrid"` round-trips through the admin CRUD
    API's `KnowledgeBaseSpec` validation, matching existing `vector`
    coverage there.

## Verification

- Full suite: `.\.venv\Scripts\python.exe -m pytest`.
- KB byte-identical regression: for both `local_folder` and `vector`, run
  the exact same query against an unmodified pre-change fixture and a
  post-change instance (no `query_expansion_model`, no `rerank_model`) and
  assert identical formatted output.
- Hybrid recovery case: build a `HybridKnowledgeBase` with `fake:<dim>`
  embeddings over documents where one chunk shares zero significant terms
  with the query but is the closest embedding match; assert it appears in
  hybrid results and would not appear from a BM25-only `LocalFolderKnowledgeBase`
  built from the same documents.
- Query expansion: with a deterministic expansion double (mirrors
  `_FakeReranker`'s pattern — no live model), assert results include a chunk
  matched only by an expansion variant, not the literal query, for all three
  KB types.
- End-to-end: run one of the existing `fake:`-model demo workflows with a
  new `type: "hybrid"` KB config (`ui/backend/workflows/` — add a
  `hybrid_knowledge_base_demo.yaml` alongside the existing
  `vector_knowledge_base_demo.yaml`) via `bestteam run`, confirm it
  completes with fused results in the tool output.
