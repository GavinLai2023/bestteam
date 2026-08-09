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

Unlike the admin route (a trusted operator, no caller-count limit), this one
is reachable by any org member, so it applies three extra guards the admin
path doesn't need (Codex review findings): a lower per-upload size ceiling, a
per-org cap on how many distinct self-service knowledge bases can exist --
this route has no delete counterpart, so without a cap an org could
accumulate an unbounded number of them -- and a confirmation gate on reusing
an existing name: the shared `upload_knowledge_base()` treats a same-name
upload as a full in-place replace (by design, for the admin's own
deliberate re-index workflow), but a customer typing a common label (e.g.
"policies") in a later wizard session has no way to know that name is
already live under a different, already-deployed team -- silently replacing
its documents would change that team's answers with no warning. A first
attempt at an existing name 409s with the file it would replace; the wizard
then re-submits with `replace=true` once the customer confirms.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .auth_api import get_current_org
from .db.models import KnowledgeBaseRecord, Organization
from .db_session import get_db
from .knowledge_bases import upload_knowledge_base

router = APIRouter(prefix="/api/org", tags=["org-knowledge-bases"])

# Self-service is deliberately tighter than the admin upload path's 30
# files / 30MB-per-file / 500MB-total: a member can retry a large upload
# repeatedly, so per-request limits alone don't bound memory use the way they
# do for a small, trusted admin population.
_MAX_FILES_PER_UPLOAD = 10
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
_MAX_TOTAL_SIZE_BYTES = 50 * 1024 * 1024  # 50MB

# Cap on distinct self-service KB names per org. Re-uploading an existing
# name (replacing its content) never counts against this -- only creating a
# new name does.
_MAX_SELF_SERVICE_KBS_PER_ORG = 20


@router.post("/knowledge-bases/{item_name}/upload")
def upload_own_knowledge_base(
    item_name: str,
    files: list[UploadFile] = File(...),
    replace: bool = Form(False),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    existing = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org.id).one_or_none()
    if existing is None:
        count = db.query(KnowledgeBaseRecord).filter_by(org_id=org.id).count()
        if count >= _MAX_SELF_SERVICE_KBS_PER_ORG:
            # 403, not 409 -- this isn't resolvable by confirming a replace
            # (the frontend's 409 handler offers exactly that), it needs an
            # admin to free up a slot.
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Your organisation has reached the limit of "
                    f"{_MAX_SELF_SERVICE_KBS_PER_ORG} document collections. "
                    "Ask an administrator to remove one before adding another."
                ),
            )
    elif not replace:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{item_name}' already exists and may be used by another team. "
                "Choose a different name, or confirm to replace its documents."
            ),
        )
    return upload_knowledge_base(
        db,
        org.id,
        item_name,
        files,
        max_files=_MAX_FILES_PER_UPLOAD,
        max_file_size_bytes=_MAX_FILE_SIZE_BYTES,
        max_total_size_bytes=_MAX_TOTAL_SIZE_BYTES,
    )
