"""Engine/session setup for the per-deployment SQLite database (Phase 1).

Usage::

    engine = make_engine("data/bestteam.db")
    init_db(engine)
    Session = session_factory(engine)

    with Session() as db:
        ...
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Mapping, Optional, Union
from urllib.request import pathname2url

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from .models import Base

# ui/backend/data -- the volume that holds the SQLite file, uploads and the
# per-user memory store. `db_session.py` and `env_check.py` take it from here.
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_DB_PATH = DATA_DIR / "bestteam.db"
MEMORY_URL = "sqlite:///:memory:"


def sqlite_url_for(path: Union[str, Path]) -> str:
    """The SQLAlchemy URL for a SQLite file path (or the `:memory:` literal)."""
    if str(path) == ":memory:":
        return MEMORY_URL
    return f"sqlite:///{Path(path)}"


def resolve_database_url(env: Optional[Mapping[str, str]] = None) -> str:
    """Which database this process uses, as a SQLAlchemy URL.

    `BESTTEAM_DATABASE_URL` wins when set. Otherwise the legacy
    `BESTTEAM_DB_PATH` (a SQLite file path, or `:memory:`) becomes a
    `sqlite:///` URL, defaulting to `ui/backend/data/bestteam.db`. This is the
    one place either variable is read: `db_session.py`, `alembic/env.py`,
    `env_check.py` and `admin.py` all call it.
    """
    env = os.environ if env is None else env
    url = (env.get("BESTTEAM_DATABASE_URL") or "").strip()
    if url:
        return url
    path = (env.get("BESTTEAM_DB_PATH") or "").strip() or str(DEFAULT_DB_PATH)
    return sqlite_url_for(path)


def sqlite_path_of(url: str) -> Optional[Path]:
    """The file behind a SQLite file URL; None for `:memory:` or another engine."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return None
    return Path(parsed.database)


def describe_database_url(url: str) -> str:
    """One line naming the database for humans. Never includes a password."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        path = sqlite_path_of(url)
        return f"sqlite file {path}" if path is not None else "sqlite in-memory"
    return f"{parsed.get_backend_name()} host {parsed.host or '?'} database {parsed.database or '?'}"


def lock_anchor_for(url: str, data_dir: Union[str, Path] = DATA_DIR) -> Union[str, Path]:
    """What `process_lock.acquire_single_instance_lock` is keyed on.

    A SQLite file keeps its lock beside the file (`<file>.lock`, unchanged
    across upgrades); `:memory:` returns the literal, which the lock skips;
    a server database locks `<data_dir>/bestteam.lock` -- the lock guards
    this host's single process, so it lives on this host's data volume.
    """
    path = sqlite_path_of(url)
    if path is not None:
        return path
    if url == MEMORY_URL:
        return ":memory:"
    return Path(data_dir) / "bestteam"


def readonly_engine(url: str) -> Engine:
    """An engine that can only read.

    A SQLite file is opened through a `file:...?mode=ro` URI, so a checklist
    or a copy can neither create the file nor write to one that exists (the
    trick `env_check` used with stdlib sqlite3; read-only still reads a live
    WAL database). A server database is opened normally; callers only SELECT.
    """
    path = sqlite_path_of(url)
    if path is not None:
        uri = "file:" + pathname2url(str(path)) + "?mode=ro"
        return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True), poolclass=NullPool)
    return create_engine(url, poolclass=NullPool)


def make_engine(db_path: Union[str, Path] = "bestteam.db", *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine for a SQLite database.

    `db_path` is `:memory:` for an ephemeral database (tests, dry runs) or a
    file path for a persistent per-deployment database. In-memory databases
    use a `StaticPool` so every connection shares the same database -- the
    default pooling behavior would otherwise hand out a fresh, empty
    in-memory database per connection.
    """
    if str(db_path) == ":memory:":
        return create_engine(
            "sqlite:///:memory:",
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    engine = create_engine(f"sqlite:///{Path(db_path)}", echo=echo)

    @event.listens_for(engine, "connect")
    def _use_wal(dbapi_connection, _record):
        # One file is shared by the run workers, the ingestion executor, the
        # email poller and every request. In the default rollback-journal
        # mode a write transaction blocks every reader until it commits; WAL
        # lets readers proceed beside one writer. The mode is persisted in
        # the file, so repeating it per connection is cheap and makes a fresh
        # file correct from its very first connection. (Busy waiting needs no
        # setting: pysqlite's default timeout is already 5 s.)
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables defined in `models.py` that don't already exist."""
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
