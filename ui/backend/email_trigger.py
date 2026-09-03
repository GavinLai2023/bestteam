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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from cryptography.fernet import InvalidToken
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bestteam.core.loader import _build_pipeline
from bestteam.core.trace import TraceEvent
from bestteam.exceptions import ConfigurationError
from bestteam.tools.email_client import make_email_tools

from . import draft_outcomes, email_budget, email_filter, secret_store, trigger_health, trigger_metrics
from .automation_results import RESULT_TYPE_BATCH_MARKER, already_drafted_uids, normalize_run_result
from .db.email_budget_settings import get_budget_caps, spent_this_month
from .db.email_credentials import AUTH_MICROSOFT_OAUTH, get_email_credentials
from .db.email_filter_settings import get_filter_settings
from .db.email_triggers import get_email_trigger
from .db.notifications import create_notification, has_fingerprint
from .db.inbox_events import (
    abandon_superseded_events,
    claim_events,
    has_pending_events,
    mailbox_identity,
    mark_dispatched,
    record_events,
    release_events,
    resolve_retry_events,
)
from .db.models import (
    EmailTrigger,
    Organization,
    OrgEmailCredential,
    Run,
    SkillRecord,
    PipelineRecord,
)
from .email_tools import build_backend_for_credential, spec_uses_email
from .knowledge_bases import (
    contain_pipeline_config_for_load,
    ensure_pipeline_cache_paths_for_source,
    load_knowledge_base_tools,
)
from .notifications import dispatch_pending
from .retention import sweep_retention
from .runtime import _executor, registry, run_in_background
from .skills import load_skills

_logger = logging.getLogger(__name__)

_PIPELINES_DIR = Path(__file__).resolve().parent / "pipelines"

TRIGGER_USERNAME = "email-trigger"

_ERROR_KIND_MAILBOX = "mailbox"
# Identifier renamed Workflow -> Pipeline; the stored VALUE stays "workflow"
# on purpose -- it's compared against EmailTrigger.last_error_kind rows an
# older app version may have written, and changing it would need a backfill
# purely for cosmetic consistency. See db/models.py's matching note.
_ERROR_KIND_PIPELINE = "workflow"

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
# round-trip, pipeline build), and only then write last_run_id -- without a
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
    the mailbox check/pipeline build) is only a fast-path optimization to
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


def _apply_health(db, trigger, outcome: str) -> None:
    """Fold one outcome into the trigger's alert state, raising a notification
    if the evaluator says this transition is worth telling someone about.

    Deliberately additive: every existing `last_error`/`last_error_kind` write
    stays exactly where it was. Those drive the dashboard's error surface and
    are pinned by Phase 0's tests; this adds the *telling someone* half.

    Isolated like the other health writes -- alerting must never be the reason
    a poll cycle or a run fails.
    """
    try:
        decision = trigger_health.evaluate(
            outcome=outcome,
            consecutive_faults=trigger.consecutive_faults or 0,
            alerted_fingerprint=trigger.alerted_fingerprint,
            threshold=trigger_health.alert_threshold(),
        )
        trigger.consecutive_faults = decision.consecutive_faults
        trigger.alerted_fingerprint = decision.alerted_fingerprint
        if decision.notification is not None:
            draft = decision.notification
            create_notification(
                db, org_id=trigger.org_id, kind=draft.kind, severity=draft.severity,
                title=draft.title, body=draft.body, fingerprint=draft.fingerprint,
            )
    except Exception:  # noqa: BLE001 -- alerting must never break the caller
        _logger.exception(
            "email trigger: health evaluation failed for org %s", trigger.org_id
        )


def _apply_backlog_health(db, trigger) -> None:
    """Fold the org's backlog level into the trigger's alert state.

    Runs once per poll cycle, after `poll_org`: dispatch pausing (a daily or
    budget cap, the overlap guard) leaves events `pending` without any run
    ever *failing*, so no `_apply_health` outcome would ever fire for it.
    Isolated like `_apply_health` -- alerting must never break the cycle.
    """
    try:
        decision = trigger_health.evaluate_backlog(
            oldest_pending_seconds=trigger_metrics.oldest_pending_seconds(
                db, trigger.org_id, _utcnow()
            ),
            threshold_seconds=trigger_metrics.backlog_alert_seconds(),
            alerted_fingerprint=trigger.alerted_fingerprint,
        )
        if decision.alerted_fingerprint == trigger.alerted_fingerprint:
            return
        trigger.alerted_fingerprint = decision.alerted_fingerprint
        if decision.notification is not None:
            draft = decision.notification
            create_notification(
                db, org_id=trigger.org_id, kind=draft.kind, severity=draft.severity,
                title=draft.title, body=draft.body, fingerprint=draft.fingerprint,
            )
        db.commit()
    except Exception:  # noqa: BLE001 -- alerting must never break the caller
        db.rollback()
        _logger.exception(
            "email trigger: backlog evaluation failed for org %s", trigger.org_id
        )


