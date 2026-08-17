# Email Phase 2 — Microsoft 365 mailbox connections (OAuth)

**Date:** 2026-08-17
**Status:** Approved
**Depends on:** Phase 0 (`2026-08-17-email-phase-0-hardening-design.md`),
Phase 1 (`2026-08-17-email-phase-1-inbox-events-design.md`)

## The problem

An organisation on Microsoft 365 / Exchange Online **cannot connect a mailbox
to this platform at all.**

`ui/backend/email_tools.py::build_org_imap_backend` is the only path a
multi-org deployment has, and it builds an `_ImapBackend` with a stored
password. `_ImapBackend._connect()` calls `conn.login(user, password)` — basic
authentication. Microsoft removed basic auth from Exchange Online, so that
login is rejected for every M365 tenant regardless of what the customer types
into the wizard. The customer sees "rejected the sign-in — use a 16-character
app password", which is advice they cannot act on, because Exchange Online has
no app passwords to give them.

The single-org / SDK path has an escape hatch (`BESTTEAM_EMAIL_BACKEND=graph`,
app-only client credentials in env vars). The **per-org path has none**, and
per-org is the path every multi-tenant customer uses.

Google Workspace and generic IMAP hosts are *not* blocked: app passwords still
authenticate. This phase exists for Exchange Online specifically.

## Scope decision: XOAUTH2, not a connector abstraction

The roadmap named this phase "MailboxConnector abstraction + Graph/Gmail
OAuth". That framing is rejected here in favour of a much narrower change,
for reasons that are worth recording so they are not re-litigated.

Exchange Online supports **OAuth over IMAP** (SASL `XOAUTH2`) with the
client-credentials grant. Reaching an M365 mailbox therefore does not require
a second protocol implementation. It requires changing how one connection
authenticates:

| | Graph-native connector | IMAP + XOAUTH2 |
|---|---|---|
| Polling | new implementation (delta queries) | `check_mailbox` unchanged |
| Cursor | `EmailTrigger.last_uid:int`/`uidvalidity:int` must become opaque strings + migration | unchanged |
| Drafts | new implementation (`createReply`) | unchanged |
| Phase 0 draft markers | **lost** — `createReply` builds the draft server-side, so `X-BestTeam-Source-Key` can't be stamped and `drafts_with_source_keys` has nothing to reconcile against | unchanged |
| Poller (`email_trigger.py`) | substantially rewritten | **zero edits** |
| Testable without a live tenant | almost none of it | all but one integration point |

The connector abstraction is deliberately **not** built. It would be an
abstraction over one and a half implementations — the `_ImapBackend` and a
`_GraphBackend` that the multi-tenant path still would not use. The right time
to extract a `MailboxConnector` protocol is when a second connector is being
written, so that the protocol is derived from two real implementations rather
than invented ahead of one.

### What this rules out, deliberately

- **Gmail / Google Workspace API.** Nothing is blocked there today — app
  passwords work. App-only Gmail access requires a service account with
  domain-wide delegation, which is a different token flow (RS256-signed JWT
  assertions) and grants access to *every* mailbox in the customer's domain.
  That is a strictly worse blast radius than Exchange's per-mailbox
  Application Access Policy, for a customer who is not blocked. Out of scope.
- **Interactive (authorisation-code) OAuth.** A delegated flow requires this
  project to own a multi-tenant app registration and a stable public HTTPS
  redirect URI. bestteam ships as a per-customer deployment with
  operator-provisioned orgs and no public registration (see
  `docs/DECISIONS.md`, "org-scoped multi-tenancy"). App-only client
  credentials stored per-org — exactly how the IMAP password is stored today —
  fit that model with no new infrastructure.
- **The env / SDK path.** `_ImapBackend.from_env()` and the `BESTTEAM_IMAP_*`
  variables are untouched. A single-org M365 deployment already has a working
  option in `BESTTEAM_EMAIL_BACKEND=graph`; adding a second one would be
  duplicate surface for a customer who is not blocked.
- **Graph-native anything.** `_GraphBackend` is not modified, promoted, or
  removed.

## What the customer's IT has to do

