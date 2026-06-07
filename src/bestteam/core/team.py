from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from ..exceptions import ConfigurationError
from .agent import Agent


class CollaborationMode(str, Enum):
    """How the agents inside a team hand work to one another.

    Customers pick a mode instead of wiring up a graph by hand — the adapter
    translates the chosen mode into whatever structure the underlying engine
    needs (e.g. a chain of LangGraph nodes for SEQUENTIAL).
    """

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    HIERARCHICAL = "hierarchical"
    DEBATE = "debate"


@dataclass
class Team:
    """A group of agents collaborating under a chosen pattern."""

    name: str
    agents: Sequence[Agent]
    mode: CollaborationMode = CollaborationMode.SEQUENTIAL
    manager: Optional[Agent] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("Team.name is required")
        if not self.agents:
            raise ConfigurationError(f"Team '{self.name}' needs at least one agent")
        if self.mode == CollaborationMode.HIERARCHICAL and self.manager is None:
            raise ConfigurationError(
                f"Team '{self.name}' uses hierarchical mode and requires a 'manager' agent"
            )
