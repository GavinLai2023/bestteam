"""Shared identifier validation for account/org provisioning (review r-ext2 #4).

Org names and usernames are addressed through path-based admin endpoints
(`/api/admin/orgs/{name}`, `/api/admin/users/{username}`), so a name containing
`/` is created but then unreachable (the router can't match it). Reject such
names -- and over-length ones -- at both the API boundary and the DB helpers, so
a direct API call can't create an unmanageable record either.
"""

from __future__ import annotations

MAX_IDENTIFIER_LENGTH = 64


def clean_identifier(value: str, *, field: str = "identifier") -> str:
    """Trim and validate a path-addressable identifier. Returns the trimmed
    value; raises ``ValueError`` on a blank, `/`-containing, or over-length one."""
    cleaned = value.strip() if value else ""
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    if "/" in cleaned:
        raise ValueError(f"{field} must not contain '/'")
    if len(cleaned) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field} must be at most {MAX_IDENTIFIER_LENGTH} characters")
    return cleaned
