"""Org-scoped API for the autonomous email trigger (`/api/org/email-trigger`).

A sibling of `org_settings.py` (same `get_current_org` guard) kept in its own
module. All rejection messages are written for the non-technical customer --
never env-var names, OS codes, or tracebacks (those go to the server log).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from bestteam.tools.email_client import _ImapBackend

from . import secret_store
from .auth_api import get_current_org
from .db.email_credentials import get_email_credentials
from .db.email_triggers import get_email_trigger, upsert_email_trigger
from .db.models import EmailTrigger, Organization, WorkflowRecord
from .db_session import get_db
from .email_tools import spec_uses_email
from .email_trigger import daily_cap, mailbox_state, triggers_disabled, _today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org", tags=["email-trigger"])


class EmailTriggerRequest(BaseModel):
    workflow_name: str
    enabled: bool


def _status_of(trigger: EmailTrigger | None) -> str:
    if trigger is None or not trigger.enabled:
        return "off"
    if triggers_disabled():
        return "disabled"
    if trigger.last_error:
        return "error"
    if trigger.runs_date == _today() and trigger.runs_today >= daily_cap():
        return "paused_cap"
    return "active"


def _payload(trigger: EmailTrigger | None) -> Dict[str, Any]:
    runs_today = 0
    if trigger is not None and trigger.runs_date == _today():
        runs_today = trigger.runs_today
    return {
        "enabled": bool(trigger is not None and trigger.enabled),
        "workflow_name": trigger.workflow_name if trigger is not None else None,
        "status": _status_of(trigger),
        "runs_today": runs_today,
        "daily_cap": daily_cap(),
        "last_checked_at": (
            trigger.last_checked_at.isoformat()
            if trigger is not None and trigger.last_checked_at
            else None
        ),
        "last_error": trigger.last_error if trigger is not None else None,
    }


@router.get("/email-trigger")
def get_trigger_status(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    return _payload(get_email_trigger(db, org.id))


@router.put("/email-trigger")
def set_trigger(
    req: EmailTriggerRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    if not req.enabled:
        trigger = get_email_trigger(db, org.id)
        if trigger is not None:
            trigger.enabled = False
            db.commit()
        return _payload(get_email_trigger(db, org.id))

    record = (
        db.query(WorkflowRecord)
        .filter_by(name=req.workflow_name, org_id=org.id)
        .one_or_none()
    )
    if record is None or record.status != "deployed":
        raise HTTPException(
            status_code=400,
            detail="That team isn't live yet -- launch it before turning on automatic runs.",
        )
    if not spec_uses_email(db, record.config, org.id):
        raise HTTPException(
            status_code=400,
            detail="This team doesn't use email, so new mail can't trigger it.",
        )
    cred = get_email_credentials(db, org.id)
    if cred is None:
        raise HTTPException(
            status_code=400,
            detail="Connect your mailbox before turning on automatic runs.",
        )
    try:
        password = secret_store.decrypt(cred.password_encrypted)
        backend = _ImapBackend(
            host=cred.host, user=cred.username, password=password,
            port=cred.port, drafts=cred.drafts_folder, restrict_to_public=True,
        )
        # Baseline = the mailbox's current max UID: the backlog never triggers.
        uidvalidity, max_uid = mailbox_state(backend)
    except Exception as exc:  # noqa: BLE001 -- always a friendly message outward
        logger.warning("email trigger enable: mailbox check failed for org %s: %s",
                       org.id, exc)
        raise HTTPException(
            status_code=400,
            detail=(
                "Couldn't reach your mailbox to start automatic runs. Test your "
                "mailbox connection and try again."
            ),
        ) from exc
    trigger = upsert_email_trigger(
        db, org.id, workflow_name=req.workflow_name, enabled=True,
        last_uid=max_uid, uidvalidity=uidvalidity,
    )
    # This enable just proved the mailbox reachable -- don't let a stale poll
    # failure keep reporting "error" until the next cycle clears it.
    trigger.last_error = None
    db.commit()
    return _payload(trigger)