This is app-only access, so the work happens once in the customer's own Azure
tenant. It is not something the platform can do on their behalf.

1. Register an application in Entra ID (Azure AD). Note the **Directory
   (tenant) ID** and **Application (client) ID**, and create a **client
   secret**.
2. Add the API permission **Office 365 Exchange Online → Application →
   `IMAP.AccessAsApp`**, and grant admin consent.
3. In Exchange Online PowerShell, register the service principal and grant it
   access to the one mailbox:
   ```powershell
   New-ServicePrincipal -AppId <client-id> -ServiceId <object-id>
   Add-MailboxPermission -Identity <mailbox> -User <object-id> -AccessRights FullAccess
   ```
4. Optionally restrict the app to that single mailbox with an Exchange
   **Application Access Policy** — the same least-privilege guidance the Graph
   backend already carries in `src/bestteam/tools/CLAUDE.md`.

The customer then enters the tenant ID, client ID, client secret and mailbox
address in the wizard. Steps 1–4 are documented in `docs/deployment.md` and
verified by `docs/email-smoke-test.md`.

**Verification caveat, stated plainly:** Microsoft has been retiring legacy
protocol access in stages, and no test in this repository can prove that a
real Exchange Online tenant accepts this flow today. The unit tests prove the
SASL string, the token lifecycle, the storage round-trip and the error
mapping. The one thing they cannot prove — that Microsoft accepts the
resulting `AUTHENTICATE XOAUTH2` — is exactly what the smoke test covers, and
it must be run against a real tenant before this is sold to an M365 customer.
The design keeps that risk cheap: if Exchange Online ever refuses OAuth-IMAP,
only `_connect()` and the credential shape are affected.

## Design

### Token provider — `src/bestteam/tools/_oauth.py` (new)

One focused module, stdlib only. `email_client.py` is already 740 lines and
this does not belong in it.

```python
class MicrosoftClientCredentialsToken:
    def __init__(self, *, tenant_id, client_id, client_secret,
                 scope="https://outlook.office365.com/.default"): ...
    def token(self) -> str: ...
```

