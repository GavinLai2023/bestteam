# Email Phase 2 — Microsoft 365 mailbox connections (OAuth) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an organisation on Microsoft 365 / Exchange Online connect its
mailbox, which is impossible today because the per-org path only ever builds a
basic-auth IMAP login and Microsoft removed basic auth.

**Architecture:** Exchange Online accepts OAuth over IMAP (SASL `XOAUTH2`,
client-credentials grant), so this changes *how a connection authenticates*,
not what protocol it speaks. A new stdlib-only token provider fetches an
app-only access token; `_ImapBackend._connect()` gains a second auth branch;
the credential row records which branch to take. Everything above
`_connect()` — polling, the UID cursor, drafts resolution, Phase 0's
source-key headers, Phase 1's event ledger, the whole of
`ui/backend/email_trigger.py` — is untouched.

**Tech Stack:** Python stdlib `urllib.request` + `imaplib`, SQLAlchemy 2.0 +
Alembic, FastAPI/Pydantic v2, React + Vite + Vitest.

**Spec:** `docs/superpowers/specs/2026-08-17-email-phase-2-microsoft-oauth-design.md`

## Global Constraints

- **No new dependency.** The token POST uses stdlib `urllib.request`, never
  `httpx`. `pyproject.toml` marks `tools-email`'s httpx as "Graph backend
  only; the IMAP backend is stdlib", and the `backend-optional-deps` CI job
  runs without optional extras.
- **Do not modify `ui/backend/email_trigger.py` or `ui/backend/runtime.py`.**
  If a task appears to need a change there, the design is wrong — stop and ask.
- **Do not modify `_GraphBackend`, `_ImapBackend.from_env()`, or any
  `BESTTEAM_IMAP_*` / `BESTTEAM_GRAPH_*` env handling.** The env path is
  explicitly out of scope.
- **No `MailboxConnector` protocol, no Graph-native code, no Gmail.**
- Auth type values are exactly `"password"` and `"microsoft_oauth"`.
- The Exchange Online IMAP host is exactly `outlook.office365.com`, set
  server-side and never taken from the client.
- The OAuth token scope is exactly `https://outlook.office365.com/.default`.
- Every test file needs a `pytestmark` — `tests/test_marker_completeness.py`
  fails the suite otherwise.
- Comments and user-facing copy in **English**; British spelling in prose
  (organisation, recognise, behaviour). Note that identifiers copied from
  Microsoft (`authorization`, `Organization` model) keep their spelling.
- Run the venv Python: `.\.venv\Scripts\python.exe -m pytest`.
- Final verification is the full non-e2e suite **serial, one process** (the
  `backend-full` equivalent): `.\.venv\Scripts\python.exe -m pytest -m "not e2e"`.

## File Structure

| File | Responsibility |
|---|---|
| `src/bestteam/tools/_oauth.py` (new, ~110 lines) | Fetch + cache an app-only access token. Knows nothing about IMAP. |
| `src/bestteam/tools/email_client.py` (modify) | `_xoauth2_authobject`; `_ImapBackend` takes `token_provider=`; `_connect()` branches. |
| `ui/backend/db/models.py` (modify) | Three columns on `OrgEmailCredential`. |
| `alembic/versions/i6j7k8l9m0n1_add_oauth_email_credentials.py` (new) | The migration. |
| `ui/backend/db/email_credentials.py` (modify) | Auth-type constants + CRUD keywords. |
| `ui/backend/email_tools.py` (modify) | `build_org_imap_backend` dispatches on `auth_type`. |
| `ui/backend/org_settings.py` (modify) | Request validation, backend construction, customer-facing errors, `GET` payload. |
| `ui/backend/admin.py` (modify) | `set-email --auth microsoft-oauth`. |
| `ui/frontend/src/lib/types.ts`, `lib/api.ts`, `components/EmailConnect.tsx` (modify) | Provider choice in the wizard. |
| `ui/frontend/src/components/EmailConnect.test.tsx` (new) | Its tests. |

---

### Task 1: OAuth token provider

**Files:**
- Create: `src/bestteam/tools/_oauth.py`
- Test: `tests/test_oauth_token.py` (new)

**Interfaces:**
- Consumes: `bestteam.exceptions.ConfigurationError`, `bestteam.tools._retry.with_retry`.
- Produces: `MicrosoftClientCredentialsToken(*, tenant_id: str, client_id: str, client_secret: str, scope: str = "https://outlook.office365.com/.default")` with `.token() -> str` and `.url -> str`. Tasks 2, 4, 5 and 6 construct it.

- [ ] **Step 1: Write the failing tests**

```python
"""OAuth client-credentials token tests -- no network is ever touched.

`urlopen` is replaced with a fake that records the request and returns a
canned body, so these assert the exact wire shape (URL, form fields) and the
caching lifecycle rather than a mocked-out approximation of them.
"""

import json
import urllib.error
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from bestteam.exceptions import ConfigurationError
from bestteam.tools import _oauth
from bestteam.tools._oauth import MicrosoftClientCredentialsToken


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _provider(**overrides):
    kwargs = {"tenant_id": "tenant-1", "client_id": "client-1", "client_secret": "shh"}
    kwargs.update(overrides)
    return MicrosoftClientCredentialsToken(**kwargs)


def _http_error(code, payload):
    return urllib.error.HTTPError(
        "https://login.microsoftonline.com/x", code, "err", {},
        io.BytesIO(json.dumps(payload).encode()),
    )


def test_the_token_request_posts_client_credentials_to_the_tenant_endpoint():
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return _FakeResponse({"access_token": "abc", "expires_in": 3599})

    with patch.object(_oauth.urllib.request, "urlopen", fake_urlopen):
        assert _provider().token() == "abc"

    assert len(calls) == 1
    request = calls[0]
    assert request.full_url == (
        "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    )
    body = urllib.parse.parse_qs(request.data.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert body["client_id"] == ["client-1"]
    assert body["client_secret"] == ["shh"]
    assert body["scope"] == ["https://outlook.office365.com/.default"]


def test_a_tenant_id_with_url_characters_is_percent_encoded_into_the_path():
    with patch.object(_oauth.urllib.request, "urlopen",
                      lambda *a, **k: _FakeResponse({"access_token": "abc", "expires_in": 3599})):
        provider = _provider(tenant_id="a/b?c")
        assert provider.url.endswith("/a%2Fb%3Fc/oauth2/v2.0/token")


def test_the_token_is_cached_and_not_refetched_within_its_lifetime():
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return _FakeResponse({"access_token": "abc", "expires_in": 3599})

    provider = _provider()
    with patch.object(_oauth.urllib.request, "urlopen", fake_urlopen):
        assert provider.token() == "abc"
        assert provider.token() == "abc"
    assert len(calls) == 1, "the second call should have come from the cache"


def test_the_token_is_refetched_once_it_is_inside_the_expiry_margin():
    """A cache-forever token would break the poller an hour after start."""
    tokens = iter(["first", "second"])

    def fake_urlopen(request, timeout=None):
        # 90s lifetime with a 60s margin => usable for 30s.
        return _FakeResponse({"access_token": next(tokens), "expires_in": 90})

    clock = [1000.0]
    provider = _provider()
    with patch.object(_oauth.urllib.request, "urlopen", fake_urlopen), \
         patch.object(_oauth.time, "monotonic", lambda: clock[0]):
        assert provider.token() == "first"
        clock[0] += 29
        assert provider.token() == "first"
        clock[0] += 2  # now 31s in: inside the margin
        assert provider.token() == "second"


def test_a_lifetime_shorter_than_the_margin_never_caches():
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return _FakeResponse({"access_token": "abc", "expires_in": 10})

    provider = _provider()
    with patch.object(_oauth.urllib.request, "urlopen", fake_urlopen):
        provider.token()
        provider.token()
    assert len(calls) == 2


def test_a_rejected_client_secret_surfaces_microsofts_own_description():
    def fake_urlopen(request, timeout=None):
        raise _http_error(401, {
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided.",
        })

    with patch.object(_oauth.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(ConfigurationError, match="AADSTS7000215"):
            _provider().token()


def test_a_client_error_is_not_retried_but_a_server_error_is():
    attempts = {"4xx": 0, "5xx": 0}

    def bad_request(request, timeout=None):
        attempts["4xx"] += 1
        raise _http_error(400, {"error_description": "nope"})

    def server_error(request, timeout=None):
        attempts["5xx"] += 1
        raise _http_error(503, {"error_description": "busy"})

    with patch("bestteam.tools._retry.time.sleep"):
        with patch.object(_oauth.urllib.request, "urlopen", bad_request):
            with pytest.raises(ConfigurationError):
                _provider().token()
        with patch.object(_oauth.urllib.request, "urlopen", server_error):
            with pytest.raises(ConfigurationError):
                _provider().token()

    assert attempts["4xx"] == 1, "a configuration error must fail fast"
    assert attempts["5xx"] == 3, "a transient error uses the shared retry budget"


def test_a_response_without_an_access_token_is_a_configuration_error():
    with patch.object(_oauth.urllib.request, "urlopen",
                      lambda *a, **k: _FakeResponse({"token_type": "Bearer"})):
        with pytest.raises(ConfigurationError, match="no access token"):
            _provider().token()
```

