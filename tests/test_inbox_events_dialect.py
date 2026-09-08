"""`record_events` picks the ON CONFLICT insert for the session's dialect (spec §4)."""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql, sqlite

from ui.backend.db.inbox_events import _insert_for


def _session_on(dialect_name):
    return SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name=dialect_name)))


def test_the_insert_construct_follows_the_session_dialect():
    assert _insert_for(_session_on("sqlite")) is sqlite.insert
    assert _insert_for(_session_on("postgresql")) is postgresql.insert


def test_other_dialects_are_refused_by_name():
    with pytest.raises(NotImplementedError, match="mysql"):
        _insert_for(_session_on("mysql"))
