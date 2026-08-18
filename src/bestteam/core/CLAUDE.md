# bestteam — `src/bestteam/core/` (Specification, Requirements, knowledge bases)

Directory-scoped notes for the Team Builder's structured-output stages and
the knowledge-base implementations. See the root `CLAUDE.md` for project
overview, architecture, and commands.

## Specification and Requirements

- **Specification = loader schema + wizard-only friendly fields**
  (`core/specification.py`): `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`/
  `WorkflowSpec`/`Specification` are pydantic models that mirror the YAML
  loader's raw dict (see `core/loader.py::_build_workflow`), plus
  presentation-only fields (`display_name`, `friendly_description`) that
  `to_raw()` strips before validation. `validate_specification()` compiles
  the stripped dict via `_build_workflow()` and raises `ConfigurationError`
  on an invalid design. `generate_specification()` drives a "Solution
  Architect" model via `with_structured_output(Specification)` and
  self-corrects on `ConfigurationError` (up to `max_attempts`) — the
  Specification-stage engine described in
  `docs/team_builder_methodology.md`.
- **Requirements = Business Analyst's structured output** (`core/requirements.py`):
  `Requirements` (summary/pain_points/goals/success_criteria/constraints/
  clarifying_questions) is the Requirements-stage counterpart to
  `Specification`. `generate_requirements(model, intent_text, as_is_text,
  feedback=...)` calls `model.with_structured_output(Requirements)` —
  no `_build_workflow` validation applies at this stage (it's a plain-language
  summary, not yet a team design). `Requirements.to_prompt()` renders it as
  text for the Solution Architect's `requirements` argument.

## Skills (`SkillSpec`, `AgentSpec.skills`)

`SkillSpec` (`core/specification.py`) is a reusable instruction document plus
the tools it depends on: `{name, description, instructions, tools}`. An
`AgentSpec` can reference skills by name via `skills: List[str]` -- a real
loader-level field (unlike `display_name`/`friendly_description`, `to_raw()`
keeps it).

`core/loader.py::_build_workflow` resolves `skills:` via an optional
`extra_skills: Dict[str, SkillSpec]` parameter (mirrors `extra_tools`;
`load_workflow(..., skills=[...])` builds it by `.name`). For each agent:

- Each skill name is looked up in `extra_skills`; an unknown name raises
  `ConfigurationError("Unknown skill '<name>'. Available skills: <...>")`.
- The skill's `tools` are appended to the agent's own `tools` (agent's tools
  first), de-duplicated preserving order, then resolved through the same
  `tool_lookup` as ordinary `tools:` -- an unresolvable name raises the
  existing `"Unknown tool '<name>'. Available tools: <...>"` error.
- The skill's `instructions` are appended to the agent's `backstory`, one per
  skill in `skills:` order, joined by `"\n\n"`.

`validate_specification()`/`generate_specification()` accept the same
`extra_skills` parameter, passed through to `_build_workflow()`.

## Knowledge bases (`core/knowledge_base.py`, `core/vector_knowledge_base.py`, `core/hybrid_knowledge_base.py`)

The most common client request is "connect our agents to the client's
knowledge base." The loader supports three knowledge base `type:`s, all
backed by a folder of documents (`tools.parse_file` + chunking):

- `local_folder` (default, `core/knowledge_base.py`): indexes chunks in
  memory with **BM25 keyword search** (`rank-bm25`). No API key, no vector
  store, no persistence. Best for the common case — a handful to a couple
  dozen documents.
- `vector` (`core/vector_knowledge_base.py`): embeds each chunk and ranks by
  **cosine similarity** for semantic search (e.g. a query about "refunds"
  matches a chunk that says "money back" with no shared keywords). Query
  expansion and reranking are both available, opt-in (see "Query expansion"
  and "Known limitations: knowledge base storage, chunking, and reranking"
  below).
- `hybrid` (`core/hybrid_knowledge_base.py`): indexes chunks with BOTH BM25
  and embeddings, fusing the two rankings via Reciprocal Rank Fusion so a
  chunk either method alone would miss (e.g. a semantically relevant chunk
  with zero keyword overlap with the query) can still surface. Requires
  both `pip install 'bestteam[tools-rag,tools-rag-vector]'`. See
  `ui/backend/workflows/hybrid_knowledge_base_demo.yaml`. The two legs are
  equal-weighted in the RRF formula itself, but `_rrf_retrieve` builds its
  ranked lists BM25-leg-before-vector-leg, and Python's stable sort keeps
  insertion order on a tie -- so a tied fused score (both legs agreeing
  exactly, or a shallow `fetch_k`/`top_k` where each leg contributes only
  its own top hit) resolves to BM25's pick. This is a side effect of
  fusion order, not a deliberate BM25-priority policy, and is most visible
  at small `top_k` with no reranker configured.

