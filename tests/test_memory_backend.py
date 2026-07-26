"""Tests for backend memory wiring (`ui/backend/runtime.py`).

Follows the direct-call pattern of `test_usage_metering.py` (invoking
`run_in_background` synchronously) rather than the racy async `POST /api/runs`
executor path.
"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from bestteam import (
    Agent,
    CollaborationMode,
    MemoryManager,
    SqliteBM25Memory,
    Team,
    Workflow,
)
from bestteam.core.memory import EPISODIC
from ui.backend.runtime import _make_memory, registry, run_in_background


class _CloseSpyManager(MemoryManager):
    """MemoryManager that counts close() calls (still closes the real store)."""

    def __init__(self, store):
        super().__init__(store)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        super().close()


def _workflow():
    agent = Agent(
        name="a", role="role-a", goal="goal-a", model=FakeListChatModel(responses=["done"])
    )
    return Workflow(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])


def test_make_memory_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)
    assert _make_memory() is None

    monkeypatch.setenv("BESTTEAM_MEMORY_DB", "   ")
    assert _make_memory() is None


def test_make_memory_enabled_with_db_path(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_MODEL", raising=False)

    manager = _make_memory()
    assert manager is not None
    assert manager.extraction_model is None


def test_run_in_background_records_episodic_memory_for_user(monkeypatch, tmp_path):
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(db_path))
    monkeypatch.delenv("BESTTEAM_MEMORY_MODEL", raising=False)

    workflow = _workflow()
    run = registry.create("wf", "hello there")

    run_in_background(run.id, workflow, "hello there", engine=None, user_id="alice")

    # Re-open the same DB file and assert an episodic row for alice exists.
    store = SqliteBM25Memory(str(db_path))
    records = store.all("alice")
    assert len(records) == 1
    assert records[0].type == EPISODIC
    assert "hello there" in records[0].content


def test_run_in_background_no_memory_when_user_absent(monkeypatch, tmp_path):
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(db_path))

    workflow = _workflow()
    run = registry.create("wf", "hello there")

    # No user_id -> memory stays disabled even though the env var is set.
    run_in_background(run.id, workflow, "hello there", engine=None)

    assert not db_path.exists()


def test_run_in_background_closes_memory_store_on_success(monkeypatch):
    # M-03: the per-run memory store is closed in run_in_background's finally.
    spy = _CloseSpyManager(SqliteBM25Memory(":memory:"))
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda: spy)

    run = registry.create("wf", "hello")
    run_in_background(run.id, _workflow(), "hello", engine=None, user_id="alice")

    assert spy.close_calls == 1


def test_run_in_background_closes_memory_store_on_failure(monkeypatch):
    # The close must run even when the worker path raises (it's in finally).
    spy = _CloseSpyManager(SqliteBM25Memory(":memory:"))
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda: spy)

    workflow = _workflow()

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(workflow, "stream", boom)
    run = registry.create("wf", "hello")

    run_in_background(run.id, workflow, "hello", engine=None, user_id="alice")

    assert spy.close_calls == 1
    events = registry.get(run.id).events
    assert any(e["type"] == "run_failed" for e in events)


def test_run_in_background_no_memory_when_env_unset(monkeypatch):
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)

    workflow = _workflow()
    run = registry.create("wf", "hello there")

    # Runs fine with a user but no configured store — no error, nothing stored.
    run_in_background(run.id, workflow, "hello there", engine=None, user_id="alice")

    events = registry.get(run.id).events
    assert any(e["type"] == "run_completed" for e in events)
