"""`make_engine` connection settings for the per-deployment SQLite file (beta B8).

One file is shared by four run workers, the ingestion executor, the email
poller and every API request. In SQLite's default rollback-journal mode a
write transaction blocks every reader for its duration; WAL lets readers
proceed alongside one writer. (A busy timeout needs no work: pysqlite's
default `timeout=5.0` already turns a write collision into a short wait.)
"""

import pytest


pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy import text

from ui.backend.db import make_engine


def _pragma(engine, name):
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_file_engine_uses_wal(tmp_path):
    engine = make_engine(tmp_path / "bestteam.db")

    assert _pragma(engine, "journal_mode") == "wal"


def test_memory_engine_is_unchanged():
    engine = make_engine(":memory:")

    # An in-memory database has no WAL; it must keep reporting its own mode
    # rather than erroring on a pragma that doesn't apply.
    assert _pragma(engine, "journal_mode") == "memory"
