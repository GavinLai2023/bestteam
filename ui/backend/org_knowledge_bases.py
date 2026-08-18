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

The org can also list, inspect and delete its own knowledge bases here
(`GET /knowledge-bases`, `GET`/`DELETE /knowledge-bases/{name}`) -- the
self-service counterpart to the admin `/api/config/knowledge_bases` routes,
without an admin having to be involved.

Unlike the admin route (a trusted operator, no caller-count limit), the
upload is reachable by any org member, so it applies three extra guards the
admin path doesn't need (Codex review findings): a lower per-upload size
ceiling, a per-org cap on how many distinct self-service knowledge bases can
exist (20 -- an org can now free a slot itself with the delete route, but the
cap still bounds how many one org holds at once), and a confirmation gate on
reusing an existing name: the shared `upload_knowledge_base()` treats a same-name
upload as a full in-place replace (by design, for the admin's own
deliberate re-index workflow), but a customer typing a common label (e.g.
"policies") in a later wizard session has no way to know that name is
already live under a different, already-deployed team -- silently replacing
its documents would change that team's answers with no warning. A first
attempt at an existing name 409s with the file it would replace; the wizard
then re-submits with `replace=true` once the customer confirms.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .auth_api import get_current_org, get_current_user
from .db import model_catalog
from .db.dependencies import workflows_referencing
from .db.models import IngestionJob, KnowledgeBaseRecord, Organization, User, iso_utc
from .db_session import get_db
from .ingestion import job_status_payload
from .knowledge_bases import _kb_upload_lock, delete_knowledge_base, upload_knowledge_base

router = APIRouter(prefix="/api/org", tags=["org-knowledge-bases"])

# "Smart search" is the wizard's customer-facing name for `type: hybrid` +
# query expansion + (if configured) reranking -- see DocumentsPage.tsx. It's
# an operator opt-in, not a customer-facing model choice (the DocumentsPage
# audience is explicitly non-technical): unset -> the toggle never appears in
# the wizard at all. Same spec-string convention as the KB's own
# `embedding_model`/`rerank_model` (`"fake:<dim>"`/`"fake:"` for $0 tests, or
# a real provider/model string).
_ENV_DEFAULT_EMBEDDING_MODEL = "BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL"
_ENV_DEFAULT_RERANK_MODEL = "BESTTEAM_KB_DEFAULT_RERANK_MODEL"


def _default_chat_model(db: Session) -> Optional[str]:
    """The same "first non-fake catalog spec, else first entry" default the
    wizard's own `pickDefaultModel` (lib/models.ts) uses, resolved
    server-side so smart search's query-expansion model is never trusted
    from the client. `None` only if the catalog is empty."""
    entries = model_catalog.list_chat_entries(db)
    if not entries:
        return None
    non_fake = [e for e in entries if not e.spec.startswith("fake:")]
    return (non_fake[0] if non_fake else entries[0]).spec

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


def _kb_summary(db: Session, record: KnowledgeBaseRecord) -> Dict[str, Any]:
    """One knowledge base as the customer's own "My documents" panel sees it.

    `latest_job` is the newest ingestion attempt of any status (ordered by
    `id`, the same monotonic-with-submission ordering `resolve_knowledge_base`
    uses), so a failed upload's error reaches the person who made it -- with
    `config` stripped, because it carries the server's absolute upload path
    and this is a customer-facing surface. (The existing per-job route still
    returns it; that pre-existing leak is out of scope here.)

    `servable` is deliberately not "the latest job succeeded": a failed
    re-upload leaves the previous completed generation live, and a KB with no
    jobs at all is a legacy/manual-path one served from disk.
    """
    latest = (
        db.query(IngestionJob)
        .filter_by(kb_id=record.id)
        .order_by(IngestionJob.id.desc())
        .first()
    )
    latest_job: Optional[Dict[str, Any]] = None
    if latest is not None:
        latest_job = job_status_payload(db, latest)
        latest_job.pop("config", None)
    has_completed = (
        db.query(IngestionJob.id).filter_by(kb_id=record.id, status="completed").first() is not None
    )
    config = record.config or {}
    return {
        "name": record.name,
        # The one sentence the uploader wrote about these documents. Null for
        # a knowledge base created before the field existed, or by a caller
        # that omitted it.
        "description": config.get("description"),
        "type": config.get("type", "local_folder"),
        # `iso_utc`, not the bare column: SQLite hands it back tz-naive, and
        # the panel's `Date` would then read a UTC timestamp as local time.
        "updated_at": iso_utc(record.updated_at),
        "used_by": workflows_referencing(db, kind="knowledge_base", resource_id=record.id),
        "servable": has_completed or latest is None,
        "latest_job": latest_job,
    }


def _own_kb_or_404(db: Session, org_id: Optional[int], item_name: str) -> KnowledgeBaseRecord:
    record = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge base '{item_name}'")
    return record


