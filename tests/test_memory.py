"""Tests for the per-user memory store and manager (`core/memory.py`)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.embeddings import Embeddings

from bestteam import Memory, MemoryManager, MemoryRecord, SqliteBM25Memory
from bestteam.core.memory import (
    EPISODIC,
    LEGACY_ORG,
    PROCEDURAL,
    SEMANTIC,
    _reciprocal_rank_fusion,
    _recency_weight,
)


class _LegacyStore(Memory):
    """A pre-SP-2 store: implements only the original ABC (no org_id kwarg)."""

    def __init__(self):
        self.records: list[MemoryRecord] = []

    def add(self, user_id, type, content, metadata=None):
        rec = MemoryRecord(id=str(len(self.records)), user_id=user_id, type=type, content=content)
        self.records.append(rec)
        return rec

    def search(self, user_id, query, types=None, top_k=5):
        return [r for r in self.records if r.user_id == user_id][:top_k]

    def all(self, user_id, types=None):
        return [r for r in self.records if r.user_id == user_id]

    def delete(self, memory_id):
        self.records = [r for r in self.records if r.id != memory_id]


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


def test_created_at_is_timezone_aware():
    # CR-018: records must use timezone-aware UTC (matches db/models._utcnow),
    # not the deprecated naive datetime.utcnow().
    from datetime import datetime

    store = _store()
    record = store.add("alice", EPISODIC, "hi")

    parsed = datetime.fromisoformat(record.created_at)
    assert parsed.tzinfo is not None


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


def test_user_ids_lists_distinct_users_with_memory():
    store = _store()
    store.add("alice", EPISODIC, "a1")
    store.add("alice", SEMANTIC, "a2")
    store.add("bob", EPISODIC, "b1")

    assert set(store.user_ids()) == {"alice", "bob"}
    # No memory -> empty.
    assert _store().user_ids() == []


def test_delete_user_removes_all_records_for_that_user_only():
    store = _store()
    store.add("alice", EPISODIC, "a1")
    store.add("alice", SEMANTIC, "a2")
    store.add("bob", EPISODIC, "b1")

    removed = store.delete_user("alice")

    assert removed == 2
    assert store.all("alice") == []
    assert {r.content for r in store.all("bob")} == {"b1"}
    # Deleting a user with no records is a no-op returning 0.
    assert store.delete_user("nobody") == 0


def test_user_summaries_counts_by_type():
    store = _store()
    store.add("alice", EPISODIC, "e1")
    store.add("alice", EPISODIC, "e2")
    store.add("alice", SEMANTIC, "s1")
    store.add("bob", PROCEDURAL, "p1")

    summaries = {s["user_id"]: s for s in store.user_summaries()}
    assert summaries["alice"] == {
        "user_id": "alice",
        "org_id": None,
        "episodic": 2,
        "semantic": 1,
        "procedural": 0,
        "total": 3,
    }
    assert summaries["bob"]["procedural"] == 1
    assert summaries["bob"]["total"] == 1


def test_all_respects_limit():
    store = _store()
    for i in range(5):
        store.add("alice", EPISODIC, f"rec {i}")

    assert len(store.all("alice")) == 5
    assert len(store.all("alice", limit=2)) == 2


def test_search_bounds_candidate_scan(monkeypatch):
    # `max_candidates` must bound the WORK (records loaded + tokenized + indexed),
    # not just the returned slice, so a search over a bloated store is bounded.
    store = _store()
    for i in range(6):
        store.add("alice", EPISODIC, f"refund note {i}")

    captured = {}
    real_candidates = store._candidates

    def spy_candidates(
        user_id, types=None, limit=None, *, org_id=None, principal_id=None, workflow_id=None,
        include_embeddings=True,
    ):
        captured["limit"] = limit
        return real_candidates(
            user_id, types, limit, org_id=org_id, principal_id=principal_id, workflow_id=workflow_id,
            include_embeddings=include_embeddings,
        )

    monkeypatch.setattr(store, "_candidates", spy_candidates)
    hits = store.search("alice", "refund", top_k=2, max_candidates=3)

    assert captured["limit"] == 3  # loaded at most 3 rows, not all 6
    assert len(hits) <= 2


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


def test_recall_preamble_frames_memory_as_untrusted_reference():
    # CR-021: recalled content is untrusted data (a prior tool result or model
    # output could carry injected instructions), so the preamble must delimit it
    # and mark it reference-only rather than splicing it in as bare instructions.
    store = _store()
    store.add("alice", SEMANTIC, "prefers concise answers")
    preamble = MemoryManager(store).recall_preamble("alice", "concise answers")

    assert "<recalled_user_memory>" in preamble
    assert "</recalled_user_memory>" in preamble
    lowered = preamble.lower()
    assert "not" in lowered and "instruction" in lowered


def test_record_run_writes_only_episodic_without_model():
    store = _store()
    manager = MemoryManager(store)

    manager.record_run("alice", "how do refunds work?", "You get money back in 30 days")

    records = store.all("alice")
    assert len(records) == 1
    assert records[0].type == EPISODIC
    assert "how do refunds work?" in records[0].content


def test_record_run_does_not_duplicate_content_in_metadata():
    # CR-022: the episodic record already carries input/output in `content`;
    # duplicating them into `metadata` doubles storage for no reader.
    store = _store()
    MemoryManager(store).record_run("alice", "how do refunds work?", "30 days")

    (record,) = store.all("alice")
    assert record.metadata == {}


def test_record_run_caps_record_size():
    # CR-022: an unbounded input (RunRequest.input has no length limit) must not
    # be persisted whole — each field is truncated before storage.
    from bestteam.core.memory import _MAX_RECORD_CHARS

    store = _store()
    big = "x" * (_MAX_RECORD_CHARS * 3)
    MemoryManager(store).record_run("alice", big, big)

    (record,) = store.all("alice")
    # both fields capped + a little framing; nowhere near the 6*cap raw input.
    assert len(record.content) <= 2 * _MAX_RECORD_CHARS + 100
    assert "truncated" in record.content


def test_record_run_noop_without_user():
    store = _store()
    MemoryManager(store).record_run(None, "x", "y")
    assert store.all("alice") == []


def test_record_run_extracts_semantic_and_procedural_with_model():
    store = _store()
    canned = (
        '{"facts": [{"action": "add", "content": "prefers bullet points"}, '
        '{"action": "add", "content": "works in finance"}], '
        '"procedural": "refund questions handled by citing the 30-day policy"}'
    )
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


# --- SP-3: provenance, extraction usage, recall count -----------------------


def test_record_run_returns_outcome_and_stamps_provenance():
    from bestteam.core.memory import MemoryOutcome

    store = _store()
    mgr = MemoryManager(store, run_id="run-1", workflow_version_id=7)

    outcome = mgr.record_run("alice", "q", "a")
    assert isinstance(outcome, MemoryOutcome)
    assert outcome.recorded == [EPISODIC]
    assert outcome.extraction_usage is None
    assert store.all("alice")[0].metadata == {"run_id": "run-1", "workflow_version_id": 7}


def test_record_run_provenance_omits_unbound_ids():
    store = _store()
    MemoryManager(store).record_run("alice", "q", "a")  # no run_id / version bound
    assert store.all("alice")[0].metadata == {}


def test_record_run_captures_extraction_usage_and_types():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    canned = AIMessage(
        content='{"facts": [{"action": "add", "content": "likes bullets"}], "procedural": "answered concisely"}',
        usage_metadata={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
    )
    mgr = MemoryManager(
        store, extraction_model=FakeMessagesListChatModel(responses=[canned]), run_id="r1"
    )

    outcome = mgr.record_run("alice", "q", "a")
    assert outcome.recorded == [EPISODIC, SEMANTIC, PROCEDURAL]
    assert outcome.extraction_usage == {"model": None, "input_tokens": 12, "output_tokens": 4}
    # Provenance is stamped on the extracted records too.
    sem = [r for r in store.all("alice") if r.type == SEMANTIC][0]
    assert sem.metadata == {"run_id": "r1"}


def test_record_run_partial_extraction_failure_keeps_usage_and_writes():
    # Review r4 #1: a failed procedural write must not discard the captured usage
    # or the semantic row that already committed.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class _FailProceduralStore(SqliteBM25Memory):
        def add(self, user_id, type, content, metadata=None, *, org_id=None):
            if type == PROCEDURAL:
                raise RuntimeError("write failed")
            return super().add(user_id, type, content, metadata, org_id=org_id)

    canned = AIMessage(
        content='{"facts": [{"action": "add", "content": "likes bullets"}], "procedural": "answered concisely"}',
        usage_metadata={"input_tokens": 31, "output_tokens": 9, "total_tokens": 40},
    )
    store = _FailProceduralStore(":memory:")
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    outcome = mgr.record_run("alice", "q", "a")
    # Usage billed (tokens were consumed) and the successful writes are reported.
    assert outcome.extraction_usage == {"model": None, "input_tokens": 31, "output_tokens": 9}
    assert outcome.recorded == [EPISODIC, SEMANTIC]  # procedural failed, not reported
    # The outcome agrees with what's actually stored.
    assert sorted(r.type for r in store.all("alice")) == [EPISODIC, SEMANTIC]


def test_extraction_continues_after_a_failed_write():
    # Review r5 #2: one failing write must not skip later independent writes, and
    # the partial failure is flagged (ok=False) while usage + successes are kept.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class _FailFirstFactStore(SqliteBM25Memory):
        def __init__(self, path=":memory:"):
            super().__init__(path)
            self._failed = False

        def add(self, user_id, type, content, metadata=None, *, org_id=None):
            if type == SEMANTIC and not self._failed:
                self._failed = True
                raise RuntimeError("first fact rejected")
            return super().add(user_id, type, content, metadata, org_id=org_id)

    canned = AIMessage(
        content=(
            '{"facts": [{"action": "add", "content": "fact one"}, '
            '{"action": "add", "content": "fact two"}], "procedural": "a note"}'
        ),
        usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    )
    store = _FailFirstFactStore()
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    outcome = mgr.record_run("alice", "q", "a")

    assert outcome.ok is False  # a write failed
    assert outcome.extraction_usage == {"model": None, "input_tokens": 10, "output_tokens": 3}
    # episodic + fact two + procedural persisted; fact one dropped, later writes not skipped
    assert outcome.recorded == [EPISODIC, SEMANTIC, PROCEDURAL]
    contents = {r.content for r in store.all("alice")}
    assert "fact two" in contents and "a note" in contents and "fact one" not in contents


def test_record_run_extraction_usage_none_for_fake_spec():
    store = _store()
    mgr = MemoryManager(store, extraction_model='fake:{"facts": [], "procedural": ""}')

    outcome = mgr.record_run("alice", "q", "a")
    assert outcome.extraction_usage is None  # fake models report no usage_metadata
    assert outcome.recorded == [EPISODIC]


def test_recall_returns_preamble_and_count():
    from bestteam.core.memory import RecallResult

    store = _store()
    store.add("alice", EPISODIC, "the user prefers concise answers about refunds")
    mgr = MemoryManager(store)

    result = mgr.recall("alice", "refunds")
    assert isinstance(result, RecallResult)
    assert result.count == 1
    assert "<recalled_user_memory>" in result.preamble
    # The string wrapper stays backward-compatible.
    assert mgr.recall_preamble("alice", "refunds") == result.preamble


def test_recall_empty_when_no_hits():
    from bestteam.core.memory import RecallResult

    assert MemoryManager(_store()).recall("alice", "anything") == RecallResult(preamble="", count=0)


# --- SP-4: recall bound (M-09), dedup (M-08), retention (M-07) ---------------


def test_recall_forwards_the_configured_scan_bound():
    # M-09: a bounded manager passes max_candidates to search.
    store = _store()
    store.add("u", EPISODIC, "the refund policy is 30 days")
    captured = {}
    real = store.search

    def spy(user_id, query, **kwargs):
        captured.update(kwargs)
        return real(user_id, query, **kwargs)

    store.search = spy
    MemoryManager(store, recall_max_candidates=3).recall("u", "refund")
    assert captured.get("max_candidates") == 3


def test_recall_unbounded_omits_the_bound():
    # No cap -> the ABC-safe call (no max_candidates kwarg), for pre-SP-4 stores.
    store = _store()
    store.add("u", EPISODIC, "refund policy")
    captured = {}
    real = store.search

    def spy(user_id, query, **kwargs):
        captured["kwargs"] = kwargs
        return real(user_id, query, **kwargs)

    store.search = spy
    MemoryManager(store).recall("u", "refund")  # recall_max_candidates defaults None
    assert "max_candidates" not in captured["kwargs"]


# --- Query expansion: MultiQueryRetriever-style fused recall ----------------


def test_recall_without_query_expansion_model_unchanged():
    store = _store()
    store.add("alice", EPISODIC, "the refund policy is 30 days")
    calls = []
    real = store.search

    def spy(user_id, query, **kwargs):
        calls.append((user_id, query, kwargs))
        return real(user_id, query, **kwargs)

    store.search = spy
    result = MemoryManager(store).recall("alice", "refund")

    assert len(calls) == 2
    assert calls[0][1] == "refund"
    assert calls[1][1] == "refund"
    assert result.expansion_usage is None


def test_recall_expands_query_and_surfaces_lexically_disjoint_match():
    store = _store()
    store.add("alice", SEMANTIC, "the company offers a money back guarantee")
    # No shared significant term with "refund" -- plain BM25 would miss this.
    mgr = MemoryManager(
        store, query_expansion_model='fake:{"queries": ["money back guarantee"]}'
    )

    result = mgr.recall("alice", "refund")
    assert result.count == 1
    assert "money back guarantee" in result.preamble


def test_recall_expansion_calls_llm_exactly_once_per_recall(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    canned = AIMessage(content='{"queries": ["alt phrasing"]}')
    model = FakeMessagesListChatModel(responses=[canned, canned, canned])
    calls = []
    original_invoke = type(model).invoke

    def spy(self, *args, **kwargs):
        calls.append(1)
        return original_invoke(self, *args, **kwargs)

    # `FakeMessagesListChatModel` is a pydantic model -- only class-level
    # patching is allowed, not setting an instance attribute for a method.
    monkeypatch.setattr(type(model), "invoke", spy)

    store = _store()
    store.add("alice", EPISODIC, "note about refunds")
    MemoryManager(store, query_expansion_model=model).recall("alice", "refund")

    # Two scoped searches (semantic, episodic/procedural) share ONE expansion call.
    assert len(calls) == 1


def test_recall_captures_expansion_usage_on_success():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    canned = AIMessage(
        content='{"queries": ["money back guarantee"]}',
        usage_metadata={"input_tokens": 15, "output_tokens": 5, "total_tokens": 20},
    )
    store = _store()
    store.add("alice", SEMANTIC, "the refund policy is 30 days")
    mgr = MemoryManager(store, query_expansion_model=FakeMessagesListChatModel(responses=[canned]))

    result = mgr.recall("alice", "refund")
    assert result.expansion_usage == {"model": None, "input_tokens": 15, "output_tokens": 5}


def test_recall_expansion_llm_failure_degrades_to_original_query_only():
    from langchain_core.language_models.chat_models import BaseChatModel

    class _RaisingChatModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "raising-fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("model provider unavailable")

    store = _store()
    store.add("alice", EPISODIC, "the refund policy is 30 days")
    mgr = MemoryManager(store, query_expansion_model=_RaisingChatModel())

    result = mgr.recall("alice", "refund")
    assert result.ok is True
    assert result.count == 1
    assert result.expansion_usage is None


def test_recall_expansion_unparseable_response_degrades_gracefully():
    store = _store()
    store.add("alice", EPISODIC, "the refund policy is 30 days")
    mgr = MemoryManager(store, query_expansion_model="fake:sorry, not JSON")

    result = mgr.recall("alice", "refund")
    assert result.count == 1
    assert result.expansion_usage is None


def test_recall_expansion_preserves_usage_when_response_parsing_raises():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    # Simulates a provider returning structured content blocks (a list) instead
    # of a plain string: content.strip() inside _parse_extraction then raises
    # AttributeError -- a failure mode distinct from "successfully parsed but
    # not valid JSON", and one that happens AFTER usage was already captured.
    canned = AIMessage(
        content=[{"type": "text", "text": "not a string"}],
        usage_metadata={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
    )
    store = _store()
    mgr = MemoryManager(store, query_expansion_model=FakeMessagesListChatModel(responses=[canned]))

    expansions, usage = mgr._expand_query("refund")
    assert expansions == []
    assert usage == {"model": None, "input_tokens": 4, "output_tokens": 1}


def test_recall_expansion_dedupes_against_original_and_itself_and_caps_count():
    canned = (
        '{"queries": ["Refund", "refund ", "alt one", "alt one", "alt two", "alt three"]}'
    )
    store = _store()
    mgr = MemoryManager(store, query_expansion_model=f"fake:{canned}", query_expansion_count=2)

    expansions, usage = mgr._expand_query("refund")
    # "Refund"/"refund " dedupe against the original query (case/whitespace
    # insensitive); "alt one" dedupes against itself; capped at count=2 before
    # ever considering "alt three".
    assert expansions == ["alt one", "alt two"]


def test_recall_expansion_count_zero_or_negative_disables_expansion():
    store = _store()
    mgr = MemoryManager(
        store,
        query_expansion_model='fake:{"queries": ["alt"]}',
        query_expansion_count=0,
    )
    expansions, usage = mgr._expand_query("refund")
    assert expansions == []
    assert usage is None


def test_recall_top_k_applied_after_fusion_across_expanded_queries():
    store = _store()
    store.add("alice", EPISODIC, "alpha widget notes")
    store.add("alice", EPISODIC, "beta widget notes")
    store.add("alice", EPISODIC, "gamma gadget notes")
    mgr = MemoryManager(store, top_k=2)

    # Two query variants together could surface 3 distinct records (2 "widget"
    # hits + 1 "gadget" hit); the post-fusion result must still respect top_k.
    results = mgr._fused_search("alice", ["widget", "gadget"], types=[EPISODIC], top_k=2)
    assert len(results) == 2


def test_semantic_candidate_search_unaffected_by_query_expansion_model(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    canned = AIMessage(content='{"queries": ["alt phrasing"]}')
    expansion_model = FakeMessagesListChatModel(responses=[canned] * 5)
    calls = []
    # `FakeMessagesListChatModel` is a pydantic model -- only class-level
    # patching is allowed, not setting an instance attribute for a method.
    monkeypatch.setattr(type(expansion_model), "invoke", lambda self, *a, **k: calls.append(1))

    store = _store()
    store.add("alice", SEMANTIC, "likes bullet points")
    extraction_canned = '{"facts": [{"action": "add", "content": "works in finance"}], "procedural": ""}'
    mgr = MemoryManager(
        store,
        extraction_model=f"fake:{extraction_canned}",
        query_expansion_model=expansion_model,
    )
    mgr.record_run("alice", "q", "a")

    # `_extract_and_store`'s near-dup candidate search never routes through
    # query expansion -- fuzzy-expanded candidates risk false dup/update merges.
    assert calls == []


def test_recall_preserves_expansion_usage_when_store_search_fails_after_expansion():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    canned = AIMessage(
        content='{"queries": ["alt phrasing"]}',
        usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
    )

    class _FailingSearchStore(SqliteBM25Memory):
        def search(self, *args, **kwargs):
            raise RuntimeError("search backend unavailable")

    store = _FailingSearchStore(":memory:")
    mgr = MemoryManager(store, query_expansion_model=FakeMessagesListChatModel(responses=[canned]))

    result = mgr.recall("alice", "refund")
    # The expansion call already happened (and is billable) even though the
    # store search that follows it blew up -- mirrors M-04 for record_run.
    assert result.ok is False
    assert result.count == 0
    assert result.expansion_usage == {"model": None, "input_tokens": 7, "output_tokens": 2}


def test_extraction_dedups_exact_facts_against_existing():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    store.add("alice", SEMANTIC, "likes bullet points")  # already known
    canned = AIMessage(
        content=(
            '{"facts": [{"action": "add", "content": "likes bullet points"}, '
            '{"action": "add", "content": "prefers short answers"}], "procedural": ""}'
        )
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    outcome = mgr.record_run("alice", "q", "a")

    sem = sorted(r.content for r in store.all("alice") if r.type == SEMANTIC)
    assert sem == ["likes bullet points", "prefers short answers"]  # the known fact not duplicated
    assert outcome.recorded.count(SEMANTIC) == 1  # only the new fact written


def test_extraction_dedups_within_one_extraction():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    canned = AIMessage(
        content=(
            '{"facts": [{"action": "add", "content": "same fact"}, '
            '{"action": "add", "content": "same fact"}], "procedural": ""}'
        )
    )
    store = _store()
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "q", "a")
    assert len([r for r in store.all("alice") if r.type == SEMANTIC]) == 1


# --- Semantic near-duplicate / update resolution -----------------------------


def test_extraction_update_replaces_superseded_fact():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    old = store.add("alice", SEMANTIC, "prefers concise answers")
    canned = AIMessage(
        content=(
            '{"facts": [{"action": "update", "content": "prefers detailed answers", '
            f'"replaces_id": "{old.id}"}}], "procedural": ""}}'
        )
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    outcome = mgr.record_run("alice", "how verbose should answers be?", "noted")

    semantic = [r.content for r in store.all("alice") if r.type == SEMANTIC]
    assert semantic == ["prefers detailed answers"]  # old row replaced, not accumulated
    assert outcome.recorded.count(SEMANTIC) == 1


def test_extraction_update_deletes_stale_fact_even_when_replacement_already_exists():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    old = store.add("alice", SEMANTIC, "prefers concise answers")
    store.add("alice", SEMANTIC, "prefers detailed answers")  # the "replacement" already stored
    canned = AIMessage(
        content=(
            '{"facts": [{"action": "update", "content": "prefers detailed answers", '
            f'"replaces_id": "{old.id}"}}], "procedural": ""}}'
        )
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "how verbose should answers be?", "noted")

    # The already-duplicate write reports False (nothing NEW stored), but the
    # stale fact must still be superseded -- not left contradicting the
    # already-current one.
    semantic = [r.content for r in store.all("alice") if r.type == SEMANTIC]
    assert semantic == ["prefers detailed answers"]


def test_extraction_update_to_same_content_as_stale_fact_does_not_delete_only_copy():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    old = store.add("alice", SEMANTIC, "prefers concise answers")
    # Mislabeled "update" whose new content is identical to the stale record's
    # own content -- the "duplicate" IS replaces_id, so deleting it would wipe
    # out the fact's only copy.
    canned = AIMessage(
        content=(
            '{"facts": [{"action": "update", "content": "prefers concise answers", '
            f'"replaces_id": "{old.id}"}}], "procedural": ""}}'
        )
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "how verbose should answers be?", "noted")

    semantic = [r.content for r in store.all("alice") if r.type == SEMANTIC]
    assert semantic == ["prefers concise answers"]


def test_extraction_update_with_unknown_replaces_id_downgrades_to_add():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    store.add("alice", SEMANTIC, "prefers concise answers")
    canned = AIMessage(
        content='{"facts": [{"action": "update", "content": "a genuinely new fact", '
        '"replaces_id": "not-a-real-id"}], "procedural": ""}'
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "q", "a")

    semantic = sorted(r.content for r in store.all("alice") if r.type == SEMANTIC)
    # A hallucinated/unknown id must never trigger a delete -- the old fact survives.
    assert semantic == ["a genuinely new fact", "prefers concise answers"]


def test_extraction_noop_action_skips_write():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    store.add("alice", SEMANTIC, "prefers concise answers")
    # Content is deliberately NOT a duplicate of anything stored, so this only
    # passes if "noop" is actually honored -- exact-dedup can't mask a bug that
    # writes every fact regardless of action.
    canned = AIMessage(
        content='{"facts": [{"action": "noop", "content": "an entirely new distinct claim"}], "procedural": ""}'
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    outcome = mgr.record_run("alice", "q", "a")

    assert SEMANTIC not in outcome.recorded
    assert [r.content for r in store.all("alice") if r.type == SEMANTIC] == ["prefers concise answers"]


def test_extraction_missing_action_field_is_dropped_not_written():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    canned = AIMessage(content='{"facts": [{"content": "a fact with no action field"}], "procedural": ""}')
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "q", "a")

    # A malformed item (no recognized action) is dropped, not blindly written.
    assert [r for r in store.all("alice") if r.type == SEMANTIC] == []


def test_extraction_add_action_writes_new_and_dedups_exact():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    store.add("alice", SEMANTIC, "prefers concise answers")
    # One exact duplicate (must dedup) and one genuinely new fact (must be
    # written) in the same call -- distinguishes a working "add" path from one
    # that's silently broken/no-op'd (which would also leave the duplicate
    # deduped, but would also drop the new fact).
    canned = AIMessage(
        content=(
            '{"facts": ['
            '{"action": "add", "content": "prefers concise answers"}, '
            '{"action": "add", "content": "works in finance"}'
            '], "procedural": ""}'
        )
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    outcome = mgr.record_run("alice", "q", "a")

    assert outcome.recorded.count(SEMANTIC) == 1  # only the new fact written
    semantic = sorted(r.content for r in store.all("alice") if r.type == SEMANTIC)
    assert semantic == ["prefers concise answers", "works in finance"]


def test_extraction_candidates_scoped_to_user_and_semantic_type():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    alice_fact = store.add("alice", SEMANTIC, "alice likes concise answers")
    store.add("bob", SEMANTIC, "bob likes concise answers")  # different user, must not be offered
    store.add("alice", EPISODIC, "alice asked about refunds")  # different type, must not be offered

    captured = {}
    real_search = store.search

    def spy(user_id, query, **kwargs):
        captured["types"] = kwargs.get("types")
        candidates = real_search(user_id, query, **kwargs)
        captured["ids"] = {c.id for c in candidates}
        return candidates

    store.search = spy
    canned = AIMessage(content='{"facts": [], "procedural": ""}')
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "q about concise answers", "a")

    assert captured["types"] == [SEMANTIC]
    assert captured["ids"] == {alice_fact.id}


def test_extraction_candidate_fetch_failure_degrades_to_plain_add():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class _FailingSearchStore(SqliteBM25Memory):
        def search(self, *args, **kwargs):
            raise RuntimeError("search backend down")

    store = _FailingSearchStore(":memory:")
    canned = AIMessage(content='{"facts": [{"action": "add", "content": "a new fact"}], "procedural": ""}')
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    outcome = mgr.record_run("alice", "q", "a")

    # Candidate lookup failing must not break extraction -- it degrades to "no candidates".
    assert SEMANTIC in outcome.recorded
    assert [r.content for r in store.all("alice") if r.type == SEMANTIC] == ["a new fact"]


def test_prune_user_type_keeps_most_recent(monkeypatch):
    import time

    store = _store()
    for i in range(4):
        store.add("alice", EPISODIC, f"note {i}")
        time.sleep(0.005)  # distinct created_at so "most recent" is unambiguous

    removed = store.prune_user_type("alice", EPISODIC, 2)
    assert removed == 2
    assert sorted(r.content for r in store.all("alice")) == ["note 2", "note 3"]


def test_prune_user_type_scopes_to_user_and_org():
    store = _store()
    for i in range(3):
        store.add("alice", EPISODIC, f"alice {i}", org_id=5)
    store.add("bob", EPISODIC, "bob", org_id=5)
    store.add("alice", EPISODIC, "alice other org", org_id=6)

    removed = store.prune_user_type("alice", EPISODIC, 1, org_id=5)
    assert removed == 2  # alice/org5: 3 -> 1
    assert len(store.all("alice", org_id=5)) == 1
    assert len(store.all("bob", org_id=5)) == 1  # untouched
    assert len(store.all("alice", org_id=6)) == 1  # other org untouched


def test_prune_user_type_zero_cap_is_noop():
    store = _store()
    store.add("alice", EPISODIC, "x")
    assert store.prune_user_type("alice", EPISODIC, 0) == 0
    assert len(store.all("alice")) == 1


def test_record_run_prunes_episodic_to_cap_and_spares_extracted():
    store = _store()
    store.add("alice", SEMANTIC, "a distilled fact")
    mgr = MemoryManager(store, max_episodic_per_user=2)

    for i in range(4):
        mgr.record_run("alice", f"q{i}", f"a{i}")

    assert len([r for r in store.all("alice") if r.type == EPISODIC]) == 2  # capped
    assert len([r for r in store.all("alice") if r.type == SEMANTIC]) == 1  # spared


def test_record_run_no_prune_when_cap_unset():
    store = _store()
    mgr = MemoryManager(store)  # max_episodic_per_user defaults None
    for i in range(5):
        mgr.record_run("alice", f"q{i}", f"a{i}")
    assert len([r for r in store.all("alice") if r.type == EPISODIC]) == 5


def test_prune_user_type_org_none_scopes_to_null_only():
    # Review r1 #1: org-less prune must touch ONLY NULL-org rows, never other orgs.
    store = _store()
    store.add("alice", EPISODIC, "org5", org_id=5)
    store.add("alice", EPISODIC, "org6", org_id=6)
    store.add("alice", EPISODIC, "legacy 1")  # NULL
    store.add("alice", EPISODIC, "legacy 2")  # NULL

    removed = store.prune_user_type("alice", EPISODIC, 1)  # org_id None -> org_id IS NULL
    assert removed == 1  # one older NULL row pruned
    assert len(store.all("alice", org_id=5)) == 1  # concrete orgs untouched
    assert len(store.all("alice", org_id=6)) == 1
    assert len([r for r in store.all("alice", org_id=None) if r.org_id is None]) == 1


def test_org_less_manager_retention_spares_other_orgs():
    # The manager path: an org-less manager's cap must not wipe org-scoped history.
    store = _store()
    store.add("alice", EPISODIC, "org5", org_id=5)
    store.add("alice", EPISODIC, "org6", org_id=6)
    MemoryManager(store, max_episodic_per_user=1).record_run("alice", "q", "a")
    assert len(store.all("alice", org_id=5)) == 1
    assert len(store.all("alice", org_id=6)) == 1


def test_add_if_absent_dedups_across_connections():
    # Review r1 #2: atomic insert-if-absent — two connections can't both insert.
    from bestteam import SqliteBM25Memory

    import tempfile, os as _os
    fd, path = tempfile.mkstemp(suffix=".db")
    _os.close(fd)
    try:
        a = SqliteBM25Memory(path)
        b = SqliteBM25Memory(path)
        r1 = a.add_if_absent("alice", SEMANTIC, "same fact")
        r2 = b.add_if_absent("alice", SEMANTIC, "same fact")  # sees a's committed row
        assert (r1 is None) != (r2 is None)  # exactly one inserted
        assert len(a.all("alice")) == 1
        a.close()
        b.close()
    finally:
        _os.unlink(path)


def test_dedup_is_per_type_not_cross_type():
    # Review r1 #4: an existing procedural must not suppress a same-text semantic.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    store.add("alice", PROCEDURAL, "handle refunds carefully")
    canned = AIMessage(
        content='{"facts": [{"action": "add", "content": "handle refunds carefully"}], '
        '"procedural": "handle refunds carefully"}'
    )
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "q", "a")
    assert len([r for r in store.all("alice") if r.type == SEMANTIC]) == 1  # written (per-type)
    assert len([r for r in store.all("alice") if r.type == PROCEDURAL]) == 1  # deduped vs existing


def test_recall_candidate_query_avoids_temp_sort():
    # Review r1 #3: the recall filter+sort is index-covered (no temp B-tree sort).
    store = _store()
    store.add("alice", EPISODIC, "x", org_id=5)
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM memories WHERE user_id = ? AND org_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        ("alice", 5, 100),
    ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan).upper()
    assert "USE TEMP B-TREE" not in detail


def test_dedup_existence_query_uses_index():
    # Review r2 #1: the add_if_absent existence check seeks via idx_memories_dedup
    # (both concrete and NULL org), not a full scan of the user's history.
    store = _store()
    for i in range(20):
        store.add("alice", SEMANTIC, f"fact {i}", org_id=5)
        store.add("alice", SEMANTIC, f"legacy fact {i}")  # NULL org
    for org_clause, params in (
        ("org_id = ?", ["alice", SEMANTIC, "fact 3", 5]),
        ("org_id IS NULL", ["alice", SEMANTIC, "legacy fact 3"]),
    ):
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM memories "
            f"WHERE user_id = ? AND type = ? AND content = ? AND {org_clause}",
            params,
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan).upper()
        assert "IDX_MEMORIES_DEDUP" in detail
        assert "SCAN MEMORIES" not in detail  # a seek, not a full-table scan


def test_extraction_respects_overridden_add_policy():
    # Review r2 #2: a subclass that overrides add() (encryption/audit/…) but hasn't
    # adopted add_if_absent must still intercept semantic/procedural writes.
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class _AuditStore(SqliteBM25Memory):
        def __init__(self, path=":memory:"):
            super().__init__(path)
            self.seen = []

        def add(self, user_id, type, content, metadata=None, *, org_id=None):
            self.seen.append(type)
            return super().add(user_id, type, content, metadata, org_id=org_id)

    store = _AuditStore()
    canned = AIMessage(content='{"facts": [{"action": "add", "content": "a fact"}], "procedural": "a note"}')
    mgr = MemoryManager(store, extraction_model=FakeMessagesListChatModel(responses=[canned]))

    mgr.record_run("alice", "q", "a")
    assert store.seen == [EPISODIC, SEMANTIC, PROCEDURAL]  # add() saw all writes


def test_oversized_config_is_clamped_no_overflow():
    # Review r2 #3: a config beyond SQLite's int range clamps instead of raising
    # OverflowError in recall/prune.
    store = _store()
    mgr = MemoryManager(store, recall_max_candidates=2**80, max_episodic_per_user=2**80)
    assert mgr.recall_max_candidates == 2**63 - 1
    assert mgr.max_episodic_per_user == 2**63 - 1

    store.add("alice", EPISODIC, "the refund policy")
    assert mgr.recall("alice", "refund").count == 1  # no OverflowError
    assert mgr.record_run("alice", "q", "a").ok is True  # prune doesn't raise


def test_add_if_absent_concurrent_threads_insert_once(tmp_path):
    # Review r2 coverage gap: a synchronized two-thread barrier — SQLite's write
    # serialization means exactly one INSERT ... WHERE NOT EXISTS wins.
    import threading

    from bestteam import SqliteBM25Memory

    path = str(tmp_path / "m.db")
    SqliteBM25Memory(path).close()  # create schema up front

    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        s = SqliteBM25Memory(path)
        barrier.wait()
        results.append(s.add_if_absent("alice", SEMANTIC, "same fact"))
        s.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len([r for r in results if r is not None]) == 1  # exactly one insert
    s = SqliteBM25Memory(path)
    assert len(s.all("alice")) == 1
    s.close()


def test_record_run_extraction_tolerates_json_with_surrounding_prose():
    store = _store()
    canned = (
        'Here you go:\n```json\n{"facts": [{"action": "add", "content": "likes graphs"}], '
        '"procedural": ""}\n```'
    )
    manager = MemoryManager(store, extraction_model=f"fake:{canned}")

    manager.record_run("alice", "q", "a")

    types = sorted(r.type for r in store.all("alice"))
    assert types == [EPISODIC, SEMANTIC]


# --- M-03: MemoryManager.close ---------------------------------------------


def test_memory_manager_close_closes_sqlite_store():
    import sqlite3

    store = _store()
    MemoryManager(store).close()

    # The connection is closed, so any further store operation raises.
    with pytest.raises(sqlite3.ProgrammingError):
        store.add("u", EPISODIC, "after close")


def test_memory_manager_close_noop_when_store_has_no_close():
    class _NoCloseStore:
        pass

    # A store without a close() must not raise (the Memory ABC has no close()).
    MemoryManager(_NoCloseStore()).close()


# --- M-11: soft type validation on add() -----------------------------------


@pytest.mark.parametrize("bad_type", [None, "", "   ", 5, ["episodic"]])
def test_add_rejects_non_string_or_empty_type(bad_type):
    from bestteam.exceptions import ConfigurationError

    store = _store()
    with pytest.raises(ConfigurationError, match="non-empty string"):
        store.add("alice", bad_type, "some content")


def test_add_accepts_known_and_custom_string_types():
    store = _store()
    # Framework enum still works, and a custom string type is still allowed
    # (the enum is deliberately open for third-party stores).
    assert store.add("alice", EPISODIC, "x").type == EPISODIC
    assert store.add("alice", "custom", "y").type == "custom"


# --- SP-2: org scoping ------------------------------------------------------


def test_add_persists_org_id():
    store = _store()
    rec = store.add("alice", EPISODIC, "content", org_id=5)
    assert rec.org_id == 5
    assert store.all("alice", org_id=5)[0].org_id == 5


def test_all_scopes_by_org():
    store = _store()
    store.add("alice", EPISODIC, "org five note", org_id=5)
    store.add("alice", EPISODIC, "org six note", org_id=6)

    assert [r.content for r in store.all("alice", org_id=5)] == ["org five note"]
    assert [r.content for r in store.all("alice", org_id=6)] == ["org six note"]
    # org_id=None (admin) sees both.
    assert len(store.all("alice", org_id=None)) == 2


def test_search_scopes_by_org():
    store = _store()
    store.add("alice", EPISODIC, "the refund policy for org five", org_id=5)
    store.add("alice", EPISODIC, "the refund policy for org six", org_id=6)

    hits5 = store.search("alice", "refund policy", org_id=5)
    assert len(hits5) == 1 and hits5[0].org_id == 5
    # Admin (org_id=None) searches across orgs.
    assert len(store.search("alice", "refund policy", org_id=None)) == 2


def test_all_legacy_scope_returns_only_null_org_rows():
    # Finding 3: a caller must be able to request ONLY legacy (pre-SP-2, org_id
    # IS NULL) rows -- distinct from org_id=None, which means "across all orgs".
    store = _store()
    store.add("alice", EPISODIC, "org five row", org_id=5)
    store.add("alice", EPISODIC, "legacy row")  # org_id NULL

    legacy = store.all("alice", org_id=LEGACY_ORG)
    assert [r.content for r in legacy] == ["legacy row"]
    assert all(r.org_id is None for r in legacy)
    # Regression: None still spans orgs; a concrete org still scopes to itself.
    assert len(store.all("alice", org_id=None)) == 2
    assert [r.content for r in store.all("alice", org_id=5)] == ["org five row"]


def test_search_legacy_scope_returns_only_null_org_rows():
    store = _store()
    store.add("alice", EPISODIC, "the refund policy in org five", org_id=5)
    store.add("alice", EPISODIC, "the refund policy from before orgs")  # NULL

    hits = store.search("alice", "refund policy", org_id=LEGACY_ORG)
    assert [r.content for r in hits] == ["the refund policy from before orgs"]


def test_user_summaries_includes_org_id():
    store = _store()
    store.add("alice", EPISODIC, "x", org_id=5)
    store.add("bob", SEMANTIC, "y", org_id=6)

    summaries = {s["user_id"]: s for s in store.user_summaries()}
    assert summaries["alice"]["org_id"] == 5
    assert summaries["bob"]["org_id"] == 6


def test_delete_org_removes_only_that_org():
    store = _store()
    store.add("alice", EPISODIC, "org five", org_id=5)
    store.add("bob", EPISODIC, "org six", org_id=6)

    removed = store.delete_org(5)
    assert removed == 1
    assert store.all("alice", org_id=None) == []
    assert len(store.all("bob", org_id=None)) == 1


def test_all_scopes_combine_org_and_principal():
    # Integration coverage for the fix/memory-legacy-scope + feat/memory-
    # principal-lifecycle merge: org's tri-state read scope (all / legacy-NULL /
    # concrete org) must compose with principal filtering (admin-unfiltered /
    # concrete account), including rows from more than one principal per org.
    store = _store()
    store.add("alice", EPISODIC, "org5 P1 row", org_id=5, principal_id="P1")
    store.add("alice", EPISODIC, "org5 P2 row", org_id=5, principal_id="P2")
    store.add("alice", EPISODIC, "org6 P3 row", org_id=6, principal_id="P3")
    store.add("alice", EPISODIC, "legacy P1 row", principal_id="P1")  # org_id NULL

    def contents(**kwargs):
        return sorted(r.content for r in store.all("alice", **kwargs))

    # org=all (None), principal=admin-unfiltered (None): every row.
    assert contents(org_id=None, principal_id=None) == [
        "legacy P1 row",
        "org5 P1 row",
        "org5 P2 row",
        "org6 P3 row",
    ]
    # org=all, principal=concrete: that principal's rows across every org scope.
    assert contents(org_id=None, principal_id="P1") == ["legacy P1 row", "org5 P1 row"]
    # org=legacy (NULL), principal=admin-unfiltered.
    assert contents(org_id=LEGACY_ORG, principal_id=None) == ["legacy P1 row"]
    # org=legacy, principal=concrete: legacy row only for the matching principal.
    assert contents(org_id=LEGACY_ORG, principal_id="P1") == ["legacy P1 row"]
    assert contents(org_id=LEGACY_ORG, principal_id="P2") == []
    # org=concrete, principal=admin-unfiltered: both principals in that org.
    assert contents(org_id=5, principal_id=None) == ["org5 P1 row", "org5 P2 row"]
    # org=concrete, principal=concrete: exactly one row.
    assert contents(org_id=5, principal_id="P1") == ["org5 P1 row"]
    assert contents(org_id=5, principal_id="P2") == ["org5 P2 row"]
    assert contents(org_id=6, principal_id="P1") == []


def test_search_scopes_combine_org_and_principal():
    store = _store()
    store.add("alice", EPISODIC, "refund policy for org5 P1", org_id=5, principal_id="P1")
    store.add("alice", EPISODIC, "refund policy for org5 P2", org_id=5, principal_id="P2")
    store.add("alice", EPISODIC, "refund policy legacy P1", principal_id="P1")  # org_id NULL

    hits = store.search("alice", "refund policy", org_id=LEGACY_ORG, principal_id="P1")
    assert [r.content for r in hits] == ["refund policy legacy P1"]

    hits = store.search("alice", "refund policy", org_id=5, principal_id="P2")
    assert [r.content for r in hits] == ["refund policy for org5 P2"]

    hits = store.search("alice", "refund policy", org_id=5, principal_id=None)
    assert sorted(r.content for r in hits) == [
        "refund policy for org5 P1",
        "refund policy for org5 P2",
    ]


# --- Deletion-lifecycle: principal-stamped memory (findings 1 & 2) ---


def test_add_persists_principal_id():
    store = _store()
    rec = store.add("alice", EPISODIC, "content", org_id=5, principal_id="P1")
    assert rec.principal_id == "P1"
    assert store.all("alice", org_id=5, principal_id="P1")[0].principal_id == "P1"


def test_all_and_search_scope_by_principal():
    store = _store()
    store.add("alice", EPISODIC, "note from principal one", org_id=5, principal_id="P1")
    store.add("alice", EPISODIC, "note from principal two", org_id=5, principal_id="P2")

    # A recreated account (new principal) recalls only its own rows (finding 1).
    assert [r.content for r in store.all("alice", org_id=5, principal_id="P2")] == [
        "note from principal two"
    ]
    hits = store.search("alice", "note principal", org_id=5, principal_id="P1")
    assert [r.content for r in hits] == ["note from principal one"]
    # principal_id=None (admin / SDK-direct) is unfiltered — sees both.
    assert len(store.all("alice", org_id=5, principal_id=None)) == 2


def test_dedup_is_per_principal():
    store = _store()
    a = store.add_if_absent("alice", SEMANTIC, "prefers dark mode", org_id=5, principal_id="P1")
    # Same text under a DIFFERENT principal is not a duplicate.
    b = store.add_if_absent("alice", SEMANTIC, "prefers dark mode", org_id=5, principal_id="P2")
    # Same (text, principal) IS a duplicate.
    c = store.add_if_absent("alice", SEMANTIC, "prefers dark mode", org_id=5, principal_id="P1")
    assert a is not None and b is not None and c is None


def test_retired_principal_write_is_fenced():
    # Finding 2: after a principal is retired (account deleted), an in-flight
    # run's late write for that principal must be dropped, not persisted.
    store = _store()
    store.retire_principal("P1")
    assert store.is_retired("P1") is True

    store.add("alice", EPISODIC, "late episodic", org_id=5, principal_id="P1")
    assert store.add_if_absent("alice", SEMANTIC, "late fact", org_id=5, principal_id="P1") is None
    assert store.all("alice", org_id=5, principal_id=None) == []

    # A different (recreated) principal is unaffected.
    store.add("alice", EPISODIC, "new life", org_id=5, principal_id="P2")
    assert [r.content for r in store.all("alice", org_id=5, principal_id="P2")] == ["new life"]


def test_retire_principal_is_idempotent():
    store = _store()
    store.retire_principal("P1")
    store.retire_principal("P1")  # no error, still retired
    assert store.is_retired("P1") is True
    assert store.is_retired("P-unknown") is False


class _RetireBeforeInsertConn:
    """Wraps a SQLite connection so that, right before an INSERT into `memories`,
    `deleter` (a second connection to the same file) retires `principal_id` and
    commits. Reproduces the TOCTOU: the retire lands between a would-be pre-check
    and the actual insert. A correct fence checks retirement atomically inside the
    insert statement and so still drops the write. (`sqlite3.Connection.execute`
    is read-only, so we wrap rather than monkeypatch it.)"""

    def __init__(self, real, deleter, principal_id):
        self._real = real
        self._deleter = deleter
        self._principal_id = principal_id

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("INSERT INTO MEMORIES"):
            self._deleter.retire_principal(self._principal_id)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _retire_before_memories_insert(writer, deleter, principal_id):
    writer._conn = _RetireBeforeInsertConn(writer._conn, deleter, principal_id)


def test_add_fence_is_atomic_with_insert(tmp_path):
    # Finding 1 (TOCTOU): a retire committed by another connection just before the
    # insert must still drop the write -- retirement and insert must be one
    # SQLite-serialized operation, not a separate pre-SELECT then insert.
    path = str(tmp_path / "m.db")
    writer, deleter = SqliteBM25Memory(path), SqliteBM25Memory(path)
    _retire_before_memories_insert(writer, deleter, "P1")

    writer.add("alice", EPISODIC, "late-after-check", org_id=5, principal_id="P1")
    assert deleter.all("alice", org_id=5, principal_id=None) == []
    writer.close()
    deleter.close()


def test_add_if_absent_fence_is_atomic_with_insert(tmp_path):
    path = str(tmp_path / "m.db")
    writer, deleter = SqliteBM25Memory(path), SqliteBM25Memory(path)
    _retire_before_memories_insert(writer, deleter, "P1")

    writer.add_if_absent("alice", SEMANTIC, "fact", org_id=5, principal_id="P1")
    assert deleter.all("alice", org_id=5, principal_id=None) == []
    writer.close()
    deleter.close()


def test_add_returns_none_when_fenced():
    # Finding 4: a fenced write returns None (not an unpersisted record), so callers
    # can tell it wasn't recorded.
    store = _store()
    store.retire_principal("P1")
    assert store.add("alice", EPISODIC, "late", org_id=5, principal_id="P1") is None


def test_record_run_does_not_report_fenced_write_as_recorded():
    # Finding 4: record_run must not report a fenced write as recorded (no false
    # memory_recorded / audit trace).
    store = _store()
    store.retire_principal("P1")
    outcome = MemoryManager(store, org_id=5, principal_id="P1").record_run("alice", "in", "out")
    assert outcome.recorded == []
    assert store.all("alice", org_id=5, principal_id=None) == []


class _RaiseOnSqlConn:
    """Wraps a connection, raising OperationalError on a statement whose SQL starts
    with `prefix` (uppercased). Used to inject a mid-transaction failure."""

    def __init__(self, real, prefix):
        self._real = real
        self._prefix = prefix.upper()

    def execute(self, sql, *args):
        import sqlite3

        if sql.lstrip().upper().startswith(self._prefix):
            raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_retire_and_delete_user_is_atomic_on_delete_failure():
    # Finding 3: retiring the principal and purging its memory must be one
    # transaction. If the delete fails, the retirement must roll back too -- else
    # the account stays active but every future memory write is silently dropped.
    import sqlite3

    store = _store()
    store.add("alice", EPISODIC, "existing secret", org_id=5, principal_id="P1")
    store._conn = _RaiseOnSqlConn(store._conn, "DELETE FROM MEMORIES")

    with pytest.raises(sqlite3.OperationalError):
        store.retire_and_delete_user("P1", "alice")

    # Rolled back: principal NOT retired, memory intact -- consistent with the SQL
    # account still existing (the caller aborts the account delete on this error).
    assert store.is_retired("P1") is False
    assert len(store.all("alice", org_id=5, principal_id=None)) == 1


def test_retire_and_delete_user_retires_and_purges_on_success():
    store = _store()
    store.add("alice", EPISODIC, "secret", org_id=5, principal_id="P1")

    removed = store.retire_and_delete_user("P1", "alice")

    assert removed == 1
    assert store.is_retired("P1") is True
    assert store.all("alice", org_id=5, principal_id=None) == []


def test_manager_stamps_writes_and_scopes_recall_by_principal():
    # The manager binds a principal at construction, stamps every write with it,
    # and scopes recall to it -- so a recreated account can't recall old rows.
    store = _store()
    MemoryManager(store, org_id=5, principal_id="P1").record_run(
        "alice", "about refunds", "our refund policy is 30 days"
    )
    rows = store.all("alice", org_id=5, principal_id="P1")
    assert rows and all(r.principal_id == "P1" for r in rows)

    assert MemoryManager(store, org_id=5, principal_id="P2").recall("alice", "refund").count == 0
    assert MemoryManager(store, org_id=5, principal_id="P1").recall("alice", "refund").count >= 1


def test_assign_null_principal_binds_only_matching_null_rows():
    store = _store()
    store.add("alice", EPISODIC, "legacy note", org_id=5)  # principal NULL
    store.add("alice", EPISODIC, "already owned", org_id=5, principal_id="P0")
    store.add("alice", EPISODIC, "other org legacy", org_id=6)  # different org

    updated = store.assign_null_principal("alice", 5, "P1")
    assert updated == 1
    assert [r.content for r in store.all("alice", org_id=5, principal_id="P1")] == ["legacy note"]
    # The already-owned row and the other-org legacy row are untouched.
    assert store.all("alice", org_id=6, principal_id="P1") == []


def test_opens_pre_principal_db_and_migrates(tmp_path):
    # A DB created before principal stamping (no principal_id column) must gain the
    # column in place, keep its rows (principal_id NULL), and work afterward.
    import sqlite3

    db_path = str(tmp_path / "pre_principal.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "type TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, org_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO memories VALUES ('id1', 'alice', 'episodic', 'legacy note', '{}', "
        "'2026-01-01T00:00:00+00:00', 5)"
    )
    conn.commit()
    conn.close()

    store = SqliteBM25Memory(db_path)
    legacy = store.all("alice", org_id=5, principal_id=None)
    assert len(legacy) == 1 and legacy[0].principal_id is None
    store.add("alice", EPISODIC, "stamped note", org_id=5, principal_id="P1")
    assert [r.content for r in store.all("alice", org_id=5, principal_id="P1")] == ["stamped note"]
    store.close()


def test_delete_org_and_legacy_scopes_correctly():
    # Deletes org-5 scoped rows + the named users' NULL-org rows, in one txn;
    # concrete rows under OTHER orgs (same username) survive.
    store = _store()
    store.add("alice", EPISODIC, "legacy")               # NULL
    store.add("alice", EPISODIC, "org five", org_id=5)   # target org
    store.add("alice", EPISODIC, "org six", org_id=6)    # other org (must survive)
    store.add("bob", EPISODIC, "bob legacy")             # NULL, not a member

    removed = store.delete_org_and_legacy(5, ["alice"])
    assert removed == 2  # org-five scoped + alice's legacy NULL
    assert {r.content for r in store.all("alice", org_id=None)} == {"org six"}
    assert len(store.all("bob", org_id=None)) == 1  # untouched


def test_delete_org_and_legacy_is_atomic_on_failure():
    # If the legacy delete fails, the scoped delete must roll back too (no
    # half-completed erasure).
    store = _store()
    store.add("alice", EPISODIC, "org five", org_id=5)
    store.add("alice", EPISODIC, "legacy")

    class _FlakyConn:
        """Delegates to the real connection but raises on the legacy DELETE."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if "org_id IS NULL" in sql:  # the second (legacy) DELETE
                raise RuntimeError("boom")
            return self._real.execute(sql, *args)

        def commit(self):
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

    real = store._conn
    store._conn = _FlakyConn(real)
    with pytest.raises(RuntimeError):
        store.delete_org_and_legacy(5, ["alice"])
    store._conn = real  # restore for the read below

    # Rolled back: both rows still present.
    assert len(store.all("alice", org_id=None)) == 2


