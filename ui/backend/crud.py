"""CRUD API for `agents`/`teams`/`knowledge_bases`/`workflows` (Phase 2).

The "advanced view" referenced by docs/team_builder_methodology.md's Phase 2:
a fine-tuning surface for already-deployed configs, reusing the same
`*Spec.to_raw()` / `_build_workflow()` validation as the wizard.

`agents`/`teams`/`knowledge_bases` are validated as standalone components
(field shape only, via `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`) -- of
these, only `knowledge_bases` are also resolvable by name from a workflow's
`tools:` list (via `ui/backend/knowledge_bases.py::load_knowledge_base_tools`);
`agents`/`teams`/`skills` are not cross-referenced into a `workflows` config
automatically. A `workflows` entry is a complete, self-contained
`Specification.to_raw()` dict and is validated as a whole via
`_build_workflow`, exactly like the wizard's Specification stage.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Type

from fastapi import APIRouter, Body, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from bestteam import AgentSpec, KnowledgeBaseSpec, SkillSpec, TeamSpec
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase
from bestteam.core.loader import _build_workflow
from bestteam.core.specification import _validate_tool_name
from bestteam.exceptions import BestTeamError

from .auth_api import get_current_user
from .db.model_catalog import delete_entry, get_entry, list_entries, upsert_entry
from .db.models import AgentRecord, KnowledgeBaseRecord, SkillRecord, TeamRecord, WorkflowRecord
from .db_session import get_db
from .knowledge_bases import load_knowledge_base_tools
from .skills import load_skills

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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(get_current_user)])


def _restore_backup(backup_dir: Path, live_dir: Path, name: str) -> None:
    """Best-effort restore of the prior KB after an aborted upload.

    If the restore itself fails, the backup directory is deliberately left in
    place -- it holds the last valid copy of the KB -- and the failure is logged
    loudly rather than silently discarded by later cleanup (CR-008)."""
    try:
        os.replace(backup_dir, live_dir)
    except OSError:
        logger.critical(
            "Knowledge base '%s': could not restore the previous version after an "
            "aborted upload. The last valid copy is preserved at %s -- restore it "
            "manually.",
            name,
            backup_dir,
        )


def _has_traversal(value: str) -> bool:
    # Check under both path flavors so the guard behaves the same on the Linux
    # server and a Windows dev box (e.g. "foo/../bar" vs "foo\\..\\bar").
    return ".." in PurePosixPath(value).parts or ".." in PureWindowsPath(value).parts


def _looks_absolute(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _validate_kb_paths(kb_config: Dict[str, Any]) -> None:
    """Reject KB filesystem paths that escape application-owned roots (CR-001).

    The `/api/config/knowledge_bases` and inline-`knowledge_bases` API
    boundaries accept caller-supplied `path`/`cache_path` strings. An absolute
    or `..`-traversing `cache_path` is a server-file *write* primitive: on the
    next run the vector KB's `_save_embedding_cache()` does
    `os.replace(tmp, cache_path)`, overwriting any file it names. Reject
    traversal on both fields and require `cache_path` to be app-relative so it
    can only resolve under the backend's own workflows directory. Absolute
    `local_folder` `path`s stay allowed (the documented "point at a folder you
    manage yourself" feature).
    """
    for field in ("path", "cache_path"):
        value = kb_config.get(field)
        if not isinstance(value, str):
            continue  # missing/malformed values are the spec validator's job
        if _has_traversal(value):
            raise HTTPException(
                status_code=400,
                detail=f"Knowledge base '{field}' must not contain '..' path segments",
            )
    cache_path = kb_config.get("cache_path")
    if isinstance(cache_path, str) and _looks_absolute(cache_path):
        raise HTTPException(
            status_code=400,
            detail="Knowledge base 'cache_path' must be a relative path "
            "(it is stored under an application-owned directory)",
        )


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


def _make_component_router(name: str, record_cls: Type, spec_cls: Type[BaseModel]) -> APIRouter:
    sub = APIRouter(prefix=f"/{name}")

    @sub.get("")
    def list_items(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
        items = db.query(record_cls).order_by(record_cls.name).all()
        return [{"name": item.name, "config": item.config} for item in items]

    @sub.get("/{item_name}")
    def get_item(item_name: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
        item = db.query(record_cls).filter_by(name=item_name).one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown {name[:-1]} '{item_name}'")
        return {"name": item.name, "config": item.config}

    @sub.put("/{item_name}")
    def upsert_item(item_name: str, config: Dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> Dict[str, Any]:
        if name == "knowledge_bases":
            _validate_kb_paths(config)
        try:
            spec = spec_cls.model_validate({**config, "name": item_name})
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw = spec.to_raw()
        item = db.query(record_cls).filter_by(name=item_name).one_or_none()
        if item is None:
            item = record_cls(name=item_name, config=raw)
            db.add(item)
        else:
            item.config = raw
        db.commit()
        if name in ("knowledge_bases", "skills"):
            _invalidate_workflow_cache()
        return {"name": item_name, "config": raw}

    @sub.delete("/{item_name}", status_code=204)
    def delete_item(item_name: str, db: Session = Depends(get_db)) -> Response:
        item = db.query(record_cls).filter_by(name=item_name).one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Unknown {name[:-1]} '{item_name}'")
        if name == "knowledge_bases":
            upload_dir = _KB_UPLOADS_DIR / item_name
            if upload_dir.is_dir():
                shutil.rmtree(upload_dir, ignore_errors=True)
        db.delete(item)
        db.commit()
        if name in ("knowledge_bases", "skills"):
            _invalidate_workflow_cache()
        return Response(status_code=204)

    return sub


router.include_router(_make_component_router("agents", AgentRecord, AgentSpec))
router.include_router(_make_component_router("teams", TeamRecord, TeamSpec))
router.include_router(_make_component_router("knowledge_bases", KnowledgeBaseRecord, KnowledgeBaseSpec))
router.include_router(_make_component_router("skills", SkillRecord, SkillSpec))


@router.post("/knowledge_bases/{item_name}/upload")
def upload_knowledge_base_files(
    item_name: str,
    files: list[UploadFile] = File(...),
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    top_k: int = 5,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        _validate_tool_name(item_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    # Stage the new files in a sibling directory and validate them there, then
    # atomically promote staging to the live directory only after validation
    # succeeds. This avoids merging new files on top of stale ones and, on any
    # failure, leaves the previously-valid KB and its DB record untouched --
    # unlike writing in place and rmtree-ing the live directory on error (CR-008).
    live_dir = _KB_UPLOADS_DIR / item_name
    token = uuid.uuid4().hex[:8]
    staging_dir = _KB_UPLOADS_DIR / f".staging_{item_name}_{token}"
    backup_dir = _KB_UPLOADS_DIR / f".backup_{item_name}_{token}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        for filename, data in contents.items():
            (staging_dir / filename).write_bytes(data)

        try:
            kb = LocalFolderKnowledgeBase(
                name=item_name,
                path=staging_dir,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                top_k=top_k,
            )
        except BestTeamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Promote staging -> live. Move the current live dir aside first so the
        # final rename targets a non-existent path (works on Windows + POSIX);
        # restore it if the promotion fails.
        had_live = live_dir.exists()
        if had_live:
            os.replace(live_dir, backup_dir)
        try:
            os.replace(staging_dir, live_dir)
        except Exception:
            if had_live:
                _restore_backup(backup_dir, live_dir, item_name)
            raise

        spec = KnowledgeBaseSpec(
            name=item_name,
            path=str(live_dir),
            type="local_folder",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            top_k=top_k,
        )
        raw = spec.to_raw()
        item = db.query(KnowledgeBaseRecord).filter_by(name=item_name).one_or_none()
        if item is None:
            item = KnowledgeBaseRecord(name=item_name, config=raw)
            db.add(item)
        else:
            item.config = raw
        # Commit the DB record while the backup still exists, so a commit
        # failure can roll the filesystem back to the prior KB -- keeping the
        # live directory and the DB record consistent (CR-008).
        try:
            db.commit()
        except Exception:
            db.rollback()
            shutil.rmtree(live_dir, ignore_errors=True)
            if had_live:
                _restore_backup(backup_dir, live_dir, item_name)
            raise
        # Commit succeeded: the new KB is durable, so the backup can go.
        if had_live:
            shutil.rmtree(backup_dir, ignore_errors=True)
        _invalidate_workflow_cache()

        return {
            "name": item_name,
            "config": raw,
            "file_count": len(contents),
            "chunk_count": len(kb._chunks),
        }
    finally:
        # Only the staging scratch dir is unconditionally safe to remove. The
        # backup is handled explicitly: dropped on success, restored on a
        # recoverable failure, and deliberately preserved if a restore failed
        # (it is then the last valid copy) -- so it must NOT be blindly removed
        # here (CR-008).
        shutil.rmtree(staging_dir, ignore_errors=True)


_workflows = APIRouter(prefix="/workflows")


@_workflows.get("")
def list_workflow_configs(db: Session = Depends(get_db)) -> list[Dict[str, Any]]:
    items = db.query(WorkflowRecord).order_by(WorkflowRecord.name).all()
    return [{"name": item.name, "status": item.status, "config": item.config} for item in items]


@_workflows.get("/{item_name}")
def get_workflow_config(item_name: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    item = db.query(WorkflowRecord).filter_by(name=item_name).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow '{item_name}'")
    return {"name": item.name, "status": item.status, "config": item.config}


@_workflows.put("/{item_name}")
def upsert_workflow_config(item_name: str, config: Dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> Dict[str, Any]:
    raw = {**config, "name": item_name}
    try:
        for kb_config in raw.get("knowledge_bases", []) or []:
            if isinstance(kb_config, dict):
                _validate_kb_paths(kb_config)
        source = _WORKFLOWS_DIR / f"{item_name}.yaml"
        kb_tools = load_knowledge_base_tools(db, raw, source)
        _build_workflow(raw, source=source, extra_tools=kb_tools, extra_skills=load_skills(db))
    except (KeyError, TypeError, BestTeamError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    item = db.query(WorkflowRecord).filter_by(name=item_name).one_or_none()
    if item is None:
        item = WorkflowRecord(name=item_name, config=raw, status="draft")
        db.add(item)
    else:
        item.config = raw
    db.commit()
    return {"name": item_name, "status": item.status, "config": raw}


@_workflows.delete("/{item_name}", status_code=204)
def delete_workflow_config(item_name: str, db: Session = Depends(get_db)) -> Response:
    item = db.query(WorkflowRecord).filter_by(name=item_name).one_or_none()
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
