from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlsplit, urlunsplit

from ..exceptions import ConfigurationError
from ._retry import with_retry

_TIMEOUT_SECONDS = 30
_MAX_REDIRECTS = 5

_BLOCKED_IP_PREDICATES = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_reserved",
    "is_multicast",
    "is_unspecified",
)


def _check_host_allowed(url: str) -> str:
    """Validate `url`'s host and return the IP the request must connect to.

    Guards against SSRF (e.g. an agent fetching cloud metadata endpoints or
    internal services) by resolving the hostname and rejecting any address
    that's private, loopback, link-local (incl. 169.254.169.254), reserved,
    multicast, or unspecified.

    Returns the first resolved address as a string so the caller can pin the
    connection to it. Pinning closes the DNS-rebinding TOCTOU (CR-023): without
    it, `httpx` would independently re-resolve the hostname when connecting, so
    an attacker-controlled name could pass this check as a public address and
    then resolve to a private one for the actual request.
    """
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ConfigurationError(f"Invalid URL '{url}': could not determine host")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise ConfigurationError(f"Could not resolve host '{hostname}': {exc}") from exc

    validated_ip: str = ""
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(getattr(ip, predicate) for predicate in _BLOCKED_IP_PREDICATES):
            raise ConfigurationError(
                f"Refusing to fetch '{url}': host '{hostname}' resolves to a "
                f"private/internal address ({ip})"
            )
        if not validated_ip:
            validated_ip = str(ip)

    if not validated_ip:
        raise ConfigurationError(f"Could not resolve host '{hostname}'")
    return validated_ip


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


def http_get(url: str, headers_json: str = "{}") -> str:
    """Make an HTTP GET request and return the response body as text.

    Useful for calling REST APIs, fetching JSON feeds, or reading public
    web resources. For authenticated endpoints, pass credentials via headers.
    Automatically retries on connection errors and 5xx responses (up to 3
    attempts, exponential backoff). 4xx responses are returned as-is.

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

    return f"[{response.status_code}] {current_url}\n\n{response.text}"
