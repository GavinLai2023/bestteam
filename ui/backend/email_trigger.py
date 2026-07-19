"""Autonomous email trigger: poll each opted-in org's mailbox for new mail and
start that org's deployed email team -- no human prompt.

Design: docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md.
- Dedup is a per-org UID baseline (`EmailTrigger.last_uid`/`uidvalidity`), never
  UNSEEN: the draft-only toolkit deliberately never marks mail seen.
- One run per poll cycle covering all new messages found in it.
- The poll loop lives in the backend process (started from main.py's lifespan)
  and must never die: every org is wrapped in try/except and failures are
  stored on the row (`last_error`) for the UI, then retried next cycle.

This module has NO FastAPI imports; the /api/org/email-trigger router lives in
`email_trigger_api.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Callable, List, Tuple

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from bestteam.exceptions import ConfigurationError
from bestteam.tools.email_client import _ImapBackend

from . import secret_store
from .db.email_credentials import get_email_credentials
from .db.models import EmailTrigger
from .runtime import _executor, registry, run_in_background

_logger = logging.getLogger(__name__)

TRIGGER_USERNAME = "email-trigger"

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY (\d+)")
_UIDNEXT_RE = re.compile(rb"UIDNEXT (\d+)")


def _parse_status(data) -> Tuple[int, int]:
    """Parse `(uidvalidity, max_uid)` out of a STATUS response line.

    RFC 3501 does not guarantee UIDVALIDITY precedes UIDNEXT in the response,
    so the two fields are searched independently rather than with one regex
    that assumes a fixed order.
    """
    line = data[0] if data else b""
    uidvalidity_match = _UIDVALIDITY_RE.search(line or b"")
    uidnext_match = _UIDNEXT_RE.search(line or b"")
    if uidvalidity_match is None or uidnext_match is None:
        raise OSError(f"unexpected INBOX STATUS response: {line!r}")
    uidvalidity, uidnext = int(uidvalidity_match.group(1)), int(uidnext_match.group(1))
    return uidvalidity, uidnext - 1  # UIDNEXT is the *next* UID to be assigned


def mailbox_state(backend) -> Tuple[int, int]:
    """`(uidvalidity, current_max_uid)` via STATUS only -- cheap enable-time
    baseline (no SELECT, no SEARCH, nothing marked seen)."""
    conn = backend._connect()
    try:
        typ, data = conn.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        if typ != "OK":
            raise OSError(f"INBOX STATUS failed: {typ}")
        return _parse_status(data)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass


def check_mailbox(backend, last_uid: int) -> Tuple[int, int, List[int]]:
    """`(uidvalidity, max_uid, new_uids)` where new_uids are strictly above
    `last_uid`, sorted ascending. Read-only throughout."""
    conn = backend._connect()
    try:
        typ, data = conn.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        if typ != "OK":
            raise OSError(f"INBOX STATUS failed: {typ}")
        uidvalidity, max_uid = _parse_status(data)
        if max_uid <= last_uid:
            return uidvalidity, max_uid, []
        conn.select("INBOX", readonly=True)
        typ, search_data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            raise OSError(f"UID SEARCH failed: {typ}")
        raw = search_data[0].split() if search_data and search_data[0] else []
        # IMAP quirk: "N:*" returns the highest-UID message even when N > max,
        # so results at or below the baseline must be filtered out here.
        new_uids = sorted(int(u) for u in raw if int(u) > last_uid)
        return uidvalidity, max_uid, new_uids
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass


POLL_SECONDS_ENV = "BESTTEAM_TRIGGER_POLL_SECONDS"
DAILY_CAP_ENV = "BESTTEAM_TRIGGER_DAILY_CAP"
DISABLED_ENV = "BESTTEAM_TRIGGERS_DISABLED"


def poll_seconds() -> float:
    return float(os.environ.get(POLL_SECONDS_ENV, "").strip() or 120)


def daily_cap() -> int:
    return int(os.environ.get(DAILY_CAP_ENV, "").strip() or 50)


def triggers_disabled() -> bool:
    return os.environ.get(DISABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _utcnow().date().isoformat()


def _friendly_poll_error(exc: Exception) -> str:
    """Customer-visible health message: actionable, never internals.

    `ConfigurationError` messages are already written for humans (e.g. login
    rejected, no mailbox connected); anything else is summarized generically
    -- the real exception goes to the server log.
    """
    if isinstance(exc, ConfigurationError):
        return str(exc)[:500]
    return "Couldn't check the mailbox. We'll keep retrying automatically."


def poll_org(db: Session, trigger: EmailTrigger, get_workflow: Callable) -> None:
    """One org's poll cycle. Never raises; all state changes committed here.

    `get_workflow` is `main._get_workflow` injected by the loop (avoids a
    circular import and lets tests pass a stub): `(name, db, org_id) -> Workflow`.
    """
    # Daily cap: reset on date rollover, and skip the mailbox entirely at cap.
    today = _today()
    if trigger.runs_date != today:
        trigger.runs_today = 0
        trigger.runs_date = today
    if trigger.runs_today >= daily_cap():
        db.commit()
        return

    # Overlap guard: previous triggered run still executing -> skip; new UIDs
    # simply accumulate for the next cycle. The in-process registry is the
    # authority: runs execute in this same process, so a run absent from the
    # registry (e.g. after a hard restart left its DB row stuck "running")
    # cannot actually be executing -- a DB-status check would wedge the
    # trigger forever in that case.
    if trigger.last_run_id:
        prev = registry.get(trigger.last_run_id)
        if prev is not None and prev.status == "running":
            db.commit()
            return

    try:
        cred = get_email_credentials(db, trigger.org_id)
        if cred is None:
            raise ConfigurationError(
                "No mailbox is connected -- reconnect it to resume automatic runs."
            )
        password = secret_store.decrypt(cred.password_encrypted)
        backend = _ImapBackend(
            host=cred.host,
            user=cred.username,
            password=password,
            port=cred.port,
            drafts=cred.drafts_folder,
            restrict_to_public=True,  # customer-supplied host
        )
        uidvalidity, max_uid, new_uids = check_mailbox(backend, trigger.last_uid)
    except (InvalidToken, secret_store.SecretsKeyError) as exc:
        _logger.warning("email trigger: cannot decrypt credentials for org %s: %s",
                        trigger.org_id, exc)
        trigger.last_error = (
            "The mailbox connection can't be read right now -- reconnect it to "
            "resume automatic runs."
        )
        trigger.last_checked_at = _utcnow()
        db.commit()
        return
    except Exception as exc:  # noqa: BLE001 -- a poll failure must never kill the loop
        _logger.warning("email trigger: poll failed for org %s: %s", trigger.org_id, exc)
        trigger.last_error = _friendly_poll_error(exc)
        trigger.last_checked_at = _utcnow()
        db.commit()
        return

    trigger.last_checked_at = _utcnow()
    trigger.last_error = None

    # Mailbox rebuilt/migrated: UIDs are not comparable across validities --
    # re-baseline to now, never reprocess.
    if trigger.uidvalidity is None or trigger.uidvalidity != uidvalidity:
        trigger.uidvalidity = uidvalidity
        trigger.last_uid = max_uid
        db.commit()
        return

    if not new_uids:
        db.commit()
        return

    _start_triggered_run(db, trigger, new_uids, get_workflow)


def _trigger_input(new_uids) -> str:
    ids = ", ".join(str(u) for u in new_uids)
    return (
        f"{len(new_uids)} new email(s) arrived in the inbox (message ids: {ids}). "
        "Read each message by id and triage it, drafting replies where appropriate."
    )


def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow) -> None:
    """Start ONE run covering all of this cycle's new messages.

    `last_uid`/`runs_today` are advanced and committed BEFORE the run starts:
    a crashed run must show as a failure in the activity list, not re-trigger
    forever. No per-user memory (`user_id=None`) -- there is no human user.
    """
    trigger.last_uid = max(new_uids)
    trigger.runs_today += 1
    input_text = _trigger_input(new_uids)
    try:
        workflow = get_workflow(trigger.workflow_name, db, trigger.org_id)
    except Exception as exc:  # noqa: BLE001 -- e.g. team deleted since enabling
        _logger.warning("email trigger: cannot load workflow %r for org %s: %s",
                        trigger.workflow_name, trigger.org_id, exc)
        trigger.last_error = (
            f"Couldn't start the team '{trigger.workflow_name}' -- it may have "
            "been changed or removed. Re-enable automatic runs from its page."
        )
        db.commit()
        return
    run = registry.create(
        trigger.workflow_name, input_text, org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )
    # FK target created later: the `runs` row itself is only inserted by
    # `run_in_background` on the worker thread, after this commit. Harmless
    # today (SQLite FK enforcement is off) -- revisit if PRAGMA foreign_keys
    # is ever enabled.
    trigger.last_run_id = run.id
    try:
        db.commit()
    except Exception:
        # Never leave an orphan "running" registry entry for the overlap
        # guard to trust -- mark it failed, then let poll_once log this.
        registry.publish(run.id, {
            "type": "run_failed",
            "workflow": trigger.workflow_name,
            "data": "The run failed due to an internal error.",
        })
        raise
    _executor.submit(
        run_in_background,
        run.id,
        workflow,
        input_text,
        engine=db.get_bind(),
        org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )


def poll_once(get_workflow: Callable, session_factory=None) -> None:
    """One pass over every enabled org. Runs on a worker thread (imaplib and
    SQLAlchemy here are synchronous); a failure in one org never stops the rest."""
    from .db_session import SessionLocal  # late import: keep module import-light

    from .db.email_triggers import list_enabled_triggers

    factory = session_factory or SessionLocal
    with factory() as db:
        for trigger in list_enabled_triggers(db):
            try:
                poll_org(db, trigger, get_workflow)
            except Exception:  # noqa: BLE001 -- the loop must outlive any org's failure
                _logger.exception("email trigger: unexpected failure for org %s",
                                  trigger.org_id)


async def poll_forever(stop_event: "asyncio.Event", get_workflow: Callable) -> None:
    """The poller task: sleep FIRST, then poll, forever until `stop_event`.

    Sleeping first means app startup (and short-lived TestClient lifespans)
    never trigger an immediate poll against the live database."""
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds())
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        if triggers_disabled():
            continue
        try:
            await asyncio.to_thread(poll_once, get_workflow)
        except Exception:  # noqa: BLE001 -- never let the task die
            _logger.exception("email trigger: poll cycle failed")
