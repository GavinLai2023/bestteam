"""OAuth client-credentials token tests -- no network is ever touched.

`urlopen` is replaced with a fake that records the request and returns a canned
body, so these assert the exact wire shape (URL, form fields) and the caching
lifecycle rather than a mocked-out approximation of them.
"""

import io
import json
import urllib.error
import urllib.parse
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
        "https://login.microsoftonline.com/x",
        code,
        "err",
        {},
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
    assert request.full_url == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    body = urllib.parse.parse_qs(request.data.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert body["client_id"] == ["client-1"]
    assert body["client_secret"] == ["shh"]
    assert body["scope"] == ["https://outlook.office365.com/.default"]


def test_a_tenant_id_with_url_characters_is_percent_encoded_into_the_path():
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
    with patch.object(_oauth.urllib.request, "urlopen", fake_urlopen), patch.object(
        _oauth.time, "monotonic", lambda: clock[0]
    ):
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
        raise _http_error(
            401,
            {
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
            },
        )

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
    with patch.object(
        _oauth.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({"token_type": "Bearer"})
    ):
        with pytest.raises(ConfigurationError, match="no access token"):
            _provider().token()
