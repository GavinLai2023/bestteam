"""Tests for the built-in tools package."""
import json
from unittest.mock import MagicMock, patch

import pytest

from bestteam.exceptions import ConfigurationError
from bestteam.tools import REGISTRY, calculator, http_get, parse_file, web_search


# ---------------------------------------------------------------------------
# REGISTRY
# ---------------------------------------------------------------------------

def test_registry_contains_all_tools():
    assert set(REGISTRY) == {"web_search", "parse_file", "http_get", "calculator"}


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
# http_get
# ---------------------------------------------------------------------------

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


def test_http_get_returns_status_and_body():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '{"ok": true}'
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
