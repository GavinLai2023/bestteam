# Email automation Phase 4a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop paying model rates for bulk mail, and give an org a daily
message cap and a monthly spend cap that actually stop dispatch.

**Architecture:** Two pure evaluator modules (`email_filter.py`,
`email_budget.py`) hold all the policy logic and know nothing about mailboxes,
databases or clocks — the shape `trigger_health.py` already established. The
poller calls them at two existing seams: filtering slots between
`check_mailbox` and `record_events` (changing an `inbox_events` row's *status*,
never whether the row exists), and the budget check slots beside the existing
`_at_daily_cap` re-check inside `_dispatch_lock`.

**Tech Stack:** Python 3.11 / SQLAlchemy 2.0 ORM / Alembic / FastAPI / pytest;
React 18 + TypeScript + Vite + Vitest + Testing Library on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-17-email-phase-4a-filtering-budgets-design.md`

## Global Constraints

- **Run everything through the project venv:** `.\.venv\Scripts\python.exe -m pytest`
  on Windows. Frontend commands run from `ui/frontend`.
- **Every new test file needs a `pytestmark`** — one of `unit`, `integration`,
  `e2e`, `optional` (optionally also `slow`). `tests/test_marker_completeness.py`
  fails the whole suite if a collected test carries none of them.
  All new backend test files in this plan use
  `pytestmark = pytest.mark.unit`.
- **Code comments in English**, even though this plan's discussion may be in
  Chinese. **British spelling** in prose and user-visible copy (*organisation*,
  *behaviour*, *recognise*).
- **`BODY.PEEK` is mandatory** in every IMAP fetch. The draft-only toolkit
  deliberately never marks mail seen; nothing added here may become the thing
  that does.
- **No `send` verb, no SMTP.** Out of scope entirely — do not add one.
- **`db/` helpers never commit.** Callers own the transaction boundary. This is
  load-bearing for Phase 1's durability guarantee.
- **Filtering must never delete or skip recording a row.** Every detected UID
  gets an `inbox_events` row in the same commit that advances
  `EmailTrigger.last_uid`.
- **Fail open.** A message whose headers cannot be fetched is recorded
  `pending`, never `filtered`.
- **No regular expressions** in customer-supplied filter patterns. Exactly two
  forms: a full address and `*@domain`.
- **Defaults must not change behaviour for an existing org on upgrade**, except
  the one deliberate exception the spec names: `skip_bulk` defaults to `True`.
  Both budget caps default to `NULL` (no cap).
- **Do not touch** `claim_events`' claim SQL, `mark_dispatched`'s attempt
  accounting, `release_events`, `resolve_retry_events`, or the retry path.

## File Structure

**New backend files**

| File | Responsibility |
|---|---|
| `ui/backend/email_filter.py` | Pure header-rule evaluator. No DB, no IMAP, no clock. |
| `ui/backend/email_budget.py` | Pure remaining-budget arithmetic. No DB, no clock. |
| `ui/backend/db/email_filter_settings.py` | Row CRUD for `org_email_filter_settings`. |
| `ui/backend/db/email_budget_settings.py` | Row CRUD for `org_email_budget_settings` + the monthly-spend query. |
| `alembic/versions/l9m0n1o2p3q4_add_email_filter_and_budgets.py` | The one migration. |

**New frontend files**

| File | Responsibility |
|---|---|
| `ui/frontend/src/components/EmailFilterSettings.tsx` | The checkbox and three pattern lists. |
| `ui/frontend/src/components/EmailBudgetSettings.tsx` | Two caps, usage against them, unpriced-model warning. |

**Modified backend files**

| File | Change |
|---|---|
| `ui/backend/db/models.py` | `OrgEmailFilterSetting`, `OrgEmailBudgetSetting`, `EmailTrigger.messages_today`. |
| `ui/backend/db/inbox_events.py` | `EVENT_FILTERED`; `record_events` takes per-id decisions; `release_filtered_event`; `list_filtered_events`. |
| `src/bestteam/tools/email_client.py` | `summaries_for` fetches the four bulk headers. |
| `ui/backend/email_trigger.py` | Call the filter in `_detect`; enforce both budgets; raise budget alerts. |
| `ui/backend/org_settings.py` | Four routes: `GET/PUT /email-filter`, `GET/PUT /email-budget`. |
| `ui/backend/email_trigger_api.py` | `GET /email-trigger/filtered`, `POST /email-trigger/filtered/{id}/release`. |

**Modified frontend files**

| File | Change |
|---|---|
| `ui/frontend/src/lib/api.ts` | Six new client methods. |
| `ui/frontend/src/lib/types.ts` | Response types. |
| `ui/frontend/src/components/EmailTriggerActivity.tsx` | A Filtered section with Release. |

**Docs updated at the end:** `ui/backend/CLAUDE.md`, `ui/frontend/CLAUDE.md`,
`docs/STATUS.md`, `CLAUDE.md`.

---

## Task 1: The pure filter evaluator

**Files:**
- Create: `ui/backend/email_filter.py`
- Test: `tests/test_email_filter.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FilterSettings` dataclass: `skip_bulk: bool = True`,
    `sender_blocklist: tuple[str, ...] = ()`,
    `sender_allowlist: tuple[str, ...] = ()`,
    `subject_blocklist: tuple[str, ...] = ()`
  - `evaluate(headers: Mapping[str, str], settings: FilterSettings) -> Optional[str]`
  - `describe(decision: str) -> str` — decision string to a customer sentence.
  - Constant `BULK_HEADERS: tuple[str, ...] = ("auto-submitted", "precedence", "list-id", "list-unsubscribe")`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_filter.py`:

```python
"""The pure pre-LLM filter evaluator (email automation Phase 4a).

No mailbox, no database, no clock -- the same shape as test_trigger_health.py,
so every rule and every ordering decision is pinned exhaustively and cheaply.
"""

import pytest

from ui.backend.email_filter import FilterSettings, describe, evaluate

pytestmark = pytest.mark.unit


def _headers(**kwargs) -> dict:
    base = {"from": "Alice <alice@example.com>", "subject": "Quote request"}
    base.update(kwargs)
    return base


def test_an_ordinary_message_is_processed():
    assert evaluate(_headers(), FilterSettings()) is None


def test_default_settings_filter_bulk_mail():
    # skip_bulk defaults on: the phase exists because customers are billed for
    # bulk mail, and a safety feature nobody switches on protects nobody.
    assert evaluate(_headers(**{"list-id": "<news.example.com>"}), FilterSettings()) == "bulk:list-id"


@pytest.mark.parametrize(
    "header,value",
    [
        ("auto-submitted", "auto-generated"),
        ("precedence", "bulk"),
        ("list-id", "<news.example.com>"),
        ("list-unsubscribe", "<https://example.com/u>"),
    ],
)
def test_each_bulk_header_is_recognised(header, value):
    assert evaluate(_headers(**{header: value}), FilterSettings()) == f"bulk:{header}"


def test_auto_submitted_none_is_not_bulk():
    # RFC 3834: "no" is spelled `none`, and every ordinary message that sets
    # the header at all sets it to that. Treating it as bulk would filter
    # normal mail.
    assert evaluate(_headers(**{"auto-submitted": "none"}), FilterSettings()) is None


def test_bulk_is_not_filtered_when_the_admin_switches_it_off():
    settings = FilterSettings(skip_bulk=False)
    assert evaluate(_headers(**{"precedence": "bulk"}), settings) is None


def test_a_blocked_sender_is_filtered():
    settings = FilterSettings(sender_blocklist=("alice@example.com",))
    assert evaluate(_headers(), settings) == "blocked_sender:alice@example.com"


def test_a_domain_wildcard_blocks_the_whole_domain():
    settings = FilterSettings(sender_blocklist=("*@example.com",))
    assert evaluate(_headers(), settings) == "blocked_sender:*@example.com"


def test_pattern_matching_ignores_case_on_both_sides():
    settings = FilterSettings(sender_blocklist=("*@EXAMPLE.com",))
    assert evaluate(_headers(**{"from": "ALICE@Example.COM"}), settings) is not None


def test_matching_uses_the_address_not_the_display_name():
    # The display name is attacker-chosen free text. Matching on it would let a
    # sender slip a blocklist, or forge past an allowlist, by typing anything.
    settings = FilterSettings(sender_blocklist=("*@spam.test",))
    headers = _headers(**{"from": '"noreply@spam.test" <real@example.com>'})
    assert evaluate(headers, settings) is None


def test_an_allowlist_filters_everyone_not_on_it():
    settings = FilterSettings(sender_allowlist=("*@client.test",))
    assert evaluate(_headers(), settings) == "not_allowlisted"


def test_an_allowlisted_sender_is_processed():
    settings = FilterSettings(sender_allowlist=("*@example.com",))
    assert evaluate(_headers(), settings) is None


def test_an_empty_allowlist_allows_everyone():
    assert evaluate(_headers(), FilterSettings(sender_allowlist=())) is None


def test_the_blocklist_outranks_the_allowlist():
    # "never this sender" must not be silently overridden by a broader
    # "anyone at this domain".
    settings = FilterSettings(
        sender_blocklist=("alice@example.com",), sender_allowlist=("*@example.com",)
    )
    assert evaluate(_headers(), settings) == "blocked_sender:alice@example.com"


def test_the_allowlist_does_not_exempt_a_sender_from_the_bulk_check():
    # An allowlisted domain that starts sending a newsletter is still sending a
    # newsletter. The admin who wants it anyway unticks skip_bulk.
    settings = FilterSettings(sender_allowlist=("*@example.com",))
    headers = _headers(**{"precedence": "bulk"})
    assert evaluate(headers, settings) == "bulk:precedence"


def test_a_blocked_subject_term_is_filtered():
    settings = FilterSettings(subject_blocklist=("unsubscribe",))
    headers = _headers(subject="Please UNSUBSCRIBE me")
    assert evaluate(headers, settings) == "blocked_subject:unsubscribe"


def test_the_subject_blocklist_matches_substrings_case_insensitively():
    settings = FilterSettings(subject_blocklist=("OUT OF OFFICE",))
    headers = _headers(subject="Re: out of office until Monday")
    assert evaluate(headers, settings) == "blocked_subject:OUT OF OFFICE"


def test_a_blocked_sender_outranks_a_blocked_subject():
    settings = FilterSettings(
        sender_blocklist=("alice@example.com",), subject_blocklist=("quote",)
    )
    assert evaluate(_headers(), settings) == "blocked_sender:alice@example.com"


def test_a_missing_from_header_is_processed():
    # Fail open: a malformed header is not evidence the message is junk.
    settings = FilterSettings(sender_blocklist=("*@example.com",))
    assert evaluate({"subject": "hi"}, settings) is None


def test_an_unparseable_from_header_is_processed():
    settings = FilterSettings(sender_allowlist=("*@client.test",))
    # No address at all to compare against -> allowlist cannot judge it.
    assert evaluate({"from": "not an address", "subject": "hi"}, settings) is None


def test_a_missing_subject_header_is_processed():
    settings = FilterSettings(subject_blocklist=("quote",))
    assert evaluate({"from": "alice@example.com"}, settings) is None


def test_header_lookup_is_case_insensitive():
    # IMAP hands headers back however the sender cased them.
    assert evaluate({"From": "a@b.test", "List-Id": "<x>"}, FilterSettings()) == "bulk:list-id"


def test_every_decision_has_a_sentence():
    decisions = [
        "bulk:list-id",
        "bulk:precedence",
        "blocked_sender:*@x.test",
        "blocked_subject:quote",
        "not_allowlisted",
    ]
    for decision in decisions:
        sentence = describe(decision)
        assert sentence and not sentence.startswith("Skipped: bulk:")


def test_an_unknown_decision_still_describes_itself():
    # A row written by a future version must not render as a blank cell.
    assert describe("something_new") == "Skipped: something_new"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_filter.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'ui.backend.email_filter'`.