All three expose the resulting knowledge base to agents as an ordinary tool
(named after the KB), so it slots into the existing `tools:` / `REGISTRY`
mechanism with no `LangGraphAdapter` changes — `query()` returns the same
formatted `"...results for: <query>\n\n1. [source: ...]\n<text>..."` / `"No
results found..."` string shape regardless of type.

**YAML usage — `local_folder`:**
```yaml
knowledge_bases:
  - name: product_docs
    path: ./docs/product   # relative to the workflow YAML's directory
    # optional: chunk_size (default 1000), chunk_overlap (default 100), top_k (default 5)

agents:
  - name: support_agent
    role: Support Specialist
    goal: Answer customer questions using the product documentation
    model: "openai:gpt-4o-mini"
    tools: [product_docs, calculator]
```

Requires `pip install 'bestteam[tools-rag]'`. See
`ui/backend/workflows/knowledge_base_demo.yaml` for a runnable example.

**YAML usage — `vector`:**
```yaml
knowledge_bases:
  - name: product_docs
    type: vector
    path: ./docs/product
    embedding_model: "openai:text-embedding-3-small"  # or "fake:<dim>" for $0 dry runs
    # optional: chunk_size (default 1000), chunk_overlap (default 100), top_k (default 5)
    # optional: score_threshold — cosine-similarity cutoff in [-1, 1]; if set and no
    #           chunk meets it, query() returns the same "No results found" message
    # optional: cache_path — JSON file persisting per-chunk embeddings (keyed by a
    #           sha256 of the embedding-model spec + chunk text) across runs, so
    #           load_workflow() doesn't re-embed unchanged chunks every time. Only
    #           applies when embedding_model is a string spec; if you pass a live
    #           Embeddings instance, caching is skipped with a warning. Resolved
    #           relative to the workflow YAML's directory, like `path`.
    cache_path: ./.bestteam_cache/product_docs.json
```

`embedding_model` is resolved like `Agent.model`: a `langchain_core.embeddings.Embeddings`
instance is used as-is, `"fake:<dim>"` (dim optional, default 32) gives a $0
deterministic embedding for dry runs/tests, and other provider strings (e.g.
`"openai:..."`) are resolved via `langchain.embeddings.init_embeddings`
(requires `pip install langchain`). Requires `pip install 'bestteam[tools-rag-vector]'`
(numpy). See `ui/backend/workflows/vector_knowledge_base_demo.yaml` for a $0
dry-run example using `"fake:"` specs, or
`ui/backend/workflows/vector_knowledge_base_demo_live.yaml` for the same
workflow wired to real OpenAI embeddings + chat model
(`text-embedding-3-small` + `gpt-4o-mini`), which demonstrates true semantic
retrieval (e.g. matching "money back" queries to a "refund" policy doc with
no shared keywords). The live variant requires `OPENAI_API_KEY`.

**YAML usage — `hybrid`:**
```yaml
knowledge_bases:
  - name: product_docs
    type: hybrid
    path: ./docs/product
    embedding_model: "openai:text-embedding-3-small"  # or "fake:<dim>" for $0 dry runs
    # optional: chunk_size (default 1000), chunk_overlap (default 100), top_k (default 5)
    # optional: score_threshold — cosine-similarity cutoff in [-1, 1], but it filters
    #           ONLY the vector leg: a chunk below the cutoff can still surface via a
    #           BM25 keyword match, so unlike `vector`, setting this does not
    #           guarantee "No results found" when no chunk meets it.
    # optional: cache_path — same per-chunk embedding cache as `vector` (see above)
    cache_path: ./.bestteam_cache/product_docs.json
```

Requires BOTH `pip install 'bestteam[tools-rag,tools-rag-vector]'` extras
(BM25 + embeddings). See `ui/backend/workflows/hybrid_knowledge_base_demo.yaml`
for a $0 dry-run example using `"fake:"` specs.

## Query expansion (opt-in, all three KB types)

