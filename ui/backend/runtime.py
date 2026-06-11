"""Shared run-execution plumbing for `main.py` (`/api/runs`) and `builder.py`
(the Team Builder Wizard's sandbox test runs, Phase 2).

Split into its own module so the two router modules don't import from each
other.
"""

from __future__ import annotations

import asyncio
import dataclasses
from concurrent.futures import ThreadPoolExecutor

from bestteam import Workflow

from .registry import RunRegistry

registry = RunRegistry()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bestteam-run")


def run_in_background(run_id: str, workflow: Workflow, input: str, loop: asyncio.AbstractEventLoop) -> None:
    """Drain `Workflow.stream()` on a worker thread and relay each event back
    onto the asyncio loop so WebSocket subscribers see it as it happens."""
    for event in workflow.stream(input):
        payload = dataclasses.asdict(event)
        loop.call_soon_threadsafe(registry.publish, run_id, payload)
