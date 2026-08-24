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
upload is reachable by any org member, so it applies four extra guards the
admin path doesn't need (Codex review findings): a lower per-upload size
ceiling, a per-org cap on how many distinct self-service knowledge bases can
exist (20 -- an org can now free a slot itself with the delete route, but the
cap still bounds how many one org holds at once), a lower cap on how many
documents one collection may hold once an `add` upload is merged in, and a
confirmation gate on reusing an existing name: a customer typing a common
label (e.g. "policies") in a later wizard session has no way to know that
name is already live under a different, already-deployed team -- silently
changing its documents would change that team's answers with no warning. A
first attempt at an existing name 409s naming what is there; the wizard then
re-submits with `mode=add` or `mode=replace` once the customer has chosen.

`mode` is one field rather than a boolean plus a mode because there are three
states -- unconfirmed, add, replace -- and a boolean carries two.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from bestteam.core.knowledge_base import _citation
from bestteam.core.tool_context import tool_call_context

from .auth_api import get_current_org, get_current_user
from .db import model_catalog
from .db.dependencies import pipelines_referencing
from .db.models import IngestionJob, KnowledgeBaseRecord, Organization, User, iso_utc
from .db.usage import record_usage
from .db_session import get_db
from .ingestion import job_status_payload
from .knowledge_bases import (
    KnowledgeBaseNotReady,
    _kb_upload_lock,
    live_documents,
    remove_knowledge_base_document,
    delete_knowledge_base,
    resolve_knowledge_base,
    upload_knowledge_base,
)

_logger = logging.getLogger(__name__)

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

# How many documents one collection may hold once an `add` upload is merged
# with the generation it extends. Tighter than the admin default for the
# same reason every other limit here is: this route is reachable by any org
# member. Three per-upload files at a time is fine; thirty documents in one
# collection is past what a single collection should be answering from.
_MAX_DOCUMENTS_PER_KB = 30

# How much of a retrieved passage the "Try a search" panel gets back. Enough
# to judge whether the right thing was retrieved; not a document reader, and
# not a way to page a whole collection out through a search box.
_MAX_RESULT_TEXT_CHARS = 1500


def _latest_completed_job(db: Session, record: KnowledgeBaseRecord) -> Optional[IngestionJob]:
    """The ingestion job a search against this knowledge base is served from.

    Same "highest `id` wins" ordering as `resolve_knowledge_base` -- `id` is
    the only column guaranteed monotonic with submission order.
    """
    return (
        db.query(IngestionJob)
        .filter_by(kb_id=record.id, status="completed")
        .order_by(IngestionJob.id.desc())
        .first()
    )


