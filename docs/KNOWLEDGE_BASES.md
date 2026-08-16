# Knowledge Bases & RAG

How bestteam connects an agent to a client's documents — the most common
customer request ("hook our agents up to our internal docs"). This page
explains both implementations end to end: how they work, how to configure
them, how to manage them through the backend API, and what they
deliberately don't do yet.

## Overview

A **knowledge base** is a folder of documents that gets parsed, chunked,
and indexed in memory, then exposed to an agent as an ordinary tool —
a single-argument function `query(text) -> str` that returns the most
relevant excerpts. Agents pick it up exactly like a built-in tool (`web_search`,
`calculator`, …): list its name in the agent's `tools:` entry.

There are three implementations, chosen via `type:` in YAML:

| | `local_folder` (default) | `vector` | `hybrid` |
|---|---|---|---|
| Retrieval | BM25 keyword search | Cosine similarity over embeddings | BM25 + vector, fused via Reciprocal Rank Fusion |
| Setup | None — no API key | Needs an `embedding_model` | Needs an `embedding_model` |
| Cost | $0 | Depends on the embedding model (`"fake:"` = $0) | Depends on the embedding model (`"fake:"` = $0) |
| Good for | Keyword-heavy queries, any corpus size that fits in memory | Semantic queries ("money back" matching a doc that says "refund") | Either — a chunk only one method would surface can still be found |
| pip extra | `bestteam[tools-rag]` | `bestteam[tools-rag-vector]` | both `tools-rag` and `tools-rag-vector` |
| Source | `src/bestteam/core/knowledge_base.py` | `src/bestteam/core/vector_knowledge_base.py` | `src/bestteam/core/hybrid_knowledge_base.py` |

All three share the same document-loading/chunking pipeline, and all three
support opt-in **query expansion** and opt-in **reranking** — see those
sections below.

## Document loading and chunking

`_load_document_chunks()` (`core/knowledge_base.py`) walks the knowledge
base's folder recursively, parses every file with a supported extension via
`bestteam.tools.parse_file`, and splits the extracted text into chunks.

**Supported file types** (`src/bestteam/tools/file_parser.py`):
- Plain text: `.txt`, `.md`, `.csv`, `.json`, `.yaml`, `.yml`, `.log`
- `.pdf` — text extraction via `pypdf`
- `.docx` — paragraphs + tables via `python-docx`
- `.xlsx` / `.xlsm` — each sheet rendered as CSV-style rows via `openpyxl`
  (legacy `.xls` is not supported)
- `.xml` — structural rendering of tags, attributes, namespaces, and mixed
  content (via `xml.etree.ElementTree`)

Files with an unsupported extension, or that fail to parse, are skipped with
a `warnings.warn(...)` — the knowledge base still builds from whatever did
parse, but you'll see a warning naming the skipped file.

**Chunking**: each document's text is split into chunks of up to
`chunk_size` characters with `chunk_overlap` characters shared between
consecutive chunks (default `1000`/`100`). `chunk_overlap` must be
non-negative and strictly less than `chunk_size`, or the knowledge base
raises a `ConfigurationError` at load time.

Chunking is **format-aware, not a fixed character slice**: `_chunk_text`
(shared by all three KB types) splits on the document's own structure —
Markdown heading boundaries, XML element boundaries, and a generic
paragraph/sentence/word fallback (with CJK sentence terminators `。！？`) —
before falling back to a raw slice, so related content tends to stay
together in one chunk instead of being cut mid-paragraph or mid-element.
`chunk_size` is still enforced as a hard ceiling either way. This is
still single-level (not "small-to-big" hierarchical/parent-child)
chunking — see "Known limitations" below.

## `local_folder`: BM25 keyword search

