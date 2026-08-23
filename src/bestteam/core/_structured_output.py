"""Shared `with_structured_output` invocation helper.

Used by both the Business Analyst (`requirements.py`) and Solution Architect
(`specification.py`) structured-output stages.
"""

from __future__ import annotations

import json
from typing import Any, List, Tuple

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage


def invoke_structured(
    model: BaseChatModel, schema: Any, messages: List[BaseMessage], *, method: str = "function_calling"
) -> Tuple[Any, str]:
    """Call `model.with_structured_output(schema, method=method).invoke(messages)`.

    Falls back to `method="json_mode"` if the default `function_calling` method
    -- which forces `tool_choice` -- is rejected. Some reasoning/"thinking mode"
    models (e.g. DeepSeek's reasoning models) refuse a forced `tool_choice`
    outright. Returns `(result, method_used)` so a caller retrying across
    several turns (e.g. `generate_specification`'s self-correction loop) can
    pass the working method back in and skip the doomed first attempt (and its
    real API cost) on every subsequent call.

    `json_mode` needs formatting instructions in the prompt (see
    `_json_mode_instructions`), and needs them on *every* turn that uses it --
    the fallback turn and each later turn a caller passes the method back into
    -- which is why they are attached per method rather than in the fallback
    branch alone.
    """
    try:
        return model.with_structured_output(schema, method=method).invoke(_prompt_for(method, schema, messages)), method
    except Exception as exc:
        if method != "function_calling" or "tool_choice" not in str(exc).lower():
            raise
        return (
            model.with_structured_output(schema, method="json_mode").invoke(
                _prompt_for("json_mode", schema, messages)
            ),
            "json_mode",
        )


def _prompt_for(method: str, schema: Any, messages: List[BaseMessage]) -> List[BaseMessage]:
    """The messages to send under `method`, as a new list.

    Never appends in place: callers retry across several turns and keep
    appending to the list they passed, so mutating it here would stack another
    copy of the instructions on every attempt.
    """
    if method != "json_mode":
        return messages
    return messages + [HumanMessage(content=_json_mode_instructions(schema))]


def _json_mode_instructions(schema: Any) -> str:
    """Formatting instructions the `json_mode` path has to supply itself.

    LangChain's `json_mode` binds `response_format={"type": "json_object"}` and
    injects nothing into the prompt, which leaves two provider requirements
    unmet: OpenAI rejects the request outright unless the word "json" appears
    in the prompt, and the model is otherwise never told which fields to
    produce -- so the parser rejects whatever it invents. Both call sites pass
    a pydantic model.
    """
    return (
        "Respond with a single json object matching this JSON schema, and "
        "nothing else -- no prose, no code fences:\n"
        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )
