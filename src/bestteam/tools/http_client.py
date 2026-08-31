from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlsplit, urlunsplit

from ..exceptions import ConfigurationError
from ._retry import with_retry

_TIMEOUT_SECONDS = 30
_MAX_REDIRECTS = 5

# Two limits, because the body serves two purposes. Extracted page text is
# prose an agent reads, so it gets the same 8,000 characters an email
# attachment does (see `email_client.py`) -- roughly a medium article. A
# non-HTML body is a REST response the caller parses, and 8,000 would break
# the API use this tool has had all along, so it gets a much larger cap whose
# only job is to stop an unbounded response from blowing up the context.
_MAX_TEXT_CHARS = 8_000
_MAX_RAW_CHARS = 50_000

# `text_content()` treats these as page text, so a page's stylesheet and its
# analytics snippet would arrive as the "article" and eat the whole budget.
_NON_TEXT_TAGS = ("script", "style", "noscript", "template")

_BLOCKED_IP_PREDICATES = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_reserved",
    "is_multicast",
    "is_unspecified",
)


def check_host_allowed(hostname: str) -> str:
    """Resolve `hostname` and return the first IP, rejecting private/internal ones.

    Guards against SSRF by rejecting any address that's private, loopback,
    link-local (incl. 169.254.169.254), reserved, multicast, or unspecified.
    Returns the first resolved address as a string so the caller can pin the
    connection to it (closing the DNS-rebinding TOCTOU, CR-023). Reused by the
    IMAP mailbox-connection endpoints, where the host is customer-supplied.
    """
    if not hostname:
        raise ConfigurationError("Could not determine host to connect to")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        # Don't embed the raw OS resolver exception in the message: it's
        # relayed verbatim to customers by the mailbox-connection endpoints
        # (ui/backend/org_settings.py) and can carry OS-specific internals.
        raise ConfigurationError(f"Could not resolve host '{hostname}'") from exc

    validated_ip: str = ""
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(getattr(ip, predicate) for predicate in _BLOCKED_IP_PREDICATES):
            # Same reasoning: don't leak the resolved private/internal IP to
            # a customer-facing caller.
            raise ConfigurationError(
                f"host '{hostname}' resolves to a private/internal address"
            )
        if not validated_ip:
            validated_ip = str(ip)

    if not validated_ip:
        raise ConfigurationError(f"Could not resolve host '{hostname}'")
    return validated_ip


def _check_host_allowed(url: str) -> str:
    """URL variant used by `http_get` -- keeps the URL in the error message."""
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ConfigurationError(f"Invalid URL '{url}': could not determine host")
    try:
        return check_host_allowed(hostname)
    except ConfigurationError as exc:
        raise ConfigurationError(f"Refusing to fetch '{url}': {exc}") from exc


def _pin_to_ip(url: str, ip: str) -> tuple[str, str, str]:
    """Rewrite `url` to connect to `ip`, keeping the hostname for Host/SNI.

    Returns `(connect_url, host_header, sni_hostname)`: `connect_url` targets the
    validated `ip` (so httpx never re-resolves), `host_header` carries the
    original host[:port] so virtual hosts still work, and `sni_hostname` is the
    hostname for https TLS SNI + cert verification (empty for http).
    """
    parts = urlsplit(url)
    host_header = parts.hostname or ""
    if parts.port is not None:
        host_header = f"{host_header}:{parts.port}"

    bracket_ip = f"[{ip}]" if ipaddress.ip_address(ip).version == 6 else ip
    netloc = f"{bracket_ip}:{parts.port}" if parts.port is not None else bracket_ip
    connect_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

    sni_hostname = parts.hostname if parts.scheme == "https" else ""
    return connect_url, host_header, sni_hostname


