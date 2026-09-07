"""URL resolution and engine construction for the two supported engines
(spec 2026-09-07-database-engine-portability, section 3)."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy.exc import OperationalError

from ui.backend.db.database import (
    DEFAULT_DB_PATH,
    describe_database_url,
    lock_anchor_for,
    readonly_engine,
    resolve_database_url,
    sqlite_path_of,
    sqlite_url_for,
)


def test_url_wins_over_path():
    env = {"BESTTEAM_DATABASE_URL": "postgresql+psycopg://u:p@db/x", "BESTTEAM_DB_PATH": "/tmp/a.db"}
    assert resolve_database_url(env) == "postgresql+psycopg://u:p@db/x"


def test_path_becomes_a_sqlite_url_and_the_default_is_the_data_dir():
    assert resolve_database_url({"BESTTEAM_DB_PATH": "/tmp/a.db"}) == f"sqlite:///{Path('/tmp/a.db')}"
    assert resolve_database_url({"BESTTEAM_DB_PATH": ":memory:"}) == "sqlite:///:memory:"
    assert resolve_database_url({}) == f"sqlite:///{DEFAULT_DB_PATH}"


def test_a_blank_url_falls_back_to_the_path():
    env = {"BESTTEAM_DATABASE_URL": "   ", "BESTTEAM_DB_PATH": ":memory:"}
    assert resolve_database_url(env) == "sqlite:///:memory:"


def test_sqlite_path_of():
    assert sqlite_path_of(sqlite_url_for(Path("/tmp/a.db"))) == Path("/tmp/a.db")
    assert sqlite_path_of("sqlite:///:memory:") is None
    assert sqlite_path_of("postgresql+psycopg://u:p@db/x") is None


def test_describe_never_shows_the_password():
    line = describe_database_url("postgresql+psycopg://alice:s3cret@db.example:5432/bestteam")
    assert "s3cret" not in line
    assert line == "postgresql host db.example database bestteam"
    assert describe_database_url("sqlite:///:memory:") == "sqlite in-memory"
    assert describe_database_url(sqlite_url_for("/tmp/a.db")).startswith("sqlite file ")


def test_lock_anchor_follows_the_engine():
    assert lock_anchor_for(sqlite_url_for("/tmp/a.db")) == Path("/tmp/a.db")
    assert lock_anchor_for("sqlite:///:memory:") == ":memory:"
    assert lock_anchor_for("postgresql+psycopg://u:p@db/x", data_dir="/data") == Path("/data") / "bestteam"


def test_readonly_engine_does_not_create_a_missing_sqlite_file(tmp_path):
    missing = tmp_path / "absent.db"
    engine = readonly_engine(sqlite_url_for(missing))
    with pytest.raises(OperationalError):
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    assert not missing.exists()


from sqlalchemy.pool import StaticPool

from ui.backend.db.database import make_engine


def test_make_engine_memory_url_uses_the_static_pool():
    assert isinstance(make_engine("sqlite:///:memory:").pool, StaticPool)
    assert isinstance(make_engine(":memory:").pool, StaticPool)


def test_make_engine_path_and_sqlite_url_are_the_same_engine(tmp_path):
    by_path = make_engine(tmp_path / "a.db")
    by_url = make_engine(sqlite_url_for(tmp_path / "a.db"))
    try:
        assert by_path.url == by_url.url
        with by_url.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
    finally:
        by_path.dispose()
        by_url.dispose()


def test_make_engine_names_the_variable_on_a_bad_url():
    with pytest.raises(ValueError, match="BESTTEAM_DATABASE_URL"):
        make_engine("://")
    with pytest.raises(RuntimeError, match="BESTTEAM_DATABASE_URL"):
        make_engine("nosuchdb://user:pw@host/db")