def test_assign_legacy_to_org_stamps_only_null_rows():
    store = _store()
    store.add("alice", EPISODIC, "legacy one")            # NULL -> becomes org 5
    store.add("alice", EPISODIC, "legacy two")            # NULL -> becomes org 5
    store.add("alice", EPISODIC, "already scoped", org_id=6)  # untouched

    updated = store.assign_legacy_to_org("alice", 5)
    assert updated == 2
    assert {r.content for r in store.all("alice", org_id=5)} == {"legacy one", "legacy two"}
    assert [r.content for r in store.all("alice", org_id=6)] == ["already scoped"]


def test_org_less_manager_works_with_pre_sp2_store():
    # A store implementing only the original ABC (no org_id) must keep working
    # when the manager has no concrete org: MemoryManager passes org_id only when
    # concrete, so an org-less caller invokes the original contract. (Finding 3)
    store = _LegacyStore()
    manager = MemoryManager(store, org_id=None)

    manager.record_run("alice", "how do refunds work?", "30 days")  # add() sans org_id
    assert manager.recall_preamble("alice", "refunds")  # search() sans org_id


def test_user_summaries_splits_legacy_and_scoped_same_username():
    # A username with both a legacy NULL-org row and an org-scoped row yields two
    # entries, each with its own total -- not one merged/misattributed row. (Finding 4)
    store = _store()
    store.add("alice", EPISODIC, "legacy note")  # org_id None
    store.add("alice", EPISODIC, "scoped note", org_id=5)

    alice = [s for s in store.user_summaries() if s["user_id"] == "alice"]
    assert len(alice) == 2
    assert {s["org_id"]: s["total"] for s in alice} == {None: 1, 5: 1}


