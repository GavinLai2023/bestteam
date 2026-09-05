from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from ..exceptions import ConfigurationError
from ._structured_output import invoke_structured
from .loader import _build_pipeline
from .pipeline import Pipeline

_VALID_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_tool_name(name: str) -> None:
    """Knowledge base names become literal tool-call function names (see
    `make_knowledge_base_tool`); most LLM provider APIs reject function names
    containing spaces or other punctuation, so reject them here rather than
    failing silently inside a real model call."""
    if not _VALID_TOOL_NAME_RE.match(name):
        raise ValueError(
            f"'{name}' is not a valid knowledge base name: use only letters, "
            "numbers, underscores, and hyphens (no spaces or other "
            "punctuation) -- this name is used directly as a tool name when "
            "an agent calls it."
        )

# Guides the "Solution Architect" agent (see docs/team_builder_methodology.md,
# Specification stage) from confirmed Requirements to a Specification. Written
# for a real, structured-output-capable model -- not a `fake:` chat model.
_ARCHITECT_SYSTEM_PROMPT = """You are the Solution Architect for bestteam, a multi-agent \
team-building platform for non-technical customers.

Given a customer's confirmed Requirements (their goals, pain points, success \
criteria, and constraints), design a Specification: a small team of AI \
"employees" (agents), organized into one or more teams, that can address \
those requirements.

For each agent, give it a clear name, role, goal, and (optionally) a \
backstory, plus a friendly display_name and a one-sentence \
friendly_description that a non-technical person would understand. Give it \
tools by adding their exact names to its `tools` list, using only names from \
the "Available built-in tools" list provided in the input; never invent a tool \
name. Pick a model spec string appropriate to the task.

If skills are listed in the input, assign them to agents via each agent's \
`skills` field (a list of skill names from the provided list). A Skill is a \
reusable instruction document for a repeatable task -- prefer assigning one \
over re-describing the same task in `backstory`. Only use skill names from \
the provided list; never invent names.

Knowledge bases work the same way: only reference one by adding its exact \
name to an agent's `tools` list, and only if it appears in an "Available \
knowledge bases" list provided in the input. Never add a `knowledge_bases` \
entry of your own with an invented `path` -- there is no way for you to \
know what exists on the server's filesystem. If no available knowledge \
base matches what the customer needs (including when none are listed at \
all), design the team without one rather than fabricating one.

Group agents into teams. Use 'sequential' mode when agents hand work to each \
other in order, 'parallel' mode when they work independently on the same \
input, and 'hierarchical' mode (with a 'manager' agent) when one agent \
should coordinate and delegate to the others -- the metaphor customers will \
see is "hiring a team with a manager". A team's `agents` are the members its \
manager delegates to, so do not list the manager itself among them. Give each \
team a friendly \
display_name and friendly_description describing how the team works \
together in plain language.

Finally, list the teams in the order they should run as the pipeline's steps.

If you receive feedback that a previous design was invalid, fix exactly the \
issue described and resubmit a complete, corrected Specification."""


