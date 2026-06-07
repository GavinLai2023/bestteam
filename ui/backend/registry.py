from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Run:
    """Tracks one workflow execution: its inputs, replayable event log, and status."""

    id: str
    workflow: str
    input: str
    status: str = "running"  # running | completed | failed
    events: List[dict] = field(default_factory=list)


class RunRegistry:
    """In-memory run tracker with pub/sub for live event streaming.

    Good enough for a single-process dev/demo deployment — a production
    deployment would back this with Redis/Postgres so runs survive restarts
    and scale across workers, behind this same interface.
    """

    def __init__(self) -> None:
        self._runs: Dict[str, Run] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}

    def create(self, workflow: str, input: str) -> Run:
        run = Run(id=uuid.uuid4().hex[:8], workflow=workflow, input=input)
        self._runs[run.id] = run
        self._subscribers[run.id] = []
        return run

    def get(self, run_id: str) -> "Run | None":
        return self._runs.get(run_id)

    def publish(self, run_id: str, event: dict) -> None:
        run = self._runs[run_id]
        run.events.append(event)
        if event["type"] == "run_completed":
            run.status = "completed"
        elif event["type"] == "run_failed":
            run.status = "failed"
        for queue in self._subscribers[run_id]:
            queue.put_nowait(event)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """Return a queue pre-loaded with events seen so far, then live updates."""
        queue: asyncio.Queue = asyncio.Queue()
        for event in self._runs[run_id].events:
            queue.put_nowait(event)
        self._subscribers[run_id].append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(run_id, [])
        if queue in subscribers:
            subscribers.remove(queue)
