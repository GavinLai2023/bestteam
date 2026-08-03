"""Unit tests for automation_results.py's normalize_run_result: the server-side
parser that turns a Property Maintenance Inbox run's JSON output into
immutable automation_item_results rows (spec sections 5.3, 10, 10.1)."""

import json
from datetime import date as _date, datetime, timezone

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.automation_results import ITEM_RESULT_TYPE, normalize_run_result, summary_for_date
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import AutomationItemResult, Run
from ui.backend.db.orgs import get_or_create_org


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    yield session
    session.close()


def _make_run(db, *, org_id, output, uids, uidvalidity=3, mailbox_credential_id=7, run_id="run-1"):
    run = Run(
        id=run_id,
        workflow="property_maintenance_inbox_demo",
        input="triage",
        output=output,
        status="completed",
        org_id=org_id,
        username="email-trigger",
        trigger_context={
            "trigger_type": "email",
            "mailbox_credential_id": mailbox_credential_id,
            "uidvalidity": uidvalidity,
            "uids": uids,
            "folder": "INBOX",
            "triggered_at": "2026-08-02T00:00:00+00:00",
        },
    )
    db.add(run)
    db.commit()
    return run


def _envelope(items):
    return json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": items,
    })


def _valid_item(message_id="42", **overrides):
    item = {
        "message_id": message_id,
        "classification": "maintenance_request",
        "category": "plumbing",
        "priority": "priority",
        "status": "needs_attention",
        "summary": "Tenant reports a leak.",
        "extracted": {"property_address": "12 Example St", "attachment_mentioned": True},
        "missing_information": ["callback_number"],
        "risk_reasons": [],
        "action": {"draft_created": True, "draft_type": "acknowledgement"},
        "needs_human": True,
        "human_reason": "Active leak.",
    }
    item.update(overrides)
    return item


def test_non_maintenance_output_is_left_alone(db):
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, output="Triaged 3 emails, drafted 1 reply.", uids=[42])
    normalize_run_result(db, run)
    assert db.query(AutomationItemResult).count() == 0


def test_valid_envelope_creates_one_row_per_uid(db):
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, output=_envelope([_valid_item("42")]), uids=[42])
    normalize_run_result(db, run)
    rows = db.query(AutomationItemResult).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.org_id == org.id
    assert row.run_id == run.id
    assert row.result_type == ITEM_RESULT_TYPE
    assert row.status == "needs_attention"
    assert row.needs_attention is True
    assert row.source_key == "mailbox:7:uidvalidity:3:uid:42"
    assert row.payload["extracted"]["property_address"] == "12 Example St"
    assert "body" not in row.payload  # never stores raw email content


def test_json_wrapped_in_markdown_fence_is_parsed(db):
    org = get_or_create_org(db, "acme")
    output = f"Here is the result:\n```json\n{_envelope([_valid_item('42')])}\n```\nDone."
    run = _make_run(db, org_id=org.id, output=output, uids=[42])
    normalize_run_result(db, run)
    assert db.query(AutomationItemResult).count() == 1


def test_missing_uid_gets_synthetic_error_row(db):
    """Spec 10.1: if the model omits a UID it was given, that UID must not
    silently disappear -- it gets a synthetic error/needs_attention row."""
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, output=_envelope([_valid_item("42")]), uids=[42, 43])
    normalize_run_result(db, run)
    rows = {r.source_key: r for r in db.query(AutomationItemResult).all()}
    assert len(rows) == 2
    missing = rows["mailbox:7:uidvalidity:3:uid:43"]
    assert missing.status == "error"
    assert missing.needs_attention is True


def test_out_of_batch_message_id_is_ignored_not_inserted(db):
    """The model can't expand its own scope: an id outside trigger_context's
    UID batch must never produce a row (would let the model forge a source)."""
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, output=_envelope([_valid_item("999")]), uids=[42])
    normalize_run_result(db, run)
    rows = db.query(AutomationItemResult).all()
    assert len(rows) == 1
    assert rows[0].status == "error"  # 42 was never covered by the model's output
    assert rows[0].source_key == "mailbox:7:uidvalidity:3:uid:42"


def test_duplicate_message_id_keeps_first_only(db):
    org = get_or_create_org(db, "acme")
    output = _envelope([
        _valid_item("42", summary="first"),
        _valid_item("42", summary="second"),
    ])
    run = _make_run(db, org_id=org.id, output=output, uids=[42])
    normalize_run_result(db, run)
    rows = db.query(AutomationItemResult).all()
    assert len(rows) == 1
    assert rows[0].payload["summary"] == "first"


def test_invalid_enum_fails_whole_envelope_with_error_rows_for_every_uid(db):
    org = get_or_create_org(db, "acme")
    output = _envelope([_valid_item("42", classification="not_a_real_classification")])
    run = _make_run(db, org_id=org.id, output=output, uids=[42, 43])
    normalize_run_result(db, run)
    rows = db.query(AutomationItemResult).all()
    assert len(rows) == 2
    assert all(r.status == "error" and r.needs_attention for r in rows)


