"""Org-scoped self-service settings (`/api/org`).

Currently: the customer connects/tests/rotates their own mailbox for the email
tools, on top of the per-org secrets store (`db/email_credentials.py`). Every
route is guarded by `get_current_org`, so the org's user manages only their own
org's mailbox -- no per-org admin role is needed while there is one user per
org (see the design spec).

The IMAP host is customer-supplied, so `PUT` and `POST /test` reject a host
that resolves to a private/internal address (SSRF guard, reusing
`http_client.check_host_allowed`). The stored password is never returned.
"""

from __future__ import annotations

import errno
import logging
import socket
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from bestteam.exceptions import ConfigurationError
from bestteam.tools._oauth import MicrosoftClientCredentialsToken
from bestteam.tools.email_client import _ImapBackend
from bestteam.tools.http_client import check_host_allowed

from .auth_api import get_current_org
from .db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    AUTH_PASSWORD,
    MICROSOFT_IMAP_HOST,
    clear_email_credentials,
    get_email_credentials,
    set_email_credentials,
)
from .db.models import Organization
from .db_session import get_db
from .email_trigger import disable_trigger, disable_trigger_on_identity_change
from .secret_store import SecretsKeyError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org", tags=["org-settings"])


class EmailConnectRequest(BaseModel):
    """A mailbox connection attempt, for either auth type.

    One model rather than two endpoints, so "validate the real thing before
    storing it" stays in one place. `auth_type` defaults to `password`, so a
    client posting the pre-Phase-2 body keeps working unchanged.
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
                raise ValueError("A Microsoft 365 mailbox needs the " + ", ".join(missing) + ".")
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
        source = self.client_secret if self.auth_type == AUTH_MICROSOFT_OAUTH else self.password
        return source or ""


def _friendly_connect_error(exc: Exception, host: str, port: int) -> str:
    """Plain-language mailbox-connection error for the wizard.

    The customer connecting a mailbox is non-technical, so raw socket strings
    (e.g. ``[WinError 10060] ...``) are useless and alarming. Map the handful of
    real failure modes to actionable sentences; never surface the OS code.
    """
    _timeout = (
        f"Couldn't reach {host} on port {port} — the server didn't respond. "
        f"Check the server address and port; most providers (including Gmail) use 993."
    )
    if isinstance(exc, ConfigurationError):
        # From _connect: a login rejection (the only ConfigurationError that
        # reaches here after the up-front SSRF check) -- otherwise show as-is.
        if "login" in str(exc).lower():
            return (
                f"{host} rejected the sign-in. Use a 16-character app password "
                "(not your normal account password), and double-check the email address."
            )
        return str(exc)
    if isinstance(exc, socket.gaierror):
        return f"Couldn't find a mail server at '{host}'. Check the spelling of the server address."
    if isinstance(exc, TimeoutError):
        return _timeout
    if isinstance(exc, ConnectionRefusedError):
        return f"{host} refused the connection on port {port}. Check the port — most providers use 993."
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == 10060 or exc.errno == errno.ETIMEDOUT:
            return _timeout
        if exc.errno == errno.ECONNREFUSED:
            return f"{host} refused the connection on port {port}. Check the port — most providers use 993."
    return (
        f"Couldn't connect to {host} on port {port}. Double-check the server "
        "address, the port, and that the mailbox has IMAP access enabled."
    )


def _reject_private_host(host: str) -> None:
    """SSRF guard: refuse a mailbox host that resolves to a private address."""
    try:
        check_host_allowed(host)
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot connect to that host: {exc}") from exc


_M365_ACCESS_HELP = (
    "Microsoft accepted the app's sign-in but refused it access to '{mailbox}'. "
    "Ask your IT administrator to grant admin consent for the IMAP.AccessAsApp "
    "permission, then register the app against this mailbox in Exchange Online "
    "(New-ServicePrincipal, then Add-MailboxPermission). See docs/deployment.md."
)


def _token_provider_for(req: "EmailConnectRequest"):
    """The token provider for this request, or None for a password mailbox."""
    if req.auth_type != AUTH_MICROSOFT_OAUTH:
        return None
    return MicrosoftClientCredentialsToken(
        tenant_id=(req.oauth_tenant_id or "").strip(),
        client_id=(req.oauth_client_id or "").strip(),
        client_secret=req.client_secret or "",
    )


def _backend_for(req: "EmailConnectRequest", token_provider):
    """The same backend `email_tools.build_org_imap_backend` will build later.

    Deliberately kept in step with it: validating a differently-built backend
    would let a mailbox pass the wizard and then fail on its first real run.
    """
    if token_provider is not None:
        return _ImapBackend(
            host=req.host, user=req.username, port=req.port, drafts=req.drafts,
            restrict_to_public=True, token_provider=token_provider,
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


def _mailbox_problem(req: "EmailConnectRequest") -> Optional[str]:
    """`None` if the mailbox is genuinely usable, else a customer-facing reason.

    Checks BOTH halves of what the toolkit needs, because a successful login
    alone was never enough: every reply this platform produces is an APPEND to
    the drafts folder, so a mailbox whose drafts folder doesn't exist under the
    configured name (or isn't writable by this account) passes a login test and
    then fails on the very first real draft, long after the customer has left
    the wizard (Phase 0, item 0.7). Nothing is written to the mailbox -- a
    SELECT that succeeds without reporting READ-ONLY is enough.

    For a Microsoft 365 mailbox the token is fetched on its own first, so that a
    credential problem (wrong client ID, expired secret, unknown tenant) is
    distinguishable from a mailbox-access problem (admin consent or
    Add-MailboxPermission missing). Those have completely different fixes and
    are not tellable apart from the error text alone.
    """
    token_provider = _token_provider_for(req)
    if token_provider is not None:
        try:
            token_provider.token()
        except ConfigurationError as exc:
            return _friendly_oauth_credential_error(exc)
    backend = _backend_for(req, token_provider)
    try:
        conn = backend._connect()
        conn.logout()
    except OSError as exc:
        return _friendly_connect_error(exc, req.host, req.port)
    except ConfigurationError as exc:
        # The token was already proven good above, so what failed here is this
        # app's access to the mailbox, not its credentials.
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


@router.get("/email")
def get_email(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """Connection status for the org's mailbox. Never returns the password."""
    cred = get_email_credentials(db, org.id)
    if cred is None:
        return {"connected": False}
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


@router.put("/email")
def set_email(
    req: EmailConnectRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Connect or rotate the org's mailbox (encrypts the password before store)."""
    _reject_private_host(req.host)
    # Validate BEFORE storing: a mailbox saved without a working login or a
    # writable drafts folder looks "connected" everywhere in the UI while
    # every automatic run against it fails.
    problem = _mailbox_problem(req)
    if problem is not None:
        raise HTTPException(status_code=400, detail=problem)
    prior = get_email_credentials(db, org.id)
    prior_identity = (prior.host, prior.username) if prior is not None else None
    try:
        set_email_credentials(
            db, org.id, host=req.host, username=req.username, password=req.secret,
            port=req.port, drafts_folder=req.drafts, auth_type=req.auth_type,
            oauth_tenant_id=(req.oauth_tenant_id or "").strip() or None,
            oauth_client_id=(req.oauth_client_id or "").strip() or None,
        )
    except SecretsKeyError as exc:
        # The encryption key (BESTTEAM_SECRETS_KEY) is missing/invalid -- a server
        # setup problem the customer can't fix. Log the real cause for the
        # operator; tell the end user something actionable (contact an admin).
        logger.error("Mailbox save failed: secrets key not configured (%s)", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "Your mailbox couldn't be saved because secure storage isn't set "
                "up on this service yet. Please contact your administrator."
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- never leak internal errors to the user
        logger.exception("Mailbox save failed unexpectedly for org %s", org.id)
        raise HTTPException(
            status_code=500,
            detail=(
                "Your mailbox couldn't be saved due to an unexpected problem. "
                "Please try again, or contact your administrator if it continues."
            ),
        ) from exc
    disable_trigger_on_identity_change(db, org.id, req.host, req.username, prior_identity)
    return {"connected": True, "host": req.host, "username": req.username}


@router.post("/email/test")
def test_email(
    req: EmailConnectRequest,
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Attempt an IMAP login with the posted (unsaved) credentials.

    Returns `{ok: true}` on success or `{ok: false, error}` on a login/network
    failure (a normal "test failed" result, not an HTTP error); a private host
    is a hard 400 (SSRF guard).
    """
    _reject_private_host(req.host)
    problem = _mailbox_problem(req)
    if problem is not None:
        return {"ok": False, "error": problem}
    return {"ok": True}


@router.delete("/email", status_code=204)
def delete_email(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
):
    """Disconnect the org's mailbox."""
    clear_email_credentials(db, org.id)
    disable_trigger(db, org.id)
