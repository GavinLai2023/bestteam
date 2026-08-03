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

from .db.models import AutomationItemResult, Run

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


class Envelope(BaseModel):
    schema_version: int = 1
    result_type: str
    items: List[EnvelopeItem] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


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
    exist."""
    action = item.action.model_dump()
    if action.get("draft_created") and item.message_id.strip() not in confirmed_draft_message_ids:
        action["draft_created"] = False
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


def _needs_attention_for(item: EnvelopeItem, confirmed_draft_message_ids: frozenset = frozenset()) -> bool:
    """The server -- not the model -- enforces the hard rule that
    possible_emergency/unknown priority always needs a human (spec 9.3), that
    an item status of needs_attention/error always does too, and that a
    claimed-but-unconfirmed draft (see `_reconciled_action`) always does too."""
    unconfirmed_draft = (
        item.action.draft_created and item.message_id.strip() not in confirmed_draft_message_ids
    )
    return (
        item.needs_human
        or item.status in ("needs_attention", "error")
        or item.priority in _ALWAYS_NEEDS_ATTENTION_PRIORITIES
        or unconfirmed_draft
    )


def _error_payload(reason: str) -> Dict[str, Any]:
    return {
        "classification": "unknown",
        "category": "unknown",
        "priority": "unknown",
        "summary": "",
        "extracted": {},
        "missing_information": [],
        "risk_reasons": [],
        "action": {"draft_created": False, "draft_type": None},
        "human_reason": _cap(reason, _FIELD_MAX_CHARS),
    }


def normalize_run_result(
    db: Session, run_row: Run, *, confirmed_draft_message_ids: Optional[frozenset] = None
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
    """
    try:
        _normalize(db, run_row, frozenset(confirmed_draft_message_ids or ()))
    except Exception:  # noqa: BLE001 -- normalization must never break a run
        _logger.exception("Automation result normalization failed for run %s", run_row.id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _normalize(db: Session, run_row: Run, confirmed_draft_message_ids: frozenset = frozenset()) -> None:
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

    raw = _extract_json_object(run_row.output or "")
    if raw is None or raw.get("result_type") != RESULT_TYPE_BATCH_MARKER:
        # Not a Property Maintenance Inbox run's output (or the model produced
        # no parseable JSON at all) -- this run never declared itself part of
        # this vertical, so leave it alone entirely.
        return

    try:
        envelope = Envelope.model_validate(raw)
    except ValidationError as exc:
        _logger.warning("Run %s: envelope failed validation: %s", run_row.id, exc)
        _write_error_rows(
            db, run_row, uids, existing_keys, mailbox_credential_id, uidvalidity,
            reason="Result normalization failed: the automation's output didn't match the expected format.",
        )
        return

    item_by_uid: Dict[str, EnvelopeItem] = {}
    for item in envelope.items:
        message_id = item.message_id.strip()
        if message_id not in set(uids):
            _logger.warning(
                "Run %s: model output referenced message id %r outside this batch -- ignored",
                run_row.id, message_id,
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
                    payload=_error_payload("The automation's output didn't include this message."),
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
                needs_attention=_needs_attention_for(item, confirmed_draft_message_ids),
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
                payload=_error_payload(reason),
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


def summary_for_date(db: Session, org_id: int, day: _date) -> Dict[str, int]:
    """Today's Maintenance Inbox counters for the Activity page (spec 6.2).
    Aggregated in Python over one org's one day of rows -- a small dataset at
    this scale, and avoids depending on SQLite's JSON1 extension being
    available for the payload-embedded fields (classification/priority/
    action.draft_created aren't their own indexed columns)."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
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
