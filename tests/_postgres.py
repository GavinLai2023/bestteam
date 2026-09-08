"""Postgres support for the test suite (spec §8).

Off unless `BESTTEAM_TEST_DATABASE_URL` is set to a *maintenance* URL -- a
database the test user may connect to while creating and dropping others,
e.g. `postgresql+psycopg://postgres:postgres@localhost:5432/postgres`. With
it set, `tests/helpers.py::make_test_engine` hands every caller a fresh
database cloned from a per-session template, and `tests/conftest.py` drops
each test's databases when the test ends (a clone is several megabytes; a
session would otherwise leave more than a thousand behind).

Not a test module (no `test_` prefix), like `tests/e2e/_guard.py`.
"""

from __future__ import annotations

import os
import uuid
from typing import List, Optional, Tuple

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

MAINTENANCE_URL = os.environ.get("BESTTEAM_TEST_DATABASE_URL", "").strip()

_template: Optional[str] = None
_template_error: Optional[BaseException] = None
# (engine, database name) created since the last drop_created(); conftest
# drains it after every test.
_created: List[Tuple[Engine, str]] = []


def enabled() -> bool:
    return bool(MAINTENANCE_URL)


def url_for(database: str) -> URL:
    return make_url(MAINTENANCE_URL).set(database=database)


def _admin() -> Engine:
    # CREATE/DROP DATABASE cannot run inside a transaction: autocommit, and no
    # pool so nothing lingers connected to a database about to be dropped.
    return create_engine(MAINTENANCE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _create(name: str, template: Optional[str] = None) -> None:
    admin = _admin()
    try:
        with admin.connect() as conn:
            suffix = f' TEMPLATE "{template}"' if template else ""
            conn.execute(text(f'CREATE DATABASE "{name}"{suffix}'))
    finally:
        admin.dispose()


def _drop(name: str) -> None:
    admin = _admin()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def ensure_template() -> str:
    """The session's template database: the current `create_all` schema, no rows."""
    global _template, _template_error
    if _template_error is not None:
        # The schema did not build once; every later test fails fast on the
        # same error instead of creating and dropping a database each.
        raise _template_error
    if _template is None:
        name = f"bestteam_tmpl_{uuid.uuid4().hex[:8]}"
        _create(name)
        engine = create_engine(url_for(name))
        try:
            from ui.backend.db import init_db

            init_db(engine)
        except Exception as exc:
            # A schema that does not build on Postgres must not leak one
            # template per test: drop it and remember why.
            engine.dispose()
            _drop(name)
            _template_error = exc
            raise
        finally:
            # CREATE DATABASE ... TEMPLATE refuses while anyone is connected.
            engine.dispose()
        _template = name
    return _template


def clone_engine() -> Engine:
    """A fresh database with the schema and no rows, dropped after the test."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    _create(name, template=ensure_template())
    # Through the app's own factory, not create_engine: the lane is here to
    # exercise what a deployment runs, including its connection settings (the
    # session timezone above all -- see `make_engine`).
    from ui.backend.db import make_engine

    engine = make_engine(url_for(name).render_as_string(hide_password=False))
    _created.append((engine, name))
    return engine


def empty_database_url() -> URL:
    """A brand-new database with NO schema (migration replay), dropped after the test."""
    name = f"e_{uuid.uuid4().hex[:12]}"
    _create(name)
    _created.append((create_engine(url_for(name), poolclass=NullPool), name))
    return url_for(name)


def drop_created() -> None:
    while _created:
        engine, name = _created.pop()
        engine.dispose()
        _drop(name)


def drop_template() -> None:
    global _template
    if _template is not None:
        _drop(_template)
        _template = None