- [ ] **Step 3: Write the implementation**

Create `ui/backend/email_filter.py`:

```python
"""Pure pre-LLM filter for inbound mail (email automation Phase 4a).

Header rules only, and deliberately so. A cheap classifier model would still
bill per message, would still read attacker-controlled text, and could not be
audited by the admin whose mail it dropped. Bulk mail identifies itself in
headers precisely so that automated agents can recognise it and stay quiet
(RFC 3834 `Auto-Submitted`, RFC 2919 `List-Id`, RFC 8058 `List-Unsubscribe`,
and the de-facto `Precedence`).

No database, no mailbox, no clock -- the same shape as `trigger_health.py`, so
every rule and every ordering decision is exhaustively testable.

See docs/superpowers/specs/2026-08-17-email-phase-4a-filtering-budgets-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Mapping, Optional, Tuple

# Headers that mean "an automaton sent this, do not reply". `auto-submitted`
# is checked for a value other than `none`, because RFC 3834 spells "this is
# ordinary mail" as `Auto-Submitted: none` and filtering on the header's mere
# presence would drop normal messages.
BULK_HEADERS: Tuple[str, ...] = (
    "auto-submitted",
    "precedence",
    "list-id",
    "list-unsubscribe",
)

_BULK_PRECEDENCE_VALUES = ("bulk", "list", "junk")


@dataclass(frozen=True)
class FilterSettings:
    """One org's rules. The defaults are what an org with no stored row gets."""

    skip_bulk: bool = True
    sender_blocklist: Tuple[str, ...] = field(default=())
    sender_allowlist: Tuple[str, ...] = field(default=())
    subject_blocklist: Tuple[str, ...] = field(default=())


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive lookup -- IMAP returns headers cased as the sender
    wrote them."""
    for key, value in headers.items():
        if key.lower() == name:
            return value or ""
    return ""


def _address(headers: Mapping[str, str]) -> str:
    """The bare address from `From`, lowercased, or "" if there isn't one.

    `parseaddr` is what discards the display name, which matters: the display
    name is attacker-chosen free text, so matching on it would let a sender
    evade a blocklist -- or forge past an allowlist -- by typing whatever they
    like into it.
    """
    _name, address = parseaddr(_header(headers, "from"))
    return address.strip().lower() if "@" in address else ""


def _matches(address: str, pattern: str) -> bool:
    """Exactly two forms: a full address, or `*@domain`.

    No regular expressions: a customer-supplied one brings catastrophic
    backtracking into the poll loop, and no admin can be told why theirs did
    not match.
    """
    pattern = pattern.strip().lower()
    if not pattern or not address:
        return False
    if pattern.startswith("*@"):
        return address.endswith(pattern[1:])
    return address == pattern


def _is_bulk(headers: Mapping[str, str]) -> Optional[str]:
    for name in BULK_HEADERS:
        value = _header(headers, name).strip().lower()
        if not value:
            continue
        if name == "auto-submitted" and value == "none":
            continue
        if name == "precedence" and value not in _BULK_PRECEDENCE_VALUES:
            continue
        return name
    return None


def evaluate(headers: Mapping[str, str], settings: FilterSettings) -> Optional[str]:
    """`None` to process the message; a decision string to skip it.

    The order below IS the behaviour, so it is fixed and documented:

    1. sender blocklist  -- "never this sender" must not be silently
       overridden by a broader "anyone at this domain" allowlist entry.
    2. allowlist miss.
    3. subject blocklist.
    4. bulk headers -- deliberately NOT exempted by the allowlist: an
       allowlisted domain that starts sending a newsletter is still sending a
       newsletter, and the admin who wants it anyway unticks `skip_bulk`.

    Anything unparseable fails open (processed), because a malformed header is
    not evidence that a message is junk.
    """
    address = _address(headers)

    for pattern in settings.sender_blocklist:
        if _matches(address, pattern):
            return f"blocked_sender:{pattern}"

    if settings.sender_allowlist and address:
        if not any(_matches(address, p) for p in settings.sender_allowlist):
            return "not_allowlisted"

    subject = _header(headers, "subject").lower()
    if subject:
        for term in settings.subject_blocklist:
            cleaned = term.strip()
            if cleaned and cleaned.lower() in subject:
                return f"blocked_subject:{term}"

    if settings.skip_bulk:
        bulk = _is_bulk(headers)
        if bulk is not None:
            return f"bulk:{bulk}"

    return None


_BULK_SENTENCES = {
    "list-id": "Skipped: bulk mail (mailing list)",
    "list-unsubscribe": "Skipped: bulk mail (has an unsubscribe link)",
    "precedence": "Skipped: bulk mail (marked bulk by the sender)",
    "auto-submitted": "Skipped: an automatic message, not written by a person",
}


def describe(decision: str) -> str:
    """A decision string as a sentence for the customer's activity list.

    A row written by a future version must still render as something, never a
    blank cell -- hence the fallback rather than a KeyError or an empty string.
    """
    if decision.startswith("bulk:"):
        return _BULK_SENTENCES.get(decision[len("bulk:"):], "Skipped: bulk mail")
    if decision.startswith("blocked_sender:"):
        return f"Skipped: the sender matches your blocked list ({decision.split(':', 1)[1]})"
    if decision.startswith("blocked_subject:"):
        return f"Skipped: the subject contains a blocked word ({decision.split(':', 1)[1]})"
    if decision == "not_allowlisted":
        return "Skipped: the sender is not on your allowed list"
    return f"Skipped: {decision}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_filter.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_filter.py tests/test_email_filter.py
git commit -m "feat(email): pure header-rule filter for inbound mail"
```

---

## Task 2: The pure budget evaluator

**Files:**
- Create: `ui/backend/email_budget.py`
- Test: `tests/test_email_budget.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BudgetCaps` dataclass: `daily_message_cap: Optional[int] = None`,
    `monthly_cost_cap: Optional[float] = None`
  - `remaining_messages(caps: BudgetCaps, used_today: int) -> Optional[int]` —
    `None` means unlimited.
  - `cost_exceeded(caps: BudgetCaps, spent_this_month: Optional[float]) -> bool`
  - `month_key(now: datetime) -> str` — `"2026-08"`, UTC.
  - `day_key(now: datetime) -> str` — `"2026-08-17"`, UTC.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_budget.py`:

```python
"""Pure budget arithmetic (email automation Phase 4a).

Split out from the poller for the same reason trigger_health.py was: the
interesting cases (no cap, a NULL cost estimate, a month boundary) are
miserable to reach through a mailbox and trivial to reach here.
"""

