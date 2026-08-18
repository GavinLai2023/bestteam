# Email automation Phase 4b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an email team read what a message carries — a quote as a PDF, line
items as a spreadsheet — without ever putting an attacker-chosen filename into
a filesystem lookup.

**Architecture:** `file_parser.py`'s four parsers are refactored to work on
bytes; `parse_file(path)` keeps its signature and becomes a thin wrapper. A new
`email_read_attachment(message_id, filename)` tool matches the name against the
message's own MIME parts and parses the bytes in `io.BytesIO` — no path crosses
the tool boundary and nothing is written to disk. `email_read` gains a manifest
so the model knows what is there before deciding to pay for it.

**Tech Stack:** Python 3.11 / `pypdf`, `openpyxl`, `python-docx`, stdlib
`email` / pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-email-phase-4b-attachments-design.md`

## Global Constraints

- **Run everything through the project venv:** `.\.venv\Scripts\python.exe -m pytest`
  on Windows. Never a bare `python`.
- **Every new test file needs a `pytestmark`** (`unit`/`integration`/`e2e`/
  `optional`) — `tests/test_marker_completeness.py` fails the whole suite
  otherwise. Both files touched here already have one; do not disturb it.
- **No attachment is ever written to disk.** Parsing runs on `io.BytesIO`. If
  you find yourself reaching for `tempfile`, stop — the design rejects it.
- **No path crosses the tool boundary.** `email_read_attachment` takes a
  message id and an attachment *name matched against that message's own MIME
  parts*. It must never accept, construct, or resolve a filesystem path.
- **`BODY.PEEK`, never bare `BODY`.** The draft-only toolkit deliberately never
  marks a customer's mail as seen.
- **No send verb, no SMTP.** Out of scope entirely.
- **`src/bestteam/` must not import from `ui/backend/`** — it is the shipped SDK.
- Code comments in English. **British spelling** in prose (organisation,
  behaviour, recognise, catalogue).
- Limits: **10 MB per attachment**, **25 MB per message total**, **8,000
  characters** of extracted text (matching `_MAX_BODY_CHARS`).
- Exceeding a limit or hitting an unsupported type returns a **plain sentence**,
  never an exception. An attachment too large to read is a fact about the
  message, not a fault in the automation.

## File Structure

| File | Change |
|---|---|
| `src/bestteam/tools/file_parser.py` | Four `*_bytes` parsers; `parse_file` delegates. Public signature unchanged. |
| `src/bestteam/tools/email_client.py` | `_ImapBackend.attachments()` / `.read_attachment()`; `_attachments_impl`; manifest in `_read_impl`; the `email_read_attachment` tool; `make_email_tools` entry. |
| `src/bestteam/tools/__init__.py` | `REGISTRY` + `__all__`. |
| `src/bestteam/__init__.py` | Re-export + `__all__`. |
| `ui/backend/deploy_validation.py` | `EMAIL_TOOL_NAMES` — **the containment boundary**. |
| `src/bestteam/adapters/langgraph_adapter.py` | `_EMAIL_TOOLS_NEEDING_REDACTION` + `_redacted_email_tool_data`. |
| `tests/test_file_parser_bytes.py` | New. |
| `tests/test_email_tools.py`, `tests/test_email_scoped_tools.py`, `tests/test_deploy_validation.py` | Extended. |
| `src/bestteam/tools/CLAUDE.md`, `docs/STATUS.md`, root `CLAUDE.md` | Docs. |

---

## Task 1: Parse from bytes, not from a path

**Files:**
- Modify: `src/bestteam/tools/file_parser.py`
- Test: `tests/test_file_parser_bytes.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_bytes(data: bytes, filename: str) -> str` — public within the SDK;
    dispatches on `filename`'s suffix exactly as `parse_file` dispatches on the
    path's.
  - `SUPPORTED_SUFFIXES: frozenset[str]` — every suffix `parse_bytes` handles.
  - `parse_file(path)` keeps its exact current signature, return value and
    error messages.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_file_parser_bytes.py`:

