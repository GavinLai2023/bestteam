"""CRUD API for `knowledge_bases`/`skills`/`workflows` + the model catalog.

The "advanced view" referenced by docs/team_builder_methodology.md's Phase 2:
an operator-facing fine-tuning surface for already-deployed configs, reusing
the same `*Spec.to_raw()` / `_build_workflow()` validation as the wizard.

A `workflows` entry is a complete, self-contained `Specification.to_raw()`
dict -- it carries its own `agents:` and `teams:` inline -- and is validated
as a whole via `_build_workflow`, exactly like the wizard's Specification
stage. `knowledge_bases` and `skills` are the only standalone records a
workflow resolves by name (via `knowledge_bases.py::load_knowledge_base_tools`
and `skills.py::load_skills`).

Standalone `agents`/`teams` CRUD used to live here too, but nothing ever
consumed those records: `_build_workflow` takes only `extra_tools`/
`extra_skills`, so they could never reach a running workflow. Both tables
were empty in every deployment, so the routes were removed; the models
remain in `db/models.py`.
"""

from __future__ import annotations

import inspect
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Type

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from bestteam import KnowledgeBaseSpec, SkillSpec
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase
from bestteam.core.loader import _build_workflow
from bestteam.core.specification import _validate_tool_name
from bestteam.exceptions import BestTeamError
from bestteam.tools import REGISTRY

from .auth_api import get_current_admin, get_current_user
from .db.model_catalog import delete_entry, get_entry, list_entries, upsert_entry
from .deploy_validation import validate_agent_models
from .db.models import (
    KnowledgeBaseRecord,
    Organization,
    SkillRecord,
    WorkflowRecord,
)
from .db.orgs import get_org_by_name, list_orgs
from .email_tools import load_email_tools
from .db_session import get_db
from .knowledge_bases import (
    _KB_CURRENT_POINTER,
    check_path_traversal,
    checked_contained_cache_path,
    kb_name_collisions,
    load_knowledge_base_tools,
)
from .skills import DEFAULT_SKILLS, load_skills

_logger = logging.getLogger(__name__)

# Seeded platform built-in skills can't be deleted -- bundled YAML workflows may
# depend on them (F4). Names only; the check applies to the platform tier.
_BUILTIN_SKILL_NAMES = frozenset(s.name for s in DEFAULT_SKILLS)

# Used as `_build_workflow`'s `source` for relative knowledge-base paths and
# the default workflow name -- same directory as the YAML demo workflows.
_WORKFLOWS_DIR = Path(__file__).parent / "workflows"

# Files uploaded via the knowledge-base upload endpoint live here, one
# subdirectory per KB -- a directory this backend owns (unlike the manual
# JSON config path, which points at a folder the user manages themselves).
_KB_UPLOADS_DIR = Path(__file__).parent / "data" / "knowledge_base_uploads"
_MAX_FILES_PER_UPLOAD = 30
_MAX_FILE_SIZE_BYTES = 30 * 1024 * 1024  # 30MB
_MAX_TOTAL_SIZE_BYTES = 500 * 1024 * 1024  # ~500MB

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(get_current_admin)])


