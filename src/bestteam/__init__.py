import importlib.metadata as _metadata

from .core.agent import Agent
from .core.hybrid_knowledge_base import HybridKnowledgeBase
from .core.knowledge_base import KnowledgeBase, LocalFolderKnowledgeBase
from .core.loader import load_pipeline
from .core.memory import Memory, MemoryManager, MemoryRecord, SqliteBM25Memory
from .core.requirements import Requirements, generate_requirements
from .core.specification import (
    AgentSpec,
    KnowledgeBaseSpec,
    PipelineSpec,
    Specification,
    SkillSpec,
    TeamSpec,
    generate_specification,
    validate_specification,
)
from .core.team import CollaborationMode, Team
from .core.tools import ToolKit
from .core.trace import TraceEvent
from .core.vector_knowledge_base import VectorKnowledgeBase
from .core.pipeline import Pipeline, PipelineResult
from .exceptions import BestTeamError, ConfigurationError, EngineError
from .tools import (
    calculator,
    email_draft_reply,
    email_find,
    email_read,
    email_read_attachment,
    http_get,
    local_business_search,
    parse_file,
    web_search,
)

# Read from installed package metadata rather than restated here: this and
# pyproject.toml's `version` are the same fact, and a second literal that has
# to be edited in step is one that eventually is not -- `bestteam --version`
# would then report a version no build ever had. The fallback covers a source
# tree that was never installed (running straight from a checkout).
try:
    __version__ = _metadata.version("bestteam")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = "0.0.0.dev0"

__all__ = [
    "Agent",
    "Team",
    "CollaborationMode",
    "Pipeline",
    "PipelineResult",
    "ToolKit",
    "TraceEvent",
    "Memory",
    "MemoryRecord",
    "SqliteBM25Memory",
    "MemoryManager",
    "KnowledgeBase",
    "LocalFolderKnowledgeBase",
    "VectorKnowledgeBase",
    "HybridKnowledgeBase",
    "Requirements",
    "generate_requirements",
    "Specification",
    "SkillSpec",
    "AgentSpec",
    "TeamSpec",
    "KnowledgeBaseSpec",
    "PipelineSpec",
    "generate_specification",
    "validate_specification",
    "load_pipeline",
    "BestTeamError",
    "ConfigurationError",
    "EngineError",
    "web_search",
    "parse_file",
    "http_get",
    "calculator",
    "email_find",
    "email_read",
    "email_read_attachment",
    "email_draft_reply",
    "local_business_search",
]
