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
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.usage import list_usage_for_run
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


def test_run_in_background_records_with_run_org_id(monkeypatch, tmp_path):
    # SP-2: the episodic record carries the run's org_id, and a different org
    # recalls nothing of it.
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(db_path))
    monkeypatch.delenv("BESTTEAM_MEMORY_MODEL", raising=False)

    run = registry.create("wf", "hello there")
    run_in_background(run.id, _workflow(), "hello there", engine=None, user_id="alice", org_id=5)

    store = SqliteBM25Memory(str(db_path))
    records = store.all("alice", org_id=None)
    assert len(records) == 1
    assert records[0].org_id == 5
    # Another org sees nothing of alice's org-5 memory.
    assert store.all("alice", org_id=6) == []
    store.close()


def test_run_in_background_stamps_provenance_metadata(monkeypatch, tmp_path):
    # SP-3 M-06: the run's id + workflow_version_id are stamped into each record's
    # metadata (via the real _make_memory binding path).
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(db_path))
    monkeypatch.delenv("BESTTEAM_MEMORY_MODEL", raising=False)

    run = registry.create("wf", "hello there")
    run_in_background(
        run.id, _workflow(), "hello there", engine=None,
        user_id="alice", org_id=5, workflow_version_id=9,
    )

    store = SqliteBM25Memory(str(db_path))
    rec = store.all("alice", org_id=None)[0]
    assert rec.metadata == {"run_id": run.id, "workflow_version_id": 9}
    assert rec.org_id == 5
    store.close()


def test_run_in_background_meters_memory_extraction(monkeypatch):
    # SP-3 M-04: the extraction LLM call's usage lands in usage_records tagged
    # agent="memory:extraction", carrying the run's org.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from bestteam import MemoryManager

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)

    extraction = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"facts": ["likes bullets"], "procedural": "answered concisely"}',
                usage_metadata={"input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
            )
        ]
    )
    mgr = MemoryManager(SqliteBM25Memory(":memory:"), extraction_model=extraction, org_id=5)
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: mgr)

    run = registry.create("wf", "hello")
    run_in_background(run.id, _workflow(), "hello", engine=engine, user_id="alice", org_id=5)

    with Session() as db:
        mem_rows = [r for r in list_usage_for_run(db, run.id) if r.agent == "memory:extraction"]
    assert len(mem_rows) == 1
    assert mem_rows[0].input_tokens == 20 and mem_rows[0].output_tokens == 6
    assert mem_rows[0].org_id == 5


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
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: spy)

    run = registry.create("wf", "hello")
    run_in_background(run.id, _workflow(), "hello", engine=None, user_id="alice")

    assert spy.close_calls == 1


def test_run_in_background_closes_memory_store_on_failure(monkeypatch):
    # The close must run even when the worker path raises (it's in finally).
    spy = _CloseSpyManager(SqliteBM25Memory(":memory:"))
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: spy)

    workflow = _workflow()

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(workflow, "stream", boom)
    run = registry.create("wf", "hello")

    run_in_background(run.id, workflow, "hello", engine=None, user_id="alice")

    assert spy.close_calls == 1
    events = registry.get(run.id).events
    assert any(e["type"] == "run_failed" for e in events)


class _CloseRaisesManager(MemoryManager):
    """MemoryManager whose close() raises (simulates a flaky custom store)."""

    def close(self):
        raise RuntimeError("close failed")


def test_run_in_background_survives_memory_close_failure(monkeypatch):
    # A store whose close() raises must not escape the worker; teardown is
    # best-effort and the terminal run state is preserved.
    spy = _CloseRaisesManager(SqliteBM25Memory(":memory:"))
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: spy)

    run = registry.create("wf", "hello")
    # Returns normally despite close() raising inside the finally.
    run_in_background(run.id, _workflow(), "hello", engine=None, user_id="alice")

    events = registry.get(run.id).events
    assert any(e["type"] == "run_completed" for e in events)
    assert not any(e["type"] == "run_failed" for e in events)


def test_run_in_background_no_memory_when_env_unset(monkeypatch):
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)

    workflow = _workflow()
    run = registry.create("wf", "hello there")

    # Runs fine with a user but no configured store — no error, nothing stored.
    run_in_background(run.id, workflow, "hello there", engine=None, user_id="alice")

    events = registry.get(run.id).events
    assert any(e["type"] == "run_completed" for e in events)