def test_concurrent_open_of_legacy_db_is_race_safe(tmp_path):
    # Many threads opening the same pre-org DB at once must all migrate cleanly;
    # the loser of the ALTER race sees "duplicate column" and continues. (Finding 5)
    import sqlite3
    import threading

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "type TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    errors: list[Exception] = []

    def open_store():
        try:
            SqliteBM25Memory(db_path).close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_opens_pre_org_db_and_migrates(tmp_path):
    # A DB created before org scoping (no org_id column) must gain the column
    # in place, keep its existing rows (org_id NULL), and work afterward.
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "type TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO memories VALUES ('id1', 'alice', 'episodic', 'legacy note', '{}', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = SqliteBM25Memory(db_path)
    # Legacy row survives with org_id NULL and is visible to the admin (org_id=None).
    legacy = store.all("alice", org_id=None)
    assert len(legacy) == 1
    assert legacy[0].org_id is None
    # New org-scoped writes work and are isolated from the legacy NULL row.
    store.add("alice", EPISODIC, "new org note", org_id=5)
    assert [r.content for r in store.all("alice", org_id=5)] == ["new org note"]
    store.close()


# --- workflow scoping (episodic/procedural isolation, cross-workflow project) ---


def test_add_persists_workflow_id():
    store = _store()
    rec = store.add("alice", EPISODIC, "content", workflow_id=1)
    assert rec.workflow_id == 1
    assert store.all("alice", workflow_id=1)[0].workflow_id == 1


