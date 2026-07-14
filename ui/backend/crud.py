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

import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Type

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
from .knowledge_bases import (
    _KB_CURRENT_POINTER,
    check_path_traversal,
    checked_contained_cache_path,
    load_knowledge_base_tools,
)
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

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(get_current_user)])


def _read_pointer(pointer: Path) -> Optional[str]:
    try:
        return pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_pointer(pointer: Path, version: str) -> None:
    """Atomically point CURRENT at `version` (os.replace of a file is atomic on
    Windows + POSIX), so a concurrent reader always sees a complete version."""
    tmp = pointer.with_name(pointer.name + ".tmp")
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

    # Write the new files into a fresh version subdirectory, validate them
    # there, then flip the CURRENT pointer atomically. The pointer always names
    # a complete version, so a concurrent reader never sees the KB directory
    # without a live version (no rename-swap gap), and the prior version is kept
    # until the new one commits -- so any failure leaves the previous KB and its
    # DB record intact (CR-008).
    kb_root = _KB_UPLOADS_DIR / item_name
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
        item = db.query(KnowledgeBaseRecord).filter_by(name=item_name).one_or_none()
        if item is None:
            item = KnowledgeBaseRecord(name=item_name, config=raw)
            db.add(item)
        else:
            item.config = raw
        try:
            db.commit()
        except Exception:
            # Point CURRENT back at the prior version (still on disk); the failed
            # new version is removed by the outer handler. Keeps FS and DB
            # consistent, with no destructive delete of the previous KB.
            db.rollback()
            if previous_version is not None:
                _write_pointer(pointer, previous_version)
            else:
                pointer.unlink(missing_ok=True)
            raise

        _invalidate_workflow_cache()
        # Durable now: drop older versions but keep the immediately-previous one
        # as a grace window for readers that just resolved to it.
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
