# Pluggable rerank for knowledge bases and memory — design

Date: 2026-08-12
Status: design (ready for implementation)
Base: `main` @ `85e3ac0` (after the memory hybrid-recall/query-expansion/
workflow-scoping PRs; independent of them — this adds a new stage on top,
touches none of their internals except `_reciprocal_rank_fusion`'s signature).

## Problem

Both retrieval paths in this project are single-stage: `core/knowledge_base.py`
(`LocalFolderKnowledgeBase`, BM25) and `core/vector_knowledge_base.py`
(`VectorKnowledgeBase`, cosine) return their raw top-k with no re-scoring
pass; `core/memory.py`'s `MemoryManager.recall()` — even with hybrid
BM25+vector and query expansion enabled — never re-ranks its fused
candidates with anything more precise than RRF over coarse retrieval scores.
Neither path has a cross-encoder or LLM re-scoring stage. This is documented
as a known limitation in `src/bestteam/core/CLAUDE.md` ("Known limitation:
vector knowledge base retrieval is single-stage" / memory's "Still no
reranking... deferred to a future pass") and is the single highest-leverage
gap identified when the project's RAG stack was benchmarked against mature
platforms (Anthropic's Contextual Retrieval writeup cites reranking as the
largest incremental accuracy gain on top of hybrid retrieval).

This design adds an **opt-in, pluggable rerank stage** to both `local_folder`
and `vector` knowledge bases, and to `MemoryManager`'s recall — using a local
cross-encoder as the v1 backend, following the same "spec string, resolved
lazily, `fake:` for $0 tests" convention already established by
`resolve_embedding_model` (`core/embeddings.py`) and `_resolve_model`.

## Key facts (verified against source, this session)

- `resolve_embedding_model(spec)` (`core/embeddings.py:12`) is the template to
  mirror: accepts a live instance, `"fake:<dim>"`, or a provider string;
  raises `ConfigurationError` uniformly on a bad spec. It does **not** decide
  fail-hard vs. fail-soft — callers do (`VectorKnowledgeBase.__init__` lets
  the raise propagate; `SqliteBM25Memory.__init__` does too, and it's the
  *backend* — `ui/backend/runtime.py::_make_memory`'s `try/except` around
  `SqliteBM25Memory(...)` — that turns that into "memory disabled for this
  run", not the resolver or the store).
- `LocalFolderKnowledgeBase.query()` (`core/knowledge_base.py:79`) and
  `VectorKnowledgeBase.query()` (`core/vector_knowledge_base.py:123`) are
  each a single object built once when a workflow is loaded/compiled
  (`ui/backend/main.py`'s `_workflow_cache` / `Workflow._compiled` keep it
  alive across runs) — so eager, fail-hard resolution of a new dependency at
  `__init__` is the existing, correct pattern here (mirrors how missing
  `rank_bm25`/`numpy` already raise `ConfigurationError` at construction).
- `MemoryManager` is **rebuilt every run** — `ui/backend/runtime.py:159`
  `_make_memory()` is called "on the worker thread that runs the workflow",
  i.e. fresh per `POST /api/runs`. Every other `MemoryManager`-level model
  knob (`extraction_model`, `query_expansion_model`) is therefore already
  lazy + fail-soft: resolved on first use inside its own try/except, never
  raising, degrading to "as if unset" (`_expand_query`, `core/memory.py:1267`,
  never raises). Only the *store's* `embedding_model` is eager+fail-hard,
  because that's baked into `SqliteBM25Memory.__init__`, not into
  `MemoryManager`. A new `MemoryManager`-level knob should follow the
  manager's own convention (lazy + fail-soft), not the store's.
- `MemoryManager._fused_search()` (`core/memory.py:1333`) has a `len(queries)
  == 1` early-return that calls `store.search()` directly, bypassing
  `_reciprocal_rank_fusion` entirely for the no-query-expansion case. Feeding
  a single ranked list through `_reciprocal_rank_fusion` produces the *same*
  relative order (scores are monotonic in rank: `1/(k+rank)`), so this
  special case can be removed once rerank needs a unified post-fusion hook —
  not a gratuitous refactor, a precondition for applying rerank identically
  regardless of whether query expansion is active.
- `MemoryManager.recall()` (`core/memory.py:1355`) issues **two** independent
  scoped searches (`semantic`; `episodic`/`procedural`) via `_fused_search`,
  each capped at `self.top_k`. Rerank must run once per scope, independently,
  always scored against the literal query in `queries[0]` — never an
  expansion variant (expansions exist only to widen recall, not to be
  reranked against).
- `_reciprocal_rank_fusion(*ranked_id_lists, k=60)` (`core/memory.py:101`) is
  unweighted today, used at two call sites (BM25+vector hybrid fusion,
  query-expansion fusion). Verified numerically that unweighted RRF can bury
  a reranker's #1 pick below a candidate that's merely consistently
  mid-ranked on both signals (k=60: retrieval-rank-20 + rerank-rank-1 scores
  0.0289 combined, vs. retrieval-rank-5 + rerank-rank-5 scores 0.0308 — the
  mediocre-but-consistent candidate wins) — reusing it unweighted for the
  rerank-vs-pre-rerank combination would materially dilute what rerank is
  for. A first pass at fixing this with `weights=(1.0, 2.0)` (implemented
  during Task 9 of the plan) turned out to be far too weak: the same
  retrieval-rank-20-vs-5 scenario at weight 2.0 still has the mediocre-but-
  consistent candidate winning (0.0462 vs. 0.0453); the break-even weight for
  even a small 5-candidate example worked out to 16.25 by hand. Pushing the
  weight past roughly 15-40 (depending on `candidate_k`) instead makes the
  reranker's order win almost unconditionally, since RRF is rank-based (not
  magnitude-based) and a continuous cross-encoder score essentially never
  ties — at that point the pre-rerank/recency signal this whole re-fusion
  step exists to preserve stops mattering in practice. `_RERANK_RRF_WEIGHT =
  8.0` (revised from the plan's original `2.0`) is a deliberate middle point:
  it meaningfully corrects the fused order on a modest signal disagreement,
  while still letting a very consistent pre-rerank candidate win over the
  reranker's pick on a wide disagreement — treated as a legitimate hedge on
  strong signal conflict, not a bug.
- `_executor = ThreadPoolExecutor(max_workers=4, ...)` (`ui/backend/
  runtime.py:31`) — up to 4 runs execute concurrently, each capable of
  triggering a memory recall. A cross-encoder is a local model with real
  load cost; caching must be process-level (module scope), not per-
  `MemoryManager`/per-KB-instance, or every run pays full model-load latency.
- `KnowledgeBaseSpec` (`core/specification.py:145`) mirrors the loader's raw
  dict 1:1 for `local_folder`/`vector`; `to_raw()` only emits
  `embedding_model`/`score_threshold`/`cache_path` for `type: "vector"` so an
  architect setting them on `local_folder` can't hit a constructor
  `TypeError`. `_build_knowledge_base` (`core/loader.py:104`) pops
  `name`/`path`/`type` then splats the rest as kwargs into the KB class —
  new fields need no loader change beyond the `Spec` class and `to_raw()`,
  as long as both KB constructors accept the same new kwarg names.
- `bestteam/__init__.py` exports `KnowledgeBase`/`LocalFolderKnowledgeBase`/
  `VectorKnowledgeBase`/`Memory`/`MemoryManager`/`SqliteBM25Memory` etc., but
  **not** `resolve_embedding_model` or anything from `core/embeddings.py` —
  a customer who wants a custom embedding model imports `langchain_core.
  embeddings.Embeddings` directly and passes an instance. The new
  `Reranker`/`resolve_reranker` follow the same precedent: internal to
  `core/reranking.py`, not added to the public top-level export list.
- `.env.example` documents `BESTTEAM_MEMORY_DB`/`BESTTEAM_MEMORY_MODEL` but
  **not** `BESTTEAM_MEMORY_EMBEDDING_MODEL`/`BESTTEAM_MEMORY_QUERY_EXPANSION_
  MODEL` (verified — no matches). The advanced opt-in knobs are documented in
  `core/CLAUDE.md` only. This design follows that established precedent:
  `BESTTEAM_MEMORY_RERANK_MODEL`/`BESTTEAM_MEMORY_RERANK_CANDIDATE_K` are
  documented in `core/CLAUDE.md`, not added to `.env.example`.
- `pyproject.toml` optional extras: `tools-rag = ["rank-bm25>=0.2.2"]`,
  `tools-rag-vector = ["numpy>=1.24"]` (lines 20-21) — a new
  `tools-rerank` extra follows the same one-line-per-package convention.

## Approach

### 1. `core/reranking.py` (new module)

Mirrors `core/embeddings.py`'s shape:

```python
class Reranker(ABC):
    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        """Public entry point: validates, never raises for a shape/NaN
        problem the subclass caused (raises `_RerankScoringError`, a
        module-private RuntimeError subclass) so every caller can use one
        try/except regardless of which failure mode fired."""
        if not texts:
            return []
        raw = self._score(query, list(texts))
        if len(raw) != len(texts):
            raise _RerankScoringError(
                f"reranker returned {len(raw)} scores for {len(texts)} texts"
            )
        scores = [float(s) for s in raw]
        if any(not math.isfinite(s) for s in scores):
            raise _RerankScoringError("reranker returned a non-finite score")
        return scores

    @abstractmethod
    def _score(self, query: str, texts: List[str]) -> Sequence[float]: ...


class _FakeReranker(Reranker):
    """Deterministic, $0, no model download: scores by
    -abs(len(text) - len(query)) -- a signal deliberately unrelated to
    BM25/cosine, so a test can assert reranking actually changed the order
    rather than coincidentally reproducing retrieval order."""


class _CrossEncoderReranker(Reranker):
    """Wraps sentence_transformers.CrossEncoder(model_name)."""


_cache_lock = threading.Lock()
_reranker_cache: Dict[str, Reranker] = {}  # successful resolutions only
_MAX_RERANK_CANDIDATE_K = 100


def _resolve_candidate_k(candidate_k: Optional[int], top_k: int) -> int:
    """Shared by both KB `__init__`s and `MemoryManager.__init__`: `None`
    defaults to `top_k * 4`; the result is always clamped into `[top_k,
    _MAX_RERANK_CANDIDATE_K]`. Callers that want a hard-reject-on-bad-config
    style (KB, YAML-authored) call this only to get the default, then
    separately raise `ConfigurationError` if the caller-supplied value falls
    outside that range; callers that want clamp-not-raise (Memory, env-
    authored) just use this function's return value directly."""


def resolve_reranker(spec: Any) -> Reranker:
    """Accept a live Reranker, "fake:" (deterministic test double), or
    "cross-encoder:<model-name>" (sentence-transformers, process-cached).
    Raises ConfigurationError uniformly on a bad spec or missing dependency
    -- this function does NOT decide fail-hard vs fail-soft; every caller
    does (KB: let it propagate; MemoryManager: catch it). Successful
    cross-encoder loads are cached at module (process) scope, keyed by the
    exact spec string, guarded by a lock held only around the "check cache,
    else construct" critical section -- NOT around scoring calls, so
    concurrent recalls/queries against an already-loaded model aren't
    serialized. A failed load is never cached (v1 simplification: every
    call retries construction rather than distinguishing a permanent
    misconfiguration from a transient one -- see "Deferred" below)."""
```

Dependency: new `pyproject.toml` extra `tools-rerank = ["sentence-transformers>=2.2"]`.
Missing package -> `ConfigurationError` at first `"cross-encoder:..."`
resolution, same message shape as the `rank_bm25`/`numpy` guards.

### 2. Knowledge bases (`local_folder` + `vector`, both)

New shared helper in `core/knowledge_base.py` (both KB classes already import
from each other's module; `vector_knowledge_base.py` already imports
`_load_document_chunks`/`_validate_chunk_params` from `knowledge_base.py`):

```python
def _rerank_candidates(
    query: str,
    candidates: List["tuple[float, _Chunk]"],  # already sorted by retrieval score, len <= candidate_k
    reranker: Optional[Reranker],
    top_k: int,
) -> List["tuple[float, _Chunk]"]:
    """Never mutates `candidates`. Empty input or no reranker configured is
    a pure slice, no model call. Any exception during scoring (including a
    `_RerankScoringError` contract violation) falls back to the pre-rerank
    `candidates[:top_k]` slice, logged as a warning -- rerank is a quality
    layer, never a reason the tool call itself fails."""
    if reranker is None or not candidates:
        return candidates[:top_k]
    try:
        rerank_scores = reranker.score(query, [chunk.text for _score, chunk in candidates])
    except Exception:
        _logger.warning("Rerank failed; falling back to retrieval order", exc_info=True)
        return candidates[:top_k]
    # Stable: ties keep the original (retrieval-order) index as the tiebreak.
    order = sorted(range(len(candidates)), key=lambda i: (-rerank_scores[i], i))
    return [candidates[i] for i in order[:top_k]]
```

Both `LocalFolderKnowledgeBase.__init__` and `VectorKnowledgeBase.__init__`
gain:
- `rerank_model: Any = None` — **eager** resolution: `self._reranker =
  resolve_reranker(rerank_model) if rerank_model is not None else None`,
  same statement shape as `self._embeddings = resolve_embedding_model(...)`
  in `VectorKnowledgeBase.__init__` (mirrors `embedding_model`: a bad
  spec/missing dependency raises `ConfigurationError` at workflow-load time,
  loud and immediate, matching every other KB dependency guard).
- `candidate_k: Optional[int] = None` — ignored when `rerank_model` is unset.
  When set, an explicit value is validated (`candidate_k >= top_k` and
  `candidate_k <= _MAX_RERANK_CANDIDATE_K`, else `ConfigurationError` —
  mirrors `_validate_chunk_params`'s "reject bad config at load" style, since
  this is YAML-authored config, not an env knob); `None` defaults to
  `_resolve_candidate_k(None, top_k)` (`top_k * 4`, clamped).

`query()` changes (both classes): take `candidate_k` candidates instead of
`top_k` from the existing BM25/cosine ranking (for `vector`,
`score_threshold` filtering still applies to the candidate_k pool at the
existing point, using the original cosine score — unaffected by rerank), then
`_rerank_candidates(query, candidates, self._reranker, top_k)` before
formatting. `rerank_model=None` (default): behavior is byte-for-byte
unchanged — `_rerank_candidates` returns `candidates[:top_k]` directly, the
existing single-slice behavior.

`KnowledgeBaseSpec` (`core/specification.py`) gains `rerank_model:
Optional[str] = None` and `candidate_k: Optional[int] = None`, emitted for
**both** `local_folder` and `vector` in `to_raw()` (unlike
`embedding_model`/`score_threshold`/`cache_path`, which are vector-only —
rerank applies to both KB types, so no type-gating needed). No `loader.py`
change beyond that: `_build_knowledge_base` already splats extra spec keys as
constructor kwargs.

### 3. Memory (`MemoryManager`, `core/memory.py`)

`SqliteBM25Memory` is **not** touched. `MemoryManager` gains:

```python
def __init__(self, ..., rerank_model: Any = None, rerank_candidate_k: Optional[int] = None):
    self.rerank_model = rerank_model
    # Same default-derivation as the KB side (top_k * 4) when unset, then
    # clamped to [top_k, _MAX_RERANK_CANDIDATE_K] -- one shared helper,
    # `_resolve_candidate_k(candidate_k, top_k)` in `core/reranking.py`,
    # used by both KB `__init__`s and here so the default/clamp logic isn't
    # duplicated three times.
    self.rerank_candidate_k = _resolve_candidate_k(rerank_candidate_k, top_k)
    self._reranker: Optional[Reranker] = None
    self._reranker_resolve_attempted = False
```

`_get_reranker()` — lazy, resolved on first use, mirrors `_expand_query`'s
never-raises posture:

```python
def _get_reranker(self) -> Optional[Reranker]:
    if not self._reranker_resolve_attempted:
        self._reranker_resolve_attempted = True
        if self.rerank_model is not None:
            try:
                self._reranker = resolve_reranker(self.rerank_model)
            except Exception:
                _logger.warning("Memory rerank disabled: could not resolve reranker", exc_info=True)
    return self._reranker
```

Cached on `self` for the run's lifetime (avoids re-attempting a known-bad
spec twice within one recall's two scoped searches); `resolve_reranker`'s own
module-level cache (§1) is what avoids reloading a *successful* model across
runs.

`_fused_search` rewrite — extends `_reciprocal_rank_fusion` to accept
optional per-list weights (default `None` = today's unweighted behavior, so
the two existing call sites are untouched):

```python
def _reciprocal_rank_fusion(*ranked_id_lists, k=60, weights=None):
    weights = weights or [1.0] * len(ranked_id_lists)
    scores = {}
    for weight, ranked_ids in zip(weights, ranked_id_lists):
        for rank, record_id in enumerate(ranked_ids, start=1):
            scores[record_id] = scores.get(record_id, 0.0) + weight / (k + rank)
    return scores

_RERANK_RRF_WEIGHT = 8.0  # internal constant; revisit once a Ragas-style eval baseline exists

def _fused_search(self, user_id, queries, types, **kwargs):
    top_k = kwargs.get("top_k", self.top_k)
    reranker = self._get_reranker()
    fetch_k = self.rerank_candidate_k if reranker is not None else top_k

    ranked_lists, by_id = [], {}
    for q in queries:
        results = self.store.search(user_id, q, types=types, **{**kwargs, "top_k": fetch_k})
        ranked_lists.append([r.id for r in results])
        for r in results:
            by_id.setdefault(r.id, r)
    pre_rerank_ranked_ids = [
        rid for rid, _ in sorted(
            _reciprocal_rank_fusion(*ranked_lists).items(), key=lambda p: p[1], reverse=True
        )
    ]
    if reranker is None:
        return [by_id[rid] for rid in pre_rerank_ranked_ids[:top_k]]

    candidate_ids = pre_rerank_ranked_ids[: self.rerank_candidate_k]
    candidates = [by_id[rid] for rid in candidate_ids]
    try:
        scores = reranker.score(queries[0], [c.content for c in candidates])  # literal query ONLY, never an expansion
        rerank_ranked_ids = [
            candidate_ids[i] for i, _ in sorted(enumerate(scores), key=lambda p: (-p[1], p[0]))
        ]
    except Exception:
        _logger.warning("Memory rerank failed; using pre-rerank order", exc_info=True)
        return candidates[:top_k]

    final = _reciprocal_rank_fusion(
        candidate_ids, rerank_ranked_ids, weights=(1.0, _RERANK_RRF_WEIGHT)
    )
    final_ranked = sorted(final.items(), key=lambda p: p[1], reverse=True)
    return [by_id[rid] for rid, _ in final_ranked[:top_k]]
```

This removes the old `len(queries) == 1` early return (see "Key facts" —
verified equivalent) and applies uniformly whether or not query expansion is
active. Runs independently per scope (`recall()`'s two `_fused_search` calls,
unchanged call sites). `rerank_model=None` (default): `_get_reranker()`
returns `None`, the function takes the `pre_rerank_ranked_ids[:top_k]`
branch — byte-for-byte today's behavior (module-level RRF is unweighted by
default, identical output).

Env config (`ui/backend/runtime.py::_make_memory`): `BESTTEAM_MEMORY_RERANK_
MODEL` (same spec convention), `BESTTEAM_MEMORY_RERANK_CANDIDATE_K`
(`_env_int`, default `None` → `MemoryManager.__init__` derives `top_k * 4`
via `_resolve_candidate_k`, same default as the KB side). Unlike the KB side,
an out-of-range explicit value is **clamped, not rejected** —
`_resolve_candidate_k` itself is clamp-only (no raising); `ConfigurationError`
on a bad `candidate_k` is a KB-side choice layered on top of the shared
helper, not something `_resolve_candidate_k` does itself. This mirrors
`recall_max_candidates`/`max_episodic_per_user`'s existing env-knob clamping
convention, since this is a runtime env value, not authored YAML.

### 4. Error-handling summary (both surfaces)

| Failure | KB | Memory |
|---|---|---|
| Bad spec / missing dependency, at construction | `ConfigurationError`, fail-hard (workflow load fails) | Logged warning, rerank silently disabled for the run's lifetime (`_get_reranker` returns `None` from then on) |
| Cross-encoder inference error, at query/recall time | Logged warning, fall back to pre-rerank `candidate_k` order, still return `top_k` | Logged warning, fall back to `pre_rerank_ranked_ids`/`candidates[:top_k]`, recall still succeeds |
| `candidate_k` invalid | `ConfigurationError` at load (YAML) | Clamped/raised to a valid value (env) |

### 5. Deferred (explicitly out of scope for v1)

- **Differentiated failure caching** (permanently cache a deterministic
  `ConfigurationError`, short-TTL-retry a possibly-transient one like a model
  download failure). v1 caches only successful resolutions; every call for
  an unresolved/failed spec retries construction. Correct in all cases,
  just not optimal for a permanently-misconfigured spec (bounded cost: one
  retry per `MemoryManager` construction, i.e. per run, since `self.
  _reranker_resolve_attempted` prevents a second retry within the same run;
  the warning log makes a stuck-bad config visible to fix).
- **Inference-time locking** across concurrent cross-encoder calls. Not added:
  `_executor`'s 4 worker threads mean a global inference lock would make
  rerank a whole-backend bottleneck (it's on the hot path before every
  configured agent run), for a benefit (avoiding hypothetical GPU memory
  contention) with no evidence of need yet. CPU inference is safe under
  PyTorch's own internal threading; a GPU deployment that hits contention is
  a future hardening item, not a v1 blocker (query-time failures already
  degrade to pre-rerank order, so contention-driven errors don't corrupt
  results, just skip the rerank benefit for that call).
- **Configurable `_RERANK_RRF_WEIGHT`.** Internal constant for v1; revisit
  once there's eval data (e.g. a small Ragas-based harness) to tune against,
  rather than guessing a customer-facing knob's right default.
- **KB rerank score exposed to the caller/agent.** `query()`'s formatted
  string shape is unchanged (`"N. [source: ...]\n<text>"`) — no rerank score
  surfaced, matching how retrieval scores aren't surfaced today either.
- **`.env.example` entries** for the two new Memory env vars — follows the
  existing precedent that `BESTTEAM_MEMORY_EMBEDDING_MODEL`/`BESTTEAM_MEMORY_
  QUERY_EXPANSION_MODEL` aren't there either; `core/CLAUDE.md` is the
  documented source of truth for these advanced opt-in knobs.
- **Public export** of `Reranker`/`resolve_reranker` from `bestteam/
  __init__.py` — internal to `core/reranking.py`, matching
  `core/embeddings.py`'s precedent.

## Files

- `src/bestteam/core/reranking.py` (new) — `Reranker` ABC + `score()`
  template method, `_FakeReranker`, `_CrossEncoderReranker`,
  `resolve_reranker()`, process-level cache + lock, `_RerankScoringError`,
  shared `_resolve_candidate_k()` default/clamp helper.
- `src/bestteam/core/knowledge_base.py` — `_rerank_candidates()` shared
  helper; `LocalFolderKnowledgeBase.__init__`/`query()`.
- `src/bestteam/core/vector_knowledge_base.py` —
  `VectorKnowledgeBase.__init__`/`query()` (reuses `_rerank_candidates` from
  `knowledge_base.py`, same import pattern as `_load_document_chunks`).
- `src/bestteam/core/specification.py` — `KnowledgeBaseSpec.rerank_model`/
  `.candidate_k`, `to_raw()`.
- `src/bestteam/core/memory.py` — `_reciprocal_rank_fusion(..., weights=)`;
  `MemoryManager.__init__(rerank_model=, rerank_candidate_k=)`,
  `_get_reranker()`, `_fused_search()` rewrite (drop the `len(queries)==1`
  special case).
- `ui/backend/runtime.py` — `_make_memory()` reads `BESTTEAM_MEMORY_RERANK_
  MODEL`/`BESTTEAM_MEMORY_RERANK_CANDIDATE_K`, passes into `MemoryManager`.
- `pyproject.toml` — new `tools-rerank = ["sentence-transformers>=2.2"]` extra.
- `src/bestteam/core/CLAUDE.md` — document KB rerank (next to the existing
  "Known limitation: vector knowledge base retrieval is single-stage"
  paragraph — this closes part of it) and Memory rerank (next to the
  existing hybrid-recall/query-expansion write-up).
- `ui/backend/CLAUDE.md` — document the two new env vars next to the
  existing `BESTTEAM_MEMORY_QUERY_EXPANSION_*` paragraph.
- Tests (new + extended):
  - `tests/test_knowledge_base.py` (or wherever `LocalFolderKnowledgeBase`
    is covered) — rerank on/off byte-identical-when-unset regression;
    `candidate_k` truncation; `fake:` reranker actually changes order vs.
    plain BM25 order; inference-failure-falls-back-to-retrieval-order;
    `candidate_k < top_k` rejected; missing `sentence-transformers` raises
    `ConfigurationError`.
  - `tests/test_vector_knowledge_base.py` — same matrix, plus
    `score_threshold` still applies pre-rerank.
  - `tests/test_memory.py` / `tests/test_memory_backend.py` — `_reciprocal_
    rank_fusion(weights=)` unweighted-default regression; `_fused_search`
    single-query path now goes through the same code as multi-query
    (equivalence check); rerank scores each expanded-query scenario against
    the literal query only; each expanded query variant fetches
    `rerank_candidate_k` (not `top_k`) from the store; fused pool is capped
    to `rerank_candidate_k` before scoring (bound the cross-encoder call
    count regardless of `query_expansion_count`); weighted RRF preserves
    recency ordering for a near-tie while letting a clear reranker winner
    surface; reranker-resolution failure disables rerank for the rest of
    that `MemoryManager`'s lifetime without raising; two scopes (`semantic`;
    `episodic`/`procedural`) each independently respect `top_k`;
    `rerank_model=None` byte-identical regression.
  - `tests/test_memory_integration.py` / backend runtime test — env vars
    thread through `_make_memory` into `MemoryManager`; a successful
    `"cross-encoder:..."` (or, in CI, a `"fake:"` spec used the same way)
    resolution is reused across two sequential `_make_memory()` calls
    (asserts the module-level cache, not a fresh model load each time).
  - `core/reranking.py`'s own unit tests: `score()` rejects a length
    mismatch / NaN / inf from a misbehaving subclass; empty-candidate list
    never invokes `_score`; `resolve_reranker` caches success across calls
    with the same spec (identity check) and does not cache a failure (two
    calls to a permanently-bad spec both raise, not memoized silently).

## Verification

- Full suite: `.\.venv\Scripts\python.exe -m pytest`.
- KB: a `local_folder` and a `vector` KB, each with a `fake:` reranker and a
  handful of documents where BM25/cosine order and length-based fake-rerank
  order provably differ — assert the formatted output order changes when
  `rerank_model` is set and is unchanged when it's not.
- Memory: two `MemoryManager`s sharing a store, one with `rerank_model` set
  (`fake:`) and one without, recalling the same query against records
  engineered so retrieval order and fake-rerank order differ — assert the
  `RecallResult` content order differs between them, and that
  `query_expansion_count` doesn't change how many candidates reach the
  cross-encoder (always capped at `rerank_candidate_k`).
- End-to-end: run one of the existing `fake:`-model demo workflows
  (`ui/backend/workflows/vector_knowledge_base_demo.yaml`) with
  `rerank_model: "fake:"` added to its KB config, via `bestteam run`, and
  confirm it completes with reranked results in the tool output.