def test_all_scopes_by_workflow():
    store = _store()
    store.add("alice", EPISODIC, "workflow one note", workflow_id=1)
    store.add("alice", EPISODIC, "workflow two note", workflow_id=2)

    assert [r.content for r in store.all("alice", workflow_id=1)] == ["workflow one note"]
    assert [r.content for r in store.all("alice", workflow_id=2)] == ["workflow two note"]
    # workflow_id=None (admin / unfiltered) sees both.
    assert len(store.all("alice", workflow_id=None)) == 2


def test_search_scopes_by_workflow():
    store = _store()
    store.add("alice", EPISODIC, "the refund policy for workflow one", workflow_id=1)
    store.add("alice", EPISODIC, "the refund policy for workflow two", workflow_id=2)

    hits1 = store.search("alice", "refund policy", workflow_id=1)
    assert len(hits1) == 1 and hits1[0].workflow_id == 1
    # Unfiltered search still spans every workflow.
    assert len(store.search("alice", "refund policy", workflow_id=None)) == 2


def test_dedup_is_per_workflow():
    store = _store()
    a = store.add_if_absent("alice", PROCEDURAL, "check order number first", workflow_id=1)
    # Same text under a DIFFERENT workflow is not a duplicate.
    b = store.add_if_absent("alice", PROCEDURAL, "check order number first", workflow_id=2)
    # Same (text, workflow) IS a duplicate.
    c = store.add_if_absent("alice", PROCEDURAL, "check order number first", workflow_id=1)
    assert a is not None and b is not None and c is None


