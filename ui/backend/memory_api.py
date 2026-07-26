"""Admin memory-management API (`/api/memory`).

Lets an admin inspect and prune what the per-user memory system
(`bestteam.core.memory`) has stored: list users with per-type record counts,
browse/search a user's records, delete a single record, or clear a user's whole
memory. Admin-only (`get_current_admin`) since memory holds other users' past
prompts and outputs.

Memory is opt-in: when `BESTTEAM_MEMORY_DB` is unset the store doesn't exist, so
read endpoints report `enabled: false` (empty results) and mutations return 409,
letting the UI show a clean "memory not enabled" state instead of erroring.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from bestteam import SqliteBM25Memory

from .auth_api import get_current_admin
from .db.models import User
from .db_session import get_db

router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    dependencies=[Depends(get_current_admin)],
)


def get_memory_store() -> Iterator[Optional[SqliteBM25Memory]]:
    """Yield a per-request memory store, or None when memory is disabled.

    Mirrors `runtime._make_memory`'s env handling but returns the raw store.
    Opened on the request's threadpool thread (so the SQLite connection stays
    thread-local) and closed afterward to avoid leaking connections.
    """
    db_path = os.environ.get("BESTTEAM_MEMORY_DB", "").strip()
    if not db_path:
        yield None
        return
    store = SqliteBM25Memory(db_path)
    try:
        yield store
    finally:
        store.close()


def _require_store(store: Optional[SqliteBM25Memory]) -> SqliteBM25Memory:
    if store is None:
        raise HTTPException(status_code=409, detail="Memory is not enabled on this deployment")
    return store


# Upper bound on records returned per request. Memory retention is unbounded, so
# the list/search endpoints cap their response rather than dumping a whole store.
_MAX_RECORDS = 1000
_DEFAULT_RECORDS = 200
# Cap on how many (most-recent) records a search tokenizes/BM25-indexes, so an
# admin search over a bloated user store does bounded DB/CPU/memory work (the
# response is further capped by `limit`). Larger than _MAX_RECORDS so ranking has
# headroom above the response cap.
_MAX_SEARCH_SCAN = 2000


@router.get("/users")
def list_users(store: Optional[SqliteBM25Memory] = Depends(get_memory_store)) -> dict:
    if store is None:
        return {"enabled": False, "users": []}
    # One aggregate query (GROUP BY) instead of scanning every record per user.
    return {"enabled": True, "users": store.user_summaries()}


@router.get("/users/{user_id}/records")
def get_user_records(
    user_id: str,
    query: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(_DEFAULT_RECORDS, ge=1, le=_MAX_RECORDS),
    store: Optional[SqliteBM25Memory] = Depends(get_memory_store),
) -> dict:
    if store is None:
        return {"enabled": False, "records": []}
    types = [type] if type else None
    if query:
        records = store.search(user_id, query, types=types, top_k=limit, max_candidates=_MAX_SEARCH_SCAN)
    else:
        records = store.all(user_id, types=types, limit=limit)
    return {"enabled": True, "records": [dataclasses.asdict(r) for r in records]}


@router.delete("/records/{memory_id}", status_code=204)
def delete_record(
    memory_id: str,
    store: Optional[SqliteBM25Memory] = Depends(get_memory_store),
) -> Response:
    _require_store(store).delete(memory_id)
    return Response(status_code=204)


@router.delete("/users/{user_id}")
def clear_user_memory(
    user_id: str,
    store: Optional[SqliteBM25Memory] = Depends(get_memory_store),
) -> dict:
    removed = _require_store(store).delete_user(user_id)
    return {"removed": removed}


@router.delete("/orgs/{org_id}")
def clear_org_memory(
    org_id: int,
    db: Session = Depends(get_db),
    store: Optional[SqliteBM25Memory] = Depends(get_memory_store),
) -> dict:
    """Erase all memory belonging to one organization (SP-2 compliance erasure).

    Deletes org-scoped rows (`org_id = :org`) AND legacy pre-SP-2 rows (`org_id
    NULL`) for the org's current members -- those usernames are resolved from the
    main DB, since the memory store has no org_id on legacy rows (review #1). A
    legacy row whose username no longer exists (the member was deleted) can't be
    attributed to an org and is out of scope here -- that belongs to account
    deletion (deletion-lifecycle sub-project, review #2).
    """
    store = _require_store(store)
    removed = store.delete_org(org_id)
    # Mop up this org's members' legacy NULL-org rows. Must delete ONLY NULL-org
    # rows for those usernames -- an unscoped delete_user would also destroy the
    # same username's rows under other concrete orgs (a moved user, or a former
    # same-named principal's history), i.e. cross-org data loss.
    usernames = [u for (u,) in db.query(User.username).filter(User.org_id == org_id)]
    removed += store.delete_legacy_for_users(usernames)
    return {"removed": removed}
