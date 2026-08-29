"""Tests for the built-in tools package."""
import json
import socket
from unittest.mock import MagicMock, call, patch

import pytest

from bestteam.exceptions import ConfigurationError
from bestteam.tools import (
    REGISTRY,
    calculator,
    http_get,
    local_business_search,
    parse_file,
    web_search,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# REGISTRY
# ---------------------------------------------------------------------------

def test_registry_contains_all_tools():
    assert set(REGISTRY) == {
        "web_search", "parse_file", "http_get", "calculator",
        "email_find", "email_read", "email_read_attachment", "email_draft_reply",
        "local_business_search",
    }


def test_registry_exposes_attachment_reading():
    # Named in a pipeline YAML only if the loader can resolve it here.
    assert "email_read_attachment" in REGISTRY


def test_knowledge_base_discovery_excludes_legacy_xls():
    # CR-016: KB folder discovery must not advertise .xls either (openpyxl
    # can't read it), so it stays consistent with parse_file's supported set.
    from bestteam.core.knowledge_base import _SUPPORTED_SUFFIXES

    assert ".xls" not in _SUPPORTED_SUFFIXES
    assert ".xlsx" in _SUPPORTED_SUFFIXES
    assert ".xml" in _SUPPORTED_SUFFIXES


def test_parse_file_rejects_legacy_xls(tmp_path):
    # CR-016: .xls (legacy BIFF) is no longer advertised -- openpyxl cannot read
    # it, so it must be rejected cleanly as an unsupported type, not routed to
    # the Excel parser where it fails with an opaque backend error.
    p = tmp_path / "legacy.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-ole2-content")
    with pytest.raises(ConfigurationError, match="Unsupported file type"):
        parse_file(str(p))


def test_registry_values_are_callables():
    for name, fn in REGISTRY.items():
        assert callable(fn), f"REGISTRY['{name}'] is not callable"


# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr,expected", [
    ("2 + 3", "5"),
    ("10 - 4", "6"),
    ("3 * 7", "21"),
    ("10 / 4", "2.5"),
    ("10 // 3", "3"),
    ("10 % 3", "1"),
    ("2 ** 8", "256"),
    ("(3 + 4) * 2 ** 8", "1792"),
    ("-5 + 10", "5"),
])
def test_calculator_arithmetic(expr, expected):
    assert calculator(expr) == expected


def test_calculator_integer_result_has_no_decimal():
    assert calculator("10 / 2") == "5"


def test_calculator_rejects_function_calls():
    with pytest.raises(ConfigurationError, match="disallowed"):
        calculator("__import__('os').system('dir')")


def test_calculator_rejects_names():
    with pytest.raises(ConfigurationError, match="disallowed"):
        calculator("x + 1")


def test_calculator_rejects_invalid_syntax():
    with pytest.raises(ConfigurationError, match="syntax"):
        calculator("2 +* 3")


def test_calculator_rejects_huge_exponent():
    with pytest.raises(ConfigurationError, match="Exponent too large"):
        calculator("2 ** 9999")


def test_calculator_rejects_oversized_result():
    with pytest.raises(ConfigurationError, match="too large"):
        calculator("99999 ** 1000")


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

def test_web_search_raises_without_package():
    with patch.dict("sys.modules", {"tavily": None}):
        with pytest.raises(ConfigurationError, match="tavily-python"):
            web_search("test query")


def test_web_search_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    fake_tavily = MagicMock()
    with patch.dict("sys.modules", {"tavily": fake_tavily}):
        with pytest.raises(ConfigurationError, match="TAVILY_API_KEY"):
            web_search("test query")


def test_web_search_returns_formatted_string(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {"title": "AI News", "url": "https://example.com", "content": "Big breakthrough."},
        ]
    }
    fake_tavily = MagicMock()
    fake_tavily.TavilyClient.return_value = mock_client
    with patch.dict("sys.modules", {"tavily": fake_tavily}):
        result = web_search("AI news", max_results=1)
    assert "AI News" in result
    assert "https://example.com" in result
    assert "Big breakthrough." in result


# ---------------------------------------------------------------------------
# local_business_search
# ---------------------------------------------------------------------------

