"""Build extra_tools for workflow loading from standalone KnowledgeBaseRecords
(created via /api/config/knowledge_bases, manually or via file upload)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from bestteam.core.knowledge_base import make_knowledge_base_tool
from bestteam.core.loader import _build_knowledge_base

from .db.models import KnowledgeBaseRecord

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
        config = resolve_kb_upload_path(contain_kb_config_for_load(record.config))
        ensure_contained_cache_path_for_source(config, source)
        kb = _build_knowledge_base(config, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools
