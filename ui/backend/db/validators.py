"""Shared identifier validation for account/org provisioning (review r-ext2 #4).

Org names and usernames are addressed through path-based admin endpoints
(`/api/admin/orgs/{name}`, `/api/admin/users/{username}`), so a name containing
`/` is created but then unreachable (the router can't match it). Reject such
names -- and over-length ones -- at both the API boundary and the DB helpers, so
a direct API call can't create an unmanageable record either.
"""

from __future__ import annotations

import re

MAX_IDENTIFIER_LENGTH = 64
# URL-safe grammar: ASCII letters/digits plus '.', '_', '-'. This excludes the
# path separators '/' and '\', whitespace, control chars, and '%' -- anything a
# client/proxy might normalize or that breaks path routing. The exact segments
# '.' and '..' pass the charset but are rejected separately: proxies collapse
# them (dot-segment removal, RFC 3986), so such a record would be unaddressable.
_IDENTIFIER_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")


def clean_identifier(value: str, *, field: str = "identifier") -> str:
    """Trim and validate a path-addressable identifier. Returns the trimmed
    value; raises ``ValueError`` on a blank, over-length, non-URL-safe, or
    dot-segment (``.``/``..``) one (review r-ext2/r-ext3 #4)."""
    cleaned = value.strip() if value else ""
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    if len(cleaned) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field} must be at most {MAX_IDENTIFIER_LENGTH} characters")
    if not _IDENTIFIER_CHARS.match(cleaned):
        raise ValueError(f"{field} may contain only letters, digits, '.', '_' and '-'")
    if cleaned in {".", ".."}:
        raise ValueError(f"{field} must not be '.' or '..'")
    return cleaned