`LocalFolderKnowledgeBase` indexes every chunk with [BM25](https://en.wikipedia.org/wiki/Okapi_BM25)
(via the `rank-bm25` package) — a classic keyword-ranking algorithm, no
embeddings or external services involved.

Querying:
1. Tokenize the query into lowercase alphanumeric words, dropping common
   English stopwords ("the", "and", "is", …).
2. Score every chunk with BM25.
3. Rank candidates by *(number of shared significant terms, BM25 score)* —
   chunks that share zero significant terms with the query are excluded
   entirely, so tiny corpora don't return noise just because BM25's
   statistics are unstable at small scale.
4. Return the top `top_k` chunks (default `5`), each tagged with its source
   filename.

If nothing matches, `query()` returns a plain `"No results found in
knowledge base '<name>' for: <query>"` string rather than raising.

Also supports opt-in query expansion and opt-in reranking — see those
sections below.

Requires `pip install 'bestteam[tools-rag]'`.

## `vector`: embedding similarity

`VectorKnowledgeBase` embeds every chunk with a configurable embedding
model and ranks candidates by cosine similarity — this catches semantic
matches a keyword search would miss (e.g. a query about "money back"
matching a chunk that only says "refund").

- Embeddings are L2-normalized and stored as an in-memory `numpy` matrix —
  no external vector store (no Chroma/FAISS/Pinecone).
- `embedding_model` accepts:
  - A ready-made `langchain_core.embeddings.Embeddings` instance, used as-is.
  - A provider spec string such as `"openai:text-embedding-3-small"`,
    resolved via `langchain.embeddings.init_embeddings()` (requires
    `pip install langchain`).
  - `"fake:<dim>"` (dimension optional, default `32`) — a deterministic,
    zero-cost embedding for dry runs and tests. No API key needed.
- `score_threshold` (optional, range `[-1, 1]`): drop any chunk whose cosine
  similarity falls below this cutoff. If nothing clears the bar, `query()`
  returns the same "No results found" message as `local_folder`.
- `cache_path` (optional): a JSON file persisting per-chunk embedding
  vectors across runs, keyed by `sha256(embedding_model_spec + chunk_text)`.
  This avoids re-embedding unchanged chunks (and re-paying a real provider)
  on every workflow load. Caching only works when `embedding_model` is a
  string spec — passing a live `Embeddings` instance directly skips the
  cache with a warning, since there's no stable spec string to key on. The
  cache is invalidated automatically if the model spec changes.

Also supports opt-in query expansion and opt-in reranking — see those
sections below.

Requires `pip install 'bestteam[tools-rag-vector]'` (numpy).

## `hybrid`: BM25 + vector, fused

`HybridKnowledgeBase` indexes every chunk with BOTH BM25 and embeddings,
then fuses the two rankings with [Reciprocal Rank
Fusion](https://en.wikipedia.org/wiki/Reciprocal_rank_fusion) (RRF) — so a
chunk either method alone would miss (a semantically relevant chunk with
zero keyword overlap, or a keyword match the embedding model scores as only
weakly similar) can still surface. This is the closest of the three types
to a "just works" default when you don't know in advance whether a corpus
will be searched by keyword or by meaning.

- Requires BOTH extras: `pip install 'bestteam[tools-rag,tools-rag-vector]'`.
- Configuration is the union of `local_folder` and `vector`'s: `path`,
  `embedding_model`, `chunk_size`/`chunk_overlap`/`top_k`, optional
  `cache_path`.
- `score_threshold` (optional) filters **only the vector leg** — a chunk
  below the cutoff can still surface via a BM25 keyword match, so unlike
  `vector` alone, setting it does not guarantee "No results found" when no
  chunk meets it.
- Also supports opt-in query expansion and opt-in reranking — see those
  sections below.

See `ui/backend/workflows/hybrid_knowledge_base_demo.yaml` for a $0 dry-run
example using `"fake:"` specs for both the chat model and the embeddings.

## Query expansion (opt-in, all three types)

All three KB types accept `query_expansion_model` (same spec-string
convention as `embedding_model`/`rerank_model` — e.g. `"openai:gpt-4o-mini"`
or `"fake:"` for $0 tests) and `query_expansion_count` (default `3`). When
set, `query()` rewrites the query into up to `query_expansion_count`
alternative phrasings via one LLM call, searches with the literal query
plus every alternative, and fuses the per-variant results with the same
Reciprocal Rank Fusion used by `hybrid`. A bad spec, an invoke error, or an
unparseable response degrades to searching the literal query alone — a
query never fails because expansion failed. Left unset (the default),
`query()` is byte-for-byte unchanged.

This call's cost is **unmetered**: knowledge base tools run inside the
agent's generic tool-calling loop, which has no hook to report a nested
LLM call's token usage back to the backend — the same pre-existing gap the
`vector` type's embedding calls already have.

## Reranking (opt-in, all three types)

All three KB types accept `rerank_model` (a spec string, e.g. `"fake:"` for
$0 tests or `"cross-encoder:<model-name>"` for a real local
`sentence_transformers.CrossEncoder`) and `candidate_k` (default
`top_k * 4`, clamped to `[top_k, 100]`). When `rerank_model` is set,
`query()` over-fetches `candidate_k` results from the existing
BM25/cosine/fused ranking instead of just `top_k`, scores each candidate
against the query with the reranker, and returns the top `top_k` by rerank
score. A bad reranker spec, or a `candidate_k` outside `[top_k, 100]`,
raises `ConfigurationError` at construction (fail-hard, like a bad
`chunk_size`); a rerank-time failure (e.g. a cross-encoder inference error)
logs a warning and falls back to the pre-rerank order — rerank is a quality
layer, never a reason a query itself fails. Left unset (the default),
`query()` is byte-for-byte unchanged.

Requires `pip install 'bestteam[tools-rerank]'`.

## Configuring a knowledge base in YAML

Knowledge bases are declared under a workflow's `knowledge_bases:` key, then
referenced by name in an agent's `tools:` list — `core/loader.py` builds
each one and registers it in the same tool lookup table used for built-in
tools.

**`local_folder` (default type):**
```yaml
knowledge_bases:
  - name: product_docs
    path: ./docs/product   # resolved relative to the workflow YAML's directory
    # optional: chunk_size (default 1000), chunk_overlap (default 100), top_k (default 5)

agents:
  - name: support_agent
    role: Support Specialist
    goal: Answer customer questions using the product documentation
    model: "openai:gpt-4o-mini"
    tools: [product_docs, calculator]
```

**`vector`:**
```yaml
knowledge_bases:
  - name: product_docs
    type: vector
    path: ./docs/product
    embedding_model: "openai:text-embedding-3-small"  # or "fake:<dim>" for $0 dry runs
    score_threshold: 0.5                              # optional
    cache_path: ./.bestteam_cache/product_docs.json    # optional
    # optional, all three types: rerank_model, candidate_k,
    # query_expansion_model, query_expansion_count — see the sections above
```

**`hybrid`:**
```yaml
knowledge_bases:
  - name: product_docs
    type: hybrid
    path: ./docs/product
    embedding_model: "openai:text-embedding-3-small"  # or "fake:<dim>" for $0 dry runs
    cache_path: ./.bestteam_cache/product_docs.json    # optional
```

Runnable examples in `ui/backend/workflows/`:
- `knowledge_base_demo.yaml` — `local_folder`, no setup needed.
- `vector_knowledge_base_demo.yaml` — `vector` with `"fake:"` embeddings, $0.
- `vector_knowledge_base_demo_live.yaml` — `vector` with real OpenAI
  embeddings + chat model, demonstrating true semantic retrieval. Requires
  `OPENAI_API_KEY` and costs real API quota.
- `hybrid_knowledge_base_demo.yaml` — `hybrid` with `"fake:"` embeddings, $0.

## How agents use a knowledge base

`core/loader.py::_build_workflow` builds every entry under
`knowledge_bases:`, wraps it with `make_knowledge_base_tool(kb)`
(`core/knowledge_base.py`) — which just gives the KB's `query` method a
`__name__` matching the KB's `name` plus an auto-generated docstring — and
adds it to the same tool lookup dict used for `web_search`/`calculator`/etc.
An agent's `tools:` list resolves names from that combined dict, so
referencing a knowledge base looks identical to referencing a built-in
tool. At runtime, `adapters/langgraph_adapter.py`'s tool-calling loop binds
all of an agent's tools to the model and dispatches by name when the model
calls one.

## Managing knowledge bases through the backend API

Beyond YAML files on disk, the backend (`ui/backend/`) lets you manage
knowledge bases as first-class records in the database, under
`/api/config/knowledge_bases`:

- `GET /api/config/knowledge_bases` — list all standalone knowledge bases.
- `GET /api/config/knowledge_bases/{name}` — fetch one's config.
- `PUT /api/config/knowledge_bases/{name}` — create/update by posting a
  `KnowledgeBaseSpec`-shaped JSON body (same fields as the YAML form).
  Works for all three types — you're expected to point `path` at a folder
  that already exists on the server.
- `DELETE /api/config/knowledge_bases/{name}` — delete the record (and, if
  it was created via the upload endpoint below, removes its upload
  directory too).

**File upload** (`POST /api/config/knowledge_bases/{name}/upload`,
`ui/backend/crud.py`) is a `local_folder`-only convenience that lets a
non-technical user create a knowledge base by uploading files directly,
with no server filesystem access needed:
- Accepts a multipart `files` list plus optional `chunk_size`/`chunk_overlap`/`top_k`.
- Limits: 30 files per upload, 30MB per file, ~500MB total.
- Uploaded filenames are sanitized to their bare basename (stripping any
  directory component) and rejected if empty, `.`, or `..` — closing off a
  path-traversal escape via a crafted filename.
- Files land under `ui/backend/data/knowledge_base_uploads/{name}/`, a
  directory the backend owns (distinct from the manual-config `path`, which
  points at a folder the user manages themselves).
- Validation is no longer synchronous: the `KnowledgeBaseRecord` is upserted
  and a `queued` ingestion job created immediately, and parsing/chunking
  (and rejecting e.g. zero readable documents as a `failed` job) happens on
  the background job instead — see "Uploads are asynchronous (ingestion
  jobs)" below.

**Org self-service upload** (`POST /api/org/knowledge-bases/{name}/upload`,
`ui/backend/org_knowledge_bases.py`) is the same shared `upload_knowledge_base()`
machinery behind the Team Builder wizard's "Your documents" step, reachable
by any org member (not just an admin), org-resolved from the caller's own
bearer token. Tighter limits than the admin route (10 files / 10MB per file /
50MB total) and a per-org cap on how many self-service knowledge bases can
exist (20). By default it still creates a `local_folder` KB, exactly like the
admin upload endpoint — but it also accepts a `smart_search` flag. The
wizard exposes this as a **"Standard" / "Enhanced" toggle**, deliberately
with no model names or KB-type jargon shown (the wizard's audience is
non-technical): `GET /api/org/knowledge-bases/capabilities` tells the
frontend whether the toggle should render at all (`smart_search_available`,
true only when the operator has set `BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL`),
and "Enhanced" sends `smart_search=true`, which upgrades the created KB to
`type: hybrid` with query expansion (using the wizard's own default chat
model from the model catalog, resolved server-side) and, if
`BESTTEAM_KB_DEFAULT_RERANK_MODEL` is also set, reranking too. "Standard" (or
smart search unavailable, or the env var unset despite a stale client sending
`smart_search=true`) always falls back to plain `local_folder` — the exact
same shape as before this toggle existed. See `.env.example`.

**Wiring into a workflow**: a workflow's `_build_workflow()` validation only
builds the standalone knowledge bases its agents actually reference by name
(`ui/backend/knowledge_bases.py::load_knowledge_base_tools`) — not every
knowledge base in the database — since building one means re-reading and
re-chunking files (and, for `vector`, calling an embedding model).

### Uploads are asynchronous (ingestion jobs)

Both upload endpoints (admin `/api/config/knowledge_bases/{name}/upload` and
org self-service `/api/org/knowledge-bases/{name}/upload`) dispatch a
background ingestion job instead of parsing/chunking/embedding inline on the
request thread. `ui/backend/ingestion.py`'s own `ThreadPoolExecutor` (4
workers) does the actual parse/chunk/(embed) work; the upload endpoint
returns immediately with `{"name", "job_id", "status": "queued"}` — no
`config`/`chunk_count`, since those aren't known until the job finishes.

