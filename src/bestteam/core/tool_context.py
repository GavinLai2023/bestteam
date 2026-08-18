"""A per-tool-call side channel between a tool and whoever is running it.

An agent tool can only *return a string* -- that string is what the model
reads next, so it can't double as the place a tool reports what it did for
the trace or what it spent. This module gives the runner (the adapter's
tool-calling loop) a contextvar-scoped box the tool can drop structured
facts into while it runs:

- `trace` -- fields for the call's `tool_completed` event, so a tool that
  knows something a generic 200-char stringification of its result cannot
  express (a knowledge base's query, hit count and sources -- never the
  document text itself) reports it directly.
- `usage` -- token usage entries for LLM calls a tool makes internally
  (a knowledge base's query expansion, for instance), which today go
  unmetered because the tool loop has no hook to report them.

Every reporting function is a **no-op when no context is active**, so a tool
called directly from the SDK (`kb.query(...)` in a script, or a unit test)
behaves exactly as it did before. A contextvar rather than a thread-local
because the backend runs workflows in a worker pool and LangGraph may run
nodes in their own contexts; each `tool_call_context()` sets and resets its
own token, so nested or concurrent calls never see each other's box.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class ToolCallContext:
    """What one tool call reported while it ran."""

    trace: Dict[str, Any] = field(default_factory=dict)
    usage: List[Dict[str, Any]] = field(default_factory=list)


_current: "ContextVar[Optional[ToolCallContext]]" = ContextVar(
    "bestteam_tool_call_context", default=None
)


@contextmanager
def tool_call_context() -> Iterator[ToolCallContext]:
    """Run a tool call with a fresh reporting context, yielded to the caller.

    The context object stays readable after the block exits -- the runner
    reads what the tool reported once the call has returned.
    """
    context = ToolCallContext()
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def report_trace(**fields: Any) -> None:
    """Record fields for this call's `tool_completed` event. No-op outside a
    run (an SDK-direct call has nobody to report to)."""
    context = _current.get()
    if context is not None:
        context.trace.update(fields)


def add_usage(entry: Dict[str, Any]) -> None:
    """Record one `{model, input_tokens, output_tokens}` entry for an LLM call
    this tool made. No-op outside a run."""
    context = _current.get()
    if context is not None:
        context.usage.append(entry)