def test_opens_pre_workflow_db_and_migrates(tmp_path):
    # A DB created before workflow scoping (no workflow_id column) must gain the
    # column in place, keep its existing rows (workflow_id NULL), and work after.
    import sqlite3

    db_path = str(tmp_path / "pre_workflow.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "type TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, org_id INTEGER, principal_id TEXT)"
    )
    conn.execute(
        "INSERT INTO memories VALUES ('id1', 'alice', 'procedural', 'legacy note', '{}', "
        "'2026-01-01T00:00:00+00:00', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    store = SqliteBM25Memory(db_path)
    legacy = store.all("alice", workflow_id=None)
    assert len(legacy) == 1 and legacy[0].workflow_id is None
    store.add("alice", PROCEDURAL, "new workflow note", workflow_id=1)
    assert [r.content for r in store.all("alice", workflow_id=1)] == ["new workflow note"]
    store.close()


def test_recall_semantic_shared_across_workflows():
    # Personal preferences (semantic) apply no matter which workflow is running.
    store = _store()
    store.add("alice", SEMANTIC, "prefers concise answers", org_id=5)
    mgr_a = MemoryManager(store, org_id=5, workflow_id=1)
    mgr_b = MemoryManager(store, org_id=5, workflow_id=2)

    assert mgr_a.recall("alice", "concise answers").count == 1
    assert mgr_b.recall("alice", "concise answers").count == 1


def test_recall_procedural_isolated_per_workflow():
    store = _store()
    store.add("alice", PROCEDURAL, "check the order number first", org_id=5, workflow_id=1)
    mgr_same = MemoryManager(store, org_id=5, workflow_id=1)
    mgr_other = MemoryManager(store, org_id=5, workflow_id=2)

    assert mgr_same.recall("alice", "order number").count == 1
    assert mgr_other.recall("alice", "order number").count == 0


def test_recall_episodic_isolated_per_workflow():
    store = _store()
    store.add("alice", EPISODIC, "user asked about the refund policy", org_id=5, workflow_id=1)
    mgr_other = MemoryManager(store, org_id=5, workflow_id=2)

    assert mgr_other.recall("alice", "refund policy").count == 0


def test_recall_workflow_id_none_reproduces_prior_behavior():
    # Back-compat: no workflow bound (SDK-direct, or a YAML-only demo workflow
    # with no WorkflowRecord) recalls episodic/procedural across ALL workflows,
    # exactly like before this scoping dimension existed.
    store = _store()
    store.add("alice", PROCEDURAL, "check the order number first", workflow_id=1)
    store.add("alice", PROCEDURAL, "escalate angry customers", workflow_id=2)

    mgr = MemoryManager(store)  # workflow_id defaults None
    assert mgr.recall("alice", "order number").count == 1
    assert mgr.recall("alice", "escalate angry customers").count == 1


def test_recall_combines_semantic_and_workflow_scoped_hits():
    store = _store()
    store.add("alice", SEMANTIC, "prefers concise refund answers", org_id=5)
    store.add("alice", PROCEDURAL, "refund requests: check order number first", org_id=5, workflow_id=1)
    mgr = MemoryManager(store, org_id=5, workflow_id=1)

    result = mgr.recall("alice", "refund")
    assert result.count == 2
    assert "prefers concise refund answers" in result.preamble
    assert "check order number first" in result.preamble


# --- workflow scoping on writes: episodic/procedural only, never semantic ---


def test_record_run_stamps_workflow_id_on_episodic():
    store = _store()
    MemoryManager(store, workflow_id=1).record_run("alice", "q", "a")

    (record,) = store.all("alice", workflow_id=1)
    assert record.type == EPISODIC


def test_record_run_stamps_workflow_id_on_extracted_procedural_not_semantic():
    canned = (
        '{"facts": [{"action": "add", "content": "prefers bullet points"}], '
        '"procedural": "check order number first"}'
    )
    store = _store()
    MemoryManager(store, workflow_id=1, extraction_model=f"fake:{canned}").record_run(
        "alice", "how do refunds work?", "30-day money back"
    )

    semantic = [r for r in store.all("alice") if r.type == SEMANTIC][0]
    procedural = [r for r in store.all("alice") if r.type == PROCEDURAL][0]
    assert semantic.workflow_id is None  # org-wide, not tied to any one workflow
    assert procedural.workflow_id == 1


def test_extraction_dedups_procedural_per_workflow_but_semantic_org_wide():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    store = _store()
    store.add("alice", SEMANTIC, "likes bullet points")  # no workflow_id -> org-wide
    store.add("alice", PROCEDURAL, "check order number first", workflow_id=2)  # a DIFFERENT workflow
    canned = AIMessage(
        content='{"facts": [{"action": "add", "content": "likes bullet points"}], '
        '"procedural": "check order number first"}'
    )
    mgr = MemoryManager(
        store, workflow_id=1, extraction_model=FakeMessagesListChatModel(responses=[canned])
    )

    outcome = mgr.record_run("alice", "q", "a")

    # The semantic fact already exists org-wide -> deduped, not rewritten.
    assert len([r for r in store.all("alice") if r.type == SEMANTIC]) == 1
    assert outcome.recorded.count(SEMANTIC) == 0
    # The procedural note is new under workflow 1 (workflow 2's note doesn't dedup it).
    assert len([r for r in store.all("alice") if r.type == PROCEDURAL]) == 2
    assert outcome.recorded.count(PROCEDURAL) == 1


# --- Hybrid (BM25 + vector) recall: RRF fusion + type-aware recency decay ---


class _TopicEmbedding(Embeddings):
    """Maps text to a small topic vector via keyword buckets, deliberately
    independent of BM25 token overlap -- lets tests prove the vector leg
    finds a match that shares zero significant terms with the query.
    `DeterministicFakeEmbedding`/`fake:` alone is hash-based and can't be
    used to assert *which* content should match a given query.
    """

    _BIKE_WORDS = ("bike", "bicycle", "cycling", "pedal")
    _FINANCE_WORDS = ("refund", "invoice", "payment", "money")

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text):
        lowered = text.lower()
        bike = 1.0 if any(w in lowered for w in self._BIKE_WORDS) else 0.0
        finance = 1.0 if any(w in lowered for w in self._FINANCE_WORDS) else 0.0
        return [bike, finance]