```python
"""Byte-based parsing (email automation Phase 4b).

Attachments are attacker-supplied and must never reach a filesystem path, so
the parsers have to work on bytes. These tests pin that the byte path produces
exactly what the path-based one does -- the refactor's whole job is to change
nothing observable.
"""

import io

import pytest

from bestteam.exceptions import ConfigurationError
from bestteam.tools.file_parser import SUPPORTED_SUFFIXES, parse_bytes, parse_file

pytestmark = pytest.mark.unit


def test_plain_text_round_trips():
    assert "hello" in parse_bytes(b"hello\nworld", "notes.txt")


def test_each_text_suffix_is_supported():
    for suffix in (".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"):
        assert parse_bytes(b"x", f"f{suffix}") == "x"


def test_an_unsupported_type_raises_with_the_type_named():
    with pytest.raises(ConfigurationError, match=r"\.exe"):
        parse_bytes(b"MZ", "payload.exe")


def test_a_file_with_no_suffix_is_unsupported():
    with pytest.raises(ConfigurationError):
        parse_bytes(b"x", "README")


def test_the_suffix_is_read_case_insensitively():
    assert parse_bytes(b"x", "NOTES.TXT") == "x"


def test_undecodable_text_does_not_raise(tmp_path):
    # A sender can attach anything named .txt. A UnicodeDecodeError escaping
    # into the poller would fail the whole run over one bad attachment.
    result = parse_bytes(b"\xff\xfe\x00binary", "notes.txt")
    assert isinstance(result, str)


def test_bytes_and_path_agree_for_text(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("# Title\n\nbody", encoding="utf-8")
    assert parse_bytes(target.read_bytes(), target.name) == parse_file(str(target))


def test_bytes_and_path_agree_for_xml(tmp_path):
    target = tmp_path / "doc.xml"
    target.write_text('<r a="1"><c>t</c></r>', encoding="utf-8")
    assert parse_bytes(target.read_bytes(), target.name) == parse_file(str(target))


def test_malformed_xml_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        parse_bytes(b"<r><unclosed>", "doc.xml")


def test_supported_suffixes_names_every_type_parse_bytes_handles():
    # A structural check: the constant is what the email tool will use to tell
    # a customer which attachments it can read, so it must not drift from the
    # dispatch table it describes.
    assert {".pdf", ".xlsx", ".xlsm", ".docx", ".xml", ".txt"} <= SUPPORTED_SUFFIXES


def test_parse_file_still_reports_a_missing_file():
    with pytest.raises(ConfigurationError, match="File not found"):
        parse_file("no/such/file.txt")
```

For PDF/XLSX/DOCX, add one round-trip test each **only if** the existing suite
already builds such fixtures — search the repo for existing `pypdf`/`openpyxl`/
`docx` test fixtures first and reuse them. If none exist, generate a minimal
one in-test with the same library (`openpyxl.Workbook()` and
`docx.Document()` can both save to a `BytesIO`), and skip the PDF case with
`pytest.importorskip("pypdf")` guarding it. Do not add a binary fixture file to
the repo.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_file_parser_bytes.py -q`
Expected: `ImportError` — `parse_bytes` does not exist.

- [ ] **Step 3: Refactor the parsers**

Each `_parse_X(path: Path)` becomes `_parse_X_bytes(data: bytes, name: str)`,
and the path version is expressed in terms of it. The libraries all accept a
file-like object, so no temp file is needed anywhere:

- `pypdf.PdfReader(io.BytesIO(data))`
- `openpyxl.load_workbook(io.BytesIO(data), ...)` — keep whatever keyword
  arguments the current call passes
- `docx.Document(io.BytesIO(data))`
- `ET.fromstring(data)` for the tree; namespace prefixes come from
  `ET.iterparse(io.BytesIO(data), events=("start-ns",))`

The `[PDF: {name} — N page(s)]` / `[Word: {name}]` / `[XML: {name}]` headers
currently use `path.name`; they now take the `name` argument, so the rendered
output is identical when called through `parse_file`.

Then:

```python
SUPPORTED_SUFFIXES = frozenset(
    {".pdf", ".xlsx", ".xlsm", ".docx", ".xml"} | _TEXT_SUFFIXES
)


