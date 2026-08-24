"""Run-lifecycle regression tests for CR-003 (worker failure always yields a
terminal event) and CR-010 (RunRegistry subscribe/publish are serialised)."""

import threading
import time

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")

from bestteam.core.trace import TraceEvent
from ui.backend import runtime
from ui.backend.registry import RunRegistry


# --- CR-003 -----------------------------------------------------------------


class _BoomPipeline:
    """Stand-in whose .stream() raises like a compile failure escaping
    Pipeline.stream() (which compiles before its own BestTeamError handler)."""

    name = "boom_wf"

    def stream(self, *args, **kwargs):
        raise RuntimeError("internal compile detail that must not leak")


def test_run_in_background_publishes_terminal_event_on_worker_exception():
    run = runtime.registry.create("boom_wf", "input")

    # engine=None / user_id=None -> no DB, no memory: isolates the failure path.
    runtime.run_in_background(run.id, _BoomPipeline(), "input")

    stored = runtime.registry.get(run.id)
    assert stored.status == "failed"
    failures = [e for e in stored.events if e["type"] == "run_failed"]
    assert len(failures) == 1
    # Sanitized: the internal exception text is not leaked to subscribers.
    assert "internal compile detail" not in failures[0]["data"]


class _CompletesThenRaisesPipeline:
    """Yields run_completed, then raises -- like a post-completion side effect
    (e.g. memory recording) failing after the run already succeeded."""

    name = "wf"

    def stream(self, *args, **kwargs):
        yield TraceEvent(type="run_completed", pipeline="wf", data="done")
        raise RuntimeError("post-completion failure")


def test_post_completion_failure_does_not_publish_second_terminal_event():
    # CR-003: a failure AFTER a terminal event was already published must not
    # flip the run to failed or emit a second terminal event.
    run = runtime.registry.create("wf", "input")

    runtime.run_in_background(run.id, _CompletesThenRaisesPipeline(), "input")

    stored = runtime.registry.get(run.id)
    assert stored.status == "completed"
    terminal = [e for e in stored.events if e["type"] in ("run_completed", "run_failed")]
    assert [e["type"] for e in terminal] == ["run_completed"]


# --- Property Maintenance Inbox: normalization must commit before publish ---


def test_normalize_run_result_commits_before_the_terminal_event_is_published(tmp_path, monkeypatch):
    """A WS subscriber (RunDetail) refetches automation-results the instant it
    observes run_completed/run_failed -- if that event were published before
    normalize_run_result's own commit, the refetch could race ahead and see
    an empty result set (Codex review finding)."""
    import json

    from bestteam import AgentSpec, Specification, TeamSpec, PipelineSpec, validate_specification

    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run
    from ui.backend.runtime import registry, run_in_background

    envelope = json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": "42", "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed", "summary": "s",
            "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": False, "draft_type": None},
            "needs_human": False, "human_reason": "",
        }],
    })
    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model=f"fake:{envelope}")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    wf = validate_specification(spec, source=tmp_path / "w.yaml")

    run = registry.create("w", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="w", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context={
                "trigger_type": "email", "mailbox_credential_id": 1, "uidvalidity": 1,
                "uids": [42], "folder": "INBOX", "triggered_at": "2026-08-02T00:00:00+00:00",
            },
        ))
        s.commit()

    rows_seen_at_publish_time = []
    orig_publish = registry.publish

    def spy_publish(run_id, event):
        if event.get("type") == "run_completed":
            with Session() as s:
                rows_seen_at_publish_time.append(
                    s.query(AutomationItemResult).filter_by(run_id=run_id).count()
                )
        orig_publish(run_id, event)

    monkeypatch.setattr(registry, "publish", spy_publish)

    run_in_background(run.id, wf, "in", engine=engine, org_id=1, username="email-trigger")

    assert rows_seen_at_publish_time == [1]  # already committed by the time run_completed was published