class _BrokenEmbedding(Embeddings):
    """Raises on every call, for testing embedding-failure degradation."""

    def embed_documents(self, texts):
        raise RuntimeError("embedding provider unavailable")

    def embed_query(self, text):
        raise RuntimeError("embedding provider unavailable")


def _hybrid_store(**kwargs):
    return SqliteBM25Memory(":memory:", embedding_model=_TopicEmbedding(), **kwargs)


def test_add_computes_and_persists_embedding_when_configured():
    store = _hybrid_store()
    store.add("alice", EPISODIC, "User rides their bicycle every weekend")

    row = store._conn.execute("SELECT embedding_json, embedding_model FROM memories").fetchone()
    assert json.loads(row["embedding_json"]) == [1.0, 0.0]
    # A live (non-string) Embeddings instance has no spec string of its own,
    # so it's tagged with a fresh per-instance identity instead of a bare
    # None (a None==None match would let a DIFFERENT, incompatible live
    # instance's vectors get reused -- see test_vector_rank_rejects_vectors_
    # from_a_different_anonymous_embedding_instance below).
    assert row["embedding_model"] == store._embedding_model_spec
    assert row["embedding_model"].startswith("anon:")


def test_vector_rank_rejects_vectors_from_a_different_anonymous_embedding_instance(tmp_path):
    path = str(tmp_path / "m.db")
    store1 = SqliteBM25Memory(path, embedding_model=_TopicEmbedding())
    store1.add("alice", EPISODIC, "User rides their bicycle every weekend")
    store1.close()

    # A second store instance -- even backed by an equivalent live embedding
    # model -- gets its OWN anonymous identity; it must not silently reuse a
    # different instance's persisted vector.
    store2 = SqliteBM25Memory(path, embedding_model=_TopicEmbedding())
    records, embeddings, specs = store2._candidates("alice")
    ranked = store2._vector_rank("cycling", records, embeddings, specs)

    assert ranked == []


