# Knowledge Bases & RAG

How bestteam connects an agent to a client's documents — the most common
customer request ("hook our agents up to our internal docs"). This page
explains all three implementations end to end: how they work, how to configure
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

Files with an unsupported extension, that fail to parse, or that yield **no
extractable text**, are skipped with a `warnings.warn(...)` naming the file
and the reason — the knowledge base still builds from whatever did parse.
"No extractable text" is a real case, not a theoretical one: every parser
prefixes its output with a bracketed header line (`[PDF: report.pdf — 3
page(s)]`, `[Sheet: Q3]`, `[Table 1]`, …), so a **scanned PDF** — pages that
are images, with no text layer — parses to that header and nothing else.
`_has_extractable_text()` strips the headers the parsers generate and checks
what is left, so such a document is reported rather than indexed as a chunk
that matches no query. Reading a scanned document needs OCR, which is not
supported (see "Known limitations").

**Chunking**: each document's text is split into chunks of up to
`chunk_size` characters with `chunk_overlap` characters shared between
consecutive chunks (default `1000`/`100`). `chunk_overlap` must be
non-negative and strictly less than `chunk_size`, or the knowledge base
raises a `ConfigurationError` at load time.

Chunking is **format-aware, not a fixed character slice**: `_chunk_text`
(shared by all three KB types) splits on the document's own structure —
Markdown heading boundaries and a generic paragraph/sentence/word fallback
(with CJK sentence terminators `。！？`) — before falling back to a raw
slice, so related content tends to stay together in one chunk instead of
being cut mid-paragraph; XML is chunked along its element tree (see "XML
`heading`" below).
`chunk_size` is still enforced as a hard ceiling either way. This is
still single-level (not "small-to-big" hierarchical/parent-child)
chunking — see "Known limitations" below.

**Chunk location metadata.** `_chunk_document()` tags each chunk with
whatever location its format makes exact, which is what a citation is built
from (see "Citations", below):

- **PDF `page`.** `_parse_pdf_bytes` joins a document's pages with a form
  feed (`\f`) rather than a blank line, and a `.pdf` is chunked **per page**,
  so no chunk straddles a page break and its `p.N` is precise. The cost is
  that a paragraph running across a page boundary loses the overlap it would
  otherwise borrow — accepted, because a citation an operator can check beats
  another hundred characters of context.
- **Markdown `heading`.** A `.md` chunk records the `#`..`####` section it
  opens under, capped at 80 characters. Approximate by design: nothing here
  parses Markdown, so a `#` line inside a fenced code block reads as a
  heading, and a chunk spanning two sections is labelled with the one it
  starts in.
- **Table `heading`, plus a repeated header row.** `.xlsx`/`.xlsm`/`.docx`
  are chunked table block by table block, splitting on the marker lines the
  parser itself writes (`[Sheet: Q1]`, `[Table 2]`). A block that fits inside
  `chunk_size` stays one chunk; a longer one has its marker line **and its
  first body row repeated at the top of every chunk**, so a row in the tenth
  chunk still says which sheet it came from and what its columns mean. The
  marker also becomes the chunk's `heading` (`Sheet: Q1`, `Table 2`), capped
  at the same 80 characters, so the chunk cites `sales.xlsx § Sheet: Q1`
  rather than the filename alone. Two caveats: the first body row *with
  anything in it* is *assumed* to be the column header — leading rows that
  are empty once commas and whitespace are removed are skipped, because a
  spacer row above the headers parses to `,,` and repeating that would say
  nothing, but a **non-empty title row** above the headers can't be told from
  a header row and does get repeated, as does a table whose first row is
  already data — and the repeated prefix comes out of the
  chunk's budget, so the body of each chunk is smaller and the overlap
  borrowed from the previous chunk shrinks (to zero if the prefix is long).
  A `.docx`'s body paragraphs, before its first table, chunk the ordinary way
  and carry no heading. A block with no readable row under its marker — an
  empty sheet, such as a workbook's untouched trailing `Sheet2`, or a
  formatted-but-empty one whose rows are bare commas — yields no chunk
  at all, so it can't become the content-free chunk described above. Two
  knock-on effects: a workbook of many small sheets now yields at least one
  chunk per sheet where the sheets used to be packed together, so a
  `vector`/`hybrid` collection embeds (and pays for) more chunks than before,
  and repeating the header row in every chunk of a long sheet makes the column
  names common across the corpus, slightly diluting their BM25 IDF.
- **XML `heading`, plus a repeated ancestor path.** An `.xml` is chunked
  along its element tree, read off the parser's own rendering (one line per
  element, two spaces of indent per level). Sibling subtrees are packed
  together while they fit; a subtree too large for one chunk is opened up one
  level, and **the element lines on the path from the root down to the
  chunk's content are repeated at the top of every chunk** — the XML
  counterpart of the repeated sheet marker and header row. This is what makes
  a flowchart or decision tree exported as nested XML usable: a
  `<branch answer="No">` cut away from its decision still arrives with
  `<decision question="Is the item unopened?">`, and with every branch and
  decision above that, so the agent reads the branch in context rather than
  as an orphan. The nearest ancestor becomes the chunk's `heading` — the
  `tag attr="…"` part of its line, capped at 80 characters — so the chunk
  cites `refund_flow.xml § decision id="3" question="Is the item unopened?"`.
  The repeated path is capped at half of `chunk_size` — outermost ancestors
  dropped first, so content always keeps at least half the chunk — and an
  ancestor that every descendant chunk had to drop is emitted once as content
  at its own level, since its opening line is the only copy of its attributes
  and inline text. A parent's own mixed-content text (`<root><label>…</label>
  root description</root>`) stays with the parent, never packed or cited under
  the child it happens to follow. Three caveats. XML chunks carry **no
  character overlap**: the repeated path is the cross-chunk context, every
  split lands on a whole line, and a raw slice of the previous chunk would put
  a cut-open tag ahead of the path. A single leaf too large for its path, or
  an element whose opening line is too long to head a path at all, falls back
  to the ordinary paragraph/sentence/word split under whatever path did fit.
  And the tree that is repeated is the *nesting*: a diagram
  tool that exports nodes and edges as a flat list (draw.io's `<mxCell
  source="3" target="7">`) has its branches in id references, which nothing
  here reconstructs — export such diagrams as nested XML, or as a text
  outline, instead. A document that fits one chunk is byte-for-byte what it
  was. Practical advice for flowcharts: one flow per file, attribute names
  that carry meaning (`question=`, `answer=`), and a `chunk_size` large enough
  that a whole branch usually fits under its path.

Every other format supplies neither, and such a chunk cites its filename
alone.

## Citations

A retrieved chunk is rendered by one shared formatter
(`knowledge_base.py::format_results`, used by all three types via the base
class's concrete `query()`), so the string an agent sees cannot drift between
knowledge base types:

```
Knowledge base 'product_docs' results for: refund window

