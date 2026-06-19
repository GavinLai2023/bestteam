"""Build extra_tools for workflow loading from standalone KnowledgeBaseRecords
(created via /api/config/knowledge_bases, manually or via file upload)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from sqlalchemy.orm import Session

from bestteam.core.knowledge_base import make_knowledge_base_tool
from bestteam.core.loader import _build_knowledge_base

from .db.models import KnowledgeBaseRecord


def load_knowledge_base_tools(db: Session, raw: Dict[str, Any], source: Path) -> Dict[str, Any]:
    """Return a name -> tool mapping for only the standalone knowledge bases
    `raw`'s agents actually reference by name in their `tools:` lists.

    Building a knowledge base means re-reading and re-chunking every file
    (and, for type: vector, calling an embedding model) -- this only pays
    that cost for knowledge bases the workflow being loaded actually uses,
    not every standalone knowledge base in the database.
    """
    referenced = {
        tool_name
        for agent in raw.get("agents", [])
        for tool_name in agent.get("tools", [])
    }
    if not referenced:
        return {}

    records = db.query(KnowledgeBaseRecord).filter(KnowledgeBaseRecord.name.in_(referenced)).all()
    tools: Dict[str, Any] = {}
    for record in records:
        kb = _build_knowledge_base(record.config, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools
