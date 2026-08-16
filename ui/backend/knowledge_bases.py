"""Build extra_tools for workflow loading from standalone KnowledgeBaseRecords
(created via /api/config/knowledge_bases, manually or via file upload)."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from bestteam import KnowledgeBaseSpec
from bestteam.core.hybrid_knowledge_base import HybridKnowledgeBase
from bestteam.core.knowledge_base import (
    LocalFolderKnowledgeBase,
    _Chunk,
    _validate_chunk_params,
    make_knowledge_base_tool,
)
from bestteam.core.loader import _build_knowledge_base, _KNOWLEDGE_BASE_TYPES
from bestteam.core.specification import _validate_tool_name
from bestteam.core.vector_knowledge_base import VectorKnowledgeBase
from bestteam.exceptions import BestTeamError, ConfigurationError
from bestteam.tools import REGISTRY

from . import ingestion
from .db.models import IngestionJob, KnowledgeBaseRecord, KnowledgeChunk, KnowledgeDocument
from .deploy_validation import find_kb_tool_collisions

# --- KB path containment (CR-001) -------------------------------------------
# A KB `cache_path` is a server-file *write* target (the vector KB's
# `_save_embedding_cache` does `os.replace(tmp, cache_path)`). We keep the SDK
# loader permissive (CLI/YAML deployments legitimately point cache_path wherever
# they manage), and instead constrain every *backend* boundary + load path so a
# caller can never influence the write location beyond a filename: the cache is
# forced into an application-owned `_kb_cache/` subdirectory that holds no source
# files, so it can't clobber a workflow YAML or escape the app roots.

_KB_CACHE_DIRNAME = "_kb_cache"

# Uploaded KBs are stored as versioned subdirectories with an atomically-swapped
# `CURRENT` pointer file naming the active version, so a replacement never leaves
# the KB directory without a live version for a concurrent reader (CR-008).
_KB_CURRENT_POINTER = "CURRENT"

# Files uploaded via a knowledge-base upload endpoint (admin `/api/config/...`
# or the org self-service `/api/org/...`) live here, one subdirectory per KB --
# a directory this backend owns (unlike the manual JSON config path, which
# points at a folder the user manages themselves).
_KB_UPLOADS_DIR = Path(__file__).parent / "data" / "knowledge_base_uploads"
_MAX_FILES_PER_UPLOAD = 30
_MAX_FILE_SIZE_BYTES = 30 * 1024 * 1024  # 30MB
_MAX_TOTAL_SIZE_BYTES = 500 * 1024 * 1024  # ~500MB

# Per-KB locks serialising the upload promotion/commit/cleanup critical section
# (and delete, in crud.py). Concurrent uploads of the same KB would otherwise
# interleave the shared CURRENT pointer + version cleanup and could leave
# CURRENT naming a version the losing uploader then deletes (CR-008). Keyed by
# "<org_id>/<name>"; a small guard lock protects the registry itself.
# Reentrant (RLock): org_knowledge_bases.py's self-service route also holds
# this same per-KB lock across its own existence/cap/replace-confirmation
# check, then calls into upload_knowledge_base() below, which re-acquires it
# by the same key on the same thread -- a plain Lock would deadlock there.
_kb_upload_locks_guard = threading.Lock()
_kb_upload_locks: Dict[str, threading.RLock] = {}


def _kb_upload_lock(name: str) -> threading.RLock:
    with _kb_upload_locks_guard:
        lock = _kb_upload_locks.get(name)
        if lock is None:
            lock = threading.RLock()
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


def _cleanup_kb_versions(kb_root: Path, keep_versions: set) -> None:
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
    lazily to avoid a knowledge_bases<->main import cycle.
    """
    from . import main

    with main._workflow_cache_lock:
        main._workflow_cache.clear()
        main._workflow_cache_generation += 1


