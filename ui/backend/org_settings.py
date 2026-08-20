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
from datetime import date, datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from bestteam.exceptions import ConfigurationError
from bestteam.tools._oauth import MicrosoftClientCredentialsToken
from bestteam.tools.email_client import _ImapBackend
from bestteam.tools.http_client import check_host_allowed

from .activity_overview import compute_overview
from .auth_api import get_current_org
from .db.email_budget_settings import (
    get_budget_caps,
    set_budget_caps,
    spent_this_month,
    unpriced_models_for_org,
    unpriced_run_count,
)
from .db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    AUTH_PASSWORD,
    MICROSOFT_IMAP_HOST,
    clear_email_credentials,
    get_email_credentials,
    set_email_credentials,
)
from .db.email_filter_settings import get_filter_settings, set_filter_settings
from .db.email_triggers import get_email_trigger
from .db.models import Organization, Run, iso_utc
from .db.notifications import get_notification_settings, set_notification_settings
from .db.retention import get_retention_settings, set_retention_days
from .db_session import get_db
from .email_budget import day_key
from .email_trigger import disable_trigger, disable_trigger_on_identity_change
from .notifications import WebhookUrlError, validate_webhook_url
from .retention import export_org_runs, purge_org_runs, purgeable_run_count
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
    # Optional, Microsoft 365 only. Read off the Azure portal page the admin
    # is already looking at when they copy the client secret; it powers the
    # advance warning before the secret expires.
    oauth_secret_expires_at: Optional[date] = None
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
            if (
                self.client_secret
                or self.oauth_tenant_id
                or self.oauth_client_id
                or self.oauth_secret_expires_at
            ):
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
        "oauth_secret_expires_at": (
            cred.oauth_secret_expires_at.date().isoformat()
            if cred.oauth_secret_expires_at is not None else None
        ),
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
            oauth_secret_expires_at=req.oauth_secret_expires_at,
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


# --- notification settings ---------------------------------------------------


class NotificationSettingsRequest(BaseModel):
    """Where this org wants its alerts delivered, if anywhere.

    `webhook_secret` is write-only: `GET` reports only whether one is stored,
    so the UI can never echo it back. Omitting it on an update keeps whatever
    is already stored; sending an empty string clears it.
    """

    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None
    enabled: bool = True


