"""Org-scoped self-service knowledge-base upload (`/api/org`).

Lets an org member (not just a platform admin) build their own knowledge base
by uploading documents, for use in the Team Builder wizard's "Your documents"
step. Mirrors `org_settings.py`'s pattern: every route is guarded by
`get_current_org`, so the org resolves from the caller's own bearer token
rather than an admin-supplied `?org=` query param.

The actual upload/chunk/index/version-swap work is shared with the admin
`/api/config/knowledge_bases/{name}/upload` route in `crud.py`, via
`knowledge_bases.upload_knowledge_base()`. Chunk-size/overlap/top_k aren't
exposed here -- this surface is deliberately simpler than the admin one,
fixed at the SDK's own defaults.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from .auth_api import get_current_org
from .db.models import Organization
from .db_session import get_db
from .knowledge_bases import upload_knowledge_base

router = APIRouter(prefix="/api/org", tags=["org-knowledge-bases"])


@router.post("/knowledge-bases/{item_name}/upload")
def upload_own_knowledge_base(
    item_name: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    return upload_knowledge_base(db, org.id, item_name, files)