1. [source: handbook.pdf, p.3 § Refunds]
Refunds are issued within 30 days of purchase…
```

The tag is `[source: <filename>]`, plus `, p.<N>` when the chunk carries a
page and ` § <heading>` when it carries a section — a Markdown heading, or a
spreadsheet sheet / Word table (`[source: sales.xlsx § Sheet: Q1]`). A chunk
with neither renders exactly as it always did. The tool's own description tells the model
to quote that same tag back when it uses an excerpt, which is what makes a
model's answer checkable against the document.

**`description`** (optional, all three types, max 500 characters) is one
sentence saying what the documents cover. It goes into the agent tool's own
description — `Search the 'product_docs' knowledge base: Refund, delivery and
warranty policies. Use it whenever…` — so a model can tell an org's
collections apart, and into the Solution Architect's knowledge base catalogue
so it assigns the right one to the right agent. Unset, the tool description
is the generic wording and nothing else changes. The wizard's "Your
documents" step asks for it ("What's in these documents? (one sentence)"),
and both upload endpoints accept it as a `description` field.

## `local_folder`: BM25 keyword search

`LocalFolderKnowledgeBase` indexes every chunk with [BM25](https://en.wikipedia.org/wiki/Okapi_BM25)
(via the `rank-bm25` package) — a classic keyword-ranking algorithm, no
embeddings or external services involved.

Querying:
1. Tokenize the query into lowercase alphanumeric words, dropping common
   English stopwords ("the", "and", "is", …). Latin/digit words are **stemmed**
   ("refunds" → "refund"), and each run of Han, kana or Hangul characters is
   split into overlapping bigrams — the same `core/text_tokenize.py` that
   indexed the chunks, so the index and the query always agree (see
   "Evaluating retrieval" below for what that does and doesn't conflate).
2. Score every chunk with BM25.
3. Rank candidates by *(number of shared significant terms, BM25 score)* —
   chunks that share zero significant terms with the query are excluded
   entirely, so tiny corpora don't return noise just because BM25's
   statistics are unstable at small scale.
4. Return the top `top_k` chunks (default `5`), each tagged with its
   citation (see "Citations" above).

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
  on every pipeline load. Caching only works when `embedding_model` is a
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

See `ui/backend/pipelines/hybrid_knowledge_base_demo.yaml` for a $0 dry-run
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

This call **is metered** — see "What a knowledge base costs, and how it is
metered", below.

## What a knowledge base costs, and how it is metered

Three things a knowledge base does cost money, and they land in the one
`usage_records` ledger the org's monthly spend cap already sums over — with
one gap on the non-upload path, noted under the table:

| Spend | When | How it is recorded |
| --- | --- | --- |
| Ingestion embeddings (`vector`/`hybrid`) | Once per upload, ingestion path only | One row per ingestion job: `agent="kb:ingest"`, `run_id` NULL, `ingestion_job_id` set |
| Query embedding (`vector`/`hybrid`) | Once per query variant, per run | Rides the calling agent's own usage, so it is a normal run row |
| Query expansion (all three types) | Once per query, when `query_expansion_model` is set | Same: a normal run row under the calling agent |

Reranking costs **$0** and is deliberately not recorded: the cross-encoder
runs locally, in-process, with no provider call to bill.

**Only the upload/ingestion path's document embeddings are metered.** A
`vector`/`hybrid` knowledge base constructed directly from a folder path —
a YAML-configured KB (`core/loader.py`), or an uploaded one with no completed
ingestion job, which the backend falls back to building from files — embeds
every chunk at **load** time, outside any run and outside any ingestion job,
and that spend is **not** recorded. Without `cache_path` it re-embeds on every
load, so the unrecorded cost repeats. Query-time spend is metered for these
knowledge bases exactly as for any other.

**Token counts for embeddings are estimated, not reported.** LangChain's
embeddings interface returns vectors and nothing else — no provider reports
token usage for an embedding call the way a chat model does — so
`bestteam.core.embeddings.estimate_embedding_tokens()` counts one token per
CJK character plus one per four other characters. Expect it to be within
about **±30%** of the provider's own count: enough to keep a spend cap
honest, not enough to reconcile against a bill. Query-expansion tokens are
*not* estimated — that is a chat model, and its reported `usage_metadata` is
used directly.

**A retried batch is not billed twice.** Document embedding goes out 100
chunks at a time, and a batch that fails gets up to three attempts — two
retries, 1s then 2s apart — with only the failing batch retried, so a
provider hiccup partway through a large upload no longer throws away the
chunks already embedded and paid for. The ingestion row's token estimate is computed once from the chunk
texts (in `ui/backend/ingestion.py`, after the embedding call returns), so a
retry adds nothing to it. That cuts the
other way too: the estimate counts each chunk once even though a retried batch
was genuinely charged more than once by the provider — one more reason it is an
estimate, not a bill.

**Nothing billable, nothing recorded.** A `"fake:"` spec is $0 by
construction and an `Embeddings`/`BaseChatModel` instance passed in from code
has no spec string for the catalog to price, so neither produces a row. A
`local_folder` KB embeds nothing at all.

**Pricing an embedding model.** Give it a `model_catalog` entry whose `tier`
is `"embedding"` (`PUT /api/config/model-catalog/<spec>`, admin-only), with
`input_price_per_1k` set to the provider's price and `output_price_per_1k`
left at `0`. That tier is what keeps it out of every place a *chat* model is
offered — the wizard's catalog, the Solution Architect's prompt, smart
search's default expansion model — so nobody can hand an embedding model to
an agent. No embedding entry is seeded by default: prices depend on the
provider you actually use. Without one the calls are still recorded, just
with a NULL `cost_estimate` (the same "we ran it but cannot price it"
signal any unlisted model gets).

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

Knowledge bases are declared under a pipeline's `knowledge_bases:` key, then
referenced by name in an agent's `tools:` list — `core/loader.py` builds
each one and registers it in the same tool lookup table used for built-in
tools.

**`local_folder` (default type):**
```yaml
knowledge_bases:
  - name: product_docs
    path: ./docs/product   # resolved relative to the pipeline YAML's directory
    description: Refund, delivery and warranty policies   # optional, max 500 chars
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

