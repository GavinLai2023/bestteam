"""Per-user-memory lifecycle shared by the operator CLI and the admin API.

Account deletion/move must purge or reconcile the deleted/moved principal's
per-user memory, failing closed. Both the `ui.backend.admin` CLI and the admin
API (`admin_api.py`) run the same steps through these helpers, which open the
configured store from `BESTTEAM_MEMORY_DB`. The web backend always runs with the
server's environment; the CLI must be invoked with it (it warns loudly if not).
"""

from __future__ import annotations

import os
from typing import Optional


def open_memory_store():
    """Open the configured memory store, or None when memory isn't usable from
    this invocation.

    Returns None when `BESTTEAM_MEMORY_DB` is unset OR points at a path that
    doesn't exist yet -- opening would *create* an empty DB and give a false
    "purged, all clean"; a store that was never written has nothing to purge.
    """
    db_path = os.environ.get("BESTTEAM_MEMORY_DB", "").strip()
    if not db_path or not os.path.exists(db_path):
        return None
    from bestteam import SqliteBM25Memory

    return SqliteBM25Memory(db_path)


def purge_user_memory(username: str) -> Optional[int]:
    """Delete all of `username`'s per-user memory, returning the rows removed, or
    ``None`` when no memory store is configured for this invocation.

    Account deletion removes the whole principal, so the unscoped
    `store.delete_user` (every row for the username, across all orgs + legacy) is
    correct here -- unlike org erasure. Raises on failure so the caller can fail
    closed and NOT release the username while its memory persists.
    """
    store = open_memory_store()
    if store is None:
        return None
    try:
        return store.delete_user(username)
    finally:
        store.close()


def reconcile_legacy_org(username: str, org_id: int) -> Optional[int]:
    """Bind `username`'s legacy NULL-org memory to `org_id` (their current org)
    before a move, or None when no store is configured. Raises on failure."""
    store = open_memory_store()
    if store is None:
        return None
    try:
        return store.assign_legacy_to_org(username, org_id)
    finally:
        store.close()