@router.get("/orgs")
def list_organizations(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
    """Organizations, for the Advanced page's org selector.

    Read-only on purpose: orgs are provisioned out-of-band by the operator
    CLI (`python -m ui.backend.admin create-org`), so this adds no
    self-service provisioning surface -- it only lets an admin target an
    existing org on the `?org=`-scoped item routes below.
    """
    return [{"name": org.name, "display_name": org.display_name} for org in list_orgs(db)]


@router.get("/tools")
def list_tools() -> list[Dict[str, Any]]:
    """The built-in tools an agent or skill can reference by name.

    Read-only: tools are Python functions in `bestteam.tools.REGISTRY`, not
    config rows. Their docstrings are already written as the LLM-facing tool
    description (that's what the loader passes to the model), so they serve
    as the reference text here too.
    """
    return [
        {"name": name, "description": inspect.getdoc(fn) or ""}
        for name, fn in sorted(REGISTRY.items())
    ]


# Per-KB locks serialising the upload promotion/commit/cleanup critical section.
# Concurrent uploads of the same KB would otherwise interleave the shared CURRENT
# pointer + version cleanup and could leave CURRENT naming a version the losing
# uploader then deletes (CR-008). Keyed by KB name; a small guard lock protects
# the registry itself.
_kb_upload_locks_guard = threading.Lock()
_kb_upload_locks: Dict[str, threading.Lock] = {}


def _kb_upload_lock(name: str) -> threading.Lock:
    with _kb_upload_locks_guard:
        lock = _kb_upload_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _kb_upload_locks[name] = lock
        return lock


def _read_pointer(pointer: Path) -> Optional[str]:
    try:
        return pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_pointer(pointer: Path, version: str) -> None:
    """Atomically point CURRENT at `version` (os.replace of a file is atomic on
    Windows + POSIX), so a concurrent reader always sees a complete version. The
    temp file is uniquely named so a leftover from a crashed write can't collide
    with a later one."""
    tmp = pointer.with_name(f"{pointer.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(version, encoding="utf-8")
    os.replace(tmp, pointer)


def _cleanup_kb_versions(kb_root: Path, keep_versions: set[str]) -> None:
    """Remove every version dir / stray file in `kb_root` except CURRENT and the
    kept versions. The immediately-previous version is kept as a grace window so
    a reader that just resolved to it still finds it (CR-008)."""
    for child in kb_root.iterdir():
        if child.name == _KB_CURRENT_POINTER or child.name in keep_versions:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _validate_kb_paths(kb_config: Dict[str, Any]) -> None:
    """Constrain caller-supplied KB paths to application-owned roots (CR-001).

    The `/api/config/knowledge_bases` and inline-`knowledge_bases` API
    boundaries accept caller-supplied `path`/`cache_path` strings. An absolute
    or `..`-traversing `cache_path` is a server-file *write* primitive (the
    vector KB's `_save_embedding_cache()` does `os.replace(tmp, cache_path)`).
    Reject `..`/absolute with a clear 400, then rewrite `cache_path` in place to
    an app-owned `_kb_cache/<filename>` -- so even a clean relative or Windows
    rooted-relative value can only ever write inside that subdir, never over a
    workflow YAML or outside the app roots. Absolute `local_folder` `path`s stay
    allowed (the documented "point at a folder you manage yourself" feature).
    """
    path = kb_config.get("path")
    if isinstance(path, str):
        check_path_traversal(path)
    cache_path = kb_config.get("cache_path")
    if isinstance(cache_path, str):
        kb_config["cache_path"] = checked_contained_cache_path(cache_path)


def _invalidate_workflow_cache() -> None:
    """Drop every cached Workflow after a KB/skill mutation.

    A cached Workflow may embed a knowledge-base tool or skill by value. The
    global `max(updated_at)` freshness key in `main._get_workflow` misses a
    *delete* (removing a non-latest record leaves the maximum unchanged), so a
    cached workflow could keep serving a deleted KB's documents (CR-005).
    Clearing the cache on every KB/skill create/update/delete is the simple,
    correct invalidation. Bumping the generation under the cache lock makes a
    concurrent `_get_workflow` that started before this call skip caching its
    now-stale result instead of repopulating the cache (CR-005). Imported
    lazily to avoid a crud<->main import cycle.
    """
    from . import main

    with main._workflow_cache_lock:
        main._workflow_cache.clear()
        main._workflow_cache_generation += 1


def _reject_builtin_kb_name(name: str) -> None:
    """Refuse a knowledge-base name that shadows a built-in tool (F1).

    All tools resolve through one flat name lookup, so a KB named after a
    built-in silently replaces it at load. Blocking the name at creation is the
    source fix -- a colliding KB can then never exist.
    """
    if name in REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A knowledge base can't reuse a built-in tool name: '{name}'. "
                "Choose a different name."
            ),
        )


def _deployed_workflows_referencing(db: Session, org_id, kind: str, name: str) -> list[str]:
    """Names of deployed workflows whose config references `name`.

    `kind="skill"` matches an agent's `skills`; `kind="knowledge_base"` matches an
    agent's `tools` (a standalone KB is referenced by name there). Malformed rows
    (non-dict config, non-list agents/tools) are skipped rather than crashing the
    scan (F6) -- such a workflow can't build anyway.
    """
    field = "skills" if kind == "skill" else "tools"
    query = db.query(WorkflowRecord).filter(WorkflowRecord.status == "deployed")
    if org_id is not None:
        query = query.filter(WorkflowRecord.org_id == org_id)
    hits = []
    for row in query:
        config = row.config if isinstance(row.config, dict) else {}
        agents = config.get("agents", [])
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            refs = agent.get(field)
            if isinstance(refs, list) and name in refs:
                hits.append(row.name)
                break
    return sorted(hits)


