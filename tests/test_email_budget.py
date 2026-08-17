"""Pure budget arithmetic (email automation Phase 4a).

Split out from the poller for the same reason trigger_health.py was: the
interesting cases (no cap, a NULL cost estimate, a month boundary) are
miserable to reach through a mailbox and trivial to reach here.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ui.backend.email_budget import (
    BudgetCaps,
    cost_exceeded,
    day_key,
    month_key,
    remaining_messages,
)

pytestmark = pytest.mark.unit


def test_no_message_cap_means_unlimited():
    assert remaining_messages(BudgetCaps(), used_today=999) is None


def test_a_message_cap_returns_what_is_left():
    assert remaining_messages(BudgetCaps(daily_message_cap=20), used_today=8) == 12


def test_a_reached_message_cap_leaves_zero():
    assert remaining_messages(BudgetCaps(daily_message_cap=20), used_today=20) == 0


def test_an_overshot_message_cap_never_goes_negative():
    # An operator lowering the cap mid-day must not produce a negative limit,
    # which claim_events would treat as "claim nothing" only by luck.
    assert remaining_messages(BudgetCaps(daily_message_cap=5), used_today=9) == 0


def test_no_cost_cap_is_never_exceeded():
    assert cost_exceeded(BudgetCaps(), spent_this_month=10_000.0) is False


def test_spending_under_the_cost_cap_is_allowed():
    assert cost_exceeded(BudgetCaps(monthly_cost_cap=50.0), spent_this_month=49.99) is False


def test_reaching_the_cost_cap_exactly_stops_dispatch():
    # ">= " not ">": a cap of 50 that permits a run at exactly 50 spent is not
    # a cap the customer would recognise.
    assert cost_exceeded(BudgetCaps(monthly_cost_cap=50.0), spent_this_month=50.0) is True


def test_no_spend_yet_is_treated_as_zero():
    # SUM() over no rows is NULL, which is not the same thing as "over budget".
    assert cost_exceeded(BudgetCaps(monthly_cost_cap=50.0), spent_this_month=None) is False


def test_period_keys_are_utc():
    moment = datetime(2026, 8, 17, 23, 30, tzinfo=timezone.utc)
    assert day_key(moment) == "2026-08-17"
    assert month_key(moment) == "2026-08"


def test_a_naive_datetime_is_read_as_utc():
    # SQLite hands datetimes back tzinfo-naive; a key that silently shifted
    # would alert twice at a month boundary or not at all.
    moment = datetime(2026, 12, 31, 22, 0)
    assert month_key(moment) == "2026-12"
    assert day_key(moment) == "2026-12-31"


def test_a_non_utc_moment_is_converted_before_the_key_is_taken():
    # 01:00 on 1 September in UTC+8 is 17:00 on 31 August in UTC. The keys are
    # notification fingerprints, so landing in the wrong month would either
    # alert twice or stay silent for one.
    moment = datetime(2026, 9, 1, 1, 0, tzinfo=timezone(timedelta(hours=8)))
    assert month_key(moment) == "2026-08"
    assert day_key(moment) == "2026-08-31"
