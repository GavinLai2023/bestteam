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
import dataclasses
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Tuple

from cryptography.fernet import InvalidToken
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bestteam.core.loader import _build_workflow
from bestteam.core.trace import TraceEvent
from bestteam.exceptions import ConfigurationError
from bestteam.tools.email_client import _ImapBackend, make_email_tools

from . import secret_store
from .db.email_credentials import get_email_credentials
from .db.email_triggers import get_email_trigger
from .db.models import EmailTrigger, Organization, Run, WorkflowRecord
from .email_tools import spec_uses_email
from .knowledge_bases import (
    contain_workflow_config_for_load,
    ensure_workflow_cache_paths_for_source,
    load_knowledge_base_tools,
)
from .runtime import _executor, registry, run_in_background
from .skills import load_skills

_logger = logging.getLogger(__name__)

_WORKFLOWS_DIR = Path(__file__).resolve().parent / "workflows"

TRIGGER_USERNAME = "email-trigger"

_ERROR_KIND_MAILBOX = "mailbox"
_ERROR_KIND_WORKFLOW = "workflow"

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
BATCH_SIZE_ENV = "BESTTEAM_TRIGGER_BATCH_SIZE"


def poll_seconds() -> float:
    return float(os.environ.get(POLL_SECONDS_ENV, "").strip() or 120)


def daily_cap() -> int:
    return int(os.environ.get(DAILY_CAP_ENV, "").strip() or 50)


