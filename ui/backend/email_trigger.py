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
import threading
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
from .automation_results import RESULT_TYPE_BATCH_MARKER, already_drafted_uids, normalize_run_result
from .db.email_credentials import get_email_credentials
from .db.email_triggers import get_email_trigger
from .db.inbox_events import (
    claim_events,
    mailbox_identity,
    mark_dispatched,
    record_events,
    release_events,
    reopen_events,
)
from .db.models import EmailTrigger, Organization, Run, SkillRecord, WorkflowRecord
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

# Shown when a detected message has exhausted its automatic retries. Without
# it a dead-lettered message would be invisible: it is no longer pending, so
# no future poll picks it up, and nothing else reports it.
_DEAD_LETTER_MESSAGE = (
    "Some new mail couldn't be processed after several attempts and has been "
    "set aside -- open the run list for details."
)

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY (\d+)")
_UIDNEXT_RE = re.compile(rb"UIDNEXT (\d+)")

# Per-org dispatch lock: serializes the overlap-guard-check-through-dispatch
# critical section of poll_org and retry_triggered_run against each other.
# Both read `trigger.last_run_id`/the in-process registry, do real work (IMAP
# round-trip, workflow build), and only then write last_run_id -- without a
# lock spanning that whole section, two dispatches racing for the same org
# (an automatic poll cycle and a manual retry, or two near-simultaneous
# manual retries) could both observe "no active run" and both fire against
# the same mailbox, risking duplicate drafts (Codex review finding). One org
# never needs more than one dispatch decision in flight at a time, so this
# does not affect throughput across different orgs.
_dispatch_locks: dict[int, threading.Lock] = {}
_dispatch_locks_guard = threading.Lock()


def _dispatch_lock(org_id: int) -> threading.Lock:
    with _dispatch_locks_guard:
        lock = _dispatch_locks.get(org_id)
        if lock is None:
            lock = threading.Lock()
            _dispatch_locks[org_id] = lock
        return lock


def _current_last_run_id(db: Session, trigger: EmailTrigger):
    """A fresh read of `EmailTrigger.last_run_id`, bypassing whatever value
    `trigger` already had cached from before this dispatch attempt acquired
    `_dispatch_lock`. The lock alone only serializes code execution -- a
    `trigger` ORM object fetched slightly earlier (e.g. poll_once's
    `list_enabled_triggers` scan, or the trigger lookup earlier in
    retry_triggered_run) keeps its already-loaded attribute values even after
    another thread commits a newer one, so the overlap-guard check must
    re-query this one column directly rather than trust `trigger.last_run_id`
    (not `db.refresh(trigger)`, which would also discard this call's own
    pending, not-yet-committed field changes like the daily-cap reset).
    """
    return db.execute(
        select(EmailTrigger.last_run_id).where(EmailTrigger.id == trigger.id)
    ).scalar_one()


def _at_daily_cap(db: Session, trigger: EmailTrigger, today) -> bool:
    """Fresh (uncached) re-check of the daily cap, taken right before dispatch
    while holding `_dispatch_lock` -- same staleness rationale as
    `_current_last_run_id`. The cap check further up (before the lock, before
    the mailbox check/workflow build) is only a fast-path optimization to
    skip unnecessary IMAP calls when obviously already at cap; without this
    second check immediately before the atomic runs_today advance, two
    dispatches that both read "under cap" before either committed its
    increment could both proceed and push the count past the cap (Codex
    review finding). `today` is the caller's already-computed `_today()`, so
    a same-process date rollover is judged consistently with the earlier
    check rather than re-derived here.
    """
    row = db.execute(
        select(EmailTrigger.runs_today, EmailTrigger.runs_date).where(EmailTrigger.id == trigger.id)
    ).one()
    fresh_runs_today = 0 if row.runs_date != today else row.runs_today
    return fresh_runs_today >= daily_cap()


def draft_marker_prefix(mailbox_credential_id, uidvalidity) -> str:
    """The per-mailbox/per-generation prefix stamped on drafts this platform
    creates. Deliberately the same shape as
    `automation_results._source_key`, so a key written into the mailbox and a
    key stored in `automation_item_results` agree by construction rather than
    by convention."""
    return f"mailbox:{mailbox_credential_id}:uidvalidity:{uidvalidity}:uid:"