def test_add_omits_embedding_when_unconfigured():
    store = _store()
    store.add("alice", EPISODIC, "hi")

    row = store._conn.execute("SELECT embedding_json, embedding_model FROM memories").fetchone()
    assert row["embedding_json"] is None
    assert row["embedding_model"] is None


def test_add_if_absent_persists_embedding_on_new_record():
    store = _hybrid_store()
    store.add_if_absent("alice", SEMANTIC, "loves cycling")

    row = store._conn.execute("SELECT embedding_json FROM memories").fetchone()
    assert row["embedding_json"] is not None


def test_add_if_absent_skips_embedding_on_duplicate(monkeypatch):
    store = _hybrid_store()
    store.add_if_absent("alice", SEMANTIC, "loves cycling")

    embed_calls = []
    original = store._embeddings.embed_documents

    def spy(texts):
        embed_calls.append(texts)
        return original(texts)

    monkeypatch.setattr(store._embeddings, "embed_documents", spy)
    result = store.add_if_absent("alice", SEMANTIC, "loves cycling")

    assert result is None  # duplicate, not written
    assert embed_calls == []  # pre-check skipped the embedding call entirely


def test_embedding_write_failure_degrades_to_null_vector_not_a_broken_write():
    store = SqliteBM25Memory(":memory:", embedding_model=_BrokenEmbedding())

    record = store.add("alice", EPISODIC, "hi")

    assert record is not None
    row = store._conn.execute("SELECT embedding_json FROM memories").fetchone()
    assert row["embedding_json"] is None


def test_search_hybrid_surfaces_zero_keyword_overlap_match():
    store = _hybrid_store()
    store.add("alice", EPISODIC, "User rides their bicycle every weekend")

    hits = store.search("alice", "cycling hobby")

    assert [h.content for h in hits] == ["User rides their bicycle every weekend"]


def test_search_without_embedding_model_ignores_semantic_matches():
    store = _store()
    store.add("alice", EPISODIC, "User rides their bicycle every weekend")

    assert store.search("alice", "cycling hobby") == []


def test_search_hybrid_still_finds_pure_keyword_matches():
    store = _hybrid_store()
    store.add("alice", EPISODIC, "asked about a refund for order 42")

    hits = store.search("alice", "refund")

    assert [h.content for h in hits] == ["asked about a refund for order 42"]


def test_search_vector_leg_skips_records_without_embeddings():
    store = _hybrid_store()
    store.add("alice", EPISODIC, "User rides their bicycle every weekend")
    # Simulate a pre-hybrid-adoption row: no stored vector.
    store._conn.execute("UPDATE memories SET embedding_json = NULL, embedding_model = NULL")
    store._conn.commit()

    assert store.search("alice", "cycling hobby") == []  # no BM25 overlap either
    assert store.search("alice", "bicycle") != []  # still found via BM25