- POSTs form-encoded credentials to
  `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token` using
  `urllib.request` — **no httpx**. The per-org IMAP path is stdlib-only today
  (`pyproject.toml` marks `tools-email`'s httpx as "Graph backend only"), and
  the `backend-optional-deps` CI job runs without optional extras. Adding a
  dependency to reach one well-known public endpoint is not worth it.
- The token endpoint is a fixed constant, never customer-supplied, so the
  `check_host_allowed` SSRF guard that customer-supplied IMAP hosts get is not
  needed here. The tenant ID is interpolated into the path and is
  percent-encoded before use.
- **Caches until expiry**, not forever: stores `expires_in` with a 60-second
  safety margin and refetches past it. A cache-forever token would break the
  poller after an hour — `_GraphBackend._token()` has exactly that bug today
  (`if self._access_token is None`), recorded below as an adjacent defect and
  deliberately not fixed here.
- Wraps the POST in the existing `with_retry` helper, retrying transport
  errors and 5xx only. A 4xx is a configuration error and fails fast.
- Raises `ConfigurationError` with the error body's `error_description` when
  Microsoft rejects the credentials, so the API layer has something specific
  to map into customer-facing language.

### `_ImapBackend` gains an auth strategy

```python
_ImapBackend(host=, user=, password=None, port=993, drafts=None,
             restrict_to_public=False, token_provider=None)
```

Exactly one of `password` / `token_provider` must be given; both or neither is
a `ConfigurationError` raised at construction, not at connect time.

`_connect()` is the only method that changes. Everything after authentication
— `find`, `read`, `draft_reply`, `summaries_for`, `drafts_with_source_keys`,
`check_drafts_writable`, `_drafts_folder`, the source-key headers — is
untouched, because after `AUTHENTICATE` succeeds the connection is an ordinary
authenticated IMAP session.

```python
conn = with_retry(factory, retriable_exc=(OSError,))
try:
    if self._token_provider is None:
        conn.login(self._user, self._password)
    else:
        conn.authenticate("XOAUTH2", _xoauth2_authobject(self._user,
                                                         self._token_provider.token()))
except imaplib.IMAP4.error as exc:
    raise ConfigurationError(...)
```

The authobject:

```python
def _xoauth2_authobject(user: str, access_token: str):
    """SASL XOAUTH2 client-first response, then an empty line on rejection."""
    initial = f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()
    state = {"sent": False}

    def authobject(challenge: bytes) -> bytes:
        if not state["sent"]:
            state["sent"] = True
            return initial
        # Rejection: the server sends a base64 JSON error and waits for an
        # empty client response before issuing the tagged NO. Returning b""
        # lets imaplib finish the exchange instead of stalling.
        return b""

    return authobject
```

This shape is dictated by verified stdlib behaviour, not assumed:
`imaplib._Authenticator.process` base64-*decodes* the server challenge before
calling the authobject and base64-*encodes* whatever bytes come back, and
`_simple_command('AUTHENTICATE', mech)` invokes the authobject on the server's
first continuation. So the client-first initial response is delivered by
returning it from the first call — the same path `imaplib.login_cram_md5`
already relies on.

Note the retry boundary is unchanged: only the socket connect is retried;
authentication failures fail fast. A rejected token is a configuration
problem, and retrying it three times against Microsoft's token endpoint would
just slow down the error.

### Credential storage — `org_email_credentials`

Three new columns, all additive (`ALTER TABLE ... ADD COLUMN`, which SQLite
supports natively, so no batch-mode table rebuild):

| Column | Type | Meaning |
|---|---|---|
| `auth_type` | `str`, default `"password"` | `"password"` \| `"microsoft_oauth"` |
| `oauth_tenant_id` | `str \| None` | Entra Directory (tenant) ID |
| `oauth_client_id` | `str \| None` | Application (client) ID |

`password_encrypted` is **reused** to hold the OAuth client secret when
`auth_type == "microsoft_oauth"`. This is deliberate: it is the column for
"the encrypted credential material for this mailbox", there is exactly one
`secret_store.encrypt`/`decrypt` call site either way, and
`ensure_secrets_key_for_stored_credentials` — which refuses to boot when a
rotated key cannot read stored credentials — keeps working unchanged for both
auth types. A second secret column would mean a second thing to forget to
check. The column's docstring records the dual meaning.

Tenant ID and client ID are identifiers, not secrets, and are stored in the
clear alongside `host` and `username`.

`set_email_credentials(...)` gains `auth_type`, `oauth_tenant_id`,
`oauth_client_id` keyword arguments, all defaulted so existing callers are
unaffected.

**Existing rows need no backfill.** `auth_type` defaults to `"password"`,
which is what every current row is.

### `build_org_imap_backend` dispatches on `auth_type`

```python
if cred.auth_type == "microsoft_oauth":
    provider = MicrosoftClientCredentialsToken(
        tenant_id=cred.oauth_tenant_id,
        client_id=cred.oauth_client_id,
        client_secret=secret_store.decrypt(cred.password_encrypted),
    )
    return _ImapBackend(host=cred.host, user=cred.username, port=cred.port,
                        drafts=cred.drafts_folder, restrict_to_public=True,
                        token_provider=provider)
```

The password branch is unchanged. `load_email_tools`'s decrypt-failure
handling covers both, since both decrypt the same column.

**The poller is not touched.** `poll_org` calls `build_org_imap_backend` and
then `check_mailbox(backend, trigger.last_uid)`, which issues IMAP `STATUS` /
`UID SEARCH` against whatever session `_connect()` returns. An XOAUTH2 session
is an IMAP session. Phase 0's health writeback, Phase 1's ledger, the daily
cap, the overlap guard and the retry path all work as-is.

### API — `PUT/POST/GET /api/org/email`

`EmailConnectRequest` becomes conditionally validated rather than split into
two endpoints, so the frontend keeps one call and the "validate before store"
guarantee stays in one place:

```python
class EmailConnectRequest(BaseModel):
    auth_type: Literal["password", "microsoft_oauth"] = "password"
    host: str = ""
    username: str
    password: Optional[str] = None
    client_secret: Optional[str] = None
    oauth_tenant_id: Optional[str] = None
    oauth_client_id: Optional[str] = None
    port: int = Field(default=993, ge=1, le=65535)
    drafts: Optional[str] = None
```

A model validator enforces:

- `password`: `host` and `password` required; OAuth fields must be absent.
- `microsoft_oauth`: `oauth_tenant_id`, `oauth_client_id`, `client_secret`
  required; `password` must be absent; **`host` is overridden server-side to
  `outlook.office365.com`** and any client-supplied value is discarded.

Pinning the host is a correctness measure, not just convenience: Exchange
Online's IMAP endpoint is always that host, the token scope is bound to it,
and accepting an arbitrary host for this auth type would only ever produce a
confusing failure. It also means the SSRF guard has nothing customer-supplied
to guard on that branch, though `_reject_private_host` still runs uniformly.

`auth_type` defaults to `"password"`, so an old client that posts the current
body keeps working byte-for-byte.

`_mailbox_problem` builds the backend the same way `build_org_imap_backend`
does and runs the same two checks it runs today (connect, then
`check_drafts_writable`). Only construction differs.

`GET /api/org/email` additionally returns `auth_type`, `oauth_tenant_id` and
`oauth_client_id`, so the UI can show how the mailbox is connected and
pre-fill a reconnect. It still never returns `password_encrypted` or anything
derived from it.

### Customer-facing error messages

`_friendly_connect_error` today assumes a password login and tells the user to
use a 16-character app password. That advice is actively wrong for an M365
connection, so the mapping becomes auth-type aware. The M365 failure modes,
which are distinguishable and each have a different fix:

| What happened | What the customer is told |
|---|---|
| Token endpoint returns `invalid_client` / `unauthorized_client` | The application ID or client secret is wrong, or the secret has expired. |
| Token endpoint returns `invalid_request` on an unknown tenant | The Directory (tenant) ID isn't recognised. |
| Token obtained, `AUTHENTICATE` rejected | Sign-in succeeded but Microsoft refused mailbox access — the `IMAP.AccessAsApp` permission needs admin consent, and the app needs `New-ServicePrincipal` + `Add-MailboxPermission` in Exchange Online. Links to the deployment doc. |
| `AUTHENTICATE` rejected naming an unknown mailbox | The mailbox address doesn't exist in this tenant. |

The third row is the one that matters: a working token with a refused mailbox
is the single most likely outcome of a half-finished Azure setup, and without
a specific message it is indistinguishable from a wrong password.

### Admin CLI

`admin set-email` gains `--auth password|microsoft-oauth`, `--tenant`,
`--client-id`. With `--auth microsoft-oauth` the prompt asks for the client
secret instead of the password and `--host` becomes optional (defaulted). The
operator CLI must keep working when the app refuses to start, which it does —
nothing here changes its import surface.

### Frontend — `EmailConnect.tsx`

A provider choice at the top of the form, defaulting to the current behaviour:

- **Standard mailbox (IMAP)** — today's form, unchanged.
- **Microsoft 365 / Outlook** — mailbox address, Directory (tenant) ID,
  Application (client) ID, client secret. Server address and port are not
  shown; they are fixed. Advanced settings collapse to just the drafts folder.

The M365 branch carries a short "send this to your IT administrator" list
naming the three things they must do (app registration, `IMAP.AccessAsApp`
with admin consent, `Add-MailboxPermission`), because a non-technical customer
cannot produce a tenant ID unaided and the wizard should say so rather than
present four empty boxes.

`startReconnect` pre-fills `auth_type`, tenant ID and client ID from the
status payload; the secret stays blank and write-only, as the password does
today.

## Data flow

```
Customer (wizard)                 Backend                          Microsoft
      |                              |                                 |
      |-- PUT /api/org/email ------->|                                 |
      |   auth_type=microsoft_oauth  |                                 |
      |                              |-- POST /oauth2/v2.0/token ----->|
      |                              |<-- access_token, expires_in ----|
      |                              |                                 |
      |                              |-- IMAP AUTHENTICATE XOAUTH2 --->| outlook.office365.com
      |                              |<-- OK / NO ---------------------|
      |                              |-- SELECT Drafts (read-only) --->|
      |                              |                                 |
      |<-- 200 / 400 + reason -------|  (store only if both succeeded) |

Later, every poll cycle and every agent tool call:
  build_org_imap_backend -> decrypt secret -> token (cached until expiry)
                         -> _connect() -> AUTHENTICATE XOAUTH2 -> ordinary IMAP
```

## Error handling

- A token fetch that fails raises `ConfigurationError`; the connect path maps
  it to customer language, and the poller's existing handling records it as a
  `mailbox`-kind trigger error, which auto-clears on the next successful check
  (Phase 0, item 0.5). It is not a `workflow`-kind error — the workflow is
  fine, the mailbox is not.
- An expired client secret (Azure secrets expire, typically at 6–24 months)
  presents as a token rejection on every cycle. Because it is a `mailbox`-kind
  error, the existing trigger-health surface already shows it on the activity
  page without new UI. No proactive expiry warning is built here; that belongs
  with Phase 3's alerting.
- A token that is valid but whose mailbox access was revoked presents as an
  `AUTHENTICATE` failure, also `mailbox`-kind.
- Nothing about failure handling in `email_trigger.py` or `runtime.py`
  changes.

## Testing

All tests are `fake:`-model / no-network, matching the existing suite.

- `tests/test_oauth_token.py` (new, `unit`) — the token request body and URL;
  caching within the expiry window; refetch past it; the 60-second margin;
  `ConfigurationError` carrying `error_description` on a 4xx; retry on 5xx and
  no retry on 4xx. A fake `urlopen` injected by monkeypatch.
- `tests/test_email_tools.py` (`unit`) — the exact XOAUTH2 SASL byte string;
  the authobject returns the initial response first and `b""` on a subsequent
  challenge; `_connect()` calls `authenticate` (not `login`) when a token
  provider is present, and `login` when it is not; constructing with both or
  neither raises.
- `tests/test_email_credentials.py` (`integration`) — round-trip of an OAuth
  credential; the secret is stored as a Fernet token, never plaintext;
  `ensure_secrets_key_for_stored_credentials` still catches an unreadable
  OAuth row; an existing row reads back as `auth_type == "password"`.
- `tests/test_org_settings.py` (`integration`) — the validator rejects each
  missing-field combination; a client-supplied `host` is discarded on the
  OAuth branch; `GET` exposes tenant/client IDs and no secret; each of the
  four error mappings; the existing password body still works unchanged.
- `tests/test_admin_cli.py` (`integration`) — `set-email --auth
  microsoft-oauth` stores the right row.
- `tests/test_migrations.py` — upgrade/downgrade round-trip; a pre-migration
  row survives with `auth_type == "password"`.
- `ui/frontend/src/components/EmailConnect.test.tsx` (new) — the provider switch
  renders the right fields and posts the right body; reconnect pre-fills the
  OAuth identifiers but not the secret.

Success criteria: the full non-e2e suite green, run **serially in one
process** (the `backend-full` equivalent, since the PR gate runs distributed);
`npm test` green.

## Adjacent defect, noted and not fixed

`_GraphBackend._token()` caches its access token forever
(`if self._access_token is None:`). Microsoft's tokens expire in about an
hour, so a long-lived process using `BESTTEAM_EMAIL_BACKEND=graph` will start
failing after that until it restarts. This is pre-existing, unrelated to the
per-org path this phase changes, and the poller never constructs a
`_GraphBackend`. Fixing it would mean editing code this phase otherwise does
not touch, so it is recorded in `docs/STATUS.md` as a known issue instead.

## Explicitly out of scope

The `MailboxConnector` protocol, Graph-native polling or drafting, Gmail /
Google Workspace, interactive authorisation-code OAuth, the env / SDK email
path, generalising `EmailTrigger.last_uid`/`uidvalidity` into opaque cursors,
proactive client-secret-expiry alerting (Phase 3), pre-LLM filtering and spend
budgets (Phase 4), and send capability (Phase 5).