def parse_bytes(data: bytes, filename: str) -> str:
    """Extract text from a file's bytes, dispatching on `filename`'s suffix.

    The byte-based entry point exists because email attachments are named by
    whoever sent the message. `parse_file` reads whatever path it is given,
    with no sandboxing -- a contract that is fine for knowledge bases, where an
    operator chooses the paths, and exactly wrong for mail. Parsing from bytes
    means there is no path for an attacker-chosen name to become.

    `filename` is used for dispatch and for the rendered header only; it is
    never resolved, opened, or joined to anything.
    """
```

Text decoding must not raise — a sender can attach anything named `.txt`. Use
`data.decode("utf-8", errors="replace")`.

`parse_file` keeps its docstring (including the no-sandboxing paragraph, which
is still true of it) and becomes:

```python
    return parse_bytes(file_path.read_bytes(), file_path.name)
```
after its existing not-found check. Its unsupported-type error message must
come out byte-identical, since a test may pin it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_file_parser_bytes.py tests/test_tools.py -q`
Expected: all pass. `tests/test_tools.py` is included because it exercises
`parse_file` through the registry — the refactor must be invisible to it.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/tools/file_parser.py tests/test_file_parser_bytes.py
git commit -m "refactor(tools): parse from bytes, so an attachment needs no path"
```

---

## Task 2: The backend reads attachments

**Files:**
- Modify: `src/bestteam/tools/email_client.py` (`_ImapBackend`)
- Test: `tests/test_email_tools.py` (extend)

**Interfaces:**
- Consumes: `parse_bytes`, `SUPPORTED_SUFFIXES` (Task 1).
- Produces, on `_ImapBackend`:
  - `attachments(message_id) -> Optional[List[Dict[str, Any]]]` — `None` if the
    message is not found; otherwise one dict per attachment with
    `{"filename": str, "content_type": str, "size": int}`.
  - `read_attachment(message_id, filename) -> Optional[Dict[str, Any]]` —
    `None` if the message is not found; otherwise
    `{"filename": str, "content_type": str, "size": int, "data": bytes}`, or
    `{"error": "<sentence>"}` when the named attachment is absent or breaches a
    limit.
- Module constants: `_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024`,
  `_MAX_ATTACHMENTS_TOTAL_BYTES = 25 * 1024 * 1024`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_email_tools.py`, next to the existing IMAP tests. That file
drives a `unittest.mock` connection from `_mock_imap_conn()` through
`patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn)`;
follow that exactly.

