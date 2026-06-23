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

There are two implementations, chosen via `type:` in YAML:

| | `local_folder` (default) | `vector` |
|---|---|---|
| Retrieval | BM25 keyword search | Cosine similarity over embeddings |
| Setup | None — no API key | Needs an `embedding_model` |
| Cost | $0 | Depends on the embedding model (`"fake:"` = $0) |
| Good for | Keyword-heavy queries, any corpus size that fits in memory | Semantic queries ("money back" matching a doc that says "refund") |
| pip extra | `bestteam[tools-rag]` | `bestteam[tools-rag-vector]` |
| Source | `src/bestteam/core/knowledge_base.py` | `src/bestteam/core/vector_knowledge_base.py` |

Both share the same document-loading and chunking pipeline.

## Document loading and chunking

`_load_document_chunks()` (`core/knowledge_base.py`) walks the knowledge
base's folder recursively, parses every file with a supported extension via
`bestteam.tools.parse_file`, and splits the extracted text into fixed-size,
overlapping chunks.

**Supported file types** (`src/bestteam/tools/file_parser.py`):
- Plain text: `.txt`, `.md`, `.csv`, `.json`, `.yaml`, `.yml`, `.log`
- `.pdf` — text extraction via `pypdf`
- `.docx` — paragraphs + tables via `python-docx`
- `.xlsx` / `.xls` / `.xlsm` — each sheet rendered as CSV-style rows via `openpyxl`

Files with an unsupported extension, or that fail to parse, are skipped with
a `warnings.warn(...)` — the knowledge base still builds from whatever did
parse, but you'll see a warning naming the skipped file.

**Chunking**: each document's text is split into chunks of `chunk_size`
characters with `chunk_overlap` characters shared between consecutive
chunks (default `1000`/`100`). `chunk_overlap` must be non-negative and
strictly less than `chunk_size`, or the knowledge base raises a
`ConfigurationError` at load time.

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

Requires `pip install 'bestteam[tools-rag-vector]'` (numpy).

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
```

Runnable examples in `ui/backend/workflows/`:
- `knowledge_base_demo.yaml` — `local_folder`, no setup needed.
- `vector_knowledge_base_demo.yaml` — `vector` with `"fake:"` embeddings, $0.
- `vector_knowledge_base_demo_live.yaml` — `vector` with real OpenAI
  embeddings + chat model, demonstrating true semantic retrieval. Requires
  `OPENAI_API_KEY` and costs real API quota.

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
  Works for both `local_folder` and `vector` — you're expected to point
  `path` at a folder that already exists on the server.
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
- The upload is validated by actually instantiating a
  `LocalFolderKnowledgeBase` against the saved files before committing the
  database record — if chunking/parsing fails (e.g. zero readable
  documents), the upload is rejected and the partial directory is cleaned up.

**Wiring into a workflow**: a workflow's `_build_workflow()` validation only
builds the standalone knowledge bases its agents actually reference by name
(`ui/backend/knowledge_bases.py::load_knowledge_base_tools`) — not every
knowledge base in the database — since building one means re-reading and
re-chunking files (and, for `vector`, calling an embedding model).

## Known limitations

- **Single-stage retrieval only.** No query rewriting/expansion (e.g.
  LLM-based rewrite, HyDE) and no reranking (cross-encoder or LLM re-scoring
  of an over-fetched candidate set) for either knowledge base type.
- **No external vector store.** `vector` knowledge bases are an in-memory
  numpy matrix plus an optional JSON file cache — no Chroma/FAISS/Pinecone/
  Weaviate, no hierarchical or "small-to-big" indexing.
- **No DMS connectors.** Neither type can ingest directly from SharePoint,
  Confluence, Google Drive, etc. — only a local folder of files.
- **No re-embedding on document changes.** The embedding cache is
  content-addressed (by chunk text) but there's no logic to detect "this
  document changed, drop its stale chunks" beyond the chunk text itself
  changing.
- **BM25 can be unstable on tiny corpora** (a handful of documents) —
  mitigated, but not eliminated, by the stopword filter and the
  shared-significant-terms gate before ranking.
- **`core/memory.py`'s `Memory` ABC is unused.** It defines a
  `remember`/`recall` interface for persistent agent memory, but only an
  in-process `InMemoryStore` implementation exists today — it isn't wired
  into knowledge base retrieval or anything else.

## File reference

| Purpose | Path |
|---|---|
| `local_folder` implementation + shared chunking/loading | `src/bestteam/core/knowledge_base.py` |
| `vector` implementation + embedding cache | `src/bestteam/core/vector_knowledge_base.py` |
| YAML loader (`_build_knowledge_base`) | `src/bestteam/core/loader.py` |
| `KnowledgeBaseSpec` (pydantic model mirroring the YAML schema) | `src/bestteam/core/specification.py` |
| Document parsing (PDF/Word/Excel/text) | `src/bestteam/tools/file_parser.py` |
| Backend CRUD + upload endpoint | `ui/backend/crud.py` |
| Backend "only build what's referenced" loading | `ui/backend/knowledge_bases.py` |
| Example: `local_folder` | `ui/backend/workflows/knowledge_base_demo.yaml` |
| Example: `vector`, $0 fake embeddings | `ui/backend/workflows/vector_knowledge_base_demo.yaml` |
| Example: `vector`, real OpenAI embeddings | `ui/backend/workflows/vector_knowledge_base_demo_live.yaml` |
