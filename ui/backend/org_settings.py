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

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from bestteam.exceptions import ConfigurationError
from bestteam.tools.email_client import _ImapBackend
from bestteam.tools.http_client import check_host_allowed

from .auth_api import get_current_org
from .db.email_credentials import (
    clear_email_credentials,
    get_email_credentials,
    set_email_credentials,
)
from .db.models import Organization
from .db_session import get_db

router = APIRouter(prefix="/api/org", tags=["org-settings"])


class EmailConnectRequest(BaseModel):
    host: str
    username: str
    password: str
    port: int = Field(default=993, ge=1, le=65535)
    drafts: Optional[str] = None


def _reject_private_host(host: str) -> None:
    """SSRF guard: refuse a mailbox host that resolves to a private address."""
    try:
        check_host_allowed(host)
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot connect to that host: {exc}") from exc


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
    }


@router.put("/email")
def set_email(
    req: EmailConnectRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Connect or rotate the org's mailbox (encrypts the password before store)."""
    _reject_private_host(req.host)
    try:
        set_email_credentials(
            db, org.id, host=req.host, username=req.username, password=req.password,
            port=req.port, drafts_folder=req.drafts,
        )
    except Exception as exc:  # noqa: BLE001 -- e.g. a missing/colliding secrets key
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    backend = _ImapBackend(
        host=req.host, user=req.username, password=req.password, port=req.port,
        drafts=req.drafts, restrict_to_public=True,
    )
    try:
        conn = backend._connect()
        conn.logout()
    except (ConfigurationError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@router.delete("/email", status_code=204)
def delete_email(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
):
    """Disconnect the org's mailbox."""
    clear_email_credentials(db, org.id)