Runnable examples in `ui/backend/pipelines/`:
- `knowledge_base_demo.yaml` — `local_folder`, no setup needed.
- `vector_knowledge_base_demo.yaml` — `vector` with `"fake:"` embeddings, $0.
- `vector_knowledge_base_demo_live.yaml` — `vector` with real OpenAI
  embeddings + chat model, demonstrating true semantic retrieval. Requires
  `OPENAI_API_KEY` and costs real API quota.
- `hybrid_knowledge_base_demo.yaml` — `hybrid` with `"fake:"` embeddings, $0.

## How agents use a knowledge base

`core/loader.py::_build_pipeline` builds every entry under
`knowledge_bases:`, wraps it with `make_knowledge_base_tool(kb)`
(`core/knowledge_base.py`) — which just gives the KB's `query` method a
`__name__` matching the KB's `name` plus an auto-generated docstring — and
adds it to the same tool lookup dict used for `web_search`/`calculator`/etc.
An agent's `tools:` list resolves names from that combined dict, so
referencing a knowledge base looks identical to referencing a built-in
tool. At runtime, `adapters/langgraph_adapter.py`'s tool-calling loop binds
all of an agent's tools to the model and dispatches by name when the model
calls one.

Two things hold the agent to the knowledge base rather than merely offering
it (grounding-lite, 2026-08-24). First, an agent that carries a knowledge-base
tool makes its **first model call with `tool_choice="required"`** on every
team mode — the same insurance a hierarchical manager and a delegated
specialist already had — so the model searches before it answers instead of
answering from what it remembers. A provider that rejects a forced
`tool_choice` (DeepSeek's thinking mode does) gets the unforced call instead,
so the worst case is today's behaviour, never a failed run. An agent whose
only tools are `web_search`, `calculator` and the like is not forced. Second,
when the turn ends, the `[source: …]` tags in the agent's final text are
**checked against the citations its own searches returned** and the result is
recorded as one `grounding_checked` trace event:

```json
{
  "searches": 1,
  "hit_count": 3,
  "cited": 2,
  "verified": 1,
  "unverified": ["handbook.pdf, p.99"]
}
```

A tag is verified when it equals a returned citation (whitespace aside), or
when it names only a filename and that document was among the hits. Anything
else is unverified — which is not the same as fabricated. It means the tag
does not exactly match a returned citation and is not a bare filename among
the hits; that includes a genuinely fabricated locator, but it also includes
a passage the agent really did retrieve, cited with a page or heading it
dropped or altered along the way (e.g. citing `handbook.pdf, p.3` for a hit
the search returned as `handbook.pdf, p.3 § Refunds`). The event carries
counts and citation labels only (at most ten unverified labels, each at most
200 characters). Under the default policy it **records rather than acts**:
the answer is returned unchanged, nothing is retried or refused — see
"Grounding policy" below for the opt-in enforcement. A knowledge-base agent that never
searched (`searches: 0`) has every tag unverified. A hierarchical manager
without a knowledge base of its own is not checked — its specialists are. A
manager that *does* carry its own knowledge-base tool is checked, but only
against **its own** searches: a tag it retells from a subordinate's answer
(the subordinate's own citation, quoted or paraphrased back by the manager)
is reported unverified, because cross-agent checking is out of scope for this
feature — a documented consequence, not a bug.

Known limitation: a citation label containing its own `]` (an unusual
document heading like "Item [2]") truncates the tag the check parses, so that
citation is reported unverified even when it is exact. Not worth a regex
change for a records-only check.

Known limitation: a filename containing `, p.` or ` § ` (e.g. an upload named
`report, p.2.pdf`) is misread as a label with a locator, because the check
splits a label at the first occurrence of either marker without knowing where
the filename actually ends. This cuts both ways: `[source: report, p.2.pdf]`
for that document is reported unverified even when cited correctly, because
the check treats `p.2.pdf` as a locator rather than part of the filename; and
a bare `[source: report]` tag for the same document can be counted verified
by the filename-only rule even though the tag omits the true document name.
Carrying the filename separately through the tool→adapter→checker contract
would fix it, but only for a check that records and never acts — not worth
the payload-shape change.

### Grounding policy: `observe` | `retry` | `refuse`

What an agent's turn *does* with a failed check is a per-agent, opt-in
setting (2026-08-26, spec `2026-08-26-grounding-policy-design.md`):

```yaml
agents:
  - name: support_agent
    role: Support Specialist
    goal: Answer questions from the product documentation
    model: "openai:gpt-4o-mini"
    tools: [product_docs]
    grounding_policy: refuse   # or retry; omit for observe (the default)
```

An answer **passes** when it cites at least one source and every cited label
is verified against the turn's own searches; anything else — no `[source: …]`
tags at all, or any unverified tag — fails.

- **`observe`** (default): record only. Byte-for-byte the behaviour described
  above, and the `grounding_checked` event keeps its exact five-field shape.
- **`retry`**: one corrective model call on the same conversation — the
  search results are already in the turn's tool messages, so the model is
  told to rewrite citing only returned sources, or to say the knowledge base
  does not contain the answer. The retried answer is returned even if it
  still fails. A retry that asks for more tools instead of answering counts
  as a failed retry. Exactly one retry, metered like any other model call.
- **`refuse`**: same single retry; if the retried answer still fails, a fixed
  refusal text (`GROUNDING_REFUSAL_TEXT` in `core/grounding.py`) is returned
  instead of the answer. Note the reach of this: an agent whose searches
  returned nothing cannot produce a passing answer, so under `refuse` a
  question the knowledge base cannot answer gets the refusal text — the
  wanted behaviour for a high-risk (prices, policy, compliance) agent, and
  the reason `refuse` is opt-in per agent rather than a global switch.

With `retry`/`refuse` the `grounding_checked` event describes the **final**
answer and adds three fields: `policy`, `retried`, `refused`. On the
streaming path, text that already streamed to a viewer is discarded (the
stream-reset control code) before the corrected answer streams or the
refusal is returned — the authoritative answer is still `run_completed`'s.

What the policy still does **not** do: judge whether a cited passage actually
supports the claim (no claim-level entailment, no grader model), check a
pipeline's final merged output (the check is per agent turn), or pin the
agent to one *specific* knowledge base when it carries several tools.

### What a search looks like in the trace

Every knowledge base search shows up in the run's trace as a `tool_completed`
event that records **what was asked and where the answer came from, never the
answer text**:

```json
{
  "tool": "product_docs",
  "success": true,
  "duration_ms": 12,
  "summary": "2 result(s) for “refund window” — sources: handbook.pdf, p.3 § Refunds, policies.md",
  "query": "refund window",
  "hit_count": 2,
  "sources": ["handbook.pdf, p.3 § Refunds", "policies.md"],
  "ingestion_job_id": 42,
  "hits": [
    {"citation": "handbook.pdf, p.3 § Refunds", "chunk_id": 911, "document_id": 37,
     "fused_score": 0.0328, "leg_scores": {"bm25": 4.1, "vector": 0.82}, "rerank_score": 2.7},
    {"citation": "policies.md", "chunk_id": 904, "document_id": 35,
     "fused_score": 0.0161, "leg_scores": {"vector": 0.61}, "rerank_score": -0.4}
  ]
}
```

That is enough to answer the questions an operator actually asks — did the
agent search at all, what did it search for, did anything match, which
documents did it draw on, **which generation of the collection answered, and
why each hit ranked where it did** — while the excerpts themselves stay out
of the `trace_events` table, the monitoring dashboard and any log that
renders one. `ingestion_job_id` is the completed job whose chunk rows the
collection was built from (`null` for a folder-built knowledge base), present
even on a zero-hit search so the record still says what was searched. Each
entry of `hits` names the `knowledge_chunks` row and its document, the
reciprocal-rank-fusion score candidates were ordered by (`fused_score`), each
retrieval leg's own raw score under the leg's name (`bm25`: Okapi; `vector`:
cosine — its keys are *which legs found it*, kept at the best value across
query-expansion variants), and the cross-encoder's `rerank_score` when a
reranker ran (`null` when none is configured, or when scoring failed and
retrieval order was kept). Scores are rounded to four decimals; no model
names appear. A query longer than 200 characters is truncated, and at most 10
sources and 10 hits are listed. Nothing here is parsed out of the tool's
return string: the tool reports these fields directly
(`core/tool_context.py`), and the adapter knows not to fall back to
summarising the result text for a knowledge base tool.

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
  directory too). Refused with `409` while an upload for that knowledge base
  is still processing — wait for the ingestion job to finish, then delete.

**File upload** (`POST /api/config/knowledge_bases/{name}/upload`,
`ui/backend/crud.py`) is a `local_folder`-only convenience that lets a
non-technical user create a knowledge base by uploading files directly,
with no server filesystem access needed:
- Accepts a multipart `files` list plus optional `chunk_size`/`chunk_overlap`/
  `top_k`/`description`.
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
admin upload endpoint — but it also accepts a `description` (the wizard's
"What's in these documents?" sentence) and a `smart_search` flag. The
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

**Adding documents, and what a second upload costs.** An upload is
`mode="replace"` (the whole collection becomes what this upload contains) or
`mode="add"` (this upload joins what is already there; a file whose *name*
matches one already in the collection is replaced by it, since two documents
of the same name would be two answers to the same question). Names are matched
**case-insensitively**, because the filesystem the staged copies land on may
be: `Policy.txt` and `policy.txt` are one file on Windows and macOS, so
treating them as two would carry the old one on top of the newly uploaded one
and leave the collection holding the previous text under the new name. The self-service
route treats the field as the confirmation too: sending no `mode` against a
name that already exists is the `409` that asks the customer which they meant,
and the wizard's dialog offers both.

"Add" is why a collection can hold more than one upload's worth of files: the
per-upload file limits above still apply per request, and
`_MAX_DOCUMENTS_PER_KB` (30 self-service, 200 admin) bounds the merged result.
It is implemented by staging the live generation's files alongside the new
ones, so the resulting ingestion job still owns a complete document set and
nothing about the atomic swap, pruning or retention changes.

Restaging is not a re-index. A document whose bytes are unchanged
(`KnowledgeDocument.content_hash`) keeps the chunks — and the **embeddings** —
the previous completed job already produced and paid for; only genuinely new
or changed documents are parsed, embedded and metered. That reuse is refused,
and everything re-embedded, whenever the previous job's `kb_type`,
`embedding_model`, `chunk_size` or `chunk_overlap` differ from this one's, so
switching the Standard/Enhanced toggle still re-indexes from scratch as
described above. A job created before this feature existed records no chunk
parameters and is never reused: the first upload after that upgrade re-embeds
once.

**Removing one document** (`DELETE /api/org/knowledge-bases/{name}/documents/{filename}`,
self-service) is the same pipeline with no new files: the live generation is
staged into a fresh version directory minus the named file and a new job
ingests the staged set under the live job's own shape and chunk parameters,
so every remaining document reuses its chunks and embeddings and nothing is
re-parsed, re-embedded or metered. It answers `202 {"name", "job_id",
"status": "queued"}` like an upload — poll the job the same way; the
collection keeps answering from its current documents until the new
generation completes. The name must match the document's exactly (as the
summary lists it — see `documents` below). Refusals: `404` for a name that is
not in the collection (naming it), `409` while an upload is still processing
(whichever job finished last would otherwise become live, and a removal built
from the previous generation could silently undo the upload), `409` for the
last document (an empty collection cannot be built; delete the collection
instead), and `409` when the live generation's files are no longer on disk.
Removing is allowed while teams search the collection — so is adding — and
the panel names those teams in its confirmation. A `failed` document (one the
ingester could not read) can be removed too: its file is otherwise carried by
every `add` and reported as skipped each time. There is no admin route for
this yet; an operator uses the org's own route or a `replace` upload.

