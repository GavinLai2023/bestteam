# bestteam — `src/bestteam/core/` (Specification, Requirements, knowledge bases)

Directory-scoped notes for the Team Builder's structured-output stages and
the knowledge-base implementations. See the root `CLAUDE.md` for project
overview, architecture, and commands.

## Specification and Requirements

- **Specification = loader schema + wizard-only friendly fields**
  (`core/specification.py`): `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`/
  `PipelineSpec`/`Specification` are pydantic models that mirror the YAML
  loader's raw dict (see `core/loader.py::_build_pipeline`), plus
  presentation-only fields (`display_name`, `friendly_description`) that
  `to_raw()` strips before validation. `validate_specification()` compiles
  the stripped dict via `_build_pipeline()` and raises `ConfigurationError`
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
  no `_build_pipeline` validation applies at this stage (it's a plain-language
  summary, not yet a team design). `Requirements.to_prompt()` renders it as
  text for the Solution Architect's `requirements` argument.

## Skills (`SkillSpec`, `AgentSpec.skills`)

`SkillSpec` (`core/specification.py`) is a reusable instruction document plus
the tools it depends on: `{name, description, instructions, tools}`. An
`AgentSpec` can reference skills by name via `skills: List[str]` -- a real
loader-level field (unlike `display_name`/`friendly_description`, `to_raw()`
keeps it).

`core/loader.py::_build_pipeline` resolves `skills:` via an optional
`extra_skills: Dict[str, SkillSpec]` parameter (mirrors `extra_tools`;
`load_pipeline(..., skills=[...])` builds it by `.name`). For each agent:

- Each skill name is looked up in `extra_skills`; an unknown name raises
  `ConfigurationError("Unknown skill '<name>'. Available skills: <...>")`.
- The skill's `tools` are appended to the agent's own `tools` (agent's tools
  first), de-duplicated preserving order, then resolved through the same
  `tool_lookup` as ordinary `tools:` -- an unresolvable name raises the
  existing `"Unknown tool '<name>'. Available tools: <...>"` error.
- The skill's `instructions` are appended to the agent's `backstory`, one per
  skill in `skills:` order, joined by `"\n\n"`.

`validate_specification()`/`generate_specification()` accept the same
`extra_skills` parameter, passed through to `_build_pipeline()`.

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
  `ui/backend/pipelines/hybrid_knowledge_base_demo.yaml`. The two legs are
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

**Retrieval and presentation are split** (P0-3). `KnowledgeBase.search(query,
top_k) -> List[_Chunk]` is the abstract method each subclass implements;
`query()` is **concrete on the base class** and is just
`format_results(self.name, query, self.search(...))`. One formatter means the
citation tags cannot drift between types, and the split is the seam the
retrieval trace (P0-5) and the eval harness (P0-7) were meant to
build on — consuming chunks rather than re-parsing a formatted string. The
**eval harness now exists** (`core/kb_eval.py` + `scripts/kb_eval.py`, P0-7):
`evaluate(kb, queries, top_k)` drives any KB type through `search()` and
reports recall@k / MRR / hit@1 **per source document** (one relevant document
per query, so recall@k == hit@k), plus an optional `expected_substring` hit@k
over the *text* of the expected document's own chunks (a match in another
document doesn't count), which catches a chunking regression. The golden set is
`tests/fixtures/kb_eval/` — 10 documents (5 EN / 5 ZH) and 20 queries split
16 `lexical` (BM25 must rank the document first) / 4 `paraphrase` (no shared
significant terms, so BM25 misses them by design; they are the headroom a real
embedding model should close, and why the guarded thresholds are recall@3 ≥
0.8 / MRR ≥ 0.7 rather than 1.0). `fake:` embeddings are deterministic noise —
the hybrid test is a smoke test and asserts no quality. The **retrieval trace
now exists too** (P0-5): `make_knowledge_base_tool`'s wrapper calls `search()`
and `format_results()` itself (instead of `query()`, which is exactly those
two) so it can report the retrieval — `query` (first 200 chars),
`hit_count`, `sources` (de-duplicated `_citation`s, at most 10) and a
`summary` — through `core/tool_context.py`, a contextvar-scoped box the
adapter's tool loop opens around each call (`ToolCallContext(trace, usage)`,
`report_trace`/`add_usage`, all no-ops when no run is active, so an
SDK-direct `kb.query()` is unaffected). The wrapper is marked
`__bestteam_tool_kind__ = "knowledge_base"` (a marker, not a name set — a KB
tool is named after its KB), and the adapter builds that call's
`tool_completed` from the report alone: a KB tool's event never carries
`_summarize(result)`, i.e. never the indexed documents' own text. `usage` is
the same channel's other half, and is now in use (P0-4): the tool loop drains
it onto the calling agent's `agent_completed.usage` so a KB's query embedding
and query-expansion calls are metered -- see "Metering a knowledge base's
spend", below. A `_Chunk` carries `source`, `text`, and two optional location fields —
`page` (PDF, chunked per page by `_chunk_document`, so `p.N` is exact) and
`heading` (a Markdown *or Word* section, or a spreadsheet sheet / Word table, that a
chunk opens under — an 80-char approximation) — which `_citation()` renders as
`[source: handbook.pdf, p.3 § Refunds]`. Both default to `None`, so a
two-field `_Chunk(source=, text=)` and every `from_chunks` caller keep working
and render byte-for-byte as before. All three types also take an
optional `description` (≤500 chars, `KnowledgeBaseSpec.description`) — one
sentence about the documents, injected into the tool's own docstring so a
model can tell an org's collections apart. See `docs/KNOWLEDGE_BASES.md`.

