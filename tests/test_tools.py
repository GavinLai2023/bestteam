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
        "email_find", "email_read", "email_draft_reply",
        "local_business_search",
    }


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


# ---------------------------------------------------------------------------
# YAML loader integration
# ---------------------------------------------------------------------------

def test_loader_resolves_calculator_tool(tmp_path):
    from bestteam import load_workflow
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
workflow:
  steps: [math_team]
"""
    p = tmp_path / "tool_test.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    wf = load_workflow(str(p))
    agent = wf.steps[0].agents[0]
    assert len(agent.tools) == 1
    assert agent.tools[0] is calculator


def test_loader_raises_for_unknown_tool(tmp_path):
    from bestteam import load_workflow
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
workflow:
  steps: [t1]
"""
    p = tmp_path / "bad_tool.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Unknown tool 'nonexistent_tool'"):
        load_workflow(str(p))


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
workflow:
  steps: [team1]
"""


def test_loader_resolves_custom_toolkit_tool(tmp_path):
    from bestteam import load_workflow
    from bestteam.core.tools import ToolKit

    my_tools = ToolKit("company")

    @my_tools.register
    def send_slack(message: str) -> str:
        return "sent"

    p = tmp_path / "custom.yaml"
    p.write_text(_TOOLKIT_YAML, encoding="utf-8")
    wf = load_workflow(str(p), toolkits=[my_tools])
    agent = wf.steps[0].agents[0]
    assert len(agent.tools) == 1
    assert agent.tools[0] is send_slack


def test_loader_resolves_skill_via_skills_param(tmp_path):
    from bestteam import SkillSpec, load_workflow

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
workflow:
  steps: [math_team]
"""
    p = tmp_path / "skill_test.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    research_skill = SkillSpec(
        name="research_skill",
        instructions="Use the calculator for any math.",
        tools=["calculator"],
    )
    wf = load_workflow(str(p), skills=[research_skill])
    agent = wf.steps[0].agents[0]
    assert agent.tools[0] is calculator
    assert "Use the calculator for any math." in agent.backstory


def test_loader_custom_tool_appears_in_error_message(tmp_path):
    from bestteam import load_workflow
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
workflow:
  steps: [t]
"""
    p = tmp_path / "err.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc_info:
        load_workflow(str(p), toolkits=[my_tools])
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