def _live_kb_type(db: Session, record: KnowledgeBaseRecord) -> str:
    """The knowledge base's *serving* type, which `config` may not be.

    `config` advances to the new spec the moment a re-upload is dispatched,
    and stays there for good if that job fails -- so it answers "what would
    the next upload build?", not "what can be searched today?". The serving
    generation's own `kb_type` answers the latter; a knowledge base with no
    completed job has nothing else to report, so it falls back to `config`.
    """
    live = _latest_completed_job(db, record)
    if live is not None and live.kb_type:
        return live.kb_type
    return (record.config or {}).get("type", "local_folder")


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
    live = _latest_completed_job(db, record)
    config = record.config or {}
    return {
        "name": record.name,
        # The one sentence the uploader wrote about these documents. Null for
        # a knowledge base created before the field existed, or by a caller
        # that omitted it.
        "description": config.get("description"),
        # The shape that actually answers a search today, not the one the
        # next upload would build (see `_live_kb_type`).
        "type": _live_kb_type(db, record),
        # `iso_utc`, not the bare column: SQLite hands it back tz-naive, and
        # the panel's `Date` would then read a UTC timestamp as local time.
        "updated_at": iso_utc(record.updated_at),
        "used_by": pipelines_referencing(db, kind="knowledge_base", resource_id=record.id),
        "servable": live is not None or latest is None,
        "latest_job": latest_job,
        # The live generation's documents, by name, each with the status the
        # ingester gave it -- what the panel lists, and what a customer can
        # remove one at a time. Empty until a first upload completes.
        "documents": [
            {"filename": doc.filename, "status": doc.status, "size_bytes": doc.size_bytes}
            for doc in live_documents(db, record)
        ],
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
    # "" is an unconfirmed upload -- the 409 below is what turns it into one
    # of the other two. One field rather than a boolean plus a mode, because
    # there are three states and a boolean can only carry two: `replace=true,
    # mode=add` would be a contradiction the server had to pick a winner for.
    mode: str = Form(""),
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
        elif not mode:
            # Name the shape that is live today: this refusal is the one
            # moment the wizard can tell the customer what they are about to
            # replace, and its own confirmation dialog adds what it would
            # become -- so cancelling and flipping the toggle is an informed
            # choice rather than a silent up/downgrade plus a full re-index.
            quality = "Enhanced" if _live_kb_type(db, existing) == "hybrid" else "Standard"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{item_name}' already exists and may be used by another team. "
                    f"It currently uses {quality} search. "
                    "Choose a different name, add these documents to it, or "
                    "replace what is in it."
                ),
            )
        else:
            # Refuse a confirmed upload while a previous one for this
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
            max_documents=_MAX_DOCUMENTS_PER_KB,
            # An unconfirmed upload only ever reaches here for a name that
            # does not exist yet, where the two modes do the same thing.
            mode=mode or "replace",
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


class KnowledgeBaseSearchRequest(BaseModel):
    """One test query against a collection the caller's org owns.

    Both bounds are the customer's, not the model's: a query longer than a
    sentence or two tells retrieval nothing useful, and `top_k` is capped
    well below any real appetite because each result costs a slice of the
    response and, for a `vector`/`hybrid` collection, the whole call costs a
    query embedding.
    """

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, v: str) -> str:
        # `min_length` counts characters, so `"   "` clears it. Retrieval has
        # nothing to match on -- but a `vector`/`hybrid` collection would still
        # pay for the query embedding first.
        if not v.strip():
            raise ValueError("query must not be blank")
        return v


def _safe_record_search_usage(
    db: Session, usage: List[Dict[str, Any]], org_id: Optional[int]
) -> None:
    """Meter what one test search spent, as `agent="kb:search"` rows.

    A search is billable on two counts -- the query embedding for a
    `vector`/`hybrid` collection, and a query-expansion LLM call for any of
    the three types -- and the knowledge base reports both through
    `core/tool_context.py`, exactly as it does inside a run. This is the same
    ledger, so the org's monthly spend cap (`db/email_budget_settings.py`, a
    `SUM(cost_estimate) WHERE org_id`) counts a test search without a second
    query.

    These are the third kind of row in `usage_records` and the only one with
    **both** foreign keys NULL: a test search belongs to no run and to no
    ingestion job. Nothing billable means nothing recorded -- a
    `local_folder` collection with no expansion model reports no usage at all.

    Best-effort, exactly like `ingestion._safe_record_ingestion_usage`: the
    search has already run and its answer is what the customer asked for, so
    a metering failure must never be able to turn it into an error.
    """
    for entry in usage:
        try:
            record_usage(
                db,
                run_id=None,
                ingestion_job_id=None,
                agent="kb:search",
                model=entry.get("model"),
                input_tokens=entry.get("input_tokens", 0),
                output_tokens=entry.get("output_tokens", 0),
                org_id=org_id,
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Could not meter a knowledge-base test search for org %s; the "
                "search itself is unaffected", org_id, exc_info=True,
            )
            db.rollback()


