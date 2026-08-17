"""Server-side normalization of a triggered run's output into immutable
`automation_item_results` rows (Property Maintenance Inbox, Release 1A).

The Maintenance Response Coordinator agent is instructed to end its turn with
a strict JSON envelope (see `workflows/property_maintenance_inbox_demo.yaml`
and the platform skill `property_maintenance_response_v1`). This module never
trusts that JSON for identity: `org_id`/`run_id`/`source_key` are always
derived from the run's own persisted `trigger_context` (the poller-detected
UID batch), never from the model's claimed `message_id` set. See
docs/superpowers/specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md
sections 5.3, 10, 10.1, 11.1.

Scoping note: `normalize_run_result` only engages once a run's output parses
as JSON carrying `result_type == "property_maintenance_email_batch"`. Every
other org's existing (unrelated) email-trigger workflow returns free text, so
this never fires for them -- see the plan doc's "Normalization decision" for
why a literal "always synthesize errors for every trigger run" reading would
have been a correctness regression for those workflows.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from .db.models import AutomationItemResult, Run, TraceEventRecord

_logger = logging.getLogger(__name__)

RESULT_TYPE_BATCH_MARKER = "property_maintenance_email_batch"
ITEM_RESULT_TYPE = "property_maintenance_email"

CLASSIFICATIONS = {
    "maintenance_request",
    "maintenance_follow_up",
    "owner_or_contractor_message",
    "non_maintenance",
    "spam_or_automated",
    "unknown",
}
CATEGORIES = {
    "plumbing", "electrical", "hot_water", "locks_security", "heating_cooling",
    "appliance", "structural", "water_damage", "pest", "garden", "cleaning",
    "other", "unknown",
}
PRIORITIES = {"routine", "priority", "possible_emergency", "unknown"}
ITEM_STATUSES = {"processed", "needs_attention", "skipped", "error"}

# Priorities the spec (section 9.3) requires to always surface as
# needs_attention, regardless of what the model itself set.
_ALWAYS_NEEDS_ATTENTION_PRIORITIES = {"possible_emergency", "unknown"}

_SUMMARY_MAX_CHARS = 500
_FIELD_MAX_CHARS = 300
_LIST_MAX_ITEMS = 20
_LIST_ITEM_MAX_CHARS = 200
_MESSAGE_ID_LOG_MAX_CHARS = 64


def _cap(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _cap_list(values: List[str], limit: int, item_limit: int) -> List[str]:
    return [_cap(v, item_limit) for v in values[:limit]]


class ExtractedFields(BaseModel):
    property_address: Optional[str] = None
    unit_number: Optional[str] = None
    sender_name: Optional[str] = None
    reply_email: Optional[str] = None
    callback_number: Optional[str] = None
    access_availability: Optional[str] = None
    permission_to_enter: Optional[str] = None
    pets_or_access_constraints: Optional[str] = None
    prior_report_reference: Optional[str] = None
    attachment_mentioned: Optional[bool] = None
    first_noticed: Optional[str] = None
    current_impact: Optional[str] = None

    model_config = {"extra": "ignore"}


class DraftAction(BaseModel):
    draft_created: bool = False
    draft_type: Optional[str] = None

    model_config = {"extra": "ignore"}


class EnvelopeItem(BaseModel):
    message_id: str
    classification: str
    category: str = "unknown"
    priority: str = "unknown"
    status: str
    summary: str = ""
    extracted: ExtractedFields = Field(default_factory=ExtractedFields)
    missing_information: List[str] = Field(default_factory=list)
    risk_reasons: List[str] = Field(default_factory=list)
    action: DraftAction = Field(default_factory=DraftAction)
    needs_human: bool = False
    human_reason: Optional[str] = None

    model_config = {"extra": "ignore"}

    @field_validator("classification")
    @classmethod
    def _valid_classification(cls, v: str) -> str:
        if v not in CLASSIFICATIONS:
            raise ValueError(f"unknown classification {v!r}")
        return v

    @field_validator("category")
    @classmethod
    def _valid_category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: str) -> str:
        if v not in PRIORITIES:
            raise ValueError(f"unknown priority {v!r}")
        return v

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in ITEM_STATUSES:
            raise ValueError(f"unknown status {v!r}")
        return v


SUPPORTED_SCHEMA_VERSION = 1


class Envelope(BaseModel):
    schema_version: int = 1
    result_type: str
    items: List[EnvelopeItem] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    @field_validator("schema_version")
    @classmethod
    def _supported_schema_version(cls, v: int) -> int:
        # `extra: "ignore"` means a newer, incompatible schema's unknown
        # fields would otherwise be silently dropped rather than surfaced --
        # fail the whole envelope instead so it gets the normal error-row
        # treatment (Codex review finding).
        if v != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {v!r}")
        return v


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of a top-level JSON object from a model's final
    message: a plain JSON body, or one inside a ```json ... ``` fence (models
    routinely wrap structured output in markdown fences). Returns None if
    nothing that looks like a JSON object is found -- never raises."""
    if not text:
        return None
    candidates = []
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1))
    candidates.append(text.strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _source_key(mailbox_credential_id: Any, uidvalidity: Any, uid: str) -> str:
    return f"mailbox:{mailbox_credential_id}:uidvalidity:{uidvalidity}:uid:{uid}"


_UNCONFIRMED_DRAFT_REASON = (
    "The automation's output claimed a draft was created, but no successful "
    "draft action was recorded for this message -- treat as not drafted."
)


def _reconciled_action(item: EnvelopeItem, confirmed_draft_message_ids: frozenset) -> Dict[str, Any]:
    """Never trust `action.draft_created` from the model alone -- only a real,
    successfully-executed `email_draft_reply` tool call for this exact message
    id (reported via the run's own trace events, see runtime.py) confirms a
    draft actually exists. A claimed-but-unconfirmed draft downgrades to
    `draft_created: false` rather than showing a customer a draft that may not
    exist; symmetrically, a claimed-false that the trace actually confirms
    upgrades to `draft_created: true` -- otherwise a model that mis-reports its
    own successful draft call would have the stored result (and the daily
    summary's count) under-report a draft that genuinely exists in the mailbox
    (Codex review finding)."""
    action = item.action.model_dump()
    confirmed = item.message_id.strip() in confirmed_draft_message_ids
    if action.get("draft_created") and not confirmed:
        action["draft_created"] = False
    elif not action.get("draft_created") and confirmed:
        action["draft_created"] = True
    action["draft_type"] = _cap(action.get("draft_type"), _FIELD_MAX_CHARS)
    return action


def _item_payload(item: EnvelopeItem, confirmed_draft_message_ids: frozenset = frozenset()) -> Dict[str, Any]:
    extracted = item.extracted.model_dump()
    for key, value in extracted.items():
        if isinstance(value, str):
            extracted[key] = _cap(value, _FIELD_MAX_CHARS)
    return {
        "classification": item.classification,
        "category": item.category,
        "priority": item.priority,
        "summary": _cap(item.summary, _SUMMARY_MAX_CHARS),
        "extracted": extracted,
        "missing_information": _cap_list(item.missing_information, _LIST_MAX_ITEMS, _LIST_ITEM_MAX_CHARS),
        "risk_reasons": _cap_list(item.risk_reasons, _LIST_MAX_ITEMS, _LIST_ITEM_MAX_CHARS),
        "action": _reconciled_action(item, confirmed_draft_message_ids),
        "human_reason": _cap(item.human_reason, _FIELD_MAX_CHARS),
    }


def _needs_attention_for(
    item: EnvelopeItem,
    confirmed_draft_message_ids: frozenset = frozenset(),
    failed_tool_message_ids: frozenset = frozenset(),
) -> bool:
    """The server -- not the model -- enforces the hard rule that
    possible_emergency/unknown priority always needs a human (spec 9.3), that
    an item status of needs_attention/error always does too, that a
    claimed-but-unconfirmed draft (see `_reconciled_action`) always does too,
    and (spec 9.5 "Tool failure -> needs_attention: yes") that a confirmed
    tool failure for this UID always does too -- the model's own envelope item
    is trusted to self-report a tool failure it saw, but not relied on as the
    only signal, the same trust boundary already applied to draft claims
    (Codex review finding)."""
    unconfirmed_draft = (
        item.action.draft_created and item.message_id.strip() not in confirmed_draft_message_ids
    )
    confirmed_tool_failure = item.message_id.strip() in failed_tool_message_ids
    return (
        item.needs_human
        or item.status in ("needs_attention", "error")
        or item.priority in _ALWAYS_NEEDS_ATTENTION_PRIORITIES
        or unconfirmed_draft
        or confirmed_tool_failure
    )


def _error_payload(reason: str, *, draft_created: bool = False) -> Dict[str, Any]:
    return {
        "classification": "unknown",
        "category": "unknown",
        "priority": "unknown",
        "summary": "",
        "extracted": {},
        "missing_information": [],
        "risk_reasons": [],
        "action": {"draft_created": draft_created, "draft_type": None},
        "human_reason": _cap(reason, _FIELD_MAX_CHARS),
    }


def normalize_run_result(
    db: Session,
    run_row: Run,
    *,
    confirmed_draft_message_ids: Optional[frozenset] = None,
    failed_tool_message_ids: Optional[frozenset] = None,
    raw_output_override: Optional[str] = None,
) -> None:
    """Normalize one triggered run's output, if it declares itself a
    Property Maintenance Inbox batch result. Never raises -- normalization is
    auxiliary to the run's own (already-recorded) terminal status; a failure
    here must not affect it. Idempotent via the (run_id, source_key) unique
    constraint, so a repeated call (e.g. a duplicate completion callback)
    inserts nothing new for rows it already wrote.

    `confirmed_draft_message_ids`: the set of message ids this run actually
    executed a successful `email_draft_reply` tool call for (runtime.py
    collects this from the run's own trace events). Used to reconcile the
    model's claimed `action.draft_created` per item -- see `_reconciled_action`.
    Defaults to empty (no confirmed drafts), which is the safe default for any
    caller that hasn't collected trace evidence.

    `failed_tool_message_ids`: the set of message ids this run's own trace
    recorded a failed `email_read`/`email_draft_reply` call for. Forces
    `needs_attention` for that UID regardless of what the model's envelope
    item claims -- see `_needs_attention_for` (spec 9.5 "Tool failure ->
    needs_attention: yes"). Defaults to empty, same rationale as above.

    `raw_output_override`: the real agent output text to parse, when
    `run_row.output` itself has already been overwritten with a redacted
    placeholder (`runtime.py` does this for a property-maintenance run before
    this is ever called, so the raw text never touches `trace_events`/
    `runs.output` -- Codex review finding). `None` (the default) means "use
    `run_row.output` as-is", unchanged behavior for every other caller.
    """
    try:
        _normalize(
            db,
            run_row,
            frozenset(confirmed_draft_message_ids or ()),
            frozenset(failed_tool_message_ids or ()),
            raw_output_override=raw_output_override,
        )
    except Exception:  # noqa: BLE001 -- normalization must never break a run
        _logger.exception("Automation result normalization failed for run %s", run_row.id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _retry_family_run_ids(db: Session, run_row: Run) -> list:
    """Every run id in `run_row`'s retry family: its root (walking back
    through `retry_of_run_id` until `None`) plus every run reachable forward
    from that root (a run can be retried more than once, so the family is a
    tree, not just a straight line -- e.g. the original run is retried,
    that retry fails, and the customer retries the *original* run again
    rather than the failed retry, creating a sibling branch). `run_row`
    itself is always included (root == run_row when it has never been
    retried before)."""
    root_id = run_row.id
    current = run_row
    while current.retry_of_run_id is not None:
        parent = db.get(Run, current.retry_of_run_id)
        if parent is None:
            break
        root_id = parent.id
        current = parent

    family = [root_id]
    frontier = [root_id]
    while frontier:
        children = db.query(Run.id).filter(Run.retry_of_run_id.in_(frontier)).all()
        frontier = [row.id for row in children if row.id not in family]
        family.extend(frontier)
    return family


def already_drafted_uids(db: Session, run_row: Run) -> frozenset:
    """The subset of `run_row`'s trigger-context UID batch that any run in
    its retry family's normalized results confirm a real draft exists for
    (`action.draft_created` true). `email_draft_reply` has no dedup -- it
    unconditionally APPENDs a new draft -- so `email_trigger.retry_triggered_run`
    uses this to exclude already-drafted messages from a retried batch;
    otherwise resubmitting the whole original batch would create a second
    draft for each one. Checking only `run_row`'s own results misses a UID
    drafted by a *different* retry attempt off the same original run -- e.g.
    the original run drafts UID 5 then crashes, a first retry (excluding 5)
    drafts UID 6 then also fails, and the customer retries the *original*
    run again (not that first retry) rather than waiting: that second retry
    must still exclude both 5 (drafted by the original) and 6 (drafted by
    the sibling first retry), even though neither is in the original run's
    own results alone (Codex review finding).

    Two independent sources of evidence are unioned, because normalized
    results alone only ever covered ONE workflow template:

    - `automation_item_results` rows, which `normalize_run_result` writes
      exclusively for the property-maintenance contract; and
    - the run's own persisted `tool_completed` trace events, which every run
      records regardless of template (`_trace_confirmed_uids`).

    Relying on the first alone meant a generic email team (e.g.
    `email_triage_reply`) had no evidence at all, so a retry resubmitted an
    entire partially-drafted batch and created a duplicate draft for every
    message the original run had already replied to -- with no crash needed,
    just an ordinary mid-run failure (Phase 0, item 0.2).
    """
    trigger_context = run_row.trigger_context or {}
    uids = [str(u) for u in trigger_context.get("uids") or []]
    if not uids:
        return frozenset()
    mailbox_credential_id = trigger_context.get("mailbox_credential_id")
    uidvalidity = trigger_context.get("uidvalidity")
    key_to_uid = {_source_key(mailbox_credential_id, uidvalidity, uid): uid for uid in uids}
    family = _retry_family_run_ids(db, run_row)
    rows = (
        db.query(AutomationItemResult.source_key, AutomationItemResult.payload)
        .filter(AutomationItemResult.run_id.in_(family))
        .filter(AutomationItemResult.source_key.in_(key_to_uid.keys()))
        .all()
    )
    from_results = {
        key_to_uid[source_key]
        for source_key, payload in rows
        if (payload.get("action") or {}).get("draft_created")
    }
    return frozenset(from_results | _trace_confirmed_uids(db, family, set(uids)))


def _trace_confirmed_uids(db: Session, run_ids, allowed_uids: set) -> set:
    """Message ids in `allowed_uids` that `run_ids`' persisted trace events
    prove a real draft was created for.

    Ground truth, not a model claim: `adapters/langgraph_adapter.py` only
    labels a `tool_completed` event `outcome="draft_created"` when the
    `email_draft_reply` call actually returned the backend's success text, and
    `runtime.py` persists every such event. Restricting to `allowed_uids` keeps
    a message id the trace mentions but the server never assigned to this batch
    from widening what a retry treats as handled.

    Malformed/absent `data` is skipped rather than raised: this guard's whole
    job is to make a retry safer, so it must never itself break the retry path.
    """
    confirmed = set()
    rows = (
        db.query(TraceEventRecord.data)
        .filter(TraceEventRecord.run_id.in_(run_ids))
        .filter(TraceEventRecord.type == "tool_completed")
        .all()
    )
    for (raw,) in rows:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("tool") != "email_draft_reply" or data.get("outcome") != "draft_created":
            continue
        message_id = data.get("message_id")
        if isinstance(message_id, str) and message_id.strip() in allowed_uids:
            confirmed.add(message_id.strip())
    return confirmed


def _normalize(
    db: Session,
    run_row: Run,
    confirmed_draft_message_ids: frozenset = frozenset(),
    failed_tool_message_ids: frozenset = frozenset(),
    *,
    raw_output_override: Optional[str] = None,
) -> None:
    trigger_context = run_row.trigger_context
    if not trigger_context or trigger_context.get("trigger_type") != "email":
        return
    if run_row.org_id is None:
        return

    uids = [str(u) for u in trigger_context.get("uids") or []]
    if not uids:
        return

    mailbox_credential_id = trigger_context.get("mailbox_credential_id")
    uidvalidity = trigger_context.get("uidvalidity")

    existing_keys = {
        row.source_key
        for row in db.query(AutomationItemResult.source_key).filter_by(run_id=run_row.id)
    }

    raw = _extract_json_object(raw_output_override if raw_output_override is not None else (run_row.output or ""))
    if raw is None or raw.get("result_type") != RESULT_TYPE_BATCH_MARKER:
        if trigger_context.get("result_contract") == RESULT_TYPE_BATCH_MARKER:
            # This run WAS dispatched against a workflow whose Response agent
            # carries the property_maintenance_response_v1 skill (stamped into
            # trigger_context at dispatch time -- see
            # email_trigger._declares_property_maintenance_contract), so an
            # unparseable output -- including a plain failure string from a
            # run that crashed before producing any JSON -- is still this
            # vertical's batch, not free text from an unrelated org's
            # email-trigger workflow. Spec 10.1: every UID in a declared batch
            # gets a result row even when the run never reached a parseable
            # envelope (Codex review finding).
            _write_error_rows(
                db, run_row, uids, existing_keys, mailbox_credential_id, uidvalidity,
                reason="Result normalization failed: the automation's output didn't match the expected format.",
                confirmed_draft_message_ids=confirmed_draft_message_ids,
            )
        # Otherwise: the model produced no parseable JSON with this marker,
        # and this workflow was never marked as using this vertical's
        # contract -- an unrelated org's email-trigger workflow, left alone
        # entirely.
        return

    try:
        envelope = Envelope.model_validate(raw)
    except ValidationError as exc:
        # Not `exc`/`str(exc)` directly: Pydantic's error repr embeds the
        # offending input value, and that value can be raw email content a
        # prompt-injected message steered into an invalid enum/id field --
        # this would put customer content into server logs (Codex review
        # finding). loc/type alone identify the failure without repeating it.
        error_summary = [{"loc": err.get("loc"), "type": err.get("type")} for err in exc.errors()]
        _logger.warning("Run %s: envelope failed validation: %s", run_row.id, error_summary)
        _write_error_rows(
            db, run_row, uids, existing_keys, mailbox_credential_id, uidvalidity,
            reason="Result normalization failed: the automation's output didn't match the expected format.",
            confirmed_draft_message_ids=confirmed_draft_message_ids,
        )
        return

    item_by_uid: Dict[str, EnvelopeItem] = {}
    for item in envelope.items:
        message_id = item.message_id.strip()
        if message_id not in set(uids):
            # Bounded, not the raw value: an out-of-batch id is exactly the
            # case where message_id is attacker-controlled -- a prompt-
            # injected email can steer the model into putting arbitrary body
            # text into this field, and it would otherwise land unbounded in
            # server logs (Codex review finding).
            _logger.warning(
                "Run %s: model output referenced message id %r outside this batch -- ignored",
                run_row.id, _cap(message_id, _MESSAGE_ID_LOG_MAX_CHARS),
            )
            continue
        if message_id in item_by_uid:
            _logger.warning(
                "Run %s: model output had a duplicate message id %r -- keeping the first", run_row.id, message_id,
            )
            continue
        item_by_uid[message_id] = item

    rows_to_add: List[AutomationItemResult] = []
    for uid in uids:
        source_key = _source_key(mailbox_credential_id, uidvalidity, uid)
        if source_key in existing_keys:
            continue
        item = item_by_uid.get(uid)
        if item is None:
            rows_to_add.append(
                AutomationItemResult(
                    org_id=run_row.org_id,
                    run_id=run_row.id,
                    source_type="email",
                    source_key=source_key,
                    result_type=ITEM_RESULT_TYPE,
                    status="error",
                    needs_attention=True,
                    payload=_error_payload(
                        "The automation's output didn't include this message.",
                        # A model can omit a message id from its envelope after
                        # already drafting a reply for it -- record.action stays
                        # accurate to the mailbox even though the item itself is
                        # an error row, so a retry (see already_drafted_uids)
                        # doesn't try to draft it again (Codex review finding).
                        draft_created=uid in confirmed_draft_message_ids,
                    ),
                )
            )
            continue
        rows_to_add.append(
            AutomationItemResult(
                org_id=run_row.org_id,
                run_id=run_row.id,
                source_type="email",
                source_key=source_key,
                result_type=ITEM_RESULT_TYPE,
                status=item.status,
                needs_attention=_needs_attention_for(item, confirmed_draft_message_ids, failed_tool_message_ids),
                payload=_item_payload(item, confirmed_draft_message_ids),
            )
        )

    if not rows_to_add:
        return
    db.add_all(rows_to_add)
    db.commit()


def _write_error_rows(
    db: Session,
    run_row: Run,
    uids: List[str],
    existing_keys: set,
    mailbox_credential_id: Any,
    uidvalidity: Any,
    *,
    reason: str,
    confirmed_draft_message_ids: frozenset = frozenset(),
) -> None:
    rows_to_add = []
    for uid in uids:
        source_key = _source_key(mailbox_credential_id, uidvalidity, uid)
        if source_key in existing_keys:
            continue
        rows_to_add.append(
            AutomationItemResult(
                org_id=run_row.org_id,
                run_id=run_row.id,
                source_type="email",
                source_key=source_key,
                result_type=ITEM_RESULT_TYPE,
                status="error",
                needs_attention=True,
                # A run that crashed/was cancelled with no parseable output can
                # still have confirmed a real draft for some of these UIDs
                # before it died -- record that so a retry (already_drafted_uids)
                # doesn't re-run email_draft_reply for them (Codex review finding).
                payload=_error_payload(reason, draft_created=uid in confirmed_draft_message_ids),
            )
        )
    if not rows_to_add:
        return
    db.add_all(rows_to_add)
    db.commit()


# ---------------------------------------------------------------------------
# Read APIs (backend main.py)
# ---------------------------------------------------------------------------


def list_automation_results(
    db: Session,
    org_id: int,
    *,
    run_id: Optional[str] = None,
    needs_attention: Optional[bool] = None,
    status: Optional[str] = None,
    result_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[AutomationItemResult], int]:
    """Org-scoped, filtered, paginated -- mirrors `main.py::list_runs`'s
    offset/limit/total convention rather than the spec's `cursor` wording, for
    consistency with the rest of this codebase's list endpoints. `run_id`
    scopes to one run's items (Run Detail's automation-results section);
    still org-filtered even then, so a cross-org run id returns nothing
    rather than leaking another org's results."""
    query = db.query(AutomationItemResult).filter(AutomationItemResult.org_id == org_id)
    if run_id:
        query = query.filter(AutomationItemResult.run_id == run_id)
    if needs_attention is not None:
        query = query.filter(AutomationItemResult.needs_attention == needs_attention)
    if status:
        query = query.filter(AutomationItemResult.status == status)
    if result_type:
        query = query.filter(AutomationItemResult.result_type == result_type)
    total = query.count()
    rows = query.order_by(AutomationItemResult.created_at.desc()).offset(offset).limit(limit).all()
    return rows, total


def summary_for_date(db: Session, org_id: int, day: _date, tz_offset_minutes: int = 0) -> Dict[str, int]:
    """Today's Maintenance Inbox counters for the Activity page (spec 6.2).
    Aggregated in Python over one org's one day of rows -- a small dataset at
    this scale, and avoids depending on SQLite's JSON1 extension being
    available for the payload-embedded fields (classification/priority/
    action.draft_created aren't their own indexed columns).

    `tz_offset_minutes` is the caller's `Date.getTimezoneOffset()` (UTC =
    local + offset): without it, `day` (the caller's local calendar date) got
    bounded as UTC midnight-to-midnight, which is the wrong window for any
    org not in UTC -- e.g. UTC+10 local midnight is still the previous UTC
    date, so its first ~10 hours of rows were excluded (Codex review
    finding). There's no org-level timezone setting in this app, so this
    takes the browser's own offset rather than adding one."""
    local_midnight = datetime(day.year, day.month, day.day)
    start = (local_midnight + timedelta(minutes=tz_offset_minutes)).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    rows = (
        db.query(AutomationItemResult)
        .filter(
            AutomationItemResult.org_id == org_id,
            AutomationItemResult.result_type == ITEM_RESULT_TYPE,
            AutomationItemResult.created_at >= start,
            AutomationItemResult.created_at < end,
        )
        .all()
    )
    # Cheap existence check (not scoped to `day`) so the caller can tell "this
    # org has never used this template" apart from "used it, nothing today" --
    # both would otherwise report emails_read == 0 (Codex review finding:
    # every org saw a Property Maintenance Inbox card, not just ones using it).
    ever_used = (
        db.query(AutomationItemResult.id)
        .filter(AutomationItemResult.org_id == org_id, AutomationItemResult.result_type == ITEM_RESULT_TYPE)
        .first()
        is not None
    )
    summary = {
        "ever_used": ever_used,
        "emails_read": len(rows),
        "maintenance_related": 0,
        "drafts_created": 0,
        "needs_attention": 0,
        "possible_emergency": 0,
        "skipped_non_maintenance": 0,
        "errors": 0,
    }
    for row in rows:
        payload = row.payload or {}
        classification = payload.get("classification")
        if classification in ("maintenance_request", "maintenance_follow_up"):
            summary["maintenance_related"] += 1
        elif classification in ("non_maintenance", "spam_or_automated"):
            summary["skipped_non_maintenance"] += 1
        if (payload.get("action") or {}).get("draft_created"):
            summary["drafts_created"] += 1
        if row.needs_attention:
            summary["needs_attention"] += 1
        if payload.get("priority") == "possible_emergency":
            summary["possible_emergency"] += 1
        if row.status == "error":
            summary["errors"] += 1
    return summary
