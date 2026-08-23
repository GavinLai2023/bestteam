"""Tests for the shared `with_structured_output` invocation helper.

The `json_mode` fallback in `core/_structured_output.py` exists for
reasoning models that reject a forced `tool_choice`. LangChain's `json_mode`
binds `response_format={"type": "json_object"}` and injects *nothing* into
the prompt, so the fallback has to supply the formatting instructions
itself -- see the stub below for the two provider rules that enforces.
"""

from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableLambda

from bestteam.core._structured_output import invoke_structured
from bestteam import Requirements

pytestmark = pytest.mark.unit


class _RejectsToolChoiceAndEnforcesJsonRules(BaseChatModel):
    """Rejects a forced `tool_choice` (so the helper falls back to
    `json_mode`), then holds the fallback to OpenAI's actual rule: a request
    with `response_format` of type `json_object` is a 400 unless the prompt
    contains the word "json".

    Records the messages the fallback sent so a test can assert on what the
    model was actually asked, rather than on the stub.
    """

    seen_messages: List[Any] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    @property
    def _llm_type(self) -> str:
        return "fake-rejects-tool-choice"

    def with_structured_output(self, schema, *, method="function_calling", **kwargs):
        if method == "function_calling":

            def _reject(_input):
                raise Exception("Thinking mode does not support this tool_choice")

            return RunnableLambda(_reject)

        seen = self.seen_messages

        def _json_mode(messages):
            seen.append(messages)
            prompt = "\n".join(str(m.content) for m in messages)
            if "json" not in prompt.lower():
                raise Exception(
                    "Error code: 400 - {'error': {'message': \"Prompt must contain "
                    "the word 'json' in some form to use 'response_format' of type "
                    "'json_object'.\"}}"
                )
            return Requirements(summary="ok")

        return RunnableLambda(_json_mode)


def test_json_mode_fallback_prompt_contains_the_word_json():
    model = _RejectsToolChoiceAndEnforcesJsonRules(seen_messages=[])

    result, method = invoke_structured(model, Requirements, _messages())

    assert method == "json_mode"
    assert result == Requirements(summary="ok")


def _messages():
    from langchain_core.messages import HumanMessage, SystemMessage

    return [
        SystemMessage(content="You are the Business Analyst."),
        HumanMessage(content="Intent/Challenge:\nHelp me answer customer email."),
    ]


def test_json_mode_fallback_prompt_describes_the_schema():
    # `json_mode` binds only `response_format`; LangChain's own docs say the
    # caller must put the target format in the prompt. Without it the model is
    # free to invent field names and the PydanticOutputParser then rejects the
    # completion.
    model = _RejectsToolChoiceAndEnforcesJsonRules(seen_messages=[])

    invoke_structured(model, Requirements, _messages())

    prompt = "\n".join(str(m.content) for m in model.seen_messages[-1])
    for field in Requirements.model_fields:
        assert field in prompt


def test_json_mode_fallback_leaves_the_callers_message_list_alone():
    # `generate_requirements`/`generate_specification` append to the same list
    # across retry turns; appending in place here would stack a fresh copy of
    # the instructions on every attempt.
    model = _RejectsToolChoiceAndEnforcesJsonRules(seen_messages=[])
    messages = _messages()

    invoke_structured(model, Requirements, messages)

    assert len(messages) == 2


def test_json_mode_instructions_are_supplied_when_the_caller_passes_the_method_back():
    # `generate_requirements`/`generate_specification` feed the working method
    # back in on every subsequent turn to skip the doomed `function_calling`
    # attempt. That turn is a direct `json_mode` call, not a fallback, and it
    # needs the same instructions -- otherwise a retry re-raises the 400 the
    # first turn just worked around.
    model = _RejectsToolChoiceAndEnforcesJsonRules(seen_messages=[])

    result, method = invoke_structured(model, Requirements, _messages(), method="json_mode")

    assert method == "json_mode"
    assert result == Requirements(summary="ok")