def _release_stale_run(db: Session, trigger: EmailTrigger, run_id: str) -> bool:
    """True if `run_id` has outlived `run_timeout_seconds()` and has now been
    released, so the overlap guard should stop honouring it.

    A run cannot be forcibly killed -- a node already executing inside
    `pipeline.stream()` can't be safely interrupted, which is why
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
    trigger.last_error_kind = _ERROR_KIND_PIPELINE
    _apply_health(db, trigger, trigger_health.OUTCOME_TIMEOUT)
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


def on_mailbox_saved(
    db: Session,
    org_id: int,
    new_host: str,
    new_username: str,
    prior_identity,
) -> None:
    """The one post-save hook for an org's mailbox, called on EVERY save.

    disable_trigger(...) iff `prior_identity` (a prior (host, username) tuple,
    or None if there was no prior mailbox) differs from the new one. A
    port-only or password-only change is a rotation, not a replacement, and
    leaves the trigger enabled.

    Abandonment, however, runs unconditionally, because "the identity changed"
    is not the same question as "is anything waiting that this mailbox cannot
    claim". A customer who disconnects and then connects a different mailbox
    arrives here with `prior_identity=None` -- the credential row is gone --
    and their old backlog would survive a check on the identity alone. The
    helper is expressed as "everything that is not the current mailbox", so
    calling it always is a no-op for a rotation and self-correcting for a
    change that was somehow missed. No generation is passed: the new mailbox's
    UIDVALIDITY is not known until the enable or the first poll.

    This site only logs; the customer performed the change themselves and the
    trigger is switched off in the same breath when it was a replacement, so an
    error banner on it would be noise (the UIDVALIDITY branch of `poll_org`,
    which the customer did not cause, does report).
    """
    if prior_identity is not None and prior_identity != (new_host, new_username):
        disable_trigger(db, org_id)
    abandoned = abandon_superseded_events(
        db, org_id=org_id,
        mailbox_identity=mailbox_identity(new_host, new_username),
    )
    db.commit()  # unconditional: never leave the UPDATE's transaction open
    if abandoned:
        _logger.info(
            "email trigger: mailbox saved for org %s; abandoned %s waiting message(s) "
            "from a previous mailbox",
            org_id, abandoned,
        )


def build_trigger_pipeline(name: str, db: Session, org_id: int, allowed_uids, backend):
    """An UNCACHED pipeline for (org_id, name) whose email tools are confined to
    `allowed_uids`. Mirrors main._get_pipeline's DB-record build but substitutes
    scoped email tools; not cached because the UID set is per-run. `backend` is
    the IMAP backend the caller already resolved for this poll cycle -- reused
    rather than re-fetched, so a mailbox swap mid-cycle can't produce a run
    that detects mail on one mailbox and reads/drafts on another. Raises on a
    missing/invalid team."""
    # Deployed-only gate (same as main._get_pipeline): a pipeline that isn't
    # deployed must not run, including via the autonomous poller. A non-deployed
    # (or absent) record is treated identically -- no run is built.
    record = (
        db.query(PipelineRecord)
        .filter_by(name=name, org_id=org_id, status="deployed")
        .one_or_none()
    )
    if record is None:
        raise ValueError(f"No deployed team named '{name}' for org {org_id}")
    # Pausing already disables the trigger, so the poller should not reach
    # here -- but a pause can land between that check and this build, and
    # `retry_triggered_run` has no trigger check of its own. Refused the same
    # way a missing team is: no build, no state advanced upstream, and the
    # messages are released penalty-free (infrastructure class).
    if not record.active:
        raise ValueError(f"Deployed team '{name}' for org {org_id} is paused")
    # A trigger stays enabled across redeploys -- only a mailbox identity
    # change disables it (on_mailbox_saved). If the team
    # was redeployed to a version with no email tools/skills, dispatching
    # would consume this cycle's UIDs and daily cap launching an unrelated
    # team with an email-triage prompt, so refuse the same way a missing
    # team does (no build, no state advanced upstream). Checked against the
    # skill versions pinned at deploy -- the ones `load_skills` below actually
    # builds with -- so a platform/org skill edited after deploy cannot make
    # this gate disagree with the pipeline it is gating.
    if not spec_uses_email(
        db, record.config, org_id, pipeline_version_id=record.current_version_id
    ):
        raise ValueError(f"Deployed team '{name}' for org {org_id} no longer uses email")
    source = _PIPELINES_DIR / f"{name}.yaml"
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
        db, org_id, pipeline_version_id=record.current_version_id
    )
    config = contain_pipeline_config_for_load(record.config)
    ensure_pipeline_cache_paths_for_source(config, source)
    pipeline = _build_pipeline(
        config,
        source=source,
        extra_tools={**kb_tools, **email_tools},
        extra_skills=skills,
    )
    # Return the version from the SAME record read that produced the config, so
    # the triggered run records exactly the version it executes even if a
    # redeploy commits concurrently (a separate current_version_id re-query could
    # observe a newer version than the one built).
    return pipeline, record.current_version_id


# The platform skill whose system prompt commits a pipeline's Response agent to
# emitting automation_results.RESULT_TYPE_BATCH_MARKER's JSON envelope (see
# skills.py / ui/backend/pipelines/property_maintenance_inbox_demo.yaml). Used
# only to stamp `trigger_context["result_contract"]` below -- advisory metadata,
# never anything build_trigger_pipeline's actual run depends on.
_PROPERTY_MAINTENANCE_RESPONSE_SKILL = "property_maintenance_response"


def _declares_property_maintenance_contract(db: Session, org_id: int, pipeline_name: str) -> bool:
    """Best-effort: does this deployed pipeline's config give any agent the
    ACTUAL platform `property_maintenance_response` skill?

    A run's own trace/output can't tell us this after the fact for a run that
    crashed before producing any JSON (`_normalize` can then only see a plain
    failure string, indistinguishable from any other org's unrelated
    email-trigger pipeline output) -- so this is captured up front, from the
    pipeline config itself, and stamped into `trigger_context` at dispatch
    time (below). That lets `automation_results._normalize` still synthesize
    the spec-required per-UID error rows for an envelope-less *maintenance-
    inbox* run, without also doing so for a crashed *unrelated* org's
    email-trigger pipeline that never declared this skill (Codex review
    finding). A read failure here must never block dispatch -- this is
    advisory only.

    A name match alone isn't enough: `load_skills` intentionally lets an
    org's own skill shadow a same-named platform built-in, so an org that
    happens to name (or repurpose) its own skill
    `property_maintenance_response` would otherwise get its unrelated
    runs wrongly redacted and stamped with synthetic maintenance error rows
    (Codex review finding). `_resolves_to_platform_skill` re-applies
    `load_skills`' own shadowing precedence to confirm the name still
    resolves to the platform-tier row.
    """
    try:
        record = (
            db.query(PipelineRecord)
            .filter_by(name=pipeline_name, org_id=org_id, status="deployed")
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


def _filter_decisions(backend, settings, uids) -> Dict[str, str]:
    """Which of `uids` the pre-LLM filter rejects, and why.

    Fails open, twice over: a UID the mailbox will not hand us a header for is
    absent from `summaries`, and a fetch that raises returns `{}`. Either way
    the message is recorded `pending` and processed. A transient IMAP hiccup
    must not silently discard a customer's mail; the worst case of failing open
    is that one junk message is billed.

    Keys are `str`, matching `record_events`'s `str(external_id) in decisions`
    lookup -- an int key here would silently leave every message `pending`, with
    no error anywhere to show the filter had stopped working.
    """
    if not uids:
        return {}
    try:
        summaries = backend.summaries_for([str(u) for u in uids])
    except Exception:  # noqa: BLE001 -- filtering is an optimisation, not a gate
        _logger.warning(
            "email trigger: header fetch failed; processing this batch unfiltered",
            exc_info=True,
        )
        return {}
    decisions = {}
    for summary in summaries:
        decision = email_filter.evaluate(summary, settings)
        if decision is not None:
            decisions[str(summary.get("id"))] = decision
    return decisions


def poll_org(db: Session, trigger: EmailTrigger, get_pipeline: Callable) -> None:
    """One org's poll cycle. Never raises; all state changes committed here.

    `get_pipeline` is `build_trigger_pipeline` injected by the loop (avoids a
    circular import and lets tests pass a stub):
    `(name, db, org_id, allowed_uids, backend) -> Pipeline`.
    """
    # Daily cap: reset on date rollover, and skip the mailbox entirely at cap.
    today = _today()
    if trigger.runs_date != today:
        trigger.runs_today = 0
        # The customer's daily MESSAGE cap shares `runs_date` on purpose: one
        # rollover check resets both counters, so they can never disagree about
        # which day it is. `retry_triggered_run` rolls the same date over and so
        # resets both here too -- a site that rolled `runs_date` but not this
        # would carry a stale message count into the new day, and no later check
        # would ever clear it.
        trigger.messages_today = 0
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
            backend = build_backend_for_credential(cred, password)
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
            _apply_health(db, trigger, trigger_health.OUTCOME_MAILBOX)
            db.commit()
            return
        except Exception as exc:  # noqa: BLE001 -- a poll failure must never kill the loop
            _logger.warning("email trigger: poll failed for org %s: %s", trigger.org_id, exc)
            trigger.last_error = _friendly_poll_error(exc)
            trigger.last_error_kind = _ERROR_KIND_MAILBOX
            trigger.last_checked_at = _utcnow()
            _apply_health(db, trigger, trigger_health.OUTCOME_MAILBOX)
            db.commit()
            return

        trigger.last_checked_at = _utcnow()
        # A successful mailbox check is direct proof connectivity/credentials are
        # fine, so a *mailbox*-kind error can auto-clear here. A *pipeline*-kind
        # error (or a legacy/unknown-kind row) must persist across empty polls
        # (F5) -- an empty poll never rebuilds the pipeline, so it proves nothing
        # about whether the team still builds. Cleared only on a successful
        # dispatch (below) or on (re-)enable (the API).
        if trigger.last_error_kind == _ERROR_KIND_MAILBOX:
            trigger.last_error = None
            trigger.last_error_kind = None
        _apply_health(db, trigger, trigger_health.OUTCOME_MAILBOX_OK)

        # Mailbox rebuilt/migrated: UIDs are not comparable across validities --
        # re-baseline to now, never reprocess.
        if trigger.uidvalidity is None or trigger.uidvalidity != uidvalidity:
            trigger.uidvalidity = uidvalidity
            trigger.last_uid = max_uid
            # Anything still waiting was detected under the OLD generation, and
            # a UID means nothing outside the generation that issued it. The
            # claim query already refuses those rows; this is what stops them
            # sitting `pending` for ever with nothing reporting them. The
            # customer did not cause a mailbox rebuild, so dropped mail is
            # named on the one field the UI surfaces -- unlike the mailbox
            # *replacement* path, which the customer performed themselves.
            abandoned = abandon_superseded_events(
                db,
                org_id=trigger.org_id,
                mailbox_identity=mailbox_identity(cred.host, cred.username),
                mailbox_generation=str(uidvalidity),
            )
            if abandoned:
                trigger.last_error = (
                    f"The mailbox was rebuilt, so {abandoned} message(s) that were "
                    "waiting to be processed have been abandoned -- their message "
                    "numbers no longer refer to the same emails."
                )
                trigger.last_error_kind = _ERROR_KIND_MAILBOX
            db.commit()
            return

        if new_uids:
            # THE durability point. Recording the work and advancing the cursor
            # in ONE commit is what stops a process kill from consuming mail
            # that nothing ran: before this, `_start_triggered_run` advanced
            # `last_uid` and only then handed the pipeline to a thread pool, and
            # a kill in between lost the batch for good.
            detected = sorted(new_uids)[: batch_size() * _DETECT_MULTIPLIER]
            # The pre-LLM filter chooses each row's STATUS, never whether the row
            # is inserted, so it belongs here, before the durability point: every
            # detected UID still gets a row, and the record_events / last_uid /
            # commit trio below stays one unit.
            decisions = _filter_decisions(
                backend, get_filter_settings(db, trigger.org_id), detected
            )
            record_events(
                db,
                org_id=trigger.org_id,
                mailbox_identity=mailbox_identity(cred.host, cred.username),
                mailbox_generation=str(trigger.uidvalidity),
                external_ids=[str(u) for u in detected],
                decisions=decisions,
            )
            trigger.last_uid = max(detected)
            db.commit()
        elif has_pending_events(
            db,
            org_id=trigger.org_id,
            mailbox_identity=mailbox_identity(cred.host, cred.username),
            mailbox_generation=str(trigger.uidvalidity),
        ):
            # No new mail, but this org already has claimable work in the
            # ledger. A `pending` row does not only come from mail arriving:
            # an admin releasing a filtered false positive makes one, and so
            # does a backlog a budget cap declined to dispatch. Both are
            # promised to be picked up "on the next check" -- by the release
            # UI, by the budget alert's own wording, and by the design spec --
            # and returning here made that promise false on a quiet mailbox,
            # because only a cycle that detected NEW mail ever reached the
            # claim. Nothing about the durability sequence above moves: this
            # branch records nothing and advances no cursor, it only declines
            # to stop early.
            db.commit()  # persist last_checked_at / the error clearing above
        else:
            db.commit()
            return

        if _at_daily_cap(db, trigger, today):
            db.commit()  # persist last_checked_at / error-clearing above; no dispatch
            return

        _start_triggered_run(db, trigger, get_pipeline, backend, cred)


def _trigger_input(uids) -> str:
    ids = ", ".join(str(u) for u in uids)
    return (
        f"{len(uids)} new email(s) arrived in the inbox (message ids: {ids}). "
        "Read each message by id and triage it, drafting replies where appropriate."
    )


def _start_triggered_run(
    db: Session, trigger: EmailTrigger, get_pipeline, backend, cred
) -> None:
    """Start ONE run over a bounded batch of this org's pending inbox events.

    The batch is whatever this run CLAIMS from the durable ledger, not whatever
    the poller happened to detect this cycle -- batching is a claim policy now,
    not a coupling. The claim is committed before any pipeline build is
    attempted, so a build failure releases the messages (penalty-free: a broken
    team config is not the message's fault) rather than consuming them.

    Then persist a durable run row and advance state in one commit, then
    dispatch. `get_pipeline` is `build_trigger_pipeline(name, db, org_id,
    allowed_uids, backend) -> (Pipeline, Optional[int] version_id)`; the version
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
        trigger.pipeline_name, "", org_id=trigger.org_id, username=TRIGGER_USERNAME,
    )
    # The customer's two budgets (the operator's `daily_cap()` runs-per-day rail
    # is separate and unchanged, checked by the caller). Both are read here,
    # inside the caller's `_dispatch_lock`, immediately before the claim -- same
    # staleness rationale as `_at_daily_cap`.
    caps = get_budget_caps(db, trigger.org_id)
    now = _utcnow()

    if email_budget.cost_exceeded(caps, spent_this_month(db, trigger.org_id, now)):
        registry.discard(run.id)
        _raise_budget_alert(
            db, trigger.org_id, "cost", email_budget.month_key(now),
            "This organisation has reached its monthly spend limit for automatic "
            "email runs. New mail is still being collected and will be processed "
            "when the new month begins, or sooner if you raise the limit.",
        )
        db.commit()
        return

    # Read off the ORM object, deliberately unlike `_at_daily_cap`'s fresh
    # column SELECT: `runs_today` needs one because a pre-lock fast-path check
    # can already have passed on a stale value and because `retry_triggered_run`
    # advances it from another session, whereas `messages_today` is checked here
    # and nowhere else and is written only by this function's own CAS below,
    # under the caller's `_dispatch_lock`. It also already carries the caller's
    # date rollover, which a raw column read would have to re-derive.
    remaining = email_budget.remaining_messages(caps, trigger.messages_today)
    if remaining == 0:
        registry.discard(run.id)
        _raise_budget_alert(
            db, trigger.org_id, "messages", email_budget.day_key(now),
            "This organisation has reached its daily limit for automatically "
            "processed emails. New mail is still being collected and will be "
            "processed tomorrow, or sooner if you raise the limit.",
        )
        db.commit()
        return

    # The message cap truncates the claim rather than rejecting the cycle: the
    # messages it leaves behind stay `pending` and are claimed by a later cycle.
    limit = batch_size() if remaining is None else min(batch_size(), remaining)
    claimed = claim_events(
        db,
        org_id=trigger.org_id,
        run_id=run.id,
        limit=limit,
        # Scoped to the mailbox this cycle actually resolved, and to its
        # current generation: a `pending` row left by a replaced or rebuilt
        # mailbox names a UID that means nothing here, and after a rebuild
        # reissues UIDs it names a completely different message.
        mailbox_identity=mailbox_identity(cred.host, cred.username),
        mailbox_generation=str(trigger.uidvalidity),
    )
    if not claimed:
        registry.discard(run.id)
        db.commit()
        return
    db.commit()  # the claim is durable before any pipeline build is attempted
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
    if _declares_property_maintenance_contract(db, trigger.org_id, trigger.pipeline_name):
        trigger_context["result_contract"] = RESULT_TYPE_BATCH_MARKER
    try:
        pipeline, version_id = get_pipeline(trigger.pipeline_name, db, trigger.org_id, set(batch), backend)
    except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since enabling
        _logger.warning("email trigger: cannot build pipeline %r for org %s: %s",
                        trigger.pipeline_name, trigger.org_id, exc)
        trigger.last_error = (
            f"Couldn't start the team '{trigger.pipeline_name}' -- it may have "
            "been changed or removed. Re-enable automatic runs from its page."
        )
        trigger.last_error_kind = _ERROR_KIND_PIPELINE
        # Penalty-free release: the pipeline is broken, not the messages. No
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
        id=run.id, pipeline=trigger.pipeline_name, input=input_text,
        status="running", org_id=trigger.org_id, username=TRIGGER_USERNAME,
        pipeline_version_id=version_id, trigger_context=trigger_context,
    )
    # Compare-and-swap: advance the batch/cap and record this run ONLY if the
    # trigger is still enabled. org_settings.py/admin.py disable the trigger in
    # their own commit when the customer/operator disconnects or replaces the
    # mailbox (a replacement disables via on_mailbox_saved),
    # separate from the credential write that may have landed while this
    # pipeline was being built. Guarding the advance and the enabled-check in
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
            # The customer's message counter advances in exactly this statement
            # and nowhere else, under the same enabled/active predicate. That
            # covers two of the three release paths for free -- a build failure
            # returns before this runs, and a trigger disabled mid-build makes
            # this match no row -- so neither charges for messages it handed
            # back. It does NOT cover the third: the submit-failure branch runs
            # after this has committed, so those messages are charged here and
            # then released to `pending`, and a later cycle claims and charges
            # them again. Same semantics `runs_today` has always had on that
            # path ("The cap was already consumed by the commit above", below),
            # and deliberately not fixed by decrementing: a second write site
            # outside the CAS would cost more in reasoning than an over-count
            # bounded at one batch, on a branch that only fires when the
            # executor is shutting down and the poller is stopping anyway.
            messages_today=EmailTrigger.messages_today + len(claimed),
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
            run_in_background, run.id, pipeline, input_text,
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
        trigger.last_error_kind = _ERROR_KIND_PIPELINE
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
            TraceEvent(type="run_failed", pipeline=trigger.pipeline_name, data=message)
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
    pipeline must still build, and the org's daily automatic-run cap must not
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
    # and pipeline rebuild entirely for the common case of an obviously
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
        backend = build_backend_for_credential(cred, password)
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
        # Same rollover, same reason as poll_org's: `messages_today` shares
        # `runs_date`, so every place that rolls the date over must roll both
        # counters or a stale message count would survive into the new day.
        trigger.messages_today = 0
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
            # The check further up is only a fast-path (skip mailbox/pipeline
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
            pipeline, version_id = build_trigger_pipeline(run_row.pipeline, db, org_id, set(retry_uids), backend)
        except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since the original run
            raise RetryError(
                f"Couldn't rebuild the team '{run_row.pipeline}' -- it may have been changed or removed."
            ) from exc

        new_run = registry.create(run_row.pipeline, input_text, org_id=org_id, username=TRIGGER_USERNAME)
        # Narrowed to retry_uids (not the original full batch): a UID already
        # confirmed drafted is excluded from what this new run is even allowed to
        # touch (build_trigger_pipeline's allowed_uids above), and its
        # trigger_context must agree, or normalize_run_result would treat that
        # already-handled UID as "missing" from this run's envelope and wrongly
        # synthesize a needs_attention error row for it under the new run id.
        new_trigger_context = {**trigger_context, "uids": retry_uids, "triggered_at": _utcnow().isoformat()}
        # Recompute, don't just carry over: the original run's result_contract
        # can be stale by the time it's retried if the deployed pipeline (or a
        # same-named skill) changed in between -- carrying it over verbatim
        # would either leave a now-maintenance pipeline's output unredacted or
        # wrongly redact/normalize a pipeline that no longer declares the
        # contract (Codex review finding).
        if _declares_property_maintenance_contract(db, org_id, run_row.pipeline):
            new_trigger_context["result_contract"] = RESULT_TYPE_BATCH_MARKER
        else:
            new_trigger_context.pop("result_contract", None)
        new_row = Run(
            id=new_run.id, pipeline=run_row.pipeline, input=input_text,
            status="running", org_id=org_id, username=TRIGGER_USERNAME,
            pipeline_version_id=version_id, trigger_context=new_trigger_context,
            retry_of_run_id=run_row.id,
        )
        db.add(new_row)
        # Mirror the decision just made onto the durable ledger: the messages
        # this retry will redo become the NEW run's responsibility, and the ones
        # it refuses to touch (a draft already exists -- possibly discovered
        # only now, by the mailbox scan above) go terminal on the original.
        # A run that predates the ledger has no events and this is a no-op.
        resolve_retry_events(
            db,
            from_run_id=run_row.id,
            to_run_id=new_run.id,
            retry_external_ids=retry_uids,
            done_external_ids=already_drafted,
        )
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
                # pipeline-kind error from a past failure keeps reporting a
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
                run_in_background, new_run.id, pipeline, input_text,
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
                TraceEvent(type="run_failed", pipeline=run_row.pipeline, data=message)
            ))
        return new_run.id