def _resolve_org_id(db: Session, org: Optional[str], *, allow_platform: bool) -> Optional[int]:
    """Resolve an admin request's `?org=<name>` to an org id.

    Omitted `org` means the platform tier (org_id NULL) where allowed
    (skills' built-ins); otherwise it's an error -- admin mutations must
    target an explicit org so a multi-org operator can't edit the wrong
    customer by accident.
    """
    if org is None or org == "":
        if allow_platform:
            return None
        raise HTTPException(
            status_code=422,
            detail="Query parameter 'org' is required (the organization the item belongs to)",
        )
    record = get_org_by_name(db, org)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown organization '{org}'")
    return record.id


def _org_name_map(db: Session) -> Dict[int, str]:
    return {o.id: o.name for o in db.query(Organization).all()}


def _make_component_router(name: str, record_cls: Type, spec_cls: Type[BaseModel]) -> APIRouter:
    sub = APIRouter(prefix=f"/{name}")
    # Skills have a platform tier (org_id NULL = built-ins visible to every
    # org), so `?org=` may be omitted there; everything else requires it.
    allow_platform = name == "skills"

    @sub.get("")
    def list_items(
        org: Optional[str] = Query(None), db: Session = Depends(get_db)
    ) -> list[Dict[str, Any]]:
        query = db.query(record_cls)
        if org is not None:
            query = query.filter(record_cls.org_id == _resolve_org_id(db, org, allow_platform=False))
        items = query.order_by(record_cls.name).all()
        org_names = _org_name_map(db)
        return [
            {"name": item.name, "org": org_names.get(item.org_id), "config": item.config}
            for item in items
        ]

    @sub.get("/{item_name}")
    def get_item(
        item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        org_id = _resolve_org_id(db, org, allow_platform=allow_platform)
        item = db.query(record_cls).filter_by(name=item_name, org_id=org_id).one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown {name[:-1]} '{item_name}'")
        return {"name": item.name, "org": org, "config": item.config}

    @sub.put("/{item_name}")
    def upsert_item(
        item_name: str,
        config: Dict[str, Any] = Body(...),
        org: Optional[str] = Query(None),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        org_id = _resolve_org_id(db, org, allow_platform=allow_platform)
        if name == "knowledge_bases":
            _validate_kb_paths(config)
            _reject_builtin_kb_name(item_name)
        try:
            spec = spec_cls.model_validate({**config, "name": item_name})
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw = spec.to_raw()
        item = db.query(record_cls).filter_by(name=item_name, org_id=org_id).one_or_none()
        if item is None:
            item = record_cls(name=item_name, config=raw, org_id=org_id)
            db.add(item)
        else:
            item.config = raw
        db.commit()
        if name in ("knowledge_bases", "skills"):
            _invalidate_workflow_cache()
        return {"name": item_name, "org": org, "config": raw}

    @sub.delete("/{item_name}", status_code=204)
    def delete_item(
        item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
    ) -> Response:
        org_id = _resolve_org_id(db, org, allow_platform=allow_platform)
        item = db.query(record_cls).filter_by(name=item_name, org_id=org_id).one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown {name[:-1]} '{item_name}'")
        # A seeded platform built-in skill can't be deleted -- bundled YAML
        # workflows may depend on it (F4). Platform tier only (org_id None).
        if name == "skills" and org_id is None and item_name in _BUILTIN_SKILL_NAMES:
            raise HTTPException(
                status_code=409,
                detail=f"'{item_name}' is a built-in skill and can't be deleted.",
            )
        if name in ("skills", "knowledge_bases"):
            kind = "skill" if name == "skills" else "knowledge_base"
            used_by = _deployed_workflows_referencing(db, org_id, kind, item_name)
            if used_by:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Can't delete '{item_name}': it's used by deployed team(s): "
                        + ", ".join(used_by)
                        + ". Update or remove those teams first."
                    ),
                )
        db.delete(item)
        db.commit()
        if name == "knowledge_bases":
            # Remove uploads only AFTER the row is committed (F3): a commit
            # failure then leaves the files intact for the still-present record,
            # rather than deleting files under a rolled-back row. Hold the per-KB
            # lock so this can't race a re-upload/promotion. A failed rmtree is
            # logged (the record is already gone -- at worst orphaned files an
            # operator can clean up), not silently swallowed.
            with _kb_upload_lock(f"{org_id}/{item_name}"):
                upload_dir = _KB_UPLOADS_DIR / str(org_id) / item_name
                if upload_dir.is_dir():
                    try:
                        shutil.rmtree(upload_dir)
                    except OSError as exc:
                        _logger.warning(
                            "Knowledge base '%s' (org %s) deleted, but its upload "
                            "directory couldn't be removed: %s",
                            item_name, org_id, exc,
                        )
        if name in ("knowledge_bases", "skills"):
            _invalidate_workflow_cache()
        return Response(status_code=204)

    return sub