def _html_to_text(html: str) -> str | None:
    """The visible text of an HTML page, or None if it can't be extracted.

    None means "use the raw body": lxml is not required to install this tool
    (`bestteam[tools-http]` is httpx alone for anyone who installed it before
    this), and a page lxml refuses to parse is still worth returning as-is. A
    tool that started raising on every HTML page would be a worse regression
    than the markup it was returning.
    """
    try:
        from lxml import etree, html as lxml_html
    except ImportError:
        return None

    try:
        root = lxml_html.fromstring(html)
    except Exception:
        return None

    etree.strip_elements(root, *_NON_TEXT_TAGS, with_tail=False)
    lines = [line.strip() for line in root.text_content().splitlines()]
    # Markup indentation leaves a blank line per nested element; collapsing
    # runs of them is what makes the character budget buy actual prose.
    text = "\n".join(line for line in lines if line)
    return text or None


def _truncated(body: str, limit: int, unit: str) -> str:
    """`body` capped at `limit`, announcing the cut so the model can relay it.

    An answer built from a silently truncated page is indistinguishable from
    one built from the whole page -- the same reason an over-long attachment
    says so rather than just stopping.
    """
    if len(body) <= limit:
        return body
    return f"{body[:limit]}\n\n[Truncated: {len(body) - limit:,} {unit} omitted.]"


def http_get(url: str, headers_json: str = "{}") -> str:
    """Make an HTTP GET request and return the response body as text.

    Useful for calling REST APIs, fetching JSON feeds, or reading the full
    text of a web page whose URL you already have -- for example one that a
    web_search result listed. An HTML page is returned as readable text with
    the markup, scripts and styling removed. For authenticated endpoints, pass
    credentials via headers. Automatically retries on connection errors and 5xx
    responses (up to 3 attempts, exponential backoff). 4xx responses are
    returned as-is. A very long response is truncated, and says so where it was
    cut.

    Args:
        url: The URL to fetch (must start with http:// or https://).
        headers_json: Optional JSON string of additional HTTP headers,
            e.g. '{"Authorization": "Bearer <token>", "Accept": "application/json"}'.

    Returns:
        String containing the HTTP status code and response body,
        e.g. "[200] https://api.example.com/data\\n\\n{...}".
    """
    try:
        import httpx
    except ImportError as exc:
        raise ConfigurationError(
            "http_get requires the 'httpx' package. "
            "Install it with: pip install httpx"
        ) from exc

    if not url.startswith(("http://", "https://")):
        raise ConfigurationError(
            f"Invalid URL '{url}': must start with http:// or https://"
        )

    try:
        headers = json.loads(headers_json)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"headers_json is not valid JSON: {exc}"
        ) from exc

    if not isinstance(headers, dict):
        raise ConfigurationError("headers_json must be a JSON object (dict), not a list or scalar")

    class _ServerError(Exception):
        def __init__(self, response):
            self.response = response

    def _do_request(connect_url, host_header, sni_hostname):
        request_headers = {**headers, "Host": host_header}
        extensions = {"sni_hostname": sni_hostname} if sni_hostname else {}
        try:
            with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = client.get(connect_url, headers=request_headers, extensions=extensions)
        except httpx.RequestError as exc:
            raise ConfigurationError(f"HTTP request failed: {exc}") from exc
        if response.status_code >= 500:
            raise _ServerError(response)
        return response

    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        # Validate + pin per hop: the connection targets the just-validated IP,
        # closing the DNS-rebinding window (CR-023).
        validated_ip = _check_host_allowed(current_url)
        connect_url, host_header, sni_hostname = _pin_to_ip(current_url, validated_ip)
        try:
            response = with_retry(
                lambda: _do_request(connect_url, host_header, sni_hostname),
                retriable_exc=(_ServerError, ConfigurationError),
            )
        except _ServerError as exc:
            response = exc.response

        location = response.headers.get("location") if response.is_redirect else None
        if not location:
            break
        current_url = str(httpx.URL(current_url).join(location))
    else:
        raise ConfigurationError(f"Too many redirects (>{_MAX_REDIRECTS}) while fetching '{url}'")

    body = response.text
    content_type = str(response.headers.get("content-type") or "").lower()
    extracted = _html_to_text(body) if content_type.startswith("text/html") else None
    if extracted is not None:
        body = _truncated(extracted, _MAX_TEXT_CHARS, "characters of text")
    else:
        body = _truncated(body, _MAX_RAW_CHARS, "characters")

    return f"[{response.status_code}] {current_url}\n\n{body}"