**Org self-service listing and deletion** (`GET /api/org/knowledge-bases`,
`GET`/`DELETE /api/org/knowledge-bases/{name}`) is the same org member's view
of what they uploaded, without an admin having to be involved. Each entry
carries `name`, `description`, `type`, `updated_at`, `used_by` (the deployed
teams whose current version depends on it), `servable`, `documents` (the live
generation's documents, sorted by name: `{"filename", "status", "size_bytes"}`
each, every status — empty until a first upload completes), and `latest_job` --
the newest ingestion attempt of any status, so a *failed* upload's error text
reaches the person who made it. `latest_job` deliberately omits the `config`
the per-job route returns: it carries the server's absolute upload path, and
this is a customer-facing list. `servable` is not "the latest job succeeded" --
a failed re-upload leaves the previous completed generation live, and a
knowledge base with no jobs at all is a legacy/manual-path one served from
disk. `DELETE` returns `204` and shares `knowledge_bases.delete_knowledge_base`
with the admin route, so it is refused with the same `409` while a deployed
team still depends on it or an upload is still processing, and a cross-org
name is a `404`.

A knowledge base whose *newest* ingestion job failed is stuck until someone
re-uploads or deletes it, so `resolve_knowledge_base` says so
("could not be indexed: <the job's own error>. Upload the documents again, or
delete it.") rather than the "wait for the current upload to finish" wording,
which was permanently wrong advice for that case. The wizard's
pre-Specification catalogue (`builder.py::_all_knowledge_base_tools`) skips an
unresolvable knowledge base with a logged warning instead of raising -- it
builds *every* one of the org's knowledge bases, so one customer's unparseable
upload used to fail spec generation for the whole org -- and the architect is
only told about the ones that actually built. The pipeline-build path
(`load_knowledge_base_tools`) still fails closed: there a broken knowledge base
is one an agent actually references.

**Wiring into a pipeline**: a pipeline's `_build_pipeline()` validation only
builds the standalone knowledge bases its agents actually reference by name
(`ui/backend/knowledge_bases.py::load_knowledge_base_tools`) — not every
knowledge base in the database — since building one means re-reading and
re-chunking files (and, for `vector`, calling an embedding model).

### Trying a search

`POST /api/org/knowledge-bases/{name}/search`
(`ui/backend/org_knowledge_bases.py`) runs one query against the org's own
collection and returns up to `top_k` of the passages an agent would rank
first — the "Try a search" panel behind each row of the "My documents" list.
Not quite the agent's own result set — the panel always sends `top_k=5`,
whatever the collection's configured `top_k` is — but that is the only
divergence: this is the same `search_hits()` an agent's tool calls, so the
collection's own query expansion and reranking run here too. Body:
`{"query": "...", "top_k": 5}`, with `query` between 1 and 500 characters and
`top_k` between 1 and 10; anything else is a `422`. Response:

```json
{
  "query": "refund policy",
  "hit_count": 2,
  "ingestion_job_id": 42,
  "results": [
    {
      "citation": "handbook.pdf, p.3 § Refunds",
      "source": "handbook.pdf",
      "page": 3,
      "heading": "Refunds",
      "text": "…at most 1500 characters of the passage…",
      "chunk_id": 911,
      "document_id": 37,
      "fused_score": 0.0328,
      "leg_scores": {"bm25": 4.1, "vector": 0.82},
      "rerank_score": 2.7
    }
  ]
}
```

`citation` is exactly what an agent's own tool output cites (see "Citations"
above), so the panel and the model name the same passage. `text` is capped at
1,500 characters: enough to judge the retrieval by, and not a way to page a
whole collection out through a search box. `ingestion_job_id`, `chunk_id`,
`document_id` and the three score fields are the same identity and scores the
agent's trace event records (see "What a search looks like in the trace") —
the panel does not display them today, but the JSON lets an operator tie a
passage to its row and generation. No model name is in the response.