def test_structurally_broken_envelope_fails_safe(db):
    """Valid JSON, declares itself our result type, but 'items' isn't a list
    -- Pydantic validation fails, and every UID in the batch gets an error row
    rather than silently vanishing."""
    org = get_or_create_org(db, "acme")
    output = json.dumps({"result_type": "property_maintenance_email_batch", "items": "not-a-list"})
    run = _make_run(db, org_id=org.id, output=output, uids=[42])
    normalize_run_result(db, run)
    rows = db.query(AutomationItemResult).all()
    assert len(rows) == 1
    assert rows[0].status == "error"


def test_unsupported_schema_version_fails_whole_envelope_with_error_rows(db):
    """A future incompatible schema_version must not be silently accepted as
    version 1 (extra: "ignore" would otherwise drop its unknown fields
    quietly) -- fail the whole envelope, same as an invalid enum
    (Codex review finding)."""
    org = get_or_create_org(db, "acme")
    output = json.dumps({
        "schema_version": 2, "result_type": "property_maintenance_email_batch",
        "items": [_valid_item("42")],
    })
    run = _make_run(db, org_id=org.id, output=output, uids=[42, 43])
    normalize_run_result(db, run)
    rows = db.query(AutomationItemResult).all()
    assert len(rows) == 2
    assert all(r.status == "error" and r.needs_attention for r in rows)


def test_draft_type_is_length_capped(db):
    """Every other free-text field in the payload is capped -- draft_type
    must be too, or a model could smuggle arbitrarily large text into it
    (Codex review finding)."""
    org = get_or_create_org(db, "acme")
    item = _valid_item("42", action={"draft_created": False, "draft_type": "x" * 1000})
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run)
    row = db.query(AutomationItemResult).one()
    assert len(row.payload["action"]["draft_type"]) <= 300


def test_totally_unparseable_output_is_left_alone(db):
    """Truncated/non-JSON text can't be identified as one of our envelopes at
    all -- there's no marker to key off, so this run is left untouched (it
    was never a Property Maintenance Inbox run in the first place)."""
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, output='{"result_type": "property_ma', uids=[42])
    normalize_run_result(db, run)
    assert db.query(AutomationItemResult).count() == 0


def test_possible_emergency_priority_forces_needs_attention_even_if_model_said_no(db):
    """Spec 9.3: possible_emergency/unknown priority ALWAYS needs a human --
    server-enforced, not trusted from the model's own needs_human flag."""
    org = get_or_create_org(db, "acme")
    item = _valid_item("42", priority="possible_emergency", status="processed", needs_human=False)
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run)
    row = db.query(AutomationItemResult).one()
    assert row.needs_attention is True


def test_repeated_normalization_is_idempotent(db):
    """A duplicate completion callback (or a second normalize call for any
    reason) must not create duplicate rows -- (run_id, source_key) unique."""
    org = get_or_create_org(db, "acme")
    run = _make_run(db, org_id=org.id, output=_envelope([_valid_item("42")]), uids=[42])
    normalize_run_result(db, run)
    normalize_run_result(db, run)
    assert db.query(AutomationItemResult).count() == 1


def test_no_trigger_context_is_a_noop(db):
    org = get_or_create_org(db, "acme")
    run = Run(
        id="manual-1", workflow="w", input="hi", output=_envelope([_valid_item("42")]),
        status="completed", org_id=org.id,
    )
    db.add(run)
    db.commit()
    normalize_run_result(db, run)
    assert db.query(AutomationItemResult).count() == 0


def test_summary_and_lists_are_length_capped(db):
    org = get_or_create_org(db, "acme")
    item = _valid_item("42", summary="x" * 1000, missing_information=["y" * 500] * 30)
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run)
    row = db.query(AutomationItemResult).one()
    assert len(row.payload["summary"]) <= 500
    assert len(row.payload["missing_information"]) <= 20
    assert all(len(v) <= 200 for v in row.payload["missing_information"])


def test_claimed_draft_is_downgraded_without_trace_confirmation(db):
    """The model can claim action.draft_created=True for any message id --
    only a confirmed successful email_draft_reply tool call (passed in from
    runtime.py's trace events) should be trusted (Codex review finding)."""
    org = get_or_create_org(db, "acme")
    item = _valid_item("42", needs_human=False, status="processed")
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run)  # no confirmed_draft_message_ids given
    row = db.query(AutomationItemResult).one()
    assert row.payload["action"]["draft_created"] is False
    assert row.needs_attention is True  # unconfirmed draft always needs a human


def test_claimed_draft_is_kept_when_confirmed_by_trace(db):
    org = get_or_create_org(db, "acme")
    item = _valid_item("42", needs_human=False, status="processed")
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run, confirmed_draft_message_ids=frozenset({"42"}))
    row = db.query(AutomationItemResult).one()
    assert row.payload["action"]["draft_created"] is True
    assert row.needs_attention is False


