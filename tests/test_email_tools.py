"""Tests for the built-in email tools (draft-only read/reply toolkit).

All tests are $0: the Graph backend is exercised against a mocked httpx
module (same style as test_tools.py's http_get tests) and the IMAP backend
against a patched imaplib.IMAP4_SSL. No live mailbox is ever contacted.
"""
import email
from email import policy
from unittest.mock import MagicMock, patch

import pytest

from bestteam.exceptions import ConfigurationError
from bestteam.tools import REGISTRY, email_draft_reply, email_find, email_read

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Registry / backend selection
# ---------------------------------------------------------------------------

def test_registry_contains_email_tools():
    assert {"email_find", "email_read", "email_draft_reply"} <= set(REGISTRY)


@pytest.mark.parametrize("tool_call", [
    lambda: email_find(""),
    lambda: email_read("42"),
    lambda: email_draft_reply("42", "Hello"),
])
def test_email_tools_raise_without_backend_env(monkeypatch, tool_call):
    monkeypatch.delenv("BESTTEAM_EMAIL_BACKEND", raising=False)
    with pytest.raises(ConfigurationError, match="BESTTEAM_EMAIL_BACKEND"):
        tool_call()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "pigeon")
    with pytest.raises(ConfigurationError, match="pigeon"):
        email_find("")


def test_draft_reply_rejects_empty_body(monkeypatch):
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "imap")
    with pytest.raises(ConfigurationError, match="body"):
        email_draft_reply("42", "   ")


# ---------------------------------------------------------------------------
# Graph backend
# ---------------------------------------------------------------------------