The other status codes: a name belonging to another org is a `404` (never a
`403` — existence is never revealed); a collection that cannot answer yet is
a `409` whose message says which case it is (still processing, the last
upload failed, or — for a legacy knowledge base never uploaded through the
app — it cannot be searched here at all, since rebuilding it would re-parse
every file from disk, and re-embed a `vector` one unmetered, on every click);
and a provider failure during the search itself is a `502`. A
`ConfigurationError` that is *not* one of those readiness cases (a missing
optional extra, a bad `rerank_model`) stays a logged `500` — it is an
operator's deployment problem, and answering it with "wait and try again"
would be wrong.

**A test search is metered like any other spend.** A `vector`/`hybrid`
collection embeds the query, and any of the three types may make a
query-expansion call, so whatever the knowledge base reports through
`core/tool_context.py` is written to `usage_records` with `agent="kb:search"`
and **both** `run_id` and `ingestion_job_id` NULL — the row belongs to no run
and to no upload, and the org's monthly spend cap
(`SUM(cost_estimate) WHERE org_id`) counts it without a second query. The
spend is recorded on the failure path too, because a query expansion is paid
for before the embedding call that raised. Recording is best-effort: a
metering failure is logged and never turns a successful search into an error.

There is deliberately **no cache and no rate limit**. The money at stake is
negligible — one short query embedding — and the metering above *records*
that spend, it does not bound it. The real cost is CPU: every call rebuilds
the knowledge base from its `KnowledgeChunk` rows (a `hybrid` one also
`json.loads`es every stored vector) on the backend's sync threadpool, which
is sub-second to a few seconds at the tens-to-hundreds-of-documents scale
this beta is sized for, with a person clicking a button rather than an agent
loop on the other end. A cache would have to be invalidated by every
re-upload to buy correctness this does not need. Both are worth revisiting if
the button is ever abused.

This answers one query at a time. For judging a collection as a whole — and
for telling whether a change to it helped — see "Evaluating retrieval" below.

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