@router.get("/notifications")
def get_notification_settings_route(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """The org's delivery settings. Never returns the webhook secret."""
    row = get_notification_settings(db, org.id)
    if row is None:
        return {"webhook_url": None, "has_webhook_secret": False, "enabled": True}
    return {
        "webhook_url": row.webhook_url,
        "has_webhook_secret": bool(row.webhook_secret_encrypted),
        "enabled": bool(row.enabled),
    }


@router.put("/notifications")
def put_notification_settings(
    req: NotificationSettingsRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Save the org's delivery settings.

    The URL is validated synchronously and a bad one is a 400: here the
    customer is present to fix it, unlike the delivery path where a failure
    must never break the poll cycle.
    """
    url = (req.webhook_url or "").strip()
    if url:
        try:
            url = validate_webhook_url(url)
        except WebhookUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    set_notification_settings(
        db, org.id,
        webhook_url=url or None,
        webhook_secret=req.webhook_secret,
        enabled=req.enabled,
        # No secret in the payload means "leave the stored one alone" -- the UI
        # never received it, so it cannot resend it. An explicit empty string
        # clears it.
        keep_existing_secret=req.webhook_secret is None,
    )
    db.commit()
    return get_notification_settings_route(db=db, org=org)


# --- run-history retention and export (Phase 3b) ------------------------------


class RetentionRequest(BaseModel):
    """NULL means keep forever -- the default, so an upgrade deletes nothing."""

    run_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)


class PurgeRequest(BaseModel):
    """`older_than_days` is required on purpose: a destructive action whose
    body says what it will remove is the difference between a confirmed action
    and a slip. 0 means everything terminal."""

    older_than_days: int = Field(ge=0, le=3650)


@router.get("/retention")
def get_retention(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """This org's retention policy, plus proof the sweep is actually running."""
    row = get_retention_settings(db, org.id)
    days = row.run_retention_days if row else None
    return {
        "run_retention_days": days,
        "last_swept_at": iso_utc(row.last_swept_at) if row and row.last_swept_at else None,
        "last_purged_count": row.last_purged_count if row else 0,
        # What saving this setting would remove on the next sweep -- the number
        # that makes it safe to press save.
        "purgeable_now": (
            0 if days is None
            else purgeable_run_count(db, org_id=org.id, older_than_days=days)
        ),
    }


@router.put("/retention")
def put_retention(
    req: RetentionRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Set or clear the policy. Saving deletes nothing by itself -- the sweep
    does, on its next cycle."""
    set_retention_days(db, org.id, req.run_retention_days)
    db.commit()
    return get_retention(db=db, org=org)


@router.post("/retention/purge")
def purge_retention(
    req: PurgeRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Remove the content of this org's runs older than the given window, now."""
    purged = purge_org_runs(db, org_id=org.id, older_than_days=req.older_than_days)
    db.commit()
    return {"purged": purged}


@router.get("/export")
def export_org(
    days: Optional[int] = None,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> JSONResponse:
    """The org's run history as JSON, so a customer can take their data out
    before a retention policy removes it."""
    bundle = export_org_runs(db, org_id=org.id, days=days)
    filename = f"bestteam-export-org-{org.id}.json"
    return JSONResponse(
        content=bundle,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/activity-overview")
def activity_overview(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """Engagement stats for the customer-facing Activity Overview tab -- how
    often this org's teams ran. Never a model name or a cost: see
    activity_overview.py."""
    timestamps = [row[0] for row in db.query(Run.created_at).filter(Run.org_id == org.id)]
    return compute_overview(timestamps, now=datetime.now(timezone.utc))


# --- pre-LLM mail filter and automation budgets (Phase 4a) --------------------


# One stored pattern: a full address, `*@domain`, or a subject term. Bounded
# per item, not just per list, because the poll loop reads every pattern an org
# has stored and compares it against every sender address -- a customer-supplied
# field the hot path reads is not left unbounded, and 200 characters is generous
# for all three uses.
FilterPattern = Annotated[str, StringConstraints(max_length=200)]


class EmailFilterRequest(BaseModel):
    """Patterns are literals, never regular expressions -- see
    `ui/backend/email_filter.py` for why."""

    skip_bulk: bool = True
    sender_blocklist: List[FilterPattern] = Field(default_factory=list, max_length=200)
    sender_allowlist: List[FilterPattern] = Field(default_factory=list, max_length=200)
    subject_blocklist: List[FilterPattern] = Field(default_factory=list, max_length=200)


class EmailBudgetRequest(BaseModel):
    """NULL means no cap -- the default, so an upgrade limits nobody."""

    daily_message_cap: Optional[int] = Field(default=None, ge=1, le=100000)
    monthly_cost_cap: Optional[float] = Field(default=None, ge=0.01, le=1000000)


@router.get("/email-filter")
def get_email_filter(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    settings = get_filter_settings(db, org.id)
    return {
        "skip_bulk": settings.skip_bulk,
        "sender_blocklist": list(settings.sender_blocklist),
        "sender_allowlist": list(settings.sender_allowlist),
        "subject_blocklist": list(settings.subject_blocklist),
    }


@router.put("/email-filter")
def put_email_filter(
    req: EmailFilterRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    set_filter_settings(
        db, org.id,
        skip_bulk=req.skip_bulk,
        sender_blocklist=req.sender_blocklist,
        sender_allowlist=req.sender_allowlist,
        subject_blocklist=req.subject_blocklist,
    )
    db.commit()
    return get_email_filter(db=db, org=org)


@router.get("/email-budget")
def get_email_budget(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """The caps, this period's usage against them, and the blind spot.

    `unpriced_models` is advisory, never an error: a model with no
    `model_catalog` row contributes 0 to the spend total, so the cap is a floor
    on reality rather than a phantom ceiling -- and the admin is told which
    models it does not cover instead of being left to infer it.
    """
    now = datetime.now(timezone.utc)
    caps = get_budget_caps(db, org.id)
    trigger = get_email_trigger(db, org.id)
    return {
        "daily_message_cap": caps.daily_message_cap,
        "monthly_cost_cap": caps.monthly_cost_cap,
        # `messages_today` is only today's count while `runs_date` still is
        # today -- the poller resets both together on the first cycle of a new
        # day, so reading the column alone would report yesterday's total until
        # then (the same guard `email_trigger_api._payload` applies to
        # `runs_today`).
        "messages_today": (
            trigger.messages_today
            if trigger is not None and trigger.runs_date == day_key(now)
            else 0
        ),
        "spent_this_month": spent_this_month(db, org.id, now),
        "unpriced_runs_this_month": unpriced_run_count(db, org.id, now),
        "unpriced_models": unpriced_models_for_org(db, org.id),
    }


@router.put("/email-budget")
def put_email_budget(
    req: EmailBudgetRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Save the caps. An unpriced model is reported back, never a refusal:
    one missing catalogue row must not stop an admin capping their spend."""
    set_budget_caps(
        db, org.id,
        daily_message_cap=req.daily_message_cap,
        monthly_cost_cap=req.monthly_cost_cap,
    )
    db.commit()
    return get_email_budget(db=db, org=org)