_BUDGET_KIND = "budget"


def _budget_fingerprint(which: str, period: str) -> str:
    """Scope a budget alert to the period it is about.

    `has_fingerprint` searches an org's entire notification history, so a bare
    name would alert once ever and every later month would be silent -- the
    same trap `_expiry_fingerprint` exists to avoid.
    """
    return f"budget_{which}:{period}"


def _raise_budget_alert(db: Session, org_id: int, which: str, period: str, body: str) -> None:
    """Alert once per period that automation has paused on a budget.

    Deliberately NOT routed through `trigger_health.evaluate`: a budget
    ceiling is a normal operating state, not a fault, and feeding it into the
    fault evaluator would corrupt `consecutive_faults` and compete with real
    faults for `alerted_fingerprint`.
    """
    fingerprint = _budget_fingerprint(which, period)
    if has_fingerprint(db, org_id, fingerprint):
        return
    create_notification(
        db, org_id=org_id, kind=_BUDGET_KIND, severity="warning",
        title="Automatic email runs have paused -- budget reached",
        body=body, fingerprint=fingerprint,
    )


# Warn this far ahead of a Microsoft 365 client secret expiring. Entra secrets
# last at most two years and always expire; when one does, IMAP starts refusing
# the app and the error reads exactly like a wrong password, so an unwarned
# customer looks in the wrong place.
# Tightest band FIRST: the lookup takes the first match, and a secret with five
# days left belongs in the seven-day band, not the thirty-day one.
_SECRET_EXPIRY_BANDS = ((7, "secret_expiry_7"), (30, "secret_expiry_30"))
_SECRET_EXPIRED_FINGERPRINT = "secret_expired"


