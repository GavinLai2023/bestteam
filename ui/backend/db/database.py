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

from sqlalchemy import Engine, create_engine
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
    return create_engine(f"sqlite:///{Path(db_path)}", echo=echo)


def init_db(engine: Engine) -> None:
    """Create all tables defined in `models.py` that don't already exist."""
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
