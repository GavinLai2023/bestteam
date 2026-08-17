"""Phase 3b: retention settings, the purge engine, and export."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.orgs import create_org
from ui.backend.db.retention import (
    get_retention_settings,
    orgs_with_retention,
    record_sweep,
    set_retention_days,
)


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_retention_is_unset_until_configured(db):
    org = create_org(db, "acme")
    assert get_retention_settings(db, org.id) is None
    assert orgs_with_retention(db) == []


def test_set_and_clear_retention_days(db):
    org = create_org(db, "acme")

    row = set_retention_days(db, org.id, 30)
    db.commit()
    assert row.run_retention_days == 30
    assert orgs_with_retention(db) == [(org.id, 30)]

    set_retention_days(db, org.id, None)
    db.commit()
    # The row survives (it carries sweep history); the policy is off.
    assert get_retention_settings(db, org.id).run_retention_days is None
    assert orgs_with_retention(db) == []


def test_record_sweep_stamps_history(db):
    from datetime import datetime, timezone

    org = create_org(db, "acme")
    set_retention_days(db, org.id, 7)
    at = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

    record_sweep(db, org.id, purged=4, at=at)
    db.commit()

    row = get_retention_settings(db, org.id)
    assert row.last_purged_count == 4
    assert row.last_swept_at.replace(tzinfo=timezone.utc) == at
