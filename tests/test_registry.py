"""Unit tests for RunRegistry's bounded eviction (memory growth under the
autonomous trigger's unattended, continuous run creation)."""

import ui.backend.registry as registry_module
from ui.backend.registry import RunRegistry


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