Poll the job to completion via:

`GET /api/config/knowledge_bases/{name}/ingestion-jobs/{job_id}` (admin,
takes `?org=`) or `GET /api/org/knowledge-bases/{name}/ingestion-jobs/{job_id}`
(org self-service, org resolved from the bearer token) — both org-scoped and
404 on an unknown KB/job. Response shape:

```json
{
  "job_id": 42,
  "status": "queued | running | completed | failed",
  "file_count": 12,
  "documents_succeeded": 11,
  "documents_failed": 1,
  "chunk_count": 340,
  "errors": [{"filename": "corrupt.pdf", "error": "..."}],
  "config": { "...": "only present once status == \"completed\"" }
}
```

**Per-document partial failure**: one bad file (fails to parse, or produces
zero chunks) doesn't fail the whole job — it's recorded as a `failed`
`KnowledgeDocument` row (capped error text) and the job continues with the
rest. The job itself only fails outright if *every* document failed (so the
KB would have zero chunks) or, for `vector`/`hybrid`, if the embedding call
itself fails (in which case the job's already-flushed document/chunk rows
are rolled back — a vector/hybrid KB with no embeddings can't serve
queries, so a total embedding failure must leave no partial rows behind).

**Retrieval: DB-backed vs. legacy file-based fallback.** A KB whose most
recent `IngestionJob` reached `status="completed"` is served entirely from
its `KnowledgeDocument`/`KnowledgeChunk` rows — built via the `from_chunks(...)`
alternate constructor on the matching `KnowledgeBase` subclass (see
`src/bestteam/core/CLAUDE.md`), never re-reading files from disk. A KB that
predates this feature (or was never re-uploaded since) has no completed
ingestion job, so it falls back to the original file-based construction
(`_build_knowledge_base`, scanning its upload directory / `CURRENT` pointer
as before). `ui/backend/knowledge_bases.py::resolve_knowledge_base()` is the
one place this choice is made, shared by both `load_knowledge_base_tools`
(above) and `builder.py::_all_knowledge_base_tools` (the wizard's
pre-Specification "every standalone KB" catalog), so a workflow build and
the wizard's KB catalog always agree on which content is live.

