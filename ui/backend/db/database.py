"""Engine/session setup for the per-deployment SQLite database (Phase 1).

Usage::

    engine = make_engine("data/bestteam.db")
    init_db(engine)
    Session = session_factory(engine)

    with Session() as db:
        ...
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


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
