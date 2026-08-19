"""Row CRUD for the Phase 4a settings tables, plus the monthly-spend query."""

from datetime import datetime, timedelta, timezone

import pytest

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_budget_settings import (
    get_budget_caps,
    set_budget_caps,
    spent_this_month,
    unpriced_run_count,
)
from ui.backend.db.email_filter_settings import get_filter_settings, set_filter_settings
from ui.backend.db.models import Organization, OrgEmailFilterSetting, Run, UsageRecord

pytestmark = pytest.mark.unit

ORG = 1
OTHER = 2


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    TestSession = session_factory(engine)
    session = TestSession()
    session.add(Organization(id=ORG, name="acme"))
    session.add(Organization(id=OTHER, name="other"))
    session.commit()
    yield session
    session.close()


def test_an_org_with_no_row_gets_the_defaults(db):
    settings = get_filter_settings(db, ORG)
    assert settings.skip_bulk is True
    assert settings.sender_blocklist == ()
    assert settings.sender_allowlist == ()
    assert settings.subject_blocklist == ()


def test_settings_round_trip(db):
    set_filter_settings(
        db, ORG,
        skip_bulk=False,
        sender_blocklist=["a@x.test"],
        sender_allowlist=[],
        subject_blocklist=["out of office"],
    )
    db.commit()
    settings = get_filter_settings(db, ORG)
    assert settings.skip_bulk is False
    assert settings.sender_blocklist == ("a@x.test",)
    assert settings.subject_blocklist == ("out of office",)


def test_saving_twice_updates_the_same_row(db):
    set_filter_settings(db, ORG, skip_bulk=True, sender_blocklist=["a@x.test"],
                        sender_allowlist=[], subject_blocklist=[])
    set_filter_settings(db, ORG, skip_bulk=True, sender_blocklist=["b@x.test"],
                        sender_allowlist=[], subject_blocklist=[])
    db.commit()
    assert get_filter_settings(db, ORG).sender_blocklist == ("b@x.test",)
    assert db.query(OrgEmailFilterSetting).count() == 1


def test_blank_and_duplicate_patterns_are_dropped(db):
    # An admin reads back the list they meant, not the one they typed twice.
    set_filter_settings(db, ORG, skip_bulk=True,
                        sender_blocklist=[" a@x.test ", "A@X.test", "", "  "],
                        sender_allowlist=[], subject_blocklist=[])
    db.commit()
    assert get_filter_settings(db, ORG).sender_blocklist == ("a@x.test",)


def test_an_org_with_no_budget_row_has_no_caps(db):
    caps = get_budget_caps(db, ORG)
    assert caps.daily_message_cap is None
    assert caps.monthly_cost_cap is None


def test_budget_caps_round_trip_and_clear(db):
    set_budget_caps(db, ORG, daily_message_cap=30, monthly_cost_cap=12.5)
    db.commit()
    caps = get_budget_caps(db, ORG)
    assert caps.daily_message_cap == 30
    assert caps.monthly_cost_cap == 12.5

    set_budget_caps(db, ORG, daily_message_cap=None, monthly_cost_cap=None)
    db.commit()
    caps = get_budget_caps(db, ORG)
    assert caps.daily_message_cap is None
    assert caps.monthly_cost_cap is None


def _usage(db, org_id, *, cost, created_at, run_id):
    db.add(Run(id=run_id, pipeline="w", input="", status="completed", org_id=org_id))
    db.add(
        UsageRecord(
            run_id=run_id, org_id=org_id, model="openai:gpt-4o-mini",
            input_tokens=10, output_tokens=10, cost_estimate=cost,
            created_at=created_at,
        )
    )


def test_spend_sums_only_this_month_and_only_this_org(db):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    _usage(db, ORG, cost=1.5, created_at=now - timedelta(days=1), run_id="r1")
    _usage(db, ORG, cost=2.0, created_at=now - timedelta(days=40), run_id="r2")
    _usage(db, OTHER, cost=99.0, created_at=now, run_id="r3")
    db.commit()
    assert spent_this_month(db, ORG, now) == pytest.approx(1.5)


def test_spend_is_none_when_nothing_is_priced(db):
    # NULL is "nothing priced", which is not the same as "nothing spent" and
    # very much not "over budget" -- the caller has to be able to tell.
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    _usage(db, ORG, cost=None, created_at=now, run_id="r1")
    db.commit()
    assert spent_this_month(db, ORG, now) is None


def test_unpriced_runs_are_counted_so_the_blind_spot_is_visible(db):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    _usage(db, ORG, cost=None, created_at=now, run_id="r1")
    _usage(db, ORG, cost=1.0, created_at=now, run_id="r2")
    db.commit()
    assert unpriced_run_count(db, ORG, now) == 1


def test_the_message_counter_column_starts_at_zero(db):
    # The migration's server_default must agree with the ORM default, or an
    # upgraded row reads NULL and every comparison against it is false.
    from ui.backend.db.email_triggers import upsert_email_trigger

    trigger = upsert_email_trigger(
        db, ORG, pipeline_name="triage", enabled=False, last_uid=0, uidvalidity=None,
    )
    db.commit()
    assert trigger.messages_today == 0
