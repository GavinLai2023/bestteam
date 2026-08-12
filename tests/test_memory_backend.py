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


def test_make_memory_applies_sp4_config(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_RECALL_MAX_CANDIDATES", "50")
    monkeypatch.setenv("BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER", "10")

    mgr = _make_memory()
    assert mgr.recall_max_candidates == 50
    assert mgr.max_episodic_per_user == 10


def test_make_memory_defaults_recall_bound_and_opt_in_retention(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_RECALL_MAX_CANDIDATES", raising=False)
    monkeypatch.delenv("BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER", raising=False)

    mgr = _make_memory()
    assert mgr.recall_max_candidates == 1000  # M-09: production recall bounded by default
    assert mgr.max_episodic_per_user is None  # M-07: retention opt-in


def test_make_memory_embedding_model_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_EMBEDDING_MODEL", raising=False)

    mgr = _make_memory()
    assert mgr.store._embeddings is None


def test_make_memory_wires_embedding_model_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_EMBEDDING_MODEL", "fake:8")

    mgr = _make_memory()
    assert mgr.store._embeddings is not None


def test_make_memory_wires_recency_half_life_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_EMBEDDING_MODEL", "fake:8")
    monkeypatch.setenv("BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS", "30")

    mgr = _make_memory()
    assert mgr.store._recency_half_life_days == 30


def test_make_memory_recency_half_life_default_when_unset_or_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS", raising=False)
    assert _make_memory().store._recency_half_life_days == 14

    monkeypatch.setenv("BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS", "not-a-number")
    assert _make_memory().store._recency_half_life_days == 14

    monkeypatch.setenv("BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS", "-5")
    assert _make_memory().store._recency_half_life_days == 14


def test_make_memory_bad_embedding_spec_disables_memory_entirely(monkeypatch, tmp_path):
    # Documents the deliberate all-or-nothing failure mode: a misconfigured
    # BESTTEAM_MEMORY_EMBEDDING_MODEL disables memory entirely (inherits
    # _make_memory's existing broad except), same as a bad BESTTEAM_MEMORY_DB.
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_EMBEDDING_MODEL", "fake:not-an-int")

    assert _make_memory() is None


def test_make_memory_query_expansion_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL", raising=False)

    mgr = _make_memory()
    assert mgr.query_expansion_model is None


def test_make_memory_wires_query_expansion_model_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL", "fake:{}")

    mgr = _make_memory()
    assert mgr.query_expansion_model == "fake:{}"


def test_make_memory_wires_query_expansion_count_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT", "5")

    mgr = _make_memory()
    assert mgr.query_expansion_count == 5


def test_make_memory_query_expansion_count_defaults_to_three(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT", raising=False)

    assert _make_memory().query_expansion_count == 3


def test_make_memory_query_expansion_count_zero_reaches_manager_as_disabled(monkeypatch, tmp_path):
    # 0 must reach MemoryManager as-is (its own <=0 disable contract), not
    # silently fall back to the default 3 -- _env_int's normal "non-positive ->
    # default" clamp is deliberately bypassed for this specific knob.
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT", "0")

    assert _make_memory().query_expansion_count == 0


def test_make_memory_query_expansion_count_negative_reaches_manager_as_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_QUERY_EXPANSION_COUNT", "-1")

    assert _make_memory().query_expansion_count == -1


def test_make_memory_bad_query_expansion_spec_does_not_disable_memory(monkeypatch, tmp_path):
    # Contrast with test_make_memory_bad_embedding_spec_disables_memory_entirely:
    # query_expansion_model is resolved lazily per-call (like extraction_model),
    # not eagerly at store construction, so a bad spec never disables memory.
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL", "not-a-real-provider:whatever")

    mgr = _make_memory()
    assert mgr is not None
    assert mgr.query_expansion_model == "not-a-real-provider:whatever"


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


def test_run_in_background_records_with_run_principal_id(monkeypatch, tmp_path):
    # Deletion-lifecycle: the episodic record carries the run's principal_id, and a
    # different principal (recreated account) recalls nothing of it (finding 1).
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(db_path))
    monkeypatch.delenv("BESTTEAM_MEMORY_MODEL", raising=False)

    run = registry.create("wf", "hello there")
    run_in_background(
        run.id, _workflow(), "hello there", engine=None,
        user_id="alice", org_id=5, principal_id="P1",
    )

    store = SqliteBM25Memory(str(db_path))
    records = store.all("alice", org_id=5, principal_id=None)
    assert len(records) == 1 and records[0].principal_id == "P1"
    # A recreated account (new principal) recalls nothing of the old one's memory.
    assert store.all("alice", org_id=5, principal_id="P2") == []
    store.close()


def test_run_in_background_records_with_run_workflow_id(monkeypatch, tmp_path):
    # The episodic record carries the run's workflow_id, and a different
    # workflow recalls nothing of it.
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(db_path))
    monkeypatch.delenv("BESTTEAM_MEMORY_MODEL", raising=False)

    run = registry.create("wf", "hello there")
    run_in_background(
        run.id, _workflow(), "hello there", engine=None, user_id="alice", workflow_id=1,
    )

    store = SqliteBM25Memory(str(db_path))
    records = store.all("alice", workflow_id=None)
    assert len(records) == 1
    assert records[0].workflow_id == 1
    # Another workflow sees nothing of workflow 1's episodic memory.
    assert store.all("alice", workflow_id=2) == []
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
                content='{"facts": [{"action": "add", "content": "likes bullets"}], '
                '"procedural": "answered concisely"}',
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


def test_run_in_background_meters_memory_query_expansion(monkeypatch):
    # The query-expansion LLM call's usage lands in usage_records tagged
    # agent="memory:query_expansion", mirroring the extraction-side test above.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from bestteam import MemoryManager

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)

    expansion = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"queries": ["alt phrasing"]}',
                usage_metadata={"input_tokens": 9, "output_tokens": 3, "total_tokens": 12},
            )
        ]
    )
    mgr = MemoryManager(SqliteBM25Memory(":memory:"), query_expansion_model=expansion, org_id=5)
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: mgr)

    run = registry.create("wf", "hello")
    run_in_background(run.id, _workflow(), "hello", engine=engine, user_id="alice", org_id=5)

    with Session() as db:
        mem_rows = [r for r in list_usage_for_run(db, run.id) if r.agent == "memory:query_expansion"]
    assert len(mem_rows) == 1
    assert mem_rows[0].input_tokens == 9 and mem_rows[0].output_tokens == 3
    assert mem_rows[0].org_id == 5


