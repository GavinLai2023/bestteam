"""CRUD API for `agents`/`teams`/`knowledge_bases`/`workflows` (Phase 2).

The "advanced view" referenced by docs/team_builder_methodology.md's Phase 2:
a fine-tuning surface for already-deployed configs, reusing the same
`*Spec.to_raw()` / `_build_workflow()` validation as the wizard.

`agents`/`teams`/`knowledge_bases` are validated as standalone components
(field shape only, via `AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`) -- they
aren't cross-referenced into a `workflows` config automatically. A
`workflows` entry is a complete, self-contained `Specification.to_raw()`
dict and is validated as a whole via `_build_workflow`, exactly like the
wizard's Specification stage.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Type

from fastapi import APIRouter, Body, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from bestteam import AgentSpec, KnowledgeBaseSpec, SkillSpec, TeamSpec
from bestteam.core.knowledge_base import LocalFolderKnowledgeBase
from bestteam.core.loader import _build_workflow
from bestteam.exceptions import BestTeamError

from .auth_api import get_current_user
from .db.model_catalog import delete_entry, get_entry, list_entries, upsert_entry
from .db.models import AgentRecord, KnowledgeBaseRecord, SkillRecord, TeamRecord, WorkflowRecord
from .db_session import get_db
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
        data = f.file.read()
        if len(data) > _MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File '{f.filename}' exceeds the {_MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB per-file limit",
            )
        total_size += len(data)
        if total_size > _MAX_TOTAL_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Total upload size exceeds the {_MAX_TOTAL_SIZE_BYTES // (1024 * 1024)}MB limit",
            )
        contents[f.filename] = data

    upload_dir = _KB_UPLOADS_DIR / item_name
    upload_dir.mkdir(parents=True, exist_ok=True)
    try:
        for filename, data in contents.items():
            (upload_dir / filename).write_bytes(data)

        try:
            kb = LocalFolderKnowledgeBase(
                name=item_name,
                path=upload_dir,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                top_k=top_k,
            )
        except BestTeamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        spec = KnowledgeBaseSpec(
            name=item_name,
            path=str(upload_dir),
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
        db.commit()

        return {
            "name": item_name,
            "config": raw,
            "file_count": len(contents),
            "chunk_count": len(kb._chunks),
        }
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
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
        _build_workflow(raw, source=_WORKFLOWS_DIR / f"{item_name}.yaml", extra_tools={}, extra_skills=load_skills(db))
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
