from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


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
        self._subscribers: Dict[str, List[Tuple[asyncio.AbstractEventLoop, "asyncio.Queue[dict]"]]] = {}

    def create(self, workflow: str, input: str) -> Run:
        run = Run(id=uuid.uuid4().hex, workflow=workflow, input=input)
        self._runs[run.id] = run
        self._subscribers[run.id] = []
        return run

    def get(self, run_id: str) -> "Run | None":
        return self._runs.get(run_id)

    def publish(self, run_id: str, event: dict) -> None:
        """Called from the worker thread running the workflow. Each
        subscriber's queue is woken up on the event loop captured when it
        subscribed (not the loop of the request that started the run, which
        may already be gone by the time this fires)."""
        run = self._runs[run_id]
        run.events.append(event)
        if event["type"] == "run_completed":
            run.status = "completed"
        elif event["type"] == "run_failed":
            run.status = "failed"
        for loop, subscriber_queue in self._subscribers[run_id]:
            loop.call_soon_threadsafe(subscriber_queue.put_nowait, event)

    def subscribe(self, run_id: str) -> "asyncio.Queue[dict]":
        """Return a queue pre-loaded with events seen so far, then live updates."""
        subscriber_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        for event in self._runs[run_id].events:
            subscriber_queue.put_nowait(event)
        self._subscribers[run_id].append((asyncio.get_running_loop(), subscriber_queue))
        return subscriber_queue

    def unsubscribe(self, run_id: str, subscriber_queue: "asyncio.Queue[dict]") -> None:
        subscribers = self._subscribers.get(run_id, [])
        self._subscribers[run_id] = [pair for pair in subscribers if pair[1] is not subscriber_queue]