def test_out_of_batch_tool_outcome_forces_needs_attention_even_if_the_model_did_not_flag_it(tmp_path):
    """The adapter reports a rejected/not_found email tool call as
    `success: True` (the call itself didn't raise) with an `outcome` that
    says otherwise -- runtime.py's failed_tool_message_ids collector
    previously only checked `success is False`, so a model that didn't
    self-report the rejection in its envelope hid it (Codex review
    finding)."""
    import json

    from bestteam.core.trace import TraceEvent as _TE
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run
    from ui.backend.runtime import registry, run_in_background

    envelope = json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": "42", "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed", "summary": "s",
            "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": False, "draft_type": None},
            "needs_human": False, "human_reason": "",
        }],
    })

    class _RejectedToolThenCompletesPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield _TE(type="run_started", pipeline="wf", data=None)
            yield _TE(
                type="tool_completed", pipeline="wf", agent="a",
                data={"tool": "email_read", "success": True, "outcome": "not_found", "message_id": "42"},
            )
            yield _TE(type="run_completed", pipeline="wf", data=envelope)

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="wf", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    run_in_background(run.id, _RejectedToolThenCompletesPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    with Session() as s:
        row = s.query(AutomationItemResult).filter_by(run_id=run.id).one()
    # The envelope itself claims a routine, non-attention-needing item --
    # only the trace's own not_found outcome forces this.
    assert row.needs_attention is True


def test_failed_attachment_read_forces_needs_attention_for_its_message(tmp_path):
    """The failed-tool collector is an enumeration of tool names, so a new
    email tool is silently exempt from spec 9.5's "tool failure ->
    needs_attention" until it is added -- nothing errors, the escalation just
    stops happening for that tool."""
    import json

    from bestteam.core.trace import TraceEvent as _TE
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run
    from ui.backend.runtime import registry, run_in_background

    envelope = json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": "42", "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed", "summary": "s",
            "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": False, "draft_type": None},
            "needs_human": False, "human_reason": "",
        }],
    })

    class _FailedAttachmentReadPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield _TE(type="run_started", pipeline="wf", data=None)
            yield _TE(
                type="tool_completed", pipeline="wf", agent="a",
                data={
                    "tool": "email_read_attachment", "success": False,
                    "summary": "Tool call failed", "message_id": "42",
                },
            )
            yield _TE(type="run_completed", pipeline="wf", data=envelope)

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="wf", input="in", status="running", org_id=1,
            username="email-trigger", trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    run_in_background(run.id, _FailedAttachmentReadPipeline(), "in", engine=engine,
                      org_id=1, username="email-trigger")

    with Session() as s:
        row = s.query(AutomationItemResult).filter_by(run_id=run.id).one()
    # The envelope claims a routine, processed item; only the trace's own
    # failure forces a human to look at the message whose quote went unread.
    assert row.needs_attention is True


def _triggered_run_context(uids):
    return {
        "trigger_type": "email", "mailbox_credential_id": 1, "uidvalidity": 1,
        "uids": uids, "folder": "INBOX", "triggered_at": "2026-08-02T00:00:00+00:00",
        "result_contract": "property_maintenance_email_batch",
    }