@router.get("/knowledge-bases")
def list_own_knowledge_bases(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> list[Dict[str, Any]]:
    records = (
        db.query(KnowledgeBaseRecord)
        .filter_by(org_id=org.id)
        .order_by(KnowledgeBaseRecord.name)
        .all()
    )
    return [_kb_summary(db, record) for record in records]


@router.get("/knowledge-bases/capabilities")
def get_knowledge_base_capabilities(org: Organization = Depends(get_current_org)) -> Dict[str, bool]:
    """Whether the wizard's "smart search" toggle has anything to turn on.

    `org` is unused beyond the auth dependency -- capability is a deployment-
    wide operator setting (an env var), not per-org, but the route still
    requires a logged-in org member like every other route in this file.
    """
    return {"smart_search_available": bool(os.environ.get(_ENV_DEFAULT_EMBEDDING_MODEL))}


@router.post("/knowledge-bases/{item_name}/upload")
def upload_own_knowledge_base(
    item_name: str,
    files: list[UploadFile] = File(...),
    replace: bool = Form(False),
    smart_search: bool = Form(False),
    # Optional, and capped here rather than left to pydantic: it becomes the
    # agent tool's description, and a 422 naming the field beats a 500 from
    # `KnowledgeBaseSpec`'s own validation deep inside the upload.
    description: Optional[str] = Form(None, max_length=500),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    # Hold the same per-KB lock upload_knowledge_base() itself takes across
    # this existence/cap/replace-confirmation check too, not just inside it --
    # otherwise two concurrent first uploads of the same not-yet-existing name
    # can both observe `existing is None` before either takes the lock, and
    # the second then silently replaces the first's documents once it
    # acquires the lock, without ever getting the 409 this route exists to
    # enforce (Codex review finding). The lock is reentrant, so
    # upload_knowledge_base's own inner acquisition of the same key below
    # doesn't deadlock.
    with _kb_upload_lock(f"{org.id}/{item_name}"):
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
        else:
            # Refuse a `replace=true` upload while a previous one for this
            # same KB is still queued/running. Without this, a member can
            # repeatedly retry a large upload and pile up unbounded work on
            # ingestion.py's fixed-size executor -- each request already
            # staged up to _MAX_TOTAL_SIZE_BYTES to disk and queued an
            # embedding call before this check would catch it otherwise
            # (Codex review finding). Held inside the same per-KB lock as
            # the existence/cap checks above, so a concurrent retry can't
            # race past this the same way the first-upload race above was
            # closed.
            in_flight = (
                db.query(IngestionJob)
                .filter(IngestionJob.kb_id == existing.id, IngestionJob.status.in_(("queued", "running")))
                .first()
            )
            if in_flight is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{item_name}' is still processing a previous upload. "
                        "Wait for it to finish before uploading again."
                    ),
                )
        kb_type = "local_folder"
        embedding_model: Optional[str] = None
        rerank_model: Optional[str] = None
        query_expansion_model: Optional[str] = None
        if smart_search:
            # Fails soft to plain `local_folder` if the operator hasn't
            # configured a default embedding model -- a stale/tampered
            # client sending `smart_search=true` without the capability
            # actually being available never errors, it just does nothing.
            embedding_model = os.environ.get(_ENV_DEFAULT_EMBEDDING_MODEL) or None
            if embedding_model:
                kb_type = "hybrid"
                rerank_model = os.environ.get(_ENV_DEFAULT_RERANK_MODEL) or None
                query_expansion_model = _default_chat_model(db)

        return upload_knowledge_base(
            db,
            org.id,
            item_name,
            files,
            description=description or None,
            kb_type=kb_type,
            embedding_model=embedding_model,
            rerank_model=rerank_model,
            query_expansion_model=query_expansion_model,
            max_files=_MAX_FILES_PER_UPLOAD,
            max_file_size_bytes=_MAX_FILE_SIZE_BYTES,
            max_total_size_bytes=_MAX_TOTAL_SIZE_BYTES,
            created_by=user.username,
        )


@router.get("/knowledge-bases/{item_name}/ingestion-jobs/{job_id}")
def get_ingestion_job_status(
    item_name: str,
    job_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    kb = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org.id).one_or_none()
    if kb is None:
        raise HTTPException(status_code=404, detail=f"Unknown knowledge base '{item_name}'")
    job = db.query(IngestionJob).filter_by(id=job_id, kb_id=kb.id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown ingestion job")
    return job_status_payload(db, job)


@router.get("/knowledge-bases/{item_name}")
def get_own_knowledge_base(
    item_name: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    return _kb_summary(db, _own_kb_or_404(db, org.id, item_name))


@router.delete("/knowledge-bases/{item_name}", status_code=204)
def delete_own_knowledge_base(
    item_name: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> None:
    # `delete_knowledge_base` owns the whole sequence (dependency guard,
    # in-flight-ingestion guard, row/file removal, cache invalidation) and
    # takes both locks itself -- see its docstring for why it can't live in a
    # route. The only thing this route decides is whose org id it runs for.
    delete_knowledge_base(db, org.id, item_name)