**YAML usage — `local_folder`:**
```yaml
knowledge_bases:
  - name: product_docs
    path: ./docs/product   # relative to the pipeline YAML's directory
    # optional: chunk_size (default 1000), chunk_overlap (default 100), top_k (default 5)

agents:
  - name: support_agent
    role: Support Specialist
    goal: Answer customer questions using the product documentation
    model: "openai:gpt-4o-mini"
    tools: [product_docs, calculator]
```

Requires `pip install 'bestteam[tools-rag]'`. See
`ui/backend/pipelines/knowledge_base_demo.yaml` for a runnable example.

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
    #           load_pipeline() doesn't re-embed unchanged chunks every time. Only
    #           applies when embedding_model is a string spec; if you pass a live
    #           Embeddings instance, caching is skipped with a warning. Resolved
    #           relative to the pipeline YAML's directory, like `path`.
    cache_path: ./.bestteam_cache/product_docs.json
```

`embedding_model` is resolved like `Agent.model`: a `langchain_core.embeddings.Embeddings`
instance is used as-is, `"fake:<dim>"` (dim optional, default 32) gives a $0
deterministic embedding for dry runs/tests, and other provider strings (e.g.
`"openai:..."`) are resolved via `langchain.embeddings.init_embeddings`
(requires `pip install langchain`). Requires `pip install 'bestteam[tools-rag-vector]'`
(numpy). See `ui/backend/pipelines/vector_knowledge_base_demo.yaml` for a $0
dry-run example using `"fake:"` specs, or
`ui/backend/pipelines/vector_knowledge_base_demo_live.yaml` for the same
pipeline wired to real OpenAI embeddings + chat model
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
(BM25 + embeddings). See `ui/backend/pipelines/hybrid_knowledge_base_demo.yaml`
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
alone -- a query never fails because expansion failed. **This call is
metered** (P0-4): `_query_variants()` reads the expansion response's
`usage_metadata` and reports it through `core/tool_context.py::add_usage`,
from which the adapter's tool loop drains it onto the calling agent's
`agent_completed.usage` -- the same list a model call's own usage rides, so
the backend needs no new event field. An unparseable response is still
metered (the call was still billed); a `fake:` model reports no
`usage_metadata` and so records nothing, and a `BaseChatModel` instance
records tokens with `model=None` (cost stays null), mirroring
`MemoryManager._usage_entry`. See
`docs/superpowers/specs/2026-08-15-kb-hybrid-retrieval-design.md`.

## Metering a knowledge base's spend (P0-4)

Query-time spend rides the tool call's own context and needs no backend
change; ingestion spend is recorded by the backend directly. What the SDK
owns:

- `core/embeddings.py::estimate_embedding_tokens(text)` -- one token per CJK
  character (via `text_tokenize._CJK_RUN_RE`) plus one per four other
  characters. Embedding providers report no token usage through LangChain's
  `Embeddings` interface, so an embedding row *has* to estimate; expect ±30%.
  Deterministic, no `tiktoken` dependency.
- `core/embeddings.py::billable_spec(model)` -- the single definition of "is
  there anything to bill": a non-`fake:` **string** spec, else None. A live
  `Embeddings`/`BaseChatModel` instance has no spec for `model_catalog` to
  price, and `fake:` is $0 by construction. Shared with
  `ui/backend/ingestion.py` so both sides agree.
- `report_query_embedding_usage()` in the same module, called from
  `VectorKnowledgeBase._vector_leg`/`HybridKnowledgeBase._vector_leg` right
  after `embed_query` -- once per query variant, so expansion's extra
  variants are billed as the extra calls they are. `self._embedding_spec` is
  the `billable_spec()` result stashed at construction (both the folder
  constructor and `from_chunks`).
- `core/embeddings.py::embed_documents_in_batches(embeddings, texts)` -- the
  one way this codebase embeds a *list* of documents (both `_embed_chunks`
  copies and `ui/backend/ingestion.py`; `memory.py`'s single-record write and
  every `embed_query` are untouched). It sends `_EMBED_BATCH_SIZE` = 100 texts
  per provider call and gives a failed batch up to `_EMBED_ATTEMPTS` = 3
  attempts -- i.e. two retries -- backing off 1s then 2s, re-raising the
  original exception if the third attempt still fails. **Only the failing batch is retried**, so a hiccup
  partway through a large corpus no longer discards the chunks already embedded
  and paid for. Any exception retries -- classifying provider exceptions would
  mean tracking every provider's taxonomy, so an auth failure waits 3s before
  surfacing. A batch returning the wrong number of vectors raises immediately,
  without retrying (a deterministic answer won't change on a second ask) --
  as `ConfigurationError`, the same type `resolve_embedding_model` raises for
  every other provider-shape problem, and the type both KB constructors have
  always raised for a mis-sized response. Neither the batch
  size nor the attempt count is a parameter -- no caller has a reason to differ.
  Metering is unaffected: ingestion estimates its `kb:ingest` tokens once from
  the chunk texts, so a retried batch is never billed twice.
- Reranking is **not** metered: a cross-encoder runs locally, in-process,
  with no provider call to bill.
- **Document embeddings are metered only on the upload/ingestion path**
  (`ui/backend/ingestion.py`). A `vector`/`hybrid` KB built straight from a
  folder path — a YAML-configured one via `core/loader.py`, or an uploaded one
  with no completed `IngestionJob`, which `resolve_knowledge_base()` falls
  back to building from files — embeds every chunk in its constructor, at load
  time, outside any run and any job; that spend is not recorded, and without
  `cache_path` it repeats on every load. Query-time metering is unaffected.

## Known limitations: knowledge base storage, chunking, and reranking

`VectorKnowledgeBase` is also in-memory plus an optional JSON embedding
cache (`cache_path`) — no external vector store (Chroma/FAISS/Pinecone) and
no hierarchical/"small-to-big" indexing. Without `cache_path`, every
pipeline load re-embeds all chunks (real embedding APIs incur cost/latency
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

**An unreadable document is reported, not silently skipped** (P0-6).
`_load_document_chunks` no longer filters unsupported suffixes out of its
`rglob` before the loop -- it warns per file, naming the type and the
supported set (`_unsupported_suffix_message`), so a `.png` dropped into a
knowledge folder is something the operator hears about. It also warns on a
document that parsed but contributed nothing: `_has_extractable_text` strips
the parser-generated header lines (`_PARSER_HEADER_RE`, covering `[PDF: …]`,
`[Word: …]`, `[Excel: …]`, `[Sheet: …]`, `[Table N]`, `[XML: …]`) and checks
what remains, because a **scanned PDF** parses to its header line alone --
non-empty, so it used to become a chunk that matched nothing and told nobody
the pages were never read. Both helpers plus `_NO_TEXT_MESSAGE` are shared
with `ui/backend/ingestion.py`, which raises them as per-document failures
instead of warnings; keeping one wording means the SDK and the upload path
cannot disagree about why a file was rejected.

**Chunking is format-aware, not hierarchical.** `_chunk_document` (shared by
all three KB types, and by `ui/backend/ingestion.py`) is the per-document
entry point. A `.pdf` is split on `PAGE_BREAK` (the `\f` `_parse_pdf_bytes`
now joins pages with -- the constant lives in `tools/file_parser.py`, with the
producer that writes it, and is imported here) and each page chunked through
`_chunk_text` on its own, so a chunk never straddles a page. An
`.xlsx`/`.xlsm`/`.docx` goes through `_chunk_tabular_document`, which splits on
the parser's own `[Sheet: ...]`/`[Table N]` marker lines and, for a block too
long to fit one chunk, repeats the marker and the first body row with anything
in it (**assumed** to be the column header) at the top of every chunk of that
block -- otherwise the second chunk on has neither the sheet name nor the
columns, and cites the filename alone. Leading rows that are empty once commas
and whitespace are removed (`_row_has_text`) are skipped when choosing that
header: `read_only` openpyxl renders the spacer row above a sheet's headers as
`,,`, and repeating *that* would make the feature silently do nothing for a
very common workbook layout. The residual limit is a **non-empty title row**
above the headers, which nothing here can tell apart from a header row. The marker also becomes the chunk's `heading`, under the same
`_MAX_HEADING_CHARS` cap, so a long sheet name can't bypass it into a citation.
An `.xml` goes through `_chunk_xml_document`, which reads the element tree
off the renderer's indentation (one line per element, two spaces per level
-- `tools/file_parser.py::_render_xml_tree`) and packs sibling subtrees
greedily; a subtree too large to fit is opened up one level, its own element
line joining the **ancestor path that is repeated at the top of every chunk**
-- the XML counterpart of the repeated sheet marker and header row. The case
it exists for is a flowchart or decision tree exported as nested XML: a
`<branch answer="No">` cut away from its `<decision question="…">` is
meaningless, so the chunk carries the decision and every branch and decision
above it, and cites the nearest one as `heading`
(`[source: flow.xml § decision id="3" question="Is the item unopened?"]`,
the `tag attr="…"` part of the line, same 80-char cap). The repeated path is
**capped at half of `chunk_size`**, outermost ancestors dropped first, so
content always keeps at least half the chunk (uncapped, a six-level path in
a small chunk shredded every leaf); an ancestor capped out of *every*
descendant chunk is emitted once as content at its own level instead, since
its opening line is the only copy of its attributes and inline text (Codex
review, P1). A parent's mixed-content tail -- rendered as a text line at the
children's depth -- is its own section, never packed, prefixed or cited under
the child it happens to follow (P2). The walk keeps its own stack rather than
recursing, as the renderer does -- a valid document nests deeper than the
Python call stack, and at a large `chunk_size` every level would otherwise
cost a frame (round 2) -- and measures indents once, since re-stripping a
deeply indented line at every level was quadratic. Three more consequences
worth knowing:
XML chunks get **no character overlap** (the path is the cross-chunk context,
every split lands on a whole line, and a raw slice of the previous chunk
would put a cut-open tag ahead of the path); a leaf too large for its path,
or an opener too long to head a path at all, falls back to the generic
separators under whatever path did fit; and the `[XML: name]` header rides
on the first chunk only when there is room, since the citation already names
the file. A document that fits one chunk is byte-for-byte what it was. Edge-based diagram exports (draw.io's `<mxCell source= target=>`)
get no special treatment: their branches are id references, not nesting,
and nothing here reconstructs them.
The repeated prefix comes out of the chunk's budget (`chunk_size - len(prefix)`
for the body), so overlap shrinks and can reach zero; a prefix that would leave
no room at all falls back to the ordinary path, still tagged with the heading.
A `.docx` takes its own branch inside `_chunk_tabular_document`
(`_chunk_docx_document`), because its tables are interleaved with its prose
rather than appended after it: `_docx_segments` cuts the parsed text into
alternating prose / `[Table N]` segments, ending a table block at the blank line
the parser puts after it. Running each marker to the next one -- what a workbook
still does, since `read_only` openpyxl legitimately renders blank rows a
blank-line terminator would cut a sheet at -- would file the paragraphs
*between* two tables under the preceding table's citation. That terminator is
unambiguous only because `_parse_docx_bytes` **drops a table row with no text in
any cell**: in a one-column table such a row renders as the empty string, and a
spacer row would otherwise end the block early and spill every row after it into
prose. Prose segments split
on the **Markdown** separators (`_MARKDOWN_SUFFIXES` = `{".md", ".docx"}`),
because `_parse_docx_bytes` now renders Word's heading styles as `#` lines; a
table block deliberately does not, so a cell beginning `# ` can't become a
heading boundary inside a run of rows. `_headings_for` takes a `start` heading
so the section a table interrupted still labels the prose after it
(`_trailing_heading` computes what to hand on -- the heading in effect *after* a
segment, which is not the last entry `_headings_for` returns), and it now
ignores parser-generated header lines when deciding whether a piece opens with
its own heading: `[Word: report.docx]` always precedes the first one, so without
that a Word document's first section always lost its heading.
`_MARKDOWN_HEADING_RE` is **imported from `tools/file_parser.py`**, which now
writes that shape and escapes any Word prose colliding with it -- one
definition, so the writer and the reader cannot disagree about what a heading
is. A block
with no readable row under its marker -- a workbook's untouched trailing
`Sheet2`, or a formatted-but-empty sheet whose rows are bare commas -- yields no
chunk at all, so table awareness can't reintroduce the content-free chunk P0-6
removed. That emptiness test is `_row_has_text` per row rather than P0-6's own
`_has_extractable_text`, which counts a `,,` row as content and is left alone
because every other format goes through it. Every
other format goes through
`_split_pieces` then `_apply_overlap` directly — the two halves `_chunk_text`
is composed of — so a `.md` chunk's section heading (`_headings_for`) can be
read off the pieces *before* overlap prefixes each one with the previous
chunk's tail, which would otherwise shift every heading by one section.
Per-page PDF chunking costs cross-page overlap — accepted for an exact `p.N`.
`_chunk_text` splits on the document's own structure — Markdown heading
boundaries and a generic paragraph/sentence/word fallback (with CJK sentence
terminators `。！？`) — replacing the old fixed-offset character slicing; XML
has its own tree-aware path above. This closes the
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

