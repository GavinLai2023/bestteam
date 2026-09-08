"""The schema compiles to the intended DDL on both supported dialects (spec §4)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from ui.backend.db.models import Organization, PipelineRecord, User

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _index(table, name):
    return next(index for index in table.indexes if index.name == name)


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()], ids=["sqlite", "postgresql"])
def test_the_one_member_per_org_index_is_partial_on_both_dialects(dialect):
    ddl = str(CreateIndex(_index(User.__table__, "uq_users_org_id_not_null")).compile(dialect=dialect))
    assert "WHERE org_id IS NOT NULL" in ddl


@pytest.mark.parametrize("model", [Organization, PipelineRecord])
def test_active_defaults_to_a_boolean_on_postgres_and_1_on_sqlite(model):
    on_postgres = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
    on_sqlite = str(CreateTable(model.__table__).compile(dialect=sqlite.dialect()))
    assert "DEFAULT true" in on_postgres
    assert "DEFAULT 1" not in on_postgres
    assert "DEFAULT 1" in on_sqlite


def test_no_migration_uses_an_integer_literal_as_a_boolean_default():
    # Postgres rejects `DEFAULT 1` on a boolean column, so a fresh Postgres
    # schema could never be built from the chain (Ruling 6 depends on it).
    pattern = re.compile(r'sa\.Boolean\(\)[^\n]*server_default=sa\.text\("[01]"\)')
    offenders = sorted(p.name for p in _VERSIONS.glob("*.py") if pattern.search(p.read_text(encoding="utf-8")))
    assert offenders == []


def test_the_two_run_pointers_are_not_foreign_keys():
    # The email trigger commits a claim (`inbox_events.run_id`) and the
    # dispatch CAS (`email_triggers.last_run_id`) before the `runs` row
    # exists, by design (a crash mid-build must leave the claim for the
    # startup sweep). A key that the design violates by construction would
    # refuse every autonomous run on Postgres, so these two are loose pointers.
    from ui.backend.db.models import EmailTrigger, InboxEvent

    assert not EmailTrigger.__table__.c.last_run_id.foreign_keys
    assert not InboxEvent.__table__.c.run_id.foreign_keys