Add `import io` and `import urllib.parse` to the test imports (used by
`_http_error` and the body assertion).

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_oauth_token.py -q`
Expected: collection error — `No module named 'bestteam.tools._oauth'`.

- [ ] **Step 3: Write the implementation**

Create `src/bestteam/tools/_oauth.py`:

```python
"""App-only (client-credentials) OAuth tokens for mailbox access.

Stdlib only, on purpose. The per-org IMAP path has no third-party HTTP
dependency today -- `pyproject.toml` marks `tools-email`'s httpx as "Graph
backend only; the IMAP backend is stdlib", and the `backend-optional-deps` CI
job runs without optional extras. Adding a dependency to reach one well-known
public endpoint is not worth it.

The token endpoint is a fixed constant, never customer-supplied, so the
`check_host_allowed` SSRF guard that customer-supplied IMAP hosts get does not
apply here. Only the tenant ID comes from the customer, and it is
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
            raise ConfigurationError(
                "Microsoft's sign-in response contained no access token."
            )
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
```

Note `urllib.error.HTTPError` subclasses `URLError`, so the `HTTPError` clause
must stay first.

- [ ] **Step 4: Run to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_oauth_token.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/tools/_oauth.py tests/test_oauth_token.py
git commit -m "feat(email): app-only OAuth token provider for mailbox access"
```

---

### Task 2: `_ImapBackend` authenticates with XOAUTH2

**Files:**
- Modify: `src/bestteam/tools/email_client.py` (`_ImapBackend.__init__`, `_connect`; new module-level `_xoauth2_authobject`)
- Test: `tests/test_email_tools.py` (append)

**Interfaces:**
- Consumes: `MicrosoftClientCredentialsToken` (Task 1) — only its `.token() -> str`; the backend duck-types the provider so tests can pass a stub.
- Produces: `_ImapBackend(*, host, user, password=None, port=993, drafts=None, restrict_to_public=False, token_provider=None)`. Tasks 4, 5 and 6 construct it both ways.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_tools.py` (it already has `pytestmark =
pytest.mark.unit`, `from unittest.mock import MagicMock, patch`, and
`_mock_imap_conn()`):

```python
class _StubTokenProvider:
    """Duck-types MicrosoftClientCredentialsToken.token()."""

    def __init__(self, token="tok-1", error=None):
        self._token = token
        self._error = error
        self.calls = 0

    def token(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._token


def test_the_xoauth2_initial_response_is_the_sasl_client_first_string():
    from bestteam.tools.email_client import _xoauth2_authobject

    authobject = _xoauth2_authobject("support@acme.com", "tok-1")
    # imaplib base64-encodes whatever this returns, so it must be the raw
    # SASL bytes: user=<addr>^Aauth=Bearer <token>^A^A
    assert authobject(b"") == b"user=support@acme.com\x01auth=Bearer tok-1\x01\x01"


def test_the_xoauth2_authobject_answers_a_rejection_challenge_with_an_empty_line():
    """Exchange sends a base64 JSON error and waits for an empty client
    response before issuing the tagged NO. Returning anything else stalls the
    exchange until the socket timeout."""
    from bestteam.tools.email_client import _xoauth2_authobject

    authobject = _xoauth2_authobject("support@acme.com", "tok-1")
    authobject(b"")
    assert authobject(b'{"status":"401"}') == b""


def test_connect_authenticates_with_xoauth2_when_a_token_provider_is_given():
    conn = _mock_imap_conn()
    provider = _StubTokenProvider("tok-1")
    backend = _ImapBackend(host="outlook.office365.com", user="support@acme.com",
                           token_provider=provider)

    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        backend._connect()

    conn.login.assert_not_called()
    mechanism, authobject = conn.authenticate.call_args.args
    assert mechanism == "XOAUTH2"
    assert authobject(b"") == b"user=support@acme.com\x01auth=Bearer tok-1\x01\x01"


def test_connect_still_uses_a_password_login_when_no_token_provider_is_given():
    conn = _mock_imap_conn()
    backend = _ImapBackend(host="imap.example.com", user="u", password="p")

    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        backend._connect()

    conn.login.assert_called_once_with("u", "p")
    conn.authenticate.assert_not_called()


def test_a_backend_needs_exactly_one_of_password_or_token_provider():
    with pytest.raises(ConfigurationError, match="neither"):
        _ImapBackend(host="h", user="u")
    with pytest.raises(ConfigurationError, match="both"):
        _ImapBackend(host="h", user="u", password="p",
                     token_provider=_StubTokenProvider())


def test_a_token_failure_is_raised_before_any_socket_is_opened():
    """A credential problem must not leave a connection dangling, and its error
    is about credentials rather than connectivity -- so it stays a token error
    instead of being remapped to a sign-in-refused message."""
    provider = _StubTokenProvider(error=ConfigurationError("AADSTS7000215: bad secret"))
    backend = _ImapBackend(host="outlook.office365.com", user="support@acme.com",
                           token_provider=provider)

    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL") as imap:
        with pytest.raises(ConfigurationError, match="AADSTS7000215"):
            backend._connect()
    imap.assert_not_called()


def test_a_refused_xoauth2_exchange_is_reported_as_a_refused_sign_in():
    conn = _mock_imap_conn()
    conn.authenticate.side_effect = imaplib.IMAP4.error("AUTHENTICATE failed")
    backend = _ImapBackend(host="outlook.office365.com", user="support@acme.com",
                           token_provider=_StubTokenProvider())

    with patch("bestteam.tools.email_client.imaplib.IMAP4_SSL", return_value=conn):
        with pytest.raises(ConfigurationError, match="refused"):
            backend._connect()
```

If `imaplib` and `_ImapBackend` are not already imported at the top of
`tests/test_email_tools.py`, add `import imaplib` and include `_ImapBackend`
in the `from bestteam.tools.email_client import ...` line.

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py -q -k "xoauth2 or token_provider or exactly_one or refused_sign_in or password_login"`
Expected: FAIL — `_xoauth2_authobject` does not exist; `_ImapBackend()` raises
`TypeError` for the unexpected `token_provider` keyword.

- [ ] **Step 3: Write the implementation**

In `src/bestteam/tools/email_client.py`, add above `class _ImapBackend`:

```python
def _xoauth2_authobject(user: str, access_token: str):
    """SASL XOAUTH2 exchange: the client-first response, then an empty line.

    `imaplib._Authenticator` base64-*decodes* the server's continuation before
    calling this and base64-*encodes* whatever comes back, and
    `authenticate()` invokes it on the server's first continuation -- so the
    client-first initial response is delivered by returning it from the first
    call, the same path `imaplib.login_cram_md5` relies on.

    On rejection Exchange sends a base64 JSON error and waits for an empty
    client response before issuing the tagged NO; returning b"" there lets the
    exchange finish instead of stalling until the socket timeout.
    """
    initial = f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()
    state = {"sent": False}

    def authobject(_challenge: bytes) -> bytes:
        if state["sent"]:
            return b""
        state["sent"] = True
        return initial

    return authobject
```

Change `_ImapBackend.__init__`'s signature and add the guard plus the new
attribute (leave every other line as it is):

```python
    def __init__(
        self,
        *,
        host: str,
        user: str,
        password: Optional[str] = None,
        port: int = 993,
        drafts: Optional[str] = None,
        restrict_to_public: bool = False,
        token_provider: Any = None,
    ) -> None:
        # Exactly one credential: a password (basic auth) or a token provider
        # (OAuth / SASL XOAUTH2). Caught here rather than at connect time so a
        # miswired caller fails where it is wrong.
        if (password is None) == (token_provider is None):
            raise ConfigurationError(
                "_ImapBackend needs exactly one of `password` or `token_provider`, got "
                + ("both" if password is not None else "neither")
            )
        self._host = host
        self._user = user
        self._password = password
        self._port = port
        self._drafts_override = (drafts or "").strip() or None
        self._token_provider = token_provider
        # Customer-supplied hosts (per-org store) validate + pin to a public IP
        # on every connect; the operator-trusted env path may point at an
        # internal IMAP server, so it stays False.
        self._restrict_to_public = restrict_to_public
```

In `_connect()`, fetch the token first, then branch at authentication:

```python
    def _connect(self):
        # Fetch the OAuth token before dialling: a credential problem should not
        # leave a socket open, and it is a credential error rather than a
        # connectivity one, so it must not be remapped below.
        access_token = None if self._token_provider is None else self._token_provider.token()
        # Verify the server's certificate against the hostname (an explicit
        # default context; imaplib's fallback does not verify), and bound every
        # socket operation so a stalled server can't pin a worker forever.
        ssl_context = ssl.create_default_context()
        ...  # (the factory selection is unchanged)
        # Retry only network-level connect errors; auth errors fail fast.
        conn = with_retry(factory, retriable_exc=(OSError,))
        if access_token is None:
            try:
                conn.login(self._user, self._password)
            except imaplib.IMAP4.error as exc:
                raise ConfigurationError(
                    f"IMAP login to '{self._host}' as '{self._user}' failed: {exc}"
                ) from exc
        else:
            try:
                conn.authenticate("XOAUTH2", _xoauth2_authobject(self._user, access_token))
            except imaplib.IMAP4.error as exc:
                # The token was issued, so the app's identity is fine; what was
                # refused is this app's access to this mailbox.
                raise ConfigurationError(
                    f"Microsoft refused the app's sign-in to the mailbox "
                    f"'{self._user}': {exc}"
                ) from exc
        return conn
```

- [ ] **Step 4: Run the whole email tool suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py tests/test_email_tls_security.py tests/test_imap_summaries_for.py tests/test_email_scoped_tools.py -q`
Expected: all pass. The existing tests construct `_ImapBackend` with
`password=`, which still satisfies the exactly-one guard.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/tools/email_client.py tests/test_email_tools.py
git commit -m "feat(email): IMAP backend can authenticate with SASL XOAUTH2"
```

---

### Task 3: Credential storage for OAuth mailboxes

**Files:**
- Modify: `ui/backend/db/models.py` (`OrgEmailCredential`)
- Create: `alembic/versions/i6j7k8l9m0n1_add_oauth_email_credentials.py`
- Modify: `ui/backend/db/email_credentials.py`
- Test: `tests/test_email_credentials.py` (append), `tests/test_migrations.py` (append)

**Interfaces:**
- Produces: constants `AUTH_PASSWORD = "password"`, `AUTH_MICROSOFT_OAUTH = "microsoft_oauth"`, `MICROSOFT_IMAP_HOST = "outlook.office365.com"` in `ui/backend/db/email_credentials.py`; `set_email_credentials(..., auth_type=AUTH_PASSWORD, oauth_tenant_id=None, oauth_client_id=None)`; columns `OrgEmailCredential.auth_type/.oauth_tenant_id/.oauth_client_id`. Tasks 4, 5, 6 and 7 use these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_credentials.py`:

```python
def test_an_oauth_credential_round_trips_with_the_secret_encrypted(db_session, secrets_key):
    from ui.backend.db.email_credentials import AUTH_MICROSOFT_OAUTH, MICROSOFT_IMAP_HOST

    org = _make_org(db_session)
    row = set_email_credentials(
        db_session, org.id,
        host=MICROSOFT_IMAP_HOST, username="support@acme.com",
        password="the-client-secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )

    assert row.auth_type == AUTH_MICROSOFT_OAUTH
    assert row.oauth_tenant_id == "tenant-1"
    assert row.oauth_client_id == "client-1"
    # The client secret goes through exactly the same encrypted column the
    # mailbox password does -- one place a secret is written, one place the
    # boot-time key check has to look.
    assert "the-client-secret" not in row.password_encrypted
    assert secret_store.decrypt(row.password_encrypted) == "the-client-secret"


def test_credentials_default_to_password_auth(db_session, secrets_key):
    from ui.backend.db.email_credentials import AUTH_PASSWORD

    org = _make_org(db_session)
    row = set_email_credentials(
        db_session, org.id, host="imap.example.com", username="u", password="p"
    )
    assert row.auth_type == AUTH_PASSWORD
    assert row.oauth_tenant_id is None


def test_switching_an_oauth_mailbox_back_to_a_password_clears_the_oauth_fields(
    db_session, secrets_key
):
    from ui.backend.db.email_credentials import AUTH_MICROSOFT_OAUTH, AUTH_PASSWORD

    org = _make_org(db_session)
    set_email_credentials(
        db_session, org.id, host="outlook.office365.com", username="support@acme.com",
        password="secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )
    row = set_email_credentials(
        db_session, org.id, host="imap.example.com", username="u", password="p"
    )

    assert row.auth_type == AUTH_PASSWORD
    assert row.oauth_tenant_id is None
    assert row.oauth_client_id is None


def test_the_boot_key_check_still_catches_an_unreadable_oauth_credential(
    db_session, secrets_key, monkeypatch
):
    from ui.backend.db.email_credentials import AUTH_MICROSOFT_OAUTH

    org = _make_org(db_session)
    set_email_credentials(
        db_session, org.id, host="outlook.office365.com", username="support@acme.com",
        password="secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )
    monkeypatch.setattr(secret_store, "can_decrypt", lambda token: False)

    with pytest.raises(RuntimeError, match="cannot decrypt"):
        ensure_secrets_key_for_stored_credentials(db_session)
```

Read the top of `tests/test_email_credentials.py` first and reuse whatever
fixtures and org-creation helper it already defines — the names above
(`db_session`, `secrets_key`, `_make_org`) are placeholders for the file's own
conventions, and `Organization` has `name`/`display_name` and **no** `slug`.
Import `set_email_credentials`, `ensure_secrets_key_for_stored_credentials`
and `secret_store` if the file does not already.

Append to `tests/test_migrations.py`, following the file's existing pattern for
driving `command.upgrade`/`command.downgrade` against a throwaway on-disk
SQLite file:

```python
def test_the_oauth_credential_columns_upgrade_and_downgrade(tmp_path):
    """Existing password mailboxes must survive the upgrade as `password`."""
    db_path = tmp_path / "migrate.db"
    config = _alembic_config(db_path)

    command.upgrade(config, "h5i6j7k8l9m0")  # the revision before this one
    engine = make_engine(str(db_path))
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (name, display_name, active) "
            "VALUES ('acme', 'Acme', 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO org_email_credentials "
            "(org_id, backend, host, port, username, password_encrypted) "
            "VALUES (1, 'imap', 'imap.example.com', 993, 'u', 'tok')"
        ))

    command.upgrade(config, "i6j7k8l9m0n1")
    with engine.begin() as conn:
        row = conn.execute(sa.text(
            "SELECT auth_type, oauth_tenant_id, oauth_client_id "
            "FROM org_email_credentials WHERE org_id = 1"
        )).one()
    assert row.auth_type == "password"
    assert row.oauth_tenant_id is None

    command.downgrade(config, "h5i6j7k8l9m0")
    with engine.begin() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("org_email_credentials")}
    assert "auth_type" not in columns
    engine.dispose()
```

`_alembic_config` is a placeholder for however the file already builds its
`Config` — reuse that helper rather than adding a second one. Because this
test drives real Alembic, it belongs in the file that is already marked
`[pytest.mark.integration, pytest.mark.slow]`.

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_credentials.py -q`
Expected: FAIL — `cannot import name 'AUTH_MICROSOFT_OAUTH'`.

- [ ] **Step 3: Add the columns**

In `ui/backend/db/models.py`, in `OrgEmailCredential`, add after
`drafts_folder` and extend the class docstring's last line:

```python
    # How this mailbox authenticates: "password" (mailbox / app password) or
    # "microsoft_oauth" (Entra app-only client credentials, SASL XOAUTH2 over
    # IMAP -- Exchange Online no longer accepts basic auth).
    auth_type: Mapped[str] = mapped_column(default="password")
    # Entra identifiers. Not secrets -- the client *secret* is what goes in
    # `password_encrypted`. NULL for password auth.
    oauth_tenant_id: Mapped[Optional[str]] = mapped_column(nullable=True)
    oauth_client_id: Mapped[Optional[str]] = mapped_column(nullable=True)
```

Replace the docstring's `IMAP only for now (backend='imap'); Graph/OAuth are
future work.` with:

```
    Always IMAP (`backend='imap'`); `auth_type` selects how it authenticates.
    `password_encrypted` holds the mailbox password for `auth_type='password'`
    and the Entra **client secret** for `auth_type='microsoft_oauth'` -- one
    encrypted column either way, so there is exactly one place a secret is
    written and one place `ensure_secrets_key_for_stored_credentials` checks.
```

- [ ] **Step 4: Write the migration**

Create `alembic/versions/i6j7k8l9m0n1_add_oauth_email_credentials.py`:

```python
"""add OAuth columns to org_email_credentials (email automation Phase 2)

