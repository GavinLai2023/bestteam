# bestteam — `src/bestteam/core/`

Team Builder structured outputs, knowledge bases, and per-user memory. Root
`CLAUDE.md` has the project overview.

**Current invariants only.** Full KB reference: `docs/KNOWLEDGE_BASES.md`.
Reasoning: `docs/DECISIONS.md`. Per-feature narrative: the dated specs under
`docs/superpowers/specs/` cited below, and git history.

## Specification and Requirements

**Specification = loader schema + wizard-only friendly fields**
(`specification.py`). `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`/`PipelineSpec`/
`Specification` mirror the YAML loader's raw dict, plus presentation-only
`display_name`/`friendly_description` that **`to_raw()` strips before
validation**. `validate_specification()` compiles the stripped dict via
`_build_pipeline()` and raises `ConfigurationError`. `generate_specification()`
drives a "Solution Architect" via `with_structured_output(Specification)` and
self-corrects on `ConfigurationError` up to `max_attempts`.

**Requirements = Business Analyst's structured output** (`requirements.py`):
summary / pain_points / goals / success_criteria / constraints /
clarifying_questions. No `_build_pipeline` validation applies — it's a
plain-language summary, not yet a design. `to_prompt()` renders it for the
architect.

⚠️ **`current` is what makes a second round a refinement rather than a
restart.** Without it the prompt is `intent_text` + this round's `feedback`
alone, so each correction re-derives from the customer's original sentence,
silently undoes the one before it, and overwrites any hand-edited field. It
renders through `to_prompt()` and is placed *before* `feedback`; the system
prompt makes that meaningful — the current understanding is the customer's own
words and is preserved unless newer text contradicts it. Default `None` is what
the wizard's first generating call wants.

**`answers` activates the interview** (spec:
`2026-08-24-clarifying-questions-design.md`). `QuestionAnswer{question, answer}`
pairs render between `current` and `feedback` as a `Q:`/`A:` block. **A blank
answer renders as `_UNANSWERED_NOTE`** — the instruction to assume, record the
assumption in `constraints` prefixed `Assumed:`, and retire the question — so
one rendering carries the whole skip ruling with no separate mode parameter.
Asking policy: up to 4 questions that would most change what team gets built,
empty if the description covers it; plus the folding contract (fold answers,
never re-ask an answered/assumed question, keep open ones — which, with
`current` passed, is what stops regeneration overwriting the list).
`clarifying_questions` stays `List[str]`; answers live in the prompt and the
backend's `feedback_history`, **never in the schema**.

## Skills (`SkillSpec`, `AgentSpec.skills`)

A reusable instruction document plus the tools it depends on:
`{name, description, instructions, tools}`. `skills: List[str]` is a real
loader-level field — unlike the friendly fields, `to_raw()` keeps it.

`loader._build_pipeline` resolves `skills:` via an optional
`extra_skills: Dict[str, SkillSpec]` (mirrors `extra_tools`). Per agent:

