"""Shared `with_structured_output` invocation helper.

Used by both the Business Analyst (`requirements.py`) and Solution Architect
(`specification.py`) structured-output stages.
"""

from __future__ import annotations

from typing import Any, List, Tuple

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage


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
    """
    try:
        return model.with_structured_output(schema, method=method).invoke(messages), method
    except Exception as exc:
        if method != "function_calling" or "tool_choice" not in str(exc).lower():
            raise
        return model.with_structured_output(schema, method="json_mode").invoke(messages), "json_mode"