A whole-job failure (e.g. the embedding call itself raised, with no
per-document failures) has no `KnowledgeDocument` rows to report, so
`errors` instead holds a single `{"filename": null, "error": "..."}` entry
carrying the job-level error.

**Per-document partial failure**: one bad file (an unsupported file type,
fails to parse, no extractable text, or produces zero chunks) doesn't fail
the whole job — it's recorded as a `failed` `KnowledgeDocument` row (capped
error text) and the job continues with the rest. Every staged file gets a
document row, so `documents_succeeded + documents_failed == file_count`
always holds: a file the ingester cannot read is *reported* to the customer
in the job's `errors` list ("Unsupported file type '.png'. Supported: …", or
"No text could be extracted from this file. If it is a scanned PDF it needs
OCR, which isn't supported yet."), never silently dropped between the upload
count and the job totals. The job itself only fails outright if *every*
document failed (so the
KB would have zero chunks) or, for `vector`/`hybrid`, if the embedding call
itself fails (in which case the job's buffered document/chunk rows are
discarded before anything is written — a vector/hybrid KB with no embeddings
can't serve queries, so a total embedding failure must leave no partial rows
behind). All of a job's rows are buffered in memory and written in a single
short transaction at the very end, so the parse loop and the embedding
provider's network round-trip never hold SQLite's write lock against the
rest of the process.

**Retrieval: DB-backed vs. legacy file-based fallback.** A KB whose most
recent `IngestionJob` reached `status="completed"` is served entirely from
its `KnowledgeDocument`/`KnowledgeChunk` rows — built via the `from_chunks(...)`
alternate constructor on the matching `KnowledgeBase` subclass (see
`src/bestteam/core/CLAUDE.md`), never re-reading files from disk. A KB that
predates this feature (or was never re-uploaded since) has no completed
ingestion job, so it falls back to the original file-based construction
(`_build_knowledge_base`, scanning its upload directory / `CURRENT` pointer
as before). Note that a KB uploaded *after* this feature also has no
`CURRENT` pointer file — nothing writes one any more — so if its very first
job hasn't completed yet (a narrow window: the fallback only applies while
*zero* completed jobs exist) that fallback would `rglob` the whole KB root,
including any retained older version subdirectory, rather than one version
directory. `ui/backend/knowledge_bases.py::resolve_knowledge_base()` is the
one place this choice is made, shared by both `load_knowledge_base_tools`
(above) and `builder.py::_all_knowledge_base_tools` (the wizard's
pre-Specification "every standalone KB" catalog), so a pipeline build and
the wizard's KB catalog always agree on which content is live.

The KB's *shape* — which subclass to build, and which embedding model embeds
the query — comes from the serving `IngestionJob`'s own `kb_type`/
`embedding_model` columns, not from the `knowledge_bases` row's `config`:
`config` advances to the new spec the moment a re-upload is dispatched,
while the live content stays the previous completed generation until the new
job finishes (and forever, if it fails). The retrieval knobs (`top_k`,
`rerank_model`/`candidate_k`, `query_expansion_model`/
`query_expansion_count`) still come from `config` — they apply uniformly
whichever generation is live.

**Changing the search quality re-indexes; a re-upload that names no shape
keeps the existing one.** `upload_knowledge_base()`'s `kb_type` is optional.
A caller that doesn't send one — the admin route can't — inherits the whole
shape group (`type`, `embedding_model`, `rerank_model`,
`query_expansion_model`) from the existing record's `config`, plus its
`description` if this upload didn't give one, so replacing an Enhanced
collection's documents never silently rebuilds it as a Standard one. A name
that doesn't exist yet has nothing to inherit and gets `local_folder`, the
historical default. Only the shape group and the description are inherited:
`chunk_size`/`chunk_overlap`/`top_k` come from the caller's own arguments on
every upload, and `candidate_k`/`query_expansion_count`/`score_threshold`
aren't upload parameters at all — so **every upload resets all six to their
defaults**, discarding whatever a YAML or admin-API config had set for them.
The description inherits in one direction only: an empty one is read as
"didn't say", so an upload can replace a description but cannot clear it (do
that through the config path). A caller that *does* send `kb_type` (the
org self-service route always does, from the wizard's Standard/Enhanced
toggle) names the whole group itself, and switching it re-embeds every
document from scratch: the new generation is a full re-index, and the
previous one keeps serving until it completes.

Because of that, the customer-facing surfaces report the shape that is
*serving*, not the one `config` holds: `_kb_summary`'s `type` and the
self-service upload's name-conflict `409` ("It currently uses Enhanced search.")
both come from `org_knowledge_bases.py::_live_kb_type()` — the latest
completed job's own `kb_type`, falling back to `config` for a knowledge base
that has never completed one. `job_status_payload`'s `config` is unchanged;
that one is the configuration *intent*, i.e. what the next upload builds.

Deleting a knowledge base cascades to delete its `IngestionJob`/
`KnowledgeDocument`/`KnowledgeChunk` rows (`ingestion.delete_kb_ingestion_data`),
and is **refused with `409` while that knowledge base has a `queued` or
`running` ingestion job** — the worker is still reading the staged files and
would otherwise commit chunks against a record that no longer exists. The
refusal lasts only as long as the upload: jobs left `queued`/`running` by a
killed process are marked `failed` at the next startup, so a crash can never
leave a knowledge base permanently undeletable.

Older completed ingestion generations are pruned automatically once a new job
completes: the current generation and the one before it are kept intact, and
an older one loses its files. Its rows go too — unless a run's trace still
references it. Every knowledge-base search leaves the generation's id and each
hit's chunk id in the run's trace, and those ids keep resolving for as long as
the trace exists: a referenced generation keeps its document and chunk rows
with the vectors dropped (text, page, heading and filename are what an audit
needs), and is reclaimed at the collection's next completed upload after the
run's content is purged by retention. A `failed` job's on-disk version
directory is reclaimed the same way — every failed job except the most recent
one loses its directory (its rows stay, as the customer-visible error record),
so repeatedly retrying an upload that can't be parsed doesn't accumulate
storage.

**Restoring the previous upload.** "Restore previous upload" on the "My
documents" panel (`POST /api/org/knowledge-bases/{name}/restore`) makes the
generation before the live one the live set again: its files are staged into
a new generation under its own settings, every chunk and embedding is reused,
and nothing is billed. It reaches back one upload only — the one whose files
are still on the server — and restoring again undoes the restore. Refused
while an upload is processing, when there is no earlier upload, and when the
previous files are gone.

