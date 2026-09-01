from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..exceptions import ConfigurationError
from .grounding import GROUNDING_LEVELS, GROUNDING_POLICIES

# A model spec is either a provider model name ("openai:gpt-4o-mini") resolved
# at compile time, or a ready-made langchain BaseChatModel/Runnable instance —
# customers never need to know which engine ends up consuming it.
ModelSpec = Any


@dataclass
class Agent:
    """A single worker — a thin, business-facing config object.

    Agent intentionally knows nothing about LangGraph or CrewAI. The adapter
    layer turns it into whatever the underlying engine needs at compile time,
    which is what lets us swap engines without breaking customer code.
    """

    name: str
    role: str
    goal: str
    backstory: str = ""
    tools: Sequence[Callable[..., Any]] = field(default_factory=tuple)
    model: ModelSpec | None = None
    # What to do when this agent's answer fails the grounding check
    # (core/grounding.py). Inert for an agent without a knowledge-base tool.
    grounding_policy: str = "observe"
    # How deep that check goes: "citation" (set membership over returned
    # citations -- the default and the pre-existing behaviour) or "claim"
    # (an additional LLM grader judges each factual claim against the turn's
    # search results). Inert without a knowledge-base tool, like the policy.
    grounding_level: str = "citation"
    # Model for the claim grader; None means the agent's own model. Only read
    # when grounding_level == "claim".
    grounding_model: ModelSpec | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("Agent.name is required")
        if not self.role:
            raise ConfigurationError(f"Agent '{self.name}' requires a role")
        if not self.goal:
            raise ConfigurationError(f"Agent '{self.name}' requires a goal")
        if self.grounding_policy not in GROUNDING_POLICIES:
            valid = ", ".join(GROUNDING_POLICIES)
            raise ConfigurationError(
                f"Agent '{self.name}' has unknown grounding_policy "
                f"'{self.grounding_policy}'. Valid values: {valid}"
            )
        if self.grounding_level not in GROUNDING_LEVELS:
            valid = ", ".join(GROUNDING_LEVELS)
            raise ConfigurationError(
                f"Agent '{self.name}' has unknown grounding_level "
                f"'{self.grounding_level}'. Valid values: {valid}"
            )

    def system_prompt(self) -> str:
        """Render this agent's identity into a system prompt for the LLM."""
        lines = [f"You are {self.name}, a {self.role}.", f"Your goal: {self.goal}"]
        if self.backstory:
            lines.append(f"Background: {self.backstory}")
        return "\n".join(lines)
