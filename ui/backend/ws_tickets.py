"""Short-lived, single-use tickets for authenticating WebSocket stream
connections (CR-013).

Browsers can't set Authorization headers when opening a WebSocket, so the old
design put the long-lived bearer token in the `?token=` query string -- where
it can leak via proxy/access logs, browser history, and Referer headers. An
authenticated REST caller instead exchanges its bearer for a ticket here
(`issue_ticket`) and puts only that ticket in the URL. A ticket is valid once
and for a few seconds, so a leaked URL is near-useless.

In-memory, single-process -- same scope as `RunRegistry`; a multi-process
deployment would back this with a shared store behind the same interface.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Dict, Optional, Tuple

_TICKET_TTL_SECONDS = 30
# ticket -> (username, security_stamp, expires_at). The stamp is the account's
# credential generation at issuance; the stream rejects the ticket if the
# account's current stamp differs (password reset / username reuse since the
# ticket was minted) -- review r-ext2 #1/#3.
_tickets: Dict[str, Tuple[str, Optional[str], float]] = {}
# Tickets are issued from request threads and redeemed/purged from the WS
# handler's event-loop thread, so all access is serialised -- otherwise a
# concurrent purge iterating `_tickets` while another op mutates it can raise
# "dictionary changed size during iteration" (CR-013).
_lock = threading.Lock()


def issue_ticket(username: str, security_stamp: Optional[str]) -> str:
    """Mint a single-use ticket bound to `username` + its security stamp."""
    ticket = secrets.token_urlsafe(32)
    with _lock:
        _purge_expired()
        _tickets[ticket] = (username, security_stamp, time.time() + _TICKET_TTL_SECONDS)
    return ticket


def consume_ticket(ticket: str) -> Optional[Tuple[str, Optional[str]]]:
    """Redeem `ticket` once, returning `(username, security_stamp)`, or None if
    unknown/expired. The ticket is removed on first lookup, so a replay fails."""
    with _lock:
        entry = _tickets.pop(ticket, None)
    if entry is None:
        return None
    username, security_stamp, expires_at = entry
    if time.time() > expires_at:
        return None
    return (username, security_stamp)


def _purge_expired() -> None:
    """Drop expired tickets. Caller must hold `_lock`."""
    now = time.time()
    for ticket in [t for t, (_, _, expires_at) in _tickets.items() if expires_at < now]:
        _tickets.pop(ticket, None)