```python
def _multipart_raw(*, attachment_bytes=b"%PDF-1.4 fake", filename="quote.pdf"):
    """A message with a text body and one attachment, as bytes on the wire."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "support@example.com"
    msg["Subject"] = "Quote for the boiler"
    msg["Date"] = "Tue, 15 Jul 2026 08:00:00 +0000"
    msg.set_content("Please see attached.")
    msg.add_attachment(
        attachment_bytes, maintype="application", subtype="pdf", filename=filename
    )
    return msg.as_bytes()


def _conn_returning(raw):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [(b"7 (BODY[] {1}", raw), b")"])
    return conn


def test_attachments_lists_name_type_and_size(imap_env):
    conn = _conn_returning(_multipart_raw())
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        items = _ImapBackend.from_env().attachments("7")

    assert [i["filename"] for i in items] == ["quote.pdf"]
    assert items[0]["content_type"] == "application/pdf"
    assert items[0]["size"] == len(b"%PDF-1.4 fake")
    # BODY.PEEK, never BODY: the toolkit must not mark a customer's mail seen.
    assert "BODY.PEEK" in conn.uid.call_args_list[0].args[2]


def test_a_message_with_no_attachments_lists_none(imap_env):
    conn = _conn_returning(_RAW_MESSAGE)
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        assert _ImapBackend.from_env().attachments("7") == []


def test_the_body_is_not_reported_as_an_attachment(imap_env):
    # `set_content` makes the body a part too; only real attachments count.
    conn = _conn_returning(_multipart_raw())
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        items = _ImapBackend.from_env().attachments("7")
    assert all(i["filename"] == "quote.pdf" for i in items)


def test_attachments_of_a_missing_message_is_none(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [None])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        assert _ImapBackend.from_env().attachments("7") is None


def test_read_attachment_returns_the_bytes(imap_env):
    conn = _conn_returning(_multipart_raw())
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        record = _ImapBackend.from_env().read_attachment("7", "quote.pdf")
    assert record["data"] == b"%PDF-1.4 fake"


def test_read_attachment_matches_the_name_case_insensitively(imap_env):
    conn = _conn_returning(_multipart_raw())
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        record = _ImapBackend.from_env().read_attachment("7", "QUOTE.PDF")
    assert record["data"] == b"%PDF-1.4 fake"


def test_read_attachment_never_treats_the_name_as_a_path(imap_env):
    # The name is matched against the message's own parts, never resolved.
    # This must report "not found", not read anything off the filesystem.
    conn = _conn_returning(_multipart_raw())
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        record = _ImapBackend.from_env().read_attachment("7", "../../etc/passwd")
    assert "error" in record


def test_an_unknown_attachment_name_is_an_error_not_an_exception(imap_env):
    conn = _conn_returning(_multipart_raw())
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        record = _ImapBackend.from_env().read_attachment("7", "nope.pdf")
    assert "error" in record


def test_an_oversized_attachment_is_refused_before_it_is_parsed(imap_env, monkeypatch):
    monkeypatch.setattr("bestteam.tools.email_client._MAX_ATTACHMENT_BYTES", 8)
    conn = _conn_returning(_multipart_raw(attachment_bytes=b"0123456789"))
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        record = _ImapBackend.from_env().read_attachment("7", "quote.pdf")
    assert "error" in record and "large" in record["error"].lower()


def test_a_message_over_the_total_limit_is_refused(imap_env, monkeypatch):
    monkeypatch.setattr("bestteam.tools.email_client._MAX_ATTACHMENTS_TOTAL_BYTES", 4)
    conn = _conn_returning(_multipart_raw(attachment_bytes=b"0123456789"))
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        record = _ImapBackend.from_env().read_attachment("7", "quote.pdf")
    assert "error" in record
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py -q -k attachment`
Expected: `AttributeError` — `_ImapBackend` has no `attachments`.

- [ ] **Step 3: Implement**

Add near `_extract_text_body`:

```python
# An attachment is parsed in memory, never written to disk, so these bound
# what one message can make this process decompress. A parser is a
# decompressor and an unbounded one is a denial-of-service surface.
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_ATTACHMENTS_TOTAL_BYTES = 25 * 1024 * 1024


def _attachment_parts(msg: EmailMessage):
    """The message's real attachments, body parts excluded.

    `iter_attachments()` is what draws that line -- `get_body()`'s chosen part
    and its alternatives are not attachments, and a multipart body would
    otherwise be reported to the customer as a file they could open.
    """
    return [p for p in msg.iter_attachments() if p.get_filename()]
```

`attachments(message_id)` opens a readonly connection, calls the existing
`_fetch_message` (which already issues `BODY.PEEK[]`), and returns one dict per
part with `filename`, `content_type` (`part.get_content_type()`) and `size`
(`len(part.get_payload(decode=True) or b"")`).

`read_attachment(message_id, filename)` does the same, then:
1. If the total of all parts' sizes exceeds `_MAX_ATTACHMENTS_TOTAL_BYTES`,
   return `{"error": ...}` naming the limit in megabytes.
2. Find the part whose filename matches `filename` **case-insensitively and
   exactly** — no path handling, no `os.path.basename`, no normalisation
   beyond `.strip().lower()` on both sides. A name that is not one of this
   message's parts is simply not found.
3. If that part's size exceeds `_MAX_ATTACHMENT_BYTES`, return
   `{"error": ...}` saying it is too large to read.
4. Otherwise return the record including `data`.

Both methods return `None` when `_fetch_message` returns `None`, matching
`read`'s existing contract, and both use `try/finally: _imap_logout(conn)` like
every other method on this class.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py -q`
Expected: all pass, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/tools/email_client.py tests/test_email_tools.py
git commit -m "feat(email): the IMAP backend can list and fetch attachments"
```

---

## Task 3: The tool, and the manifest

**Files:**
- Modify: `src/bestteam/tools/email_client.py`
- Test: `tests/test_email_tools.py`, `tests/test_email_scoped_tools.py`

