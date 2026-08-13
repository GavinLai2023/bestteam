# Format-aware hierarchical chunking — design

Date: 2026-08-13
Status: design (ready for implementation)
Base: `main` @ `2a6cd84` (after the XML `parse_file`/knowledge-base support
and its follow-up correctness fixes — independent of them, but reuses the
XML renderer's indentation structure as one of its inputs).

## Problem

`_chunk_text` (`src/bestteam/core/knowledge_base.py:124-138`) — the only
chunker used by both `LocalFolderKnowledgeBase` and `VectorKnowledgeBase`
(via the shared `_load_document_chunks`) — splits documents into fixed-size,
fixed-offset overlapping windows with **no structural awareness**:

```python
def _chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    step = chunk_size - chunk_overlap
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        start += step
    return chunks
```

This cuts mid-word, mid-sentence, mid-XML-element, and mid-Markdown-heading
with no protection — flagged during the XML support work (a chunk boundary
could split an element's opening tag from its content) and already a known
gap in `src/bestteam/core/CLAUDE.md`'s "no hierarchical/small-to-big
indexing" limitation note. This design fixes the **structure-aware
splitting** half of that gap (never cut a chunk where the document's own
structure says not to, when a coarser split can keep it under `chunk_size`).
**Small-to-big multi-level retrieval** (index small units, return larger
parent context) is explicitly out of scope — a follow-on design once this
lands.

## Approach

A recursive separator-hierarchy splitter (the same family as LangChain's
`RecursiveCharacterTextSplitter`): given an ordered list of separators from
coarsest to finest, always prefer the coarsest one that still produces
pieces `<= chunk_size`, and only recurse into a finer separator for whichever
individual piece is still too big. The separator hierarchy is chosen **per
file suffix**, so Markdown headings and XML element boundaries — the two
formats in this codebase with clear syntactic structure — get first
priority; every other format (`.txt`, `.json`, `.csv`, extracted `.pdf`/
`.docx` text, etc.) falls back to a generic paragraph → sentence → word
hierarchy, which is still a real improvement over blind character offsets.

Two alternatives considered and rejected for this pass:

- **Generic-only splitter** (no per-format separator tables): simpler, but
  doesn't specifically respect Markdown headings or XML elements, which was
  the motivating complaint.
- **Full structural chunker** (chunk against each format's native
  object — `python-docx` paragraphs, the `ElementTree` node tree, parsed
  JSON/YAML — instead of `parse_file`'s flattened string output): most
  faithful, but changes `parse_file`'s "always returns one string" contract
  and needs a bespoke chunker per format. Bigger rewrite, deferred.

## Data flow

Unchanged everywhere except one call site. `_load_document_chunks`
(`knowledge_base.py:153`) already knows the file's suffix — thread it into
`_chunk_text`:

```python
text = parse_file(str(file_path))
...
for piece in _chunk_text(text, chunk_size, chunk_overlap, suffix=file_path.suffix.lower()):
    chunks.append(_Chunk(source=source, text=piece))
```

`LocalFolderKnowledgeBase`, `VectorKnowledgeBase`, `KnowledgeBase.query()`,
and the `_Chunk` NamedTuple are all untouched — this is purely an internal
change to how `_chunk_text` splits its input.

## Components

**`_recursive_split(text, separators, chunk_size) -> List[str]`** — the
general engine. `separators` is an ordered list, coarsest first, ending in
`""` (char-level, guarantees termination). At each level: split on the
current separator, greedily repack pieces up to `chunk_size` (via a shared
`_pack_pieces` helper), and recurse into the remaining separators only for
whichever piece is still oversized after packing.

**Boundary-marker preservation.** `text.split(sep)` discards the separator
itself. If a split-off piece becomes the *start* of a new chunk (rather than
being merged onto the previous one), naively dropping `sep` would strip a
Markdown chunk of its own heading (`"## Section 2"` at the top of a chunk
would just read `"Section 2"` with the `##` silently on the previous chunk,
or lost entirely). Fix: re-attach `sep` as a prefix to every piece except
the very first (`pieces = [raw[0]] + [sep + p for p in raw[1:]]`) *before*
packing, so a piece that ends up starting its own chunk still carries its
marker.

**Per-suffix separator tables**:

```python
_MARKDOWN_SEPARATORS = ["\n# ", "\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " ", ""]
_DEFAULT_SEPARATORS  = ["\n\n", "\n", ". ", " ", ""]

def _separators_for_suffix(suffix: str) -> List[str]:
    return _MARKDOWN_SEPARATORS if suffix == ".md" else _DEFAULT_SEPARATORS
```

**XML gets a dedicated first pass**, not a literal-string separator, because
its structural boundary isn't a fixed string — it's "before a line at
exactly one level of the renderer's 2-space indentation" (a direct child of
the root, in the `_render_xml_tree` output added in `file_parser.py`):

```python
_XML_TOP_LEVEL_BOUNDARY = re.compile(r"(?=\n  <)")

def _chunk_text(text, chunk_size, chunk_overlap, suffix=""):
    text = text.strip()
    if not text:
        return []
    if suffix == ".xml":
        sections = _XML_TOP_LEVEL_BOUNDARY.split(text)
        pieces = _pack_pieces(sections, chunk_size, _DEFAULT_SEPARATORS)
    else:
        pieces = _recursive_split(text, _separators_for_suffix(suffix), chunk_size)
    pieces = [p for p in pieces if p.strip()]
    return _apply_overlap(pieces, chunk_overlap)
```

The lookahead form (`(?=\n  <)`, zero-width) keeps the leading `\n` attached
to the *following* piece, so re-joining all sections with `""` reproduces
the original text exactly — the same invariant the literal-separator path
gets from `str.split`. The `[XML: filename]` header line and the root
element's own opening tag are both at 0 indentation, so they stay attached
to the first section (the root's first child), which is the desired
grouping.

**Overlap stays a separate post-pass**, applied uniformly regardless of
format:

```python
def _apply_overlap(pieces: List[str], chunk_overlap: int) -> List[str]:
    if chunk_overlap <= 0 or len(pieces) <= 1:
        return pieces
    result = [pieces[0]]
    for prev, piece in zip(pieces, pieces[1:]):
        result.append(prev[-chunk_overlap:] + piece)
    return result
```

Each chunk after the first gets the *previous chunk's* trailing
`chunk_overlap` characters prepended — same intent as today's sliding
window, just applied between semantically-bounded chunks instead of blind
fixed-offset ones. `prev` is drawn from the pre-overlap `pieces` list (not
the already-overlapped `result`), so overlap doesn't compound across chunks.

## Error handling / edge cases

- **Termination is guaranteed.** The separator list is finite and strictly
  shrinks each recursive call, bottoming out at a char-level split
  (`[text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]`) once
  `separators` is empty — no infinite loop possible, no new exception type.
- **Backward-compatible signature.** `suffix` defaults to `""`, which maps
  to `_DEFAULT_SEPARATORS` — every existing direct caller/test of
  `_chunk_text` (there are three in `tests/test_knowledge_base.py`) keeps
  working unchanged.
- **Degenerate text with no natural separators** (e.g. one very long token)
  falls through to the char-level split — byte-for-byte the same worst-case
  behavior as today's fixed-offset slicing, so nothing regresses.
- **Malformed XML never reaches this code.** `parse_file` already raises
  `ConfigurationError` for that upstream (existing behavior, unchanged) —
  `_chunk_text`'s XML path only ever sees a successfully-rendered document.
- `chunk_size`/`chunk_overlap` validation (`_validate_chunk_params`) is
  unchanged — still enforced at `LocalFolderKnowledgeBase`/
  `VectorKnowledgeBase` construction, before any chunking happens.

## Testing

- Unit tests directly on `_recursive_split`/`_chunk_text`, parametrized per
  suffix:
  - **Markdown**: a document with several `## ` sections, each individually
    under `chunk_size` — assert each resulting chunk starts with its own
    heading line (never mid-section), and that a section which is *itself*
    oversized still gets sentence/word-split rather than erroring or being
    silently truncated.
  - **XML**: a multi-child rendered document — assert no chunk boundary
    lands mid-element-line, and each chunk's first non-blank line is a
    top-level (0- or 2-space-indent) tag, never a deeply nested one landing
    without its ancestor context.
  - **Default/plain text**: assert chunks no longer cut mid-word (a direct
    regression test of the bug this design fixes), and that the three
    existing tests (`test_chunk_text_empty_string`,
    `test_chunk_text_short_text_is_single_chunk`,
    `test_chunk_text_long_text_produces_overlapping_chunks`) still pass
    unmodified (verified during design: the all-`"a"` fixture the third test
    uses has no natural separators, so it bottoms out at the same
    char-level split as before — the overlap-equality assertion holds
    regardless of exact boundary placement).
- One end-to-end `LocalFolderKnowledgeBase` ingestion test per structurally-
  aware format (Markdown, XML), mirroring the existing
  `test_knowledge_base_ingests_xml_files` pattern: a query for content that
  only appears in one structural section still finds the right source after
  chunking changes.

## Out of scope (explicitly deferred)

- Small-to-big / multi-level retrieval (index small units, return larger
  parent context) — the other half of the "hierarchical chunking" goal,
  scoped as a separate follow-on design once this lands.
- Per-format separator tables beyond Markdown/XML (e.g. treating JSON/YAML's
  own key structure, or CSV row boundaries, as first-class split points) —
  those formats fall back to the generic hierarchy for now, which is still
  strictly better than today's blind character offsets.
- A configurable/pluggable separator table (e.g. a `separators=` knowledge
  base constructor param) — not requested, and the per-suffix defaults
  cover every currently-supported format reasonably; add only if a real use
  case needs a custom hierarchy.