All three KB types accept `query_expansion_model`/`query_expansion_count`
(same spec-string convention and MultiQueryRetriever-style behavior as
Memory's `query_expansion_model` -- see "Per-user memory", below): when set,
`query()` rewrites the query into up to `query_expansion_count` alternative
phrasings via one LLM call, searches with the literal query plus every
alternative, and fuses the per-variant ranked results with Reciprocal Rank
Fusion (`core/fusion.py`, shared with Memory) before slicing to `top_k`.
Unset (the default) -> `query()` is byte-for-byte unchanged. A bad spec /
invoke error / unparseable response degrades to searching the literal query
alone -- a query never fails because expansion failed. **This call's cost is
unmetered**: KB tools run inside the agent's generic tool-calling loop
(`adapters/langgraph_adapter.py`), which has no hook to report a nested LLM
call's token usage back to the backend (unlike Memory's recall, which runs
at the `Workflow.stream()` orchestration layer) -- the same pre-existing gap
`VectorKnowledgeBase`'s embedding calls already have. See
`docs/superpowers/specs/2026-08-15-kb-hybrid-retrieval-design.md`.

## Known limitations: knowledge base storage, chunking, and reranking

`VectorKnowledgeBase` is also in-memory plus an optional JSON embedding
cache (`cache_path`) — no external vector store (Chroma/FAISS/Pinecone) and
no hierarchical/"small-to-big" indexing. Without `cache_path`, every
workflow load re-embeds all chunks (real embedding APIs incur cost/latency
on each run). There's no DMS connector (SharePoint/Confluence/Google Drive)
for any of the three knowledge base types.

**A mis-encoded plain-text document is still skipped, not ingested.** The
Phase 4b bytes refactor shares `file_parser._decode_text` between knowledge
bases and email attachments, which pulled in opposite directions: an attachment
must never fail a customer's run (a sender can name anything `.txt`), while a
knowledge base is better served by a warning than by chunks full of U+FFFD that
nobody can search and nobody was told about. The decoder therefore takes
`lenient`, and only the attachment path turns it on. `parse_file` keeps strict
decoding, so `_load_document_chunks` still skips a non-UTF-8 document with a
warning exactly as it did before.
Suffix support is also shared now: `_SUPPORTED_SUFFIXES` is an alias of
`file_parser.SUPPORTED_SUFFIXES`, so a suffix added to `parse_bytes` is
discovered by folder scanning automatically.

