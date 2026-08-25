"""The single-instance startup lock (`ui/backend/process_lock.py`).

The backend is single-process by design (in-memory RunRegistry, per-org
dispatch locks, one serial mailbox poller, a startup sweep that releases every
outstanding claim). A second process against the same database -- `uvicorn
--workers N`, or a second replica -- would create duplicate drafts and release
claims the first process is still working. The lock turns that misconfiguration
into a refusal at startup instead of silent corruption.
"""

from __future__ import annotations

import multiprocessing
import sys

import pytest

from ui.backend.process_lock import SingleInstanceError, acquire_single_instance_lock

pytestmark = pytest.mark.unit


def test_memory_path_needs_no_lock(tmp_path):
    assert acquire_single_instance_lock(":memory:") is None


def test_acquire_creates_lock_file_and_releases(tmp_path):
    db_path = tmp_path / "bestteam.db"
    lock = acquire_single_instance_lock(db_path)
    assert lock is not None
    assert (tmp_path / "bestteam.db.lock").exists()
    lock.release()
    # Releasing twice must be harmless (lifespan finally-blocks can re-run).
    lock.release()


def test_second_acquire_refuses_while_held(tmp_path):
    db_path = tmp_path / "bestteam.db"
    lock = acquire_single_instance_lock(db_path)
    try:
        with pytest.raises(SingleInstanceError) as excinfo:
            acquire_single_instance_lock(db_path)
        # The message must tell the operator what is wrong and what not to do.
        assert "another backend process" in str(excinfo.value)
        assert "--workers" in str(excinfo.value)
    finally:
        lock.release()


def test_reacquire_after_release_succeeds(tmp_path):
    db_path = tmp_path / "bestteam.db"
    lock = acquire_single_instance_lock(db_path)
    lock.release()
    lock2 = acquire_single_instance_lock(db_path)
    assert lock2 is not None
    lock2.release()


def _hold_lock_in_child(db_path: str, held: "multiprocessing.Event", done: "multiprocessing.Event") -> None:
    from ui.backend.process_lock import acquire_single_instance_lock

    lock = acquire_single_instance_lock(db_path)
    held.set()
    done.wait(timeout=30)
    lock.release()


def test_lock_is_cross_process(tmp_path):
    """The lock must hold across OS processes, not just within one -- that is
    the whole point (uvicorn workers are separate processes)."""
    db_path = tmp_path / "bestteam.db"
    ctx = multiprocessing.get_context("spawn")
    held = ctx.Event()
    done = ctx.Event()
    child = ctx.Process(target=_hold_lock_in_child, args=(str(db_path), held, done))
    child.start()
    try:
        assert held.wait(timeout=30), "child never acquired the lock"
        with pytest.raises(SingleInstanceError):
            acquire_single_instance_lock(db_path)
    finally:
        done.set()
        child.join(timeout=30)
    # After the child released and exited, this process can acquire.
    lock = acquire_single_instance_lock(db_path)
    lock.release()
