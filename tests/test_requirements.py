from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from bestteam import Requirements, generate_requirements

pytestmark = pytest.mark.unit


class _FakeAnalystChatModel(BaseChatModel):
    """Cycles through pre-scripted Requirements objects via `with_structured_output`,
    independent of `bind_tools`/tool-calling -- mirrors `_FakeArchitectChatModel`
    in tests/test_specification.py.

    The response index lives on the instance (not a `with_structured_output`-local
    closure) so it still advances correctly if a caller builds a fresh
    `with_structured_output(...)` runnable on each retry, matching how a real
    `BaseChatModel`'s stateless `with_structured_output` behaves."""

    responses: List[Any] = Field(default_factory=list)
    call_index: List[int] = Field(default_factory=lambda: [0])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    @property
    def _llm_type(self) -> str:
        return "fake-analyst"

    def with_structured_output(self, schema, **kwargs):
        responses = self.responses
        state = self.call_index

        def _invoke(_input):
            i = min(state[0], len(responses) - 1)
            state[0] += 1
            item = responses[i]
            if isinstance(item, BaseException):
                raise item
            return item

        return RunnableLambda(_invoke)


def test_generate_requirements_returns_structured_output():
    expected = Requirements(
        summary="They want faster email support.",
        pain_points=["Email replies take too long"],
        goals=["Answer customer emails within an hour"],
        success_criteria=["Average reply time under 1 hour"],
        constraints=["Must stay in English"],
    )
    model = _FakeAnalystChatModel(responses=[expected])

    result = generate_requirements(model, "Our support inbox is overwhelming.", "We answer emails manually.")

    assert result == expected


def test_generate_requirements_includes_feedback_in_prompt():
    seen_messages = []

    class _RecordingModel(_FakeAnalystChatModel):
        def with_structured_output(self, schema, **kwargs):
            def _invoke(messages):
                seen_messages.append(messages)
                return self.responses[0]

            return RunnableLambda(_invoke)

    expected = Requirements(summary="Updated summary")
    model = _RecordingModel(responses=[expected])

    result = generate_requirements(model, "Help with support.", "Manual today.", feedback="We also use Zendesk.")

    assert result == expected
    assert "We also use Zendesk." in seen_messages[0][1].content


def test_generate_requirements_with_fake_model_raises_clear_error():
    # A `fake:` chat model can't do structured output; instead of the cryptic
    # "with_structured_output is not implemented for this model", the customer
    # should get a clear, actionable message.
    import pytest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from bestteam.exceptions import ConfigurationError

    fake = FakeListChatModel(responses=["anything"])
    with pytest.raises(ConfigurationError, match="real AI model"):
        generate_requirements(fake, "Help me with my email.")


class _ThinkingModeRejectsForcedToolChoice(BaseChatModel):
    """Simulates a reasoning/"thinking mode" model (e.g. DeepSeek's reasoning
    models) that rejects a forced `tool_choice`, matching the real provider
    error `generate_requirements` must fall back past
    (see `core/_structured_output.py`)."""

    response: Any = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    @property
    def _llm_type(self) -> str:
        return "fake-thinking-mode"

    def with_structured_output(self, schema, *, method="function_calling", **kwargs):
        if method == "function_calling":

            def _reject(_input):
                raise Exception("Thinking mode does not support this tool_choice")

            return RunnableLambda(_reject)
        response = self.response
        return RunnableLambda(lambda _input: response)


def test_generate_requirements_falls_back_to_json_mode_when_model_rejects_forced_tool_choice():
    expected = Requirements(summary="Updated summary")
    model = _ThinkingModeRejectsForcedToolChoice(response=expected)

    result = generate_requirements(model, "Help me with my email.")

    assert result == expected


def test_generate_requirements_retries_when_completion_fails_to_parse():
    # Mirrors generate_specification's OutputParserException handling
    # (tests/test_specification.py) -- the Business Analyst's completion can
    # fail to even fit the Requirements schema, which with_structured_output
    # raises before we ever get a Requirements to return.
    from langchain_core.exceptions import OutputParserException

    expected = Requirements(summary="Updated summary")
    bad_completion = '{"summary": 123}'
    parse_failure = OutputParserException(
        f"Failed to parse Requirements from completion {bad_completion}. Got: 1 validation error",
        llm_output=bad_completion,
    )
    model = _FakeAnalystChatModel(responses=[parse_failure, expected])

    result = generate_requirements(model, "Help me with my email.")

    assert result == expected


def test_generate_requirements_raises_after_max_attempts_on_repeated_parse_failure():
    from langchain_core.exceptions import OutputParserException

    from bestteam.exceptions import ConfigurationError

    parse_failure = OutputParserException(
        "Failed to parse Requirements from completion {}. Got: field required", llm_output="{}"
    )
    model = _FakeAnalystChatModel(responses=[parse_failure])

    with pytest.raises(ConfigurationError, match="could not produce valid Requirements"):
        generate_requirements(model, "Help me with my email.", max_attempts=2)


def test_requirements_to_prompt_renders_sections():
    requirements = Requirements(
        summary="Faster support.",
        pain_points=["Slow replies"],
        goals=["Reply within an hour"],
        success_criteria=["Median reply time < 1h"],
        constraints=["English only"],
    )

    prompt = requirements.to_prompt()

    assert "Faster support." in prompt
    assert "Pain points:" in prompt
    assert "- Slow replies" in prompt
    assert "Goals:" in prompt
    assert "Constraints:" in prompt