def test_cancelled_triggered_run_gets_synthetic_error_rows_for_its_batch(tmp_path):
    """Spec 10.1: a UID batch must never silently disappear from
    Needs-attention -- including when the run is cancelled, not just failed
    outright. _mark_cancelled previously never normalized the run at all
    (Codex review finding)."""
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run
    from ui.backend.runtime import registry, run_in_background

    class _NeverStreamedPipeline:
        name = "w"

        def stream(self, *args, **kwargs):
            raise AssertionError("must not stream once cancellation was requested up front")

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("w", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="w", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42, 43]),
        ))
        s.commit()
    registry.request_cancel(run.id)

    run_in_background(run.id, _NeverStreamedPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    with Session() as s:
        rows = s.query(AutomationItemResult).filter_by(run_id=run.id).all()
    assert len(rows) == 2
    assert all(r.status == "error" and r.needs_attention for r in rows)


def test_worker_exception_before_any_event_still_normalizes_the_triggered_batch(tmp_path):
    """Same spec 10.1 guarantee as above, for a run that crashes (e.g. a
    compile failure) before pipeline.stream() ever yields a single event --
    the outer except-Exception fallback previously never normalized either
    (Codex review finding)."""
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run
    from ui.backend.runtime import registry, run_in_background

    class _BoomTriggeredPipeline:
        name = "boom_wf"

        def stream(self, *args, **kwargs):
            raise RuntimeError("internal compile detail")

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("boom_wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="boom_wf", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    run_in_background(run.id, _BoomTriggeredPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    with Session() as s:
        rows = s.query(AutomationItemResult).filter_by(run_id=run.id).all()
    assert len(rows) == 1
    assert rows[0].status == "error" and rows[0].needs_attention is True


def test_pm_contract_run_redacts_raw_agent_output_from_publish_and_persisted_trace(tmp_path, monkeypatch):
    """A property-maintenance run's agent output is derived from customer
    email content (the envelope's free-text fields can quote it directly) --
    the same trust boundary that already redacts tool_completed data for
    email_find/email_read/email_draft_reply must also cover agent_completed
    and the terminal run_completed event's raw text, or it leaks unredacted
    into live WS broadcasts and persisted trace_events/runs.output (Codex
    review finding). The structured result must still be correctly derived
    from the real (unredacted) text -- the automation_item_results row below
    proves normalization still saw it."""
    import json

    from bestteam.core.trace import TraceEvent as _TE
    from helpers import make_concurrent_safe_engine
    from ui.backend import runtime
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run, TraceEventRecord
    from ui.backend.runtime import registry, run_in_background

    envelope = json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": "42", "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed",
            "summary": "Tenant says: pipe under my sink burst, water everywhere, call 555-1234",
            "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": False, "draft_type": None},
            "needs_human": False, "human_reason": "",
        }],
    })

    class _TwoAgentPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield _TE(type="run_started", pipeline="wf", data=None)
            yield _TE(type="agent_completed", pipeline="wf", agent="intake", data="raw email body quoted here")
            yield _TE(type="agent_completed", pipeline="wf", agent="responder", data=envelope)
            yield _TE(type="run_completed", pipeline="wf", data=envelope)

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="wf", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    published_data = []
    orig_publish = registry.publish

    def spy_publish(run_id, event):
        if event.get("type") in ("agent_completed", "run_completed"):
            published_data.append(event.get("data"))
        orig_publish(run_id, event)

    monkeypatch.setattr(registry, "publish", spy_publish)

    run_in_background(run.id, _TwoAgentPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    assert published_data == [runtime._PM_TRACE_REDACTED] * 3  # 2 agent_completed + 1 run_completed

    with Session() as s:
        row = s.get(Run, run.id)
        assert row.output == runtime._PM_TRACE_REDACTED
        trace_rows = s.query(TraceEventRecord).filter_by(run_id=run.id, type="agent_completed").all()
        assert all(json.loads(r.data) == runtime._PM_TRACE_REDACTED for r in trace_rows)
        # Normalization still parsed the REAL (unredacted) envelope.
        item = s.query(AutomationItemResult).filter_by(run_id=run.id).one()
        assert item.status == "processed" and item.needs_attention is False


def test_pm_contract_run_redacts_hierarchical_delegate_events(tmp_path, monkeypatch):
    """If a declared maintenance pipeline uses HIERARCHICAL mode, the
    manager/subordinate delegate exchange (delegation_started/subagent_started's
    `task_summary`, subagent_completed/delegation_completed's `summary`)
    carries the same customer-email-derived text as agent_completed/
    run_completed -- the redaction boundary previously covered only the
    latter two, leaking the former around it (Codex review finding)."""
    import json

    from bestteam.core.trace import TraceEvent as _TE
    from helpers import make_concurrent_safe_engine
    from ui.backend import runtime
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import Run, TraceEventRecord
    from ui.backend.runtime import registry, run_in_background

    envelope = json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": "42", "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed",
            "summary": "ok", "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": False, "draft_type": None},
            "needs_human": False, "human_reason": "",
        }],
    })

    class _HierarchicalPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield _TE(type="run_started", pipeline="wf", data=None)
            yield _TE(
                type="delegation_started", pipeline="wf", agent="manager",
                data={"to": "responder", "task_summary": "tenant says pipe burst, call 555-1234"},
            )
            yield _TE(
                type="subagent_started", pipeline="wf", agent="responder",
                data={"task_summary": "tenant says pipe burst, call 555-1234"},
            )
            yield _TE(
                type="subagent_completed", pipeline="wf", agent="responder",
                data={"success": True, "summary": "drafted reply quoting tenant's phone 555-1234"},
            )
            yield _TE(
                type="delegation_completed", pipeline="wf", agent="manager",
                data={"to": "responder", "summary": "drafted reply quoting tenant's phone 555-1234"},
            )
            yield _TE(type="run_completed", pipeline="wf", data=envelope)

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="wf", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    run_in_background(run.id, _HierarchicalPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    with Session() as s:
        trace_rows = (
            s.query(TraceEventRecord)
            .filter(TraceEventRecord.run_id == run.id)
            .filter(TraceEventRecord.type.in_(
                ["delegation_started", "subagent_started", "subagent_completed", "delegation_completed"]
            ))
            .all()
        )
        assert len(trace_rows) == 4
        for row in trace_rows:
            assert json.loads(row.data) == runtime._PM_TRACE_REDACTED

    live_events = registry.get(run.id).events
    for event in live_events:
        if event["type"] in ("delegation_started", "subagent_started", "subagent_completed", "delegation_completed"):
            assert event["data"] == runtime._PM_TRACE_REDACTED


def test_pm_contract_run_redacts_the_manager_own_delegate_tool_completed_event(tmp_path, monkeypatch):
    """The manager's `delegate_to_<name>` call also produces its OWN
    `tool_completed` event (`adapters/langgraph_adapter.py`'s generic
    tool-calling loop, separate from the `on_event`-driven subagent_completed/
    delegation_completed events) whose `summary` is the same raw subordinate
    output -- this second event path was still leaking it unredacted (Codex
    review finding). A non-delegate tool_completed (e.g. email_read) must stay
    untouched."""
    import json

    from bestteam.core.trace import TraceEvent as _TE
    from helpers import make_concurrent_safe_engine
    from ui.backend import runtime
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import Run, TraceEventRecord
    from ui.backend.runtime import registry, run_in_background

    envelope = json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": "42", "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed",
            "summary": "ok", "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": False, "draft_type": None},
            "needs_human": False, "human_reason": "",
        }],
    })

    class _HierarchicalPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield _TE(type="run_started", pipeline="wf", data=None)
            yield _TE(
                type="tool_completed", pipeline="wf", agent="intake",
                data={"tool": "email_read", "success": True, "outcome": "read", "message_id": "42"},
            )
            yield _TE(
                type="tool_completed", pipeline="wf", agent="manager",
                data={
                    "tool": "delegate_to_responder", "success": True, "duration_ms": 5,
                    "summary": "drafted reply quoting tenant's phone 555-1234",
                },
            )
            yield _TE(type="run_completed", pipeline="wf", data=envelope)

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="wf", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    run_in_background(run.id, _HierarchicalPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    with Session() as s:
        trace_rows = (
            s.query(TraceEventRecord)
            .filter(TraceEventRecord.run_id == run.id, TraceEventRecord.type == "tool_completed")
            .order_by(TraceEventRecord.seq)
            .all()
        )
        assert len(trace_rows) == 2
        # email_read (not a delegate call) is untouched.
        assert json.loads(trace_rows[0].data) == {
            "tool": "email_read", "success": True, "outcome": "read", "message_id": "42",
        }
        # delegate_to_responder is redacted.
        assert json.loads(trace_rows[1].data) == runtime._PM_TRACE_REDACTED

    live_events = registry.get(run.id).events
    delegate_events = [e for e in live_events if e["type"] == "tool_completed" and e.get("agent") == "manager"]
    assert len(delegate_events) == 1
    assert delegate_events[0]["data"] == runtime._PM_TRACE_REDACTED


def test_pm_contract_run_empties_unverified_grounding_labels_but_keeps_counts(tmp_path, monkeypatch):
    """`grounding_checked.unverified` is model-written text lifted verbatim
    from a knowledge-base agent's final answer -- a [source: ...] citation
    label the model wrote, which for a property-maintenance run can carry
    customer-email-derived content the same way agent_completed's raw text
    does. It isn't covered by _PM_REDACTED_EVENT_TYPES's wholesale
    replacement, so it must be scrubbed on its own: the four integer counts
    survive (they can't carry content, and they're the whole point of the
    event for an operator watching automated replies), but `unverified` is
    emptied (Codex review finding)."""
    import json

    from bestteam.core.trace import TraceEvent as _TE
    from helpers import make_concurrent_safe_engine
    from ui.backend import runtime
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import Run, TraceEventRecord
    from ui.backend.runtime import registry, run_in_background

    envelope = json.dumps({
        "schema_version": 1,
        "result_type": "property_maintenance_email_batch",
        "items": [{
            "message_id": "42", "classification": "maintenance_request", "category": "plumbing",
            "priority": "routine", "status": "processed",
            "summary": "ok", "extracted": {}, "missing_information": [], "risk_reasons": [],
            "action": {"draft_created": False, "draft_type": None},
            "needs_human": False, "human_reason": "",
        }],
    })

    class _GroundedPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield _TE(type="run_started", pipeline="wf", data=None)
            yield _TE(
                type="grounding_checked", pipeline="wf", agent="responder",
                data={
                    "searches": 1, "hit_count": 3, "cited": 2, "verified": 1,
                    "unverified": ["tenant's lease at 42 Elm St, p.99"],
                },
            )
            yield _TE(type="run_completed", pipeline="wf", data=envelope)

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="wf", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    run_in_background(run.id, _GroundedPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    expected = {"searches": 1, "hit_count": 3, "cited": 2, "verified": 1, "unverified": []}

    with Session() as s:
        row = (
            s.query(TraceEventRecord)
            .filter_by(run_id=run.id, type="grounding_checked")
            .one()
        )
        assert json.loads(row.data) == expected

    live_events = [e for e in registry.get(run.id).events if e["type"] == "grounding_checked"]
    assert len(live_events) == 1
    assert live_events[0]["data"] == expected


def test_non_pm_run_keeps_grounding_checked_labels_intact(tmp_path):
    """The scrubbing above is scoped to a property-maintenance run only --
    an ordinary run (no trigger_context, or a trigger_context without the
    maintenance result_contract marker) must see `unverified` unchanged,
    same as every other PM redaction branch."""
    import json

    from bestteam.core.trace import TraceEvent as _TE
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import TraceEventRecord
    from ui.backend.runtime import registry, run_in_background

    class _GroundedPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield _TE(type="run_started", pipeline="wf", data=None)
            yield _TE(
                type="grounding_checked", pipeline="wf", agent="responder",
                data={
                    "searches": 1, "hit_count": 3, "cited": 2, "verified": 1,
                    "unverified": ["handbook.pdf, p.99"],
                },
            )
            yield _TE(type="run_completed", pipeline="wf", data="all done")

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("wf", "in", org_id=1, username="alice")

    run_in_background(run.id, _GroundedPipeline(), "in", engine=engine, org_id=1, username="alice")

    expected = {"searches": 1, "hit_count": 3, "cited": 2, "verified": 1, "unverified": ["handbook.pdf, p.99"]}

    with Session() as s:
        row = (
            s.query(TraceEventRecord)
            .filter_by(run_id=run.id, type="grounding_checked")
            .one()
        )
        assert json.loads(row.data) == expected

    live_events = [e for e in registry.get(run.id).events if e["type"] == "grounding_checked"]
    assert len(live_events) == 1
    assert live_events[0]["data"] == expected


def test_cancelled_triggered_run_normalizes_before_publishing_the_cancellation_event(tmp_path, monkeypatch):
    """Same ordering guarantee as the normal terminal path
    (test_normalize_run_result_commits_before_the_terminal_event_is_published
    above), for a cancelled run -- _mark_cancelled previously published
    run_cancelled BEFORE committing/normalizing, so a live subscriber's
    refetch could race ahead of the synthetic error rows (Codex review
    finding)."""
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run
    from ui.backend.runtime import registry, run_in_background

    class _NeverStreamedPipeline:
        name = "w"

        def stream(self, *args, **kwargs):
            raise AssertionError("must not stream once cancellation was requested up front")

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("w", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="w", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()
    registry.request_cancel(run.id)

    rows_seen_at_publish_time = []
    orig_publish = registry.publish

    def spy_publish(run_id, event):
        if event.get("type") == "run_cancelled":
            with Session() as s:
                rows_seen_at_publish_time.append(
                    s.query(AutomationItemResult).filter_by(run_id=run_id).count()
                )
        orig_publish(run_id, event)

    monkeypatch.setattr(registry, "publish", spy_publish)

    run_in_background(run.id, _NeverStreamedPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    assert rows_seen_at_publish_time == [1]  # already committed by the time run_cancelled was published


def test_worker_exception_before_any_event_normalizes_before_publishing_run_failed(tmp_path, monkeypatch):
    """Same ordering guarantee, for a pre-stream crash -- the outer
    except-Exception fallback previously published run_failed BEFORE
    committing/normalizing (Codex review finding)."""
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import AutomationItemResult, Run
    from ui.backend.runtime import registry, run_in_background

    class _BoomTriggeredPipeline:
        name = "boom_wf"

        def stream(self, *args, **kwargs):
            raise RuntimeError("internal compile detail")

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    run = registry.create("boom_wf", "in", org_id=1, username="email-trigger")
    with Session() as s:
        s.add(Run(
            id=run.id, pipeline="boom_wf", input="in", status="running", org_id=1, username="email-trigger",
            trigger_context=_triggered_run_context([42]),
        ))
        s.commit()

    rows_seen_at_publish_time = []
    orig_publish = registry.publish

    def spy_publish(run_id, event):
        if event.get("type") == "run_failed":
            with Session() as s:
                rows_seen_at_publish_time.append(
                    s.query(AutomationItemResult).filter_by(run_id=run_id).count()
                )
        orig_publish(run_id, event)

    monkeypatch.setattr(registry, "publish", spy_publish)

    run_in_background(run.id, _BoomTriggeredPipeline(), "in", engine=engine, org_id=1, username="email-trigger")

    assert rows_seen_at_publish_time == [1]  # already committed by the time run_failed was published


def test_commit_failure_on_terminal_status_still_publishes_a_run_failed_event(tmp_path, monkeypatch):
    """terminal_seen was previously set to True BEFORE the terminal status
    commit, so a DB error committing that status made the except-Exception
    fallback believe a terminal event had already been published and skip
    its own run_failed fallback -- leaving the run looking permanently
    'running' to any live subscriber (Codex review finding)."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession

    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db
    from ui.backend.db.models import Run
    from ui.backend.runtime import registry, run_in_background

    class _CompletesPipeline:
        name = "wf"

        def stream(self, *args, **kwargs):
            yield TraceEvent(type="run_started", pipeline="wf", data=None)
            yield TraceEvent(type="run_completed", pipeline="wf", data="done")

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    run = registry.create("wf", "in")

    orig_commit = SASession.commit
    state = {"raised": False}

    def flaky_commit(self):
        for obj in list(self.dirty):
            if isinstance(obj, Run) and obj.status in ("completed", "failed") and not state["raised"]:
                state["raised"] = True
                raise OperationalError("boom", {}, Exception("boom"))
        return orig_commit(self)

    monkeypatch.setattr(SASession, "commit", flaky_commit)

    run_in_background(run.id, _CompletesPipeline(), "in", engine=engine)

    stored = registry.get(run.id)
    failures = [e for e in stored.events if e["type"] == "run_failed"]
    assert len(failures) == 1
    completions = [e for e in stored.events if e["type"] == "run_completed"]
    assert completions == []  # the terminal commit never actually landed


# --- CR-010 -----------------------------------------------------------------


def test_publish_is_blocked_while_registry_lock_is_held():
    # Proves publish() acquires the shared lock, so it cannot interleave with
    # subscribe()'s replay+registration critical section (the lost-event race).
    reg = RunRegistry()
    run = reg.create("wf", "in")

    reg._lock.acquire()
    done = []

    def _publish():
        reg.publish(run.id, {"type": "agent_completed"})
        done.append(True)

    worker = threading.Thread(target=_publish)
    worker.start()
    try:
        time.sleep(0.05)
        assert done == [], "publish should block while the registry lock is held"
    finally:
        reg._lock.release()

    worker.join(timeout=2)
    assert done == [True]
    assert any(e["type"] == "agent_completed" for e in reg.get(run.id).events)


def test_subscribe_replays_history_then_receives_live_events():
    import asyncio

    reg = RunRegistry()
    run = reg.create("wf", "in")
    reg.publish(run.id, {"type": "run_started"})  # before any subscriber

    async def go():
        queue = reg.subscribe(run.id)
        assert queue.get_nowait()["type"] == "run_started"  # replayed under lock
        reg.publish(run.id, {"type": "agent_completed"})
        await asyncio.sleep(0)  # let call_soon_threadsafe deliver
        assert queue.get_nowait()["type"] == "agent_completed"

    asyncio.run(go())
