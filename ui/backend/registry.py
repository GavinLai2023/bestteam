from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Run:
    """Tracks one pipeline execution: its inputs, replayable event log, and status.

    `org_id` is the owning organization -- run reads (GET + the WebSocket
    stream) are refused across orgs. `username` records who started it
    (informational; ownership is org-level). None values (e.g. builder
    sandbox runs pre-org, or legacy runs) are visible to platform admins only.
    """

    id: str
    pipeline: str
    input: str
    org_id: Optional[int] = None
    username: Optional[str] = None
    status: str = "running"  # running | completed | failed | cancelled
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
        # Cooperative-cancellation flags, one per run -- the worker thread
        # can't be force-killed mid-`pipeline.stream()`, so this is checked
        # between yielded events instead (see runtime.py::run_in_background).
        self._cancel_flags: Dict[str, threading.Event] = {}
        # The reply text streamed so far, per run -- the ONLY thing kept from
        # the transient delta channel, and only while the run is live. The
        # worker starts before the POST response lets the client open its
        # WebSocket, so without it a subscriber that arrives a moment late
        # watches a misleading suffix of the reply instead of the whole thing
        # (Codex review finding). Dropped at the terminal event, since
        # `run_completed` then carries the authoritative text.
        self._live_text: Dict[str, str] = {}
        # The agents currently working, per run -- the second thing kept from
        # the transient channel (beside `_live_text`), for the same reason: a
        # subscriber that arrives or reconnects mid-agent would otherwise see
        # no live milestone until the next node flushes. Agent name -> kind
        # ("agent" | "subagent"), insertion-ordered. Dropped at the terminal
        # event. See docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md.
        self._live_working: Dict[str, Dict[str, str]] = {}
        # One lock serialises the event append, the replay snapshot, and
        # subscriber insertion so a publish landing between subscribe()'s
        # replay and its registration can't be lost to that subscriber
        # (CR-010). The worker thread publishes; the event-loop thread(s)
        # subscribe -- so this must be a real cross-thread lock.
        self._lock = threading.Lock()

    def create(
        self,
        pipeline: str,
        input: str,
        *,
        org_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Run:
        run = Run(id=uuid.uuid4().hex, pipeline=pipeline, input=input, org_id=org_id, username=username)
        with self._lock:
            self._runs[run.id] = run
            self._subscribers[run.id] = []
            self._cancel_flags[run.id] = threading.Event()
            self._live_text[run.id] = ""
            self._live_working[run.id] = {}
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
            self._cancel_flags.pop(run_id, None)
            self._live_text.pop(run_id, None)
            self._live_working.pop(run_id, None)

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
            self._cancel_flags.pop(run_id, None)
            self._live_text.pop(run_id, None)
            self._live_working.pop(run_id, None)

    def get(self, run_id: str) -> "Run | None":
        return self._runs.get(run_id)

    def purge_content(self, run_id: str) -> bool:
        """Forget a finished run's input and events, keeping the entry.

        The retention purge (`ui/backend/retention.py`) clears the persisted
        copy, but this registry holds its own for up to `_MAX_RETAINED_RUNS`
        runs -- and that copy is what `GET /api/runs/{id}` and the WebSocket
        replay serve. Without this, content the customer deleted stays
        readable until the entry is evicted or the process restarts.

        A `running` run is left alone and returns False: its worker is still
        appending events, and the DB purge refuses it too.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status == "running":
                return False
            run.input = ""
            run.events = []
            self._live_text.pop(run_id, None)
            self._live_working.pop(run_id, None)
            return True

    def request_cancel(self, run_id: str) -> bool:
        """Ask a running run to stop cooperatively. Returns False (no-op) for
        an unknown run or one that's already terminal -- there's nothing left
        to cancel."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status != "running":
                return False
            self._cancel_flags[run_id].set()
            return True

    def cancel_requested(self, run_id: str) -> bool:
        flag = self._cancel_flags.get(run_id)
        return flag.is_set() if flag else False

    def publish(self, run_id: str, event: dict) -> None:
        """Called from the worker thread running the pipeline. Each
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
            elif event["type"] == "run_cancelled":
                run.status = "cancelled"
            agent = event.get("agent")
            if event["type"] in ("agent_completed", "subagent_completed") and agent:
                # The persisted completion is the authoritative "no longer
                # working" -- idempotent with the transient one below.
                self._live_working.get(run_id, {}).pop(agent, None)
            if event["type"] in ("run_completed", "run_failed", "run_cancelled"):
                # The terminal event carries the authoritative reply, so a
                # retained preview would only show it twice.
                self._live_text.pop(run_id, None)
                self._live_working.pop(run_id, None)
            for loop, subscriber_queue in self._subscribers[run_id]:
                loop.call_soon_threadsafe(subscriber_queue.put_nowait, event)

    def publish_transient(self, run_id: str, event: dict) -> None:
        """Fan an event out to live subscribers without recording it.

        Token deltas and the live `agent_working` milestone (see
        docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md and
        docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md).
        Unlike `publish`, this appends nothing to `run.events` and drives no
        status change: a long reply would otherwise put thousands of entries
        into a log that is replayed in full to every new subscriber and held
        for up to `_MAX_RETAINED_RUNS` runs.

        The consequence is deliberate -- a visitor who reconnects mid-run sees
        no partial text, and then receives the complete reply on
        `run_completed`, which IS replayed. The durable path stays the source
        of truth.
        """
        with self._lock:
            if run_id not in self._runs:
                # Evicted, or never created -- same silent drop as `publish`.
                return
            if event.get("type") == "reply_delta":
                self._live_text[run_id] = self._live_text.get(run_id, "") + str(event.get("data") or "")
            elif event.get("type") == "reply_reset":
                self._live_text[run_id] = ""
            elif event.get("type") == "agent_working" and event.get("agent"):
                data = event.get("data") or {}
                working = self._live_working.setdefault(run_id, {})
                if data.get("state") == "completed":
                    working.pop(event["agent"], None)
                else:
                    # First kind wins: a delegated subordinate's own
                    # `agent_started` follows its `subagent_started`, and it
                    # must stay a subordinate for the strip's rendering.
                    working.setdefault(event["agent"], data.get("kind", "agent"))
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
            streamed_so_far = self._live_text.get(run_id) or ""
            if streamed_so_far:
                # One synthetic delta carrying everything streamed before this
                # subscriber arrived, so the preview it builds is the whole
                # reply rather than whatever suffix it happened to catch.
                subscriber_queue.put_nowait({"type": "reply_delta", "data": streamed_so_far})
            for agent, kind in (self._live_working.get(run_id) or {}).items():
                # One synthetic "started" per agent still working, so a
                # subscriber arriving mid-agent gets its strip back.
                subscriber_queue.put_nowait(
                    {"type": "agent_working", "agent": agent, "data": {"kind": kind, "state": "started"}}
                )
            self._subscribers[run_id].append((asyncio.get_running_loop(), subscriber_queue))
        return subscriber_queue

    def unsubscribe(self, run_id: str, subscriber_queue: "asyncio.Queue[dict]") -> None:
        with self._lock:
            subscribers = self._subscribers.get(run_id, [])
            self._subscribers[run_id] = [pair for pair in subscribers if pair[1] is not subscriber_queue]