def test_search_vector_leg_skips_dimension_mismatched_embeddings():
    store = _hybrid_store()
    store.add("alice", EPISODIC, "User rides their bicycle every weekend")
    # Simulate a changed embedding model: same (None) spec, different dimension.
    store._conn.execute("UPDATE memories SET embedding_json = ?", (json.dumps([1.0, 0.0, 0.0]),))
    store._conn.commit()

    # No crash; the mismatched vector is excluded from the vector leg, so a
    # zero-keyword-overlap query no longer finds it.
    assert store.search("alice", "cycling hobby") == []


def test_reciprocal_rank_fusion_combines_two_lists():
    scores = _reciprocal_rank_fusion(["a", "b", "c"], ["c", "a"])

    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)


def test_reciprocal_rank_fusion_respects_custom_k():
    assert _reciprocal_rank_fusion(["a"], k=1)["a"] == pytest.approx(1 / 2)


def test_reciprocal_rank_fusion_empty_lists():
    assert _reciprocal_rank_fusion([], []) == {}


def test_recency_weight_semantic_never_decays():
    now = datetime(2026, 1, 30, tzinfo=timezone.utc)
    old = "2020-01-01T00:00:00+00:00"
    assert _recency_weight(SEMANTIC, old, now=now, half_life_days=14) == 1.0


def test_recency_weight_halves_at_half_life():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()

    weight = _recency_weight(EPISODIC, created_at, now=now, half_life_days=14)

    assert weight == pytest.approx(0.5)


def test_recency_weight_disabled_when_half_life_none_or_nonpositive():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    created_at = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()

    assert _recency_weight(EPISODIC, created_at, now=now, half_life_days=None) == 1.0
    assert _recency_weight(EPISODIC, created_at, now=now, half_life_days=0) == 1.0


def test_recency_weight_tolerates_unparseable_or_future_timestamp():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)

    assert _recency_weight(EPISODIC, "not-a-date", now=now, half_life_days=14) == 1.0
    future = datetime(2026, 2, 1, tzinfo=timezone.utc).isoformat()
    assert _recency_weight(EPISODIC, future, now=now, half_life_days=14) == 1.0


def test_search_hybrid_applies_recency_decay_to_ranking():
    store = _hybrid_store(recency_half_life_days=1)
    older = store.add("alice", EPISODIC, "asked about a refund")
    newer = store.add("alice", EPISODIC, "asked about a refund")
    old_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store._conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (old_time, older.id))
    store._conn.commit()

    hits = store.search("alice", "refund", top_k=2)

    assert hits[0].id == newer.id


def test_search_hybrid_semantic_beats_stale_episodic_at_equal_fusion_score():
    store = _hybrid_store(recency_half_life_days=1)
    same_content = "asked about a refund"
    semantic = store.add("alice", SEMANTIC, same_content)
    store.add("alice", EPISODIC, same_content)
    old_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store._conn.execute("UPDATE memories SET created_at = ?", (old_time,))
    store._conn.commit()

    hits = store.search("alice", "refund", types=[SEMANTIC, EPISODIC], top_k=2)

    assert hits[0].id == semantic.id


def test_opens_pre_embedding_db_and_migrates(tmp_path):
    # A DB created before hybrid recall (no embedding_json/embedding_model
    # columns) must gain them in place, keep its existing rows (both NULL),
    # and work after.
    import sqlite3

    db_path = str(tmp_path / "pre_embedding.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "type TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, org_id INTEGER, principal_id TEXT, workflow_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO memories VALUES ('id1', 'alice', 'semantic', 'legacy fact', '{}', "
        "'2026-01-01T00:00:00+00:00', NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    store = SqliteBM25Memory(db_path, embedding_model=_TopicEmbedding())
    legacy = store.all("alice")
    assert len(legacy) == 1
    row = store._conn.execute("SELECT embedding_json FROM memories WHERE id = 'id1'").fetchone()
    assert row["embedding_json"] is None  # not backfilled
    store.add("alice", SEMANTIC, "new fact about cycling")
    assert len(store.all("alice")) == 2
    store.close()


def test_sqlite_bm25_requires_numpy_when_embedding_model_set(monkeypatch):
    import builtins

    from bestteam.exceptions import ConfigurationError

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("no numpy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ConfigurationError, match="numpy"):
        SqliteBM25Memory(":memory:", embedding_model="fake:8")


def test_sqlite_bm25_no_numpy_required_without_embedding_model(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("no numpy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    store = SqliteBM25Memory(":memory:")  # must not raise
    assert store.add("alice", EPISODIC, "hi") is not None


# --- Codex review fixes: vector-leg relevance floor, dedup-precheck race,
# --- query-embedding reuse, embedding-parse skip ---


def test_search_hybrid_does_not_let_unrelated_vector_candidate_outrank_decayed_exact_match():
    store = _hybrid_store(recency_half_life_days=1)
    old_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    refund = store.add("alice", EPISODIC, "asked about a refund")
    store._conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (old_time, refund.id))
    store._conn.commit()
    store.add("alice", EPISODIC, "left a note about their bike")  # fresh, unrelated topic

    hits = store.search("alice", "refund", top_k=1)

    assert hits[0].id == refund.id


def test_vector_rank_returns_nothing_for_zero_query_vector():
    store = _hybrid_store()
    store.add("alice", EPISODIC, "User rides their bicycle every weekend")
    records, embeddings, specs = store._candidates("alice")

    ranked = store._vector_rank("no relevant topic words here", records, embeddings, specs)

    assert ranked == []


def test_add_skips_embedding_for_a_retired_principal(monkeypatch):
    store = _hybrid_store()
    store.retire_principal("p1")
    embed_calls = []
    monkeypatch.setattr(store._embeddings, "embed_documents", lambda texts: embed_calls.append(texts) or [])

    result = store.add("alice", EPISODIC, "bicycle notes", principal_id="p1")

    assert result is None  # dropped by the deletion-lifecycle fence
    assert embed_calls == []  # never paid an external embedding call for it


def test_add_if_absent_skips_embedding_for_a_retired_principal(monkeypatch):
    store = _hybrid_store()
    store.retire_principal("p1")
    embed_calls = []
    monkeypatch.setattr(store._embeddings, "embed_documents", lambda texts: embed_calls.append(texts) or [])

    result = store.add_if_absent("alice", SEMANTIC, "bicycle notes", principal_id="p1")

    assert result is None
    assert embed_calls == []


def test_add_if_absent_self_heals_embedding_when_precheck_races_a_deletion(monkeypatch):
    store = _hybrid_store()
    dup = store.add("alice", SEMANTIC, "loves cycling")

    embed_calls = []
    original_embed_documents = store._embeddings.embed_documents

    def spy(texts):
        embed_calls.append(texts)
        return original_embed_documents(texts)

    monkeypatch.setattr(store._embeddings, "embed_documents", spy)

    class _RacyConn:
        """Delegates to the real connection, but simulates another connection
        deleting the "duplicate" row right after our existence precheck runs
        (and before our own INSERT), racing the cost-optimization precheck."""

        def __init__(self, conn, dup_id):
            self._conn = conn
            self._dup_id = dup_id
            self.exists_calls = 0

        def execute(self, sql, params=()):
            if sql.strip().startswith("SELECT EXISTS"):
                self.exists_calls += 1
                result = self._conn.execute(sql, params)
                self._conn.execute("DELETE FROM memories WHERE id = ?", (self._dup_id,))
                self._conn.commit()
                return result
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    proxy = _RacyConn(store._conn, dup.id)
    monkeypatch.setattr(store, "_conn", proxy)

    record = store.add_if_absent("alice", SEMANTIC, "loves cycling")

    assert record is not None  # the "duplicate" was actually gone by insert time
    assert proxy.exists_calls == 1
    row = proxy._conn.execute(
        "SELECT embedding_json FROM memories WHERE id = ?", (record.id,)
    ).fetchone()
    assert row["embedding_json"] is not None  # self-healed, not left permanently NULL
    assert len(embed_calls) == 1  # precheck skipped the first call; only the self-heal ran


def test_search_reuses_query_embedding_across_repeated_calls(monkeypatch):
    store = _hybrid_store()
    store.add("alice", SEMANTIC, "loves cycling")
    store.add("alice", EPISODIC, "asked about their bicycle")

    embed_calls = []
    original = store._embeddings.embed_query

    def spy(text):
        embed_calls.append(text)
        return original(text)

    monkeypatch.setattr(store._embeddings, "embed_query", spy)

    store.search("alice", "cycling hobby", types=[SEMANTIC])
    store.search("alice", "cycling hobby", types=[EPISODIC, PROCEDURAL])

    assert embed_calls == ["cycling hobby"]  # second call reused the cached vector


def test_candidates_skips_embedding_parsing_when_not_requested():
    store = _hybrid_store()
    store.add("alice", EPISODIC, "User rides their bicycle every weekend")

    _records, embeddings, specs = store._candidates("alice", include_embeddings=False)
    assert embeddings == [None]
    assert specs == [None]

    _records2, embeddings2, _specs2 = store._candidates("alice", include_embeddings=True)
    assert embeddings2 == [[1.0, 0.0]]


def test_all_does_not_request_embeddings(monkeypatch):
    store = _hybrid_store()
    store.add("alice", EPISODIC, "hi")

    calls = []
    real = store._candidates

    def spy(*args, **kwargs):
        calls.append(kwargs.get("include_embeddings"))
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "_candidates", spy)
    store.all("alice")

    assert calls == [False]


def test_search_without_hybrid_configured_does_not_request_embeddings(monkeypatch):
    store = _store()
    store.add("alice", EPISODIC, "refund note")

    calls = []
    real = store._candidates

    def spy(*args, **kwargs):
        calls.append(kwargs.get("include_embeddings"))
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "_candidates", spy)
    store.search("alice", "refund")

    assert calls == [False]


def test_search_hybrid_requests_embeddings(monkeypatch):
    store = _hybrid_store()
    store.add("alice", EPISODIC, "refund note")

    calls = []
    real = store._candidates

    def spy(*args, **kwargs):
        calls.append(kwargs.get("include_embeddings"))
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "_candidates", spy)
    store.search("alice", "refund")

    assert calls == [True]