from datetime import datetime, timezone

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_budget.py -q`
Expected: collection error — no module `ui.backend.email_budget`.

- [ ] **Step 3: Write the implementation**

Create `ui/backend/email_budget.py`:

```python
"""Pure per-org budget arithmetic (email automation Phase 4a).

The existing `BESTTEAM_EMAIL_DAILY_CAP` counts *runs*, and a run processes up
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_budget.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_budget.py tests/test_email_budget.py
git commit -m "feat(email): pure per-org message and spend budget arithmetic"
```

---

## Task 3: Schema, migration, and settings CRUD

**Files:**
- Modify: `ui/backend/db/models.py`
- Create: `ui/backend/db/email_filter_settings.py`
- Create: `ui/backend/db/email_budget_settings.py`
- Create: `alembic/versions/l9m0n1o2p3q4_add_email_filter_and_budgets.py`
- Test: `tests/test_email_filter_settings_db.py`

**Interfaces:**
- Consumes: `FilterSettings` and `BudgetCaps` from Tasks 1 and 2.
- Produces:
  - `ui.backend.db.email_filter_settings.get_filter_row(db, org_id) -> Optional[OrgEmailFilterSetting]`
  - `ui.backend.db.email_filter_settings.get_filter_settings(db, org_id) -> FilterSettings`
  - `ui.backend.db.email_filter_settings.set_filter_settings(db, org_id, *, skip_bulk, sender_blocklist, sender_allowlist, subject_blocklist) -> OrgEmailFilterSetting`
  - `ui.backend.db.email_budget_settings.get_budget_row(db, org_id) -> Optional[OrgEmailBudgetSetting]`
  - `ui.backend.db.email_budget_settings.get_budget_caps(db, org_id) -> BudgetCaps`
  - `ui.backend.db.email_budget_settings.set_budget_caps(db, org_id, *, daily_message_cap, monthly_cost_cap) -> OrgEmailBudgetSetting`
  - `ui.backend.db.email_budget_settings.spent_this_month(db, org_id, now) -> Optional[float]`
  - `ui.backend.db.email_budget_settings.unpriced_run_count(db, org_id, now) -> int`
  - New column `EmailTrigger.messages_today: int` (default 0).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_filter_settings_db.py`:

```python
"""Row CRUD for the Phase 4a settings tables, plus the monthly-spend query."""

from datetime import datetime, timedelta, timezone

import pytest

from ui.backend.db.email_budget_settings import (
    get_budget_caps,
    set_budget_caps,
    spent_this_month,
    unpriced_run_count,
)
from ui.backend.db.email_filter_settings import get_filter_settings, set_filter_settings
from ui.backend.db.models import Run, UsageRecord

pytestmark = pytest.mark.unit


def test_an_org_with_no_row_gets_the_defaults(db_session, org):
    settings = get_filter_settings(db_session, org.id)
    assert settings.skip_bulk is True
    assert settings.sender_blocklist == ()
    assert settings.sender_allowlist == ()
    assert settings.subject_blocklist == ()


def test_settings_round_trip(db_session, org):
    set_filter_settings(
        db_session, org.id,
        skip_bulk=False,
        sender_blocklist=["a@x.test"],
        sender_allowlist=[],
        subject_blocklist=["out of office"],
    )
    db_session.commit()
    settings = get_filter_settings(db_session, org.id)
    assert settings.skip_bulk is False
    assert settings.sender_blocklist == ("a@x.test",)
    assert settings.subject_blocklist == ("out of office",)


def test_saving_twice_updates_the_same_row(db_session, org):
    set_filter_settings(
        db_session, org.id, skip_bulk=True,
        sender_blocklist=["a@x.test"], sender_allowlist=[], subject_blocklist=[],
    )
    set_filter_settings(
        db_session, org.id, skip_bulk=True,
        sender_blocklist=["b@x.test"], sender_allowlist=[], subject_blocklist=[],
    )
    db_session.commit()
    assert get_filter_settings(db_session, org.id).sender_blocklist == ("b@x.test",)


def test_an_org_with_no_budget_row_has_no_caps(db_session, org):
    caps = get_budget_caps(db_session, org.id)
    assert caps.daily_message_cap is None
    assert caps.monthly_cost_cap is None


def test_budget_caps_round_trip_and_clear(db_session, org):
    set_budget_caps(db_session, org.id, daily_message_cap=30, monthly_cost_cap=12.5)
    db_session.commit()
    caps = get_budget_caps(db_session, org.id)
    assert caps.daily_message_cap == 30
    assert caps.monthly_cost_cap == 12.5

    set_budget_caps(db_session, org.id, daily_message_cap=None, monthly_cost_cap=None)
    db_session.commit()
    caps = get_budget_caps(db_session, org.id)
    assert caps.daily_message_cap is None
    assert caps.monthly_cost_cap is None


def _usage(db_session, org_id, *, cost, created_at, run_id):
    db_session.add(Run(id=run_id, workflow="w", input="", status="completed", org_id=org_id))
    db_session.add(
        UsageRecord(
            run_id=run_id, org_id=org_id, model="openai:gpt-4o-mini",
            input_tokens=10, output_tokens=10, cost_estimate=cost,
            created_at=created_at,
        )
    )


def test_spend_sums_only_this_month_and_only_this_org(db_session, org, other_org):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    _usage(db_session, org.id, cost=1.5, created_at=now - timedelta(days=1), run_id="r1")
    _usage(db_session, org.id, cost=2.0, created_at=now - timedelta(days=40), run_id="r2")
    _usage(db_session, other_org.id, cost=99.0, created_at=now, run_id="r3")
    db_session.commit()
    assert spent_this_month(db_session, org.id, now) == pytest.approx(1.5)


def test_spend_is_none_when_nothing_is_priced(db_session, org):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    _usage(db_session, org.id, cost=None, created_at=now, run_id="r1")
    db_session.commit()
    assert spent_this_month(db_session, org.id, now) is None


def test_unpriced_runs_are_counted_so_the_blind_spot_is_visible(db_session, org):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    _usage(db_session, org.id, cost=None, created_at=now, run_id="r1")
    _usage(db_session, org.id, cost=1.0, created_at=now, run_id="r2")
    db_session.commit()
    assert unpriced_run_count(db_session, org.id, now) == 1
```

**Fixtures:** `db_session`, `org` and `other_org` already exist in
`tests/conftest.py` — read it before writing this file and use them exactly as
`tests/test_retention.py` does. If `other_org` does not exist under that name,
create a second organisation inline the way `tests/test_retention.py` does and
drop the fixture argument.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_filter_settings_db.py -q`
Expected: collection error — no module `ui.backend.db.email_filter_settings`.

- [ ] **Step 3: Add the two models and the trigger column**

In `ui/backend/db/models.py`, next to `OrgRetentionSetting` (follow that class
exactly for style, and put the tables in the same neighbourhood):

```python
class OrgEmailFilterSetting(Base):
    """One org's pre-LLM mail filter rules (Phase 4a).

    An org with no row behaves as `skip_bulk=True` and three empty lists --
    bulk mail is filtered by default. That default is deliberate: the phase
    exists because customers are billed model rates for mail no human wrote,
    and a safety feature nobody switches on protects nobody. It is recoverable:
    one checkbox turns it off, and every filtered message stays visible and
    releasable.
    """

    __tablename__ = "org_email_filter_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True, nullable=False
    )
    skip_bulk: Mapped[bool] = mapped_column(default=True)
    # Lists of patterns; each entry is a full address or `*@domain`. Never a
    # regular expression -- see ui/backend/email_filter.py.
    sender_blocklist: Mapped[list] = mapped_column(JSON, default=list)
    sender_allowlist: Mapped[list] = mapped_column(JSON, default=list)
    subject_blocklist: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class OrgEmailBudgetSetting(Base):
    """One org's customer-facing automation budget (Phase 4a).

    Both caps are NULL by default -- an upgrade must never start refusing to
    process a customer's mail because of a limit they never set. The
    deployment-wide `BESTTEAM_EMAIL_DAILY_CAP` (runs/day) is a separate,
    operator-owned safety rail and is unaffected by these.
    """

    __tablename__ = "org_email_budget_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True, nullable=False
    )
    daily_message_cap: Mapped[Optional[int]] = mapped_column(nullable=True)
    monthly_cost_cap: Mapped[Optional[float]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
```

In `EmailTrigger`, directly under `runs_today`/`runs_date`:

```python
    # Messages (not runs) handed to a model today, for the per-org message cap.
    # Shares `runs_date` on purpose: one rollover check resets both, so the two
    # counters can never disagree about which day it is.
    messages_today: Mapped[int] = mapped_column(default=0)
```

- [ ] **Step 4: Write the two CRUD modules**

Create `ui/backend/db/email_filter_settings.py`:

```python
"""Per-org pre-LLM filter settings (email automation Phase 4a).

Row CRUD only. The rules themselves live in `ui/backend/email_filter.py` so
they can be tested without a database -- the same split `retention.py` uses.

Nothing here commits: callers own the transaction boundary.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.orm import Session

from ..email_filter import FilterSettings
from .models import OrgEmailFilterSetting


def get_filter_row(db: Session, org_id: int) -> Optional[OrgEmailFilterSetting]:
    return (
        db.query(OrgEmailFilterSetting)
        .filter(OrgEmailFilterSetting.org_id == org_id)
        .one_or_none()
    )


def _clean(values) -> tuple:
    """Drop blanks and duplicates while keeping the admin's order, so a list
    they can read back is the list they typed."""
    seen, out = set(), []
    for value in values or []:
        text = str(value).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return tuple(out)


def get_filter_settings(db: Session, org_id: int) -> FilterSettings:
    """This org's rules, or the defaults when it has no row (bulk filtered)."""
    row = get_filter_row(db, org_id)
    if row is None:
        return FilterSettings()
    return FilterSettings(
        skip_bulk=bool(row.skip_bulk),
        sender_blocklist=_clean(row.sender_blocklist),
        sender_allowlist=_clean(row.sender_allowlist),
        subject_blocklist=_clean(row.subject_blocklist),
    )


def set_filter_settings(
    db: Session,
    org_id: int,
    *,
    skip_bulk: bool,
    sender_blocklist: Sequence,
    sender_allowlist: Sequence,
    subject_blocklist: Sequence,
) -> OrgEmailFilterSetting:
    row = get_filter_row(db, org_id)
    if row is None:
        row = OrgEmailFilterSetting(org_id=org_id)
        db.add(row)
    row.skip_bulk = bool(skip_bulk)
    row.sender_blocklist = list(_clean(sender_blocklist))
    row.sender_allowlist = list(_clean(sender_allowlist))
    row.subject_blocklist = list(_clean(subject_blocklist))
    db.flush()
    return row
```

Create `ui/backend/db/email_budget_settings.py`:

```python
"""Per-org automation budget settings and the spend query (Phase 4a).

Monthly spend is queried, never counted into a column: `usage_records.org_id`
is already denormalised for exactly this, and a stored counter would need its
own reset, its own backfill and its own drift bug.

Nothing here commits: callers own the transaction boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..email_budget import BudgetCaps
from .models import OrgEmailBudgetSetting, UsageRecord


def _month_start(now: datetime) -> datetime:
    """First instant of `now`'s UTC month, tzinfo-naive to match how SQLite
    stores and returns `usage_records.created_at`."""
    moment = now.astimezone(timezone.utc) if now.tzinfo else now
    return datetime(moment.year, moment.month, 1)


def get_budget_row(db: Session, org_id: int) -> Optional[OrgEmailBudgetSetting]:
    return (
        db.query(OrgEmailBudgetSetting)
        .filter(OrgEmailBudgetSetting.org_id == org_id)
        .one_or_none()
    )


def get_budget_caps(db: Session, org_id: int) -> BudgetCaps:
    row = get_budget_row(db, org_id)
    if row is None:
        return BudgetCaps()
    return BudgetCaps(
        daily_message_cap=row.daily_message_cap,
        monthly_cost_cap=row.monthly_cost_cap,
    )


def set_budget_caps(
    db: Session,
    org_id: int,
    *,
    daily_message_cap: Optional[int],
    monthly_cost_cap: Optional[float],
) -> OrgEmailBudgetSetting:
    """Set or clear the caps. The row is kept either way, like
    `set_retention_days`."""
    row = get_budget_row(db, org_id)
    if row is None:
        row = OrgEmailBudgetSetting(org_id=org_id)
        db.add(row)
    row.daily_message_cap = daily_message_cap
    row.monthly_cost_cap = monthly_cost_cap
    db.flush()
    return row


def spent_this_month(db: Session, org_id: int, now: datetime) -> Optional[float]:
    """Estimated spend so far this UTC month, or `None` if nothing is priced.

    `None` is not zero and not "over budget": it means every usage record this
    month came from a model with no `model_catalog` entry, which the budget
    surfaces rather than hides.
    """
    return db.execute(
        select(func.sum(UsageRecord.cost_estimate)).where(
            UsageRecord.org_id == org_id,
            UsageRecord.created_at >= _month_start(now),
        )
    ).scalar()


def unpriced_run_count(db: Session, org_id: int, now: datetime) -> int:
    """Distinct runs this month whose usage carried no price.

    Reported in the UI so an admin can tell "we spent almost nothing" from "the
    cap does not cover what we ran".
    """
    return int(
        db.execute(
            select(func.count(func.distinct(UsageRecord.run_id))).where(
                UsageRecord.org_id == org_id,
                UsageRecord.created_at >= _month_start(now),
                UsageRecord.cost_estimate.is_(None),
            )
        ).scalar()
        or 0
    )
```

- [ ] **Step 5: Write the migration**

Create `alembic/versions/l9m0n1o2p3q4_add_email_filter_and_budgets.py`. Copy
the guarded-op structure of `alembic/versions/k8l9m0n1o2p3_add_retention.py`
exactly — `ui/backend/db_session.py` runs `create_all` at import, so a fresh
database already has these objects when the migration runs and every op must
be guarded.

```python
"""add per-org email filter + budget settings and trigger message counter (Phase 4a)