def triggers_disabled() -> bool:
    return os.environ.get(DISABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def batch_size() -> int:
    return int(os.environ.get(BATCH_SIZE_ENV, "").strip() or 20)


_MIN_POLL_SECONDS = 5


def validate_trigger_env() -> None:
    """Fail fast at startup on a malformed trigger env var, rather than
    letting the poller task die silently mid-loop later (poll_forever only
    catches asyncio.TimeoutError, so a bad value would otherwise kill
    automatic runs for every org with no supervision or restart).

    `math.isfinite` matters because `float("nan")`/`float("inf")` both parse
    without raising, and both compare `False` to a plain `<= 0` check, so
    they'd otherwise slip through: a nan daily cap makes the cap comparison
    always False (bypasses the safety rail), and an infinite batch size
    crashes `list[:inf]` with a TypeError. `_MIN_POLL_SECONDS` blocks a
    positive-but-absurdly-small interval from hammering the IMAP server in a
    practical tight loop.
    """
    for env_name, getter, minimum in (
        (POLL_SECONDS_ENV, poll_seconds, _MIN_POLL_SECONDS),
        (DAILY_CAP_ENV, daily_cap, 1),
        (BATCH_SIZE_ENV, batch_size, 1),
    ):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            value = getter()
        except ValueError:
            raise RuntimeError(f"{env_name}={raw!r} is not a valid number.") from None
        if not math.isfinite(value) or value < minimum:
            raise RuntimeError(
                f"{env_name}={raw!r} must be a finite number >= {minimum}."
            )


def disable_trigger(db: Session, org_id: int) -> None:
    """Disable an org's trigger if it's currently enabled (mailbox cleared or
    replaced). Shared by the self-service API (org_settings.py) and the
    operator CLI (admin.py) so both mailbox-change paths behave the same."""
    trigger = get_email_trigger(db, org_id)
    if trigger is not None and trigger.enabled:
        trigger.enabled = False
        db.commit()


def disable_trigger_on_identity_change(
    db: Session,
    org_id: int,
    new_host: str,
    new_username: str,
    prior_identity,
) -> None:
    """disable_trigger(...) iff `prior_identity` (a prior (host, username)
    tuple, or None if there was no prior mailbox) differs from the new one. A
    port-only or password-only change is a rotation, not a replacement, and
    leaves the trigger enabled."""
    if prior_identity is not None and prior_identity != (new_host, new_username):
        disable_trigger(db, org_id)


def build_trigger_workflow(name: str, db: Session, org_id: int, allowed_uids, backend):
    """An UNCACHED workflow for (org_id, name) whose email tools are confined to
    `allowed_uids`. Mirrors main._get_workflow's DB-record build but substitutes
    scoped email tools; not cached because the UID set is per-run. `backend` is
    the IMAP backend the caller already resolved for this poll cycle -- reused
    rather than re-fetched, so a mailbox swap mid-cycle can't produce a run
    that detects mail on one mailbox and reads/drafts on another. Raises on a
    missing/invalid team."""
    # Deployed-only gate (same as main._get_workflow): a workflow that isn't
    # deployed must not run, including via the autonomous poller. A non-deployed
    # (or absent) record is treated identically -- no run is built.
    record = (
        db.query(WorkflowRecord)
        .filter_by(name=name, org_id=org_id, status="deployed")
        .one_or_none()
    )
    if record is None:
        raise ValueError(f"No deployed team named '{name}' for org {org_id}")
    # A trigger stays enabled across redeploys -- only a mailbox identity
    # change disables it (disable_trigger_on_identity_change). If the team
    # was redeployed to a version with no email tools/skills, dispatching
    # would consume this cycle's UIDs and daily cap launching an unrelated
    # team with an email-triage prompt, so refuse the same way a missing
    # team does (no build, no state advanced upstream).
    if not spec_uses_email(db, record.config, org_id):
        raise ValueError(f"Deployed team '{name}' for org {org_id} no longer uses email")
    source = _WORKFLOWS_DIR / f"{name}.yaml"
    kb_tools = load_knowledge_base_tools(db, record.config, source, org_id=org_id)
    email_tools = make_email_tools(backend, allowed_uids=allowed_uids)
    skills = load_skills(db, org_id)
    config = contain_workflow_config_for_load(record.config)
    ensure_workflow_cache_paths_for_source(config, source)
    workflow = _build_workflow(
        config,
        source=source,
        extra_tools={**kb_tools, **email_tools},
        extra_skills=skills,
    )
    # Return the version from the SAME record read that produced the config, so
    # the triggered run records exactly the version it executes even if a
    # redeploy commits concurrently (a separate current_version_id re-query could
    # observe a newer version than the one built).
    return workflow, record.current_version_id


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

    `get_workflow` is `build_trigger_workflow` injected by the loop (avoids a
    circular import and lets tests pass a stub):
    `(name, db, org_id, allowed_uids, backend) -> Workflow`.
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
        trigger.last_error_kind = _ERROR_KIND_MAILBOX
        trigger.last_checked_at = _utcnow()
        db.commit()
        return
    except Exception as exc:  # noqa: BLE001 -- a poll failure must never kill the loop
        _logger.warning("email trigger: poll failed for org %s: %s", trigger.org_id, exc)
        trigger.last_error = _friendly_poll_error(exc)
        trigger.last_error_kind = _ERROR_KIND_MAILBOX
        trigger.last_checked_at = _utcnow()
        db.commit()
        return

    trigger.last_checked_at = _utcnow()
    # A successful mailbox check is direct proof connectivity/credentials are
    # fine, so a *mailbox*-kind error can auto-clear here. A *workflow*-kind
    # error (or a legacy/unknown-kind row) must persist across empty polls
    # (F5) -- an empty poll never rebuilds the workflow, so it proves nothing
    # about whether the team still builds. Cleared only on a successful
    # dispatch (below) or on (re-)enable (the API).
    if trigger.last_error_kind == _ERROR_KIND_MAILBOX:
        trigger.last_error = None
        trigger.last_error_kind = None

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

    _start_triggered_run(db, trigger, new_uids, get_workflow, backend)


def _trigger_input(uids) -> str:
    ids = ", ".join(str(u) for u in uids)
    return (
        f"{len(uids)} new email(s) arrived in the inbox (message ids: {ids}). "
        "Read each message by id and triage it, drafting replies where appropriate."
    )


def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow, backend) -> None:
    """Start ONE run over a bounded batch of the detected UIDs.

    Build the workflow FIRST (a build failure must consume no message and no
    cap), then persist a durable run row and advance state in one commit, then
    dispatch. `get_workflow` is `build_trigger_workflow(name, db, org_id,
    allowed_uids, backend) -> (Workflow, Optional[int] version_id)`; the version
    is captured from the same record read that built the config so the run
    records exactly the version it executes.
    """
    batch = sorted(new_uids)[:batch_size()]
    input_text = _trigger_input(batch)
    try:
        workflow, version_id = get_workflow(trigger.workflow_name, db, trigger.org_id, set(batch), backend)
    except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since enabling
        _logger.warning("email trigger: cannot build workflow %r for org %s: %s",
                        trigger.workflow_name, trigger.org_id, exc)
        trigger.last_error = (
            f"Couldn't start the team '{trigger.workflow_name}' -- it may have "
            "been changed or removed. Re-enable automatic runs from its page."
        )
        trigger.last_error_kind = _ERROR_KIND_WORKFLOW
        db.commit()  # NB: last_uid / runs_today deliberately NOT advanced
        return
    run = registry.create(
        trigger.workflow_name, input_text, org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )
    # Durable activity record before dispatch (worker updates its terminal
    # status; run_in_background reuses this row rather than re-inserting).
    run_row = Run(
        id=run.id, workflow=trigger.workflow_name, input=input_text,
        status="running", org_id=trigger.org_id, username=TRIGGER_USERNAME,
        workflow_version_id=version_id,
    )
    # Compare-and-swap: advance the batch/cap and record this run ONLY if the
    # trigger is still enabled. org_settings.py/admin.py disable the trigger in
    # their own commit when the customer/operator disconnects or replaces the
    # mailbox (a replacement disables via disable_trigger_on_identity_change),
    # separate from the credential write that may have landed while this
    # workflow was being built. Guarding the advance and the enabled-check in
    # ONE statement closes the read-then-commit window a separate refresh would
    # leave open: if a disable landed in it, the UPDATE matches no row and we
    # never dispatch against a mailbox they just disconnected/replaced.
    advanced = db.execute(
        update(EmailTrigger)
        .where(
            EmailTrigger.id == trigger.id,
            EmailTrigger.enabled.is_(True),
            # ...and the org is still active. Deactivation can land AFTER trigger
            # enumeration but before this atomic advance; requiring active here
            # (not just at enumeration) is what makes "full suspend" hold for the
            # autonomous path (review r-ext2 #2).
            EmailTrigger.org_id.in_(select(Organization.id).where(Organization.active.is_(True))),
        )
        .values(
            last_uid=max(batch),
            runs_today=EmailTrigger.runs_today + 1,
            last_run_id=run.id,
            last_error=None,  # a run is going out: clear any prior fault
            last_error_kind=None,
        )
    ).rowcount
    if not advanced:
        registry.discard(run.id)
        db.commit()  # persist last_checked_at; batch/cap deliberately NOT advanced
        db.refresh(trigger)
        _logger.info(
            "email trigger: org %s disabled while this cycle's run was building -- discarding",
            trigger.org_id,
        )
        return
    db.add(run_row)
    db.commit()
    db.refresh(trigger)  # resync the ORM object with the values the CAS wrote
    try:
        _executor.submit(
            run_in_background, run.id, workflow, input_text,
            engine=db.get_bind(), org_id=trigger.org_id, username=TRIGGER_USERNAME,
        )
    except Exception:  # noqa: BLE001 -- submission itself must never wedge the trigger
        # The batch/cap were already consumed by the commit above (the same
        # accepted commit-then-crash window already disclosed for a process
        # kill at this point). Without this, both the registry entry and the
        # Run row would stay "running" forever and the overlap guard would
        # block every later poll for this org.
        _logger.exception("email trigger: failed to dispatch run %s for org %s",
                          run.id, trigger.org_id)
        # This batch's UIDs were already advanced past (above, before this
        # try block) -- they will NOT be retried. Say so plainly rather than
        # implying a retry that won't happen.
        message = (
            "Couldn't start the automatic run. It won't be retried, but "
            "automatic runs will resume when new mail arrives."
        )
        registry.publish(run.id, dataclasses.asdict(
            TraceEvent(type="run_failed", workflow=trigger.workflow_name, data=message)
        ))
        run_row.status = "failed"
        run_row.output = message
        trigger.last_error = message
        trigger.last_error_kind = _ERROR_KIND_WORKFLOW
        db.commit()


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
                # Roll back BEFORE touching `trigger` again: a failed flush leaves
                # the session's objects expired, so logging trigger.org_id first
                # would itself raise PendingRollbackError trying to reload it.
                db.rollback()
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
