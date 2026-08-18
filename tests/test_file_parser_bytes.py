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


def test_undecodable_text_raises_by_default():
    # Strict is the default so `parse_bytes` and `parse_file` agree. A
    # mis-encoded document belongs in a warning to whoever owns it, not in a
    # knowledge base as mojibake nobody can search.
    with pytest.raises(UnicodeDecodeError):
        parse_bytes(bytes([0xFF, 0xFE, 0x00]) + b"binary", "notes.txt")


def test_undecodable_text_is_replaced_when_lenient():
    # The attachment path opts in: a sender can name anything `.txt`, and a
    # UnicodeDecodeError escaping into the poller would fail a customer's
    # whole run over one bad attachment.
    result = parse_bytes(bytes([0xFF, 0xFE, 0x00]) + b"binary", "notes.txt",
                         lenient_text=True)
    assert isinstance(result, str)


def test_parse_file_raises_on_a_mis_encoded_document(tmp_path):
    # What `Path.read_text(encoding="utf-8")` did before the byte refactor,
    # and what lets a knowledge base skip the file with a warning rather
    # than ingest it as mojibake.
    target = tmp_path / "legacy.txt"
    target.write_bytes("你好".encode("gbk"))
    with pytest.raises(UnicodeDecodeError):
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
    assert result == parse_file(str(target))
