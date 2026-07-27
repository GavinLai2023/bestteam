"""Shared per-user-memory lifecycle helpers (used by the CLI and the admin API).

See docs/superpowers/specs/2026-07-27-admin-org-user-management-design.md.
"""

from __future__ import annotations

from ui.backend.account_memory import purge_user_memory, reconcile_legacy_org


def test_purge_returns_none_when_no_store(monkeypatch):
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)
    assert purge_user_memory("alice") is None


def test_reconcile_returns_none_when_no_store(monkeypatch):
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)
    assert reconcile_legacy_org("alice", 1) is None


def test_purge_removes_records_and_returns_count(monkeypatch, tmp_path):
    from bestteam import SqliteBM25Memory

    mem_path = tmp_path / "mem.db"
    store = SqliteBM25Memory(str(mem_path))
    store.add("alice", "semantic", "likes tea")
    store.close()

    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(mem_path))
    assert purge_user_memory("alice") == 1

    store = SqliteBM25Memory(str(mem_path))
    assert store.all("alice") == []
    store.close()
