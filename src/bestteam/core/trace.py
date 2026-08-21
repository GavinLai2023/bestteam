from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TraceEvent:
    """A single observable moment in a pipeline run.

    This is the unit a monitoring UI subscribes to — engine-agnostic, so
    whatever engine produced it, the UI only ever sees this shape.

    `type` is one of: "run_started", "run_queued" (backend-lifecycle only —
    the SDK never emits it, see `ui/backend/registry.py`), "agent_started",
    "agent_progress" (`data` = {"note": str}, emitted before a tool-calling
    agent's 2nd+ model call in one turn), "tool_started" (`data` =
    {"tool": str} — never the call's raw args), "tool_completed" (`data` =
    {"tool", "success": bool, "duration_ms": int, "summary": str} — a
    truncated, business-safe summary of the tool result, never the raw
    exception on failure; a knowledge base tool's event instead reports what
    was searched, adding {"query": str (≤200 chars), "hit_count": int,
    "sources": List[str]} and a `summary` built from those; each source is a
    filename plus its page/heading citation label, never body text),
    "delegation_started"/"delegation_completed"
    (`data` = {"to": str, "task_summary"/"summary": str}, emitted on the
    HIERARCHICAL manager), "subagent_started"/"subagent_completed" (emitted
    on the delegated subordinate, `agent` = subordinate name), "agent_completed",
    "run_completed" (always the FINAL event of a run), "run_failed",
    "run_cancelled" (backend-lifecycle only, see cooperative cancellation in
    `ui/backend/runtime.py`), and (when per-user memory is
    active, SP-3) "memory_recalled" (`data` = count recalled, 0 included),
    "memory_recorded" (`data` = record types written; `usage` = the extraction
    call's token usage, if any), and "memory_failed" (`data` = "recall" | "record",
    sanitized — no exception detail; `usage` carries the extraction spend when every
    write failed, so it's still billed). `memory_recalled`/`memory_failed("recall")`
    precede the agents; recording events (`memory_recorded`/`memory_failed("record")`)
    are emitted AFTER `run_completed`, so a slow/hung extraction can't wedge the run
    — a consumer that stops on the terminal event won't see them, but the backend
    drains the full stream to meter/record them.

    **Diagnostic runs only** (`Pipeline.run/stream(..., diagnostic=True)`, an
    admin's diagnostic re-run of a poor run -- never a customer-initiated run):
    "agent_prompt" (`data` = {"system_prompt": str, "input": str}, the exact
    messages the agent's first model call received, emitted right after
    `agent_started`), "model_turn" (`data` = {"turn": int, "content": str,
    "tool_calls": [{"name": str, "args": dict | None}]}, one per model call,
    including the final one -- never the provider's call ids), and two
    extensions: `tool_started` gains `"args": dict` and `tool_completed` gains
    `"result": str`, the full string returned to the model (a knowledge base
    tool's `result` is therefore the retrieved excerpts the model read). Every
    diagnostic string is capped (`_MAX_DIAGNOSTIC_CHARS` in the adapter). The
    email tools are exempt on every path -- no `args`, no `result`, and `None`
    args in `model_turn` -- because their args/results are mail content.

    `usage` holds zero or more per-model-call token usage entries (each
    `{"model": <spec str>, "input_tokens": int, "output_tokens": int}`)
    recorded while producing this event -- empty for engines/models that
    don't report `usage_metadata` (e.g. `fake:` models).
    """

    type: str
    pipeline: str
    agent: Optional[str] = None
    data: Any = None
    usage: List[Dict[str, Any]] = field(default_factory=list)
