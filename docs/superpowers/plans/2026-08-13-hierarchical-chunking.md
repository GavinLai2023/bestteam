# Format-Aware Hierarchical Chunking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `_chunk_text`'s blind fixed-offset character slicing with a
recursive, format-aware splitter that prefers document structure (Markdown
headings, XML element boundaries, paragraph/sentence/word for everything
else) over arbitrary cut points, without changing anything else in the
knowledge-base pipeline.

**Architecture:** A recursive separator-hierarchy splitter (`_recursive_split`
+ `_pack_pieces`) tries the coarsest separator in an ordered list first and
only descends to a finer one for pieces still too large. Markdown and XML get
bespoke separator handling (headings; a regex boundary matching the XML
renderer's 2-space-per-depth indentation); every other format uses a generic
paragraph → sentence → word fallback. `_load_document_chunks` threads the
file's suffix into `_chunk_text` so both `LocalFolderKnowledgeBase` and
`VectorKnowledgeBase` pick this up automatically (they already share that one
call site).

**Tech Stack:** Python stdlib only (`re`); no new dependencies. All changes
are confined to `src/bestteam/core/knowledge_base.py` and
`tests/test_knowledge_base.py`.

## Global Constraints

- `_chunk_text`'s `suffix` parameter MUST default to `""` and MUST map to the
  generic separator hierarchy, so the three pre-existing tests
  (`test_chunk_text_empty_string`, `test_chunk_text_short_text_is_single_chunk`,
  `test_chunk_text_long_text_produces_overlapping_chunks`) keep passing
  unmodified with no changes to their call sites.
- The separator list passed to `_recursive_split` MUST always end in `""`
  (char-level split), so splitting always terminates — never add a separator
  hierarchy without this terminal fallback.
- No new exception types. Malformed input is not a concern here — `parse_file`
  already raises `ConfigurationError` upstream for anything `_chunk_text`
  would choke on (verified: malformed XML never reaches `_chunk_text`).
- `_validate_chunk_params` (chunk_size > 0, 0 <= chunk_overlap < chunk_size)
  is unchanged and still the only validation gate, enforced at
  `LocalFolderKnowledgeBase`/`VectorKnowledgeBase` construction time, before
  any chunking happens.
- Scope is structure-aware splitting only. Do NOT implement small-to-big
  retrieval, and do NOT add separator tables for JSON/YAML/CSV — both are
  explicitly out of scope per
  `docs/superpowers/specs/2026-08-13-hierarchical-chunking-design.md`'s "Out
  of scope" section.

---

## Task 1: Recursive splitter engine + generic separator fallback

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py:124-138` (replace `_chunk_text`'s body; add new helpers above it)
- Test: `tests/test_knowledge_base.py` (add new tests near the existing `_chunk_text` tests, lines 14-32)

**Interfaces:**
- Consumes: nothing new (stdlib only).
- Produces (used by Task 2 and Task 3):
  - `_pack_pieces(pieces: List[str], chunk_size: int, fallback_separators: List[str]) -> List[str]`
  - `_recursive_split(text: str, separators: List[str], chunk_size: int) -> List[str]`
  - `_apply_overlap(pieces: List[str], chunk_overlap: int) -> List[str]`
  - `_DEFAULT_SEPARATORS: List[str]` (module-level constant)
  - `_chunk_text(text: str, chunk_size: int, chunk_overlap: int, suffix: str = "") -> List[str]` (new `suffix` kwarg, unused by this task's logic — every suffix maps to `_DEFAULT_SEPARATORS` until Task 2/3 add real dispatch)

- [ ] **Step 1: Write the failing test — no mid-word cuts**

Add to `tests/test_knowledge_base.py`, right after `test_chunk_text_long_text_produces_overlapping_chunks` (around line 32):

```python
def test_chunk_text_does_not_cut_mid_word():
    text = " ".join(f"word{i}" for i in range(1, 15))
    chunks = _chunk_text(text, chunk_size=20, chunk_overlap=0)
    assert len(chunks) > 1
    reconstructed = " ".join(chunks).split()
    assert reconstructed == text.split()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py::test_chunk_text_does_not_cut_mid_word -v`
Expected: FAIL — the current fixed-offset `_chunk_text` cuts `text[0:20]` mid-word (`"word1 word2 word3 w"`), so `reconstructed != text.split()`.

- [ ] **Step 3: Implement the recursive splitter engine**

In `src/bestteam/core/knowledge_base.py`, replace the existing `_chunk_text` function (lines 124-138) with:

```python
_DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


def _pack_pieces(pieces: List[str], chunk_size: int, fallback_separators: List[str]) -> List[str]:
    """Greedily merge adjacent pieces up to chunk_size; recurse into
    fallback_separators for any individual piece that's still too large on
    its own."""
    results: List[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                results.append(current)
            if len(piece) > chunk_size:
                results.extend(_recursive_split(piece, fallback_separators, chunk_size))
                current = ""
            else:
                current = piece
    if current:
        results.append(current)
    return results


def _recursive_split(text: str, separators: List[str], chunk_size: int) -> List[str]:
    """Split text on the coarsest separator that fits chunk_size, recursing
    into finer separators only for pieces that are still too large."""
    if len(text) <= chunk_size:
        return [text] if text else []
    if not separators:
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    sep, *rest = separators
    raw_pieces = text.split(sep) if sep else list(text)
    # Re-attach the separator to every piece but the first, so a piece that
    # ends up starting its own chunk still carries its own marker (e.g. a
    # Markdown "## Heading" or an XML tag) instead of silently losing it.
    pieces = [raw_pieces[0]] + [sep + p for p in raw_pieces[1:]]
    return _pack_pieces(pieces, chunk_size, rest)


def _apply_overlap(pieces: List[str], chunk_overlap: int) -> List[str]:
    """Prepend each chunk (after the first) with the previous chunk's
    trailing chunk_overlap characters, so retrieval keeps context across a
    chunk boundary -- same intent as the old fixed-offset overlap, applied
    between semantically-bounded chunks instead."""
    if chunk_overlap <= 0 or len(pieces) <= 1:
        return pieces
    result = [pieces[0]]
    for prev, piece in zip(pieces, pieces[1:]):
        result.append(prev[-chunk_overlap:] + piece)
    return result


def _chunk_text(text: str, chunk_size: int, chunk_overlap: int, suffix: str = "") -> List[str]:
    """Split text into chunks, preferring the document's own structure
    (paragraphs, sentences, words) over blind fixed-size character cuts."""
    text = text.strip()
    if not text:
        return []
    pieces = _recursive_split(text, _DEFAULT_SEPARATORS, chunk_size)
    pieces = [p for p in pieces if p.strip()]
    return _apply_overlap(pieces, chunk_overlap)
```

- [ ] **Step 4: Run the new test and the three pre-existing `_chunk_text` tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -k chunk_text -v`
Expected: all PASS, including `test_chunk_text_empty_string`,
`test_chunk_text_short_text_is_single_chunk`,
`test_chunk_text_long_text_produces_overlapping_chunks`, and the new
`test_chunk_text_does_not_cut_mid_word`.

- [ ] **Step 5: Run the full test suite for this module**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py tests/test_tools.py -v`
Expected: all PASS (this task doesn't touch `_load_document_chunks`'s call
site yet, so `LocalFolderKnowledgeBase`/`VectorKnowledgeBase` ingestion tests
are unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "$(cat <<'EOF'
feat(core): replace fixed-offset chunking with a recursive separator splitter

_chunk_text previously sliced text at blind fixed-size character offsets,
which could cut mid-word. Replace it with a recursive splitter that tries
increasingly fine separators (paragraph, line, sentence, word, char) and
only descends when a coarser split still produces an oversized piece.
Adds a suffix kwarg (unused for now, defaults to the generic hierarchy)
that Markdown/XML-specific handling will hook into next.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Markdown heading-aware separator table

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py` (add `_MARKDOWN_SEPARATORS`, `_separators_for_suffix`, update `_chunk_text` to dispatch through it)
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `_recursive_split`, `_pack_pieces`, `_apply_overlap`, `_DEFAULT_SEPARATORS`, `_chunk_text(text, chunk_size, chunk_overlap, suffix="")` from Task 1.
- Produces (used by Task 4's end-to-end test):
  - `_MARKDOWN_SEPARATORS: List[str]`
  - `_separators_for_suffix(suffix: str) -> List[str]`
  - `_chunk_text(..., suffix=".md")` now returns Markdown-heading-aware chunks.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_knowledge_base.py`, after `test_chunk_text_does_not_cut_mid_word`:

```python
def test_chunk_text_markdown_preserves_heading_boundaries():
    text = (
        "## Section One\n"
        "Some content about apples.\n\n"
        "## Section Two\n"
        "Some content about oranges.\n\n"
        "## Section Three\n"
        "Some content about bananas.\n"
    )
    chunks = _chunk_text(text, chunk_size=40, chunk_overlap=0, suffix=".md")
    assert len(chunks) > 1
    for chunk in chunks:
        if "## Section" in chunk:
            heading_index = chunk.index("## Section")
            assert chunk[:heading_index].strip() == ""


def test_chunk_text_markdown_splits_oversized_section_by_sentence():
    text = "## Big Section\n" + "This is sentence one. " * 20
    chunks = _chunk_text(text, chunk_size=60, chunk_overlap=0, suffix=".md")
    assert len(chunks) > 1
    assert all(len(chunk) <= 60 for chunk in chunks)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -k markdown -v`
Expected: FAIL — `suffix=".md"` is currently accepted but ignored, so
`_chunk_text` still uses `_DEFAULT_SEPARATORS`, which has no heading-aware
split points (the first test's heading-boundary assertion will fail once a
chunk boundary lands mid-paragraph without respecting `"## "`).

- [ ] **Step 3: Add the Markdown separator table and suffix dispatch**

In `src/bestteam/core/knowledge_base.py`, add above `_chunk_text`:

```python
_MARKDOWN_SEPARATORS = ["\n# ", "\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " ", ""]


def _separators_for_suffix(suffix: str) -> List[str]:
    return _MARKDOWN_SEPARATORS if suffix == ".md" else _DEFAULT_SEPARATORS
```

Then update `_chunk_text`'s body to use it:

```python
def _chunk_text(text: str, chunk_size: int, chunk_overlap: int, suffix: str = "") -> List[str]:
    """Split text into chunks, preferring the document's own structure
    (paragraphs, sentences, words) over blind fixed-size character cuts."""
    text = text.strip()
    if not text:
        return []
    pieces = _recursive_split(text, _separators_for_suffix(suffix), chunk_size)
    pieces = [p for p in pieces if p.strip()]
    return _apply_overlap(pieces, chunk_overlap)
```

- [ ] **Step 4: Run the new tests plus Task 1's tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -k "chunk_text or markdown" -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "$(cat <<'EOF'
feat(core): add Markdown heading-aware chunk splitting

_chunk_text now prefers splitting on Markdown heading boundaries
(#, ##, ### , ####) before falling back to the generic paragraph/
sentence/word hierarchy, so a heading never ends up stranded
mid-chunk without its own section.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: XML element-boundary-aware first pass

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py` (add `import re`, `_XML_TOP_LEVEL_BOUNDARY`, XML branch in `_chunk_text`)
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `_pack_pieces`, `_recursive_split`, `_apply_overlap`, `_DEFAULT_SEPARATORS`, `_separators_for_suffix` from Tasks 1-2.
- Produces (used by Task 4's end-to-end test): `_chunk_text(..., suffix=".xml")` now splits on the XML renderer's top-level (2-space-indent) element boundaries before falling back to the generic hierarchy for any oversized section.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_knowledge_base.py`, after the Markdown tests:

```python
def test_chunk_text_xml_splits_on_top_level_element_boundaries():
    text = (
        "[XML: catalog.xml]\n"
        "<catalog>\n"
        '  <book id="bk101"> Widgets Explained\n'
        '  <book id="bk102"> Gadgets Explained\n'
        '  <book id="bk103"> Gizmos Explained'
    )
    chunks = _chunk_text(text, chunk_size=60, chunk_overlap=0, suffix=".xml")
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.strip():
                assert line.lstrip().startswith("<") or line.startswith("[XML:")


def test_chunk_text_xml_oversized_element_falls_back_without_cutting_words():
    # A single top-level element too large to fit in one chunk on its own --
    # the top-level-boundary regex can't help here (there's only one
    # section), so this must fall back to _DEFAULT_SEPARATORS. That fallback
    # can't preserve a "<tag>" prefix on every resulting line once it's
    # forced down to word-level splitting (there's nothing left to attach a
    # tag marker to), so the guarantee this test checks is the honest one:
    # no word gets cut in half and no chunk exceeds chunk_size -- not "every
    # line starts with a tag", which only holds when sections individually
    # fit within chunk_size (see the previous test).
    long_title = " ".join(f"word{i}" for i in range(1, 30))
    text = (
        "[XML: catalog.xml]\n"
        "<catalog>\n"
        '  <book id="bk101">\n'
        f"    <title> {long_title}"
    )
    chunks = _chunk_text(text, chunk_size=50, chunk_overlap=0, suffix=".xml")
    assert len(chunks) > 1
    assert all(len(chunk) <= 50 for chunk in chunks)
    reconstructed = " ".join(chunks).split()
    assert reconstructed == text.split()
```

The first test asserts every non-blank line in every chunk is a *complete*
`<tag ...> text` line (or the `[XML: ...]` header) — never a fragment, which
is what a mid-line cut would produce. It holds because none of that
fixture's sections individually exceed `chunk_size`, so packing only ever
merges or separates whole section-lines. The second test covers the
complementary case (a section that's individually oversized) with the
weaker-but-true word-safety guarantee instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -k "xml_splits_on_top_level or xml_oversized" -v`
Expected: FAIL — `suffix=".xml"` currently falls through to
`_separators_for_suffix`'s default (`_DEFAULT_SEPARATORS`), which splits on
generic paragraph/line/sentence/word/char boundaries with no awareness of
the renderer's indentation, so a chunk boundary can land mid-line in the
first test. The second test may coincidentally pass even before Task 3's
implementation, since word-safety is already a property of the generic
`_DEFAULT_SEPARATORS` fallback from Task 1 — that's fine, it's here to lock
in the fallback behavior once the `.xml` branch exists, not to prove a
regression.

- [ ] **Step 3: Add the XML boundary regex and dedicated branch**

In `src/bestteam/core/knowledge_base.py`, add `import re` to the top-of-file
imports (alongside `import logging` / `import warnings`):

```python
import re
```

Add the module-level compiled pattern near `_DEFAULT_SEPARATORS`:

```python
_XML_TOP_LEVEL_BOUNDARY = re.compile(r"(?=\n  <)")
```

Update `_chunk_text` to branch on `.xml`:

```python
def _chunk_text(text: str, chunk_size: int, chunk_overlap: int, suffix: str = "") -> List[str]:
    """Split text into chunks, preferring the document's own structure
    (paragraphs, sentences, words) over blind fixed-size character cuts."""
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

- [ ] **Step 4: Run the new test plus all prior `_chunk_text` tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -k "chunk_text or markdown or xml" -v`
Expected: all PASS.

- [ ] **Step 5: Run the full module test suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py tests/test_tools.py -v`
Expected: all PASS (this task still hasn't wired the real call site in
`_load_document_chunks`, so end-to-end KB ingestion tests are unaffected —
that's Task 4).

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "$(cat <<'EOF'
feat(core): add XML element-boundary-aware chunk splitting

_chunk_text now recognizes the XML renderer's top-level (2-space-indent)
element boundaries as the preferred split point before falling back to
the generic separator hierarchy for any oversized section, so a chunk
never starts mid-element-line.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Wire suffix from the real call site + end-to-end ingestion tests

**Files:**
- Modify: `src/bestteam/core/knowledge_base.py:153-169` (`_load_document_chunks`)
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `_chunk_text(text, chunk_size, chunk_overlap, suffix="")` from Tasks 1-3; `LocalFolderKnowledgeBase` (unchanged constructor/`.query()`).
- Produces: nothing new — this is the integration point where the whole
  feature becomes reachable through the public `LocalFolderKnowledgeBase`/
  `VectorKnowledgeBase` API.

- [ ] **Step 1: Write the failing end-to-end tests**

Add to `tests/test_knowledge_base.py`, after `test_knowledge_base_ingests_xml_files`:

```python
def test_knowledge_base_markdown_chunking_respects_headings(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## Refunds\n"
        + "Refunds are issued within 7 days of the request. " * 3
        + "\n\n## Shipping\n"
        + "Orders ship within two business days of confirmation. " * 3,
        encoding="utf-8",
    )
    kb = LocalFolderKnowledgeBase("docs", tmp_path, chunk_size=80, chunk_overlap=0)
    result = kb.query("refund request")
    assert "guide.md" in result
    assert "Refunds" in result


def test_knowledge_base_xml_chunking_respects_element_boundaries(tmp_path):
    (tmp_path / "catalog.xml").write_text(
        "<catalog>"
        '<book id="bk101"><title>Widgets Explained</title></book>'
        '<book id="bk102"><title>Gadgets Explained</title></book>'
        '<book id="bk103"><title>Completely unrelated automotive repair manual</title></book>'
        "</catalog>",
        encoding="utf-8",
    )
    kb = LocalFolderKnowledgeBase("docs", tmp_path, chunk_size=80, chunk_overlap=0)
    result = kb.query("automotive repair manual")
    assert "catalog.xml" in result
    assert "automotive repair manual" in result
```

- [ ] **Step 2: Run tests to verify they fail or are unreliable**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -k "chunking_respects" -v`
Expected: FAIL or flaky-pass — `_load_document_chunks` doesn't pass `suffix`
to `_chunk_text` yet, so every file (regardless of extension) is still
chunked with the generic default hierarchy, not the format-specific one
these tests are designed to exercise.

- [ ] **Step 3: Wire the suffix through `_load_document_chunks`**

In `src/bestteam/core/knowledge_base.py`, in `_load_document_chunks`
(around line 167), change:

```python
        for piece in _chunk_text(text, chunk_size, chunk_overlap):
```

to:

```python
        for piece in _chunk_text(text, chunk_size, chunk_overlap, suffix=file_path.suffix.lower()):
```

- [ ] **Step 4: Run the new end-to-end tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py -k "chunking_respects" -v`
Expected: PASS.

- [ ] **Step 5: Run the full project test suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py tests/test_tools.py tests/test_load_email_tools.py tests/test_skill_seeding.py -v`
(These four files cover the SDK-level tests most likely to touch knowledge
bases, tools, or workflow loading; the last two are known to fail in this
environment for an unrelated pre-existing reason — a missing
`BESTTEAM_SECRETS_KEY` for a leftover local secrets DB — so only check that
`test_knowledge_base.py` and `test_tools.py` are fully green.)
Expected: all `test_knowledge_base.py`/`test_tools.py` tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/knowledge_base.py tests/test_knowledge_base.py
git commit -m "$(cat <<'EOF'
feat(core): wire format-aware chunking into knowledge base ingestion

_load_document_chunks now threads each file's suffix into _chunk_text,
so LocalFolderKnowledgeBase and VectorKnowledgeBase (both routed through
this one shared function) automatically get Markdown heading-aware and
XML element-boundary-aware chunking with no other change needed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification (after all 4 tasks)

- [ ] Run `.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_base.py tests/test_tools.py -v` — confirm all tests pass, including the three original `_chunk_text` tests, all new Markdown/XML/default-splitter unit tests, and the new end-to-end ingestion tests.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest` — confirm no regressions elsewhere (the same pre-existing `BESTTEAM_SECRETS_KEY` collection errors from the UI-backend test suite are expected and unrelated to this change).
- [ ] Skim `src/bestteam/core/CLAUDE.md`'s knowledge-base section — if it's updated to mention the "no hierarchical/small-to-big indexing" limitation is now partially addressed (structure-aware splitting done, small-to-big retrieval still deferred), keep the wording precise about which half shipped.
