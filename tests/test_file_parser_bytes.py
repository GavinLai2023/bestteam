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
    for suffix in (".txt", ".md", ".json", ".yaml", ".yml", ".log"):
        assert parse_bytes(b"x", f"f{suffix}") == "x"
    # `.csv` is still plain text on the way in, but it leaves as a table
    # block: the marker is what earns it the same chunking a spreadsheet gets.
    assert parse_bytes(b"x", "f.csv") == "[CSV: f.csv]\nx"


def test_an_unsupported_type_raises_with_the_type_named():
    with pytest.raises(ConfigurationError, match=r"\.exe"):
        parse_bytes(b"MZ", "payload.exe")


def test_a_file_with_no_suffix_is_unsupported():
    with pytest.raises(ConfigurationError):
        parse_bytes(b"x", "README")


def test_the_suffix_is_read_case_insensitively():
    assert parse_bytes(b"x", "NOTES.TXT") == "x"


def test_undecodable_text_raises_by_default():
    # Strict is still the default once the encoding chain is exhausted, so
    # `parse_bytes` and `parse_file` agree and a mis-encoded document belongs
    # in a warning to whoever owns it, not in a knowledge base as mojibake
    # nobody can search. These bytes open with a UTF-16 BOM and then aren't
    # UTF-16, so they exercise the fall-through past a BOM's own claim.
    with pytest.raises(ConfigurationError):
        parse_bytes(bytes([0xFF, 0xFE, 0x00]) + b"binary", "notes.txt")


def test_undecodable_text_is_replaced_when_lenient():
    # The attachment path opts in: a sender can name anything `.txt`, and a
    # UnicodeDecodeError escaping into the poller would fail a customer's
    # whole run over one bad attachment.
    result = parse_bytes(bytes([0xFF, 0xFE, 0x00]) + b"binary", "notes.txt",
                         lenient_text=True)
    assert isinstance(result, str)


def test_parse_file_reads_a_gbk_document(tmp_path):
    # This used to raise: `Path.read_text(encoding="utf-8")` was what the
    # byte refactor preserved, and GBK is what a Chinese Windows box writes.
    target = tmp_path / "legacy.txt"
    target.write_bytes("你好".encode("gbk"))
    assert parse_file(str(target)) == "你好"


def test_parse_file_raises_on_a_mis_encoded_document(tmp_path):
    # A document in none of the chain's encodings is still skipped with a
    # warning rather than ingested as mojibake.
    target = tmp_path / "legacy.txt"
    target.write_bytes(bytes([0x81, 0x30, 0xFF]))
    with pytest.raises(ConfigurationError):
        parse_file(str(target))


def test_leniency_does_not_loosen_the_binary_parsers():
    # The switch is scoped to plain text, so nothing can smuggle a broken
    # document past a parser by asking for leniency.
    with pytest.raises(ConfigurationError):
        parse_bytes(b"<r><unclosed>", "doc.xml", lenient_text=True)

def test_windows_and_classic_mac_line_endings_are_normalised():
    # `Path.read_text` translates newlines by default, so a bare decode would
    # change parse_file's output for any CRLF file. This asserts on literal
    # bytes rather than write_text, which emits \r\n only on Windows -- CI runs
    # on Linux, where a platform-dependent version of this test proves nothing.
    assert parse_bytes(b"a\r\nb\rc\nd", "notes.txt") == "a\nb\nc\nd"


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


# ---------------------------------------------------------------------------
# Binary formats. No fixture files live in the repo -- each document is built
# in-test with the same library that reads it, matching how tests/test_tools.py
# already generates its .docx fixtures.
# ---------------------------------------------------------------------------

def test_bytes_and_path_agree_for_pdf(tmp_path):
    pypdf = pytest.importorskip("pypdf")

    buffer = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    data = buffer.getvalue()

    target = tmp_path / "doc.pdf"
    target.write_bytes(data)

    result = parse_bytes(data, target.name)
    assert result.startswith("[PDF: doc.pdf — 1 page(s)]")
    assert result == parse_file(str(target))