Revision ID: i6j7k8l9m0n1
Revises: h5i6j7k8l9m0
Create Date: 2026-08-17 15:00:00.000000

Exchange Online no longer accepts basic auth, so a mailbox now records how it
authenticates. Purely additive: every existing row is a password mailbox,
which is the server default, so there is no backfill.

Guarded ops (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has these columns
when this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'i6j7k8l9m0n1'
down_revision: Union[str, Sequence[str], None] = 'h5i6j7k8l9m0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "org_email_credentials"


def _columns(inspector) -> set:
    if _TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    existing = _columns(inspector)
    if not existing:
        return
    if "auth_type" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("auth_type", sa.String(), nullable=False, server_default="password"),
        )
    if "oauth_tenant_id" not in existing:
        op.add_column(_TABLE, sa.Column("oauth_tenant_id", sa.String(), nullable=True))
    if "oauth_client_id" not in existing:
        op.add_column(_TABLE, sa.Column("oauth_client_id", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    inspector = sa.inspect(op.get_bind())
    existing = _columns(inspector)
    for name in ("oauth_client_id", "oauth_tenant_id", "auth_type"):
        if name in existing:
            op.drop_column(_TABLE, name)
```

`ALTER TABLE ... DROP COLUMN` needs SQLite >= 3.35; this environment reports
3.45.3, so plain `op.drop_column` is fine and no batch-mode table rebuild is
needed.

- [ ] **Step 5: Extend the CRUD helper**

In `ui/backend/db/email_credentials.py`, add the constants below the imports:

```python
# How a stored mailbox authenticates.
AUTH_PASSWORD = "password"
AUTH_MICROSOFT_OAUTH = "microsoft_oauth"
# Exchange Online's IMAP endpoint. Fixed, never customer-supplied: the OAuth
# scope is bound to this host, so accepting another could only ever fail.
MICROSOFT_IMAP_HOST = "outlook.office365.com"
```

and extend `set_email_credentials`:

```python
def set_email_credentials(
    db: Session,
    org_id: int,
    *,
    host: str,
    username: str,
    password: str,
    port: int = 993,
    drafts_folder: Optional[str] = None,
    backend: str = "imap",
    auth_type: str = AUTH_PASSWORD,
    oauth_tenant_id: Optional[str] = None,
    oauth_client_id: Optional[str] = None,
) -> OrgEmailCredential:
    """Create or replace an org's mailbox credentials (upsert on `org_id`).

    `password` is plaintext in; it is encrypted before storage. For
    `auth_type=AUTH_MICROSOFT_OAUTH` it is the Entra **client secret** rather
    than a mailbox password -- the same encrypted column either way. Raises
    `secret_store.SecretsKeyError` if `BESTTEAM_SECRETS_KEY` is unset/invalid,
    or if it collides with the JWT signing key.

    Every field is assigned unconditionally, so switching a mailbox between
    auth types can't leave the previous type's fields behind.
    """
    secret_store.ensure_key_separation()
    token = secret_store.encrypt(password)
    row = get_email_credentials(db, org_id)
    if row is None:
        row = OrgEmailCredential(org_id=org_id)
        db.add(row)
    row.backend = backend
    row.host = host
    row.port = port
    row.username = username
    row.password_encrypted = token
    row.drafts_folder = drafts_folder
    row.auth_type = auth_type
    row.oauth_tenant_id = oauth_tenant_id
    row.oauth_client_id = oauth_client_id
    db.commit()
    db.refresh(row)
    return row
```

- [ ] **Step 6: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_credentials.py tests/test_migrations.py tests/test_db.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add ui/backend/db/models.py ui/backend/db/email_credentials.py alembic/versions/i6j7k8l9m0n1_add_oauth_email_credentials.py tests/test_email_credentials.py tests/test_migrations.py
git commit -m "feat(email): store how a mailbox authenticates, plus its Entra identifiers"
```

---

### Task 4: Build the right backend for a stored mailbox

**Files:**
- Modify: `ui/backend/email_tools.py` (`build_org_imap_backend`)
- Test: `tests/test_load_email_tools.py` (append)

**Interfaces:**
- Consumes: `MicrosoftClientCredentialsToken` (Task 1), `_ImapBackend(..., token_provider=)` (Task 2), `AUTH_MICROSOFT_OAUTH` (Task 3).
- Produces: `build_org_imap_backend(db, org_id)` returning an `_ImapBackend` wired for whichever auth type is stored. `load_email_tools` is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_load_email_tools.py`, matching its existing fixtures:

```python
def test_an_oauth_mailbox_builds_a_backend_with_a_token_provider(db_session, secrets_key):
    from ui.backend.db.email_credentials import AUTH_MICROSOFT_OAUTH, MICROSOFT_IMAP_HOST
    from bestteam.tools._oauth import MicrosoftClientCredentialsToken

    org = _make_org(db_session)
    set_email_credentials(
        db_session, org.id, host=MICROSOFT_IMAP_HOST, username="support@acme.com",
        password="the-client-secret", auth_type=AUTH_MICROSOFT_OAUTH,
        oauth_tenant_id="tenant-1", oauth_client_id="client-1",
    )

    backend = build_org_imap_backend(db_session, org.id)

    assert backend._password is None
    assert isinstance(backend._token_provider, MicrosoftClientCredentialsToken)
    # The provider must carry the *decrypted* secret, and address the right
    # tenant -- getting either wrong fails only at first use, in production.
    assert backend._token_provider.url.endswith("/tenant-1/oauth2/v2.0/token")
    assert backend._token_provider._client_id == "client-1"
    assert backend._token_provider._client_secret == "the-client-secret"
    assert backend._restrict_to_public is True


def test_a_password_mailbox_still_builds_a_password_backend(db_session, secrets_key):
    org = _make_org(db_session)
    set_email_credentials(
        db_session, org.id, host="imap.example.com", username="u", password="p"
    )

    backend = build_org_imap_backend(db_session, org.id)

    assert backend._password == "p"
    assert backend._token_provider is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_load_email_tools.py -q`
Expected: FAIL — `build_org_imap_backend` passes the decrypted client secret
as `password=`, so `_token_provider` is `None`.

- [ ] **Step 3: Implement the dispatch**

In `ui/backend/email_tools.py`, add to the imports:

```python
from bestteam.tools._oauth import MicrosoftClientCredentialsToken
from .db.email_credentials import AUTH_MICROSOFT_OAUTH, get_email_credentials
```

(replacing the existing `from .db.email_credentials import get_email_credentials`)

and replace `build_org_imap_backend`:

```python
def build_org_imap_backend(db: Session, org_id: int):
    """The org's IMAP backend from stored credentials, or None if unconnected.

    Decrypts the stored secret; raises secret_store.SecretsKeyError /
    InvalidToken on a bad/rotated key (the caller decides how to surface that).
    `auth_type` chooses how the connection authenticates -- Exchange Online
    mailboxes use an app-only OAuth token because basic auth is gone there.
    """
    cred = get_email_credentials(db, org_id)
    if cred is None:
        return None
    secret = secret_store.decrypt(cred.password_encrypted)
    if cred.auth_type == AUTH_MICROSOFT_OAUTH:
        return _ImapBackend(
            host=cred.host,
            user=cred.username,
            port=cred.port,
            drafts=cred.drafts_folder,
            restrict_to_public=True,
            token_provider=MicrosoftClientCredentialsToken(
                tenant_id=cred.oauth_tenant_id or "",
                client_id=cred.oauth_client_id or "",
                client_secret=secret,
            ),
        )
    return _ImapBackend(
        host=cred.host,
        user=cred.username,
        password=secret,
        port=cred.port,
        drafts=cred.drafts_folder,
        restrict_to_public=True,  # customer-supplied host: validate + pin on connect
    )
```

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_load_email_tools.py tests/test_email_trigger.py -q`
Expected: all pass. `email_trigger.py` is untouched — it calls
`build_org_imap_backend` and then speaks ordinary IMAP to whatever session
`_connect()` returns, which is exactly what an XOAUTH2 session is.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_tools.py tests/test_load_email_tools.py
git commit -m "feat(email): resolve an org's mailbox through its stored auth type"
```

---

### Task 5: Connect / test a Microsoft 365 mailbox through the API

**Files:**
- Modify: `ui/backend/org_settings.py`
- Test: `tests/test_org_settings.py` (append)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `PUT`/`POST /api/org/email` accepting `auth_type`, `client_secret`, `oauth_tenant_id`, `oauth_client_id`; `GET /api/org/email` returning `auth_type`, `oauth_tenant_id`, `oauth_client_id`. Task 7 calls these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_org_settings.py`, reusing its existing authenticated
client fixture:

```python
_OAUTH_BODY = {
    "auth_type": "microsoft_oauth",
    "username": "support@acme.com",
    "client_secret": "shh",
    "oauth_tenant_id": "tenant-1",
    "oauth_client_id": "client-1",
}


def test_connecting_a_microsoft_mailbox_stores_the_oauth_identifiers(client, db_session):
    with patch("ui.backend.org_settings._mailbox_problem", return_value=None):
        response = client.put("/api/org/email", json=_OAUTH_BODY)
    assert response.status_code == 200

    status = client.get("/api/org/email").json()
    assert status["auth_type"] == "microsoft_oauth"
    assert status["oauth_tenant_id"] == "tenant-1"
    assert status["oauth_client_id"] == "client-1"
    # Fixed server-side: the token scope is bound to this host.
    assert status["host"] == "outlook.office365.com"
    # The secret is never echoed back, in any field.
    assert "shh" not in response.text
    assert "shh" not in client.get("/api/org/email").text


def test_a_client_supplied_host_is_discarded_for_a_microsoft_mailbox(client):
    with patch("ui.backend.org_settings._mailbox_problem", return_value=None):
        client.put("/api/org/email", json={**_OAUTH_BODY, "host": "evil.example.com"})
    assert client.get("/api/org/email").json()["host"] == "outlook.office365.com"


@pytest.mark.parametrize("missing", ["client_secret", "oauth_tenant_id", "oauth_client_id"])
def test_a_microsoft_mailbox_needs_every_oauth_field(client, missing):
    body = {k: v for k, v in _OAUTH_BODY.items() if k != missing}
    assert client.put("/api/org/email", json=body).status_code == 422


def test_a_microsoft_mailbox_rejects_a_password(client):
    assert client.put(
        "/api/org/email", json={**_OAUTH_BODY, "password": "p"}
    ).status_code == 422


def test_a_password_mailbox_rejects_stray_oauth_fields(client):
    assert client.put("/api/org/email", json={
        "host": "imap.example.com", "username": "u", "password": "p",
        "oauth_tenant_id": "tenant-1",
    }).status_code == 422


def test_the_existing_password_body_still_works_unchanged(client):
    """An older client posts no auth_type at all; it must keep working."""
    with patch("ui.backend.org_settings._mailbox_problem", return_value=None):
        response = client.put("/api/org/email", json={
            "host": "imap.example.com", "username": "u", "password": "p", "port": 993,
            "drafts": None,
        })
    assert response.status_code == 200
    assert client.get("/api/org/email").json()["auth_type"] == "password"


def test_a_bad_client_secret_is_reported_as_an_application_problem(client):
    """A token failure and a mailbox-access failure have completely different
    fixes, and by Microsoft's message alone they are not tellable apart -- so
    they are told apart by which step failed."""
    from bestteam.exceptions import ConfigurationError

    with patch("ui.backend.org_settings.MicrosoftClientCredentialsToken") as provider:
        provider.return_value.token.side_effect = ConfigurationError(
            "Microsoft rejected the application's sign-in (401): "
            "AADSTS7000215: Invalid client secret provided."
        )
        response = client.post("/api/org/email/test", json=_OAUTH_BODY)

    body = response.json()
    assert body["ok"] is False
    assert "client secret" in body["error"].lower()
    assert "app password" not in body["error"].lower(), "password advice is wrong here"


def test_an_unknown_tenant_is_reported_as_a_tenant_problem(client):
    from bestteam.exceptions import ConfigurationError

    with patch("ui.backend.org_settings.MicrosoftClientCredentialsToken") as provider:
        provider.return_value.token.side_effect = ConfigurationError(
            "Microsoft rejected the application's sign-in (400): "
            "AADSTS90002: Tenant 'nope' not found."
        )
        response = client.post("/api/org/email/test", json=_OAUTH_BODY)

    assert "Directory (tenant) ID" in response.json()["error"]


def test_a_working_token_with_a_refused_mailbox_names_the_exchange_setup(client):
    """The most likely outcome of a half-finished Azure setup, and the one that
    is useless without a specific message."""
    from bestteam.exceptions import ConfigurationError

    with patch("ui.backend.org_settings.MicrosoftClientCredentialsToken") as provider, \
         patch.object(_ImapBackend, "_connect",
                      side_effect=ConfigurationError("Microsoft refused the app's sign-in")):
        provider.return_value.token.return_value = "tok-1"
        response = client.post("/api/org/email/test", json=_OAUTH_BODY)

    error = response.json()["error"]
    assert "Add-MailboxPermission" in error
    assert "IMAP.AccessAsApp" in error
    assert "support@acme.com" in error
```

Add `from unittest.mock import patch` and
`from bestteam.tools.email_client import _ImapBackend` to the test file's
imports if they are not already there.

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_org_settings.py -q`
Expected: FAIL — `auth_type` is an unknown field, so the OAuth bodies 422 on
the missing `host`/`password`.

- [ ] **Step 3: Implement the request model and validation**

In `ui/backend/org_settings.py`, extend the imports:

```python
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from bestteam.tools._oauth import MicrosoftClientCredentialsToken
from .db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    AUTH_PASSWORD,
    MICROSOFT_IMAP_HOST,
    clear_email_credentials,
    get_email_credentials,
    set_email_credentials,
)
```

Replace `EmailConnectRequest`:

```python
class EmailConnectRequest(BaseModel):
    """A mailbox connection attempt, for either auth type.

    One model rather than two endpoints, so "validate the real thing before
    storing it" stays in one place. `auth_type` defaults to `password`, so a
    client that posts the pre-Phase-2 body keeps working unchanged.
    """

    auth_type: Literal["password", "microsoft_oauth"] = AUTH_PASSWORD
    host: str = ""
    username: str
    password: Optional[str] = None
    client_secret: Optional[str] = None
    oauth_tenant_id: Optional[str] = None
    oauth_client_id: Optional[str] = None
    port: int = Field(default=993, ge=1, le=65535)
    drafts: Optional[str] = None

    @model_validator(mode="after")
    def _check_credentials_match_the_auth_type(self) -> "EmailConnectRequest":
        if self.auth_type == AUTH_MICROSOFT_OAUTH:
            missing = [
                label
                for label, value in (
                    ("Directory (tenant) ID", self.oauth_tenant_id),
                    ("Application (client) ID", self.oauth_client_id),
                    ("client secret", self.client_secret),
                )
                if not (value or "").strip()
            ]
            if missing:
                raise ValueError(
                    "A Microsoft 365 mailbox needs the " + ", ".join(missing) + "."
                )
            if self.password:
                raise ValueError(
                    "A Microsoft 365 mailbox signs in with a client secret, not a password."
                )
            # Set here, never taken from the client: the OAuth scope is bound to
            # Exchange Online's endpoint, so any other host could only fail.
            self.host = MICROSOFT_IMAP_HOST
        else:
            if not (self.password or "").strip():
                raise ValueError("A password is required.")
            if not self.host.strip():
                raise ValueError("A mail server address is required.")
            if self.client_secret or self.oauth_tenant_id or self.oauth_client_id:
                raise ValueError(
                    "Microsoft 365 details only apply when connecting a Microsoft 365 mailbox."
                )
        return self

    @property
    def secret(self) -> str:
        """The credential material to encrypt, whichever auth type this is."""
        source = (
            self.client_secret if self.auth_type == AUTH_MICROSOFT_OAUTH else self.password
        )
        return source or ""
```

Add the backend builder and the OAuth error mapping above `_mailbox_problem`:

```python
_M365_ACCESS_HELP = (
    "Microsoft accepted the app's sign-in but refused it access to '{mailbox}'. "
    "Ask your IT administrator to grant admin consent for the IMAP.AccessAsApp "
    "permission, then register the app against this mailbox in Exchange Online "
    "(New-ServicePrincipal, then Add-MailboxPermission). See docs/deployment.md."
)


def _backend_for(req: "EmailConnectRequest") -> _ImapBackend:
    """The same backend `email_tools.build_org_imap_backend` will build later.

    Deliberately kept in step with it: validating a differently-built backend
    would let a mailbox pass the wizard and then fail on its first real run.
    """
    if req.auth_type == AUTH_MICROSOFT_OAUTH:
        return _ImapBackend(
            host=req.host, user=req.username, port=req.port, drafts=req.drafts,
            restrict_to_public=True,
            token_provider=MicrosoftClientCredentialsToken(
                tenant_id=(req.oauth_tenant_id or "").strip(),
                client_id=(req.oauth_client_id or "").strip(),
                client_secret=req.client_secret or "",
            ),
        )
    return _ImapBackend(
        host=req.host, user=req.username, password=req.password or "", port=req.port,
        drafts=req.drafts, restrict_to_public=True,
    )


def _friendly_oauth_credential_error(exc: Exception) -> str:
    """A token-fetch failure, in language the customer can act on.

    Only two things can be wrong at this stage -- the tenant, or the
    application's own identity/secret -- so the mapping stays coarse on purpose
    rather than tracking Microsoft's whole AADSTS catalogue. Microsoft's own
    sentence is kept on the end, because a support conversation needs it.
    """
    text = str(exc)
    lowered = text.lower()
    if "could not be reached" in lowered:
        return text
    if "aadsts90002" in lowered or "tenant" in lowered:
        return (
            "Microsoft didn't recognise that Directory (tenant) ID. Copy it from the "
            f"app registration's Overview page in the Azure portal. ({text})"
        )
    return (
        "Microsoft didn't accept the application's sign-in. Check the Application "
        "(client) ID, and that the client secret is correct and hasn't expired. "
        f"({text})"
    )
```

Replace `_mailbox_problem`'s body (keep its docstring and add the second
paragraph):

```python
def _mailbox_problem(req: "EmailConnectRequest") -> Optional[str]:
    """`None` if the mailbox is genuinely usable, else a customer-facing reason.

    Checks BOTH halves of what the toolkit needs, because a successful login
    alone was never enough: every reply this platform produces is an APPEND to
    the drafts folder, so a mailbox whose drafts folder doesn't exist under the
    configured name (or isn't writable by this account) passes a login test and
    then fails on the very first real draft, long after the customer has left
    the wizard (Phase 0, item 0.7). Nothing is written to the mailbox -- a
    SELECT that succeeds without reporting READ-ONLY is enough.

    For a Microsoft 365 mailbox the token is fetched on its own first, so that
    a credential problem (wrong client ID, expired secret, unknown tenant) is
    distinguishable from a mailbox-access problem (consent or
    Add-MailboxPermission missing). They have completely different fixes and
    are not tellable apart from the error text alone.
    """
    backend = _backend_for(req)
    if req.auth_type == AUTH_MICROSOFT_OAUTH:
        try:
            backend._token_provider.token()
        except ConfigurationError as exc:
            return _friendly_oauth_credential_error(exc)
    try:
        conn = backend._connect()
        conn.logout()
    except OSError as exc:
        return _friendly_connect_error(exc, req.host, req.port)
    except ConfigurationError as exc:
        if req.auth_type == AUTH_MICROSOFT_OAUTH:
            return _M365_ACCESS_HELP.format(mailbox=req.username)
        return _friendly_connect_error(exc, req.host, req.port)
    try:
        backend.check_drafts_writable()
    except ConfigurationError as exc:
        # Already written for a human by check_drafts_writable (it names the
        # folder it actually resolved), so it passes through as-is.
        return str(exc)
    except OSError as exc:
        return _friendly_connect_error(exc, req.host, req.port)
    return None
```

Splitting `except (ConfigurationError, OSError)` into two clauses keeps the
password path's behaviour identical (`socket.gaierror` and `TimeoutError` are
both `OSError`s) while letting the OAuth path say something specific.

In `set_email`, pass the new fields through:

```python
        set_email_credentials(
            db, org.id, host=req.host, username=req.username, password=req.secret,
            port=req.port, drafts_folder=req.drafts, auth_type=req.auth_type,
            oauth_tenant_id=(req.oauth_tenant_id or "").strip() or None,
            oauth_client_id=(req.oauth_client_id or "").strip() or None,
        )
```

In `get_email`, extend the connected payload:

```python
    return {
        "connected": True,
        "host": cred.host,
        "username": cred.username,
        "port": cred.port,
        "drafts": cred.drafts_folder,
        "auth_type": cred.auth_type,
        # Identifiers, not secrets -- the client secret lives only in
        # `password_encrypted` and is never returned.
        "oauth_tenant_id": cred.oauth_tenant_id,
        "oauth_client_id": cred.oauth_client_id,
    }
```

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_org_settings.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/org_settings.py tests/test_org_settings.py
git commit -m "feat(email): connect a Microsoft 365 mailbox from the org settings API"
```

---

### Task 6: `admin set-email --auth microsoft-oauth`

**Files:**
- Modify: `ui/backend/admin.py`
- Test: `tests/test_admin_cli.py` (append)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin_cli.py`, matching its fixtures and the way it
already drives `main(argv)`:

```python
def test_set_email_stores_a_microsoft_oauth_mailbox(db_session, secrets_key, monkeypatch):
    from ui.backend.db.email_credentials import AUTH_MICROSOFT_OAUTH, MICROSOFT_IMAP_HOST

    _make_org(db_session, "acme")
    monkeypatch.setattr("ui.backend.admin.getpass.getpass", lambda *a: "the-secret")

    assert admin.main([
        "set-email", "acme", "--auth", "microsoft-oauth",
        "--user", "support@acme.com", "--tenant", "tenant-1", "--client-id", "client-1",
    ]) == 0

    cred = get_email_credentials(db_session, _org_id(db_session, "acme"))
    assert cred.auth_type == AUTH_MICROSOFT_OAUTH
    assert cred.host == MICROSOFT_IMAP_HOST
    assert cred.oauth_tenant_id == "tenant-1"
    assert cred.oauth_client_id == "client-1"
    assert secret_store.decrypt(cred.password_encrypted) == "the-secret"


def test_set_email_microsoft_oauth_requires_the_entra_identifiers(db_session, capsys):
    _make_org(db_session, "acme")
    with pytest.raises(SystemExit):
        admin.main(["set-email", "acme", "--auth", "microsoft-oauth",
                    "--user", "support@acme.com"])
    assert "--tenant" in capsys.readouterr().err


def test_set_email_password_auth_still_requires_a_host(db_session, capsys):
    _make_org(db_session, "acme")
    with pytest.raises(SystemExit):
        admin.main(["set-email", "acme", "--user", "u"])
    assert "--host" in capsys.readouterr().err
```

`_make_org` / `_org_id` are placeholders for whatever the file already uses.

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_admin_cli.py -q`
Expected: FAIL — `--auth` is an unrecognised argument, and `--host` is
currently `required=True`.

- [ ] **Step 3: Implement the CLI**

In `ui/backend/admin.py`, make `_prompt_password` say what it is asking for:

```python
def _prompt_password(parser: argparse.ArgumentParser, label: str = "Password") -> str:
    password = getpass.getpass(f"{label}: ")
    if not password:
        ...  # unchanged
    if getpass.getpass(f"Repeat {label.lower()}: ") != password:
        ...  # unchanged
    return password
```

Replace the `set-email` parser block:

```python
    set_email_p = sub.add_parser(
        "set-email",
        help="connect an org's mailbox (prompts for the password or client secret)",
    )
    set_email_p.add_argument("org", help="organization name")
    set_email_p.add_argument(
        "--auth", choices=["password", "microsoft-oauth"], default="password",
        help="password: an app password. microsoft-oauth: Entra app-only "
             "credentials for Exchange Online, which no longer accepts basic auth.",
    )
    set_email_p.add_argument("--host", default=None,
                             help="IMAP host, e.g. imap.gmail.com (fixed for microsoft-oauth)")
    set_email_p.add_argument("--user", required=True, help="IMAP username / email address")
    set_email_p.add_argument("--tenant", default=None,
                             help="Entra Directory (tenant) ID (microsoft-oauth only)")
    set_email_p.add_argument("--client-id", dest="client_id", default=None,
                             help="Entra Application (client) ID (microsoft-oauth only)")
    set_email_p.add_argument("--port", type=int, default=993)
    set_email_p.add_argument("--drafts", default=None, help="Drafts folder name (auto-detected if omitted)")
    set_email_p.add_argument(
        "--test", action="store_true", help="verify the credentials with a login before saving"
    )
```

Replace the `set-email` handler block:

```python
        if args.command == "set-email":
            org = get_org_by_name(db, args.org)
            if org is None:
                parser.error(f"Unknown organization '{args.org}'. Create it first with create-org.")
            oauth = args.auth == "microsoft-oauth"
            if oauth:
                if not args.tenant or not args.client_id:
                    parser.error("--auth microsoft-oauth needs --tenant and --client-id.")
                # Exchange Online's endpoint; the OAuth scope is bound to it.
                host = args.host or MICROSOFT_IMAP_HOST
            else:
                if not args.host:
                    parser.error("--host is required with --auth password.")
                if args.tenant or args.client_id:
                    parser.error("--tenant/--client-id only apply with --auth microsoft-oauth.")
                host = args.host
            secret = _prompt_password(parser, "Client secret" if oauth else "Password")
            if args.test:
                # Build the same backend the tools use and attempt a login, so a
                # bad credential is caught here rather than at first run.
                from bestteam.exceptions import ConfigurationError
                from bestteam.tools._oauth import MicrosoftClientCredentialsToken
                from bestteam.tools.email_client import _ImapBackend

                if oauth:
                    backend = _ImapBackend(
                        host=host, user=args.user, port=args.port, drafts=args.drafts,
                        restrict_to_public=True,
                        token_provider=MicrosoftClientCredentialsToken(
                            tenant_id=args.tenant, client_id=args.client_id,
                            client_secret=secret,
                        ),
                    )
                else:
                    backend = _ImapBackend(
                        host=host, user=args.user, password=secret,
                        port=args.port, drafts=args.drafts, restrict_to_public=True,
                    )
                try:
                    conn = backend._connect()
                    conn.logout()
                except ConfigurationError as exc:
                    parser.error(f"Login test failed, not saved: {exc}")
                except OSError as exc:
                    parser.error(f"Could not reach '{host}:{args.port}', not saved: {exc}")
            prior = get_email_credentials(db, org.id)
            prior_identity = (prior.host, prior.username) if prior is not None else None
            try:
                set_email_credentials(
                    db, org.id, host=host, username=args.user, password=secret,
                    port=args.port, drafts_folder=args.drafts,
                    auth_type=AUTH_MICROSOFT_OAUTH if oauth else AUTH_PASSWORD,
                    oauth_tenant_id=args.tenant if oauth else None,
                    oauth_client_id=args.client_id if oauth else None,
                )
            except Exception as exc:  # noqa: BLE001 -- surface a clear CLI error (e.g. missing key)
                parser.error(str(exc))
            email_trigger.disable_trigger_on_identity_change(
                db, org.id, host, args.user, prior_identity
            )
            print(f"Connected mailbox '{args.user}' for organization '{args.org}'.")
            return 0
```

Extend the existing import so the constants are in scope:

```python
from .db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    AUTH_PASSWORD,
    MICROSOFT_IMAP_HOST,
    clear_email_credentials,
    get_email_credentials,
    set_email_credentials,
)
```

Also update the module docstring's example at line 16 to keep it accurate; it
already shows a password connection, which is still valid, so add one line
beneath it:

```
    docker compose exec backend python -m ui.backend.admin set-email acme \
        --auth microsoft-oauth --user support@acme.com \
        --tenant <directory-id> --client-id <application-id>
```

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_admin_cli.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/admin.py tests/test_admin_cli.py
git commit -m "feat(email): admin set-email can connect a Microsoft 365 mailbox"
```

---

### Task 7: The wizard offers a Microsoft 365 mailbox

**Files:**
- Modify: `ui/frontend/src/lib/types.ts`, `ui/frontend/src/lib/api.ts`, `ui/frontend/src/components/EmailConnect.tsx`
- Test: `ui/frontend/src/components/EmailConnect.test.tsx` (new)

**Interfaces:**
- Consumes: the API from Task 5.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing tests**

Create `ui/frontend/src/components/EmailConnect.test.tsx`:

```tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import EmailConnect from './EmailConnect'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getOrgEmail: vi.fn(),
    setOrgEmail: vi.fn(),
    testOrgEmail: vi.fn(),
    clearOrgEmail: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const chooseMicrosoft = () =>
  fireEvent.click(screen.getByLabelText(/microsoft 365/i))

describe('EmailConnect', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getOrgEmail.mockResolvedValue({ connected: false })
    mockedApi.setOrgEmail.mockResolvedValue({ connected: true })
  })

  it('defaults to the standard IMAP form', async () => {
    render(<EmailConnect />)
    expect(await screen.findByLabelText(/imap server/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/directory \(tenant\) id/i)).not.toBeInTheDocument()
  })

  it('swaps in the Microsoft 365 fields and hides the server address', async () => {
    render(<EmailConnect />)
    await screen.findByLabelText(/imap server/i)
    chooseMicrosoft()

    expect(screen.getByLabelText(/directory \(tenant\) id/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/application \(client\) id/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/client secret/i)).toBeInTheDocument()
    // The server address is fixed for Exchange Online, so asking for it would
    // only invite a wrong answer.
    expect(screen.queryByLabelText(/imap server/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/^app password/i)).not.toBeInTheDocument()
  })

  it('posts a Microsoft 365 body with no password field', async () => {
    render(<EmailConnect />)
    await screen.findByLabelText(/imap server/i)
    chooseMicrosoft()

    fireEvent.change(screen.getByLabelText(/email address/i), {
      target: { value: 'support@acme.com' },
    })
    fireEvent.change(screen.getByLabelText(/directory \(tenant\) id/i), {
      target: { value: 'tenant-1' },
    })
    fireEvent.change(screen.getByLabelText(/application \(client\) id/i), {
      target: { value: 'client-1' },
    })
    fireEvent.change(screen.getByLabelText(/client secret/i), {
      target: { value: 'shh' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save|connect/i }))

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(mockedApi.setOrgEmail.mock.calls[0][0]).toMatchObject({
      auth_type: 'microsoft_oauth',
      username: 'support@acme.com',
      oauth_tenant_id: 'tenant-1',
      oauth_client_id: 'client-1',
      client_secret: 'shh',
      password: null,
    })
  })

  it('pre-fills the Entra identifiers on reconnect but never a secret', async () => {
    mockedApi.getOrgEmail.mockResolvedValue({
      connected: true,
      host: 'outlook.office365.com',
      username: 'support@acme.com',
      port: 993,
      auth_type: 'microsoft_oauth',
      oauth_tenant_id: 'tenant-1',
      oauth_client_id: 'client-1',
    })
    render(<EmailConnect />)

    fireEvent.click(await screen.findByRole('button', { name: /reconnect/i }))

    expect(screen.getByLabelText(/directory \(tenant\) id/i)).toHaveValue('tenant-1')
    expect(screen.getByLabelText(/application \(client\) id/i)).toHaveValue('client-1')
    expect(screen.getByLabelText(/client secret/i)).toHaveValue('')
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ui/frontend && npm test -- EmailConnect`
Expected: FAIL — there is no Microsoft 365 radio to click.

- [ ] **Step 3: Extend the types and API client**

In `ui/frontend/src/lib/types.ts`, replace `OrgEmailStatus` and add the
payload type:

```ts
export type OrgEmailAuthType = 'password' | 'microsoft_oauth'

export interface OrgEmailStatus {
  connected: boolean
  host?: string
  username?: string
  port?: number
  drafts?: string | null
  auth_type?: OrgEmailAuthType
  oauth_tenant_id?: string | null
  oauth_client_id?: string | null
}

// The mailbox connect/test body. `password` and the three OAuth fields are
// mutually exclusive; the backend rejects a mix.
export interface OrgEmailConnectPayload {
  auth_type: OrgEmailAuthType
  host: string
  username: string
  password: string | null
  client_secret: string | null
  oauth_tenant_id: string | null
  oauth_client_id: string | null
  port: number
  drafts: string | null
}
```

In `ui/frontend/src/lib/api.ts`, add `OrgEmailConnectPayload` to the type
import and change the two signatures:

```ts
  setOrgEmail: (payload: OrgEmailConnectPayload) =>
    request<OrgEmailStatus>('/api/org/email', { method: 'PUT', body: JSON.stringify(payload) }),
  testOrgEmail: (payload: OrgEmailConnectPayload) =>
    request<{ ok: boolean; error?: string }>('/api/org/email/test', { method: 'POST', body: JSON.stringify(payload) }),
```

Keep whatever return type `testOrgEmail` already declares if it differs.

- [ ] **Step 4: Implement the component changes**

In `ui/frontend/src/components/EmailConnect.tsx`:

Extend the form state:

```tsx
interface EmailForm {
  authType: OrgEmailAuthType
  host: string
  username: string
  password: string
  tenantId: string
  clientId: string
  clientSecret: string
  port: number | string
  drafts: string
}

const EMPTY_FORM: EmailForm = {
  authType: 'password', host: '', username: '', password: '',
  tenantId: '', clientId: '', clientSecret: '', port: 993, drafts: '',
}
```

Replace both `setForm({ host: '', ... })` literals with `setForm(EMPTY_FORM)`
and the `useState` initialiser with `useState<EmailForm>(EMPTY_FORM)`.

Replace `payload`:

```tsx
  const isM365 = form.authType === 'microsoft_oauth'

  const payload = (): OrgEmailConnectPayload => ({
    auth_type: form.authType,
    // Fixed server-side for Microsoft 365; sending '' says so honestly.
    host: isM365 ? '' : form.host.trim(),
    username: form.username.trim(),
    password: isM365 ? null : form.password,
    client_secret: isM365 ? form.clientSecret : null,
    oauth_tenant_id: isM365 ? form.tenantId.trim() : null,
    oauth_client_id: isM365 ? form.clientId.trim() : null,
    port: Number(form.port) || 993,
    drafts: form.drafts.trim() || null,
  })
```

Replace `canSubmit`:

```tsx
  const canSubmit = isM365
    ? form.username.trim() && form.tenantId.trim() && form.clientId.trim() && form.clientSecret
    : form.host.trim() && form.username.trim() && form.password
```

Replace `startReconnect`:

```tsx
  const startReconnect = () => {
    if (!status) return
    setForm({
      ...EMPTY_FORM,
      authType: status.auth_type || 'password',
      host: status.host || '',
      username: status.username || '',
      tenantId: status.oauth_tenant_id || '',
      clientId: status.oauth_client_id || '',
      port: status.port || 993,
      drafts: status.drafts || '',
    })
    setShowAdvanced(Boolean((status.port && status.port !== 993) || status.drafts))
    setEditing(true)
  }
```

Insert the provider chooser as the first thing inside the editing branch,
before the existing `IMAP server` field, and make the credential fields
conditional:

```tsx
          <fieldset className="field">
            <legend>How is this mailbox hosted?</legend>
            <label htmlFor="ec-auth-password">
              <input
                id="ec-auth-password" type="radio" name="ec-auth" value="password"
                checked={!isM365}
                onChange={() => setForm({ ...form, authType: 'password' })}
              />{' '}
              Standard mailbox (IMAP) — Gmail, and most providers
            </label>
            <label htmlFor="ec-auth-m365">
              <input
                id="ec-auth-m365" type="radio" name="ec-auth" value="microsoft_oauth"
                checked={isM365}
                onChange={() => setForm({ ...form, authType: 'microsoft_oauth' })}
              />{' '}
              Microsoft 365 / Outlook (Exchange Online)
            </label>
          </fieldset>

          {isM365 ? (
            <>
              <p className="hint">
                Microsoft 365 no longer allows app passwords, so this connects through
                an app registration instead. Ask your IT administrator to register an
                app in Azure, grant it the <strong>IMAP.AccessAsApp</strong> permission
                with admin consent, and give it access to this mailbox in Exchange
                Online. They will then have the three values below.
              </p>
              <div className="field">
                <label htmlFor="ec-user">Email address</label>
                <input id="ec-user" type="text" value={form.username}
                       onChange={field('username')} placeholder="support@yourcompany.com" />
              </div>
              <div className="field">
                <label htmlFor="ec-tenant">Directory (tenant) ID</label>
                <input id="ec-tenant" type="text" value={form.tenantId} onChange={field('tenantId')} />
              </div>
              <div className="field">
                <label htmlFor="ec-client">Application (client) ID</label>
                <input id="ec-client" type="text" value={form.clientId} onChange={field('clientId')} />
              </div>
              <div className="field">
                <label htmlFor="ec-secret">Client secret</label>
                <input id="ec-secret" type="password" value={form.clientSecret}
                       onChange={field('clientSecret')} autoComplete="off" />
              </div>
            </>
          ) : (
            <>
              {/* the existing IMAP server / email address / app password fields, unchanged */}
            </>
          )}
```

The `field` helper already accepts any `keyof EmailForm`, so it covers the new
keys without change. Inside the Advanced block, render the port input only
when `!isM365` — Exchange Online is always 993 and the drafts folder override
is still useful for both.

Import `OrgEmailAuthType` and `OrgEmailConnectPayload` alongside the existing
`OrgEmailStatus` type import.

- [ ] **Step 5: Run the frontend tests and the type check**

Run: `cd ui/frontend && npm test -- EmailConnect && npm run build`
Expected: the new tests pass and the TypeScript build succeeds.

- [ ] **Step 6: Run the whole frontend suite**

Run: `cd ui/frontend && npm test`
Expected: no regressions — the wizard tests that render `EmailConnect`
indirectly still pass, since the default auth type reproduces today's form.

- [ ] **Step 7: Commit**

```bash
git add ui/frontend/src/lib/types.ts ui/frontend/src/lib/api.ts ui/frontend/src/components/EmailConnect.tsx ui/frontend/src/components/EmailConnect.test.tsx
git commit -m "feat(ui): connect a Microsoft 365 mailbox from the wizard"
```

---

### Task 8: Documentation and status

**Files:**
- Modify: `src/bestteam/tools/CLAUDE.md`, `ui/backend/CLAUDE.md`, `ui/backend/db/CLAUDE.md`, `docs/deployment.md`, `docs/email-smoke-test.md`, `docs/STATUS.md`, `docs/DECISIONS.md`
- Test: none (documentation)

**Interfaces:** none.

- [ ] **Step 1: `src/bestteam/tools/CLAUDE.md`**

In the "Per-mailbox seam" paragraph, record the auth strategy: `_ImapBackend`
takes exactly one of `password=` or `token_provider=`; the token provider is
`bestteam.tools._oauth.MicrosoftClientCredentialsToken` (stdlib `urllib`, no
httpx — the httpx note in `pyproject.toml` still holds); `_connect()`
authenticates with SASL `XOAUTH2` when a provider is present, and everything
above `_connect()` is unchanged because an authenticated IMAP session is an
authenticated IMAP session. Note that the token is cached until 60 seconds
before it expires, which the poller depends on.

- [ ] **Step 2: `ui/backend/CLAUDE.md`**

Add a short "Microsoft 365 mailboxes" block: `auth_type` on the credential row
selects how `build_org_imap_backend` authenticates; the host is fixed
server-side to `outlook.office365.com`; the connect API validates the token
and the mailbox access as two separate steps because their fixes differ; the
poller, the trigger and the event ledger are unchanged.

- [ ] **Step 3: `ui/backend/db/CLAUDE.md`**

Document the three new `org_email_credentials` columns and — the load-bearing
part — that `password_encrypted` holds the Entra **client secret** when
`auth_type = 'microsoft_oauth'`, so there stays exactly one encrypted column
and one boot-time key check.

- [ ] **Step 4: `docs/deployment.md`**

Add a subsection under the existing per-org email credentials section giving
the four customer-IT steps verbatim from the spec's "What the customer's IT
has to do", including the two PowerShell commands and the Application Access
Policy recommendation, plus the `admin set-email --auth microsoft-oauth`
invocation.

- [ ] **Step 5: `docs/email-smoke-test.md`**

Add a Microsoft 365 section covering: connect via the wizard, confirm the four
failure modes produce their specific messages (wrong secret, wrong tenant, no
`Add-MailboxPermission`, wrong mailbox address), then a real end-to-end
triage run. State plainly that this is the **only** verification that a live
Exchange Online tenant accepts the flow — no test in the repository can prove
it — and that it must be run before selling to an M365 customer.

- [ ] **Step 6: `docs/STATUS.md`**

Add the Done entry for Phase 2. Under known issues add two entries: (1)
Microsoft 365 support is unverified against a live tenant until the smoke test
is run; (2) `_GraphBackend._token()` caches its access token forever, so a
long-lived process on `BESTTEAM_EMAIL_BACKEND=graph` fails about an hour after
start — pre-existing, unrelated to the per-org path, and deliberately not
fixed here.

- [ ] **Step 7: `docs/DECISIONS.md`**

Append an entry using the file's template so the scope call is not
re-litigated:

```markdown
## Email: OAuth over IMAP for Microsoft 365, not a Graph connector

- **Status**: Accepted (2026-08-17)
- **Context**: Exchange Online no longer accepts basic auth, so an M365 org
  could not connect a mailbox at all. The roadmap's Phase 2 named a
  "MailboxConnector abstraction + Graph/Gmail OAuth".
- **Decision**: Reach Exchange Online through IMAP with SASL XOAUTH2 and
  app-only client credentials. No connector protocol, no Graph-native code,
  no Gmail, no interactive authorisation-code OAuth.
- **Reasons**:
  - Graph-native would mean a second polling implementation, a second draft
    implementation, and migrating `EmailTrigger.last_uid`/`uidvalidity` to
    opaque cursors — and it would *regress* Phase 0, because Graph's
    server-side `createReply` cannot carry the `X-BestTeam-Source-Key` header
    that retry reconciliation reads.
  - A `MailboxConnector` protocol would abstract over one and a half
    implementations. The right time to extract it is while writing a second
    connector, so it is derived from two real ones.
  - Gmail is not blocked (app passwords work), and app-only Gmail needs
    domain-wide delegation covering every mailbox in the domain — a worse
    blast radius than Exchange's per-mailbox Application Access Policy.
  - Interactive OAuth needs a multi-tenant app registration and a stable
    public redirect URI. bestteam ships per-customer with operator-provisioned
    orgs and no public registration, so app-only credentials stored per org
    fit the existing model with no new infrastructure.
- **Consequences**: `ui/backend/email_trigger.py`, the UID cursor and the
  Phase 1 event ledger are untouched. Customers must do a one-time Azure app
  registration. No test can prove a live tenant accepts the flow, so
  `docs/email-smoke-test.md` is the gate. If Graph-native is ever needed, this
  does not block it.
```

- [ ] **Step 8: Verify the full suite serially, then commit**

Run: `.\.venv\Scripts\python.exe -m pytest -m "not e2e"`
Expected: green, no new warnings. Run it serially in one process (not
`-n auto`) — that is what catches ordering and cross-test isolation bugs, and
it is what `backend-full` does on `main`.

```bash
git add docs src/bestteam/tools/CLAUDE.md ui/backend/CLAUDE.md ui/backend/db/CLAUDE.md
git commit -m "docs: record Microsoft 365 mailbox connections and why not Graph"
```

---

## Self-Review

**1. Spec coverage.** Token provider → Task 1. `_ImapBackend` auth strategy and
the authobject → Task 2. Credential storage, the three columns and the
migration → Task 3. `build_org_imap_backend` dispatch → Task 4. API request
model, host pinning, `_mailbox_problem`, the four error mappings, `GET`
payload → Task 5. Admin CLI → Task 6. Frontend → Task 7. Docs, the smoke-test
caveat, the `_GraphBackend` adjacent defect and the DECISIONS entry → Task 8.
The spec's "poller unchanged" claim is covered as a Global Constraint and
asserted by running `tests/test_email_trigger.py` in Task 4.

**2. Placeholder scan.** Three places name a repo convention instead of exact
code — the fixtures in `tests/test_email_credentials.py`,
`tests/test_load_email_tools.py` and `tests/test_admin_cli.py`, and
`_alembic_config` in `tests/test_migrations.py`. Each says explicitly to read
the file and reuse what is there; inventing a second set of fixtures would be
the worse outcome. Task 7 Step 4 says "the existing IMAP fields, unchanged"
inside the JSX — that is a genuine no-op move of existing lines, not a
deferred decision.

**3. Type consistency.** `auth_type` values `"password"` / `"microsoft_oauth"`
are used identically in Tasks 3–7; the CLI's `--auth` flag deliberately spells
its choice `microsoft-oauth` (hyphen, argparse convention) and maps to the
constant in the handler. `MicrosoftClientCredentialsToken`'s keyword-only
`tenant_id` / `client_id` / `client_secret` match at all four construction
sites (Tasks 4, 5, 6 and the tests). `_ImapBackend`'s `token_provider=` matches
across Tasks 2, 4, 5 and 6. `OrgEmailConnectPayload`'s field names match the
Pydantic model's exactly, including `client_secret` rather than `password`.
