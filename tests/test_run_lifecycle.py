"""Run-lifecycle regression tests for CR-003 (worker failure always yields a
terminal event) and CR-010 (RunRegistry subscribe/publish are serialised)."""

import threading
import time

import pytest

pytest.importorskip("sqlalchemy")

from bestteam.core.trace import TraceEvent
from ui.backend import runtime
from ui.backend.registry import RunRegistry


# --- CR-003 -----------------------------------------------------------------


class _BoomWorkflow:
    """Stand-in whose .stream() raises like a compile failure escaping
    Workflow.stream() (which compiles before its own BestTeamError handler)."""

    name = "boom_wf"

    def stream(self, *args, **kwargs):
        raise RuntimeError("internal compile detail that must not leak")


def test_run_in_background_publishes_terminal_event_on_worker_exception():
    run = runtime.registry.create("boom_wf", "input")

    # engine=None / user_id=None -> no DB, no memory: isolates the failure path.
    runtime.run_in_background(run.id, _BoomWorkflow(), "input")

    stored = runtime.registry.get(run.id)
    assert stored.status == "failed"
    failures = [e for e in stored.events if e["type"] == "run_failed"]
    assert len(failures) == 1
    # Sanitized: the internal exception text is not leaked to subscribers.
    assert "internal compile detail" not in failures[0]["data"]


class _CompletesThenRaisesWorkflow:
    """Yields run_completed, then raises -- like a post-completion side effect
    (e.g. memory recording) failing after the run already succeeded."""

    name = "wf"

    def stream(self, *args, **kwargs):
        yield TraceEvent(type="run_completed", workflow="wf", data="done")
        raise RuntimeError("post-completion failure")


def test_post_completion_failure_does_not_publish_second_terminal_event():
    # CR-003: a failure AFTER a terminal event was already published must not
    # flip the run to failed or emit a second terminal event.
    run = runtime.registry.create("wf", "input")

    runtime.run_in_background(run.id, _CompletesThenRaisesWorkflow(), "input")

    stored = runtime.registry.get(run.id)
    assert stored.status == "completed"
    terminal = [e for e in stored.events if e["type"] in ("run_completed", "run_failed")]
    assert [e["type"] for e in terminal] == ["run_completed"]


# --- CR-010 -----------------------------------------------------------------


def test_publish_is_blocked_while_registry_lock_is_held():
    # Proves publish() acquires the shared lock, so it cannot interleave with
    # subscribe()'s replay+registration critical section (the lost-event race).
    reg = RunRegistry()
    run = reg.create("wf", "in")

    reg._lock.acquire()
    done = []

    def _publish():
        reg.publish(run.id, {"type": "agent_completed"})
        done.append(True)

    worker = threading.Thread(target=_publish)
    worker.start()
    try:
        time.sleep(0.05)
        assert done == [], "publish should block while the registry lock is held"
    finally:
        reg._lock.release()

    worker.join(timeout=2)
    assert done == [True]
    assert any(e["type"] == "agent_completed" for e in reg.get(run.id).events)


def test_subscribe_replays_history_then_receives_live_events():
    import asyncio

    reg = RunRegistry()
    run = reg.create("wf", "in")
    reg.publish(run.id, {"type": "run_started"})  # before any subscriber

    async def go():
        queue = reg.subscribe(run.id)
        assert queue.get_nowait()["type"] == "run_started"  # replayed under lock
        reg.publish(run.id, {"type": "agent_completed"})
        await asyncio.sleep(0)  # let call_soon_threadsafe deliver
        assert queue.get_nowait()["type"] == "agent_completed"

    asyncio.run(go())
