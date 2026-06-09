from __future__ import annotations

import json

from ..exceptions import ConfigurationError

_TIMEOUT_SECONDS = 30


def http_get(url: str, headers_json: str = "{}") -> str:
    """Make an HTTP GET request and return the response body as text.

    Useful for calling REST APIs, fetching JSON feeds, or reading public
    web resources. For authenticated endpoints, pass credentials via headers.

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

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise ConfigurationError(f"HTTP request failed: {exc}") from exc

    return f"[{response.status_code}] {url}\n\n{response.text}"