def _expiry_fingerprint(band: str, expiry_date: date) -> str:
    """Scope a band to the secret it is warning about.

    `has_fingerprint` searches an org's whole notification history, so a bare
    band name warns each org exactly once ever: replace the expiring secret
    and the new one's 30-day warning is suppressed by the old one's record
    (Codex review finding). The expiry date distinguishes secrets -- a
    replacement has a new one, and a replacement that somehow expires on the
    same day genuinely is the same deadline.
    """
    return f"{band}:{expiry_date.isoformat()}"


def sweep_secret_expiry(db: Session, today: Optional[date] = None) -> int:
    """Warn each org whose stored M365 client secret is close to expiring.

    `today` is injected so the test never depends on the wall clock. Returns
    how many notifications were raised.

    Credentials with no recorded expiry are skipped entirely: the date is
    optional and admin-entered, and inventing one would warn about a deadline
    nobody stated. The already-warned check reads the notifications themselves
    because this state belongs to the credential, which has no fingerprint
    column of its own.
    """
    reference = today or datetime.now(timezone.utc).date()
    raised = 0
    credentials = (
        db.query(OrgEmailCredential)
        .filter(OrgEmailCredential.auth_type == AUTH_MICROSOFT_OAUTH)
        .filter(OrgEmailCredential.oauth_secret_expires_at.isnot(None))
        .all()
    )
    for cred in credentials:
        expires = cred.oauth_secret_expires_at
        if expires is None:
            continue
        expiry_date = expires.date() if isinstance(expires, datetime) else expires
        days_left = (expiry_date - reference).days

        if days_left <= 0:
            fingerprint = _expiry_fingerprint(_SECRET_EXPIRED_FINGERPRINT, expiry_date)
            severity, title = "error", "Your Microsoft 365 app password has expired"
            body = (
                "The client secret for this mailbox has expired, so no new mail "
                "can be collected. Create a new client secret in Azure and "
                "reconnect the mailbox in your organisation's settings."
            )
        else:
            band = next((f for d, f in _SECRET_EXPIRY_BANDS if days_left <= d), None)
            if band is None:
                continue
            fingerprint = _expiry_fingerprint(band, expiry_date)
            severity, title = "warning", "Your Microsoft 365 app password expires soon"
            body = (
                f"The client secret for this mailbox expires in {days_left} day(s). "
                "Create a new client secret in Azure and reconnect the mailbox "
                "before then, or automatic email runs will stop."
            )

        if has_fingerprint(db, cred.org_id, fingerprint):
            continue
        create_notification(
            db, org_id=cred.org_id, kind="secret_expiry", severity=severity,
            title=title, body=body, fingerprint=fingerprint,
        )
        raised += 1

    if raised:
        db.commit()
    return raised