def _make_mock_httpx_post(side_effects):
    """Build a fake httpx module whose Client.post raises/returns side_effects."""
    import httpx as _real_httpx

    mock_client_instance = MagicMock()
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=False)
    mock_client_instance.post.side_effect = side_effects

    mock_httpx = MagicMock()
    mock_httpx.Client.return_value = mock_client_instance
    mock_httpx.RequestError = _real_httpx.RequestError
    return mock_httpx, mock_client_instance


def test_local_business_search_raises_without_package():
    with patch.dict("sys.modules", {"httpx": None}):
        with pytest.raises(ConfigurationError, match="httpx"):
            local_business_search("plumber in Bondi NSW")


def test_local_business_search_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    fake_httpx = MagicMock()
    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        with pytest.raises(ConfigurationError, match="GOOGLE_MAPS_API_KEY"):
            local_business_search("plumber in Bondi NSW")


def test_local_business_search_returns_formatted_string(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "places": [
            {
                "displayName": {"text": "Bondi Plumbing Co"},
                "formattedAddress": "1 Beach Rd, Bondi NSW",
                "rating": 4.7,
                "userRatingCount": 128,
                "priceLevel": "PRICE_LEVEL_MODERATE",
                "googleMapsUri": "https://maps.google.com/?cid=123",
            }
        ]
    }
    mock_httpx, mock_client_instance = _make_mock_httpx_post([response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = local_business_search("plumber in Bondi NSW", max_results=1)
    assert "Bondi Plumbing Co" in result
    assert "1 Beach Rd, Bondi NSW" in result
    assert "4.7" in result
    assert "128" in result
    assert "$$" in result
    assert "https://maps.google.com/?cid=123" in result


def test_local_business_search_no_results(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"places": []}
    mock_httpx, mock_client_instance = _make_mock_httpx_post([response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = local_business_search("nonexistent trade in nowhere")
    assert "No results found" in result


def test_local_business_search_does_not_retry_on_4xx(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    response = MagicMock()
    response.status_code = 400
    response.text = "Invalid request"
    mock_httpx, mock_client_instance = _make_mock_httpx_post([response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            with pytest.raises(ConfigurationError, match="400"):
                local_business_search("plumber in Bondi NSW")
    assert mock_client_instance.post.call_count == 1
    assert mock_sleep.call_count == 0


def test_local_business_search_retries_on_5xx(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    server_error = MagicMock()
    server_error.status_code = 503
    server_error.text = "Service Unavailable"

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.json.return_value = {"places": []}

    mock_httpx, mock_client_instance = _make_mock_httpx_post([server_error, ok_response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            result = local_business_search("plumber in Bondi NSW")
    assert mock_client_instance.post.call_count == 2
    assert mock_sleep.call_count == 1
    assert "No results found" in result


# ---------------------------------------------------------------------------
# http_get
# ---------------------------------------------------------------------------

def _patch_getaddrinfo(monkeypatch, ip_or_map):
    """Monkeypatch socket.getaddrinfo used by http_client's SSRF check.

    `ip_or_map` is either a single IP string (used for any host) or a dict
    mapping hostname -> IP string.
    """
    def fake_getaddrinfo(host, *args, **kwargs):
        ip = ip_or_map[host] if isinstance(ip_or_map, dict) else ip_or_map
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr("bestteam.tools.http_client.socket.getaddrinfo", fake_getaddrinfo)


def test_http_get_raises_without_package():
    with patch.dict("sys.modules", {"httpx": None}):
        with pytest.raises(ConfigurationError, match="httpx"):
            http_get("https://example.com")


def test_http_get_rejects_non_http_url():
    with pytest.raises(ConfigurationError, match="must start with"):
        http_get("ftp://example.com")


def test_http_get_rejects_invalid_headers_json():
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        http_get("https://example.com", headers_json="not-json")


def test_http_get_rejects_non_dict_headers():
    with pytest.raises(ConfigurationError, match="JSON object"):
        http_get("https://example.com", headers_json='["list", "not", "dict"]')


def test_http_get_returns_status_and_body(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '{"ok": true}'
    mock_response.is_redirect = False
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_response
    mock_httpx = MagicMock()
    mock_httpx.Client.return_value = mock_client
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = http_get("https://example.com")
    assert "[200]" in result
    assert '{"ok": true}' in result


# ---------------------------------------------------------------------------
# http_get SSRF protection
# ---------------------------------------------------------------------------

def test_http_get_blocks_loopback_address(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "127.0.0.1")
    with pytest.raises(ConfigurationError, match="private/internal"):
        http_get("http://localhost/admin")


def test_http_get_blocks_link_local_metadata_address(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "169.254.169.254")
    with pytest.raises(ConfigurationError, match="private/internal"):
        http_get("http://169.254.169.254/latest/meta-data/")


def test_http_get_rejects_redirect_to_private_address(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {"example.com": "93.184.216.34", "127.0.0.1": "127.0.0.1"})

    redirect_response = MagicMock()
    redirect_response.status_code = 302
    redirect_response.is_redirect = True
    redirect_response.headers = {"location": "http://127.0.0.1/admin"}

    mock_httpx, mock_client_instance = _make_mock_httpx([redirect_response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with pytest.raises(ConfigurationError, match="private/internal"):
            http_get("https://example.com")
    assert mock_client_instance.get.call_count == 1


def test_http_get_follows_safe_redirect(monkeypatch):
    _patch_getaddrinfo(monkeypatch, {"example.com": "93.184.216.34", "example.org": "93.184.216.35"})

    redirect_response = MagicMock()
    redirect_response.status_code = 302
    redirect_response.is_redirect = True
    redirect_response.headers = {"location": "https://example.org/final"}

    final_response = MagicMock()
    final_response.status_code = 200
    final_response.text = "final body"
    final_response.is_redirect = False

    mock_httpx, mock_client_instance = _make_mock_httpx([redirect_response, final_response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = http_get("https://example.com")
    assert mock_client_instance.get.call_count == 2
    assert "[200]" in result
    assert "example.org/final" in result
    assert "final body" in result


def test_http_get_pins_connection_to_validated_ip(monkeypatch):
    # CR-023: after validating the resolved address, the request must connect to
    # THAT ip rather than the hostname (which httpx would re-resolve, opening a
    # DNS-rebinding window). The hostname is preserved for the Host header and
    # TLS SNI so virtual hosts and cert verification still work.
    _patch_getaddrinfo(monkeypatch, {"example.com": "93.184.216.34"})

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.text = "ok"
    ok_response.is_redirect = False

    mock_httpx, mock_client_instance = _make_mock_httpx([ok_response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = http_get("https://example.com/path?q=1")

    args, kwargs = mock_client_instance.get.call_args
    connect_url = args[0]
    assert "93.184.216.34" in connect_url  # connects to the validated ip
    assert "example.com" not in connect_url  # not the hostname (no re-resolution)
    assert "/path?q=1" in connect_url  # path/query preserved
    assert kwargs["headers"]["Host"] == "example.com"  # vhost preserved
    assert kwargs["extensions"]["sni_hostname"] == "example.com"  # TLS SNI/cert
    # The displayed/returned URL stays the hostname URL, not the ip.
    assert "example.com/path" in result


# ---------------------------------------------------------------------------
# parse_file
# ---------------------------------------------------------------------------

def test_parse_file_raises_for_missing_file(tmp_path):
    with pytest.raises(ConfigurationError, match="File not found"):
        parse_file(str(tmp_path / "nonexistent.txt"))


def test_parse_file_reads_txt(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("Hello, world!", encoding="utf-8")
    assert parse_file(str(f)) == "Hello, world!"


def test_parse_file_reads_csv(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    assert "a,b" in parse_file(str(f))


def test_parse_file_csv_gets_a_table_marker(tmp_path):
    # A CSV is a table, and the knowledge base only knows that from the
    # parser's own marker line -- without it the file chunks as prose.
    f = tmp_path / "items.csv"
    f.write_text("sku,name\nA1,Widget\n", encoding="utf-8")
    assert parse_file(str(f)).splitlines() == ["[CSV: items.csv]", "sku,name", "A1,Widget"]


def test_parse_file_csv_quoted_newline_stays_one_row(tmp_path):
    # Excel writes a cell containing a line break as a quoted field spanning
    # two physical lines. Passed through verbatim it becomes two rows, and
    # every column after the break belongs to the wrong header.
    f = tmp_path / "notes.csv"
    f.write_text('sku,note\nA1,"first line\nsecond line"\n', encoding="utf-8")
    assert parse_file(str(f)).splitlines() == [
        "[CSV: notes.csv]",
        "sku,note",
        'A1,first line second line',
    ]


def test_parse_file_csv_keeps_a_comma_inside_a_field_quoted(tmp_path):
    # Re-joining fields bare would turn one value into two columns, silently
    # shifting every column after it out from under the repeated header row.
    f = tmp_path / "items.csv"
    f.write_text('sku,name\nA1,"Widget, large"\n', encoding="utf-8")
    assert parse_file(str(f)).splitlines()[-1] == 'A1,"Widget, large"'


def test_parse_file_csv_written_by_chinese_excel(tmp_path):
    # The whole point of both halves of this change: Excel's "CSV (comma
    # delimited)" export on a Chinese Windows box, which is GBK.
    f = tmp_path / "订单.csv"
    f.write_bytes("编号,名称\n1,螺丝\n".encode("gbk"))
    assert parse_file(str(f)).splitlines() == ["[CSV: 订单.csv]", "编号,名称", "1,螺丝"]


def test_parse_file_csv_keeps_a_field_larger_than_128kb(tmp_path):
    # `csv.reader`'s default 131,072-character field limit is not a limit this
    # application chose. A CSV with a long notes column is well inside the
    # 30MB per-file upload cap and used to be refused as unreadable.
    f = tmp_path / "notes.csv"
    f.write_text('id,note\n1,"' + "x" * 200_000 + '"\n', encoding="utf-8")
    assert parse_file(str(f)).splitlines()[-1] == "1," + "x" * 200_000


def test_parse_file_csv_with_an_unbalanced_quote_swallows_the_rest(tmp_path):
    # An unbalanced quote makes `csv.reader` read everything after it as one
    # field. That was already what a *small* such file did; it additionally
    # raised past the 128KB field limit, so one malformation failed loudly or
    # quietly depending on the file's size, and the message blamed a quotation
    # mark for every oversized field. Lifting the limit makes it uniform.
    f = tmp_path / "broken.csv"
    f.write_text('sku,name\nA1,"' + "x" * 200_000, encoding="utf-8")
    lines = parse_file(str(f)).splitlines()
    assert lines[:2] == ["[CSV: broken.csv]", "sku,name"]
    assert len(lines) == 3


def test_parse_file_csv_row_shaped_like_a_marker_is_escaped(tmp_path):
    # `[CSV: name]` is the block marker the chunker splits on, so a row that
    # reads like one would split the table and cite every row after it as
    # coming from a different document.
    f = tmp_path / "rows.csv"
    f.write_text("note\n[CSV: other.csv]\nplain\n", encoding="utf-8")
    assert parse_file(str(f)).splitlines() == [
        "[CSV: rows.csv]",
        "note",
        "\\[CSV: other.csv]",
        "plain",
    ]


def test_parse_file_excel_keeps_a_comma_inside_a_cell_quoted(tmp_path):
    # Same defect the CSV path already fixed: re-joining cells bare turns one
    # value into two apparent columns, silently shifting every column after it
    # out from under the header row the chunker repeats above each chunk.
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "items.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["sku", "name"])
    workbook.active.append(["A1", "Widget, large"])
    workbook.save(str(f))
    assert parse_file(str(f)).splitlines()[-1] == 'A1,"Widget, large"'


def test_parse_file_excel_cell_with_newline_stays_one_row(tmp_path):
    # A cell containing a line break must stay one field on one line --
    # rendered verbatim it becomes an extra, shorter row, and every column
    # after the break sits under the wrong header.
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "notes.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["sku", "note"])
    workbook.active.append(["A1", "first line\nsecond line"])
    workbook.save(str(f))
    assert parse_file(str(f)).splitlines() == [
        "[Excel: notes.xlsx]",
        "",
        "[Sheet: Sheet]",
        "sku,note",
        "A1,first line second line",
    ]


def test_parse_file_excel_rejects_a_workbook_that_unpacks_too_large(tmp_path, monkeypatch):
    # An .xlsx is a zip, and a kilobyte upload can declare gigabytes once
    # inflated. The parser refuses on the archive's own unpacked sizes,
    # before a single sheet is read.
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "big.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["a"])
    workbook.save(str(f))
    monkeypatch.setattr("bestteam.tools.file_parser._MAX_XLSX_UNPACKED_BYTES", 10)
    with pytest.raises(ConfigurationError, match="big.xlsx.*unpacks to more than"):
        parse_file(str(f))


def test_parse_file_excel_rejects_a_workbook_with_too_many_cells(tmp_path, monkeypatch):
    # A stray-formatted sheet declares millions of empty cells; iterating
    # them all would pin a CPU for minutes on the shared instance. The count
    # covers every cell the sheets declare, not just the ones with values.
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "wide.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["a", "b"])
    workbook.active.append(["c", "d"])
    workbook.save(str(f))
    monkeypatch.setattr("bestteam.tools.file_parser._MAX_XLSX_CELLS", 3)
    with pytest.raises(ConfigurationError, match="wide.xlsx.*cells"):
        parse_file(str(f))


def test_parse_file_rejects_unsupported_type(tmp_path):
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG")
    with pytest.raises(ConfigurationError, match="Unsupported file type"):
        parse_file(str(f))


def test_parse_file_pdf_raises_without_package(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    with patch.dict("sys.modules", {"pypdf": None}):
        with pytest.raises(ConfigurationError, match="pypdf"):
            parse_file(str(f))


def test_parse_file_excel_raises_without_package(tmp_path):
    f = tmp_path / "data.xlsx"
    f.write_bytes(b"PK")
    with patch.dict("sys.modules", {"openpyxl": None}):
        with pytest.raises(ConfigurationError, match="openpyxl"):
            parse_file(str(f))


def test_parse_file_excel_cell_shaped_like_a_marker_is_escaped(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")

    f = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Q1"
    ws.append(["note"])
    ws.append(["[Sheet: Q2]"])
    ws.append(["plain"])
    wb.save(str(f))

    lines = parse_file(str(f)).splitlines()
    assert lines.count("[Sheet: Q1]") == 1
    assert "\\[Sheet: Q2]" in lines


def test_parse_file_docx_raises_without_package(tmp_path):
    f = tmp_path / "doc.docx"
    f.write_bytes(b"PK")
    with patch.dict("sys.modules", {"docx": None}):
        with pytest.raises(ConfigurationError, match="python-docx"):
            parse_file(str(f))


def test_parse_file_reads_docx(tmp_path):
    docx = pytest.importorskip("docx")

    f = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_paragraph("Hello from Word.")
    document.add_paragraph("Second paragraph.")
    document.save(str(f))

    result = parse_file(str(f))
    assert "Hello from Word." in result
    assert "Second paragraph." in result


def test_parse_file_reads_xml(tmp_path):
    f = tmp_path / "catalog.xml"
    f.write_text(
        '<?xml version="1.0"?>\n'
        '<catalog>\n'
        '  <book id="bk101">\n'
        '    <title>Widgets Explained</title>\n'
        '  </book>\n'
        '</catalog>\n',
        encoding="utf-8",
    )

    result = parse_file(str(f))
    lines = result.splitlines()
    assert lines[0] == "[XML: catalog.xml]"

    book_line = next(line for line in lines if "<book" in line)
    title_line = next(line for line in lines if "<title" in line)
    assert book_line.strip() == '<book id="bk101">'
    assert title_line.strip() == "<title> Widgets Explained"
    # the title element is nested one level deeper than book
    assert len(title_line) - len(title_line.lstrip(" ")) > len(book_line) - len(book_line.lstrip(" "))


def test_parse_file_rejects_malformed_xml(tmp_path):
    f = tmp_path / "broken.xml"
    f.write_text("<catalog><book></catalog>", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Failed to parse XML file"):
        parse_file(str(f))


def test_parse_file_xml_preserves_mixed_content_tail_text(tmp_path):
    f = tmp_path / "mixed.xml"
    f.write_text(
        "<p>Hello <b>world</b>, how are you?</p>",
        encoding="utf-8",
    )
    result = parse_file(str(f))
    assert "how are you?" in result


def test_parse_file_xml_escapes_quotes_in_attributes(tmp_path):
    f = tmp_path / "quoted.xml"
    f.write_text(
        '<book note="He said &quot;hi&quot;"></book>',
        encoding="utf-8",
    )
    result = parse_file(str(f))
    # the embedded quote must not appear to close the attribute value early
    assert 'note="He said "hi""' not in result
    assert "He said" in result and "hi" in result


def test_parse_file_xml_resolves_namespace_prefixes(tmp_path):
    f = tmp_path / "ns.xml"
    f.write_text(
        '<ns:root xmlns:ns="http://example.com">'
        "<ns:child>text</ns:child>"
        "</ns:root>",
        encoding="utf-8",
    )
    result = parse_file(str(f))
    assert "<ns:root>" in result
    assert "<ns:child>" in result
    assert "{http://example.com}" not in result


def test_parse_file_xml_normalizes_multiline_text(tmp_path):
    f = tmp_path / "pretty.xml"
    f.write_text(
        "<root>\n  <item>\n    line one\n    line two\n  </item>\n</root>",
        encoding="utf-8",
    )
    result = parse_file(str(f))
    item_line = next(line for line in result.splitlines() if "<item>" in line)
    assert item_line.strip() == "<item> line one line two"


def test_parse_file_xml_handles_deeply_nested_elements_without_recursion_error(tmp_path):
    depth = 2000
    xml_content = "<a>" * depth + "text" + "</a>" * depth
    f = tmp_path / "deep.xml"
    f.write_text(xml_content, encoding="utf-8")

    result = parse_file(str(f))
    assert result.count("<a>") == depth


def test_parse_file_reads_docx_tables(tmp_path):
    docx = pytest.importorskip("docx")

    f = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_paragraph("Intro paragraph.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Price"
    table.cell(1, 0).text = "Widget"
    table.cell(1, 1).text = "9.99"
    document.save(str(f))

    result = parse_file(str(f))
    assert "Intro paragraph." in result
    assert "Name" in result
    assert "Price" in result
    assert "Widget" in result
    assert "9.99" in result


def test_parse_file_docx_headings_become_markdown(tmp_path):
    docx = pytest.importorskip("docx")

    f = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_heading("Quarterly Report", 0)
    document.add_heading("Pricing", 1)
    document.add_paragraph("Body text.")
    document.add_heading("Tiers", 3)
    document.add_heading("Footnote", 6)
    document.save(str(f))

    lines = parse_file(str(f)).splitlines()
    assert "# Quarterly Report" in lines
    assert "# Pricing" in lines
    assert "Body text." in lines
    assert "### Tiers" in lines
    # Levels deeper than the chunker's separators clamp to the deepest one.
    assert "#### Footnote" in lines


def test_parse_file_docx_tables_in_document_order(tmp_path):
    docx = pytest.importorskip("docx")

    f = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_paragraph("Before the table.")
    first = document.add_table(rows=1, cols=2)
    first.cell(0, 0).text = "Name"
    first.cell(0, 1).text = "multi\nline"
    document.add_paragraph("Between the tables.")
    second = document.add_table(rows=1, cols=1)
    second.cell(0, 0).text = "Second"
    document.add_paragraph("After the tables.")
    document.save(str(f))

    result = parse_file(str(f))
    order = [
        result.index("Before the table."),
        result.index("[Table 1]"),
        result.index("Between the tables."),
        result.index("[Table 2]"),
        result.index("After the tables."),
    ]
    assert order == sorted(order)
    # A cell's own newlines must not break the one-line-per-row contract the
    # tabular chunker reads.
    assert "Name,multi line" in result.splitlines()
    # A blank line terminates each table block.
    assert "\n\n[Table 1]\nName,multi line\n\n" in result


def test_parse_file_docx_empty_table_row_does_not_end_the_block(tmp_path):
    """A one-column table's spacer row renders as an empty string, which would
    read as the blank line that terminates the block -- dropping every row
    after it out of the table."""
    docx = pytest.importorskip("docx")

    f = tmp_path / "doc.docx"
    document = docx.Document()
    table = document.add_table(rows=4, cols=1)
    table.cell(0, 0).text = "Category"
    table.cell(1, 0).text = "Electronics"
    table.cell(2, 0).text = ""
    table.cell(3, 0).text = "Apparel"
    document.save(str(f))

    result = parse_file(str(f))
    block = result.split("[Table 1]\n", 1)[1]
    assert "\n\n" not in block
    assert block.splitlines() == ["Category", "Electronics", "Apparel"]


def test_parse_file_docx_prose_shaped_like_a_heading_is_escaped(tmp_path):
    """A Normal-styled paragraph that happens to start with `# ` must not be
    readable as a generated heading -- the chunker would cite it as a section
    and cut a chunk boundary at it."""
    docx = pytest.importorskip("docx")

    f = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_heading("Real Section", 1)
    document.add_paragraph("# Not a heading, just prose")
    document.add_paragraph("#### Also not one")
    document.add_paragraph("#5 is safe, no space after the hash")
    document.save(str(f))

    lines = parse_file(str(f)).splitlines()
    assert "# Real Section" in lines
    assert "\\# Not a heading, just prose" in lines
    assert "\\#### Also not one" in lines
    # Only the shape the chunker actually reads as a heading gets escaped.
    assert "#5 is safe, no space after the hash" in lines


def test_parse_file_docx_prose_shaped_like_a_table_marker_is_escaped(tmp_path):
    """The heading escape's twin. A Normal-styled paragraph reading
    `[Table 2]` was read back as a generated table marker, so every paragraph
    after it was indexed as that table's rows and cited as `Table 2`."""
    docx = pytest.importorskip("docx")

    f = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_paragraph("Intro paragraph about pricing.")
    document.add_paragraph("[Table 2]")
    document.add_paragraph("Ordinary prose that follows.")
    document.save(str(f))

    lines = parse_file(str(f)).splitlines()
    assert "\\[Table 2]" in lines
    assert "Ordinary prose that follows." in lines


# ---------------------------------------------------------------------------
# YAML loader integration
# ---------------------------------------------------------------------------

def test_loader_resolves_calculator_tool(tmp_path):
    from bestteam import load_pipeline
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    yaml_text = """
name: tool_test
agents:
  - name: cruncher
    role: Number Cruncher
    goal: Crunch numbers
    model: "fake:42"
    tools: [calculator]
teams:
  - name: math_team
    agents: [cruncher]
    mode: sequential
pipeline:
  steps: [math_team]
"""
    p = tmp_path / "tool_test.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    wf = load_pipeline(str(p))
    agent = wf.steps[0].agents[0]
    assert len(agent.tools) == 1
    assert agent.tools[0] is calculator


def test_loader_raises_for_unknown_tool(tmp_path):
    from bestteam import load_pipeline
    from bestteam.exceptions import ConfigurationError

    yaml_text = """
name: bad_tool
agents:
  - name: agent1
    role: Worker
    goal: Do things
    model: "fake:ok"
    tools: [nonexistent_tool]
teams:
  - name: t1
    agents: [agent1]
    mode: sequential
pipeline:
  steps: [t1]
"""
    p = tmp_path / "bad_tool.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Unknown tool 'nonexistent_tool'"):
        load_pipeline(str(p))


# ---------------------------------------------------------------------------
# ToolKit → YAML loader integration
# ---------------------------------------------------------------------------

_TOOLKIT_YAML = """
name: custom_tool_test
agents:
  - name: helper
    role: Helper
    goal: Help
    model: "fake:done"
    tools: [send_slack]
teams:
  - name: team1
    agents: [helper]
    mode: sequential
pipeline:
  steps: [team1]
"""


def test_loader_resolves_custom_toolkit_tool(tmp_path):
    from bestteam import load_pipeline
    from bestteam.core.tools import ToolKit

    my_tools = ToolKit("company")

    @my_tools.register
    def send_slack(message: str) -> str:
        return "sent"

    p = tmp_path / "custom.yaml"
    p.write_text(_TOOLKIT_YAML, encoding="utf-8")
    wf = load_pipeline(str(p), toolkits=[my_tools])
    agent = wf.steps[0].agents[0]
    assert len(agent.tools) == 1
    assert agent.tools[0] is send_slack


def test_loader_resolves_skill_via_skills_param(tmp_path):
    from bestteam import SkillSpec, load_pipeline

    yaml_text = """
name: skill_test
agents:
  - name: cruncher
    role: Number Cruncher
    goal: Crunch numbers
    model: "fake:42"
    skills: [research_skill]
teams:
  - name: math_team
    agents: [cruncher]
    mode: sequential
pipeline:
  steps: [math_team]
"""
    p = tmp_path / "skill_test.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    research_skill = SkillSpec(
        name="research_skill",
        instructions="Use the calculator for any math.",
        tools=["calculator"],
    )
    wf = load_pipeline(str(p), skills=[research_skill])
    agent = wf.steps[0].agents[0]
    assert agent.tools[0] is calculator
    assert "Use the calculator for any math." in agent.backstory


def test_loader_custom_tool_appears_in_error_message(tmp_path):
    from bestteam import load_pipeline
    from bestteam.core.tools import ToolKit
    from bestteam.exceptions import ConfigurationError

    my_tools = ToolKit("company")

    @my_tools.register
    def send_slack(message: str) -> str:
        return "sent"

    yaml_text = """
name: err_test
agents:
  - name: a
    role: R
    goal: G
    model: "fake:x"
    tools: [not_a_tool]
teams:
  - name: t
    agents: [a]
    mode: sequential
pipeline:
  steps: [t]
"""
    p = tmp_path / "err.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc_info:
        load_pipeline(str(p), toolkits=[my_tools])
    assert "send_slack" in str(exc_info.value)


# ---------------------------------------------------------------------------
# web_search retry
# ---------------------------------------------------------------------------

def test_web_search_retries_on_transient_error(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.search.side_effect = [
        RuntimeError("transient"),
        RuntimeError("transient"),
        {"results": [{"title": "T", "url": "http://x.com", "content": "ok"}]},
    ]
    fake_tavily = MagicMock()
    fake_tavily.TavilyClient.return_value = mock_client
    with patch.dict("sys.modules", {"tavily": fake_tavily}):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            result = web_search("query")
    assert mock_client.search.call_count == 3
    assert mock_sleep.call_count == 2
    assert "T" in result


def test_web_search_raises_after_max_retries(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.search.side_effect = RuntimeError("always fails")
    fake_tavily = MagicMock()
    fake_tavily.TavilyClient.return_value = mock_client
    with patch.dict("sys.modules", {"tavily": fake_tavily}):
        with patch("bestteam.tools._retry.time.sleep"):
            with pytest.raises(RuntimeError, match="always fails"):
                web_search("query")
    assert mock_client.search.call_count == 3


# ---------------------------------------------------------------------------
# http_get retry
# ---------------------------------------------------------------------------

def _make_mock_httpx(side_effects):
    """Build a fake httpx module whose Client.get raises/returns side_effects."""
    import httpx as _real_httpx

    mock_client_instance = MagicMock()
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=False)
    mock_client_instance.get.side_effect = side_effects

    mock_httpx = MagicMock()
    mock_httpx.Client.return_value = mock_client_instance
    mock_httpx.RequestError = _real_httpx.RequestError
    mock_httpx.URL = _real_httpx.URL
    return mock_httpx, mock_client_instance


def test_http_get_retries_on_connection_error(monkeypatch):
    import httpx as _real_httpx

    _patch_getaddrinfo(monkeypatch, "93.184.216.34")

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.text = "ok"
    ok_response.is_redirect = False

    mock_httpx, mock_client_instance = _make_mock_httpx([
        _real_httpx.ConnectError("timeout"),
        _real_httpx.ConnectError("timeout"),
        ok_response,
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            result = http_get("https://example.com")
    assert mock_client_instance.get.call_count == 3
    assert mock_sleep.call_count == 2
    assert "[200]" in result


def test_http_get_retries_on_5xx(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")

    server_error = MagicMock()
    server_error.status_code = 503
    server_error.text = "Service Unavailable"

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.text = "ok"
    ok_response.is_redirect = False

    mock_httpx, mock_client_instance = _make_mock_httpx([server_error, ok_response])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            result = http_get("https://example.com")
    assert mock_client_instance.get.call_count == 2
    assert mock_sleep.call_count == 1
    assert "[200]" in result


def test_http_get_does_not_retry_on_4xx(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")

    not_found = MagicMock()
    not_found.status_code = 404
    not_found.text = "Not Found"
    not_found.is_redirect = False

    mock_httpx, mock_client_instance = _make_mock_httpx([not_found])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            result = http_get("https://example.com")
    assert mock_client_instance.get.call_count == 1
    assert mock_sleep.call_count == 0
    assert "[404]" in result
