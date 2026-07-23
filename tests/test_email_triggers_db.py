"""CRUD tests for the `email_triggers` table (autonomous email trigger state)."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_triggers import (
    get_email_trigger,
    list_enabled_triggers,
    upsert_email_trigger,
)
from ui.backend.db.orgs import get_or_create_org


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    yield session
    session.close()


def test_get_returns_none_when_absent(db):
    org = get_or_create_org(db, "acme")
    assert get_email_trigger(db, org.id) is None


def test_upsert_creates_then_updates_in_place(db):
    org = get_or_create_org(db, "acme")
    t = upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=41, uidvalidity=7)
    assert (t.workflow_name, t.enabled, t.last_uid, t.uidvalidity) == ("triage", True, 41, 7)
    assert t.runs_today == 0 and t.runs_date is None and t.last_run_id is None
    t2 = upsert_email_trigger(db, org.id, workflow_name="other", enabled=False,
                              last_uid=99, uidvalidity=8)
    assert t2.id == t.id  # one row per org, updated in place
    assert (t2.workflow_name, t2.enabled, t2.last_uid) == ("other", False, 99)


def test_list_enabled_returns_only_enabled(db):
    a = get_or_create_org(db, "a")
    b = get_or_create_org(db, "b")
    upsert_email_trigger(db, a.id, workflow_name="wa", enabled=True, last_uid=0, uidvalidity=None)
    upsert_email_trigger(db, b.id, workflow_name="wb", enabled=False, last_uid=0, uidvalidity=None)
    enabled = list_enabled_triggers(db)
    assert [t.org_id for t in enabled] == [a.id]
