# Bounded RunRegistry Eviction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound `RunRegistry`'s in-memory growth so the process's memory
stops growing without limit under the autonomous email trigger's unattended,
continuous run creation.

**Architecture:** One module-level constant plus one eviction check inside
`RunRegistry.create()`'s existing lock — no new lock, no background sweep,
no new configuration surface. See the design spec for full rationale.

**Tech Stack:** Python 3 / `threading.Lock` (existing), pytest.

## Global Constraints

- Full backend suite must stay green:
  `.\.venv\Scripts\python.exe -m pytest`
- `_MAX_RETAINED_RUNS = 1000`, a hardcoded module constant in
  `ui/backend/registry.py` — not an env var (see design spec's rationale).
- A `running` run, or a terminal run with an active WebSocket subscriber, is
  never evicted.
- No new lock; eviction runs inside `create()`'s existing `with self._lock:`
  block.
- Design spec: `docs/superpowers/specs/2026-07-22-run-registry-bounded-eviction-design.md`.

---

### Task 1: Bounded eviction in `RunRegistry`

**Files:**
- Modify: `ui/backend/registry.py`
- Create: `tests/test_registry.py`

**Interfaces:**
- Produces: `ui.backend.registry._MAX_RETAINED_RUNS` (module-level `int`
  constant, monkeypatchable by tests) and
  `RunRegistry._evict_if_over_bound(self) -> None` (private, assumes
  `self._lock` already held by the caller).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_registry.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_registry.py -v`
Expected: FAIL — with today's `RunRegistry`, nothing is ever evicted, so
`test_eviction_leaves_registry_under_the_bound` and
`test_registry_stays_within_bound_across_many_creates` fail (registry keeps
growing past the bound); the other two tests may incidentally pass already
(nothing to evict yet), which is fine — the two growth-bound tests are the
ones that must fail for the right reason.

- [ ] **Step 3: Implement**

In `ui/backend/registry.py`, add the constant right before the `RunRegistry`
class:
```python
class Run:
```
becomes (insert before the blank line preceding `class RunRegistry`):
```python
class Run:
```
... (Run dataclass unchanged) ...
```python
_MAX_RETAINED_RUNS = 1000


class RunRegistry:
```

Replace `create()`:
```python
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
        return run
```
with:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_registry.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all PASS (no regressions — `_MAX_RETAINED_RUNS = 1000` is far above
anything any existing test creates, so no existing test's runs get evicted).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/registry.py tests/test_registry.py
git commit -m "fix(registry): bound RunRegistry growth via terminal-run eviction"
```

---

### Task 2: Docs

**Files:**
- Modify: `ui/backend/CLAUDE.md`
- Modify: `docs/STATUS.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `ui/backend/CLAUDE.md`**

In the "Sync-to-async streaming bridge" section, the paragraph currently
ends with:
```
...blocking `to_thread` call isn't cancellable, so it hung the same way when a
client disconnected before the run finished.)
```
Append a new paragraph directly after it:
```
`RunRegistry` bounds its own growth (`_MAX_RETAINED_RUNS = 1000`, hardcoded
in `registry.py`): every `create()` call evicts the oldest terminal
(non-`running`), subscriber-free runs until back within the bound. Added
because the autonomous email trigger creates runs unattended and
indefinitely, unlike the previous purely human-click-triggered regime this
registry was originally sized for. A `running` run or one with an active
WebSocket subscriber is never evicted. The autonomous-trigger activity list
(`GET /api/org/email-trigger/activity`) is unaffected -- it reads the
persisted `runs` table, not the registry; only the monitoring dashboard's
`GET /api/runs/{id}` and its stream WebSocket can miss a very old, evicted
run, which they already handle as "unknown run" (404 / WS 4404). Spec:
`docs/superpowers/specs/2026-07-22-run-registry-bounded-eviction-design.md`.
```

In the "Autonomous email trigger" section, the paragraph currently ends with
(after round-2 hardening):
```
...Deferred: `RunRegistry` eviction and awaiting in-flight
polling threads on shutdown (see `docs/STATUS.md`, Known issues).
```
Replace with:
```
...Deferred: awaiting in-flight polling threads on shutdown (see
`docs/STATUS.md`, Known issues). `RunRegistry` eviction (the other
previously-deferred item) is no longer deferred -- see "Sync-to-async
streaming bridge" above.
```

- [ ] **Step 2: Update `docs/STATUS.md`**

Insert a new "Done" bullet right after the "Autonomous email-trigger
hardening round 2" bullet:
```markdown
- Bounded `RunRegistry` eviction: a third-round independent review
  re-raised the RunRegistry unbounded-growth finding (previously deferred
  twice) on the basis that the autonomous trigger removes the
  human-rate-limiter that made those deferrals safe -- unattended,
  continuous run creation vs. click-driven. `create()` now evicts the
  oldest terminal, subscriber-free runs once the registry exceeds
  `_MAX_RETAINED_RUNS` (1000, hardcoded). Spec:
  `2026-07-22-run-registry-bounded-eviction-design.md`.
```

Replace the "`RunRegistry` remains the in-memory live layer" bullet under
"Known issues / tech debt":
```markdown
- **`RunRegistry` remains the in-memory live layer** — a `runs` row is now
  persisted per run (CR-012) so usage/trace foreign keys are valid, but
  `trace_events` persistence, restart recovery, and a run-history API remain
  Phase 5. See `ui/backend/db/CLAUDE.md`.
```
with:
```markdown
- **`RunRegistry` remains the in-memory live layer** — a `runs` row is now
  persisted per run (CR-012) so usage/trace foreign keys are valid, but
  `trace_events` persistence, restart recovery, and a run-history API remain
  Phase 5. Growth is now bounded (terminal-run eviction, see Done above) —
  what's left is that a restart still loses all live trace-event history,
  not that it grows unbounded. See `ui/backend/db/CLAUDE.md`.
```

Also replace the "Autonomous trigger residuals" bullet:
```markdown
- **Autonomous trigger residuals:** `asyncio.to_thread` poll cycles aren't
  awaited on shutdown, so a mailbox check/commit/dispatch already in flight
  can keep running briefly after the ASGI shutdown handler returns; a process
  killed between a trigger's state commit and dispatch orphans a `runs` row
  (overlap guard self-recovers on restart; no reconciliation sweep yet);
  `RunRegistry` never evicts terminal runs, so autonomous volume grows
  process memory.
```
with:
```markdown
- **Autonomous trigger residuals:** `asyncio.to_thread` poll cycles aren't
  awaited on shutdown, so a mailbox check/commit/dispatch already in flight
  can keep running briefly after the ASGI shutdown handler returns; a process
  killed between a trigger's state commit and dispatch orphans a `runs` row
  (overlap guard self-recovers on restart; no reconciliation sweep yet).
```
(the `RunRegistry` clause is removed from this bullet since it's now fixed,
recorded in Done instead.)

- [ ] **Step 3: Commit**

```bash
git add ui/backend/CLAUDE.md docs/STATUS.md
git commit -m "docs(registry): record bounded RunRegistry eviction"
```

## Self-Review

**Spec coverage:** the design spec's bound/policy/lock-placement/testing
sections all map to Task 1; the docs section maps to Task 2. No gaps.

**Placeholder scan:** none — every step has complete code.

**Type consistency:** `_evict_if_over_bound(self) -> None` is the only new
method; used only internally by `create()`. No other task/file references it.