router.include_router(_make_component_router("knowledge_bases", KnowledgeBaseRecord, KnowledgeBaseSpec))
router.include_router(_make_component_router("skills", SkillRecord, SkillSpec))


@router.post("/knowledge_bases/{item_name}/upload")
def upload_knowledge_base_files(
    item_name: str,
    files: list[UploadFile] = File(...),
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    top_k: int = 5,
    org: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    try:
        _validate_tool_name(item_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _reject_builtin_kb_name(item_name)

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > _MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files ({len(files)}); max {_MAX_FILES_PER_UPLOAD} per upload",
        )

    # Read all file contents up front to enforce size limits before writing anything to disk.
    contents: Dict[str, bytes] = {}
    total_size = 0
    for f in files:
        # f.filename comes from the client-controlled Content-Disposition
        # header -- strip it to a bare filename so it can't escape upload_dir
        # via "../" segments or an absolute path.
        filename = Path(f.filename or "").name
        if filename in ("", ".", ".."):
            raise HTTPException(status_code=400, detail=f"Invalid filename: '{f.filename}'")

        data = f.file.read()
        if len(data) > _MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File '{filename}' exceeds the {_MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB per-file limit",
            )
        total_size += len(data)
        if total_size > _MAX_TOTAL_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Total upload size exceeds the {_MAX_TOTAL_SIZE_BYTES // (1024 * 1024)}MB limit",
            )
        contents[filename] = data

    # Write the new files into a fresh version subdirectory, validate them
    # there, then flip the CURRENT pointer atomically. The pointer always names
    # a complete version, so a concurrent reader never sees the KB directory
    # without a live version (no rename-swap gap), and the prior version is kept
    # until the new one commits -- so any failure leaves the previous KB and its
    # DB record intact (CR-008).
    # Uploads are org-scoped on disk so two orgs' same-named KBs can't share
    # (or clobber) a directory. Legacy pre-multi-tenancy uploads at
    # `_KB_UPLOADS_DIR/<name>` keep working: KB configs embed the absolute
    # root, so existing records still resolve their old directory.
    kb_root = _KB_UPLOADS_DIR / str(org_id) / item_name
    pointer = kb_root / _KB_CURRENT_POINTER
    version = f"v_{uuid.uuid4().hex[:12]}"
    version_dir = kb_root / version
    version_dir.mkdir(parents=True, exist_ok=True)
    try:
        for filename, data in contents.items():
            (version_dir / filename).write_bytes(data)

        try:
            kb = LocalFolderKnowledgeBase(
                name=item_name,
                path=version_dir,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                top_k=top_k,
            )
        except BestTeamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Serialise the pointer flip + commit + cleanup per KB so concurrent
        # uploads of the same KB can't interleave and leave CURRENT dangling.
        # (The file writes + validation above run outside the lock -- each
        # uploader owns a unique version dir, so they don't conflict.)
        with _kb_upload_lock(f"{org_id}/{item_name}"):
            previous_version = _read_pointer(pointer)
            _write_pointer(pointer, version)  # atomic promotion

            spec = KnowledgeBaseSpec(
                name=item_name,
                path=str(kb_root),
                type="local_folder",
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                top_k=top_k,
            )
            raw = spec.to_raw()
            item = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
            if item is None:
                item = KnowledgeBaseRecord(name=item_name, config=raw, org_id=org_id)
                db.add(item)
            else:
                item.config = raw
            try:
                db.commit()
            except Exception:
                # Point CURRENT back at the prior version (still on disk); the
                # failed new version is removed by the outer handler. Keeps FS
                # and DB consistent, with no destructive delete of the prior KB.
                db.rollback()
                if previous_version is not None:
                    _write_pointer(pointer, previous_version)
                else:
                    pointer.unlink(missing_ok=True)
                raise

            _invalidate_workflow_cache()
            # Durable now: drop older versions but keep the immediately-previous
            # one as a grace window for readers that just resolved to it.
            _cleanup_kb_versions(kb_root, {version, previous_version} - {None})

        return {
            "name": item_name,
            "config": raw,
            "file_count": len(contents),
            "chunk_count": len(kb._chunks),
        }
    except Exception:
        # CURRENT still names the prior version (or is absent on a first upload),
        # so removing only the uncommitted new version preserves the prior KB.
        shutil.rmtree(version_dir, ignore_errors=True)
        raise


_workflows = APIRouter(prefix="/workflows")


@_workflows.get("")
def list_workflow_configs(
    org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> list[Dict[str, Any]]:
    query = db.query(WorkflowRecord)
    if org is not None:
        query = query.filter(WorkflowRecord.org_id == _resolve_org_id(db, org, allow_platform=False))
    items = query.order_by(WorkflowRecord.name).all()
    org_names = _org_name_map(db)
    return [
        {"name": item.name, "org": org_names.get(item.org_id), "status": item.status, "config": item.config}
        for item in items
    ]


@_workflows.get("/{item_name}")
def get_workflow_config(
    item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    item = db.query(WorkflowRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow '{item_name}'")
    return {"name": item.name, "org": org, "status": item.status, "config": item.config}


@_workflows.put("/{item_name}")
def upsert_workflow_config(
    item_name: str,
    config: Dict[str, Any] = Body(...),
    org: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    raw = {**config, "name": item_name}
    try:
        kb_collisions = kb_name_collisions(db, org_id, raw)
        if kb_collisions:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A knowledge base can't reuse a built-in tool name: "
                    + ", ".join(kb_collisions)
                    + ". Rename the knowledge base."
                ),
            )
        for kb_config in raw.get("knowledge_bases", []) or []:
            if isinstance(kb_config, dict):
                _validate_kb_paths(kb_config)
        source = _WORKFLOWS_DIR / f"{item_name}.yaml"
        # Dependencies resolve within the workflow's own org (+ built-in skills).
        extra_tools = {
            **load_knowledge_base_tools(db, raw, source, org_id=org_id),
            **(load_email_tools(db, org_id) if org_id is not None else {}),
        }
        _build_workflow(raw, source=source, extra_tools=extra_tools, extra_skills=load_skills(db, org_id))
    except (KeyError, TypeError, BestTeamError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    model_problems = validate_agent_models(raw, {e.spec for e in list_entries(db)})
    if model_problems:
        raise HTTPException(
            status_code=400,
            detail=(
                "This team can't be deployed: "
                + "; ".join(model_problems)
                + ". Pick a model from the catalog."
            ),
        )

    item = db.query(WorkflowRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        item = WorkflowRecord(name=item_name, config=raw, status="deployed", org_id=org_id)
        db.add(item)
    else:
        item.config = raw
        item.status = "deployed"
    db.commit()
    return {"name": item_name, "org": org, "status": item.status, "config": raw}


@_workflows.delete("/{item_name}", status_code=204)
def delete_workflow_config(
    item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> Response:
    org_id = _resolve_org_id(db, org, allow_platform=False)
    item = db.query(WorkflowRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow '{item_name}'")
    db.delete(item)
    db.commit()
    return Response(status_code=204)


router.include_router(_workflows)


class ModelCatalogEntrySpec(BaseModel):
    """Field shape for a `model_catalog` entry (Phase 3)."""

    display_name: str
    description: str = ""
    tier: str = "balanced"
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0


def _model_catalog_entry_to_dict(entry) -> Dict[str, Any]:
    return {
        "spec": entry.spec,
        "display_name": entry.display_name,
        "description": entry.description,
        "tier": entry.tier,
        "input_price_per_1k": entry.input_price_per_1k,
        "output_price_per_1k": entry.output_price_per_1k,
    }


_model_catalog = APIRouter(prefix="/model-catalog")


@_model_catalog.get("")
def list_model_catalog(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
    return [_model_catalog_entry_to_dict(entry) for entry in list_entries(db)]


@_model_catalog.get("/{spec:path}")
def get_model_catalog_entry(spec: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    entry = get_entry(db, spec)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown model catalog entry '{spec}'")
    return _model_catalog_entry_to_dict(entry)


@_model_catalog.put("/{spec:path}")
def upsert_model_catalog_entry(spec: str, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        item = ModelCatalogEntrySpec.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entry = upsert_entry(db, spec, **item.model_dump())
    return _model_catalog_entry_to_dict(entry)


@_model_catalog.delete("/{spec:path}", status_code=204)
def delete_model_catalog_entry(spec: str, db: Session = Depends(get_db)) -> Response:
    if not delete_entry(db, spec):
        raise HTTPException(status_code=404, detail=f"Unknown model catalog entry '{spec}'")
    return Response(status_code=204)


router.include_router(_model_catalog)


# Read-only model-catalog access for ANY authenticated user (not just admins).
# The Team Builder wizard runs as an org member and needs the catalog to pick a
# real model; without it the frontend falls back to a fake model and team
# generation fails. Kept separate from the admin-gated `router` above -- this
# exposes the list only, no CRUD.
public_router = APIRouter(prefix="/api", tags=["model-catalog"], dependencies=[Depends(get_current_user)])


@public_router.get("/model-catalog")
def list_model_catalog_public(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
    return [_model_catalog_entry_to_dict(entry) for entry in list_entries(db)]
