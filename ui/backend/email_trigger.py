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

import logging
import os
import re
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from bestteam.exceptions import ConfigurationError
from bestteam.tools.email_client import _ImapBackend

from . import secret_store
from .db.email_credentials import get_email_credentials
from .db.models import EmailTrigger, Run

_logger = logging.getLogger(__name__)

TRIGGER_USERNAME = "email-trigger"

_STATUS_RE = re.compile(rb"UIDVALIDITY (\d+) UIDNEXT (\d+)")


def _parse_status(data) -> Tuple[int, int]:
    """Parse `(uidvalidity, max_uid)` out of a STATUS response line."""
    line = data[0] if data else b""
    match = _STATUS_RE.search(line or b"")
    if match is None:
        raise OSError(f"unexpected INBOX STATUS response: {line!r}")
    uidvalidity, uidnext = int(match.group(1)), int(match.group(2))
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
    # simply accumulate for the next cycle.
    if trigger.last_run_id:
        prev = db.get(Run, trigger.last_run_id)
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


def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow) -> None:
    raise NotImplementedError  # implemented in the next commit