**What the tokenizer does** (P1-1): it lowercases text, stems each Latin/digit
token with `snowballstemmer`'s English stemmer, and splits each maximal run of
Han, kana or Hangul characters into overlapping bigrams (a lone character
becomes its own token). Stemming means only inflections of one word conflate
("refund"/"refunds"/"refunded"), never synonyms — that headroom is still the
`vector`/`hybrid` types' job. `_STOPWORDS` is stemmed with the same stemmer so
a stemmed query token still matches its stopword entry -- which also means a
word that stems *into* a stopword ("willing" -> "will", "doing" -> "do") is
dropped from `significant_terms`, and so stops counting towards the overlap
gate. That is standard IR behaviour rather than a defect, and BM25 scoring is
unaffected either way. Kana and Hangul sit in
the *same* character class as Han, so a kanji+kana Japanese word is one run
rather than two fragments cut at the script boundary. `snowballstemmer` is a
**soft import** (in the `tools-rag` and `tools` extras): without it `_stem` is
the identity, which keeps `core/embeddings.py` — which imports this module only
for `_CJK_RUN_RE` — working in an install with no RAG extra, and keeps one
process symmetric on both the index and the query side either way. The stemmer
object is per-thread (`threading.local`), because Snowball's `stemWord` mutates
instance state and the backend queries from a worker pool, and `_stem` is
memoized (a bounded `lru_cache`) because stemming a token costs ~150x
tokenizing it and a corpus reuses one small vocabulary — without the cache,
indexing a chunk went from 0.10 ms to 16 ms, paid on every pipeline load.
Tokens are never persisted, so changing any of this needs no migration or
backfill.

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
so `Pipeline.stream` can emit it on a `memory_recorded` TraceEvent and the backend
meters it (`agent="memory:extraction"`, M-04) — the SDK never touches the backend
DB. `Pipeline.stream` also emits `memory_recalled` (`data`=count, 0 included) and,
on a recall/record failure, a sanitized `memory_failed` (`data`=`"recall"`/
`"record"`) for observability (M-05). Recall events precede the agents; **recording
events are emitted AFTER `run_completed`** (see the ordering note below), so a
slow/hung extraction can't wedge the run. Recording stays best-effort (a failure
yields `memory_failed`, never `run_failed`). `Pipeline.run`
surfaces the same instrumentation on `PipelineResult` for parity with `stream()`:
`.memory` (recording `MemoryOutcome`; `None`=disabled, `ok=False`=recording
failure) and `.recall` (`RecallResult`; `None`=disabled, `count`=records drawn,
`ok=False`=recall failure). Provenance is stamped into each record's
`metadata={run_id, pipeline_version_id}` (M-06), bound by `runtime._make_memory`.
Extraction usage is captured immediately after the model call, so a failure still
bills the spend; the usage rides exactly one emitted event (`memory_recorded`, or
`memory_failed` when *every* write failed) so it's metered once even on total
failure. Each extracted write is isolated (`MemoryOutcome.ok=False` on any
partial/total failure → a `memory_failed` event) so one bad write can't skip the
rest. **Recording (including the extraction LLM call) runs AFTER the terminal
`run_completed` event** (`Pipeline.stream`), so a slow/hung extraction can never
delay or wedge a finished run — no timeout machinery needed. The backend still
meters/records these post-terminal events because `run_in_background` drains the
whole event stream; a live WebSocket that stops on `run_completed` just won't
*display* them (no durable billing/provenance data depends on that), and
`registry.publish` tolerates a run evicted between the terminal event and a late
memory event. On the backend, usage persistence goes through `_safe_record_usage`,
which isolates a `usage_records` write failure from run status. See
`docs/MEMORY_REVIEW_TRIAGE.md`.