def test_unclaimed_draft_is_upgraded_when_confirmed_by_trace(db):
    """The model can just as easily under-report a draft it actually created
    as over-claim one -- a trace-confirmed email_draft_reply call upgrades a
    claimed-false action.draft_created to true, so the stored result (and the
    daily summary's count) doesn't under-report a real mailbox side effect
    (Codex review finding)."""
    org = get_or_create_org(db, "acme")
    item = _valid_item(
        "42", needs_human=False, status="processed",
        action={"draft_created": False, "draft_type": None},
    )
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run, confirmed_draft_message_ids=frozenset({"42"}))
    row = db.query(AutomationItemResult).one()
    assert row.payload["action"]["draft_created"] is True


def test_failed_tool_call_forces_needs_attention_even_if_model_did_not_flag_it(db):
    """Spec 9.5 'Tool failure -> needs_attention: yes' -- enforced server-side
    from the run's own trace (runtime.py's failed_tool_message_ids), the same
    trust boundary already applied to claimed drafts, so a model that doesn't
    self-report a tool failure for a UID can't hide it (Codex review
    finding)."""
    org = get_or_create_org(db, "acme")
    item = _valid_item("42", needs_human=False, status="processed", priority="routine")
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run, failed_tool_message_ids=frozenset({"42"}))
    row = db.query(AutomationItemResult).one()
    assert row.needs_attention is True


def test_envelope_less_failed_run_with_declared_contract_gets_synthetic_error_rows(db):
    """Spec 10.1: a UID batch must never silently disappear. A run that
    crashed before producing any JSON output looks identical, from the
    output alone, to an unrelated org's non-maintenance email-trigger
    workflow -- trigger_context['result_contract'] (stamped at dispatch time,
    see email_trigger._declares_property_maintenance_contract) is the
    positive signal that this WAS a Property Maintenance Inbox batch, so it
    still gets synthetic error rows instead of vanishing (Codex review
    finding)."""
    org = get_or_create_org(db, "acme")
    run = Run(
        id="run-1", workflow="property_maintenance_inbox_demo", input="triage",
        output="The run failed due to an internal error.", status="failed",
        org_id=org.id, username="email-trigger",
        trigger_context={
            "trigger_type": "email",
            "mailbox_credential_id": 7,
            "uidvalidity": 3,
            "uids": [42, 43],
            "folder": "INBOX",
            "triggered_at": "2026-08-02T00:00:00+00:00",
            "result_contract": "property_maintenance_email_batch",
        },
    )
    db.add(run)
    db.commit()
    normalize_run_result(db, run)
    rows = db.query(AutomationItemResult).all()
    assert len(rows) == 2
    assert all(r.status == "error" and r.needs_attention for r in rows)


def test_envelope_less_failed_run_without_declared_contract_is_still_left_alone(db):
    """Without the dispatch-time marker, an unparseable/failed output stays
    indistinguishable from any other org's unrelated email-trigger workflow
    -- must not start creating rows for it (would regress that workflow, see
    the module docstring's 'Scoping note')."""
    org = get_or_create_org(db, "acme")
    run = _make_run(
        db, org_id=org.id, output="The run failed due to an internal error.", uids=[42], run_id="run-1",
    )
    run.status = "failed"
    db.commit()
    normalize_run_result(db, run)
    assert db.query(AutomationItemResult).count() == 0


def test_summary_respects_the_callers_local_day_via_tz_offset(db):
    """Without tz_offset_minutes, a UTC+10 caller's first ~10 local hours of
    'today' are still the previous UTC date and got dropped from the summary
    -- the browser's own offset (Date.getTimezoneOffset()) fixes this (Codex
    review finding)."""
    org = get_or_create_org(db, "acme")
    run = Run(id="run-1", workflow="w", input="x", status="completed", org_id=org.id)
    db.add(run)
    db.commit()
    row = AutomationItemResult(
        org_id=org.id, run_id=run.id, source_type="email", source_key="k",
        result_type=ITEM_RESULT_TYPE, status="processed", needs_attention=False,
        payload={},
        created_at=datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc),  # 01:00 local in UTC+10, on local Aug 3
    )
    db.add(row)
    db.commit()

    without_offset = summary_for_date(db, org.id, _date(2026, 8, 3))
    assert without_offset["emails_read"] == 0  # UTC-bounded day misses it

    with_offset = summary_for_date(db, org.id, _date(2026, 8, 3), tz_offset_minutes=-600)
    assert with_offset["emails_read"] == 1


def test_unclaimed_draft_is_unaffected_by_confirmation_set(db):
    org = get_or_create_org(db, "acme")
    item = _valid_item("42", needs_human=False, status="processed", action={"draft_created": False, "draft_type": None})
    run = _make_run(db, org_id=org.id, output=_envelope([item]), uids=[42])
    normalize_run_result(db, run)
    row = db.query(AutomationItemResult).one()
    assert row.payload["action"]["draft_created"] is False
    assert row.needs_attention is False