**Chunking is format-aware, not hierarchical.** `_chunk_text` (shared by all
three KB types) now splits on the document's own structure — Markdown heading
boundaries, XML element boundaries (via the renderer's indentation), and a
generic paragraph/sentence/word fallback (with CJK sentence terminators
`。！？`) — replacing the old fixed-offset character slicing. This closes the
"chunking is naive" half of the gap above; small-to-big multi-level retrieval
is still the remaining, unaddressed half. Overlap between chunks is still a
raw character-slice of the previous chunk's tail (not structure-aware), and
can drop to zero when greedy packing fills a piece to exactly `chunk_size`
with no headroom left to borrow from.

**Reranking (opt-in, all three KB types, `core/reranking.py`).**
`LocalFolderKnowledgeBase`, `VectorKnowledgeBase`, and `HybridKnowledgeBase`
all accept `rerank_model` (a spec string or a live `Reranker` instance) and
`candidate_k`. When `rerank_model` is set, `query()` over-fetches
`candidate_k` results from the existing BM25/cosine/fused ranking (default
`top_k * 4`, clamped to `[top_k, 100]`) instead of just `top_k`, scores each
candidate against the query with the reranker, and returns the top `top_k`
by rerank score (`_rerank_candidates()`, a shared helper in
`knowledge_base.py` that `vector_knowledge_base.py` and
`hybrid_knowledge_base.py` both reuse). `rerank_model`
follows the same spec-string convention as `embedding_model`: `"fake:"`
(deterministic, $0, scores by negative length-distance to the query — for
tests/dry runs) or `"cross-encoder:<model-name>"` (a real local
`sentence_transformers.CrossEncoder`, cached at process scope by model
name); requires `pip install 'bestteam[tools-rerank]'`. Unset (the default,
all three types) → `query()` is byte-for-byte unchanged (no over-fetch, no rerank
call). A bad reranker spec or an out-of-`[top_k, 100]` `candidate_k` raises
`ConfigurationError` at construction (fail-hard, like a bad `chunk_size`); a
rerank-time failure (a cross-encoder inference error) logs a warning and
falls back to the pre-rerank `candidate_k`-ordered slice — rerank is a
quality layer, never a reason a query itself fails. See
`docs/superpowers/specs/2026-08-12-pluggable-rerank-design.md`.

**Backend consumption: `from_chunks(...)` alternate constructors.** All
three `KnowledgeBase` classes (`LocalFolderKnowledgeBase`,
`VectorKnowledgeBase`, `HybridKnowledgeBase`) now also expose a `from_chunks`
classmethod that builds directly from a list of pre-parsed `_Chunk`s (and,
for `vector`/`hybrid`, pre-computed embedding vectors), skipping the
file-parsing/chunking pipeline entirely. This is purely a backend
consumption pattern — an upload-managed KB served by `ui/backend/` persists
its chunks (and embeddings) once, in the database
(`knowledge_ingestion_jobs`/`knowledge_documents`/`knowledge_chunks`, see
`ui/backend/db/CLAUDE.md`), and reconstructs the in-memory KB from those rows
on every subsequent load instead of re-parsing files each time
(`ui/backend/knowledge_bases.py::resolve_knowledge_base`). The SDK core
itself remains entirely file-based and DB-free: `from_chunks` takes plain
Python data (chunks/vectors), never a database session or any backend
concept, and the ordinary `__init__` path (parse files from `path` on every
load) is unchanged and still the only path the CLI/SDK-direct/YAML-loader
callers use. See
`docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`.

## Per-user memory (`core/memory.py`, `core/text_tokenize.py`)

`core/memory.py` implements a per-user memory system so the platform remembers
an end-user across sessions. It shares the CJK-aware tokenizer with the
knowledge base — both now import `tokenize`/`significant_terms` from
`core/text_tokenize.py` (extracted so the BM25 logic lives in one place).

- **`Memory` ABC** — `add`/`search`/`all`/`delete` over `MemoryRecord`
  (`id, user_id, type, content, metadata, created_at`). The old
  `remember`/`recall` key-value stub and `InMemoryStore` were removed (unused).
  The admin/management operations (`user_ids`, `user_summaries`, `delete_user`,
  `close`) are **not** on the ABC — they're concrete on `SqliteBM25Memory` only,
  so a third-party store implementing just the four core methods still works.
- **`SqliteBM25Memory`** — the default store: stdlib `sqlite3` persistence
  (own connection + DB file, no SQLAlchemy) + `rank-bm25` keyword search over
  `content`, using the same overlap-then-score ranking as
  `LocalFolderKnowledgeBase.query`. Requires `bestteam[tools-rag]`; raises
  `ConfigurationError` otherwise (mirrors the KB). Adds management helpers used
  by the admin API: `user_ids()`, `user_summaries()` (per-type counts via one
  `GROUP BY`), `all(..., limit=)`, `delete_user()`, and `close()`.
  `search(..., max_candidates=N)` caps how many most-recent records get
  tokenized/BM25-indexed, so an admin search over a large store does bounded
  work (the admin API sets it). `max_candidates=None` (the default) keeps the
  full-store scan that per-run **recall** uses by design — the documented
  single-stage BM25 tradeoff, unchanged.
- **`MemoryManager`** — the execution-path glue. `recall_preamble(user_id,
  query)` formats the top search hits into a system-prompt block (`""` if none);
  `record_run(user_id, input, output)` always writes one **episodic** record
  and, when `extraction_model` is set, makes one LLM call (`_resolve_model`, so
  `fake:` specs are $0) to also write **semantic** (facts) and **procedural**
  (how-handled) records.

The four memory types: **working** = the live `_TeamState` (not stored here);
**episodic**/**semantic**/**procedural** = `MemoryRecord` rows tagged by `type`.

**Instrumentation (SP-3).** `record_run` returns a `MemoryOutcome(recorded,
extraction_usage)` and `recall` a `RecallResult(preamble, count)` (`recall_preamble`
is a thin string wrapper, unchanged). The extraction call's `usage_metadata` is
captured as a `{model, input_tokens, output_tokens}` entry (mirroring the adapter),
so `Workflow.stream` can emit it on a `memory_recorded` TraceEvent and the backend
meters it (`agent="memory:extraction"`, M-04) — the SDK never touches the backend
DB. `Workflow.stream` also emits `memory_recalled` (`data`=count, 0 included) and,
on a recall/record failure, a sanitized `memory_failed` (`data`=`"recall"`/
`"record"`) for observability (M-05). Recall events precede the agents; **recording
events are emitted AFTER `run_completed`** (see the ordering note below), so a
slow/hung extraction can't wedge the run. Recording stays best-effort (a failure
yields `memory_failed`, never `run_failed`). `Workflow.run`
surfaces the same instrumentation on `WorkflowResult` for parity with `stream()`:
`.memory` (recording `MemoryOutcome`; `None`=disabled, `ok=False`=recording
failure) and `.recall` (`RecallResult`; `None`=disabled, `count`=records drawn,
`ok=False`=recall failure). Provenance is stamped into each record's
`metadata={run_id, workflow_version_id}` (M-06), bound by `runtime._make_memory`.
Extraction usage is captured immediately after the model call, so a failure still
bills the spend; the usage rides exactly one emitted event (`memory_recorded`, or
`memory_failed` when *every* write failed) so it's metered once even on total
failure. Each extracted write is isolated (`MemoryOutcome.ok=False` on any
partial/total failure → a `memory_failed` event) so one bad write can't skip the
rest. **Recording (including the extraction LLM call) runs AFTER the terminal
`run_completed` event** (`Workflow.stream`), so a slow/hung extraction can never
delay or wedge a finished run — no timeout machinery needed. The backend still
meters/records these post-terminal events because `run_in_background` drains the
whole event stream; a live WebSocket that stops on `run_completed` just won't
*display* them (no durable billing/provenance data depends on that), and
`registry.publish` tolerates a run evicted between the terminal event and a late
memory event. On the backend, usage persistence goes through `_safe_record_usage`,
which isolates a `usage_records` write failure from run status. See
`docs/MEMORY_REVIEW_TRIAGE.md`.

