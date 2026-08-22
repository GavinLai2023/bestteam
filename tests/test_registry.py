"""Unit tests for RunRegistry's bounded eviction (memory growth under the
autonomous trigger's unattended, continuous run creation)."""

import pytest

import ui.backend.registry as registry_module
from ui.backend.registry import RunRegistry

pytestmark = pytest.mark.unit


def _complete(reg, run_id):
    reg.publish(run_id, {"type": "run_completed", "data": "done"})


def test_eviction_leaves_registry_under_the_bound(monkeypatch):
    monkeypatch.setattr(registry_module, "_MAX_RETAINED_RUNS", 3)
    reg = RunRegistry()
    ids = []
    for i in range(5):
        run = reg.create("wf", f"input {i}")
        _complete(reg, run.id)
        ids.append(run.id)

    assert len(reg._runs) == 3
    # Oldest two evicted, newest three retained.
    assert reg.get(ids[0]) is None
    assert reg.get(ids[1]) is None
    assert reg.get(ids[2]) is not None
    assert reg.get(ids[3]) is not None
    assert reg.get(ids[4]) is not None


def test_purge_content_forgets_the_input_and_events():
    # The retention purge clears the DB; this registry serves GET /api/runs/{id}
    # and the WebSocket replay from its own copy, which must go too.
    reg = RunRegistry()
    run = reg.create("wf", "From alice@example.com: my boiler leaks")
    _complete(reg, run.id)

    assert reg.purge_content(run.id) is True
    assert reg.get(run.id).input == ""
    assert reg.get(run.id).events == []
    # The entry itself stays -- the run is still a real run that happened.
    assert reg.get(run.id).status == "completed"


def test_purge_content_leaves_a_running_run_alone():
    # Its worker is still appending events, and the DB purge refuses it too.
    reg = RunRegistry()
    run = reg.create("wf", "still going")

    assert reg.purge_content(run.id) is False
    assert reg.get(run.id).input == "still going"


def test_purge_content_is_a_no_op_for_an_unknown_run():
    assert RunRegistry().purge_content("never-existed") is False


def test_running_runs_are_never_evicted(monkeypatch):
    monkeypatch.setattr(registry_module, "_MAX_RETAINED_RUNS", 2)
    reg = RunRegistry()
    ids = []
    for i in range(4):
        run = reg.create("wf", f"input {i}")  # left "running" -- never completed
        ids.append(run.id)

    # Over the bound (4 > 2), but nothing is eligible for eviction.
    for run_id in ids:
        assert reg.get(run_id) is not None
    assert len(reg._runs) == 4


def test_subscribed_terminal_run_is_skipped_for_eviction(monkeypatch):
    import asyncio

    monkeypatch.setattr(registry_module, "_MAX_RETAINED_RUNS", 2)
    reg = RunRegistry()

    async def _run():
        run1 = reg.create("wf", "input 0")
        _complete(reg, run1.id)
        reg.subscribe(run1.id)  # active subscriber -- must survive

        run2 = reg.create("wf", "input 1")
        _complete(reg, run2.id)

        run3 = reg.create("wf", "input 2")  # pushes the registry over the bound
        _complete(reg, run3.id)

        assert reg.get(run1.id) is not None   # subscribed -- preserved
        assert reg.get(run2.id) is None       # oldest eligible -- evicted instead
        assert reg.get(run3.id) is not None   # newest -- retained

    asyncio.run(_run())


def test_registry_stays_within_bound_across_many_creates(monkeypatch):
    monkeypatch.setattr(registry_module, "_MAX_RETAINED_RUNS", 5)
    reg = RunRegistry()
    for i in range(50):
        run = reg.create("wf", f"input {i}")
        _complete(reg, run.id)

    assert len(reg._runs) == 5
    assert len(reg._subscribers) == 5


def test_subscribe_returns_none_for_an_evicted_run(monkeypatch):
    # Before eviction existed, once get() returned non-None a run could never
    # later disappear. Eviction breaks that invariant: a run can be evicted by
    # a concurrent create() between a caller's existence check and its
    # subscribe() call. subscribe() must degrade gracefully (None), not raise
    # KeyError on the now-missing id.
    monkeypatch.setattr(registry_module, "_MAX_RETAINED_RUNS", 1)
    reg = RunRegistry()
    run1 = reg.create("wf", "input 0")
    _complete(reg, run1.id)
    reg.create("wf", "input 1")  # evicts run1 (terminal, unsubscribed, over bound)
    assert reg.get(run1.id) is None  # confirm it was actually evicted

    assert reg.subscribe(run1.id) is None


def test_publish_tolerates_an_evicted_run(monkeypatch):
    # A post-terminal event (e.g. `memory_recorded`, published after
    # `run_completed`, SP-3) can arrive after the run was evicted by a concurrent
    # create(). publish() must drop it silently, not KeyError.
    monkeypatch.setattr(registry_module, "_MAX_RETAINED_RUNS", 1)
    reg = RunRegistry()
    run1 = reg.create("wf", "input 0")
    _complete(reg, run1.id)
    reg.create("wf", "input 1")  # evicts run1
    assert reg.get(run1.id) is None

    # No exception; a no-op for the missing run.
    reg.publish(run1.id, {"type": "memory_recorded", "data": "episodic"})


def test_publish_transient_reaches_a_subscriber_without_recording():
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        queue = reg.subscribe(run.id)

        reg.publish_transient(run.id, {"type": "reply_delta", "data": "hi"})

        assert await asyncio.wait_for(queue.get(), timeout=1) == {"type": "reply_delta", "data": "hi"}
        assert reg.get(run.id).events == [], "deltas must not enter the replay log"
        assert reg.get(run.id).status == "running"

    asyncio.run(_run())


def test_a_transient_event_is_never_replayed_to_a_later_subscriber():
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        reg.publish_transient(run.id, {"type": "reply_delta", "data": "hi"})

        queue = reg.subscribe(run.id)
        assert queue.empty()

    asyncio.run(_run())


def test_publish_transient_is_a_no_op_for_an_unknown_run():
    RunRegistry().publish_transient("nope", {"type": "reply_delta", "data": "hi"})