- Unknown name → `ConfigurationError("Unknown skill '<name>'. Available: …")`.
- The skill's `tools` are appended to the agent's own (**agent's tools first**),
  de-duplicated preserving order, then resolved through the same `tool_lookup`.
- The skill's `instructions` are appended to `backstory`, one per skill in
  `skills:` order, joined by `"\n\n"`.

`validate_specification()`/`generate_specification()` take the same parameter.

## Knowledge bases

Three `type:`s, all backed by a folder of documents (`parse_file` + chunking):

| Type | Ranking | Extras needed |
|---|---|---|
| `local_folder` (default) | BM25 keyword (`rank-bm25`), in-memory, no API key | `tools-rag` |
| `vector` | cosine similarity over embeddings | `tools-rag-vector` |
| `hybrid` | both, fused by Reciprocal Rank Fusion | both |

⚠️ **Hybrid tie-break resolves to BM25, as a side effect rather than a policy.**
The legs are equal-weighted in the RRF formula, but `_rrf_retrieve` builds its
ranked lists BM25-leg-before-vector-leg and Python's stable sort keeps insertion
order on a tie. Most visible at small `top_k` with no reranker.

All three expose the KB to agents as an ordinary tool named after the KB, so it
slots into the existing `tools:`/`REGISTRY` mechanism with no adapter changes —
`query()` returns the same formatted string shape regardless of type.

### Retrieval and presentation are split

`search_hits(query, top_k) -> List[RetrievalHit]` is the abstract method each
subclass implements; `search()` and `query()` are **concrete on the base class**,
so citation tags cannot drift between types.

A `RetrievalHit` is the chunk plus *why it ranked there*: `fused_score` (what
candidates were ordered by), `leg_scores` (each leg's own raw score under its
name — `bm25`/`vector` — best value across expansion variants, so the keys say
which legs surfaced it) and `rerank_score` (`None` when no reranker is
configured or it failed and retrieval order was kept — **never faked**).

A `_Chunk` carries `source`, `text`, two optional location fields — `page` (PDF,
chunked per page, so `p.N` is exact) and `heading` (an 80-char approximation) —
rendered by `_citation()` as `[source: handbook.pdf, p.3 § Refunds]`, plus three
optional **identity** fields (`chunk_id`, `document_id`, `ingestion_job_id`) that
the backend's `from_chunks` path fills and the SDK path leaves `None`. **Identity
is never part of the citation tag**; it rides the hit and the trace. All five
default to `None`, so a two-field `_Chunk` renders byte-for-byte as before.

### The trace channel (`core/tool_context.py`)

`make_knowledge_base_tool`'s wrapper calls `search()` and `format_results()`
itself (instead of `query()`, which is exactly those two) so it can report the
retrieval through a contextvar-scoped box the adapter opens around each call
(`ToolCallContext(trace, usage)` — all no-ops when no run is active, so an
SDK-direct `kb.query()` is unaffected).

⚠️ **`citations` and `sources` are different fields and the difference is
load-bearing.** `sources` is de-duplicated and capped at 10 for the trace event.
`citations` is every `_citation` in rank order, unbounded and not de-duplicated,
because `core/grounding.py::check_grounding` needs the whole list — **it rides
the report solely for the grounding check and never reaches a trace event**, the
same way a KB tool's event never carries the indexed documents' own text.

The wrapper is marked `__bestteam_tool_kind__ = "knowledge_base"` (a marker, not
a name set — a KB tool is named after its KB), and the adapter builds
`tool_completed` from **only the named fields it knows about**
(`query`/`hit_count`/`sources`/`summary`/`ingestion_job_id`/`hits`).

⚠️ **A hand-written custom KB tool carrying the marker but not calling
`report_trace(..., citations=...)` will have every `[source: …]` tag in its
agent's answers reported unverified by grounding-lite.** Either report the field
or leave the marker off.

A KB built from chunks that all share one `ingestion_job_id` exposes it as
`kb.ingestion_job_id` (`None` for a folder or a mixed list), reported on every
search — hits or none — so a run's trace says which *generation* it was answered
from. All three types take an optional `description` (≤500 chars) injected into
the tool's docstring so a model can tell an org's collections apart.

### Eval harness (`core/kb_eval.py` + `scripts/kb_eval.py`)

`evaluate(kb, queries, top_k)` drives any KB type through `search()` and reports
recall@k / MRR / hit@1 **per source document** (one relevant document per query,
so recall@k == hit@k), plus an optional `expected_substring` hit@k over the
*text* of the expected document's own chunks — a match in another document
doesn't count, which is what catches a chunking regression.

Golden set: `tests/fixtures/kb_eval/` — 10 documents (5 EN / 5 ZH), 20 queries
split 16 `lexical` (BM25 must rank the document first) / 4 `paraphrase` (no
shared significant terms, so BM25 misses them by design — they are the headroom
a real embedding model should close, and why the guarded thresholds are
recall@3 ≥ 0.8 / MRR ≥ 0.7 rather than 1.0). `fake:` embeddings are
deterministic noise, so the hybrid test is a smoke test asserting no quality.
Real-model quality is gated by `tests/test_kb_eval_live.py` (`optional`
marker, self-skips without `OPENAI_API_KEY`, run by hand pre-release):
recall@3 floors for `vector`/`hybrid` under the deployment-default embedding
model, calibrated 2026-08-26 — paraphrase ≥ 3/4 is the load-bearing one.

Hard set: `tests/fixtures/kb_eval_hard/` — 11 documents / 17 queries across
four further kinds (`table`, `long`, `distractor`, `crosslingual`; the last
shares zero tokens with its English-only document, so BM25 scores 0 by
construction). `tests/test_kb_eval_hard.py` pins the $0 BM25 baseline; the
live gate holds the real-model floors. `tests/test_kb_eval_rerank_live.py`
(`optional`+`slow`, also needs `tools-rerank`) gates a REAL multilingual
cross-encoder (`BAAI/bge-reranker-base` — mmarco-mMiniLMv2 FAILED this gate
by preferring parallel translated documents): same floors reranked, plus
"reranking loses at most one query vs the same run unreranked" — and it
asserts `rerank_score` is populated first, because rerank failure is
fail-soft and would otherwise pass the floors without ever reranking.

### YAML

```yaml
knowledge_bases:
  - name: product_docs
    type: hybrid            # or vector; omit for local_folder
    path: ./docs/product    # relative to the pipeline YAML's directory
    embedding_model: "openai:text-embedding-3-small"  # or "fake:<dim>" for $0
    # optional: chunk_size (1000), chunk_overlap (100), top_k (5)
    # optional: description (<=500 chars, shown to the model)
    # optional: rerank_model / candidate_k       — see Reranking
    # optional: query_expansion_model / _count   — see Query expansion
    # optional: score_threshold — cosine cutoff in [-1, 1].
    #   vector: no chunk meets it -> "No results found".
    #   hybrid: filters ONLY the vector leg, so a chunk below it can still
    #           surface via BM25 — the guarantee does NOT hold.
    # optional: cache_path — JSON file persisting per-chunk embeddings, keyed by
    #   sha256(embedding-model spec + chunk text). Only applies to a string spec;
    #   a live Embeddings instance skips caching with a warning.
    cache_path: ./.bestteam_cache/product_docs.json

agents:
  - name: support_agent
    role: Support Specialist
    goal: Answer questions using the product documentation
    model: "openai:gpt-4o-mini"
    tools: [product_docs, calculator]
```

`embedding_model` resolves like `Agent.model`: an `Embeddings` instance is used
as-is, `"fake:<dim>"` (default 32) is $0 deterministic, other strings go through
`init_embeddings`. Runnable examples in `ui/backend/pipelines/*_demo.yaml`; the
`_live` variants need `OPENAI_API_KEY`.

### Query expansion (opt-in, all three types)

`query_expansion_model`/`query_expansion_count`: `query()` rewrites the query
into up to N alternative phrasings via one LLM call, searches with the literal
query plus every alternative, and fuses per-variant rankings with RRF
(`core/fusion.py`, shared with Memory) before slicing to `top_k`. Unset →
byte-for-byte unchanged. **A bad spec / invoke error / unparseable response
degrades to the literal query alone — a query never fails because expansion
failed.**

**The call is metered**: `_query_variants()` reads the response's
`usage_metadata` and reports it via `add_usage`, which the adapter drains onto
the calling agent's `agent_completed.usage`. An unparseable response is **still
metered** (the call was billed); a `fake:` model records nothing; a
`BaseChatModel` instance records tokens with `model=None`.

### Metering (`core/embeddings.py`)

- `estimate_embedding_tokens(text)` — one token per CJK character plus one per
  four other characters. **Embedding providers report no token usage through
  LangChain's `Embeddings` interface, so this has to estimate; expect ±30%.**
  Deterministic, no `tiktoken` dependency.
- `billable_spec(model)` — **the single definition of "is there anything to
  bill"**: a non-`fake:` **string** spec, else None. A live instance has no spec
  for `model_catalog` to price. Shared with `ui/backend/ingestion.py`.
- `report_query_embedding_usage()` — called from each `_vector_leg` right after
  `embed_query`, **once per query variant**, so expansion's extra variants are
  billed as the extra calls they are.
- `embed_documents_in_batches()` — **the one way this codebase embeds a list**
  (both `_embed_chunks` copies and `ui/backend/ingestion.py`; single-record
  writes and every `embed_query` are untouched). 100 texts per call, 3 attempts
  per batch (1s then 2s backoff), **only the failing batch retried**. Any
  exception retries — classifying provider taxonomies isn't worth it, so an auth
  failure waits 3s before surfacing. A wrong-length response raises immediately
  as `ConfigurationError`, no retry (a deterministic answer won't change).
  Neither constant is a parameter — no caller has a reason to differ.
- **Reranking is never metered** — a cross-encoder runs locally, no provider call.
- ⚠️ **Document embeddings are metered only on the upload/ingestion path.** A
  `vector`/`hybrid` KB built straight from a folder path — YAML-configured, or an
  uploaded one with no completed `IngestionJob` — embeds every chunk **in its
  constructor, at load time, outside any run and any job**. That spend is not
  recorded, and without `cache_path` it repeats on every load.

### Chunking is format-aware, not hierarchical

`_chunk_document` is the per-document entry point, shared by all three types and
by `ui/backend/ingestion.py`. Full detail: `docs/KNOWLEDGE_BASES.md`.

- **`.pdf`** — split on `PAGE_BREAK` (`\f`, the constant lives in
  `tools/file_parser.py` with the producer that writes it), each page chunked on
  its own, so a chunk never straddles a page. Costs cross-page overlap; accepted
  for an exact `p.N`.
- **`.xlsx`/`.xlsm`/`.docx`/`.csv`** — `_chunk_tabular_document` splits on the
  parser's `[Sheet: …]`/`[Table N]`/`[CSV: …]` markers and, for a block too long
  for one chunk, **repeats the marker and the assumed column-header row at the
  top of every chunk of that block** — otherwise the second chunk on has neither
  the sheet name nor the columns. Leading rows empty once commas and whitespace
  are stripped (`_row_has_text`) are skipped when choosing that header, because
  `read_only` openpyxl renders a spacer row as `,,` and repeating *that* would
  silently do nothing for a very common layout. Residual limit: a **non-empty
  title row** above the headers, which nothing here can tell from a header row.
- **`.docx`** takes its own branch (`_chunk_docx_document`) because its tables
  are interleaved with prose rather than appended. `_docx_segments` cuts into
  alternating prose / `[Table N]` segments, ending a table block at the blank
  line the parser puts after it — running each marker to the next one (what a
  workbook still does, since openpyxl legitimately renders blank rows) would file
  the paragraphs *between* two tables under the preceding table's citation. That
  terminator is unambiguous only because `_parse_docx_bytes` **drops a table row
  with no text in any cell**. Prose splits on the **Markdown** separators
  (`_MARKDOWN_SUFFIXES = {".md", ".docx"}`, since Word heading styles render as
  `#` lines); a table block deliberately does not, so a cell beginning `# ` can't
  become a heading boundary.
- **`.csv`** is in `_TABULAR_SUFFIXES` but **not** `_MARKDOWN_SUFFIXES` — a cell
  beginning `# ` must not open a section. It needs no branch: the parser renders
  it as one table block whose marker *is* its header line.
- **`.xml`** — `_chunk_xml_document` reads the element tree off the renderer's
  indentation and packs sibling subtrees greedily; a too-large subtree is opened
  one level, its element line joining the **ancestor path repeated at the top of
  every chunk**. The case it exists for is a flowchart exported as nested XML: a
  `<branch answer="No">` cut from its `<decision question="…">` is meaningless.
  The repeated path is **capped at half of `chunk_size`**, outermost dropped
  first, so content keeps at least half the chunk; an ancestor capped out of
  *every* descendant chunk is emitted once as content at its own level, since its
  opening line is the only copy of its attributes. A parent's mixed-content tail
  is its own section, never packed under the child it follows. The walk keeps its
  own stack rather than recursing (a valid document nests deeper than the Python
  call stack) and measures indents once (re-stripping was quadratic). **XML chunks
  get no character overlap** — the path is the cross-chunk context, every split
  lands on a whole line, and a raw slice would put a cut-open tag ahead of the
  path. Edge-based diagram exports (draw.io's `<mxCell source= target=>`) get no
  special treatment: their branches are id references, not nesting.
- **Everything else** goes through `_split_pieces` then `_apply_overlap` — the
  two halves `_chunk_text` is composed of — so a `.md` chunk's section heading can
  be read off the pieces *before* overlap prefixes each one with the previous
  chunk's tail, which would otherwise shift every heading by one section.

The repeated prefix comes out of the chunk's budget, so overlap shrinks and can
reach zero; a prefix leaving no room falls back to the ordinary path, still
tagged with the heading. A block with no readable row under its marker yields no
chunk at all, so table awareness can't reintroduce the content-free chunk P0-6
removed. `_MARKDOWN_HEADING_RE` is **imported from `tools/file_parser.py`**,
which writes that shape and escapes colliding Word prose — one definition, so
writer and reader cannot disagree.

Still unaddressed: small-to-big multi-level retrieval. Overlap is still a raw
character-slice of the previous chunk's tail, not structure-aware.

### Unreadable and mis-encoded documents

**An unreadable document is reported, not silently skipped.**
`_load_document_chunks` no longer filters unsupported suffixes out of its `rglob`
before the loop — it warns per file naming the type and the supported set. It
also warns on a document that parsed but contributed nothing:
`_has_extractable_text` strips parser-generated header lines
(`[PDF: …]`, `[Word: …]`, `[Excel: …]`, `[CSV: …]`, `[Sheet: …]`, `[Table N]`,
`[XML: …]`) and checks what remains, **because a scanned PDF parses to its header
line alone** — non-empty, so it used to become a chunk that matched nothing and
told nobody the pages were never read. Both helpers and `_NO_TEXT_MESSAGE` are
shared with `ui/backend/ingestion.py` (which raises them as per-document failures
instead of warnings), so SDK and upload path cannot disagree about why a file was
rejected.

**A mis-encoded plain-text document is still skipped, not ingested.**
`file_parser._decode_text` is shared with email attachments, which pull the
opposite way — an attachment must never fail a run (a sender can name anything
`.txt`), while a KB is better served by a warning than by chunks full of U+FFFD.
The decoder takes `lenient` and **only the attachment path turns it on**.
`_SUPPORTED_SUFFIXES` is an alias of `file_parser.SUPPORTED_SUFFIXES`, so a
suffix added to `parse_bytes` is discovered by folder scanning automatically.

### Reranking (opt-in, all three types, `core/reranking.py`)

`rerank_model` (a spec string or a live `Reranker`) and `candidate_k`. When set,
`query()` over-fetches `candidate_k` from the existing ranking (default
`top_k * 4`, clamped to `[top_k, 100]`), scores each candidate against the query,
and returns the top `top_k` by rerank score. `"fake:"` is deterministic and $0;
`"cross-encoder:<name>"` is a real local `sentence_transformers.CrossEncoder`,
cached at process scope by name (needs `tools-rerank`). Unset → byte-for-byte
unchanged.

**Fail-hard at construction, fail-soft at query time**: a bad spec or an
out-of-range `candidate_k` raises `ConfigurationError` at construction (like a
bad `chunk_size`); a rerank-time inference error logs a warning and falls back to
the pre-rerank slice — **rerank is a quality layer, never a reason a query
fails**. Spec: `2026-08-12-pluggable-rerank-design.md`.

### `from_chunks(...)` — the backend consumption pattern

All three classes expose a `from_chunks` classmethod building directly from
pre-parsed `_Chunk`s (and, for `vector`/`hybrid`, pre-computed vectors), skipping
parsing/chunking entirely. An upload-managed KB persists chunks and embeddings
once in the DB and reconstructs in-memory from those rows on every load.

**The SDK core stays entirely file-based and DB-free**: `from_chunks` takes plain
Python data, never a session or any backend concept, and the ordinary `__init__`
path is unchanged and still the only one CLI/SDK-direct/YAML callers use.

## Per-user memory (`core/memory.py`, `core/text_tokenize.py`)

Shares the CJK-aware tokenizer with the knowledge base (`text_tokenize.py`, so
the BM25 logic lives in one place).

**The tokenizer** lowercases, stems each Latin/digit token with
`snowballstemmer`'s English stemmer, and splits each maximal run of Han, kana or
Hangul into overlapping bigrams (a lone character becomes its own token).
Stemming conflates only inflections, never synonyms — that headroom is the
`vector`/`hybrid` types' job. Kana and Hangul sit in the *same* class as Han, so
a kanji+kana Japanese word is one run rather than two fragments cut at the script
boundary. `_STOPWORDS` is stemmed with the same stemmer, which also means a word
stemming *into* a stopword ("willing"→"will") is dropped from
`significant_terms` and stops counting toward the overlap gate — standard IR
behaviour, not a defect; BM25 scoring is unaffected.

`snowballstemmer` is a **soft import**: without it `_stem` is the identity, which
keeps `core/embeddings.py` working in an install with no RAG extra and keeps one
process symmetric on index and query side either way. The stemmer object is
**per-thread** (`threading.local`) because Snowball's `stemWord` mutates instance
state and the backend queries from a worker pool, and `_stem` is memoized —
stemming costs ~150× tokenizing, and without the cache indexing a chunk went from
0.10 ms to 16 ms, paid on every pipeline load. **Tokens are never persisted**, so
changing any of this needs no migration.

### Shapes

- **`Memory` ABC** — `add`/`search`/`all`/`delete` over `MemoryRecord`
  (`id, user_id, type, content, metadata, created_at`). ⚠️ **Management
  operations (`user_ids`, `user_summaries`, `delete_user`, `close`) are
  deliberately NOT on the ABC** — they're concrete on `SqliteBM25Memory` only, so
  a third-party store implementing just the four core methods still works. The
  same applies to every scoping dimension below.
- **`SqliteBM25Memory`** — stdlib `sqlite3` (own connection and DB file, no
  SQLAlchemy) + `rank-bm25` over `content`, same overlap-then-score ranking as
  `LocalFolderKnowledgeBase`. `search(..., max_candidates=N)` caps how many
  most-recent records get tokenized/indexed so an admin search does bounded work;
  **`max_candidates=None` (the default) keeps the full-store scan that per-run
  recall uses by design.**
- **`MemoryManager`** — execution-path glue. `recall_preamble(user_id, query)`
  formats top hits into a system-prompt block (`""` if none); `record_run` always
  writes one **episodic** record and, when `extraction_model` is set, makes one
  LLM call to also write **semantic** (facts) and **procedural** (how-handled).

Four types: **working** = the live `_TeamState` (not stored here);
**episodic**/**semantic**/**procedural** = rows tagged by `type`.

### Instrumentation and ordering

`record_run` returns `MemoryOutcome(recorded, extraction_usage)`; `recall`
returns `RecallResult(preamble, count)`. The extraction call's `usage_metadata`
is captured as a `{model, input_tokens, output_tokens}` entry so `Pipeline.stream`
can emit it on `memory_recorded` and the backend meters it — **the SDK never
touches the backend DB.** `Pipeline.stream` also emits `memory_recalled`
(`data`=count, 0 included) and a sanitized `memory_failed`
(`data`=`"recall"`/`"record"`) on failure. `Pipeline.run` surfaces the same on
`PipelineResult` (`.memory`, `.recall`).

⚠️ **Recall events precede the agents; recording events are emitted AFTER
`run_completed`** — so a slow or hung extraction can never delay or wedge a
finished run, with no timeout machinery. The backend still meters them because
`run_in_background` drains the whole stream; a live WebSocket that stops on
`run_completed` just won't *display* them.

Extraction usage is captured immediately after the model call, so a failure still
bills the spend, and **the usage rides exactly one emitted event** (`memory_recorded`,
or `memory_failed` when *every* write failed) so it's metered once even on total
failure. Each extracted write is isolated so one bad write can't skip the rest.
Provenance is stamped into `metadata={run_id, pipeline_version_id}`.

`Pipeline.run/stream(input, *, user_id=None, memory=None)` recall a preamble
(threaded through `_initial_state` → `_TeamState.memory_preamble` → each agent's
`extra_system_prompt`, so the cached compiled graph is reused with no recompile)
and record afterward. Both default None → unchanged behaviour.

### Known limitations

- **Dedup and consolidation.** Extraction dedups **exact** semantic/procedural
  content via `add_if_absent` — an atomic, **per-type** `INSERT ... WHERE NOT
  EXISTS` keyed by `(user_id, type, content, org-scope)`. Per-type means a
  semantic and a procedural row with the same text don't collide; atomic under
  SQLite's write serialisation means two connections can't both insert.
  **Semantic near-duplicate/update resolution** is implemented: before writing,
  `_extract_and_store` fetches the most relevant existing `semantic` memories
  (BM25, capped at 20, same org/principal scope) and shows them to the model with
  their ids; each fact carries an `action` (`"add"`/`"update"`/`"noop"`).
  ⚠️ **`store.delete` is only ever called for an id this call's own candidate
  search returned**, so a hallucinated `replaces_id` can never delete a real
  record — it falls back to a plain add, as does a missing/unrecognised `action`.
  A candidate-fetch failure degrades to "no candidates". **Semantic only** —
  `procedural`'s free-text shape makes near-duplicate judgment unreliable, so
  procedural consolidation is deferred, as is cross-run concurrency and any
  effectiveness measurement.
- **Bounds.** Recall scans the most-recent `recall_max_candidates` records
  (backend default 1000, clamped to SQLite's int range so a fat-fingered value
  can't `OverflowError` the `LIMIT`). Composite `(user_id, created_at)` /
  `(org_id, user_id, created_at)` indexes make the recall filter+sort
  index-covered; `(org_id, user_id, type, content)` makes the dedup check a seek.
  Episodic **retention** is opt-in (`BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER`):
  `record_run` prunes the oldest episodic rows beyond the cap, scoped to a
  **single** org — `org_id=None` means `org_id IS NULL`, **NEVER all-orgs**, so
  retention can't delete another org's history. Semantic/procedural are spared;
  unbounded by default. Age-based TTL, per-org quotas and a background sweep are
  deferred.
- **Graceful degradation for custom stores.** Extraction routes through
  `add_if_absent` **unless** the store overrides `add()` with custom policy
  without adopting `add_if_absent` — then `add()` is honoured for every write and
  dedup steps aside, so a pre-SP-4 subclass keeps intercepting. Prune is skipped.
- **Best-effort on both sides.** `record_run` and `recall_preamble` (via
  `Pipeline._safe_recall`) are each wrapped so a failure degrades rather than
  failing the run. The per-run store is closed in `run_in_background`'s `finally`,
  and **that close is itself best-effort** — a raising `close()` is logged, not
  propagated, so teardown can't escape the worker as an unobserved Future
  exception. `add` rejects a non-string/empty `type`, but **the type enum stays
  open** — a custom store may model other string types.
- **Recall is BM25-only by default.** Plain BM25 needs at least one shared
  significant term — a semantically identical but lexically disjoint memory
  returns nothing. `BESTTEAM_MEMORY_EMBEDDING_MODEL` enables a second,
  overlap-free leg: each write persists an embedding (**at write time — no
  backfill of pre-existing rows**), and `search()` fuses BM25 with cosine via RRF
  (`k=60`), then applies type-aware recency decay
  (`BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS`, default 14) — **SEMANTIC records
  never decay** (a stored fact doesn't get less true with age);
  EPISODIC/PROCEDURAL do. A misconfigured spec **disables memory entirely**, same
  as a bad `BESTTEAM_MEMORY_DB`. A row without an embedding simply doesn't
  participate in the vector leg.
- **Query expansion (opt-in)** lives in `MemoryManager`, not the store. One LLM
  call per `recall()` — **once per recall, not once per scope** — then each scoped
  search fans out across the literal query plus every expansion, fused by RRF.
  ⚠️ **Failure shape differs from `embedding_model` on purpose**: resolved lazily
  inside `_expand_query`'s own try/except, so a bad spec never disables memory —
  it falls back to the literal query. `count<=0` disables it even with a model set.
  **Cost**: 1 expansion call, but up to `2 × (1+E)` `store.search()` calls and, with
  hybrid also on, up to `2 × (1+E)` embedding calls — the one-entry query-embedding
  cache only helps when both scoped searches share the exact same text, which stops
  holding once there's more than one variant.
  ⚠️ **Deliberately NOT wired into two other `store.search()` sites**: the admin
  API (`memory_api.py` builds a `SqliteBM25Memory` directly — admin search wants
  precise literal lookup) and `_semantic_candidates` (a fuzzy-expanded candidate
  search risks a false "update" merge deleting a real fact). The expansion call's
  usage rides `RecallResult.expansion_usage`, **preserved even when the store
  search that follows a successful expansion fails** — `recall()` catches that
  internally rather than leaving it to `_safe_recall`'s outer catch, specifically
  so `expansion_usage` survives on the `ok=False` result.
- **Reranking (opt-in)** layers on the fused recall in `_fused_search`. Each
  scoped search fetches `rerank_candidate_k` (default `top_k * 4`, clamped)
  instead of `top_k` and scores the pool against the **LITERAL query only**
  (`queries[0]` — never an expansion variant, even when expansion is configured),
  then **re-fuses** the pre-rerank ranking with the rerank ranking via a
  **weighted** RRF (`weights=(1.0, 8.0)`) rather than taking the reranker's order
  outright — hand-derived so the cross-encoder's signal isn't diluted by
  equal-weight RRF while a very consistent pre-rerank candidate can still win on a
  wide disagreement. Resolution mirrors query expansion's lazy/fail-soft shape,
  not `embedding_model`'s eager/fail-hard one: resolved once per `MemoryManager`,
  and a bad spec disables rerank for that run rather than disabling memory.
  Deferred: no differentiated failure caching (every call for an unresolved spec
  retries, bounded to one per run) and no inference-time lock across concurrent
  cross-encoder calls (safe under PyTorch CPU threading, could contend for GPU
  memory — a contention failure just degrades to pre-rerank order).
- **Memory is org-scoped.** Records carry `org_id`; the run path binds the run's
  org so recall/record only ever touch that org (closing the username-reuse
  isolation gap). In the store a **concrete** `org_id` filters;
  **`org_id=None` means "across orgs"** — used only by the admin surface — and the
  **`LEGACY_ORG` sentinel (`"legacy"`) filters to `org_id IS NULL` only**, the
  third read scope, so the admin API can view just a NULL-org identity's legacy
  rows without falling back to `None`'s cross-org meaning. Pre-SP-2 rows have
  `org_id NULL` (no backfill — the username→org map lives in the main DB,
  unreachable from the store's connection); they aren't recalled by an org run but
  stay visible/deletable via the admin API.
- **Memory is principal-scoped.** Records carry `principal_id` — the backend's
  immutable, never-rotated `users.principal_id` — shaped exactly like `org_id`
  (persisted by `add`/`add_if_absent` and **included in the dedup key**; filters
  when concrete). A deleted-then-recreated same-`(org, username)` account gets a
  new principal and **can't recall the deleted account's rows**. A
  **`retired_principals`** table plus a **write-fence** in `add`/`add_if_absent`
  drops any write carrying a retired principal, so a run finishing after its
  account was deleted can't re-create rows behind the purge — **the shared SQLite
  file is the cross-process coordination point, so no drain fence or lock is
  needed.** Legacy NULL-principal rows aren't recalled by a stamped run;
  `assign_null_principal` (the opt-in `backfill-memory-principals` CLI)
  reconciles them. Spec: `2026-07-30-memory-principal-lifecycle-design.md`.
- **Episodic/procedural are pipeline-scoped; semantic is org-scoped.** Records
  carry `pipeline_id` (`PipelineRecord.id`, the stable head — survives a redeploy,
  unlike `pipeline_version_id`, which is pure per-deploy provenance).
  `recall()` runs **two** scoped searches: `semantic` never receives
  `pipeline_id` (personal preferences stay shared across an org's pipelines);
  `episodic`/`procedural` do (one team's task experience doesn't leak into an
  unrelated team's context). `pipeline_id=None` reproduces pipeline-agnostic
  behaviour for SDK-direct callers and YAML-only demos. No admin-API filter and no
  backfill of pre-existing rows. Spec:
  `2026-08-11-cross-workflow-memory-scoping-design.md` (filename kept as
  originally published).
- **Recalled memory is treated as untrusted reference, not escaped.**
  `recall_preamble` delimits recalled content (`<recalled_user_memory>`) and
  frames it reference-only to resist injection from a prior tool result that was
  stored, but there is **no content escaping/filtering engine** — a proportionate
  mitigation for the disabled-by-default, per-user model, not full hardening.
- Procedural memory is per-user (could be promoted to global/agent-level later).
- An **admin-only** Memory page allows view/search/delete, but **no manual
  add/edit**. `record_run` caps each field at `_MAX_RECORD_CHARS`. Total growth is
  bounded only when episodic retention is enabled.