def test_run_in_background_meters_query_expansion_usage_even_when_recall_search_fails(monkeypatch):
    # Mirrors test_total_write_failure_still_meters_extraction, applied to the
    # recall side: the expansion call already happened and is billable even
    # though the store search that follows it fails.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from bestteam import MemoryManager

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)

    class _FailSearchStore(SqliteBM25Memory):
        def search(self, *args, **kwargs):
            raise RuntimeError("boom")

    expansion = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"queries": ["alt phrasing"]}',
                usage_metadata={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
            )
        ]
    )
    mgr = MemoryManager(_FailSearchStore(":memory:"), query_expansion_model=expansion, org_id=5)
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: mgr)

    run = registry.create("wf", "hi")
    run_in_background(run.id, _workflow(), "hi", engine=engine, user_id="alice", org_id=5)

    with Session() as db:
        mem_rows = [r for r in list_usage_for_run(db, run.id) if r.agent == "memory:query_expansion"]
    assert len(mem_rows) == 1  # billed despite the recall search failing
    assert mem_rows[0].input_tokens == 11 and mem_rows[0].output_tokens == 4
    events = registry.get(run.id).events
    assert any(e["type"] == "run_completed" for e in events)


def test_usage_persistence_failure_does_not_fail_run(monkeypatch):
    # Review r5 #1: a usage_records write failing must not flip a successful run
    # to run_failed (metering is isolated from run status).
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from bestteam import MemoryManager

    engine = make_engine(":memory:")
    init_db(engine)

    extraction = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"facts": [{"action": "add", "content": "x"}], "procedural": "y"}',
                usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            )
        ]
    )
    mgr = MemoryManager(SqliteBM25Memory(":memory:"), extraction_model=extraction, org_id=5)
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: mgr)

    def boom(*args, **kwargs):
        raise RuntimeError("usage db down")

    monkeypatch.setattr("ui.backend.runtime.record_usage", boom)

    run = registry.create("wf", "hi")
    run_in_background(run.id, _workflow(), "hi", engine=engine, user_id="alice", org_id=5)

    events = registry.get(run.id).events
    assert any(e["type"] == "run_completed" for e in events)
    assert not any(e["type"] == "run_failed" for e in events)


def test_total_write_failure_still_meters_extraction(monkeypatch):
    # Review r6 #1: a paid extraction call must be billed even when EVERY memory
    # write fails -- exactly one memory:extraction usage row.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from bestteam import MemoryManager

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)

    class _FailAddStore(SqliteBM25Memory):
        def add(self, *args, **kwargs):
            raise RuntimeError("boom")

    extraction = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"facts": [{"action": "add", "content": "x"}], "procedural": "y"}',
                usage_metadata={"input_tokens": 17, "output_tokens": 4, "total_tokens": 21},
            )
        ]
    )
    mgr = MemoryManager(_FailAddStore(":memory:"), extraction_model=extraction, org_id=5)
    monkeypatch.setattr("ui.backend.runtime._make_memory", lambda *a, **k: mgr)

    run = registry.create("wf", "hi")
    run_in_background(run.id, _workflow(), "hi", engine=engine, user_id="alice", org_id=5)

    with Session() as db:
        mem_rows = [r for r in list_usage_for_run(db, run.id) if r.agent == "memory:extraction"]
    assert len(mem_rows) == 1  # billed despite zero successful writes
    assert mem_rows[0].input_tokens == 17 and mem_rows[0].output_tokens == 4
    assert mem_rows[0].org_id == 5
    events = registry.get(run.id).events
    assert any(e["type"] == "run_completed" for e in events)


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


def test_make_memory_rerank_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_RERANK_MODEL", raising=False)

    mgr = _make_memory()
    assert mgr.rerank_model is None


def test_make_memory_wires_rerank_model_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_RERANK_MODEL", "fake:")

    mgr = _make_memory()
    assert mgr.rerank_model == "fake:"


def test_make_memory_wires_rerank_candidate_k_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_RERANK_CANDIDATE_K", "30")

    mgr = _make_memory()
    assert mgr.rerank_candidate_k == 30


def test_make_memory_bad_rerank_spec_does_not_disable_memory(monkeypatch, tmp_path):
    # Contrast with test_make_memory_bad_embedding_spec_disables_memory_entirely:
    # rerank_model is resolved lazily (like query_expansion_model), not
    # eagerly at store construction, so a bad spec never disables memory.
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_RERANK_MODEL", "not-a-real-provider:whatever")

    mgr = _make_memory()
    assert mgr is not None
    assert mgr._get_reranker() is None
