from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from ..exceptions import ConfigurationError
from ..tools import REGISTRY as _TOOL_REGISTRY
from .agent import Agent
from .team import CollaborationMode, Team
from .workflow import Workflow


def load_workflow(path) -> Workflow:
    """Build a Workflow from a declarative YAML file.

    This is what lets customers define agents/teams/pipelines without writing
    any orchestration code — the CLI's `run`/`graph` commands are thin
    wrappers around this loader plus Workflow.run()/.visualize().
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

    try:
        return _build_workflow(raw, source=path)
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"Malformed workflow config in '{path}': missing or invalid field {exc}") from exc


def _build_workflow(raw: Dict[str, Any], *, source: Path) -> Workflow:
    agents = {spec["name"]: _build_agent(spec) for spec in raw.get("agents", [])}

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


def _build_agent(spec: Dict[str, Any]) -> Agent:
    spec = dict(spec)
    raw_tools = spec.pop("tools", []) or []
    tools = []
    for name in raw_tools:
        if name not in _TOOL_REGISTRY:
            available = ", ".join(sorted(_TOOL_REGISTRY))
            raise ConfigurationError(
                f"Unknown tool '{name}'. Available built-in tools: {available}"
            )
        tools.append(_TOOL_REGISTRY[name])
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