`Pipeline.run/stream(input, *, user_id=None, memory=None)` recall a preamble
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
  `recall_preamble` (read, via `Pipeline._safe_recall`) are each wrapped so a
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
  (not left to `Pipeline._safe_recall`'s outer catch) specifically so
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
- **Memory is pipeline-scoped for episodic/procedural, org-scoped for
  semantic** (cross-pipeline memory scoping). Records also carry a
  `pipeline_id` (`PipelineRecord.id`, the stable team head — survives a
  redeploy, unlike `pipeline_version_id`, which is pure per-deploy
  provenance). `add`/`add_if_absent`/`search`/`all` accept it as a
  concrete-store extension exactly like `org_id`/`principal_id` (`None` =
  unfiltered). `MemoryManager.recall()` runs two scoped searches instead of
  one: `semantic` never receives `pipeline_id` (personal preferences stay
  shared across an org's pipelines); `episodic`/`procedural` do (one team's
  task experience doesn't leak into an unrelated team's context) —
  `pipeline_id=None` reproduces pre-existing, pipeline-agnostic behavior for
  SDK-direct callers and YAML-only demo pipelines (no `PipelineRecord`).
  `record_run`/`_extract_and_store` route `pipeline_id` into episodic/procedural
  writes only, never semantic. The backend binds it in
  `main.py::create_run` → `run_in_background` → `_make_memory` — see
  `ui/backend/CLAUDE.md`. No admin-API filter and no backfill of
  pre-existing (pipeline_id-NULL) rows; see
  `docs/superpowers/specs/2026-08-11-cross-workflow-memory-scoping-design.md`
  (spec filename kept as originally published — see `docs/STATUS.md`'s note on
  not rewriting history).
