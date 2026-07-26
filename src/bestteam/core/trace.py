from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TraceEvent:
    """A single observable moment in a workflow run.

    This is the unit a monitoring UI subscribes to — engine-agnostic, so
    whatever engine produced it, the UI only ever sees this shape.

    `type` is one of: "run_started", "agent_completed", "run_completed",
    "run_failed", and (when per-user memory is active, SP-3) "memory_recalled"
    (`data` = number of records recalled) and "memory_recorded" (`data` = the
    record types written; `usage` = the extraction call's token usage, if any).

    `usage` holds zero or more per-model-call token usage entries (each
    `{"model": <spec str>, "input_tokens": int, "output_tokens": int}`)
    recorded while producing this event -- empty for engines/models that
    don't report `usage_metadata` (e.g. `fake:` models).
    """

    type: str
    workflow: str
    agent: Optional[str] = None
    data: Any = None
    usage: List[Dict[str, Any]] = field(default_factory=list)