Deleting a knowledge base cascades to delete its `IngestionJob`/
`KnowledgeDocument`/`KnowledgeChunk` rows (`ingestion.delete_kb_ingestion_data`).
Older completed ingestion generations are pruned automatically (keeping the
current one plus one grace-window generation) once a new job completes.

See `docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`
for the full design.

## Known limitations

- **Chunking is format-aware, not hierarchical.** Related content tends to
  stay together in one chunk (see "Document loading and chunking" above),
  but this is still single-level chunking — no "small-to-big"/parent-child
  multi-resolution indexing, and overlap between chunks is a raw
  character-slice of the previous chunk's tail (not structure-aware).
- **No external vector store.** `vector`/`hybrid` knowledge bases embed into
  an in-memory numpy matrix plus an optional JSON file cache — no
  Chroma/FAISS/Pinecone/Weaviate/pgvector, so this doesn't scale past a
  single-process, small-to-medium corpus.
- **No DMS connectors.** None of the three types can ingest directly from
  SharePoint, Confluence, Google Drive, etc. — only a local folder of files.
- **No re-embedding on document changes.** The embedding cache is
  content-addressed (by chunk text) but there's no logic to detect "this
  document changed, drop its stale chunks" beyond the chunk text itself
  changing.
- **BM25 can be unstable on tiny corpora** (a handful of documents) —
  mitigated, but not eliminated, by the stopword filter and the
  shared-significant-terms gate before ranking.