@pytest.fixture
def graph_env(monkeypatch):
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "graph")
    monkeypatch.setenv("BESTTEAM_GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.setenv("BESTTEAM_GRAPH_CLIENT_ID", "client-1")
    monkeypatch.setenv("BESTTEAM_GRAPH_CLIENT_SECRET", "secret-1")
    monkeypatch.setenv("BESTTEAM_GRAPH_MAILBOX", "support@example.com")


def _response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


def _make_mock_httpx(side_effects):
    """Fake httpx module whose Client.request raises/returns side_effects."""
    import httpx as _real_httpx

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.side_effect = side_effects

    mock_httpx = MagicMock()
    mock_httpx.Client.return_value = mock_client
    mock_httpx.RequestError = _real_httpx.RequestError
    return mock_httpx, mock_client


_TOKEN_RESPONSE = {"access_token": "tok-123", "token_type": "Bearer"}


def test_graph_raises_without_httpx(graph_env):
    with patch.dict("sys.modules", {"httpx": None}):
        with pytest.raises(ConfigurationError, match="tools-email"):
            email_find("")


def test_graph_raises_with_missing_env(monkeypatch):
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "graph")
    monkeypatch.setenv("BESTTEAM_GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.delenv("BESTTEAM_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("BESTTEAM_GRAPH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("BESTTEAM_GRAPH_MAILBOX", raising=False)
    with pytest.raises(ConfigurationError, match="BESTTEAM_GRAPH_CLIENT_ID"):
        email_find("")


def test_graph_find_unread_lists_messages(graph_env):
    messages = {
        "value": [
            {
                "id": "AAA1",
                "from": {"emailAddress": {"address": "alice@example.com"}},
                "subject": "Order #77 delayed?",
                "receivedDateTime": "2026-07-15T08:00:00Z",
                "bodyPreview": "Hi, my order seems late...",
            }
        ]
    }
    mock_httpx, mock_client = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(200, messages),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_find("")

    assert "AAA1" in result
    assert "alice@example.com" in result
    assert "Order #77 delayed?" in result

    token_call = mock_client.request.call_args_list[0]
    assert token_call.args[0] == "POST"
    assert "tenant-1" in token_call.args[1]
    assert token_call.kwargs["data"]["client_id"] == "client-1"
    assert token_call.kwargs["data"]["grant_type"] == "client_credentials"

    find_call = mock_client.request.call_args_list[1]
    assert find_call.args[0] == "GET"
    assert "/users/support@example.com/messages" in find_call.args[1]
    assert find_call.kwargs["params"]["$filter"] == "isRead eq false"
    assert find_call.kwargs["headers"]["Authorization"] == "Bearer tok-123"


def test_graph_find_with_query_uses_search(graph_env):
    mock_httpx, mock_client = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(200, {"value": []}),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_find("invoice")

    assert "No emails found" in result
    find_call = mock_client.request.call_args_list[1]
    assert find_call.kwargs["params"]["$search"] == '"invoice"'
    assert "$filter" not in find_call.kwargs["params"]


def test_graph_find_empty_unread_returns_string(graph_env):
    mock_httpx, _ = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(200, {"value": []}),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_find("")
    assert "No unread emails" in result


def test_graph_read_returns_headers_and_body(graph_env):
    message = {
        "id": "AAA1",
        "from": {"emailAddress": {"address": "alice@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "support@example.com"}}],
        "subject": "Order #77 delayed?",
        "receivedDateTime": "2026-07-15T08:00:00Z",
        "body": {"contentType": "text", "content": "Hi, where is my order?"},
    }
    mock_httpx, mock_client = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(200, message),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_read("AAA1")

    assert "alice@example.com" in result
    assert "Order #77 delayed?" in result
    assert "Hi, where is my order?" in result
    read_call = mock_client.request.call_args_list[1]
    assert "/messages/AAA1" in read_call.args[1]
    assert read_call.kwargs["headers"]["Prefer"] == 'outlook.body-content-type="text"'


def test_graph_read_missing_message_returns_string(graph_env):
    mock_httpx, _ = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(404, {}, "Not Found"),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_read("GONE")
    assert "No message found" in result
    assert "GONE" in result


def test_graph_read_caps_body(graph_env):
    long_body = "x" * 20_000
    message = {
        "id": "AAA1",
        "from": {"emailAddress": {"address": "a@example.com"}},
        "subject": "Big",
        "receivedDateTime": "2026-07-15T08:00:00Z",
        "body": {"contentType": "text", "content": long_body},
    }
    mock_httpx, _ = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(200, message),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_read("AAA1")
    assert "truncated" in result
    assert len(result) < 10_000


def test_graph_draft_reply_creates_and_patches(graph_env):
    mock_httpx, mock_client = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(201, {"id": "DRAFT9"}),
        _response(200, {"id": "DRAFT9"}),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_draft_reply("AAA1", "Your order ships tomorrow.")

    assert "Draft" in result
    create_call = mock_client.request.call_args_list[1]
    assert create_call.args[0] == "POST"
    assert "/messages/AAA1/createReply" in create_call.args[1]
    patch_call = mock_client.request.call_args_list[2]
    assert patch_call.args[0] == "PATCH"
    assert "/messages/DRAFT9" in patch_call.args[1]
    assert patch_call.kwargs["json"]["body"]["content"] == "Your order ships tomorrow."


def test_graph_draft_reply_missing_message_returns_string(graph_env):
    mock_httpx, _ = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(404, {}, "Not Found"),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = email_draft_reply("GONE", "Hello")
    assert "No message found" in result


def test_graph_retries_on_5xx(graph_env):
    mock_httpx, mock_client = _make_mock_httpx([
        _response(200, _TOKEN_RESPONSE),
        _response(503, {}, "Service Unavailable"),
        _response(200, {"value": []}),
    ])
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            result = email_find("")
    assert mock_client.request.call_count == 3
    assert mock_sleep.call_count == 1
    assert "No unread emails" in result


# ---------------------------------------------------------------------------
# IMAP backend
# ---------------------------------------------------------------------------

@pytest.fixture
def imap_env(monkeypatch):
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "imap")
    monkeypatch.setenv("BESTTEAM_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("BESTTEAM_IMAP_USER", "support@example.com")
    monkeypatch.setenv("BESTTEAM_IMAP_PASSWORD", "secret")
    monkeypatch.delenv("BESTTEAM_IMAP_DRAFTS", raising=False)
    monkeypatch.delenv("BESTTEAM_IMAP_PORT", raising=False)


_RAW_HEADERS = (
    b"From: Alice <alice@example.com>\r\n"
    b"Subject: Order #77 delayed?\r\n"
    b"Date: Tue, 15 Jul 2026 08:00:00 +0000\r\n"
    b"\r\n"
)

_RAW_MESSAGE = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: support@example.com\r\n"
    b"Subject: Order #77 delayed?\r\n"
    b"Date: Tue, 15 Jul 2026 08:00:00 +0000\r\n"
    b"Message-ID: <orig-123@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hi, where is my order?\r\n"
)


def _mock_imap_conn():
    conn = MagicMock()
    conn.login.return_value = ("OK", [b"Logged in"])
    conn.select.return_value = ("OK", [b"1"])
    conn.append.return_value = ("OK", [b"APPEND completed"])
    conn.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])
    return conn


def test_imap_raises_with_missing_env(monkeypatch):
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "imap")
    monkeypatch.delenv("BESTTEAM_IMAP_HOST", raising=False)
    monkeypatch.setenv("BESTTEAM_IMAP_USER", "u")
    monkeypatch.setenv("BESTTEAM_IMAP_PASSWORD", "p")
    with pytest.raises(ConfigurationError, match="BESTTEAM_IMAP_HOST"):
        email_find("")


