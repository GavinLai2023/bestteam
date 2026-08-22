"""Org-scoped API for the autonomous email trigger (`/api/org/email-trigger`).

A sibling of `org_settings.py` (same `get_current_org` guard) kept in its own
module. All rejection messages are written for the non-technical customer --
never env-var names, OS codes, or tracebacks (those go to the server log).
"""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import email_filter, secret_store
from .auth_api import get_current_org
from .db.email_credentials import get_email_credentials
from .db.email_triggers import get_email_trigger, upsert_email_trigger
from .db.inbox_events import (
    abandon_superseded_events,
    list_filtered_events,
    mailbox_identity,
    release_filtered_event,
)
from .db.models import EmailTrigger, Organization, PipelineRecord, Run, iso_utc
from .db_session import get_db
from .email_tools import build_backend_for_credential, spec_uses_email
from .email_trigger import daily_cap, mailbox_state, triggers_disabled, TRIGGER_USERNAME, _today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org", tags=["email-trigger"])


class EmailTriggerRequest(BaseModel):
    pipeline_name: str
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
        "pipeline_name": trigger.pipeline_name if trigger is not None else None,
        "status": _status_of(trigger),
        "runs_today": runs_today,
        "daily_cap": daily_cap(),
        "last_checked_at": (
            # SQLite drops tzinfo -- reattach UTC so the browser doesn't parse
            # this as local time.
            trigger.last_checked_at.replace(tzinfo=timezone.utc).isoformat()
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
        db.query(PipelineRecord)
        .filter_by(name=req.pipeline_name, org_id=org.id)
        .one_or_none()
    )
    if record is None or record.status != "deployed":
        raise HTTPException(
            status_code=400,
            detail="That team isn't live yet -- launch it before turning on automatic runs.",
        )
    if not spec_uses_email(
        db,
        record.config,
        org.id,
        pipeline_version_id=record.current_version_id,
    ):
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
        # The shared factory, not a local `_ImapBackend`: it is what honours
        # `auth_type`, and building one here with `password=` offered an M365
        # org's client secret as an IMAP password, so this baseline login
        # always failed and automatic runs could never be turned on.
        backend = build_backend_for_credential(
            cred, secret_store.decrypt(cred.password_encrypted)
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
        db, org.id, pipeline_name=req.pipeline_name, enabled=True,
        last_uid=max_uid, uidvalidity=uidvalidity,
    )
    # This enable just proved the mailbox reachable -- don't let a stale poll
    # failure keep reporting "error" until the next cycle clears it.
    trigger.last_error = None
    trigger.last_error_kind = None
    # The baseline above is what makes a leftover row unclaimable, and this is
    # the only site that knows BOTH the mailbox and its generation. A mailbox
    # replaced or rebuilt while automation was off leaves rows the scoped claim
    # query refuses; `poll_org`'s re-baseline branch cannot retire them either,
    # because the UIDVALIDITY just written here is already the current one.
    # Logged rather than reported: the customer performed this themselves, and
    # the two lines above deliberately clear the field it would be reported on.
    abandoned = abandon_superseded_events(
        db, org_id=org.id,
        mailbox_identity=mailbox_identity(cred.host, cred.username),
        mailbox_generation=str(uidvalidity),
    )
    if abandoned:
        logger.info(
            "email trigger: enable for org %s abandoned %s waiting message(s) "
            "from a previous mailbox or generation", org.id, abandoned,
        )
    db.commit()
    return _payload(trigger)


@router.get("/email-trigger/activity")
def trigger_activity(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """Recent runs for this org (newest first, max 50) from the persisted
    `runs` rows -- so autonomous activity is visible even though full Phase-5
    trace persistence doesn't exist yet."""
    rows = (
        db.query(Run)
        .filter(Run.org_id == org.id, Run.username == TRIGGER_USERNAME)
        .order_by(Run.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "runs": [
            {
                "id": r.id,
                "pipeline": r.pipeline,
                "status": r.status,
                # SQLite drops tzinfo -- reattach UTC so the browser doesn't
                # parse this as local time.
                "started_at": (
                    r.created_at.replace(tzinfo=timezone.utc).isoformat()
                    if r.created_at else None
                ),
                # always True post-filter; kept for the frontend contract.
                "autonomous": r.username == TRIGGER_USERNAME,
            }
            for r in rows
        ]
    }


@router.get("/email-trigger/filtered")
def list_filtered(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Mail the pre-LLM filter skipped, newest first.

    Shown rather than silently dropped because a rule-based filter will have
    false positives, and the cost of one has to be "an admin clicks Release",
    not "the enquiry vanished and nobody knew".
    """
    rows = list_filtered_events(db, org_id=org.id, limit=limit)
    return {
        "filtered": [
            {
                "id": row.id,
                "external_id": row.external_id,
                "decision": row.decision,
                "reason": email_filter.describe(row.decision or ""),
                "detected_at": iso_utc(row.detected_at) if row.detected_at else None,
            }
            for row in rows
        ]
    }


@router.post("/email-trigger/filtered/{event_id}/release")
def release_filtered(
    event_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Hand one skipped message back for normal processing on the next cycle."""
    # 404 rather than 403 for another org's row, and for one already released:
    # the two are indistinguishable to a caller, so probing learns nothing.
    if not release_filtered_event(db, org_id=org.id, event_id=event_id):
        raise HTTPException(status_code=404, detail="No such filtered message.")
    db.commit()
    return {"released": True}