- **Citations are filename-only.** A returned chunk is tagged with its
  source filename, not a chunk id, page number, or heading/section — no
  precise click-through citation or "which version of which page" audit
  trail.
- **The wizard's self-service "Enhanced" toggle is all-or-nothing and
  operator-configured, not customer-tunable.** A customer can choose
  Standard vs. Enhanced, but not the embedding/rerank model, `chunk_size`,
  `top_k`, or `candidate_k` — those stay fixed at the SDK's defaults plus
  whichever models the operator set in `BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL`/
  `BESTTEAM_KB_DEFAULT_RERANK_MODEL`. Fine-grained control over any of these
  still requires the YAML/admin-API config path, not the wizard.
- **`core/memory.py` implements a separate per-user memory system**
  (`Memory` ABC + `SqliteBM25Memory` + `MemoryManager`) — not a knowledge
  base type, but it shares the CJK-aware tokenizer (`core/text_tokenize.py`),
  the RRF fusion helper (`core/fusion.py`), and the reranking helper
  (`core/reranking.py`) with the knowledge base types above. See
  `src/bestteam/core/CLAUDE.md`. Memory is not wired into knowledge base
  retrieval, or vice versa — recalling a user's memory and querying a
  knowledge base remain two independent tools.

## File reference

| Purpose | Path |
|---|---|
| `local_folder` implementation + shared chunking/loading | `src/bestteam/core/knowledge_base.py` |
| `vector` implementation + embedding cache | `src/bestteam/core/vector_knowledge_base.py` |
| `hybrid` implementation | `src/bestteam/core/hybrid_knowledge_base.py` |
| Shared RRF fusion + query expansion helpers | `src/bestteam/core/fusion.py` |
| Shared reranking helper | `src/bestteam/core/reranking.py` |
| YAML loader (`_build_knowledge_base`) | `src/bestteam/core/loader.py` |
| `KnowledgeBaseSpec` (pydantic model mirroring the YAML schema) | `src/bestteam/core/specification.py` |
| Document parsing (PDF/Word/Excel/XML/text) | `src/bestteam/tools/file_parser.py` |
| Backend CRUD + admin upload endpoint | `ui/backend/crud.py` |
| Shared upload/index/version logic + "only build what's referenced" loading | `ui/backend/knowledge_bases.py` |
| Async ingestion jobs (parse/chunk/embed on a background thread) | `ui/backend/ingestion.py` |
| Org self-service upload + "smart search" toggle | `ui/backend/org_knowledge_bases.py` |
| Example: `local_folder` | `ui/backend/workflows/knowledge_base_demo.yaml` |
| Example: `vector`, $0 fake embeddings | `ui/backend/workflows/vector_knowledge_base_demo.yaml` |
| Example: `vector`, real OpenAI embeddings | `ui/backend/workflows/vector_knowledge_base_demo_live.yaml` |
| Example: `hybrid`, $0 fake embeddings | `ui/backend/workflows/hybrid_knowledge_base_demo.yaml` |
