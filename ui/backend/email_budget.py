"""Pure per-org budget arithmetic (email automation Phase 4a).

The existing `BESTTEAM_TRIGGER_DAILY_CAP` counts *runs*, and a run processes up
to `batch_size()` messages at an unknown price -- so it is a platform safety
rail, not a customer budget. These two caps are the customer's: how many
messages a day, and how much money a month.

No database and no clock: the caller supplies both the usage and the moment,
which is what makes a month boundary a one-line test.

See docs/superpowers/specs/2026-08-17-email-phase-4a-filtering-budgets-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class BudgetCaps:
    """`None` means no cap. Both default to `None` so an upgrade never starts
    refusing to process mail because of a limit the customer never set."""

    daily_message_cap: Optional[int] = None
    monthly_cost_cap: Optional[float] = None


def remaining_messages(caps: BudgetCaps, used_today: int) -> Optional[int]:
    """How many more messages may be handed to a model today; `None` =
    unlimited.

    Clamped at zero: an operator lowering the cap below what an org has already
    used today must produce "no more", not a negative limit that a downstream
    `LIMIT` clause would interpret by accident.
    """
    if caps.daily_message_cap is None:
        return None
    return max(int(caps.daily_message_cap) - int(used_today), 0)


def cost_exceeded(caps: BudgetCaps, spent_this_month: Optional[float]) -> bool:
    """Whether this org's monthly spend cap is reached.

    `spent_this_month` is `None` when the org has no priced usage at all --
    `SUM()` over no rows is NULL, which means "nothing spent", not "over
    budget". `>=` rather than `>`: a cap of 50 that still permits a run at
    exactly 50 spent is not a cap a customer would recognise.
    """
    if caps.monthly_cost_cap is None:
        return False
    return float(spent_this_month or 0.0) >= float(caps.monthly_cost_cap)


def _as_utc(moment: datetime) -> datetime:
    """SQLite round-trips datetimes tzinfo-naive. A key that silently shifted
    would alert twice at a month boundary, or not at all."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def day_key(now: datetime) -> str:
    return _as_utc(now).strftime("%Y-%m-%d")


def month_key(now: datetime) -> str:
    return _as_utc(now).strftime("%Y-%m")
