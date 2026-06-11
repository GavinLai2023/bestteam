from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from ..exceptions import ConfigurationError
from ..tools import REGISTRY as _TOOL_REGISTRY
from .agent import Agent
from .knowledge_base import KnowledgeBase, LocalFolderKnowledgeBase, make_knowledge_base_tool
from .team import CollaborationMode, Team
from .vector_knowledge_base import VectorKnowledgeBase
from .workflow import Workflow

_KNOWLEDGE_BASE_TYPES = {
    "local_folder": LocalFolderKnowledgeBase,
    "vector": VectorKnowledgeBase,
}


def load_workflow(path, *, toolkits=None) -> Workflow:
    """Build a Workflow from a declarative YAML file.

    This is what lets customers define agents/teams/pipelines without writing
    any orchestration code — the CLI's `run`/`graph` commands are thin
    wrappers around this loader plus Workflow.run()/.visualize().

    Args:
        path: Path to the YAML workflow file.
        toolkits: Optional list of ToolKit instances whose tools are made
            available to agents defined in this workflow. Custom tools are
            merged with the built-in REGISTRY and can be referenced by name
            in the YAML ``tools:`` list.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Workflow file not found: {path}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Could not parse '{path}' as YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"'{path}' must contain a YAML mapping at the top level")

    extra_tools: Dict[str, Any] = {}
    for tk in (toolkits or []):
        extra_tools.update(tk.items())

    try:
        return _build_workflow(raw, source=path, extra_tools=extra_tools)
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"Malformed workflow config in '{path}': missing or invalid field {exc}") from exc


def _build_workflow(raw: Dict[str, Any], *, source: Path, extra_tools: Dict[str, Any]) -> Workflow:
    tool_lookup = {**_TOOL_REGISTRY, **extra_tools}
    for spec in raw.get("knowledge_bases", []):
        kb = _build_knowledge_base(spec, source)
        tool_lookup[kb.name] = make_knowledge_base_tool(kb)

    agents = {spec["name"]: _build_agent(spec, tool_lookup) for spec in raw.get("agents", [])}

    teams: Dict[str, Team] = {}
    for spec in raw.get("teams", []):
        team_name = spec["name"]
        team_agents = [_lookup(agents, name, "agent", team_name) for name in spec["agents"]]
        manager = _lookup(agents, spec["manager"], "agent", team_name) if "manager" in spec else None
        teams[team_name] = Team(
            name=team_name,
            agents=team_agents,
            mode=_parse_mode(spec.get("mode", "sequential"), team_name),
            manager=manager,
        )

    workflow_spec = raw.get("workflow", {})
    steps = [_lookup(teams, name, "team", "workflow") for name in workflow_spec.get("steps", [])]

    return Workflow(name=raw.get("name", source.stem), steps=steps)


def _build_knowledge_base(spec: Dict[str, Any], source: Path) -> KnowledgeBase:
    spec = dict(spec)
    name = spec.pop("name")
    raw_path = spec.pop("path")
    kb_type = spec.pop("type", "local_folder")

    path = Path(raw_path)
    if not path.is_absolute():
        path = (source.parent / path).resolve()

    if not path.is_dir():
        raise ConfigurationError(
            f"Knowledge base '{name}' path does not exist or is not a directory: {path}"
        )

    try:
        kb_cls = _KNOWLEDGE_BASE_TYPES[kb_type]
    except KeyError as exc:
        valid = ", ".join(sorted(_KNOWLEDGE_BASE_TYPES))
        raise ConfigurationError(
            f"Knowledge base '{name}' has unknown type '{kb_type}'. Valid types: {valid}"
        ) from exc

    if "cache_path" in spec:
        cache_path = Path(spec["cache_path"])
        if not cache_path.is_absolute():
            cache_path = (source.parent / cache_path).resolve()
        spec["cache_path"] = cache_path

    return kb_cls(name=name, path=path, **spec)


def _build_agent(spec: Dict[str, Any], tool_lookup: Dict[str, Any]) -> Agent:
    spec = dict(spec)
    raw_tools = spec.pop("tools", []) or []
    tools = []
    for name in raw_tools:
        if name not in tool_lookup:
            available = ", ".join(sorted(tool_lookup))
            raise ConfigurationError(
                f"Unknown tool '{name}'. Available tools: {available}"
            )
        tools.append(tool_lookup[name])
    return Agent(**spec, tools=tools)


def _lookup(registry: Dict[str, Any], name: str, kind: str, owner: str) -> Any:
    try:
        return registry[name]
    except KeyError as exc:
        raise ConfigurationError(f"'{owner}' references unknown {kind} '{name}'") from exc


def _parse_mode(value: str, team_name: str) -> CollaborationMode:
    try:
        return CollaborationMode(value)
    except ValueError as exc:
        valid = ", ".join(mode.value for mode in CollaborationMode)
        raise ConfigurationError(
            f"Team '{team_name}' has invalid mode '{value}'. Valid modes: {valid}"
        ) from exc
