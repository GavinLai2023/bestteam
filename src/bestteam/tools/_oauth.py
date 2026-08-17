"""App-only (client-credentials) OAuth tokens for mailbox access.

Stdlib only, on purpose. The per-org IMAP path has no third-party HTTP
dependency today -- ``pyproject.toml`` marks ``tools-email``'s httpx as "Graph
backend only; the IMAP backend is stdlib", and the ``backend-optional-deps`` CI
job runs without optional extras. Adding a dependency to reach one well-known
public endpoint is not worth it.

The token endpoint is a fixed constant, never customer-supplied, so the
``check_host_allowed`` SSRF guard that customer-supplied IMAP hosts get does
not apply here. Only the tenant ID comes from the customer, and it is
percent-encoded into the path.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from ..exceptions import ConfigurationError
from ._retry import with_retry

_TIMEOUT_SECONDS = 30
# Refresh this long before the token really expires, so one can't die midway
# through an IMAP session that was authenticated with it.
_EXPIRY_MARGIN_SECONDS = 60
_LOGIN_BASE = "https://login.microsoftonline.com"
_EXCHANGE_SCOPE = "https://outlook.office365.com/.default"


class _TokenServerError(Exception):
    """A 5xx or transport failure from the token endpoint -- worth retrying."""


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Microsoft's `error_description` from an OAuth error body.

    The descriptions name the actual problem ("AADSTS7000215: Invalid client
    secret provided"), which is what the API layer turns into customer-facing
    language. Falls back to the raw body, then to the HTTP reason.
    """
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 -- the body is best-effort context only
        return exc.reason or ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw[:500]
    return str(parsed.get("error_description") or parsed.get("error") or raw)[:500]


class MicrosoftClientCredentialsToken:
    """App-only access token for one Exchange Online mailbox.

    Caches the token until shortly before it expires. Caching it forever would
    break the autonomous poller about an hour after the process starts.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str = _EXCHANGE_SCOPE,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    @property
    def url(self) -> str:
        tenant = urllib.parse.quote(self._tenant_id, safe="")
        return f"{_LOGIN_BASE}/{tenant}/oauth2/v2.0/token"

    def token(self) -> str:
        """A valid access token, from cache when one is still good."""
        if self._access_token is not None and time.monotonic() < self._expires_at:
            return self._access_token
        payload = self._fetch()
        access_token = payload.get("access_token")
        if not access_token:
            raise ConfigurationError("Microsoft's sign-in response contained no access token.")
        try:
            lifetime = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        self._access_token = str(access_token)
        self._expires_at = time.monotonic() + max(lifetime - _EXPIRY_MARGIN_SECONDS, 0.0)
        return self._access_token

    def _fetch(self) -> Dict[str, Any]:
        body = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": self._scope,
                "grant_type": "client_credentials",
            }
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        def _do() -> Dict[str, Any]:
            try:
                with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # HTTPError subclasses URLError, so this clause must stay first.
                detail = _error_detail(exc)
                if exc.code >= 500:
                    raise _TokenServerError(detail) from exc
                # A 4xx is a configuration problem; retrying it would only make
                # the customer wait longer for the same answer.
                raise ConfigurationError(
                    f"Microsoft rejected the application's sign-in ({exc.code}): {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                raise _TokenServerError(str(exc.reason)) from exc

        try:
            return with_retry(_do, retriable_exc=(_TokenServerError,))
        except _TokenServerError as exc:
            raise ConfigurationError(
                f"Microsoft's sign-in service could not be reached: {exc}"
            ) from exc
