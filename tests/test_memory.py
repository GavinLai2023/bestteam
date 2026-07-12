"""Tests for the per-user memory store and manager (`core/memory.py`)."""

import pytest

from bestteam import MemoryManager, MemoryRecord, SqliteBM25Memory
from bestteam.core.memory import EPISODIC, PROCEDURAL, SEMANTIC


def _store():
    return SqliteBM25Memory(":memory:")


def test_add_returns_record_with_id_and_timestamp():
    store = _store()
    record = store.add("alice", EPISODIC, "asked about refunds")

    assert isinstance(record, MemoryRecord)
    assert record.id
    assert record.created_at
    assert record.user_id == "alice"
    assert record.type == EPISODIC
    assert record.content == "asked about refunds"


def test_add_persists_metadata():
    store = _store()
    store.add("alice", EPISODIC, "hi", metadata={"input": "x", "output": "y"})

    (record,) = store.all("alice")
    assert record.metadata == {"input": "x", "output": "y"}


def test_search_ranks_by_relevance_and_isolates_users():
    store = _store()
    store.add("alice", EPISODIC, "user asked about the refund policy")
    store.add("alice", EPISODIC, "user asked about shipping times")
    store.add("bob", EPISODIC, "user asked about the refund policy")

    hits = store.search("alice", "refund")

    assert len(hits) == 1
    assert "refund" in hits[0].content
    # bob's identical row must never surface for alice.
    assert all(h.user_id == "alice" for h in hits)


def test_search_type_filter():
    store = _store()
    store.add("alice", EPISODIC, "refund question happened")
    store.add("alice", SEMANTIC, "prefers refund details up front")

    hits = store.search("alice", "refund", types=[SEMANTIC])

    assert len(hits) == 1
    assert hits[0].type == SEMANTIC


def test_search_empty_when_no_records():
    assert _store().search("nobody", "anything") == []


def test_search_cjk_query_matches_cjk_content():
    store = _store()
    store.add("bob", EPISODIC, "用户询问退款政策")
    store.add("bob", EPISODIC, "用户询问配送时间")

    hits = store.search("bob", "退款")

    assert hits
    assert hits[0].content == "用户询问退款政策"


def test_delete_removes_record():
    store = _store()
    record = store.add("alice", EPISODIC, "temporary")

    store.delete(record.id)

    assert store.all("alice") == []


def test_all_orders_newest_first_and_filters_type():
    store = _store()
    store.add("alice", EPISODIC, "first")
    store.add("alice", SEMANTIC, "a fact")

    everything = store.all("alice")
    assert len(everything) == 2

    semantic_only = store.all("alice", types=[SEMANTIC])
    assert [r.type for r in semantic_only] == [SEMANTIC]


def test_sqlite_bm25_requires_rank_bm25(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "rank_bm25":
            raise ImportError("no rank_bm25")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from bestteam.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="rank-bm25"):
        SqliteBM25Memory(":memory:")


def test_recall_preamble_empty_without_user_or_hits():
    store = _store()
    manager = MemoryManager(store)

    assert manager.recall_preamble(None, "anything") == ""
    assert manager.recall_preamble("alice", "anything") == ""


def test_recall_preamble_formats_hits():
    store = _store()
    store.add("alice", SEMANTIC, "prefers concise bullet-point answers")
    manager = MemoryManager(store)

    preamble = manager.recall_preamble("alice", "how should I format bullet answers")

    assert "prefers concise bullet-point answers" in preamble
    assert "previous sessions" in preamble


def test_record_run_writes_only_episodic_without_model():
    store = _store()
    manager = MemoryManager(store)

    manager.record_run("alice", "how do refunds work?", "You get money back in 30 days")

    records = store.all("alice")
    assert len(records) == 1
    assert records[0].type == EPISODIC
    assert "how do refunds work?" in records[0].content


def test_record_run_noop_without_user():
    store = _store()
    MemoryManager(store).record_run(None, "x", "y")
    assert store.all("alice") == []


def test_record_run_extracts_semantic_and_procedural_with_model():
    store = _store()
    canned = '{"facts": ["prefers bullet points", "works in finance"], "procedural": "refund questions handled by citing the 30-day policy"}'
    manager = MemoryManager(store, extraction_model=f"fake:{canned}")

    manager.record_run("alice", "how do refunds work?", "30-day money back")

    by_type = {}
    for record in store.all("alice"):
        by_type.setdefault(record.type, []).append(record.content)

    assert len(by_type[EPISODIC]) == 1
    assert set(by_type[SEMANTIC]) == {"prefers bullet points", "works in finance"}
    assert len(by_type[PROCEDURAL]) == 1


def test_record_run_tolerates_unparseable_model_output():
    store = _store()
    manager = MemoryManager(store, extraction_model="fake:sorry, I can't do that")

    # Episodic must still be written even when extraction yields no JSON.
    manager.record_run("alice", "q", "a")

    records = store.all("alice")
    assert [r.type for r in records] == [EPISODIC]


def test_record_run_extraction_tolerates_json_with_surrounding_prose():
    store = _store()
    canned = 'Here you go:\n```json\n{"facts": ["likes graphs"], "procedural": ""}\n```'
    manager = MemoryManager(store, extraction_model=f"fake:{canned}")

    manager.record_run("alice", "q", "a")

    types = sorted(r.type for r in store.all("alice"))
    assert types == [EPISODIC, SEMANTIC]