**Interfaces:**
- Consumes: Task 1's `parse_bytes`/`SUPPORTED_SUFFIXES`, Task 2's
  `attachments`/`read_attachment`.
- Produces:
  - `email_read_attachment(message_id: str, filename: str) -> str` — module-level
    tool, same shape as `email_read`.
  - `_attachment_impl(backend, message_id, filename) -> str`.
  - `make_email_tools` returns a **fourth** key, `"email_read_attachment"`,
    scoped by `allowed_uids` exactly as `email_read` is.
  - `_read_impl`'s output gains an `Attachments (N):` block when there are any.

- [ ] **Step 1: Write the failing tests**

In `tests/test_email_tools.py`:

```python
def test_read_lists_the_attachments_it_found(imap_env):
    conn = _conn_returning(_multipart_raw())
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read("7")
    assert "Attachments (1)" in result
    assert "quote.pdf" in result
    # The manifest is a list, not the content: reading costs a separate call.
    assert "%PDF" not in result


def test_read_says_nothing_about_attachments_when_there_are_none(imap_env):
    conn = _conn_returning(_RAW_MESSAGE)
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        assert "Attachments" not in email_read("7")


def test_read_attachment_returns_the_extracted_text(imap_env):
    raw = _multipart_raw(attachment_bytes=b"line one\nline two", filename="notes.txt")
    conn = _conn_returning(raw)
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read_attachment("7", "notes.txt")
    assert "line one" in result


def test_an_unsupported_attachment_type_names_the_type(imap_env):
    raw = _multipart_raw(attachment_bytes=b"MZ", filename="payload.exe")
    conn = _conn_returning(raw)
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read_attachment("7", "payload.exe")
    assert ".exe" in result
    # A sentence the model can relay, not a traceback.
    assert "Traceback" not in result


def test_an_archive_is_refused_rather_than_expanded(imap_env):
    raw = _multipart_raw(attachment_bytes=b"PK\x03\x04", filename="invoices.zip")
    conn = _conn_returning(raw)
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read_attachment("7", "invoices.zip")
    assert ".zip" in result


def test_a_file_that_lies_about_its_type_fails_cleanly(imap_env):
    # A sender can name anything .pdf. pypdf then raises; the model must get a
    # sentence, and the run must not fail over one bad attachment.
    raw = _multipart_raw(attachment_bytes=b"not a pdf at all", filename="fake.pdf")
    conn = _conn_returning(raw)
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read_attachment("7", "fake.pdf")
    assert isinstance(result, str) and "fake.pdf" in result


def test_extracted_text_is_truncated_at_the_body_limit(imap_env):
    raw = _multipart_raw(attachment_bytes=b"x" * 20000, filename="big.txt")
    conn = _conn_returning(raw)
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read_attachment("7", "big.txt")
    assert "truncated" in result.lower()
    assert len(result) < 20000


def test_reading_an_attachment_of_a_missing_message(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [None])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        assert "No message found" in email_read_attachment("7", "quote.pdf")


def test_a_backend_without_attachment_support_says_so(imap_env):
    # The Graph backend does not implement these methods. The tool must return
    # a sentence rather than raising AttributeError into the run.
    class _NoAttachments:
        pass

    from bestteam.tools.email_client import _attachment_impl

    result = _attachment_impl(_NoAttachments(), "7", "quote.pdf")
    assert isinstance(result, str) and "attachment" in result.lower()
```

In `tests/test_email_scoped_tools.py` — read it first and match its existing
style:

```python
def test_attachment_reading_is_confined_to_the_batch():
    # Same containment as email_read: a run may only touch the messages the
    # poller detected for it.
    tools = make_email_tools(_FakeBackend(), allowed_uids={"42", "43"})
    assert tools["email_read_attachment"]("44", "quote.pdf") == _OUT_OF_BATCH


def test_the_toolkit_exposes_exactly_four_tools():
    tools = make_email_tools(_FakeBackend())
    assert set(tools) == {
        "email_find", "email_read", "email_draft_reply", "email_read_attachment",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py tests/test_email_scoped_tools.py -q`
Expected: `ImportError` on `email_read_attachment`.

