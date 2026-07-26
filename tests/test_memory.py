"""Tests for the per-user memory store and manager (`core/memory.py`)."""

import pytest

from bestteam import Memory, MemoryManager, MemoryRecord, SqliteBM25Memory
from bestteam.core.memory import EPISODIC, PROCEDURAL, SEMANTIC


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
    real_all = store.all

    def spy_all(user_id, types=None, limit=None, *, org_id=None):
        captured["limit"] = limit
        return real_all(user_id, types, limit, org_id=org_id)

    monkeypatch.setattr(store, "all", spy_all)
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