**Retrying a failed upload.** A failed ingestion — an embedding-provider
outage, or a job interrupted by a server restart — leaves its staged files on
the server, so "Retry" on the "My documents" panel
(`POST /api/org/knowledge-bases/{name}/ingestion-jobs/{job_id}/retry`) re-runs
the job in place instead of asking for the documents again. The same job row
is reset and re-dispatched (a 202 with the same id to poll), unchanged
documents still reuse the previous completed generation's chunks and
embeddings, and nothing is double-billed — a failed attempt is never metered.
Only the collection's newest job can be retried; refused when the job didn't
fail, when a newer upload superseded it, and when its files are no longer on
the server (the panel disables the button, naming the reason).

See `docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md`
for the full design.

## Evaluating retrieval

The "Try a search" panel (above) answers one query at a time: did *this*
question return the right passage? Judging a whole collection by clicking
through queries until the answers look reasonable does not scale, and it
cannot tell you whether a change helped. `scripts/kb_eval.py` turns the
question into a number: it runs a fixed set of queries whose right answer is
known against a knowledge base, and reports how often the expected document
came back and how highly it ranked.

```powershell
# The bundled golden set through the default BM25 knowledge base
.\.venv\Scripts\python.exe scripts/kb_eval.py

# The same questions through hybrid retrieval -- $0, no API key
.\.venv\Scripts\python.exe scripts/kb_eval.py --type hybrid --embedding-model fake:32

# The same questions with a real embedding model (this one costs money)
.\.venv\Scripts\python.exe scripts/kb_eval.py --type hybrid `
    --embedding-model openai:text-embedding-3-small
```

Other flags: `--rerank-model`, `--expansion-model`, `--chunk-size`,
`--chunk-overlap`, `--top-k`, `--docs`/`--queries` to point at your own
corpus, and `--json` for the same report as machine-readable output. The
defaults (`top_k=3`, `chunk_size=300`, `chunk_overlap=50`) are the bundled
golden set's smoke configuration, and live in `core/kb_eval.py` so the
documented numbers and the measured ones cannot drift apart.

**`fake:` specs prove the harness runs, never that retrieval improved.** A
`fake:<dim>` embedding is deterministic noise, so a hybrid run with one scores
differently from `local_folder` for no reason worth acting on. Only a real
embedding/rerank model says anything about quality.

### What it measures

Every metric is computed **per source document**, not per chunk — a knowledge
base that returns three chunks of the right document has answered the
question, whichever chunk of it ranked first. Because each query in a golden
set has exactly one relevant document, recall@k here is the same quantity as
hit@k.

| Metric | Meaning |
|---|---|
| `recall@k` | Fraction of queries whose expected document appeared in the top `k`. |
| `MRR` | Mean reciprocal rank — 1.0 for a hit at position 1, 0.5 at position 2, 0 for a miss. Rewards ranking it highly, not merely returning it. |
| `hit@1` | Fraction of queries whose expected document ranked first. |
| `substring hit@k` | Fraction of queries whose `expected_substring` (a concrete fact) appeared in the *text* of a retrieved chunk **of the expected document** — this is what catches a chunking change that separates the answer from the words that found it. Another document quoting the same fact does not count. |

The report splits every metric by query `kind`, and lists each query that did
not rank its expected document first, with what came back instead.

`evaluate()` consumes `KnowledgeBase.search()` (structured chunks), never the
formatted `query()` string, so a change to the citation format cannot quietly
change a score.

### The golden set

`tests/fixtures/kb_eval/` holds `docs/` — ten short support documents, five
English and five Chinese (refunds, shipping, opening hours, password reset,
warranty in each language) — and `queries.yaml`, twenty queries over them.
Each document is 300–600 characters and carries three to five concrete facts
(amounts, deadlines, place names) so a query has something exact to hit.

```yaml
queries:
  - query: restocking fee for opened items
    expected_source: refund_policy.md   # folder-relative path, as in a citation
    kind: lexical                       # or: paraphrase
    expected_substring: 15%             # optional
```

`kind` records why a query is in the set:

- **`lexical`** (16 of the 20) shares wording with its document. Keyword
  search is expected to rank that document first, and
  `tests/test_kb_eval.py::test_local_folder_baseline_on_golden_set` fails if
  any of them slips — the guarded thresholds are recall@3 ≥ 0.8 and MRR ≥ 0.7.
- **`paraphrase`** (4 of the 20) deliberately shares *no* significant term
  with its document ("I have changed my mind about a purchase and want my
  money back" for the refund policy). BM25 misses these by construction, so
  `local_folder` scores exactly 0 on them — they are the headroom a real
  embedding model is supposed to close, and the reason the whole-set
  thresholds sit at 0.8 rather than 1.0.

### Extending it

Add a document to `docs/` and at least one query naming it in `queries.yaml`
— `test_golden_set_is_well_formed` enforces the invariants (ten documents,
twenty queries, the 16/4 split, every `expected_source` an existing file,
every `expected_substring` genuinely present in it, every document the answer
to at least one query), so extending the set means updating those counts in
the same commit. For a *client's* corpus, leave the fixture alone and pass
`--docs`/`--queries`; nothing in the harness is specific to the bundled set.

Keep new lexical queries honest against the tokeniser (`core/text_tokenize.py`):
it lowercases English into alphanumeric tokens and stems them, so "cost" does
match "costs" but only inflections of one word conflate (never synonyms), and
it has no word segmenter for Chinese, Japanese or Korean, so those match on
character bigrams.

### What it does not measure

Retrieval only. Nothing here scores the *answer* an agent then writes, and
there is no regression baseline stored on disk to compare a run against — the
numbers are printed, not tracked over time.

### The release gate (real embedding model)

`fake:` runs prove the harness; only a real model says anything about
quality, and those runs cost money — so they live behind the `optional`
marker in `tests/test_kb_eval_live.py` and are run **by hand before a
release**, never by CI (the module skips itself when `OPENAI_API_KEY` is
unset):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_kb_eval_live.py -m optional
```

