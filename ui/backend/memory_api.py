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

from bestteam import SqliteBM25Memory
from bestteam.core.memory import EPISODIC, PROCEDURAL, SEMANTIC

from .auth_api import get_current_admin

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


@router.get("/users")
def list_users(store: Optional[SqliteBM25Memory] = Depends(get_memory_store)) -> dict:
    if store is None:
        return {"enabled": False, "users": []}
    users = []
    for user_id in store.user_ids():
        records = store.all(user_id)
        counts = {EPISODIC: 0, SEMANTIC: 0, PROCEDURAL: 0}
        for record in records:
            if record.type in counts:
                counts[record.type] += 1
        users.append(
            {
                "user_id": user_id,
                "episodic": counts[EPISODIC],
                "semantic": counts[SEMANTIC],
                "procedural": counts[PROCEDURAL],
                "total": len(records),
            }
        )
    return {"enabled": True, "users": users}


@router.get("/users/{user_id}/records")
def get_user_records(
    user_id: str,
    query: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    store: Optional[SqliteBM25Memory] = Depends(get_memory_store),
) -> dict:
    if store is None:
        return {"enabled": False, "records": []}
    types = [type] if type else None
    if query:
        records = store.search(user_id, query, types=types)
    else:
        records = store.all(user_id, types=types)
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
