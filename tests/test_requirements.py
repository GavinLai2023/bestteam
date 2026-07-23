from typing import Any, List

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from bestteam import Requirements, generate_requirements


class _FakeAnalystChatModel(BaseChatModel):
    """Cycles through pre-scripted Requirements objects via `with_structured_output`,
    independent of `bind_tools`/tool-calling -- mirrors `_FakeArchitectChatModel`
    in tests/test_specification.py."""

    responses: List[Any] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    @property
    def _llm_type(self) -> str:
        return "fake-analyst"

    def with_structured_output(self, schema, **kwargs):
        responses = self.responses
        state = {"index": 0}

        def _invoke(_input):
            i = min(state["index"], len(responses) - 1)
            state["index"] += 1
            return responses[i]

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