def _mailbox_drafted_uids(backend, trigger_context, uids) -> set:
    """UIDs the MAILBOX itself still shows a platform-written draft for.

    Defence in depth behind the run's own trace evidence
    (`automation_results.already_drafted_uids`): only the mailbox can reveal a
    draft that was really APPENDed but whose confirming trace event never got
    persisted, because the process was killed between the two.

    Best-effort by contract. Not every IMAP server searches custom headers
    well, and this runs on the retry path -- a scan failure must degrade to
    trace evidence alone, never block a legitimate retry.
    """
    prefix = draft_marker_prefix(
        trigger_context.get("mailbox_credential_id"), trigger_context.get("uidvalidity")
    )
    key_to_uid = {f"{prefix}{uid}": str(uid) for uid in uids}
    if not key_to_uid:
        return set()
    try:
        found = backend.drafts_with_source_keys(list(key_to_uid))
    except Exception as exc:  # noqa: BLE001 -- advisory; must never block a retry
        _logger.warning("email trigger: drafts reconciliation scan failed: %s", exc)
        return set()
    return {key_to_uid[key] for key in found or () if key in key_to_uid}


def _release_stale_run(db: Session, trigger: EmailTrigger, run_id: str) -> bool:
    """True if `run_id` has outlived `run_timeout_seconds()` and has now been
    released, so the overlap guard should stop honouring it.

    A run cannot be forcibly killed -- a node already executing inside
    `workflow.stream()` can't be safely interrupted, which is why
    `registry.request_cancel` is cooperative (see registry.py). So this makes a
    timed-out run non-blocking rather than trying to stop it: request
    cancellation, mark the durable row failed, and record the fault on the
    trigger. Without it, a single hung run closed an org's overlap guard
    permanently and silently -- automation stopped forever with `last_error`
    empty, so nothing in the UI reported a fault (Phase 0, item 0.4).
    """
    run_row = db.get(Run, run_id)
    if run_row is None or run_row.created_at is None:
        return False
    created_at = run_row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if (_utcnow() - created_at).total_seconds() <= run_timeout_seconds():
        return False

    _logger.warning(
        "email trigger: run %s for org %s exceeded the run timeout; releasing the overlap guard",
        run_id, trigger.org_id,
    )
    try:
        registry.request_cancel(run_id)
    except Exception:  # noqa: BLE001 -- releasing the guard matters more
        _logger.exception("email trigger: cancel request failed for stale run %s", run_id)
    message = (
        "The previous automatic run didn't finish in time and was stopped. "
        "Automatic runs have resumed."
    )
    if run_row.status == "running":
        run_row.status = "failed"
        run_row.output = message
    trigger.last_error = message
    trigger.last_error_kind = _ERROR_KIND_WORKFLOW
    # Infrastructure-class: the run hung, the messages are innocent. Hand them
    # back so they are reprocessed rather than consumed by a wedged run. This
    # path never reaches runtime's completion hook (the worker produced no
    # terminal event), so releasing here is the only thing that frees them.
    if release_events(db, run_id, max_attempts=max_event_attempts(), error=message):
        trigger.last_error = _DEAD_LETTER_MESSAGE
    db.commit()
    # The worker never reached a terminal event, so it never normalized this
    # run either -- without this a declared batch would be marked failed with
    # zero automation_item_results rows and vanish from Needs-attention, the
    # same gap the dispatch-failure branches close.
    normalize_run_result(db, run_row)
    return True


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
RUN_TIMEOUT_ENV = "BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS"
MAX_EVENT_ATTEMPTS_ENV = "BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS"


def poll_seconds() -> float:
    return float(os.environ.get(POLL_SECONDS_ENV, "").strip() or 120)


def daily_cap() -> int:
    return int(os.environ.get(DAILY_CAP_ENV, "").strip() or 50)