def run_maintenance(db: Session) -> None:
    """Timer-driven upkeep: secret-expiry warnings, the retention sweep, and
    notification delivery.

    Piggy-backs on the poller because it already runs on a timer. Never raises:
    the poll loop must outlive any one of these.
    """
    try:
        sweep_secret_expiry(db)
        sweep_retention(db)
        dispatch_pending(db)
    except Exception:  # noqa: BLE001 -- upkeep must never break polling
        db.rollback()
        _logger.exception("email trigger: maintenance failed")


def maintenance_once(session_factory=None) -> None:
    """`run_maintenance` with its own session, for the paused branch of
    `poll_forever` -- retention is not part of what a trigger pause pauses."""
    from .db_session import SessionLocal  # late import: keep module import-light

    factory = session_factory or SessionLocal
    with factory() as db:
        run_maintenance(db)


def poll_once(get_pipeline: Callable, session_factory=None) -> None:
    """One pass over every enabled org. Runs on a worker thread (imaplib and
    SQLAlchemy here are synchronous); a failure in one org never stops the rest."""
    from .db_session import SessionLocal  # late import: keep module import-light

    from .db.email_triggers import list_enabled_triggers

    factory = session_factory or SessionLocal
    with factory() as db:
        for trigger in list_enabled_triggers(db):
            try:
                poll_org(db, trigger, get_pipeline)
                _apply_backlog_health(db, trigger)
                # What the customer did with each platform draft (B1). Its own
                # isolation boundary, and free when nothing is pending -- see
                # draft_outcomes.reconcile_org.
                draft_outcomes.reconcile_org(db, trigger)
            except Exception:  # noqa: BLE001 -- the loop must outlive any org's failure
                # Roll back BEFORE touching `trigger` again: a failed flush leaves
                # the session's objects expired, so logging trigger.org_id first
                # would itself raise PendingRollbackError trying to reload it.
                db.rollback()
                _logger.exception("email trigger: unexpected failure for org %s",
                                  trigger.org_id)

        # Deliver whatever this cycle (or an earlier one) raised, and run the
        # rest of the timer-driven upkeep. Piggy-backing on the poller rather
        # than running a thread of its own: it already runs on a timer, and a
        # notification that waits one cycle has still arrived far sooner than
        # the customer noticing by themselves.
        run_maintenance(db)


async def poll_forever(stop_event: "asyncio.Event", get_pipeline: Callable) -> None:
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
            # A platform-wide pause of AUTOMATION is not a pause of data
            # deletion -- an org's retention policy keeps running.
            try:
                await asyncio.to_thread(maintenance_once)
            except Exception:  # noqa: BLE001 -- never let the task die
                _logger.exception("email trigger: maintenance cycle failed")
            continue
        try:
            await asyncio.to_thread(poll_once, get_pipeline)
        except Exception:  # noqa: BLE001 -- never let the task die
            _logger.exception("email trigger: poll cycle failed")