def test_imap_find_unseen_lists_messages(imap_env):
    conn = _mock_imap_conn()

    def uid(command, *args):
        if command == "search":
            return ("OK", [b"7"])
        if command == "fetch":
            return ("OK", [(b"7 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {90}", _RAW_HEADERS), b")"])
        raise AssertionError(f"unexpected uid command {command}")

    conn.uid.side_effect = uid
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_find("")

    assert "7" in result
    assert "alice@example.com" in result
    assert "Order #77 delayed?" in result
    # Search used UNSEEN; select was readonly so nothing gets marked as read.
    search_call = conn.uid.call_args_list[0]
    assert "UNSEEN" in search_call.args
    conn.select.assert_called_once_with("INBOX", readonly=True)
    fetch_call = conn.uid.call_args_list[1]
    assert "BODY.PEEK" in fetch_call.args[2]


def test_imap_find_empty_returns_string(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [b""])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_find("")
    assert "No unread emails" in result


def test_imap_read_returns_plain_text_body(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [(b"7 (BODY[] {200}", _RAW_MESSAGE), b")"])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read("7")

    assert "alice@example.com" in result
    assert "Hi, where is my order?" in result
    fetch_call = conn.uid.call_args_list[0]
    assert "BODY.PEEK" in fetch_call.args[2]


def test_imap_read_prefers_text_plain_in_multipart(imap_env):
    multipart = (
        b"From: a@example.com\r\n"
        b"To: support@example.com\r\n"
        b"Subject: Multi\r\n"
        b"Message-ID: <m@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/alternative; boundary=BOUND\r\n"
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"plain body here\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<p>html body here</p>\r\n"
        b"--BOUND--\r\n"
    )
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [(b"7 (BODY[] {400}", multipart), b")"])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read("7")
    assert "plain body here" in result
    assert "<p>" not in result


def test_imap_read_missing_message_returns_string(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [None])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_read("999")
    assert "No message found" in result


def test_imap_draft_reply_builds_threaded_mime(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [(b"7 (BODY[] {200}", _RAW_MESSAGE), b")"])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        result = email_draft_reply("7", "Your order ships tomorrow.")

    assert "Draft" in result
    conn.append.assert_called_once()
    folder, flags, _, raw = conn.append.call_args.args
    assert folder == "Drafts"  # fallback when no override / SPECIAL-USE
    assert "\\Draft" in flags
    draft = email.message_from_bytes(raw, policy=policy.default)
    assert draft["To"] == "Alice <alice@example.com>"
    assert draft["Subject"] == "Re: Order #77 delayed?"
    assert draft["In-Reply-To"] == "<orig-123@example.com>"
    assert "<orig-123@example.com>" in draft["References"]
    assert draft["From"] == "support@example.com"
    assert "Your order ships tomorrow." in draft.get_content()


def test_imap_draft_folder_env_override(imap_env, monkeypatch):
    monkeypatch.setenv("BESTTEAM_IMAP_DRAFTS", "INBOX.Entwürfe")
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [(b"7 (BODY[] {200}", _RAW_MESSAGE), b")"])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        email_draft_reply("7", "Hello")
    folder = conn.append.call_args.args[0]
    assert folder == "INBOX.Entwürfe"


def test_imap_draft_folder_from_special_use(imap_env):
    conn = _mock_imap_conn()
    conn.list.return_value = (
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Drafts) "/" "INBOX/My Drafts"',
        ],
    )
    conn.uid.return_value = ("OK", [(b"7 (BODY[] {200}", _RAW_MESSAGE), b")"])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        email_draft_reply("7", "Hello")
    folder = conn.append.call_args.args[0]
    assert folder == "INBOX/My Drafts"


def test_imap_rejects_non_numeric_port(imap_env, monkeypatch):
    monkeypatch.setenv("BESTTEAM_IMAP_PORT", "not-a-port")
    with pytest.raises(ConfigurationError, match="BESTTEAM_IMAP_PORT"):
        email_find("")


def test_imap_find_ascii_query_uses_quoted_text_search(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [b""])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        email_find("invoice")
    search_call = conn.uid.call_args_list[0]
    assert search_call.args == ("search", None, "TEXT", '"invoice"')


def test_imap_find_cjk_query_uses_utf8_literal(imap_env):
    # Non-ASCII search text must go as a UTF-8 literal with CHARSET —
    # imaplib raises if it's passed inline like an ASCII quoted string.
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [b""])
    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        email_find("发票")
    search_call = conn.uid.call_args_list[0]
    assert search_call.args == ("search", "CHARSET", "UTF-8", "TEXT")
    assert conn.literal == "发票".encode("utf-8")


def test_imap_retries_on_connect_oserror(imap_env):
    conn = _mock_imap_conn()
    conn.uid.return_value = ("OK", [b""])
    with patch(
        "bestteam.tools.email_client.imaplib.IMAP4_SSL",
        side_effect=[OSError("connection refused"), conn],
    ):
        with patch("bestteam.tools._retry.time.sleep") as mock_sleep:
            result = email_find("")
    assert mock_sleep.call_count == 1
    assert "No unread emails" in result
