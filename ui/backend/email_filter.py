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
# is checked for a value other than "ordinary mail", because RFC 3834 s5
# spells that as `Auto-Submitted: no` (the other defined values are
# `auto-generated` and `auto-replied`) and filtering on the header's mere
# presence would drop normal messages. `none` is tolerated too: it is not the
# RFC spelling, but a common malformation in the wild, and accepting it only
# ever fails open -- the direction this module is required to fail.
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
        if name == "auto-submitted" and value in ("no", "none"):
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
