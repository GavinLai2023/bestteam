from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Run:
    """Tracks one workflow execution: its inputs, replayable event log, and status.

    `org_id` is the owning organization -- run reads (GET + the WebSocket
    stream) are refused across orgs. `username` records who started it
    (informational; ownership is org-level). None values (e.g. builder
    sandbox runs pre-org, or legacy runs) are visible to platform admins only.
    """

    id: str
    workflow: str
    input: str
    org_id: Optional[int] = None
    username: Optional[str] = None
    status: str = "running"  # running | completed | failed
    events: List[dict] = field(default_factory=list)


_MAX_RETAINED_RUNS = 1000


class RunRegistry:
    """In-memory run tracker with pub/sub for live event streaming.

    Good enough for a single-process dev/demo deployment — a production
    deployment would back this with Redis/Postgres so runs survive restarts
    and scale across workers, behind this same interface.
    """

    def __init__(self) -> None:
        self._runs: Dict[str, Run] = {}
        self._subscribers: Dict[str, List[Tuple[asyncio.AbstractEventLoop, "asyncio.Queue[dict]"]]] = {}
        # One lock serialises the event append, the replay snapshot, and
        # subscriber insertion so a publish landing between subscribe()'s
        # replay and its registration can't be lost to that subscriber
        # (CR-010). The worker thread publishes; the event-loop thread(s)
        # subscribe -- so this must be a real cross-thread lock.
        self._lock = threading.Lock()

    def create(
        self,
        workflow: str,
        input: str,
        *,
        org_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Run:
        run = Run(id=uuid.uuid4().hex, workflow=workflow, input=input, org_id=org_id, username=username)
        with self._lock:
            self._runs[run.id] = run
            self._subscribers[run.id] = []
            self._evict_if_over_bound()
        return run

    def discard(self, run_id: str) -> None:
        """Drop a run that was created but never dispatched.

        The autonomous email trigger optimistically creates a run before its
        final compare-and-swap advance; if that CAS finds the trigger was
        disabled in the same cycle, the run is abandoned. Without this it would
        linger in `_runs` as a `running` entry the eviction pass never reclaims.
        Safe (no-op) if the id is unknown.
        """
        with self._lock:
            self._runs.pop(run_id, None)
            self._subscribers.pop(run_id, None)

    def _evict_if_over_bound(self) -> None:
        """Evict the oldest terminal, subscriber-free runs until back within
        `_MAX_RETAINED_RUNS`. Must be called with `self._lock` already held.

        The autonomous trigger creates runs unattended, indefinitely -- unlike
        the previous purely human-click-triggered regime this registry was
        originally sized for -- so without a bound, a long-lived process's
        memory (every run's full input/output/event history) grows without
        limit. A `running` run, or one with an active WebSocket subscriber, is
        never evicted (a live view must never be pulled out from under it).
        `_runs` is a plain dict, insertion-ordered since Python 3.7, so
        iterating it is oldest-to-newest with no extra bookkeeping needed.
        """
        if len(self._runs) <= _MAX_RETAINED_RUNS:
            return
        for run_id, run in list(self._runs.items()):
            if len(self._runs) <= _MAX_RETAINED_RUNS:
                return
            if run.status == "running" or self._subscribers.get(run_id):
                continue
            del self._runs[run_id]
            del self._subscribers[run_id]

    def get(self, run_id: str) -> "Run | None":
        return self._runs.get(run_id)

    def publish(self, run_id: str, event: dict) -> None:
        """Called from the worker thread running the workflow. Each
        subscriber's queue is woken up on the event loop captured when it
        subscribed (not the loop of the request that started the run, which
        may already be gone by the time this fires)."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                # The run was evicted (bounded retention) after it went terminal
                # but before a late post-terminal event (e.g. `memory_recorded`,
                # which is published after `run_completed`) arrived. Nothing to
                # append or notify; durable usage/provenance persistence does not
                # depend on this publish. Silently drop rather than KeyError.
                return
            run.events.append(event)
            if event["type"] == "run_completed":
                run.status = "completed"
            elif event["type"] == "run_failed":
                run.status = "failed"
            for loop, subscriber_queue in self._subscribers[run_id]:
                loop.call_soon_threadsafe(subscriber_queue.put_nowait, event)

    def subscribe(self, run_id: str) -> "asyncio.Queue[dict] | None":
        """Return a queue pre-loaded with events seen so far, then live
        updates -- or None if `run_id` is unknown.

        A caller that already checked `get(run_id)` can still see this: the
        run may have been evicted (see `_evict_if_over_bound`) in the window
        between that check and this call (e.g. across an `await` point). The
        caller should treat None the same as a `get()` miss.
        """
        subscriber_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        with self._lock:
            if run_id not in self._runs:
                return None
            for event in self._runs[run_id].events:
                subscriber_queue.put_nowait(event)
            self._subscribers[run_id].append((asyncio.get_running_loop(), subscriber_queue))
        return subscriber_queue

    def unsubscribe(self, run_id: str, subscriber_queue: "asyncio.Queue[dict]") -> None:
        with self._lock:
            subscribers = self._subscribers.get(run_id, [])
            self._subscribers[run_id] = [pair for pair in subscribers if pair[1] is not subscriber_queue]