def test_bytes_and_path_agree_for_excel(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")

    buffer = io.BytesIO()
    workbook = openpyxl.Workbook()
    workbook.active.append(["Name", "Price"])
    workbook.active.append(["Widget", 9.99])
    workbook.save(buffer)
    data = buffer.getvalue()

    target = tmp_path / "data.xlsx"
    target.write_bytes(data)

    result = parse_bytes(data, target.name)
    assert "Name,Price" in result
    assert result == parse_file(str(target))


def test_bytes_and_path_agree_for_docx(tmp_path):
    docx = pytest.importorskip("docx")

    buffer = io.BytesIO()
    document = docx.Document()
    document.add_heading("Invoice", 1)
    document.add_paragraph("Hello from Word.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Price"
    document.save(buffer)
    data = buffer.getvalue()

    target = tmp_path / "doc.docx"
    target.write_bytes(data)

    result = parse_bytes(data, target.name)
    assert result.startswith("[Word: doc.docx]")
    assert "Hello from Word." in result
    # An emailed attachment gets the same heading rendering a knowledge-base
    # upload does -- one parser, one output contract, both entry points.
    assert "# Invoice" in result.splitlines()
    assert result == parse_file(str(target))


def test_pdf_pages_are_separated_by_form_feed():
    """Page boundaries survive parsing, so the knowledge base can chunk per
    page and cite an exact `p.N`. pypdf can write a PDF but not a text-bearing
    one, so the reader is stubbed -- what's under test is the join, not pypdf.
    """
    import types
    from unittest.mock import patch

    pages = [types.SimpleNamespace(extract_text=lambda text=text: text) for text in ("one", "two")]
    fake_pypdf = types.SimpleNamespace(PdfReader=lambda _stream: types.SimpleNamespace(pages=pages))

    with patch.dict("sys.modules", {"pypdf": fake_pypdf}):
        result = parse_bytes(b"%PDF-1.4", "doc.pdf")

    assert result == "[PDF: doc.pdf — 2 page(s)]\none\ftwo"


def test_a_form_feed_inside_a_page_does_not_shift_page_numbers():
    """The form feed is a delimiter now, so a page that contains one of its
    own must not read as a page break -- that would renumber every page after
    it and make the `p.N` in a citation quietly wrong."""
    import types
    from unittest.mock import patch

    from bestteam.core.knowledge_base import _chunk_document, _citation

    texts = ("Refunds are allowed.\fShipping is free.", "Warranty needs the receipt.")
    pages = [types.SimpleNamespace(extract_text=lambda text=text: text) for text in texts]
    fake_pypdf = types.SimpleNamespace(PdfReader=lambda _stream: types.SimpleNamespace(pages=pages))

    with patch.dict("sys.modules", {"pypdf": fake_pypdf}):
        result = parse_bytes(b"%PDF-1.4", "doc.pdf")

    # One separator for two pages -- the in-page one became a space.
    assert result.count("\f") == 1
    assert "Refunds are allowed. Shipping is free." in result

    chunks = _chunk_document("doc.pdf", result, chunk_size=1000, chunk_overlap=0, suffix=".pdf")
    assert [chunk.page for chunk in chunks] == [1, 2]
    warranty = next(chunk for chunk in chunks if "Warranty" in chunk.text)
    assert _citation(warranty) == "doc.pdf, p.2"


def test_pdf_pages_are_joined_by_the_page_break_constant():
    """The delimiter is one named constant owned by the producer, so the
    knowledge base's per-page chunking cannot drift from what the parser
    actually writes."""
    import types
    from unittest.mock import patch

    from bestteam.tools.file_parser import PAGE_BREAK

    pages = [types.SimpleNamespace(extract_text=lambda text=text: text) for text in ("one", "two")]
    fake_pypdf = types.SimpleNamespace(PdfReader=lambda _stream: types.SimpleNamespace(pages=pages))

    with patch.dict("sys.modules", {"pypdf": fake_pypdf}):
        result = parse_bytes(b"%PDF-1.4", "doc.pdf")

    assert PAGE_BREAK == "\f"
    assert result == f"[PDF: doc.pdf — 2 page(s)]\none{PAGE_BREAK}two"


# --- Text encodings beyond UTF-8 -------------------------------------------
#
# A plain-text document a customer produces on a Chinese Windows box is very
# often not UTF-8: Notepad and Excel's own "CSV (comma delimited)" export both
# write GBK, and Excel's "Unicode text" export writes UTF-16 with a BOM. Every
# one of those used to reach the customer as a raw UnicodeDecodeError naming a
# byte offset.


def test_gbk_text_is_decoded():
    assert parse_bytes("季度报告".encode("gbk"), "notes.txt") == "季度报告"


def test_gb18030_text_is_decoded():
    # GBK's superset, and the encoding actually tried -- it covers GB2312 and
    # GBK documents as well as its own.
    assert parse_bytes("季度报告".encode("gb18030"), "notes.txt") == "季度报告"


def test_a_utf8_byte_order_mark_is_stripped():
    # Excel's "CSV UTF-8" export writes one. Left in place it becomes the
    # first character of the document -- and so of its first heading.
    decoded = parse_bytes("﻿Hello".encode("utf-8"), "notes.txt")
    assert decoded == "Hello"


def test_utf16_text_is_decoded():
    # Excel's "Unicode text" export: UTF-16 with a BOM.
    assert parse_bytes("Hello\tworld".encode("utf-16"), "notes.txt") == "Hello\tworld"


def test_a_gb18030_decode_without_cjk_is_refused():
    # These bytes DO decode as GB18030 -- to "αβ total", no CJK anywhere.
    # That is the shape a Western-encoded document takes when its high bytes
    # happen to form valid GB18030 pairs, and accepting it would turn a loud
    # failure into silently indexed mojibake. Refusing it also refuses the
    # rare genuine GB18030 document that contains no Chinese; that trade is
    # deliberate (see `_decode_text`).
    payload = bytes([0xA6, 0xC1, 0xA6, 0xC2]) + b" total"
    assert payload.decode("gb18030") == "αβ total"
    with pytest.raises(ConfigurationError):
        parse_bytes(payload, "notes.txt")


def test_an_undecodable_document_names_the_file_and_the_fix():
    # What a self-service customer sees in the upload panel, via
    # ui/backend/ingestion.py. "invalid start byte at position 0" is not
    # something they can act on; "save it as UTF-8" is.
    with pytest.raises(ConfigurationError) as excinfo:
        parse_bytes(bytes([0x81, 0x30, 0xFF]), "notes.txt")
    message = str(excinfo.value)
    assert "notes.txt" in message
    assert "UTF-8" in message


def test_lenient_decoding_still_never_raises():
    # The attachment path's contract is unchanged: a sender can name anything
    # `.txt`, and one bad attachment must not fail a customer's whole run.
    result = parse_bytes(bytes([0x81, 0x30, 0xFF]), "notes.txt", lenient_text=True)
    assert isinstance(result, str)


def test_lenient_decoding_prefers_a_real_encoding_over_replacement():
    # Leniency is the last resort, not the first: an attachment that IS
    # decodable as GBK should arrive as its own text, not as U+FFFDs.
    assert parse_bytes("季度报告".encode("gbk"), "notes.txt", lenient_text=True) == "季度报告"