It runs the bundled golden set against `vector` and `hybrid` knowledge bases
under `openai:text-embedding-3-small` (the documented deployment default)
and fails if recall@3 drops below the floors calibrated from the 2026-08-26
measurement: overall ≥ 0.90, lexical ≥ 15/16, **paraphrase ≥ 3/4** — the
last one being the point: the four queries BM25 misses by construction must
be recovered by the embedding model, which the `fake:` smoke tests
structurally cannot show. Chunk embeddings persist in the gitignored
`.bestteam_cache/kb_eval_live.json`, so a full run costs well under $0.01
and re-runs pay only the query embeddings. The measured baselines are
recorded in the test module's docstring; recalibrate them in the same commit
that changes chunking, fusion or the golden set.

## Known limitations

- **Chunking is format-aware, not hierarchical.** Related content tends to
  stay together in one chunk (see "Document loading and chunking" above),
  but this is still single-level chunking — no "small-to-big"/parent-child
  multi-resolution indexing, and overlap between chunks is a raw
  character-slice of the previous chunk's tail (not structure-aware).
- **No external vector store.** `vector`/`hybrid` knowledge bases search an
  in-memory numpy matrix — built per process from the persisted
  `knowledge_chunks` rows on the upload path, or from an optional JSON file
  cache on the SDK path — with a linear cosine scan and no ANN index. No
  Chroma/FAISS/Pinecone/Weaviate/pgvector, so this doesn't scale past a
  single-process, small-to-medium corpus (the per-collection document caps
  are what keep it in range today).
- **No DMS connectors.** None of the three types can ingest directly from
  SharePoint, Confluence, Google Drive, etc. — only a local folder of files.
- **No OCR, and no image understanding at all.** A scanned PDF (or any
  page that is an image with no text layer) yields no extractable text.
  Since P0-6 that is *reported* — a warning on the SDK path, a `failed`
  document with a customer-readable reason on the upload path — rather than
  quietly indexed as a header-only chunk, but the document still cannot be
  searched. The same gap as the email toolkit's attachment reading, which
  is deliberately text-only (`src/bestteam/tools/CLAUDE.md`).
- **Change detection is per whole document, by name + content hash.** On the
  upload path an ingestion job reuses an unchanged document's chunks and
  embeddings (see "Uploads are asynchronous") and re-chunks/re-embeds a
  changed one in full — there is no chunk-level diff, so editing one
  paragraph of a 50-page PDF re-embeds all 50 pages. A renamed file with
  identical content is treated as new (the reuse key includes the filename),
  and the SDK path (a path-constructed KB) has only the per-chunk-text JSON
  cache, with no document-level reuse at all.
- **BM25 can be unstable on tiny corpora** (a handful of documents) —
  mitigated, but not eliminated, by the stopword filter and the
  shared-significant-terms gate before ranking.
- **Citations locate a chunk, but nothing links to it.** A returned chunk is
  tagged with its filename plus a page (PDF) or section heading (Markdown) —
  enough for a person to find the passage — but there is no chunk id in the
  *tag* the model quotes and no click-through to the document. The audit
  trail lives beside the tag rather than in it: the run's `tool_completed`
  event records the `ingestion_job_id` the collection was built from and each
  hit's `chunk_id`/`document_id` (see "What a search looks like in the
  trace"), so "which generation of which row did this answer draw on" is
  answerable from the trace — but a pipeline version does **not** pin a
  knowledge base generation: a run always searches the newest completed job,
  so re-running an old pipeline version after a re-upload searches today's
  documents. Plain text still cites its filename alone, and the Markdown
  heading, the spreadsheet/table one and the XML ancestor one are all
  approximations — see "Chunk location metadata" above.
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
- **Grounding checks citations, not claims.** `grounding_checked` says
  whether an answer's citations name passages that were retrieved; it does
  not say the passage supports the claim. The opt-in per-agent
  `grounding_policy` (`retry`/`refuse`, see "Grounding policy") can
  regenerate or refuse an answer that fails the citation check, but
  claim-level entailment, grader models and answer-level evaluation are not
  built, and the default remains record-only.

## File reference

| Purpose | Path |
|---|---|
| `local_folder` implementation + shared chunking/loading | `src/bestteam/core/knowledge_base.py` |
| `vector` implementation + embedding cache | `src/bestteam/core/vector_knowledge_base.py` |
| `hybrid` implementation | `src/bestteam/core/hybrid_knowledge_base.py` |
| Shared RRF fusion + query expansion helpers | `src/bestteam/core/fusion.py` |
| Shared reranking helper | `src/bestteam/core/reranking.py` |
| Per-tool-call trace/usage side channel | `src/bestteam/core/tool_context.py` |
| YAML loader (`_build_knowledge_base`) | `src/bestteam/core/loader.py` |
| Retrieval-quality metrics (`evaluate`, `recall_at_k`, `mrr`) | `src/bestteam/core/kb_eval.py` |
| Evaluation CLI | `scripts/kb_eval.py` |
| Golden set (documents + graded queries) | `tests/fixtures/kb_eval/` |
| `KnowledgeBaseSpec` (pydantic model mirroring the YAML schema) | `src/bestteam/core/specification.py` |
| Document parsing (PDF/Word/Excel/XML/text) | `src/bestteam/tools/file_parser.py` |
| Backend CRUD + admin upload endpoint | `ui/backend/crud.py` |
| Shared upload/index/version logic + "only build what's referenced" loading | `ui/backend/knowledge_bases.py` |
| Async ingestion jobs (parse/chunk/embed on a background thread) | `ui/backend/ingestion.py` |
| Org self-service upload + "smart search" toggle | `ui/backend/org_knowledge_bases.py` |
| Example: `local_folder` | `ui/backend/pipelines/knowledge_base_demo.yaml` |
| Example: `vector`, $0 fake embeddings | `ui/backend/pipelines/vector_knowledge_base_demo.yaml` |
| Example: `vector`, real OpenAI embeddings | `ui/backend/pipelines/vector_knowledge_base_demo_live.yaml` |
| Example: `hybrid`, $0 fake embeddings | `ui/backend/pipelines/hybrid_knowledge_base_demo.yaml` |
