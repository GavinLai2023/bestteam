import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Team, Workflow

pytestmark = pytest.mark.unit


def _agent(name, response):
    return Agent(
        name=name,
        role=f"role-{name}",
        goal=f"goal-{name}",
        model=FakeListChatModel(responses=[response]),
    )


class _FakeToolCallingChatModel(FakeMessagesListChatModel):
    """Cycles through scripted AIMessages and accepts `bind_tools` as a no-op,
    so tests can script tool-call responses without a real provider."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def test_stream_emits_agent_started_around_agent_completed_for_simple_agent():
    a = _agent("a", "output from a")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))

    assert [e.type for e in events] == [
        "run_started",
        "agent_started",
        "agent_completed",
        "run_completed",
    ]
    assert events[1].agent == "a"


def test_stream_emits_tool_started_and_completed_around_tool_call():
    def echo_tool(text: str) -> str:
        return f"echoed: {text}"

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            AIMessage(content="The tool said: echoed: hi"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[echo_tool])
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))
    types = [e.type for e in events]

    assert types.index("agent_started") < types.index("tool_started") < types.index("tool_completed") < types.index("agent_completed")
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["tool"] == "echo_tool"
    assert tool_completed.data["success"] is True
    assert "args" not in tool_completed.data
    assert tool_completed.data["summary"] == "echoed: hi"


def test_tool_completed_summary_is_truncated_for_long_result():
    def long_tool(text: str) -> str:
        return "x" * 300

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "long_tool", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[long_tool])
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))
    tool_completed = next(e for e in events if e.type == "tool_completed")

    assert len(tool_completed.data["summary"]) <= 201


def test_failed_tool_call_emits_tool_completed_with_success_false_and_no_exception_detail():
    def failing_tool(text: str) -> str:
        raise RuntimeError("super secret internal detail")

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "failing_tool", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[failing_tool])
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))
    tool_completed = next(e for e in events if e.type == "tool_completed")

    assert tool_completed.data["success"] is False
    assert "super secret internal detail" not in tool_completed.data["summary"]


def test_hierarchical_delegation_emits_events_in_order():
    researcher_model = FakeMessagesListChatModel(responses=[AIMessage(content="research findings")])
    researcher = Agent(name="researcher", role="Researcher", goal="research things", model=researcher_model)

    manager_model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delegate_to_researcher", "args": {"task": "look into X"}, "id": "call_1"}
                ],
            ),
            AIMessage(content="Final report based on: research findings"),
        ]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    workflow = Workflow(name="wf", steps=[team])

    events = list(workflow.stream("do the thing"))
    types = [e.type for e in events]

    assert types.index("delegation_started") < types.index("subagent_started")
    assert types.index("subagent_started") < types.index("agent_started", types.index("subagent_started"))
    subordinate_agent_started = types.index("agent_started", types.index("subagent_started"))
    assert subordinate_agent_started < types.index("subagent_completed")
    assert types.index("subagent_completed") < types.index("delegation_completed")
    assert types.index("delegation_completed") < types.index("agent_completed")

    subagent_started_event = next(e for e in events if e.type == "subagent_started")
    assert subagent_started_event.agent == "researcher"
    delegation_started_event = next(e for e in events if e.type == "delegation_started")
    assert delegation_started_event.agent == "manager"
    assert delegation_started_event.data["to"] == "researcher"


def test_parallel_team_trace_events_do_not_collide_between_branches():
    a = _agent("a", "alpha")
    b = _agent("b", "beta")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.PARALLEL)],
    )

    events = list(workflow.stream("do the thing"))
    started_agents = {e.agent for e in events if e.type == "agent_started"}
    completed_agents = {e.agent for e in events if e.type == "agent_completed"}

    assert started_agents == {"a", "b"}
    assert completed_agents == {"a", "b"}


# --- Email tool trace redaction (spec: subject/body/draft text must never
# reach a tool_completed trace event -- see docs/superpowers/specs/
# 2026-08-02-property-maintenance-inbox-phase-1-development-plan.md section 15.2) --


def _email_tool_call_workflow(tool_fn, tool_name, args, final_message="done"):
    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "call_1"}]),
            AIMessage(content=final_message),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[tool_fn])
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )
    return list(workflow.stream("do the thing"))


def test_email_read_tool_completed_summary_never_contains_body_or_subject():
    def email_read(message_id: str) -> str:
        return (
            "From: tenant@example.com\nTo: pm@example.com\n"
            "Subject: URGENT gas leak smell in kitchen\nDate: today\n\n"
            "Please send someone immediately, my landlord's phone is 555-1234."
        )

    events = _email_tool_call_workflow(email_read, "email_read", {"message_id": "42"})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    summary = tool_completed.data["summary"]
    assert "gas leak" not in summary
    assert "555-1234" not in summary
    assert "tenant@example.com" not in summary
    assert "42" in summary  # the message id itself is fine to record


def test_email_find_tool_completed_summary_never_contains_subject_lines():
    def email_find(query: str = "") -> str:
        return "Found 2 message(s):\n42 · a@b.com · Confidential lease dispute · today\n43 · c@d.com · gas smell · today"

    events = _email_tool_call_workflow(email_find, "email_find", {"query": ""})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    summary = tool_completed.data["summary"]
    assert "lease dispute" not in summary
    assert "gas smell" not in summary
    assert summary == "Found 2 message(s)."


def test_email_draft_reply_tool_completed_summary_never_contains_draft_body():
    def email_draft_reply(message_id: str, body: str) -> str:
        return "Draft reply saved to the 'Drafts' folder (reply to message 42)."

    events = _email_tool_call_workflow(
        email_draft_reply, "email_draft_reply",
        {"message_id": "42", "body": "We will send a plumber tomorrow and cover the cost."},
    )
    tool_completed = next(e for e in events if e.type == "tool_completed")
    summary = tool_completed.data["summary"]
    assert "plumber" not in summary
    assert "cover the cost" not in summary
    assert "42" in summary


def test_non_email_tool_summary_is_unaffected_by_redaction():
    def some_tool(text: str) -> str:
        return f"result: {text}"

    events = _email_tool_call_workflow(some_tool, "some_tool", {"text": "hello"})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["summary"] == "result: hello"


def test_draft_reply_success_is_recorded_with_a_draft_created_outcome():
    def email_draft_reply(message_id: str, body: str) -> str:
        return "Draft reply saved to the 'Drafts' folder (reply to message 42)."

    events = _email_tool_call_workflow(
        email_draft_reply, "email_draft_reply", {"message_id": "42", "body": "ok"},
    )
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["outcome"] == "draft_created"
    assert tool_completed.data["message_id"] == "42"


def test_out_of_batch_rejection_is_not_recorded_as_a_successful_read_or_draft():
    # A UID-scoped tool's rejection text (tools/email_client.py's _OUT_OF_BATCH)
    # must never be mislabeled as "Read message"/"Draft reply saved" -- that
    # would hide a real rejection behind an apparent success in the trace.
    def email_read(message_id: str) -> str:
        return "That message isn't part of this batch of new mail."

    events = _email_tool_call_workflow(email_read, "email_read", {"message_id": "99"})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["outcome"] == "out_of_batch"
    assert "Rejected" in tool_completed.data["summary"]


def test_failed_email_read_still_records_the_message_id():
    # A raised exception (network error, malformed args, etc.) must not lose
    # the UID -- automation_results.py's per-UID needs_attention enforcement
    # needs it to correlate a tool failure back to its message (Codex review
    # finding).
    def email_read(message_id: str) -> str:
        raise RuntimeError("IMAP connection reset")

    events = _email_tool_call_workflow(email_read, "email_read", {"message_id": "42"})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["success"] is False
    assert tool_completed.data["message_id"] == "42"


def test_failed_email_draft_reply_still_records_the_message_id():
    def email_draft_reply(message_id: str, body: str) -> str:
        raise RuntimeError("IMAP connection reset")

    events = _email_tool_call_workflow(
        email_draft_reply, "email_draft_reply", {"message_id": "42", "body": "ok"},
    )
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["success"] is False
    assert tool_completed.data["message_id"] == "42"


def test_failed_email_find_does_not_fabricate_a_message_id():
    # email_find has no single message id to attach (it's a search) -- must
    # not invent one from missing call args.
    def email_find(query: str = "") -> str:
        raise RuntimeError("IMAP connection reset")

    events = _email_tool_call_workflow(email_find, "email_find", {"query": ""})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["success"] is False
    assert "message_id" not in tool_completed.data


def test_failed_non_email_tool_is_unaffected():
    def some_tool(text: str) -> str:
        raise RuntimeError("boom")

    events = _email_tool_call_workflow(some_tool, "some_tool", {"text": "hello"})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["success"] is False
    assert "message_id" not in tool_completed.data


def test_message_id_is_length_bounded_in_the_trace():
    def email_read(message_id: str) -> str:
        return "From: a\nTo: b\nSubject: c\nDate: d\n\nbody"

    huge_id = "x" * 500
    events = _email_tool_call_workflow(email_read, "email_read", {"message_id": huge_id})
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert len(tool_completed.data["message_id"]) <= 65
    assert huge_id not in tool_completed.data["summary"]


def test_message_id_is_stripped_in_the_trace_to_match_the_email_tools_own_normalization():
    # email_client.py's _read_impl/_draft_impl call .strip() on message_id
    # before touching the mailbox. If the trace kept the raw " 42 ", a later
    # normalization pass comparing it against the model's own (stripped)
    # claimed id would see a mismatch and fail to recognize a real draft as
    # confirmed -- risking a duplicate draft on retry (Codex review finding).
    def email_draft_reply(message_id: str, body: str) -> str:
        return "Draft reply saved to the 'Drafts' folder (reply to message 42)."

    events = _email_tool_call_workflow(
        email_draft_reply, "email_draft_reply", {"message_id": " 42 ", "body": "ok"},
    )
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["message_id"] == "42"