- [ ] **Step 3: Implement**

`_read_impl` gains the manifest after the body. Guard it — a backend without
`attachments` must not break `email_read`:

```python
    items = backend.attachments(message_id.strip()) if hasattr(backend, "attachments") else []
```
and render, only when non-empty:
```
Attachments (2):
  - quote.pdf (application/pdf, 84 KB)
```
Sizes in KB (integer division, minimum 1) so a customer-facing number stays
readable.

`_attachment_impl(backend, message_id, filename)`:
1. `hasattr(backend, "read_attachment")` — else return a sentence saying this
   mailbox connection cannot read attachments.
2. `record = backend.read_attachment(...)`; `None` → `f"No message found with
   id '{...}'."`, matching `_read_impl`'s wording exactly.
3. `record.get("error")` → return it as the sentence.
4. Suffix not in `SUPPORTED_SUFFIXES` → a sentence naming the suffix and
   listing what can be read. **Check this before parsing**, so `.zip` is
   refused rather than handed to a parser.
5. `parse_bytes(record["data"], record["filename"])`, wrapped in
   `try/except Exception` returning
   `f"'{filename}' couldn't be read: ..."` — a sender can attach anything and
   one malformed file must not fail the run.
6. Truncate at `_MAX_BODY_CHARS` with the same announced-truncation wording
   `_read_impl` already uses.

The module-level tool mirrors `email_read`'s shape (a `_get_backend()` call and
a docstring the LLM reads — say plainly that it reads one attachment of a
message already in this batch, and list the readable types).

In `make_email_tools`, add the fourth entry with the same
`allowed`/`_OUT_OF_BATCH` guard `read` uses, and update the function's
docstring — it currently says "the three email tools".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py tests/test_email_scoped_tools.py tests/test_load_email_tools.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/tools/email_client.py tests/test_email_tools.py tests/test_email_scoped_tools.py
git commit -m "feat(email): read one attachment on demand, and list the rest"
```

---

## Task 4: The five exhaustive lists

**Files:**
- Modify: `src/bestteam/tools/__init__.py`, `src/bestteam/__init__.py`,
  `ui/backend/deploy_validation.py`,
  `src/bestteam/adapters/langgraph_adapter.py`
- Test: `tests/test_deploy_validation.py`, `tests/test_tools.py`,
  `tests/test_trace_granularity.py` (whichever pins
  `_redacted_email_tool_data` — find it first)

**Interfaces:**
- Consumes: `email_read_attachment` (Task 3).
- Produces: no new API. The tool becomes nameable in a workflow YAML,
  importable from `bestteam`, covered by the egress-conflict rule, and
  redacted in traces.

**Why this is its own task:** every one of these five is an enumeration that
fails **silently and open** if missed. `EMAIL_TOOL_NAMES` is the containment
boundary — miss it and a workflow may pair attachment reading with `http_get`,
which is precisely the exfiltration path the draft-only argument excludes.

- [ ] **Step 1: Write the failing tests**

In `tests/test_deploy_validation.py`:

```python
def test_attachment_reading_conflicts_with_an_egress_tool():
    # An injected attachment could otherwise direct the model to put mailbox
    # content into a URL it fetches -- exfiltration without ever sending mail.
    problems = find_email_egress_conflicts(
        [("triager", {"email_read_attachment", "http_get"})]
    )
    assert problems


def test_every_tool_the_email_toolkit_returns_is_treated_as_an_email_tool():
    # Structural, so the NEXT email tool cannot repeat Phase 4b's near-miss:
    # a tool absent from EMAIL_TOOL_NAMES is silently exempt from the
    # egress-conflict rule.
    from bestteam.tools.email_client import make_email_tools

    class _Backend:
        pass

    assert set(make_email_tools(_Backend())) <= EMAIL_TOOL_NAMES
```

In `tests/test_tools.py`:

```python
def test_registry_exposes_attachment_reading():
    assert "email_read_attachment" in REGISTRY