def triggers_disabled() -> bool:
    return os.environ.get(DISABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def batch_size() -> int:
    return int(os.environ.get(BATCH_SIZE_ENV, "").strip() or 20)


def run_timeout_seconds() -> float:
    """How long a triggered run may stay `running` before the overlap guard
    stops honouring it. Default 30 minutes -- comfortably longer than any
    realistic multi-agent email batch, short enough that a hung run doesn't
    cost an org a day of automation."""
    return float(os.environ.get(RUN_TIMEOUT_ENV, "").strip() or 1800)


def max_event_attempts() -> int:
    """How many times one detected message may be handed to a run before it is
    dead-lettered. Only infrastructure-class failures (crash, dispatch failure,
    watchdog timeout) consume an attempt -- a message that reaches the model and
    fails is terminal immediately and waits for a human retry."""
    return int(os.environ.get(MAX_EVENT_ATTEMPTS_ENV, "").strip() or 3)


# How much of a backlog one detection cycle may record. Bounds the transaction
# after a long outage without changing steady-state behaviour: `new_uids` is
# sorted ascending, so slicing keeps the oldest and the rest are picked up on
# the next cycle.
_DETECT_MULTIPLIER = 10

_MIN_POLL_SECONDS = 5
_MIN_RUN_TIMEOUT_SECONDS = 60


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
        (RUN_TIMEOUT_ENV, run_timeout_seconds, _MIN_RUN_TIMEOUT_SECONDS),
        (MAX_EVENT_ATTEMPTS_ENV, max_event_attempts, 1),
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
    # Stamp every draft this run creates with a deterministic source key, so a
    # later retry can reconcile against the mailbox itself and recognise a
    # draft that was really written even if the run died before recording it.
    cred = get_email_credentials(db, org_id)
    trigger = get_email_trigger(db, org_id)
    marker_prefix = (
        None
        if cred is None or trigger is None
        else draft_marker_prefix(cred.id, trigger.uidvalidity)
    )
    email_tools = make_email_tools(
        backend, allowed_uids=allowed_uids, draft_marker_prefix=marker_prefix
    )
    skills = load_skills(
        db, org_id, workflow_version_id=record.current_version_id
    )
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


# The platform skill whose system prompt commits a workflow's Response agent to
# emitting automation_results.RESULT_TYPE_BATCH_MARKER's JSON envelope (see
# skills.py / ui/backend/workflows/property_maintenance_inbox_demo.yaml). Used
# only to stamp `trigger_context["result_contract"]` below -- advisory metadata,
# never anything build_trigger_workflow's actual run depends on.
_PROPERTY_MAINTENANCE_RESPONSE_SKILL = "property_maintenance_response_v1"


def _declares_property_maintenance_contract(db: Session, org_id: int, workflow_name: str) -> bool:
    """Best-effort: does this deployed workflow's config give any agent the
    ACTUAL platform `property_maintenance_response_v1` skill?

    A run's own trace/output can't tell us this after the fact for a run that
    crashed before producing any JSON (`_normalize` can then only see a plain
    failure string, indistinguishable from any other org's unrelated
    email-trigger workflow output) -- so this is captured up front, from the
    workflow config itself, and stamped into `trigger_context` at dispatch
    time (below). That lets `automation_results._normalize` still synthesize
    the spec-required per-UID error rows for an envelope-less *maintenance-
    inbox* run, without also doing so for a crashed *unrelated* org's
    email-trigger workflow that never declared this skill (Codex review
    finding). A read failure here must never block dispatch -- this is
    advisory only.

    A name match alone isn't enough: `load_skills` intentionally lets an
    org's own skill shadow a same-named platform built-in, so an org that
    happens to name (or repurpose) its own skill
    `property_maintenance_response_v1` would otherwise get its unrelated
    runs wrongly redacted and stamped with synthetic maintenance error rows
    (Codex review finding). `_resolves_to_platform_skill` re-applies
    `load_skills`' own shadowing precedence to confirm the name still
    resolves to the platform-tier row.
    """
    try:
        record = (
            db.query(WorkflowRecord)
            .filter_by(name=workflow_name, org_id=org_id, status="deployed")
            .one_or_none()
        )
        if record is None:
            return False
        agents = (record.config or {}).get("agents") or []
        declares_by_name = any(
            _PROPERTY_MAINTENANCE_RESPONSE_SKILL in (agent.get("skills") or [])
            for agent in agents
        )
        return declares_by_name and _resolves_to_platform_skill(
            db, org_id, _PROPERTY_MAINTENANCE_RESPONSE_SKILL
        )
    except Exception:  # noqa: BLE001 -- advisory only, must never block dispatch
        return False


def _resolves_to_platform_skill(db: Session, org_id: int, skill_name: str) -> bool:
    """True iff `skill_name`, resolved for `org_id` with the same
    org-shadows-platform precedence `load_skills` uses, is actually the
    platform-tier (`org_id IS NULL`) skill -- not an org skill of the same
    name."""
    org_record = db.query(SkillRecord).filter_by(name=skill_name, org_id=org_id).one_or_none()
    if org_record is not None:
        return False
    platform_record = db.query(SkillRecord).filter_by(name=skill_name, org_id=None).one_or_none()
    return platform_record is not None


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
    #
    # Held for this whole section, through the eventual _start_triggered_run
    # dispatch below: the guard's check-then-act isn't atomic on its own, so
    # without a lock spanning both the check and the dispatch, this poll cycle
    # could race a concurrent retry_triggered_run call for the same org and
    # both observe "no active run" (Codex review finding; see _dispatch_lock).
    with _dispatch_lock(trigger.org_id):
        last_run_id = _current_last_run_id(db, trigger)
        if last_run_id:
            prev = registry.get(last_run_id)
            if prev is not None and prev.status == "running":
                # ...unless it has hung past the run timeout, in which case it
                # is released rather than allowed to wedge this org forever.
                if not _release_stale_run(db, trigger, last_run_id):
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

        # THE durability point. Recording the work and advancing the cursor in
        # ONE commit is what stops a process kill from consuming mail that
        # nothing ran: before this, `_start_triggered_run` advanced `last_uid`
        # and only then handed the workflow to a thread pool, and a kill in
        # between lost the batch for good.
        detected = sorted(new_uids)[: batch_size() * _DETECT_MULTIPLIER]
        record_events(
            db,
            org_id=trigger.org_id,
            mailbox_identity=mailbox_identity(cred.host, cred.username),
            mailbox_generation=str(trigger.uidvalidity),
            external_ids=[str(u) for u in detected],
        )
        trigger.last_uid = max(detected)
        db.commit()

        if _at_daily_cap(db, trigger, today):
            db.commit()  # persist last_checked_at / error-clearing above; no dispatch
            return

        _start_triggered_run(db, trigger, get_workflow, backend, cred)


def _trigger_input(uids) -> str:
    ids = ", ".join(str(u) for u in uids)
    return (
        f"{len(uids)} new email(s) arrived in the inbox (message ids: {ids}). "
        "Read each message by id and triage it, drafting replies where appropriate."
    )


def _start_triggered_run(
    db: Session, trigger: EmailTrigger, get_workflow, backend, cred
) -> None:
    """Start ONE run over a bounded batch of this org's pending inbox events.

    The batch is whatever this run CLAIMS from the durable ledger, not whatever
    the poller happened to detect this cycle -- batching is a claim policy now,
    not a coupling. The claim is committed before any workflow build is
    attempted, so a build failure releases the messages (penalty-free: a broken
    team config is not the message's fault) rather than consuming them.

    Then persist a durable run row and advance state in one commit, then
    dispatch. `get_workflow` is `build_trigger_workflow(name, db, org_id,
    allowed_uids, backend) -> (Workflow, Optional[int] version_id)`; the version
    is captured from the same record read that built the config so the run
    records exactly the version it executes.

    `cred` (the org's `OrgEmailCredential` row) is stamped into the run's
    `trigger_context` (below) so a later server-side result normalization or
    manual retry can reconstruct exactly which mailbox/UIDVALIDITY/UID batch
    this run covers, without trusting anything the model itself claims to
    have processed -- see `automation_results.py` and `docs/superpowers/specs/
    2026-08-02-property-maintenance-inbox-phase-1-development-plan.md` section 11.1.
    `mailbox_host`/`mailbox_username` (not just the row id) are stamped too:
    `set_email_credentials` upserts one row per org, so the row id never
    changes even when the customer replaces the mailbox entirely -- host/
    username are what `retry_triggered_run` actually needs to detect that.
    """
    # The run id has to exist before the claim can stamp it, so the registry
    # entry is created first and discarded on any path that doesn't dispatch
    # (the same create-then-discard shape the disabled-mid-build branch below
    # already used).
    run = registry.create(
        trigger.workflow_name, "", org_id=trigger.org_id, username=TRIGGER_USERNAME,
    )
    claimed = claim_events(db, org_id=trigger.org_id, run_id=run.id, limit=batch_size())
    if not claimed:
        registry.discard(run.id)
        db.commit()
        return
    db.commit()  # the claim is durable before any workflow build is attempted
    batch = [int(e.external_id) for e in claimed]
    input_text = _trigger_input(batch)
    run.input = input_text
    trigger_context = {
        "trigger_type": "email",
        "mailbox_credential_id": cred.id,
        "mailbox_host": cred.host,
        "mailbox_username": cred.username,
        "uidvalidity": trigger.uidvalidity,
        "uids": batch,
        "folder": "INBOX",
        "triggered_at": _utcnow().isoformat(),
    }
    if _declares_property_maintenance_contract(db, trigger.org_id, trigger.workflow_name):
        trigger_context["result_contract"] = RESULT_TYPE_BATCH_MARKER
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
        # Penalty-free release: the workflow is broken, not the messages. No
        # attempt was charged (that happens at dispatch), so these retry until
        # the customer fixes the team -- which is today's behaviour, and the
        # reason attempts are not charged at claim time.
        release_events(db, run.id, max_attempts=max_event_attempts(), error=None)
        registry.discard(run.id)
        db.commit()  # NB: runs_today deliberately NOT advanced
        return
    # Durable activity record before dispatch (worker updates its terminal
    # status; run_in_background reuses this row rather than re-inserting).
    run_row = Run(
        id=run.id, workflow=trigger.workflow_name, input=input_text,
        status="running", org_id=trigger.org_id, username=TRIGGER_USERNAME,
        workflow_version_id=version_id, trigger_context=trigger_context,
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
            # NB: last_uid is NOT written here any more. Detection already
            # advanced it, past everything it recorded -- which is a superset of
            # this run's claimed batch, so writing max(batch) would regress the
            # cursor and re-detect (harmlessly, but pointlessly) the remainder.
            runs_today=EmailTrigger.runs_today + 1,
            last_run_id=run.id,
            last_error=None,  # a run is going out: clear any prior fault
            last_error_kind=None,
        )
    ).rowcount
    if not advanced:
        # Penalty-free release, same reasoning as the build-failure branch: the
        # customer disconnected the mailbox or the org was suspended, which says
        # nothing about these messages. They stay queued for whenever the
        # trigger is re-enabled.
        release_events(db, run.id, max_attempts=max_event_attempts(), error=None)
        registry.discard(run.id)
        db.commit()  # persist last_checked_at; cap deliberately NOT advanced
        db.refresh(trigger)
        _logger.info(
            "email trigger: org %s disabled while this cycle's run was building -- discarding",
            trigger.org_id,
        )
        return
    db.add(run_row)
    mark_dispatched(db, run.id)
    db.commit()
    db.refresh(trigger)  # resync the ORM object with the values the CAS wrote
    try:
        _executor.submit(
            run_in_background, run.id, workflow, input_text,
            engine=db.get_bind(), org_id=trigger.org_id, username=TRIGGER_USERNAME,
        )
    except Exception:  # noqa: BLE001 -- submission itself must never wedge the trigger
        # The cap was already consumed by the commit above. Without this, both
        # the registry entry and the Run row would stay "running" forever and
        # the overlap guard would block every later poll for this org.
        _logger.exception("email trigger: failed to dispatch run %s for org %s",
                          run.id, trigger.org_id)
        message = (
            "Couldn't start the automatic run. The affected mail will be "
            "picked up again on a later check."
        )
        run_row.status = "failed"
        run_row.output = message
        trigger.last_error = message
        trigger.last_error_kind = _ERROR_KIND_WORKFLOW
        # Infrastructure-class: nothing reached the model, so the messages go
        # back for reprocessing. An attempt WAS charged above, so a mailbox that
        # can never be dispatched dead-letters rather than looping forever --
        # and the customer is told, since nothing else would surface it.
        if release_events(db, run.id, max_attempts=max_event_attempts(), error=message):
            trigger.last_error = _DEAD_LETTER_MESSAGE
        db.commit()
        # The worker never started, so run_in_background's own normalization
        # never runs either -- without this, a declared property-maintenance
        # batch would be marked failed with no automation_item_results rows
        # at all and silently vanish from Needs-attention (Codex review
        # finding). Normalize BEFORE publishing run_failed -- a live Run
        # Detail view can react to the terminal event immediately and would
        # otherwise fetch zero automation rows before this commits them,
        # with no later terminal transition to prompt a re-fetch (Codex
        # review finding).
        normalize_run_result(db, run_row)
        registry.publish(run.id, dataclasses.asdict(
            TraceEvent(type="run_failed", workflow=trigger.workflow_name, data=message)
        ))


