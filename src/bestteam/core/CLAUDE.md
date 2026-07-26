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

## Knowledge bases (`core/knowledge_base.py`, `core/vector_knowledge_base.py`)

The most common client request is "connect our agents to the client's
knowledge base." The loader supports two knowledge base `type:`s, both
backed by a folder of documents (`tools.parse_file` + chunking):

- `local_folder` (default, `core/knowledge_base.py`): indexes chunks in
  memory with **BM25 keyword search** (`rank-bm25`). No API key, no vector
  store, no persistence. Best for the common case — a handful to a couple
  dozen documents.
- `vector` (`core/vector_knowledge_base.py`): embeds each chunk and ranks by
  **cosine similarity** for semantic search (e.g. a query about "refunds"
  matches a chunk that says "money back" with no shared keywords). Retrieval
  is single-stage — no query rewriting/expansion and no reranking (see
  "Known limitation: vector knowledge base retrieval is single-stage"
  below).

Both expose the resulting knowledge base to agents as an ordinary tool (named
after the KB), so it slots into the existing `tools:` / `REGISTRY` mechanism
with no `LangGraphAdapter` changes — `query()` returns the same formatted
`"...results for: <query>\n\n1. [source: ...]\n<text>..."` / `"No results
found..."` string shape regardless of type.

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

## Known limitation: vector knowledge base retrieval is single-stage

`VectorKnowledgeBase` does cosine-similarity search only — no query
rewriting/expansion (e.g. LLM-based rewrite or HyDE) and no reranking
(cross-encoder or LLM-based re-scoring of an over-fetched candidate set).
It's also in-memory plus an optional JSON embedding cache (`cache_path`) — no
external vector store (Chroma/FAISS/Pinecone) and no hierarchical/
"small-to-big" indexing. Without `cache_path`, every workflow load re-embeds
all chunks (real embedding APIs incur cost/latency on each run). There's no
DMS connector (SharePoint/Confluence/Google Drive) for either knowledge base
type.

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
  connections can't both insert. (Near-dup / contradiction resolution /
  consolidation — needs embeddings/LLM — is deferred.) Recall bounds its scan to
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
- Single-stage BM25 recall (no rerank/expansion) — same tradeoff as the KB;
  the scan is now bounded to the most-recent N (SP-4/M-09, above).
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
  "across orgs"** — used only by the admin surface. Rows written before SP-2
  have `org_id NULL` (no cross-DB backfill: the username→org map lives in the
  main DB, unreachable from the store's own connection); they aren't recalled by
  an org run but stay visible/deletable via the admin API. The API route
  `DELETE /api/memory/orgs/{org_id}` resolves the org's **current members** (from
  the main DB `users` table) then deletes scoped + those members' legacy NULL-org
  rows in one store transaction (`delete_org_and_legacy`, rollback on failure), so
  compliance erasure is atomic and complete for anyone still in the org. Legacy rows for a
  username that no longer exists are out of scope here — that's account deletion
  (deletion-lifecycle sub-project). The **`Memory` ABC** deliberately does *not*
  carry `org_id`: it's a concrete-store extension (like `limit`/`max_candidates`),
  and `MemoryManager` passes it only when a concrete org is bound, so a pre-SP-2
  custom store still works for org-less callers.
- **Recalled memory is treated as untrusted reference, not escaped.**
  `recall_preamble` delimits recalled content (`<recalled_user_memory>`) and
  frames it reference-only to resist prompt injection from a prior tool result
  or model output that was stored (CR-021), but there's no content
  escaping/filtering engine — a proportionate mitigation for the disabled-by-
  default, per-user model, not full hardening.
- Procedural memory is per-user (could be promoted to global/agent-level later).