```

For the redaction test, first locate what pins `_redacted_email_tool_data`
(`grep -rn "_redacted_email_tool_data\|tool_completed" tests/`) and add, in
that file's style:

```python
def test_attachment_text_never_reaches_the_trace():
    data = _redacted_email_tool_data(
        "email_read_attachment",
        {"message_id": "42", "filename": "quote.pdf"},
        "Confidential: the tender price is 250,000",
    )
    assert "250,000" not in str(data)
    assert "Confidential" not in str(data)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_deploy_validation.py tests/test_tools.py -q`
Expected: the conflict test returns `[]`, the registry test `KeyError`.

- [ ] **Step 3: Implement — all five sites**

1. `ui/backend/deploy_validation.py`:
   `EMAIL_TOOL_NAMES = frozenset({"email_find", "email_read", "email_draft_reply", "email_read_attachment"})`
2. `src/bestteam/adapters/langgraph_adapter.py`: add the name to
   `_EMAIL_TOOLS_NEEDING_REDACTION`, and give `_redacted_email_tool_data` a
   branch for it returning `{"summary": ..., "message_id": ..., "outcome": ...}`
   with **no extracted text**. Follow the `email_read` branch's shape. Check
   whether the `if call["name"] in ("email_read", "email_draft_reply")`
   correlation at roughly line 400 should include the new name — read the
   surrounding comment and decide; if it should not, say why in your report.
3. `src/bestteam/tools/__init__.py`: import, `REGISTRY` entry, `__all__` entry.
4. `src/bestteam/__init__.py`: re-export and `__all__` entry.
5. Confirm Task 3 already added the `make_email_tools` key.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_deploy_validation.py tests/test_tools.py tests/test_trace_granularity.py tests/test_email_tools.py tests/test_crud_api.py -q`
Expected: all pass. `test_crud_api.py` is included because it validates tool
names against the registry when saving an agent.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam ui/backend/deploy_validation.py tests/
git commit -m "feat(email): register attachment reading everywhere it must be known"
```

---

## Task 5: Docs, and the whole suite

**Files:**
- Modify: `src/bestteam/tools/CLAUDE.md`, `docs/STATUS.md`, root `CLAUDE.md`

- [ ] **Step 1: `src/bestteam/tools/CLAUDE.md`**

Add `email_read_attachment` to the tools table, and a paragraph on the trust
boundary: attachments are parsed from bytes and never written to disk, the tool
takes no path, and `parse_file`'s no-sandboxing contract is why. State the
three limits and the supported types. Note that the tool is in
`EMAIL_TOOL_NAMES`, so pairing it with an egress tool is refused at deploy
validation.

- [ ] **Step 2: `docs/STATUS.md`**

A **Done** entry for Phase 4b in the voice of the 3a/3b/4a entries. Then find
the known-issues sentence saying **attachments are invisible** — Phase 4a's
docs task rewrote that bullet and left this half standing — and correct it.

Add to known issues: no OCR or image understanding; archives refused; the whole
message is still fetched (`BODY.PEEK[]`) so a large attachment costs memory
even when nothing reads it; extraction is text-only (layout, formulas and
embedded images are lost); dispatch trusts the filename suffix.

- [ ] **Step 3: Root `CLAUDE.md`**

Update the email bullets in "Known limitations / unimplemented extension
points" to say attachment reading now exists and what shape it takes.

- [ ] **Step 4: Full backend suite, serially**

Run: `.\.venv\Scripts\python.exe -m pytest -m "not e2e"` (serial, **no**
`-n auto`). This is `backend-full` parity — that CI job is gated to `main` and
will not run on this branch, so this is the only place ordering and
cross-test-isolation problems surface. Phase 4a caught a cross-file escape this
way.

Expected: all pass. `tests/test_packaging.py::test_python_dash_m_bestteam_entry_point`
fails under PowerShell only (a pre-existing GBK console-codec artefact; it
passes under Git Bash). **Anything else that fails is a real finding — report
it and never weaken an assertion to make it pass.**

- [ ] **Step 5: Frontend suite**

From `ui/frontend`: `npm test -- --run && npx tsc --noEmit && npm run lint`.
Nothing in this phase touches the frontend, so this is a regression check;
report the counts.

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/tools/CLAUDE.md docs/STATUS.md CLAUDE.md
git commit -m "docs: record attachment reading, and what it deliberately cannot do"
```