def test_requirements_to_prompt_omits_empty_sections():
    requirements = Requirements(summary="Just a summary.")

    prompt = requirements.to_prompt()

    assert prompt == "Just a summary."


def test_generate_requirements_includes_the_current_understanding_in_prompt():
    """A refinement round re-derives from the customer's *edited* summary, not
    from the original intent alone -- otherwise each round forgets the last."""
    seen_messages = []

    class _RecordingModel(_FakeAnalystChatModel):
        def with_structured_output(self, schema, **kwargs):
            def _invoke(messages):
                seen_messages.append(messages)
                return self.responses[0]

            return RunnableLambda(_invoke)

    model = _RecordingModel(responses=[Requirements(summary="Updated summary")])
    current = Requirements(
        summary="They want faster email support.",
        goals=["Answer customer emails within an hour"],
        constraints=["Never quote a refund amount"],
    )

    generate_requirements(model, "Help with support.", "Manual today.", current=current)

    content = seen_messages[0][1].content
    assert "Answer customer emails within an hour" in content
    assert "Never quote a refund amount" in content


def _recording_model(seen_messages):
    class _RecordingModel(_FakeAnalystChatModel):
        def with_structured_output(self, schema, **kwargs):
            def _invoke(messages):
                seen_messages.append(messages)
                return self.responses[0]

            return RunnableLambda(_invoke)

    return _RecordingModel(responses=[Requirements(summary="Updated summary")])


def test_generate_requirements_renders_answers_paired_with_questions():
    from bestteam import QuestionAnswer

    seen_messages = []
    model = _recording_model(seen_messages)
    current = Requirements(clarifying_questions=["How many emails per day?"])

    generate_requirements(
        model,
        "Help with support.",
        "",
        current=current,
        answers=[QuestionAnswer(question="How many emails per day?", answer="About 40")],
    )

    content = seen_messages[0][1].content
    assert "The customer was asked these clarifying questions:" in content
    assert "Q: How many emails per day?" in content
    assert "A: About 40" in content


def test_generate_requirements_marks_blank_answers_for_assumption():
    from bestteam import QuestionAnswer

    seen_messages = []
    model = _recording_model(seen_messages)

    generate_requirements(
        model,
        "Help with support.",
        "",
        current=Requirements(clarifying_questions=["Which mailbox provider?"]),
        answers=[QuestionAnswer(question="Which mailbox provider?", answer="   ")],
    )

    content = seen_messages[0][1].content
    assert "A: (not answered" in content
    assert '"Assumed:"' in content


def test_generate_requirements_puts_answers_between_current_and_feedback():
    from bestteam import QuestionAnswer

    seen_messages = []
    model = _recording_model(seen_messages)
    current = Requirements(goals=["Reply within an hour"])

    generate_requirements(
        model,
        "Help with support.",
        "",
        current=current,
        answers=[QuestionAnswer(question="Which tone?", answer="Friendly")],
        feedback="Also cover refunds.",
    )

    content = seen_messages[0][1].content
    assert content.index("Reply within an hour") < content.index("Q: Which tone?")
    assert content.index("Q: Which tone?") < content.index("Also cover refunds.")


def test_question_answer_defaults_to_unanswered():
    from bestteam import QuestionAnswer

    assert QuestionAnswer(question="Q?").answer == ""


def test_generate_requirements_restates_still_open_questions_from_current():
    """to_prompt() omits clarifying_questions (it also feeds the architect),
    so a partially-answered Confirm round must restate the unanswered ones
    explicitly or the analyst silently drops them (Codex review finding)."""
    from bestteam import QuestionAnswer

    seen_messages = []
    model = _recording_model(seen_messages)
    current = Requirements(
        clarifying_questions=["How many emails per day?", "Which mailbox provider?"]
    )

    generate_requirements(
        model,
        "Help with support.",
        "",
        current=current,
        answers=[QuestionAnswer(question="How many emails per day?", answer="About 40")],
    )

    content = seen_messages[0][1].content
    assert "Still-open clarifying questions" in content
    assert "- Which mailbox provider?" in content


def test_analyst_prompt_carries_the_asking_and_folding_policy():
    from bestteam.core.requirements import _ANALYST_SYSTEM_PROMPT

    assert "up to 4" in _ANALYST_SYSTEM_PROMPT
    assert "Assumed:" in _ANALYST_SYSTEM_PROMPT
    assert "Never re-ask" in _ANALYST_SYSTEM_PROMPT


def test_generate_requirements_puts_feedback_after_the_current_understanding():
    """The customer's new sentence is the later, winning word when it conflicts
    with a field they had edited earlier."""
    seen_messages = []

    class _RecordingModel(_FakeAnalystChatModel):
        def with_structured_output(self, schema, **kwargs):
            def _invoke(messages):
                seen_messages.append(messages)
                return self.responses[0]

            return RunnableLambda(_invoke)

    model = _RecordingModel(responses=[Requirements(summary="Updated summary")])
    current = Requirements(goals=["Answer customer emails within an hour"])

    generate_requirements(model, "Help with support.", "", current=current, feedback="Make it two hours instead.")

    content = seen_messages[0][1].content
    assert content.index("Answer customer emails within an hour") < content.index("Make it two hours instead.")