class RetryError(Exception):
    """A customer-facing reason a triggered run can't be retried right now.
    `main.py`'s `POST /api/runs/{id}/retry` maps this to a 400/409."""


def retry_triggered_run(db: Session, run_row: Run) -> str:
    """Rebuild and dispatch a NEW run over the exact UID batch `run_row`
    originally covered (spec section 11.2). Never mutates `run_row` itself --
    history stays immutable; the new run records `retry_of_run_id`.

    Revalidates before dispatch: the original run must have actually failed
    (a `completed` run may already have real mailbox side effects -- e.g.
    drafts saved -- so it is never eligible, only ever `failed`), the current
    mailbox must still be the same one (host/username, not just its
    UIDVALIDITY -- a replaced mailbox that coincidentally shares a UIDVALIDITY
    value would otherwise pass), the mailbox credential must still work, the
    workflow must still build, and the org's daily automatic-run cap must not
    already be hit. Raises `RetryError` with a customer-facing message for any
    ineligibility; returns the new run id on successful dispatch.
    """
    trigger_context = run_row.trigger_context
    if not trigger_context or trigger_context.get("trigger_type") != "email":
        raise RetryError("This run has no recorded email batch to retry.")
    if run_row.status == "running":
        raise RetryError("This run is still in progress.")
    if run_row.status != "failed":
        # Anything other than a failed run (completed, cancelled, ...) may
        # already have real mailbox side effects for this batch -- retrying it
        # risks duplicate drafts. Only a failed run is safe to redo.
        raise RetryError("Only a failed run can be retried.")
    # Fast-path only, both checks below: skip the mailbox connectivity check
    # and workflow rebuild entirely for the common case of an obviously
    # ineligible retry. Neither is the authoritative gate -- both are
    # re-checked fresh, while holding the per-org dispatch lock, immediately
    # before the actual dispatch further down (Codex review finding: a
    # concurrent second retry racing this one could otherwise pass both
    # checks here on data that predates the first retry's own
    # dispatch/normalization).
    if (
        db.query(Run)
        .filter(Run.retry_of_run_id == run_row.id, Run.status == "running")
        .first()
        is not None
    ):
        raise RetryError("A retry of this run is already in progress.")

    # A failed run can still have drafted replies for some UIDs before a
    # later message or tool failure ended it -- email_draft_reply has no
    # dedup, so blindly resubmitting the whole original batch would create a
    # second draft for each of those. Exclude anything the original run's own
    # normalized results already confirm a draft for.
    already_drafted = already_drafted_uids(db, run_row)
    retry_uids = [u for u in (trigger_context.get("uids") or []) if str(u) not in already_drafted]
    if not retry_uids:
        raise RetryError("Every message in this batch already has a confirmed draft -- nothing left to retry.")

    org_id = run_row.org_id
    cred = get_email_credentials(db, org_id)
    if cred is None:
        raise RetryError("Connect a mailbox before retrying.")
    if (
        cred.host != trigger_context.get("mailbox_host")
        or cred.username != trigger_context.get("mailbox_username")
    ):
        raise RetryError(
            "The connected mailbox has changed since this run -- this batch can no longer be safely retried."
        )
    try:
        password = secret_store.decrypt(cred.password_encrypted)
        backend = _ImapBackend(
            host=cred.host, user=cred.username, password=password,
            port=cred.port, drafts=cred.drafts_folder, restrict_to_public=True,
        )
        uidvalidity, _max_uid = mailbox_state(backend)
    except (InvalidToken, secret_store.SecretsKeyError) as exc:
        raise RetryError("The mailbox connection can't be read right now -- reconnect it and try again.") from exc
    except Exception as exc:  # noqa: BLE001 -- always a friendly message outward
        raise RetryError("Couldn't reach the mailbox to verify it before retrying.") from exc

    if uidvalidity != trigger_context.get("uidvalidity"):
        raise RetryError(
            "The mailbox has changed since this run (its UIDVALIDITY no longer matches) "
            "-- this batch can no longer be safely retried."
        )

    trigger = get_email_trigger(db, org_id)
    if trigger is None:
        raise RetryError("No automatic-run configuration exists for this team anymore.")
    today = _today()
    if trigger.runs_date != today:
        trigger.runs_today = 0
        trigger.runs_date = today
    if trigger.runs_today >= daily_cap():
        db.commit()
        raise RetryError("Today's automatic-run limit has been reached -- try again tomorrow.")

    # Same overlap guard poll_org enforces -- held for this whole section,
    # through the eventual dispatch below, via the same per-org lock poll_org
    # uses: the check-then-act here isn't atomic on its own, so without a lock
    # spanning both the check and the last_run_id write, this retry could race
    # a concurrent poll cycle (or another concurrent retry) for the same org
    # and both observe "no active run", both dispatch, and both create
    # mailbox drafts while only one is represented by the guard afterward
    # (Codex review finding; see _dispatch_lock).
    with _dispatch_lock(org_id):
        last_run_id = _current_last_run_id(db, trigger)
        if last_run_id:
            prev = registry.get(last_run_id)
            if prev is not None and prev.status == "running":
                # Same watchdog as poll_org's guard: a run hung past the
                # timeout must not make manual retry permanently unavailable.
                if not _release_stale_run(db, trigger, last_run_id):
                    db.commit()
                    raise RetryError(
                        "A run against this mailbox is already in progress -- try again once it finishes."
                    )

        if _at_daily_cap(db, trigger, today):
            # The check further up is only a fast-path (skip mailbox/workflow
            # work when obviously already at cap); this fresh re-check, taken
            # while holding the lock right before dispatch, is what actually
            # closes the race -- two dispatches for this org that both read
            # "under cap" before either committed its increment could
            # otherwise both pass and push the count past the cap (Codex
            # review finding).
            db.commit()
            raise RetryError("Today's automatic-run limit has been reached -- try again tomorrow.")

        # Re-check "retry already running" and already-drafted freshly, now
        # that this call holds the per-org dispatch lock -- the equivalent
        # checks further up (before mailbox I/O) are only a fast-path and can
        # be stale by the time execution reaches here: a second concurrent
        # retry of the SAME run could pass both on data that predates the
        # first retry's dispatch/normalization, and since email_draft_reply
        # has no dedup, both would then create a duplicate draft (Codex
        # review finding).
        if (
            db.query(Run)
            .filter(Run.retry_of_run_id == run_row.id, Run.status == "running")
            .first()
            is not None
        ):
            db.commit()
            raise RetryError("A retry of this run is already in progress.")

        # Union of both evidence sources: the run family's own records (result
        # rows plus persisted trace events) and, as defence in depth, the
        # mailbox itself -- the only place a draft that was APPENDed but never
        # recorded can still be seen (Phase 0, items 0.1/0.2).
        already_drafted = set(already_drafted_uids(db, run_row))
        candidate_uids = [u for u in (trigger_context.get("uids") or []) if str(u) not in already_drafted]
        already_drafted |= _mailbox_drafted_uids(backend, trigger_context, candidate_uids)
        retry_uids = [u for u in (trigger_context.get("uids") or []) if str(u) not in already_drafted]
        if not retry_uids:
            db.commit()
            raise RetryError("Every message in this batch already has a confirmed draft -- nothing left to retry.")
        input_text = _trigger_input(retry_uids)

        try:
            workflow, version_id = build_trigger_workflow(run_row.workflow, db, org_id, set(retry_uids), backend)
        except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since the original run
            raise RetryError(
                f"Couldn't rebuild the team '{run_row.workflow}' -- it may have been changed or removed."
            ) from exc

        new_run = registry.create(run_row.workflow, input_text, org_id=org_id, username=TRIGGER_USERNAME)
        # Narrowed to retry_uids (not the original full batch): a UID already
        # confirmed drafted is excluded from what this new run is even allowed to
        # touch (build_trigger_workflow's allowed_uids above), and its
        # trigger_context must agree, or normalize_run_result would treat that
        # already-handled UID as "missing" from this run's envelope and wrongly
        # synthesize a needs_attention error row for it under the new run id.
        new_trigger_context = {**trigger_context, "uids": retry_uids, "triggered_at": _utcnow().isoformat()}
        # Recompute, don't just carry over: the original run's result_contract
        # can be stale by the time it's retried if the deployed workflow (or a
        # same-named skill) changed in between -- carrying it over verbatim
        # would either leave a now-maintenance workflow's output unredacted or
        # wrongly redact/normalize a workflow that no longer declares the
        # contract (Codex review finding).
        if _declares_property_maintenance_contract(db, org_id, run_row.workflow):
            new_trigger_context["result_contract"] = RESULT_TYPE_BATCH_MARKER
        else:
            new_trigger_context.pop("result_contract", None)
        new_row = Run(
            id=new_run.id, workflow=run_row.workflow, input=input_text,
            status="running", org_id=org_id, username=TRIGGER_USERNAME,
            workflow_version_id=version_id, trigger_context=new_trigger_context,
            retry_of_run_id=run_row.id,
        )
        db.add(new_row)
        # Atomic SQL-level advance (mirrors _start_triggered_run's CAS), not a
        # Python-level `trigger.runs_today += 1` -- the latter reads whatever
        # value this call's own `trigger` object was loaded with (before the
        # lock) and would silently lose a concurrent dispatch's increment for
        # the same org instead of just racing the cap check. Also requires
        # the trigger to still be enabled and the org still active, same as
        # _start_triggered_run's own CAS -- without this guard, a customer
        # disconnecting/replacing the mailbox (or an operator deactivating
        # the org) during this call's own pre-lock credential/mailbox check
        # would go undetected, and this retry would dispatch a real
        # email_draft_reply against a mailbox the customer already
        # disconnected (Codex review finding).
        advanced = db.execute(
            update(EmailTrigger)
            .where(
                EmailTrigger.id == trigger.id,
                EmailTrigger.enabled.is_(True),
                EmailTrigger.org_id.in_(select(Organization.id).where(Organization.active.is_(True))),
            )
            .values(
                runs_today=EmailTrigger.runs_today + 1,
                # Register with the same overlap guard poll_org checks
                # (trigger.last_run_id) -- otherwise an automatic poll cycle
                # running concurrently with this retry has no way to know a
                # run against this mailbox is already in flight.
                last_run_id=new_run.id,
                # A run is going out: clear any prior fault, same as
                # _start_triggered_run's atomic advance -- otherwise a sticky
                # workflow-kind error from a past failure keeps reporting a
                # failure indefinitely despite this successful dispatch
                # (Codex review finding).
                last_error=None,
                last_error_kind=None,
            )
        ).rowcount
        if not advanced:
            registry.discard(new_run.id)
            db.rollback()
            raise RetryError(
                "Automatic runs were turned off for this mailbox while the retry was being "
                "prepared -- reconnect it and try again."
            )
        db.commit()
        try:
            _executor.submit(
                run_in_background, new_run.id, workflow, input_text,
                engine=db.get_bind(), org_id=org_id, username=TRIGGER_USERNAME,
            )
        except Exception:  # noqa: BLE001 -- submission itself must never raise out of this call
            _logger.exception("email trigger retry: failed to dispatch run %s for org %s", new_run.id, org_id)
            message = "Couldn't start the retry. Try again."
            new_row.status = "failed"
            new_row.output = message
            db.commit()
            # Same rationale as _start_triggered_run's analogous branch: the
            # worker never started, so run_in_background never normalizes
            # this run either (Codex review finding). Normalize before
            # publishing run_failed, same ordering fix as that branch (Codex
            # review finding).
            normalize_run_result(db, new_row)
            registry.publish(new_run.id, dataclasses.asdict(
                TraceEvent(type="run_failed", workflow=run_row.workflow, data=message)
            ))
        return new_run.id


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
