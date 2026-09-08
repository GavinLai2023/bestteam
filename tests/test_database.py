"""`make_engine` connection settings for the per-deployment SQLite file (beta B8).

One file is shared by four run workers, the ingestion executor, the email
poller and every API request. In SQLite's default rollback-journal mode a
write transaction blocks every reader for its duration; WAL lets readers
proceed alongside one writer. (A busy timeout needs no work: pysqlite's
default `timeout=5.0` already turns a write collision into a short wait.)

Postgres has one such setting too: the session timezone, which decides what a
naive `timestamp` column actually stores.
"""

from datetime import datetime, timezone

import pytest


pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy import text

from helpers import make_test_engine
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Organization


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


def test_an_aware_utc_write_reads_back_as_the_same_utc_wall_clock():
    # Every timestamp column is naive and holds UTC: `models._utcnow` writes an
    # aware UTC value and `models.iso_utc` stamps the marker back on read.
    # SQLite drops the offset, so the wall clock survives. Postgres converts an
    # aware value into the SESSION's zone before storing it in a `timestamp
    # without time zone` -- on a server in, say, Australia/Brisbane every
    # stored time landed ten hours out until `make_engine` pinned the session
    # to UTC. This must hold on both engines.
    engine = make_test_engine()
    init_db(engine)
    written = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    try:
        with session_factory(engine)() as db:
            db.add(Organization(name="tz-probe", created_at=written))
            db.commit()
        with session_factory(engine)() as db:
            stored = db.query(Organization.created_at).filter_by(name="tz-probe").scalar()
    finally:
        engine.dispose()

    assert stored.tzinfo is None
    assert stored.replace(tzinfo=timezone.utc) == written
