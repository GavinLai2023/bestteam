from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class TraceEvent:
    """A single observable moment in a workflow run.

    This is the unit a monitoring UI subscribes to — engine-agnostic, so
    whatever engine produced it, the UI only ever sees this shape.

    `type` is one of: "run_started", "agent_completed", "run_completed", "run_failed"
    """

    type: str
    workflow: str
    agent: Optional[str] = None
    data: Any = None