@router.post("/knowledge-bases/{item_name}/search")
def search_own_knowledge_base(
    item_name: str,
    req: KnowledgeBaseSearchRequest,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
) -> Dict[str, Any]:
    """Run one query against the org's own collection and return up to
    `top_k` of the passages an agent would rank first, each with the citation
    the agent sees. `top_k` is the one divergence from what an agent's own
    tool call sees: the panel sends 5 whatever the collection is configured
    for. Everything else is the same retrieval -- this calls the very
    `kb.search()` the tool calls, so the collection's own query expansion and
    reranking run here too (which is why there is spend to meter below).

    Deliberately **uncached and unthrottled**. The money at stake is
    negligible -- one short query embedding, and metering *records* that, it
    does not bound it. The real cost is CPU: every call rebuilds the knowledge
    base from its Document/Chunk rows (a `hybrid` one also `json.loads`es
    every stored vector) on the sync threadpool, which is sub-second to a few
    seconds at the tens-to-hundreds-of-documents this beta is sized for, with
    a person clicking a button rather than an agent loop on the other end. A
    cache would need invalidating on every re-upload to buy correctness this
    does not yet need. Revisit both if the button is ever abused.
    """
    record = _own_kb_or_404(db, org.id, item_name)
    try:
        # No `source`: this route has no pipeline to resolve relative paths
        # against, and a legacy file-backed collection is refused rather than
        # rebuilt from disk on every click (see `resolve_knowledge_base`).
        kb = resolve_knowledge_base(db, record)
    except KnowledgeBaseNotReady as exc:
        # The customer's own conflict, and its message already says which one
        # it is. Every other `ConfigurationError` -- a missing optional extra,
        # a bad `rerank_model` -- is an operator's deployment problem that the
        # customer cannot act on, so it falls through to the app's generic
        # (logged) 500 rather than masquerading as "wait and try again".
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    with tool_call_context() as ctx:
        try:
            hits = kb.search_hits(req.query, req.top_k)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Test search against knowledge base '%s' failed", item_name, exc_info=True
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "The search could not be run: the search provider did not "
                    "respond. Try again in a moment."
                ),
            ) from exc
        finally:
            # On the failure path too: a `hybrid` collection pays for its
            # query expansion before the embedding call that raised, and that
            # money is spent either way -- the same rule the adapter's tool
            # loop follows when it drains `tool_ctx.usage`.
            _safe_record_search_usage(db, ctx.usage, org.id)

    # The same identity and scores the agent's trace event records (see
    # `make_knowledge_base_tool`), so a passage shown here can be tied to its
    # chunk row and the ingestion job that wrote it. Scores, not model names:
    # nothing here says which embedding or rerank model the collection uses.
    return {
        "query": req.query,
        "hit_count": len(hits),
        "ingestion_job_id": getattr(kb, "ingestion_job_id", None),
        "results": [
            {
                "citation": _citation(hit.chunk),
                "source": hit.chunk.source,
                "page": hit.chunk.page,
                "heading": hit.chunk.heading,
                "text": hit.chunk.text[:_MAX_RESULT_TEXT_CHARS],
                "chunk_id": hit.chunk.chunk_id,
                "document_id": hit.chunk.document_id,
                "fused_score": round(hit.fused_score, 4),
                "leg_scores": {name: round(score, 4) for name, score in hit.leg_scores.items()},
                "rerank_score": None if hit.rerank_score is None else round(hit.rerank_score, 4),
            }
            for hit in hits
        ],
    }


@router.delete("/knowledge-bases/{item_name}/documents/{filename}", status_code=202)
def remove_own_knowledge_base_document(
    item_name: str,
    filename: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Remove one document from the org's own collection. A `202` with the
    job to poll, like an upload: the collection keeps answering from its
    current documents until the new generation is ready. Allowed while teams
    use the collection -- an `add` is too, and the panel names them in its
    confirmation -- refused while an upload is still processing, for the last
    document (delete the collection instead), and for a name that is not
    there; see `knowledge_bases.remove_knowledge_base_document`."""
    return remove_knowledge_base_document(
        db, org.id, item_name, filename, created_by=user.username
    )


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