def upload_knowledge_base(
    db: Session,
    org_id: Optional[int],
    item_name: str,
    files: List[UploadFile],
    *,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    top_k: int = 5,
    kb_type: str = "local_folder",
    embedding_model: Optional[str] = None,
    rerank_model: Optional[str] = None,
    query_expansion_model: Optional[str] = None,
    max_files: int = _MAX_FILES_PER_UPLOAD,
    max_file_size_bytes: int = _MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = _MAX_TOTAL_SIZE_BYTES,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate uploaded documents and dispatch an async ingestion job.

    Shared by the admin `/api/config/knowledge_bases/{name}/upload` route
    (always `local_folder`, the historical default) and the org self-service
    `/api/org/knowledge-bases/{name}/upload` route -- the differences between
    them are how `org_id` gets resolved (an explicit `?org=` for the admin
    surface, the bearer token's own org for self-service), the tighter
    `max_*` limits `org_knowledge_bases.py` passes in, and, for the "smarter
    search" toggle described there, `kb_type`/`embedding_model`/
    `rerank_model`/`query_expansion_model`. `embedding_model` is required
    when `kb_type` is `vector` or `hybrid` and ignored otherwise.

    This validates synchronously (name/size limits, `kb_type`, chunk params),
    writes the uploaded files to a fresh version directory, upserts the
    `KnowledgeBaseRecord` with its final config, and creates a `queued`
    `IngestionJob` row -- then submits `ingestion.run_ingestion_job` to
    `ingestion._executor` and returns immediately. The actual parsing/
    chunking/embedding happens on a background thread; callers poll
    `GET .../ingestion-jobs/{job_id}` (Task 9) for completion. The returned
    dict is `{"name", "job_id", "status"}` -- no `config`/`chunk_count`
    (those are only known once the job finishes).
    """
    try:
        _validate_tool_name(item_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _reject_builtin_kb_name(item_name)

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > max_files:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files ({len(files)}); max {max_files} per upload",
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
        if len(data) > max_file_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File '{filename}' exceeds the {max_file_size_bytes // (1024 * 1024)}MB per-file limit",
            )
        total_size += len(data)
        if total_size > max_total_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Total upload size exceeds the {max_total_size_bytes // (1024 * 1024)}MB limit",
            )
        contents[filename] = data

    if kb_type not in _KNOWLEDGE_BASE_TYPES:
        valid = ", ".join(sorted(_KNOWLEDGE_BASE_TYPES))
        raise HTTPException(
            status_code=400,
            detail=f"Knowledge base has unknown type '{kb_type}'. Valid types: {valid}",
        )
    try:
        _validate_chunk_params(item_name, chunk_size, chunk_overlap)
    except BestTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Write the new files into a fresh version subdirectory, then dispatch an
    # async ingestion job to parse/chunk/(embed) them. No CURRENT pointer is
    # written for this path -- nothing reads it; retrieval resolves the live
    # version via the ingestion job's own `status` (see ui/backend/ingestion.py).
    # Uploads are org-scoped on disk so two orgs' same-named KBs can't share
    # (or clobber) a directory. Legacy pre-multi-tenancy uploads at
    # `_KB_UPLOADS_DIR/<name>` keep working: KB configs embed the absolute
    # root, so existing records still resolve their old directory.
    kb_root = _KB_UPLOADS_DIR / str(org_id) / item_name
    version = f"v_{uuid.uuid4().hex[:12]}"
    version_dir = kb_root / version
    # Hold the per-KB lock across staging + commit + dispatch. A concurrent
    # delete takes the same lock and rmtree's the whole KB root; if staging ran
    # outside the lock, the delete could remove a version staged here (F1).
    # Same-KB uploads also serialize now (each still owns a unique version
    # dir; the extra contention is fine).
    with _kb_upload_lock(f"{org_id}/{item_name}"):
        version_dir.mkdir(parents=True, exist_ok=True)
        try:
            for filename, data in contents.items():
                (version_dir / filename).write_bytes(data)

            spec = KnowledgeBaseSpec(
                name=item_name,
                path=str(kb_root),
                type=kb_type,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                top_k=top_k,
                embedding_model=embedding_model if kb_type in ("vector", "hybrid") else None,
                cache_path=(f"kb_{org_id}_{item_name}.json" if kb_type in ("vector", "hybrid") else None),
                rerank_model=rerank_model,
                query_expansion_model=query_expansion_model,
            )
            raw = spec.to_raw()
            item = db.query(KnowledgeBaseRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
            if item is None:
                item = KnowledgeBaseRecord(name=item_name, config=raw, org_id=org_id)
                db.add(item)
            else:
                item.config = raw
            db.flush()  # need item.id below, before the outer commit

            job = IngestionJob(
                kb_id=item.id,
                org_id=org_id,
                version=version,
                status="queued",
                file_count=len(contents),
                created_by=created_by,
            )
            db.add(job)
            db.commit()

            ingestion._executor.submit(
                ingestion.run_ingestion_job,
                job.id,
                item.id,
                org_id,
                version_dir,
                kb_type=kb_type,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                embedding_model=embedding_model,
                engine=db.get_bind(),
            )

            return {"name": item_name, "job_id": job.id, "status": "queued"}
        except Exception:
            # No CURRENT pointer is written for the DB-backed path (nothing
            # reads it -- retrieval resolves the live version via the
            # ingestion job's own status), so on a pre-dispatch failure the
            # only cleanup needed is the just-written version directory.
            db.rollback()
            shutil.rmtree(version_dir, ignore_errors=True)
            raise


def resolve_kb_upload_path(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve an upload-managed KB path to its active version directory.

    If the KB's `path` contains a `CURRENT` pointer file, return a copy with
    `path` pointing at the named version subdir. Manual-config KBs and legacy
    flat upload dirs (no pointer) are returned unchanged, so they scan their
    path directly. The pointer always names a complete version, so a reader
    never observes the brief no-live-dir window a rename-based swap would create
    (CR-008)."""
    path = config.get("path")
    if not isinstance(path, str):
        return config
    try:
        version = (Path(path) / _KB_CURRENT_POINTER).read_text(encoding="utf-8").strip()
    except OSError:
        return config  # no pointer -> flat/manual layout, scan path as-is
    version_dir = Path(path) / version
    if not version_dir.is_dir():
        return config
    return {**config, "path": str(version_dir)}


def has_traversal(value: str) -> bool:
    # Check under both path flavors so the guard behaves the same on the Linux
    # server and a Windows dev box (e.g. "foo/../bar" vs "foo\\..\\bar").
    return ".." in PurePosixPath(value).parts or ".." in PureWindowsPath(value).parts


def looks_absolute(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _cache_basename(value: str) -> str:
    # Strip to the final path component across both separators, so a rooted or
    # nested value can only ever contribute a filename.
    base = re.split(r"[\\/]", value)[-1].strip()
    return base if base not in ("", ".", "..") else "cache.json"


def contained_cache_path(value: str) -> str:
    """Force any cache_path into the app-owned `_kb_cache/` subdir (CR-001)."""
    return f"{_KB_CACHE_DIRNAME}/{_cache_basename(value)}"


def checked_contained_cache_path(value: str) -> str:
    """Boundary guard: reject absolute/`..` cache_path with a clear 400, then
    return the contained relative path. Callers store the returned value."""
    if has_traversal(value):
        raise HTTPException(status_code=400, detail="Knowledge base 'cache_path' must not contain '..' path segments")
    if looks_absolute(value):
        raise HTTPException(
            status_code=400,
            detail="Knowledge base 'cache_path' must be a relative path (it is stored under an application-owned directory)",
        )
    return contained_cache_path(value)


def check_path_traversal(value: str) -> None:
    """Boundary guard for a KB `path` (absolute local_folder paths stay allowed;
    only `..` traversal is rejected)."""
    if has_traversal(value):
        raise HTTPException(status_code=400, detail="Knowledge base 'path' must not contain '..' path segments")


def contain_kb_config_for_load(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a KB config with cache_path contained. Non-raising, for
    load time -- so a record persisted before the boundary guards existed still
    can't write outside `_kb_cache/`."""
    cache_path = config.get("cache_path")
    if isinstance(cache_path, str):
        return {**config, "cache_path": contained_cache_path(cache_path)}
    return config


def contain_workflow_config_for_load(config: Dict[str, Any]) -> Dict[str, Any]:
    """As above, for a workflow config's inline `knowledge_bases` list."""
    kbs = config.get("knowledge_bases")
    if not isinstance(kbs, list):
        return config
    return {
        **config,
        "knowledge_bases": [
            contain_kb_config_for_load(kb) if isinstance(kb, dict) else kb for kb in kbs
        ],
    }


def ensure_contained_cache_path_for_source(config: Dict[str, Any], source: Path) -> None:
    """Reject a cache path whose resolved target escapes the owned cache dir.

    ``contained_cache_path`` removes lexical traversal, but an existing
    ``_kb_cache`` directory could itself be a symlink/junction. Resolve both
    sides before the vector KB creates its cache so backend-managed workflows
    never turn that into an arbitrary server-file write (CR-001).
    """
    cache_path = config.get("cache_path")
    if not isinstance(cache_path, str):
        return

    root = source.parent.resolve()
    cache_root = (root / _KB_CACHE_DIRNAME).resolve()
    target = (source.parent / cache_path).resolve()
    try:
        target.relative_to(cache_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Knowledge base 'cache_path' resolves outside the application-owned cache directory",
        ) from exc


def ensure_workflow_cache_paths_for_source(config: Dict[str, Any], source: Path) -> None:
    """Apply resolved-target containment to every inline knowledge base."""
    knowledge_bases = config.get("knowledge_bases")
    if not isinstance(knowledge_bases, list):
        return
    for kb in knowledge_bases:
        if isinstance(kb, dict):
            ensure_contained_cache_path_for_source(kb, source)


def load_knowledge_base_tools(
    db: Session, raw: Dict[str, Any], source: Path, *, org_id: Optional[int] = None
) -> Dict[str, Any]:
    """Return a name -> tool mapping for only the standalone knowledge bases
    `raw`'s agents actually reference by name in their `tools:` lists.

    Building a knowledge base means re-reading and re-chunking every file
    (and, for type: vector, calling an embedding model) -- this only pays
    that cost for knowledge bases the workflow being loaded actually uses,
    not every standalone knowledge base in the database.

    Name resolution is org-scoped: only `org_id`'s knowledge bases resolve
    (KBs have no platform tier), so one org can never reference another
    org's KB by name.
    """
    referenced = {
        tool_name
        for agent in raw.get("agents", [])
        for tool_name in agent.get("tools", [])
    }
    if not referenced:
        return {}

    records = (
        db.query(KnowledgeBaseRecord)
        .filter(
            KnowledgeBaseRecord.name.in_(referenced),
            KnowledgeBaseRecord.org_id == org_id,
        )
        .all()
    )
    tools: Dict[str, Any] = {}
    for record in records:
        # Fail closed on a legacy KB whose name shadows a built-in tool (F4).
        # New collisions are blocked at KB PUT/upload, but a record predating
        # that guard would silently replace the built-in at load; refuse instead
        # (covers both `_get_workflow` and the autonomous trigger, which share
        # this loader).
        if record.name in REGISTRY:
            raise ConfigurationError(
                f"Knowledge base '{record.name}' shadows a built-in tool of the "
                "same name; rename the knowledge base."
            )
        kb = resolve_knowledge_base(db, record, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools


def resolve_knowledge_base(db: Session, record: KnowledgeBaseRecord, source: Path) -> Any:
    """Build the `KnowledgeBase` for one `KnowledgeBaseRecord`: DB-backed
    (from its most recent completed `IngestionJob`'s Document/Chunk rows) if
    one exists, else the original file-based construction (a pre-existing KB
    that predates this feature and was never re-uploaded). Shared by
    `load_knowledge_base_tools` (above) and `builder.py::_all_knowledge_base_tools`
    (the pre-Specification "every standalone KB" catalog builder), so both
    resolve a KB's live content the same way."""
    job = (
        db.query(IngestionJob)
        .filter_by(kb_id=record.id, status="completed")
        .order_by(IngestionJob.completed_at.desc())
        .first()
    )
    if job is not None:
        return _build_knowledge_base_from_job(record, job, db)
    # Pre-existing KB (predates this feature, never re-uploaded) -- fall back
    # to the original file-based construction, unchanged.
    config = resolve_kb_upload_path(contain_kb_config_for_load(record.config))
    ensure_contained_cache_path_for_source(config, source)
    return _build_knowledge_base(config, source)


def _build_knowledge_base_from_job(record: KnowledgeBaseRecord, job: "IngestionJob", db: Session) -> Any:
    """Build the matching KnowledgeBase subclass from a completed job's
    Document/Chunk rows -- the DB-backed read path (see
    docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md).
    Never reads from disk."""
    rows = (
        db.query(KnowledgeChunk, KnowledgeDocument.filename)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .filter(KnowledgeDocument.ingestion_job_id == job.id)
        .order_by(KnowledgeDocument.filename, KnowledgeChunk.chunk_index)
        .all()
    )
    chunks = [_Chunk(source=filename, text=chunk.text) for chunk, filename in rows]

    config = record.config
    kb_type = config.get("type", "local_folder")
    common_kwargs: Dict[str, Any] = {
        "top_k": config.get("top_k", 5),
        "rerank_model": config.get("rerank_model"),
        "candidate_k": config.get("candidate_k"),
        "query_expansion_model": config.get("query_expansion_model"),
        "query_expansion_count": config.get("query_expansion_count", 3),
    }

    if kb_type == "local_folder":
        return LocalFolderKnowledgeBase.from_chunks(record.name, chunks, **common_kwargs)

    vectors = [json.loads(chunk.embedding_json) for chunk, _filename in rows]
    embedding_model = config.get("embedding_model")
    vector_kwargs = {**common_kwargs, "score_threshold": config.get("score_threshold")}
    if kb_type == "vector":
        return VectorKnowledgeBase.from_chunks(record.name, chunks, vectors, embedding_model, **vector_kwargs)
    if kb_type == "hybrid":
        return HybridKnowledgeBase.from_chunks(record.name, chunks, vectors, embedding_model, **vector_kwargs)
    raise ConfigurationError(f"Knowledge base '{record.name}' has unknown type '{kb_type}'")


def kb_name_collisions(db: Session, org_id: Optional[int], raw_spec: Dict[str, Any]) -> List[str]:
    """KB names in `raw_spec` (inline + referenced standalone) that shadow a built-in tool.

    Name-only (no KB is built), so it can run before path validation / build.
    """
    referenced = {
        tool
        for agent in raw_spec.get("agents", []) or []
        for tool in (agent.get("tools") or [])
    }
    standalone: set = set()
    if referenced:
        standalone = {
            row.name
            for row in db.query(KnowledgeBaseRecord.name).filter(
                KnowledgeBaseRecord.org_id == org_id,
                KnowledgeBaseRecord.name.in_(referenced),
            )
        }
    return find_kb_tool_collisions(raw_spec, standalone, REGISTRY)