`Workflow.run/stream(input, *, user_id=None, memory=None)` recall a preamble
(threaded through the adapter's `_initial_state` → `_TeamState.memory_preamble`
→ each agent's `extra_system_prompt`, so the cached compiled graph is reused
with no recompile) and record the run afterward. Both kwargs default to None →
current behavior unchanged. The backend enables it per worker thread from
`BESTTEAM_MEMORY_DB` (opt-in path) + `BESTTEAM_MEMORY_MODEL` (opt-in
extraction) — see `ui/backend/runtime.py::_make_memory`.

### Known limitations (per-user memory)

- **Quality & scale (SP-4).** Extraction dedups **exact** semantic/procedural
  content on write via `SqliteBM25Memory.add_if_absent` (M-08) — an atomic,
  **per-type** `INSERT ... WHERE NOT EXISTS` keyed by `(user_id, type, content,
  org-scope)`. Per-type means a semantic and a procedural row with the same text
  don't collide; atomic under SQLite's write serialization means two concurrent
  connections can't both insert. **Semantic near-duplicate/update resolution**
  is also implemented: before writing, `_extract_and_store` fetches the user's
  existing `semantic` memories most relevant to the run (BM25, capped at 20
  candidates, same org/principal scope) and shows them to the extraction model
  with their ids; each extracted fact now carries an `action`
  (`"add"`/`"update"`/`"noop"`) instead of being a bare string. `"update"`
  deletes the referenced candidate before inserting the new fact (write-once
  storage is otherwise unchanged — no soft-delete/history); `store.delete` is
  only ever called for an id this call's own candidate search returned, so a
  hallucinated `replaces_id` can never delete a real record — it falls back to
  a plain add instead, as does a missing/unrecognized `action`. `"noop"` skips
  the write. A candidate-fetch failure degrades to "no candidates" (plain add)
  rather than breaking extraction. This covers **semantic only** —
  `procedural`'s free-text shape makes near-duplicate judgment unreliable, so
  procedural consolidation is still deferred, as is cross-run concurrency (two
  simultaneous runs both updating the same old record) and any effectiveness
  measurement of the reconciliation itself (M-13). See
  `docs/MEMORY_REVIEW_TRIAGE.md`. Recall bounds its scan to
  the most-recent `recall_max_candidates` records (backend default 1000 via
  `BESTTEAM_MEMORY_RECALL_MAX_CANDIDATES`, M-09; clamped to SQLite's int range so a
  fat-fingered value can't `OverflowError` the `LIMIT`). Composite `(user_id,
  created_at)` / `(org_id, user_id, created_at)` indexes make the recall filter+sort
  index-covered (no temp-B-tree sort), and `(org_id, user_id, type, content)`
  makes the dedup existence check a seek (not a scan of the user's episodic-inflated
  history). Extraction routes through `add_if_absent` for dedup **unless** the store
  overrides `add()` with custom policy (encryption/audit/…) without adopting
  `add_if_absent` — then `add()` is honored for every write (dedup steps aside), so
  a pre-SP-4 subclass keeps intercepting semantic/procedural writes. Episodic
  **retention** is opt-in (`BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER`, M-07): when
  set, `record_run` prunes the oldest episodic rows beyond the cap
  (`prune_user_type`), scoped to a **single** org — `org_id=None` means
  `org_id IS NULL` (an org-less manager's own rows), NEVER all-orgs, so retention
  can't delete another org's history. Semantic/procedural are spared; unbounded by
  default (pruning is destructive). Age-based TTL, per-org quotas, and a background
  sweep are deferred. All three degrade gracefully for a custom store (best-effort
  concrete-store extensions: dedup falls back to a plain `add`, prune is skipped).
- Memory is best-effort on **both** sides of a run: `record_run` (write) and
  `recall_preamble` (read, via `Workflow._safe_recall`) are each wrapped so a
  failure degrades (empty preamble / skipped write) rather than failing the run
  (M-02). On the backend run path the per-run store is closed in
  `run_in_background`'s `finally` (M-03), and that close is itself best-effort —
  a store whose `close()` raises is logged, not propagated, so teardown can't
  escape the worker as an unobserved Future exception. `SqliteBM25Memory.add`
  rejects a
  non-string/empty `type`, but the type **enum stays open** — a custom store may
  still model other string types (M-11). See `docs/MEMORY_REVIEW_TRIAGE.md`.
- **Recall is BM25-only by default; hybrid (BM25 + vector) recall is opt-in.**
  Plain BM25 requires at least one shared significant term between the query
  and a record's content — a semantically identical but lexically disjoint
  memory returns nothing. Setting `BESTTEAM_MEMORY_EMBEDDING_MODEL` (same spec
  convention as the vector knowledge base — `"fake:<dim>"` for $0 tests, or a
  provider string like `"openai:text-embedding-3-small"`) enables a second,
  overlap-free ranking leg: each write computes and persists an embedding
  (`memories.embedding_json`/`embedding_model`, added at write time — no
  backfill of pre-existing rows), and `search()` fuses the BM25 ranking with a
  cosine-similarity ranking via Reciprocal Rank Fusion (`k=60`), then applies
  type-aware recency decay (`BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS`, default
  14 days) — SEMANTIC records never decay (a stored fact doesn't get less true
  with age), EPISODIC/PROCEDURAL do. The embedding-resolution/cosine-math
  primitives are shared with the vector knowledge base via `core/embeddings.py`.
  Unset (the default) → behavior is byte-for-byte identical to plain BM25. A
  misconfigured spec (bad string, missing `numpy`) disables memory entirely,
  same as a bad `BESTTEAM_MEMORY_DB` path. A row without an embedding
  (pre-adoption, or the embedding call failed) simply doesn't participate in
  the vector leg and is found via BM25 only. Reranking (cross-encoder
  re-scoring of the fused candidate set) is available, opt-in — see below.
- **Query expansion (opt-in, MultiQueryRetriever-style)** is layered on top of
  recall, in `MemoryManager` (not the store — it's the one other place this
  subsystem makes an LLM call, alongside extraction). Setting
  `query_expansion_model`/`BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL` (same
  `_resolve_model` spec convention as `extraction_model`) rewrites the recall
  query into up to `query_expansion_count`/`BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT`
  (default 3) alternative phrasings via one LLM call — computed ONCE per
  `recall()`, not once per scope — and each scoped search (`semantic`;
  `episodic`/`procedural`) fans out across the literal query plus every
  expansion, fusing the per-query ranked results with `_reciprocal_rank_fusion`
  before taking the scope's `top_k`. Unlike `embedding_model` (eagerly
  resolved at store construction, so a bad spec disables memory entirely),
  this follows the `extraction_model` failure shape: resolved lazily inside
  `_expand_query`'s own try/except, so a bad spec/unparseable response/model
  error never disables memory — it just falls back to searching the literal
  query alone. `query_expansion_count<=0` disables expansion even with a
  model configured. Unset (the default) → `recall()` is byte-for-byte
  unchanged (`_fused_search`'s single-query branch calls `store.search()`
  with identical arguments to pre-expansion behavior).
  **Cost tradeoff**: with `query_expansion_count=E`, exactly 1 expansion LLM
  call per `recall()`, but up to `2 × (1+E)` `store.search()` calls (vs. 2
  today) and, when hybrid vector recall is *also* configured, up to
  `2 × (1+E)` embedding-provider calls too — `SqliteBM25Memory`'s one-entry
  query-embedding cache only helps when both scoped searches share the exact
  same query text, which no longer holds once there's more than one query
  variant. Deliberately **not** wired into two other `store.search()` call
  sites: the admin browsing/search API (`memory_api.py::get_memory_store`
  builds a `SqliteBM25Memory` directly, never a `MemoryManager` — admin search
  wants precise literal lookup) and `_extract_and_store`'s near-duplicate
  candidate search (`_semantic_candidates` — a fuzzy-expanded candidate search
  risks a false "duplicate"/"update" merge deleting a real fact). The
  expansion call's usage is metered like extraction's: it rides
  `RecallResult.expansion_usage` → the `memory_recalled`/`memory_failed`
  `TraceEvent.usage`, recorded by the backend as a `usage_records` row tagged
  `agent="memory:query_expansion"` — see `ui/backend/CLAUDE.md`. Preserved
  even when the store search that follows a successful expansion fails (the
  paid call already happened): `recall()` catches that failure internally
  (not left to `Workflow._safe_recall`'s outer catch) specifically so
  `expansion_usage` survives on the resulting `ok=False` result.
- **Reranking (opt-in)** is layered on top of the fused BM25/hybrid recall,
  in `MemoryManager._fused_search` (`core/reranking.py`). Setting
  `rerank_model`/`BESTTEAM_MEMORY_RERANK_MODEL` (same spec-string convention
  as the KB's `rerank_model` — `"fake:"` for $0 tests,
  `"cross-encoder:<model-name>"` for a real local
  `sentence_transformers.CrossEncoder`) makes each scoped search fetch
  `rerank_candidate_k`/`BESTTEAM_MEMORY_RERANK_CANDIDATE_K` (default
  `top_k * 4`, clamped like the KB's `candidate_k`) fused candidates instead
  of `top_k`, scores the capped pool against the LITERAL query only
  (`queries[0]` — never an expansion variant, even when query expansion is
  also configured) with the reranker, then re-fuses the pre-rerank
  (BM25/hybrid/recency-decayed) ranking with the rerank ranking via a
  **weighted** Reciprocal Rank Fusion
  (`_reciprocal_rank_fusion(..., weights=(1.0, _RERANK_RRF_WEIGHT))`,
  `_RERANK_RRF_WEIGHT = 8.0`) rather than just taking the reranker's order
  outright — hand-derived (see the constant's comment in `core/memory.py`)
  so the cross-encoder's signal isn't diluted by equal-weight RRF, while
  still letting a very consistent pre-rerank candidate win over the
  reranker's pick on a wide signal disagreement. Resolution mirrors
  `query_expansion_model`'s lazy/fail-soft shape, not `embedding_model`'s
  eager/fail-hard one: `_get_reranker()` resolves `rerank_model` lazily on
  first use, once per `MemoryManager` (i.e. once per run — cached on
  `self._reranker`/`self._reranker_resolve_attempted`), and a bad
  spec/missing dependency logs a warning and disables rerank for that run's
  lifetime rather than disabling memory; a rerank-time failure (a
  cross-encoder inference error) logs a warning and falls back to the
  pre-rerank `candidates[:top_k]` order — recall must never fail because of
  rerank. Unset (the default) → `recall()` is byte-for-byte unchanged. Two
  items are deliberately deferred from the v1 design (see
  `docs/superpowers/specs/2026-08-12-pluggable-rerank-design.md`,
  "Deferred"): **no differentiated failure caching** — every call for a
  still-unresolved spec retries `resolve_reranker()` from scratch (bounded to
  one retry per run, since `_reranker_resolve_attempted` prevents a second
  one within the same `MemoryManager`), rather than permanently caching a
  deterministic `ConfigurationError` separately from a possibly-transient
  failure (e.g. a model download error); and **no inference-time lock**
  across concurrent cross-encoder calls — the backend's worker pool can run
  several reranker inferences in parallel, which is safe under PyTorch's own
  internal threading on CPU but could contend for GPU memory under a GPU
  deployment (a future hardening item, not a v1 blocker, since a
  contention-driven failure just degrades to pre-rerank order like any other
  rerank error).
- An **admin-only** Memory management page (`ui/backend/memory_api.py`,
  `/api/memory`) lets admins view/search/delete a user's records and clear a
  user's whole memory, but there's no manual add/edit. `record_run` caps each
  field at `_MAX_RECORD_CHARS` (CR-022). Total growth is bounded only when
  episodic retention is enabled (SP-4/M-07, above); by default episodic rows
  accumulate until an admin clears them.
- **Memory is org-scoped** (SP-2, M-01). Records carry an `org_id`; the run
  path binds the run's org into `MemoryManager`, so recall/record only ever
  touch that org's memory (closing the username-reuse isolation gap). In the
  store, a **concrete** `org_id` filters `search`/`all`; **`org_id=None` means
  "across orgs"** — used only by the admin surface — and the **`LEGACY_ORG`
  sentinel (`"legacy"`) filters to `org_id IS NULL` only** (the third read scope,
  so the admin API can view just the legacy rows of a NULL-org identity without
  falling back to the cross-org meaning of `None`). Rows written before SP-2
  have `org_id NULL` (no cross-DB backfill: the username→org map lives in the
  main DB, unreachable from the store's own connection); they aren't recalled by
  an org run but stay visible/deletable via the admin API. The API route
  `DELETE /api/memory/orgs/{org_id}` resolves the org's **current members** (from
  the main DB `users` table) then deletes scoped + those members' legacy NULL-org
  rows in one store transaction (`delete_org_and_legacy`, rollback on failure), so
  compliance erasure is atomic and complete for anyone still in the org. Legacy rows for a
  username that no longer exists are out of scope here — that's account deletion
  (deletion-lifecycle sub-project — now implemented, below). The **`Memory` ABC**
  deliberately does *not* carry `org_id`: it's a concrete-store extension (like
  `limit`/`max_candidates`), and `MemoryManager` passes it only when a concrete
  org is bound, so a pre-SP-2 custom store still works for org-less callers.
- **Memory is principal-scoped** (deletion-lifecycle). Records also carry a
  `principal_id` — the backend's immutable, never-rotated `users.principal_id` —
  a second concrete-store scoping dimension shaped exactly like `org_id` (column
  + idempotent ALTER + `idx_memories_principal`; `add`/`add_if_absent` persist it
  and include it in the dedup key; `search`/`all`/`prune` filter when concrete,
  `None` = unfiltered; the ABC stays unchanged, `MemoryManager` binds it via
  `_scope_kwargs` only when set). The run path binds the run's `principal_id`, so
  recall/writes only touch that account instance — a deleted-then-recreated
  same-`(org, username)` account gets a new principal and **can't recall the
  deleted account's rows** (finding 1). A **`retired_principals`** table + the
  `add`/`add_if_absent` **write-fence** drop any write carrying a retired
  principal, so a run finishing after its account was deleted can't re-create
  rows behind the purge (finding 2 — `retire_principal`/`is_retired`, called from
  the backend's account-deletion path; the shared SQLite file is the
  cross-process coordination point, so no drain fence/lock is needed). Legacy
  NULL-principal rows aren't recalled by a stamped run (SP-2 accept-legacy
  precedent); `assign_null_principal` (the opt-in `backfill-memory-principals`
  operator CLI) reconciles them. Design:
  `docs/superpowers/specs/2026-07-30-memory-principal-lifecycle-design.md`.
- **Recalled memory is treated as untrusted reference, not escaped.**
  `recall_preamble` delimits recalled content (`<recalled_user_memory>`) and
  frames it reference-only to resist prompt injection from a prior tool result
  or model output that was stored (CR-021), but there's no content
  escaping/filtering engine — a proportionate mitigation for the disabled-by-
  default, per-user model, not full hardening.
- Procedural memory is per-user (could be promoted to global/agent-level later).
- **Memory is workflow-scoped for episodic/procedural, org-scoped for
  semantic** (cross-workflow memory scoping). Records also carry a
  `workflow_id` (`WorkflowRecord.id`, the stable team head — survives a
  redeploy, unlike `workflow_version_id`, which is pure per-deploy
  provenance). `add`/`add_if_absent`/`search`/`all` accept it as a
  concrete-store extension exactly like `org_id`/`principal_id` (`None` =
  unfiltered). `MemoryManager.recall()` runs two scoped searches instead of
  one: `semantic` never receives `workflow_id` (personal preferences stay
  shared across an org's workflows); `episodic`/`procedural` do (one team's
  task experience doesn't leak into an unrelated team's context) —
  `workflow_id=None` reproduces pre-existing, workflow-agnostic behavior for
  SDK-direct callers and YAML-only demo workflows (no `WorkflowRecord`).
  `record_run`/`_extract_and_store` route `workflow_id` into episodic/procedural
  writes only, never semantic. The backend binds it in
  `main.py::create_run` → `run_in_background` → `_make_memory` — see
  `ui/backend/CLAUDE.md`. No admin-API filter and no backfill of
  pre-existing (workflow_id-NULL) rows; see
  `docs/superpowers/specs/2026-08-11-cross-workflow-memory-scoping-design.md`.