class AgentSpec(BaseModel):
    """Mirrors an `agents:` entry in the loader's raw dict (see `core/loader.py`).

    `display_name`/`friendly_description` are wizard-only presentation fields
    -- `to_raw()` strips them before handing the spec to `_build_pipeline`.
    """

    name: str
    role: str
    goal: str
    backstory: str = ""
    model: str
    tools: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    # A loader-level field like `skills` (kept by `to_raw()`), not a
    # wizard-only presentation field: round-tripping a pipeline that sets it
    # must not drop it. Validated by `Agent.__post_init__` at compile time.
    grounding_policy: Optional[str] = None
    grounding_level: Optional[str] = None
    grounding_model: Optional[str] = None
    display_name: Optional[str] = None
    friendly_description: Optional[str] = None

    def to_raw(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {"name": self.name, "role": self.role, "goal": self.goal, "model": self.model}
        if self.backstory:
            raw["backstory"] = self.backstory
        if self.tools:
            raw["tools"] = list(self.tools)
        if self.skills:
            raw["skills"] = list(self.skills)
        if self.grounding_policy:
            raw["grounding_policy"] = self.grounding_policy
        if self.grounding_level:
            raw["grounding_level"] = self.grounding_level
        if self.grounding_model:
            raw["grounding_model"] = self.grounding_model
        return raw


class SkillSpec(BaseModel):
    """A reusable instruction document plus the tools it depends on.

    Referenced by name from `AgentSpec.skills` and resolved against
    `extra_skills` in `core/loader.py::_build_pipeline` -- the skill's
    `instructions` are appended to the referencing agent's `backstory` and
    its `tools` are merged into the agent's `tools`.
    """

    name: str
    description: str = ""
    instructions: str
    tools: List[str] = Field(default_factory=list)

    def to_raw(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {"name": self.name, "instructions": self.instructions}
        if self.description:
            raw["description"] = self.description
        if self.tools:
            raw["tools"] = list(self.tools)
        return raw


class TeamSpec(BaseModel):
    """Mirrors a `teams:` entry in the loader's raw dict (see `core/loader.py`)."""

    name: str
    agents: List[str]
    mode: str = "sequential"
    manager: Optional[str] = None
    display_name: Optional[str] = None
    friendly_description: Optional[str] = None

    def to_raw(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {"name": self.name, "agents": list(self.agents), "mode": self.mode}
        if self.manager is not None:
            raw["manager"] = self.manager
        return raw


class KnowledgeBaseSpec(BaseModel):
    """Mirrors a `knowledge_bases:` entry in the loader's raw dict (see `core/loader.py`).

    `embedding_model`/`score_threshold`/`cache_path` only apply to
    `type: vector` or `type: hybrid` -- `to_raw()` omits them for
    `local_folder` so an architect that sets them on the wrong type doesn't
    trigger an unexpected `TypeError` from the knowledge base constructor.
    `rerank_model`/`candidate_k`/`query_expansion_model`/
    `query_expansion_count` apply to ALL THREE types (retrieval-method-agnostic).
    """

    name: str
    path: str
    # One sentence saying what these documents cover. It becomes the agent
    # tool's own description, so it is what tells a model which of an org's
    # collections answers a question; capped because it is prompt text.
    description: Optional[str] = Field(default=None, max_length=500)
    type: str = "local_folder"
    chunk_size: int = 1000
    chunk_overlap: int = 100
    top_k: int = 5
    embedding_model: Optional[str] = None
    score_threshold: Optional[float] = None
    cache_path: Optional[str] = None
    rerank_model: Optional[str] = None
    candidate_k: Optional[int] = None
    query_expansion_model: Optional[str] = None
    query_expansion_count: int = 3

    @field_validator("name")
    @classmethod
    def _name_is_valid_tool_identifier(cls, v: str) -> str:
        _validate_tool_name(v)
        return v

    def to_raw(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k": self.top_k,
            "query_expansion_model": self.query_expansion_model,
            "query_expansion_count": self.query_expansion_count,
        }
        if self.description is not None:
            raw["description"] = self.description
        if self.rerank_model is not None:
            raw["rerank_model"] = self.rerank_model
        if self.candidate_k is not None:
            raw["candidate_k"] = self.candidate_k
        if self.type in ("vector", "hybrid"):
            if self.embedding_model is not None:
                raw["embedding_model"] = self.embedding_model
            if self.score_threshold is not None:
                raw["score_threshold"] = self.score_threshold
            if self.cache_path is not None:
                raw["cache_path"] = self.cache_path
        return raw


class PipelineSpec(BaseModel):
    """Mirrors the `pipeline:` entry in the loader's raw dict (see `core/loader.py`)."""

    steps: List[str] = Field(default_factory=list)


class Specification(BaseModel):
    """A complete, loader-shaped team design plus wizard-only friendly fields.

    This is the structured-output schema the Solution Architect agent fills
    in (`generate_specification`), and what `validate_specification` checks
    against `_build_pipeline` before it's shown to (or accepted by) a customer.
    """

    name: str
    knowledge_bases: List[KnowledgeBaseSpec] = Field(default_factory=list)
    agents: List[AgentSpec] = Field(default_factory=list)
    teams: List[TeamSpec] = Field(default_factory=list)
    pipeline: PipelineSpec = Field(default_factory=PipelineSpec)

    def to_raw(self) -> Dict[str, Any]:
        """Strip wizard-only fields and produce the loader's raw dict shape."""
        return {
            "name": self.name,
            "knowledge_bases": [kb.to_raw() for kb in self.knowledge_bases],
            "agents": [agent.to_raw() for agent in self.agents],
            "teams": [team.to_raw() for team in self.teams],
            "pipeline": {"steps": list(self.pipeline.steps)},
        }


def validate_specification(
    spec: Specification,
    *,
    source: Path,
    extra_tools: Optional[Dict[str, Any]] = None,
    extra_skills: Optional[Dict[str, Any]] = None,
) -> Pipeline:
    """Compile a Specification through the same code path as a YAML pipeline file.

    Returns the resulting `Pipeline` if the spec is valid. Raises
    `ConfigurationError` with a message suitable for showing to a customer or
    feeding back to the Solution Architect agent for self-correction.
    """
    raw = spec.to_raw()
    try:
        return _build_pipeline(raw, source=source, extra_tools=extra_tools or {}, extra_skills=extra_skills or {})
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"Specification is malformed: missing or invalid field {exc}") from exc


def generate_specification(
    model: BaseChatModel,
    requirements: str,
    *,
    source: Path,
    extra_tools: Optional[Dict[str, Any]] = None,
    extra_skills: Optional[Dict[str, Any]] = None,
    pre_validate: Optional[Callable[[Specification], None]] = None,
    max_attempts: int = 3,
) -> Specification:
    """Generate a Specification from Requirements, self-correcting on validation errors.

    Calls `model.with_structured_output(Specification)` to get a candidate
    design, then validates it via `validate_specification`. If validation
    fails, the `ConfigurationError` is turned into feedback appended to the
    conversation and the architect tries again, up to `max_attempts` times.
    """
    messages: List[BaseMessage] = [
        SystemMessage(content=_ARCHITECT_SYSTEM_PROMPT),
        HumanMessage(content=requirements),
    ]

    last_error: Optional[ConfigurationError] = None
    method = "function_calling"
    for _ in range(max_attempts):
        try:
            result, method = invoke_structured(model, Specification, messages, method=method)
        except NotImplementedError as exc:
            raise ConfigurationError(
                "The Team Builder needs a real AI model that can produce structured "
                "output; the selected model can't (for example, a demo 'fake:' model). "
                "Choose a real model to design your team."
            ) from exc
        except OutputParserException as exc:
            # The completion didn't even fit the Specification schema (e.g. a full
            # agent object nested under teams[].agents[], which must be a list of
            # agent NAMES, or a missing top-level `name`) -- raised by
            # `with_structured_output` before we ever get a Specification to hand to
            # `validate_specification`. Self-correct on it the same way, instead of
            # letting it crash the whole request.
            last_error = ConfigurationError(str(exc))
            messages.append(AIMessage(content=exc.llm_output or str(exc)))
            messages.append(
                HumanMessage(
                    content=(
                        f"That design doesn't work yet: {exc} "
                        "Please revise the specification to fix this issue."
                    )
                )
            )
            continue
        spec = result if isinstance(result, Specification) else Specification.model_validate(result)
        try:
            if pre_validate is not None:
                pre_validate(spec)
            validate_specification(spec, source=source, extra_tools=extra_tools, extra_skills=extra_skills)
            return spec
        except ConfigurationError as exc:
            last_error = exc
            messages.append(AIMessage(content=spec.model_dump_json()))
            messages.append(
                HumanMessage(
                    content=(
                        f"That design doesn't work yet: {exc} "
                        "Please revise the specification to fix this issue."
                    )
                )
            )

    raise ConfigurationError(
        f"Solution Architect could not produce a valid specification after "
        f"{max_attempts} attempts. Last error: {last_error}"
    )
