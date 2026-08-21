"""The `diagnostic` flag on `Pipeline.run/stream` (admin diagnostic re-runs).

With the flag on, the adapter additionally emits what a normal trace deliberately
leaves out -- the exact prompt each agent received, every model turn (including
the one that chose a tool), tool-call arguments, and the full tool result the
model read. With it off (the default) the event stream must stay byte-identical
to before. Email tools keep their redaction either way. See
docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md.
"""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Pipeline, Team
from bestteam.adapters import langgraph_adapter

pytestmark = pytest.mark.unit


class _FakeToolCallingChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _tool_pipeline(tool_fn, tool_name, args, final="The tool said: echoed: hi"):
    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "call_1"}]),
            AIMessage(content=final),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", backstory="story", model=model, tools=[tool_fn])
    return agent, Pipeline(
        name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)]
    )


def _echo(text: str) -> str:
    return f"echoed: {text}"


def test_diagnostic_stream_emits_prompt_turns_args_and_result():
    agent, pipeline = _tool_pipeline(_echo, "_echo", {"text": "hi"})

    events = list(pipeline.stream("do the thing", diagnostic=True))
    types = [e.type for e in events]

    assert types == [
        "run_started",
        "agent_started",
        "agent_prompt",
        "model_turn",
        "tool_started",
        "tool_completed",
        "agent_progress",
        "model_turn",
        "agent_completed",
        "run_completed",
    ]
    prompt = events[2]
    assert prompt.agent == "a"
    assert prompt.data == {"system_prompt": agent.system_prompt(), "input": "do the thing"}

    first_turn = events[3]
    assert first_turn.data == {
        "turn": 1,
        "content": "",
        "tool_calls": [{"name": "_echo", "args": {"text": "hi"}}],
    }
    assert events[4].data == {"tool": "_echo", "args": {"text": "hi"}}
    completed = events[5].data
    assert completed["result"] == "echoed: hi"
    assert completed["summary"] == "echoed: hi"
    assert events[7].data == {"turn": 2, "content": "The tool said: echoed: hi", "tool_calls": []}


def test_default_stream_is_unchanged_by_the_diagnostic_feature():
    _agent, pipeline = _tool_pipeline(_echo, "_echo", {"text": "hi"})

    events = list(pipeline.stream("do the thing"))

    assert not any(e.type in ("agent_prompt", "model_turn") for e in events)
    tool_started = next(e for e in events if e.type == "tool_started")
    assert tool_started.data == {"tool": "_echo"}
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert "args" not in tool_completed.data
    assert "result" not in tool_completed.data


def test_diagnostic_run_path_also_works():
    _agent, pipeline = _tool_pipeline(_echo, "_echo", {"text": "hi"})
    assert pipeline.run("do the thing", diagnostic=True).output == "The tool said: echoed: hi"


def test_email_tools_stay_redacted_in_diagnostic_mode():
    def email_read(message_id: str) -> str:
        return "Subject: URGENT gas leak\n\nPlease send someone, phone 555-1234."

    _agent, pipeline = _tool_pipeline(email_read, "email_read", {"message_id": "42"}, final="done")
    events = list(pipeline.stream("do the thing", diagnostic=True))

    tool_started = next(e for e in events if e.type == "tool_started")
    assert tool_started.data == {"tool": "email_read"}
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert "result" not in tool_completed.data
    assert "gas leak" not in str(tool_completed.data)
    # The model turn that *chose* the tool still shows the call, but the
    # model-authored args are email-tool args (a message id / a draft body),
    # so they are dropped there too.
    turn = next(e for e in events if e.type == "model_turn")
    assert turn.data["tool_calls"] == [{"name": "email_read", "args": None}]


def test_failed_email_tool_stays_redacted_in_diagnostic_mode():
    def email_read(message_id: str) -> str:
        raise RuntimeError("IMAP connection reset: Subject: secret")

    _agent, pipeline = _tool_pipeline(email_read, "email_read", {"message_id": "42"}, final="done")
    events = list(pipeline.stream("do the thing", diagnostic=True))
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["success"] is False
    assert "result" not in tool_completed.data
    assert "secret" not in str(tool_completed.data)


def test_failed_tool_result_in_diagnostic_mode_is_the_error_text_the_model_read():
    def failing(text: str) -> str:
        raise RuntimeError("boom")

    _agent, pipeline = _tool_pipeline(failing, "failing", {"text": "hi"}, final="done")
    events = list(pipeline.stream("do the thing", diagnostic=True))
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["success"] is False
    # The model was told "Error calling tool ..." -- that IS what it read, so
    # the diagnostic result shows it; the business-safe summary stays bland.
    assert tool_completed.data["result"] == "Error calling tool 'failing': boom"
    assert tool_completed.data["summary"] == "Tool call failed"


def test_diagnostic_fields_are_truncated(monkeypatch):
    monkeypatch.setattr(langgraph_adapter, "_MAX_DIAGNOSTIC_CHARS", 50)

    def long_tool(text: str) -> str:
        return "x" * 300

    _agent, pipeline = _tool_pipeline(long_tool, "long_tool", {"text": "hi"}, final="done")
    events = list(pipeline.stream("do the thing", diagnostic=True))
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["result"] == "x" * 50 + "…[truncated]"


def test_diagnostic_tool_args_are_bounded_too(monkeypatch):
    monkeypatch.setattr(langgraph_adapter, "_MAX_DIAGNOSTIC_CHARS", 50)
    args = {"text": "y" * 300, "nested": {"items": ["z" * 300, 7]}}

    _agent, pipeline = _tool_pipeline(_echo, "_echo", args, final="done")
    events = list(pipeline.stream("do the thing", diagnostic=True))

    bounded = {"text": "y" * 50 + "…[truncated]", "nested": {"items": ["z" * 50 + "…[truncated]", 7]}}
    assert next(e for e in events if e.type == "tool_started").data["args"] == bounded
    assert next(e for e in events if e.type == "model_turn").data["tool_calls"][0]["args"] == bounded


def test_hierarchical_subordinate_inherits_the_diagnostic_flag():
    researcher_model = FakeMessagesListChatModel(responses=[AIMessage(content="research findings")])
    researcher = Agent(name="researcher", role="Researcher", goal="research things", model=researcher_model)
    manager_model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "delegate_to_researcher", "args": {"task": "look into X"}, "id": "c1"}],
            ),
            AIMessage(content="Final report"),
        ]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate", model=manager_model)
    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    pipeline = Pipeline(name="wf", steps=[team])

    events = list(pipeline.stream("do the thing", diagnostic=True))

    prompts = {e.agent: e.data for e in events if e.type == "agent_prompt"}
    assert set(prompts) == {"manager", "researcher"}
    assert prompts["researcher"]["input"] == "look into X"
    assert "delegate_to_researcher" in prompts["manager"]["system_prompt"]
    manager_turns = [e for e in events if e.type == "model_turn" and e.agent == "manager"]
    assert manager_turns[0].data["tool_calls"] == [
        {"name": "delegate_to_researcher", "args": {"task": "look into X"}}
    ]