Revision ID: l9m0n1o2p3q4
Revises: k8l9m0n1o2p3
Create Date: 2026-08-17 22:00:00.000000

Purely additive. `skip_bulk` defaults True (bulk mail is filtered by default,
the one deliberate behaviour change this phase makes); both budget caps start
NULL, so no org gains a limit it did not ask for.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'l9m0n1o2p3q4'
down_revision: Union[str, Sequence[str], None] = 'k8l9m0n1o2p3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FILTER = "org_email_filter_settings"
_FILTER_INDEX = "ix_org_email_filter_settings_org_id"
_BUDGET = "org_email_budget_settings"
_BUDGET_INDEX = "ix_org_email_budget_settings_org_id"
_TRIGGERS = "email_triggers"
_MESSAGES_TODAY = "messages_today"


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _columns(inspector, table: str) -> set:
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _tables(inspector)

    if _FILTER not in tables:
        op.create_table(
            _FILTER,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"),
                      nullable=False),
            sa.Column("skip_bulk", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sender_blocklist", sa.JSON(), nullable=True),
            sa.Column("sender_allowlist", sa.JSON(), nullable=True),
            sa.Column("subject_blocklist", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index(_FILTER_INDEX, _FILTER, ["org_id"], unique=True)

    if _BUDGET not in tables:
        op.create_table(
            _BUDGET,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"),
                      nullable=False),
            sa.Column("daily_message_cap", sa.Integer(), nullable=True),
            sa.Column("monthly_cost_cap", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index(_BUDGET_INDEX, _BUDGET, ["org_id"], unique=True)

    if _TRIGGERS in tables and _MESSAGES_TODAY not in _columns(inspector, _TRIGGERS):
        op.add_column(
            _TRIGGERS,
            sa.Column(_MESSAGES_TODAY, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    """Downgrade schema.

    SQLite in this project's venv is 3.45.3, past the 3.35 that made
    `ALTER TABLE ... DROP COLUMN` work, so no batch-mode rebuild is needed.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _tables(inspector)

    if _MESSAGES_TODAY in _columns(inspector, _TRIGGERS):
        op.drop_column(_TRIGGERS, _MESSAGES_TODAY)

    if _BUDGET in tables:
        op.drop_index(_BUDGET_INDEX, table_name=_BUDGET)
        op.drop_table(_BUDGET)

    if _FILTER in tables:
        op.drop_index(_FILTER_INDEX, table_name=_FILTER)
        op.drop_table(_FILTER)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_filter_settings_db.py -q`
Expected: all pass.

- [ ] **Step 7: Verify the migration applies to a real database**

Run:
```powershell
.\.venv\Scripts\python.exe -c "import tempfile, os, subprocess, sys; d=tempfile.mkdtemp(); os.environ['BESTTEAM_DB_PATH']=os.path.join(d,'t.db'); sys.exit(subprocess.call(['.venv/Scripts/python.exe','-m','alembic','upgrade','head']))"
```
Expected: exit 0, no error. If `BESTTEAM_DB_PATH` is not the env var this
project uses, read `alembic/env.py` and `ui/backend/db/database.py` for the
correct one and use that.

Then confirm the revision chain has a single head:
`.\.venv\Scripts\python.exe -m alembic heads`
Expected: exactly one head, `l9m0n1o2p3q4`.

- [ ] **Step 8: Commit**

```bash
git add ui/backend/db/models.py ui/backend/db/email_filter_settings.py ui/backend/db/email_budget_settings.py alembic/versions/l9m0n1o2p3q4_add_email_filter_and_budgets.py tests/test_email_filter_settings_db.py
git commit -m "feat(email): per-org filter rules and budget caps, and their schema"
```

---

## Task 4: The ledger learns `filtered`

**Files:**
- Modify: `ui/backend/db/inbox_events.py`
- Test: `tests/test_inbox_events.py` (extend — read it first and match its style)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - Constant `EVENT_FILTERED = "filtered"`
  - `record_events(db, *, org_id, mailbox_identity, mailbox_generation, external_ids, connector_type=DEFAULT_CONNECTOR, decisions: Optional[Mapping[str, str]] = None) -> int`
    — `decisions` maps an `external_id` to a decision string; those rows are
    inserted `filtered` with `decision` set, the rest `pending`.
  - `list_filtered_events(db, *, org_id, limit) -> List[InboxEvent]` — newest first.
  - `release_filtered_event(db, *, org_id, event_id) -> bool` — flip to
    `pending`, clear `decision`; `False` if no such filtered row for this org.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inbox_events.py` (keep the file's existing fixtures and
helpers; do not restructure it):

```python
def test_a_decision_records_the_row_filtered_not_pending(db_session, org):
    record_events(
        db_session, org_id=org.id, mailbox_identity="h:u", mailbox_generation="1",
        external_ids=["10", "11"], decisions={"11": "bulk:list-id"},
    )
    db_session.commit()
    rows = {r.external_id: r for r in db_session.query(InboxEvent).all()}
    assert rows["10"].status == "pending" and rows["10"].decision is None
    assert rows["11"].status == "filtered" and rows["11"].decision == "bulk:list-id"


def test_a_filtered_row_is_never_claimed(db_session, org):
    record_events(
        db_session, org_id=org.id, mailbox_identity="h:u", mailbox_generation="1",
        external_ids=["10", "11"], decisions={"10": "not_allowlisted"},
    )
    db_session.commit()
    claimed = claim_events(db_session, org_id=org.id, run_id="run-1", limit=10)
    assert [e.external_id for e in claimed] == ["11"]


def test_releasing_a_filtered_row_makes_it_claimable(db_session, org):
    record_events(
        db_session, org_id=org.id, mailbox_identity="h:u", mailbox_generation="1",
        external_ids=["10"], decisions={"10": "bulk:precedence"},
    )
    db_session.commit()
    row = db_session.query(InboxEvent).one()

    assert release_filtered_event(db_session, org_id=org.id, event_id=row.id) is True
    db_session.commit()

    db_session.refresh(row)
    assert row.status == "pending"
    assert row.decision is None
    claimed = claim_events(db_session, org_id=org.id, run_id="run-1", limit=10)
    assert [e.external_id for e in claimed] == ["10"]


def test_releasing_is_idempotent_and_never_resurrects_a_done_row(db_session, org):
    record_events(
        db_session, org_id=org.id, mailbox_identity="h:u", mailbox_generation="1",
        external_ids=["10"], decisions={"10": "bulk:precedence"},
    )
    db_session.commit()
    row = db_session.query(InboxEvent).one()
    assert release_filtered_event(db_session, org_id=org.id, event_id=row.id) is True
    db_session.commit()
    # Already pending -- a second release changes nothing and says so.
    assert release_filtered_event(db_session, org_id=org.id, event_id=row.id) is False


def test_releasing_another_orgs_row_does_nothing(db_session, org, other_org):
    record_events(
        db_session, org_id=other_org.id, mailbox_identity="h:u", mailbox_generation="1",
        external_ids=["10"], decisions={"10": "bulk:precedence"},
    )
    db_session.commit()
    row = db_session.query(InboxEvent).one()
    assert release_filtered_event(db_session, org_id=org.id, event_id=row.id) is False
    db_session.refresh(row)
    assert row.status == "filtered"


def test_filtered_events_list_newest_first_and_only_this_org(db_session, org, other_org):
    record_events(
        db_session, org_id=org.id, mailbox_identity="h:u", mailbox_generation="1",
        external_ids=["10", "11"], decisions={"10": "bulk:list-id", "11": "not_allowlisted"},
    )
    record_events(
        db_session, org_id=other_org.id, mailbox_identity="h2:u2", mailbox_generation="1",
        external_ids=["99"], decisions={"99": "bulk:list-id"},
    )
    db_session.commit()
    rows = list_filtered_events(db_session, org_id=org.id, limit=10)
    assert [r.external_id for r in rows] == ["11", "10"]


def test_recording_with_no_decisions_is_unchanged(db_session, org):
    # The existing call site passes no `decisions` at all; that path must stay
    # byte-for-byte equivalent to today's behaviour.
    record_events(
        db_session, org_id=org.id, mailbox_identity="h:u", mailbox_generation="1",
        external_ids=["10", "11"],
    )
    db_session.commit()
    assert {r.status for r in db_session.query(InboxEvent).all()} == {"pending"}
```

Add `release_filtered_event`, `list_filtered_events` and `EVENT_FILTERED` to the
file's existing import from `ui.backend.db.inbox_events`. If `other_org` is not
an existing fixture, create a second organisation inline as
`tests/test_retention.py` does.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_inbox_events.py -q`
Expected: `ImportError` on `release_filtered_event`.

- [ ] **Step 3: Implement**

In `ui/backend/db/inbox_events.py`:

```python
EVENT_FILTERED = "filtered"
```
next to the other status constants, and extend the module docstring's status
list to mention it.

Extend `record_events` with a `decisions` keyword. The row dicts it already
builds gain `status` and `decision` per id:

```python
def record_events(
    db: Session,
    *,
    org_id: int,
    mailbox_identity: str,
    mailbox_generation: str,
    external_ids: Sequence,
    connector_type: str = DEFAULT_CONNECTOR,
    decisions: Optional[Mapping[str, str]] = None,
) -> int:
    """Record each id, ignoring ones already known.

    `decisions` maps an `external_id` to a pre-LLM filter decision (Phase 4a);
    those ids are recorded `filtered` with the reason, the rest `pending`.
    Filtering changes a row's status, never whether the row exists -- Phase 1's
    durability guarantee is that the commit consuming the mail is the commit
    recording the work, and a filter that skipped the insert would become a
    second way to consume mail with no record of it.
    """
```

and inside the row-building comprehension:

```python
            "status": (
                EVENT_FILTERED if decisions and str(external_id) in decisions
                else EVENT_PENDING
            ),
            "decision": (decisions or {}).get(str(external_id)),
```

Append the two new helpers:

```python
def list_filtered_events(db: Session, *, org_id: int, limit: int) -> List[InboxEvent]:
    """This org's filtered messages, newest first -- what the activity view
    shows so a false positive is discoverable rather than silently lost."""
    return list(
        db.execute(
            select(InboxEvent)
            .where(InboxEvent.org_id == org_id, InboxEvent.status == EVENT_FILTERED)
            .order_by(InboxEvent.id.desc())
            .limit(limit)
        ).scalars()
    )


def release_filtered_event(db: Session, *, org_id: int, event_id: int) -> bool:
    """Hand one filtered message back for normal processing.

    A single status flip is the whole feature: the next poll cycle claims it
    like any other pending row, so there is no second dispatch path to keep
    correct. Scoped by `org_id` and by `status == filtered`, so it can neither
    touch another org's row nor resurrect one that already ran.
    """
    updated = db.execute(
        update(InboxEvent)
        .where(
            InboxEvent.id == event_id,
            InboxEvent.org_id == org_id,
            InboxEvent.status == EVENT_FILTERED,
        )
        .values(status=EVENT_PENDING, decision=None, run_id=None)
        .execution_options(synchronize_session="fetch")
    ).rowcount
    return bool(updated)
```

Add `Mapping` to the `typing` import.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_inbox_events.py -q`
Expected: all pass, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/inbox_events.py tests/test_inbox_events.py
git commit -m "feat(email): the inbox ledger learns filtered, and how to release one"
```

---

## Task 5: Fetch the bulk headers

**Files:**
- Modify: `src/bestteam/tools/email_client.py:426-450` (`_fetch_summaries`)
- Test: `tests/test_email_tools.py` (extend — locate the existing
  `summaries_for` / `_fetch_summaries` tests first and match them)

**Interfaces:**
- Consumes: `BULK_HEADERS` conceptually (do **not** import it — `src/bestteam`
  must not depend on `ui/backend`; the header list is repeated here with a
  comment saying why).
- Produces: `summaries_for(uids)` dicts additionally carry
  `"auto-submitted"`, `"precedence"`, `"list-id"`, `"list-unsubscribe"` keys
  (empty string when absent). Existing keys `id`/`from`/`subject`/`date`/
  `snippet` are unchanged.

- [ ] **Step 1: Write the failing test**

In `tests/test_email_tools.py`, alongside the existing fake-IMAP tests:

```python
def test_summaries_carry_the_bulk_headers_for_the_pre_llm_filter():
    # Phase 4a filters on headers, so the summary fetch has to return them.
    # BODY.PEEK is asserted too: the draft-only toolkit never marks mail seen,
    # and this must not become the thing that does.
    raw = (
        b"From: news@example.com\r\n"
        b"Subject: Weekly\r\n"
        b"Date: Mon, 17 Aug 2026 09:00:00 +0000\r\n"
        b"List-Id: <news.example.com>\r\n"
        b"Precedence: bulk\r\n\r\n"
    )
    conn = _FakeConn(fetch_bytes=raw)
    backend = _backend_with(conn)

    summaries = backend.summaries_for(["7"])

    assert summaries[0]["list-id"] == "<news.example.com>"
    assert summaries[0]["precedence"] == "bulk"
    assert summaries[0]["auto-submitted"] == ""
    assert summaries[0]["subject"] == "Weekly"
    assert "BODY.PEEK" in conn.last_fetch_spec
    for header in ("AUTO-SUBMITTED", "PRECEDENCE", "LIST-ID", "LIST-UNSUBSCRIBE"):
        assert header in conn.last_fetch_spec
```

`_FakeConn` / `_backend_with` are illustrative names — **read
`tests/test_email_tools.py` first and use whatever fake-connection helper it
already provides**, extending it with a `last_fetch_spec` capture if it does
not record the fetch string. Do not introduce a second fake.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py -q -k bulk_headers`
Expected: `KeyError: 'list-id'`.

- [ ] **Step 3: Implement**

In `src/bestteam/tools/email_client.py`, above `_ImapBackend`:

```python
# Headers the UI backend's pre-LLM filter needs (email automation Phase 4a).
# Repeated here rather than imported: `src/bestteam` is the SDK and must not
# depend on `ui/backend`. The two lists are small, stable, and defined by RFCs.
_SUMMARY_HEADER_FIELDS = (
    "FROM", "SUBJECT", "DATE",
    "AUTO-SUBMITTED", "PRECEDENCE", "LIST-ID", "LIST-UNSUBSCRIBE",
)
```

In `_fetch_summaries`, replace the literal fetch string and add the headers to
the returned dict:

```python
            typ, msg_data = conn.uid(
                "fetch",
                uid,
                f"(BODY.PEEK[HEADER.FIELDS ({' '.join(_SUMMARY_HEADER_FIELDS)})])",
            )
            ...
            summary = {
                "id": uid.decode(),
                "from": str(headers.get("From", "")),
                "subject": str(headers.get("Subject", "")),
                "date": str(headers.get("Date", "")),
                "snippet": "",
            }
            for field_name in _SUMMARY_HEADER_FIELDS[3:]:
                summary[field_name.lower()] = str(headers.get(field_name, ""))
            messages.append(summary)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_tools.py -q`
Expected: all pass — including the existing `find`/`read`/`draft_reply` tests,
which share `_fetch_summaries` through `find`.

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/tools/email_client.py tests/test_email_tools.py
git commit -m "feat(email): summaries carry the bulk headers the filter needs"
```

---

## Task 6: The poller filters

**Files:**
- Modify: `ui/backend/email_trigger.py` (`_detect`, around the
  `record_events` call at roughly line 686-698)
- Test: `tests/test_email_trigger.py` (extend)

**Interfaces:**
- Consumes: `evaluate`/`FilterSettings` (Task 1),
  `get_filter_settings` (Task 3), `record_events(..., decisions=...)`
  (Task 4), `summaries_for` returning bulk headers (Task 5).
- Produces: a module-level helper
  `_filter_decisions(backend, org_settings, uids) -> Dict[str, str]` in
  `email_trigger.py`, for the tests to reach directly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_trigger.py`. It already has everything needed:
the `db` fixture, `_org_with_trigger`, `_SubmitRecorder`,
`_fake_workflow_getter`, and the `monkeypatch.setattr(email_trigger,
"check_mailbox", ...)` idiom. Use them; do not build a second harness.

```python
class _SummaryBackend:
    """FakeBackend plus the header summaries the Phase 4a filter reads."""

    def __init__(self, summaries, raises=False):
        self._summaries = summaries
        self.raises = raises
        self.calls = []

    def _connect(self):
        raise AssertionError("check_mailbox is monkeypatched in these tests")

    def summaries_for(self, uids):
        self.calls.append(list(uids))
        if self.raises:
            raise OSError("imap went away")
        return [s for s in self._summaries if s["id"] in {str(u) for u in uids}]


def _filtering_org(db, monkeypatch, backend, *, new_uids=(42, 43)):
    """One org whose next poll cycle detects `new_uids` through `backend`."""
    from ui.backend.db.workflows import publish_workflow_version

    org, trigger = _org_with_trigger(db, last_uid=41)
    publish_workflow_version(db, org_id=org.id, name="triage", config={"v": 1})
    db.commit()
    monkeypatch.setattr(
        email_trigger, "check_mailbox",
        lambda b, u: (3, max(new_uids), list(new_uids)),
    )
    monkeypatch.setattr(email_trigger, "_make_backend", lambda cred, password: backend)
    monkeypatch.setattr(email_trigger, "_executor", _SubmitRecorder())
    return org, trigger


def _events(db, org_id):
    from ui.backend.db.models import InboxEvent
    return {
        e.external_id: e
        for e in db.query(InboxEvent).filter(InboxEvent.org_id == org_id).all()
    }


def test_bulk_mail_is_recorded_filtered_and_never_reaches_a_run(db, monkeypatch):
    backend = _SummaryBackend([
        {"id": "42", "from": "alice@client.test", "subject": "Quote"},
        {"id": "43", "from": "news@x.test", "subject": "Weekly", "list-id": "<n.x.test>"},
    ])
    org, trigger = _filtering_org(db, monkeypatch, backend)

    poll_org(db, trigger, _fake_workflow_getter([]))

    rows = _events(db, org.id)
    assert rows["43"].status == "filtered"
    assert rows["43"].decision == "bulk:list-id"
    assert rows["42"].status in ("pending", "claimed")
    assert rows["42"].decision is None


def test_filtering_still_advances_the_cursor_over_every_detected_uid(db, monkeypatch):
    # Filtering must never become a second way to consume mail unrecorded:
    # every detected UID gets a row, and last_uid moves past all of them, in
    # the one commit Phase 1's durability guarantee rests on.
    backend = _SummaryBackend([
        {"id": "42", "from": "n@x.test", "subject": "a", "precedence": "bulk"},
        {"id": "43", "from": "n@x.test", "subject": "b", "precedence": "bulk"},
    ])
    org, trigger = _filtering_org(db, monkeypatch, backend)

    poll_org(db, trigger, _no_workflow)  # nothing claimable -> never builds

    assert trigger.last_uid == 43
    assert len(_events(db, org.id)) == 2
    assert {e.status for e in _events(db, org.id).values()} == {"filtered"}


def test_a_header_fetch_failure_fails_open(db, monkeypatch):
    # A transient IMAP hiccup must not silently discard a customer's mail. The
    # worst case of failing open is that one junk message is billed.
    backend = _SummaryBackend([], raises=True)
    org, trigger = _filtering_org(db, monkeypatch, backend)

    poll_org(db, trigger, _fake_workflow_getter([]))

    rows = _events(db, org.id)
    assert {e.status for e in rows.values()} <= {"pending", "claimed"}
    assert all(e.decision is None for e in rows.values())


def test_a_uid_with_no_summary_returned_is_processed(db, monkeypatch):
    # summaries_for skips UIDs it cannot fetch; those default to pending.
    backend = _SummaryBackend([
        {"id": "42", "from": "n@x.test", "subject": "a", "list-id": "<n>"},
    ])
    org, trigger = _filtering_org(db, monkeypatch, backend)

    poll_org(db, trigger, _fake_workflow_getter([]))

    rows = _events(db, org.id)
    assert rows["42"].status == "filtered"
    assert rows["43"].status in ("pending", "claimed")


def test_an_org_that_turned_the_bulk_rule_off_keeps_its_bulk_mail(db, monkeypatch):
    from ui.backend.db.email_filter_settings import set_filter_settings

    backend = _SummaryBackend([
        {"id": "42", "from": "n@x.test", "subject": "a", "precedence": "bulk"},
        {"id": "43", "from": "n@x.test", "subject": "b", "precedence": "bulk"},
    ])
    org, trigger = _filtering_org(db, monkeypatch, backend)
    set_filter_settings(db, org.id, skip_bulk=False, sender_blocklist=[],
                        sender_allowlist=[], subject_blocklist=[])
    db.commit()

    poll_org(db, trigger, _fake_workflow_getter([]))

    assert all(e.status != "filtered" for e in _events(db, org.id).values())
```

**`_make_backend` may not exist.** `_detect` currently constructs
`_ImapBackend(...)` inline (around `ui/backend/email_trigger.py:630`). Extract
that construction into a module-level `_make_backend(cred, password)` as part
of Step 3 so these tests have a seam to patch — a pure extraction, no
behaviour change. If a different seam already exists in the file, use that
instead and adjust the tests.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -q -k filter`
Expected: fail — nothing writes `filtered`.

- [ ] **Step 3: Implement**

Add to `ui/backend/email_trigger.py`'s imports:

```python
from . import email_filter
from .db.email_filter_settings import get_filter_settings
```

Add the helper near the other module-level helpers:

```python
def _filter_decisions(backend, settings, uids) -> Dict[str, str]:
    """Which of `uids` the pre-LLM filter rejects, and why.

    Fails open, twice over: a UID the mailbox will not hand us a header for is
    absent from `summaries`, and a fetch that raises returns `{}`. Either way
    the message is recorded `pending` and processed. A transient IMAP hiccup
    must not silently discard a customer's mail; the worst case of failing open
    is that one junk message is billed.
    """
    if not uids:
        return {}
    try:
        summaries = backend.summaries_for([str(u) for u in uids])
    except Exception:  # noqa: BLE001 -- filtering is an optimisation, not a gate
        _logger.warning(
            "email trigger: header fetch failed; processing this batch unfiltered",
            exc_info=True,
        )
        return {}
    decisions = {}
    for summary in summaries:
        decision = email_filter.evaluate(summary, settings)
        if decision is not None:
            decisions[str(summary.get("id"))] = decision
    return decisions
```

In `_detect`, between computing `detected` and calling `record_events`:

```python
        detected = sorted(new_uids)[: batch_size() * _DETECT_MULTIPLIER]
        decisions = _filter_decisions(
            backend, get_filter_settings(db, trigger.org_id), detected
        )
        record_events(
            db,
            org_id=trigger.org_id,
            mailbox_identity=mailbox_identity(cred.host, cred.username),
            mailbox_generation=str(trigger.uidvalidity),
            external_ids=[str(u) for u in detected],
            decisions=decisions,
        )
        trigger.last_uid = max(detected)
        db.commit()
```

`trigger.last_uid` and `record_events` still land in the same commit — do not
move either.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -q`
Expected: all pass, including every pre-existing trigger test.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(email): filter inbound mail before any model sees it"
```

---

## Task 7: The poller enforces both budgets

**Files:**
- Modify: `ui/backend/email_trigger.py` (`_start_triggered_run`, the
  `_at_daily_cap` neighbourhood, and the CAS `update` at ~line 805)
- Test: `tests/test_email_trigger.py` (extend)

**Interfaces:**
- Consumes: `BudgetCaps`, `remaining_messages`, `cost_exceeded`, `day_key`,
  `month_key` (Task 2); `get_budget_caps`, `spent_this_month` (Task 3);
  `EmailTrigger.messages_today` (Task 3).
- Produces: module-level `_BUDGET_MESSAGES_KIND = "budget"` and the two
  fingerprint builders below, used only inside this module.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_email_trigger.py`, reusing `_filtering_org` and
`_SummaryBackend` from Task 6:

```python
def _budget_org(db, monkeypatch, *, new_uids):
    """An org whose cycle detects `new_uids` and whose filter passes them all."""
    summaries = [
        {"id": str(u), "from": "alice@client.test", "subject": "Quote"}
        for u in new_uids
    ]
    backend = _SummaryBackend(summaries)
    recorder = _SubmitRecorder()
    org, trigger = _filtering_org(db, monkeypatch, backend, new_uids=new_uids)
    monkeypatch.setattr(email_trigger, "_executor", recorder)
    return org, trigger, recorder


def _spend(db, org_id, amount, when):
    from ui.backend.db.models import Run, UsageRecord
    db.add(Run(id=f"spent-{amount}-{when.isoformat()}", workflow="w", input="",
               status="completed", org_id=org_id))
    db.add(UsageRecord(run_id=f"spent-{amount}-{when.isoformat()}", org_id=org_id,
                       model="openai:gpt-4o-mini", input_tokens=1, output_tokens=1,
                       cost_estimate=amount, created_at=when))
    db.commit()


def test_the_message_cap_truncates_the_claim(db, monkeypatch):
    from ui.backend.db.email_budget_settings import set_budget_caps

    org, trigger, recorder = _budget_org(db, monkeypatch, new_uids=(42, 43, 44, 45, 46))
    set_budget_caps(db, org.id, daily_message_cap=3, monthly_cost_cap=None)
    db.commit()

    poll_org(db, trigger, _fake_workflow_getter([]))

    assert len(recorder.calls) == 1
    _, args, _kwargs = recorder.calls[0]
    assert args[2].count(",") == 2  # three uids in the input text
    assert trigger.messages_today == 3


def test_a_reached_message_cap_dispatches_nothing(db, monkeypatch):
    from ui.backend.db.email_budget_settings import set_budget_caps
    from ui.backend.db.models import Run

    org, trigger, recorder = _budget_org(db, monkeypatch, new_uids=(42, 43))
    set_budget_caps(db, org.id, daily_message_cap=2, monthly_cost_cap=None)
    trigger.messages_today = 2
    trigger.runs_date = email_trigger._today()
    db.commit()

    poll_org(db, trigger, _no_workflow)

    assert recorder.calls == []
    assert db.query(Run).count() == 0
    assert {e.status for e in _events(db, org.id).values()} == {"pending"}


def test_the_message_counter_resets_with_the_date(db, monkeypatch):
    # messages_today shares runs_date on purpose: one rollover check resets
    # both, so the two counters can never disagree about which day it is.
    from ui.backend.db.email_budget_settings import set_budget_caps

    org, trigger, recorder = _budget_org(db, monkeypatch, new_uids=(42,))
    set_budget_caps(db, org.id, daily_message_cap=5, monthly_cost_cap=None)
    trigger.messages_today = 5
    trigger.runs_today = 5
    trigger.runs_date = "2020-01-01"
    db.commit()

    poll_org(db, trigger, _fake_workflow_getter([]))

    assert trigger.messages_today == 1  # reset to 0, then this cycle's one
    assert trigger.runs_today == 1


def test_a_reached_spend_cap_blocks_dispatch(db, monkeypatch):
    from ui.backend.db.email_budget_settings import set_budget_caps
    from ui.backend.db.models import Run

    org, trigger, recorder = _budget_org(db, monkeypatch, new_uids=(42,))
    set_budget_caps(db, org.id, daily_message_cap=None, monthly_cost_cap=10.0)
    _spend(db, org.id, 10.0, datetime.now(timezone.utc))

    poll_org(db, trigger, _no_workflow)

    assert recorder.calls == []
    assert db.query(Run).count() == 1  # only the spend fixture's own row
    assert {e.status for e in _events(db, org.id).values()} == {"pending"}


def test_a_budget_alert_is_raised_once_per_period(db, monkeypatch):
    from ui.backend.db.email_budget_settings import set_budget_caps
    from ui.backend.db.models import Notification

    org, trigger, _ = _budget_org(db, monkeypatch, new_uids=(42,))
    set_budget_caps(db, org.id, daily_message_cap=None, monthly_cost_cap=10.0)
    _spend(db, org.id, 10.0, datetime.now(timezone.utc))

    poll_org(db, trigger, _no_workflow)
    poll_org(db, trigger, _no_workflow)

    assert db.query(Notification).filter_by(org_id=org.id, kind="budget").count() == 1


def test_a_new_month_alerts_again(db, monkeypatch):
    # A month-scoped fingerprint is what makes "once per period" mean per
    # period rather than once ever -- the _expiry_fingerprint lesson, applied
    # before it can bite a second time.
    from ui.backend.db.email_budget_settings import set_budget_caps
    from ui.backend.db.models import Notification

    org, trigger, _ = _budget_org(db, monkeypatch, new_uids=(42,))
    set_budget_caps(db, org.id, daily_message_cap=None, monthly_cost_cap=10.0)
    _spend(db, org.id, 10.0, datetime(2026, 7, 15, tzinfo=timezone.utc))
    _spend(db, org.id, 10.0, datetime(2026, 8, 15, tzinfo=timezone.utc))

    monkeypatch.setattr(email_trigger, "_utcnow",
                        lambda: datetime(2026, 7, 20, tzinfo=timezone.utc))
    poll_org(db, trigger, _no_workflow)
    monkeypatch.setattr(email_trigger, "_utcnow",
                        lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    poll_org(db, trigger, _no_workflow)

    assert db.query(Notification).filter_by(org_id=org.id, kind="budget").count() == 2


def test_a_budget_pause_does_not_disturb_the_fault_evaluator(db, monkeypatch):
    # A budget ceiling is a normal operating state, not a fault. Routing it
    # through trigger_health.evaluate would corrupt consecutive_faults and
    # compete with real faults for alerted_fingerprint.
    from ui.backend.db.email_budget_settings import set_budget_caps

    org, trigger, _ = _budget_org(db, monkeypatch, new_uids=(42,))
    set_budget_caps(db, org.id, daily_message_cap=None, monthly_cost_cap=1.0)
    _spend(db, org.id, 5.0, datetime.now(timezone.utc))

    poll_org(db, trigger, _no_workflow)

    assert trigger.consecutive_faults == 0
    assert trigger.alerted_fingerprint is None
    assert trigger.last_error is None


def test_an_org_with_no_caps_behaves_exactly_as_before(db, monkeypatch):
    org, trigger, recorder = _budget_org(db, monkeypatch, new_uids=(42, 43, 44))

    poll_org(db, trigger, _fake_workflow_getter([]))

    assert len(recorder.calls) == 1
    assert trigger.messages_today == 3
    assert trigger.runs_today == 1
```

The `_utcnow` patch in the month-boundary test is why the implementation must
take its moment from `email_trigger._utcnow()` and pass it down, rather than
calling `datetime.now` inside `email_budget` or the DB helpers.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -q -k budget`
Expected: fail — no cap is enforced.

- [ ] **Step 3: Implement**

Imports:

```python
from . import email_budget
from .db.email_budget_settings import get_budget_caps, spent_this_month
```

Fingerprints and the alert, near `_expiry_fingerprint`:

```python
_BUDGET_KIND = "budget"


def _budget_fingerprint(which: str, period: str) -> str:
    """Scope a budget alert to the period it is about.

    `has_fingerprint` searches an org's entire notification history, so a bare
    name would alert once ever and every later month would be silent -- the
    same trap `_expiry_fingerprint` exists to avoid.
    """
    return f"budget_{which}:{period}"


def _raise_budget_alert(db: Session, org_id: int, which: str, period: str, body: str) -> None:
    """Alert once per period that automation has paused on a budget.

    Deliberately NOT routed through `trigger_health.evaluate`: a budget
    ceiling is a normal operating state, not a fault, and feeding it into the
    fault evaluator would corrupt `consecutive_faults` and compete with real
    faults for `alerted_fingerprint`.
    """
    fingerprint = _budget_fingerprint(which, period)
    if has_fingerprint(db, org_id, fingerprint):
        return
    create_notification(
        db, org_id=org_id, kind=_BUDGET_KIND, severity="warning",
        title="Automatic email runs have paused -- budget reached",
        body=body, fingerprint=fingerprint,
    )
```

Date rollover: find where `runs_today`/`runs_date` are reset for a new day and
reset `messages_today` in the same place, so the two counters cannot disagree.

In `_start_triggered_run`, replace the fixed claim limit:

```python
    caps = get_budget_caps(db, trigger.org_id)
    now = _utcnow()

    if email_budget.cost_exceeded(caps, spent_this_month(db, trigger.org_id, now)):
        registry.discard(run.id)
        _raise_budget_alert(
            db, trigger.org_id, "cost", email_budget.month_key(now),
            "This organisation has reached its monthly spend limit for automatic "
            "email runs. New mail is still being collected and will be processed "
            "when the new month begins, or sooner if you raise the limit.",
        )
        db.commit()
        return

    remaining = email_budget.remaining_messages(caps, trigger.messages_today)
    if remaining == 0:
        registry.discard(run.id)
        _raise_budget_alert(
            db, trigger.org_id, "messages", email_budget.day_key(now),
            "This organisation has reached its daily limit for automatically "
            "processed emails. New mail is still being collected and will be "
            "processed tomorrow, or sooner if you raise the limit.",
        )
        db.commit()
        return

    limit = batch_size() if remaining is None else min(batch_size(), remaining)
    claimed = claim_events(db, org_id=trigger.org_id, run_id=run.id, limit=limit)
```

The `registry.create` call stays above this block — the run id must exist
before the claim can stamp it, and `registry.discard` is the same
create-then-discard shape the existing no-claim and disabled-mid-build
branches use.

In the CAS `update(...).values(...)` that advances `runs_today`, add:

```python
            messages_today=EmailTrigger.messages_today + len(claimed),
```

so the message counter advances in exactly the same atomic statement as the
run counter, under the same enabled/active guard, and is not advanced on any
path that releases the claim.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py tests/test_trigger_health.py -q`
Expected: all pass. `test_trigger_health.py` is included deliberately: budget
alerts must not have disturbed the fault evaluator's state.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "feat(email): a real daily message cap and monthly spend cap"
```

---

## Task 8: The API

**Files:**
- Modify: `ui/backend/org_settings.py` (append after the retention section)
- Modify: `ui/backend/email_trigger_api.py`
- Test: `tests/test_email_filter_api.py` (create)
- Test: `tests/test_email_trigger_api.py` (extend)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces:
  - `GET /api/org/email-filter` → `{skip_bulk, sender_blocklist, sender_allowlist, subject_blocklist}`
  - `PUT /api/org/email-filter` — same body, returns the same shape
  - `GET /api/org/email-budget` → `{daily_message_cap, monthly_cost_cap, messages_today, spent_this_month, unpriced_runs_this_month, unpriced_models}`
  - `PUT /api/org/email-budget` — body `{daily_message_cap, monthly_cost_cap}`, returns the GET shape
  - `GET /api/org/email-trigger/filtered?limit=` → `{filtered: [{id, external_id, decision, reason, detected_at}]}`
  - `POST /api/org/email-trigger/filtered/{event_id}/release` → `{released: true}` or 404

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_filter_api.py`, modelled on the retention route tests
in `tests/test_retention_api.py` (read it first for the auth-client fixture):

```python
"""Filter and budget settings routes (email automation Phase 4a)."""

import pytest

pytestmark = pytest.mark.unit


def test_an_org_with_no_row_reads_the_defaults(client):
    body = client.get("/api/org/email-filter").json()
    assert body["skip_bulk"] is True
    assert body["sender_blocklist"] == []


def test_saving_rules_round_trips(client):
    client.put("/api/org/email-filter", json={
        "skip_bulk": False,
        "sender_blocklist": ["noreply@x.test", " noreply@x.test "],
        "sender_allowlist": [],
        "subject_blocklist": ["out of office"],
    })
    body = client.get("/api/org/email-filter").json()
    assert body["skip_bulk"] is False
    # Duplicates and whitespace are cleaned, so the admin reads back what they
    # meant rather than what they typed twice.
    assert body["sender_blocklist"] == ["noreply@x.test"]


def test_a_regex_is_stored_as_a_literal_not_compiled(client):
    # No promise is made that this matches anything -- the point is that it is
    # accepted as text and never evaluated as a pattern.
    client.put("/api/org/email-filter", json={
        "skip_bulk": True, "sender_blocklist": ["(a+)+@x.test"],
        "sender_allowlist": [], "subject_blocklist": [],
    })
    assert client.get("/api/org/email-filter").json()["sender_blocklist"] == ["(a+)+@x.test"]


def test_budget_defaults_to_no_caps(client):
    body = client.get("/api/org/email-budget").json()
    assert body["daily_message_cap"] is None
    assert body["monthly_cost_cap"] is None


def test_saving_and_clearing_caps(client):
    client.put("/api/org/email-budget", json={
        "daily_message_cap": 25, "monthly_cost_cap": 40.0,
    })
    assert client.get("/api/org/email-budget").json()["daily_message_cap"] == 25
    client.put("/api/org/email-budget", json={
        "daily_message_cap": None, "monthly_cost_cap": None,
    })
    assert client.get("/api/org/email-budget").json()["monthly_cost_cap"] is None


def test_a_negative_cap_is_rejected(client):
    assert client.put("/api/org/email-budget", json={
        "daily_message_cap": -1, "monthly_cost_cap": None,
    }).status_code == 422


def test_saving_a_spend_cap_names_the_models_it_cannot_cover(client, unpriced_workflow_org):
    # The cap still saves -- the admin may be about to add the catalogue row --
    # but they are told which models the limit does not see.
    body = client.put("/api/org/email-budget", json={
        "daily_message_cap": None, "monthly_cost_cap": 10.0,
    }).json()
    assert "fake:demo" in body["unpriced_models"]


def test_the_routes_are_org_scoped(client, second_org_client):
    client.put("/api/org/email-filter", json={
        "skip_bulk": False, "sender_blocklist": ["a@x.test"],
        "sender_allowlist": [], "subject_blocklist": [],
    })
    assert second_org_client.get("/api/org/email-filter").json()["skip_bulk"] is True
```

Fixture names (`client`, `second_org_client`, `unpriced_workflow_org`) are
illustrative — **read `tests/test_retention_api.py` and `tests/conftest.py`
first and use the fixtures that already exist**, constructing the unpriced-model
case inline if no fixture fits.

Extend `tests/test_email_trigger_api.py`:

```python
def test_filtered_messages_are_listed_with_a_readable_reason(client, ...):
    body = client.get("/api/org/email-trigger/filtered").json()
    assert body["filtered"][0]["reason"].startswith("Skipped:")
    assert body["filtered"][0]["decision"] == "bulk:list-id"


def test_releasing_a_filtered_message_makes_it_pending(client, db_session, ...):
    event_id = ...
    assert client.post(f"/api/org/email-trigger/filtered/{event_id}/release").status_code == 200
    assert db_session.get(InboxEvent, event_id).status == "pending"


def test_releasing_an_unknown_id_is_404(client):
    assert client.post("/api/org/email-trigger/filtered/999999/release").status_code == 404


def test_releasing_another_orgs_message_is_404_not_403(client, other_org_event_id):
    # 403 would confirm the row exists; 404 tells a cross-org prober nothing.
    assert client.post(
        f"/api/org/email-trigger/filtered/{other_org_event_id}/release"
    ).status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_filter_api.py tests/test_email_trigger_api.py -q`
Expected: 404s from the not-yet-registered routes.

- [ ] **Step 3: Implement the settings routes**

Append to `ui/backend/org_settings.py`, after the retention section, following
that section's structure exactly:

```python
# --- pre-LLM mail filter and automation budgets (Phase 4a) --------------------


class EmailFilterRequest(BaseModel):
    """Patterns are literals, never regular expressions -- see
    `ui/backend/email_filter.py` for why."""

    skip_bulk: bool = True
    sender_blocklist: List[str] = Field(default_factory=list, max_length=200)
    sender_allowlist: List[str] = Field(default_factory=list, max_length=200)
    subject_blocklist: List[str] = Field(default_factory=list, max_length=200)


class EmailBudgetRequest(BaseModel):
    """NULL means no cap -- the default, so an upgrade limits nobody."""

    daily_message_cap: Optional[int] = Field(default=None, ge=1, le=100000)
    monthly_cost_cap: Optional[float] = Field(default=None, ge=0.01, le=1000000)


@router.get("/email-filter")
def get_email_filter(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    settings = get_filter_settings(db, org.id)
    return {
        "skip_bulk": settings.skip_bulk,
        "sender_blocklist": list(settings.sender_blocklist),
        "sender_allowlist": list(settings.sender_allowlist),
        "subject_blocklist": list(settings.subject_blocklist),
    }


@router.put("/email-filter")
def put_email_filter(
    req: EmailFilterRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    set_filter_settings(
        db, org.id,
        skip_bulk=req.skip_bulk,
        sender_blocklist=req.sender_blocklist,
        sender_allowlist=req.sender_allowlist,
        subject_blocklist=req.subject_blocklist,
    )
    db.commit()
    return get_email_filter(db=db, org=org)


@router.get("/email-budget")
def get_email_budget(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
) -> Dict[str, Any]:
    """The caps, this period's usage against them, and the blind spot.

    `unpriced_models` is advisory, never an error: a model with no
    `model_catalog` row contributes 0 to the spend total, so the cap is a floor
    on reality rather than a phantom ceiling -- and the admin is told which
    models it does not cover instead of being left to infer it.
    """
    now = datetime.now(timezone.utc)
    caps = get_budget_caps(db, org.id)
    trigger = db.query(EmailTrigger).filter(EmailTrigger.org_id == org.id).one_or_none()
    return {
        "daily_message_cap": caps.daily_message_cap,
        "monthly_cost_cap": caps.monthly_cost_cap,
        "messages_today": trigger.messages_today if trigger else 0,
        "spent_this_month": spent_this_month(db, org.id, now),
        "unpriced_runs_this_month": unpriced_run_count(db, org.id, now),
        "unpriced_models": unpriced_models_for_org(db, org.id),
    }


@router.put("/email-budget")
def put_email_budget(
    req: EmailBudgetRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    set_budget_caps(
        db, org.id,
        daily_message_cap=req.daily_message_cap,
        monthly_cost_cap=req.monthly_cost_cap,
    )
    db.commit()
    return get_email_budget(db=db, org=org)
```

`unpriced_models_for_org(db, org_id) -> List[str]` is a small helper to add to
`ui/backend/db/email_budget_settings.py`. It reads the org's trigger's
workflow config, collects every distinct model spec it names, and returns
those with no `model_catalog` row. Read `ui/backend/crud.py` for how a
workflow's agents and their `model` specs are loaded for an org, and reuse that
— do not re-parse YAML here. Return `[]` when the org has no trigger or no
workflow, and swallow a lookup failure into `[]` with a `_logger.warning`:
this is advisory copy on a settings page and must never break saving a cap.

- [ ] **Step 4: Implement the filtered-message routes**

Append to `ui/backend/email_trigger_api.py`:

```python
@router.get("/email-trigger/filtered")
def list_filtered(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Mail the pre-LLM filter skipped, newest first.

    Shown rather than silently dropped because a rule-based filter will have
    false positives, and the cost of one has to be "an admin clicks Release",
    not "the enquiry vanished and nobody knew".
    """
    rows = list_filtered_events(db, org_id=org.id, limit=limit)
    return {
        "filtered": [
            {
                "id": row.id,
                "external_id": row.external_id,
                "decision": row.decision,
                "reason": email_filter.describe(row.decision or ""),
                "detected_at": iso_utc(row.detected_at) if row.detected_at else None,
            }
            for row in rows
        ]
    }


@router.post("/email-trigger/filtered/{event_id}/release")
def release_filtered(
    event_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Hand one skipped message back for normal processing on the next cycle."""
    # 404 rather than 403 for another org's row, and for one already released:
    # the two are indistinguishable to a caller, so probing learns nothing.
    if not release_filtered_event(db, org_id=org.id, event_id=event_id):
        raise HTTPException(status_code=404, detail="No such filtered message.")
    db.commit()
    return {"released": True}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_filter_api.py tests/test_email_trigger_api.py tests/test_org_settings*.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/org_settings.py ui/backend/email_trigger_api.py ui/backend/db/email_budget_settings.py tests/test_email_filter_api.py tests/test_email_trigger_api.py
git commit -m "feat(email): filter, budget and released-message APIs"
```

---

## Task 9: The frontend

**Files:**
- Create: `ui/frontend/src/components/EmailFilterSettings.tsx`
- Create: `ui/frontend/src/components/EmailFilterSettings.test.tsx`
- Create: `ui/frontend/src/components/EmailBudgetSettings.tsx`
- Create: `ui/frontend/src/components/EmailBudgetSettings.test.tsx`
- Modify: `ui/frontend/src/lib/api.ts`, `ui/frontend/src/lib/types.ts`
- Modify: `ui/frontend/src/components/EmailTriggerActivity.tsx` and its test

**Interfaces:**
- Consumes: the six routes from Task 8.
- Produces: `api.getEmailFilter()`, `api.setEmailFilter(payload)`,
  `api.getEmailBudget()`, `api.setEmailBudget(payload)`,
  `api.listFilteredMessages(limit?)`, `api.releaseFilteredMessage(id)`.

- [ ] **Step 1: Write the failing tests**

`EmailFilterSettings.test.tsx`, modelled exactly on
`ui/frontend/src/components/WebhookSettings.test.tsx` (read it first — same
`vi.mock('../lib/api')` shape, same `findByLabelText` style):

```tsx
it('loads the current rules', async () => { /* skip_bulk checked, lists rendered */ })
it('saves edited rules', async () => { /* setEmailFilter called with the parsed lists */ })
it('parses one pattern per line', async () => { /* textarea -> string[] */ })
it('says which two pattern forms are allowed', async () => {
  // The UI has to state this: an admin who types a regex and sees nothing
  // filtered has no other way to find out why.
  expect(await screen.findByText(/\*@example\.com/)).toBeInTheDocument()
})
it('shows the API error instead of pretending it saved', async () => { /* ... */ })
```

`EmailBudgetSettings.test.tsx`:

```tsx
it('shows usage against each cap', async () => { /* "12 / 25 today" */ })
it('saves both caps', async () => { /* ... */ })
it('clearing a field sends null, not zero', async () => {
  // 0 would be a cap of zero -- automation off -- which is not what an empty
  // box means.
})
it('warns when the cap cannot cover a model', async () => {
  expect(await screen.findByText(/does not cover/i)).toBeInTheDocument()
})
it('reports unpriced runs so the blind spot is visible', async () => { /* ... */ })
```

`EmailTriggerActivity.test.tsx` additions:

```tsx
it('lists filtered messages with a readable reason', async () => { /* ... */ })
it('releases one and removes it from the list without a reload', async () => {
  // The same defect the Phase 3b review found in RunDetail: deleting
  // server-side and leaving the row on screen.
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run from `ui/frontend`: `npm test -- --run EmailFilterSettings EmailBudgetSettings EmailTriggerActivity`
Expected: fail — components/methods do not exist.

- [ ] **Step 3: Implement**

Add to `ui/frontend/src/lib/api.ts`, matching the file's existing method style:

```ts
  getEmailFilter: () => get<EmailFilterSettings>('/api/org/email-filter'),
  setEmailFilter: (payload: EmailFilterSettings) =>
    put<EmailFilterSettings>('/api/org/email-filter', payload),
  getEmailBudget: () => get<EmailBudget>('/api/org/email-budget'),
  setEmailBudget: (payload: EmailBudgetInput) =>
    put<EmailBudget>('/api/org/email-budget', payload),
  listFilteredMessages: (limit?: number) =>
    get<{ filtered: FilteredMessage[] }>(
      `/api/org/email-trigger/filtered${limit ? `?limit=${limit}` : ''}`,
    ),
  releaseFilteredMessage: (id: number) =>
    post<{ released: boolean }>(`/api/org/email-trigger/filtered/${id}/release`, {}),
```

Use whatever the file's actual `get`/`put`/`post` helpers are named — read it
first and match; do not introduce a new fetch wrapper.

Types in `ui/frontend/src/lib/types.ts`:

```ts
export interface EmailFilterSettings {
  skip_bulk: boolean
  sender_blocklist: string[]
  sender_allowlist: string[]
  subject_blocklist: string[]
}

export interface EmailBudgetInput {
  daily_message_cap: number | null
  monthly_cost_cap: number | null
}

export interface EmailBudget extends EmailBudgetInput {
  messages_today: number
  spent_this_month: number | null
  unpriced_runs_this_month: number
  unpriced_models: string[]
}

export interface FilteredMessage {
  id: number
  external_id: string
  decision: string | null
  reason: string
  detected_at: string | null
}
```

Build the two components on `WebhookSettings.tsx`'s skeleton: fetch on mount,
local state, a save button, an error line, a "Saved." line. Each pattern list
is a `<textarea>`, one pattern per line, split on newline and trimmed. Copy
must state the two allowed forms verbatim: *"one per line — a full address
(`noreply@example.com`) or a whole domain (`*@example.com`)"*.

In `EmailTriggerActivity.tsx`, add a Filtered section listing
`reason` + `external_id` + `detected_at` with a Release button. On a successful
release, **remove the row from local state immediately** rather than waiting
for a refetch — this is the exact defect the Phase 3b review found in
`RunDetail.tsx`.

- [ ] **Step 4: Run the tests, the type-checker and the linter**

From `ui/frontend`:
```
npm test -- --run
npx tsc --noEmit
npm run lint
```
Expected: all tests pass, no type errors, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src
git commit -m "feat(ui): filter rules, budgets, and releasing a skipped message"
```

---

## Task 10: Docs, and the whole suite

**Files:**
- Modify: `ui/backend/CLAUDE.md`, `ui/frontend/CLAUDE.md`, `docs/STATUS.md`,
  `CLAUDE.md`

- [ ] **Step 1: Update `ui/backend/CLAUDE.md`**

Add, in the email-automation section:
- `email_filter.py` and `email_budget.py` as pure evaluators, and **why**
  rules rather than a classifier (cost per message, injection surface,
  auditability).
- The fixed evaluation order, and that the allowlist does not exempt from the
  bulk check.
- That filtering changes an `inbox_events` row's *status*, never whether the
  row exists, and that release is a single status flip.
- That budget alerts bypass `trigger_health.evaluate` deliberately, with
  period-scoped fingerprints, and why.
- The three limits and their two audiences.
- That `summaries_for` now costs one extra IMAP login per productive cycle.

- [ ] **Step 2: Update `ui/frontend/CLAUDE.md`**

Note the two new settings panels, the Filtered section in the trigger activity
view, and that a release updates local state rather than refetching.

- [ ] **Step 3: Update `docs/STATUS.md`**

Add a **Done** entry for Phase 4a in the voice of the existing Phase 3a/3b
entries. Then edit the "known issues" bullet that currently reads *"there is
no pre-LLM filtering, so spam is billed at model rates; the daily cap counts
runs, not messages or spend"* — that sentence is now wrong. Replace it with
what remains true: header-only filtering does not catch a human-written
irrelevant email, and attachments are still invisible (Phase 4b).

Add to known issues:
- Costs are estimates from an operator-maintained `model_catalog`, reconciled
  against no provider bill.
- The spend cap is enforced between runs, not within one.
- Filtered rows are never purged (`inbox_events` already grows unboundedly).

- [ ] **Step 4: Update the root `CLAUDE.md`**

In "Known limitations / unimplemented extension points", update the email
bullets to say filtering and budgets now exist and what shape they take.

- [ ] **Step 5: Run the full backend suite serially**

This is `backend-full` parity — the CI job that catches ordering and
cross-test isolation bugs. It is gated to `main`, so it will **not** run on
this branch's PR; running it locally is the only way to get that coverage
before merging.

Run: `.\.venv\Scripts\python.exe -m pytest -m "not e2e"` (serial, **no**
`-n auto`)
Expected: all pass except `tests/test_packaging.py::test_python_dash_m_bestteam_entry_point`,
which fails on this machine for a pre-existing environmental reason (the CLI's
UTF-8 `--help` output vs the console's GBK codec) unrelated to this work.

- [ ] **Step 6: Run the frontend suite**

From `ui/frontend`: `npm test -- --run && npx tsc --noEmit && npm run lint`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add ui/backend/CLAUDE.md ui/frontend/CLAUDE.md docs/STATUS.md CLAUDE.md
git commit -m "docs: record pre-LLM filtering and real budgets, and their limits"
```
