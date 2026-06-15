from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from bestteam import (
    AgentSpec,
    KnowledgeBaseSpec,
    Specification,
    SkillSpec,
    TeamSpec,
    WorkflowSpec,
    calculator,
    generate_specification,
    validate_specification,
)
from bestteam.exceptions import ConfigurationError


class _FakeArchitectChatModel(BaseChatModel):
    """Cycles through pre-scripted Specification objects via `with_structured_output`,
    independent of `bind_tools`/tool-calling -- lets tests script a Solution
    Architect's structured outputs without a real provider."""

    responses: List[Any] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    @property
    def _llm_type(self) -> str:
        return "fake-architect"

    def with_structured_output(self, schema, **kwargs):
        responses = self.responses
        state = {"index": 0}

        def _invoke(_input):
            i = min(state["index"], len(responses) - 1)
            state["index"] += 1
            return responses[i]

        return RunnableLambda(_invoke)


def _basic_spec(
    *, mode: str = "sequential", agent_tools=None, agent_skills=None, manager: str | None = None
) -> Specification:
    return Specification(
        name="support_workflow",
        agents=[
            AgentSpec(
                name="support_agent",
                role="Customer Support Specialist",
                goal="Answer customer questions",
                model="fake:hello",
                tools=agent_tools or [],
                skills=agent_skills or [],
                display_name="Support Specialist",
                friendly_description="Answers customer questions.",
            )
        ],
        teams=[
            TeamSpec(
                name="support_team",
                agents=["support_agent"],
                mode=mode,
                manager=manager,
                display_name="Support Team",
                friendly_description="The support specialist handles every request.",
            )
        ],
        workflow=WorkflowSpec(steps=["support_team"]),
    )


def test_to_raw_strips_friendly_fields_and_matches_loader_shape():
    spec = _basic_spec()
    raw = spec.to_raw()

    assert raw == {
        "name": "support_workflow",
        "knowledge_bases": [],
        "agents": [
            {
                "name": "support_agent",
                "role": "Customer Support Specialist",
                "goal": "Answer customer questions",
                "model": "fake:hello",
            }
        ],
        "teams": [{"name": "support_team", "agents": ["support_agent"], "mode": "sequential"}],
        "workflow": {"steps": ["support_team"]},
    }


def test_skill_spec_round_trips_basic_fields():
    skill = SkillSpec(
        name="research_skill",
        description="Research helper",
        instructions="Use the calculator for math.",
        tools=["calculator"],
    )

    assert skill.name == "research_skill"
    assert skill.description == "Research helper"
    assert skill.instructions == "Use the calculator for math."
    assert skill.tools == ["calculator"]


def test_agent_spec_to_raw_omits_skills_when_empty():
    spec = AgentSpec(
        name="support_agent",
        role="Customer Support Specialist",
        goal="Answer customer questions",
        model="fake:hello",
    )

    assert "skills" not in spec.to_raw()


def test_agent_spec_to_raw_includes_skills_when_set():
    spec = AgentSpec(
        name="support_agent",
        role="Customer Support Specialist",
        goal="Answer customer questions",
        model="fake:hello",
        skills=["research_skill"],
    )

    assert spec.to_raw()["skills"] == ["research_skill"]


def test_validate_specification_accepts_a_valid_spec_and_runs(tmp_path):
    spec = _basic_spec()
    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml")

    result = workflow.run("hi")
    assert result.output == "hello"


def test_validate_specification_rejects_unknown_tool(tmp_path):
    spec = _basic_spec(agent_tools=["does_not_exist"])

    with pytest.raises(ConfigurationError, match="Unknown tool"):
        validate_specification(spec, source=tmp_path / "workflow.yaml")


def test_validate_specification_resolves_skill_into_tools_and_backstory(tmp_path):
    spec = _basic_spec(agent_skills=["research_skill"])
    extra_skills = {
        "research_skill": SkillSpec(
            name="research_skill",
            instructions="Use the calculator for any math in the customer's question.",
            tools=["calculator"],
        )
    }

    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)
    agent = workflow.steps[0].agents[0]

    assert calculator in agent.tools
    assert "Use the calculator for any math in the customer's question." in agent.backstory


def test_validate_specification_dedupes_skill_tools_with_agent_tools(tmp_path):
    spec = _basic_spec(agent_tools=["calculator"], agent_skills=["research_skill"])
    extra_skills = {
        "research_skill": SkillSpec(
            name="research_skill",
            instructions="Use the calculator for any math in the customer's question.",
            tools=["calculator"],
        )
    }

    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)
    agent = workflow.steps[0].agents[0]

    assert agent.tools.count(calculator) == 1


def test_validate_specification_concatenates_multiple_skill_instructions_in_order(tmp_path):
    spec = _basic_spec(agent_skills=["skill_a", "skill_b"])
    extra_skills = {
        "skill_a": SkillSpec(name="skill_a", instructions="Follow step A first."),
        "skill_b": SkillSpec(name="skill_b", instructions="Then follow step B."),
    }

    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)
    agent = workflow.steps[0].agents[0]

    assert agent.backstory.index("Follow step A first.") < agent.backstory.index("Then follow step B.")


def test_validate_specification_rejects_unknown_skill(tmp_path):
    spec = _basic_spec(agent_skills=["does_not_exist"])

    with pytest.raises(ConfigurationError, match="Unknown skill"):
        validate_specification(spec, source=tmp_path / "workflow.yaml")


def test_validate_specification_rejects_skill_with_unknown_tool(tmp_path):
    spec = _basic_spec(agent_skills=["research_skill"])
    extra_skills = {
        "research_skill": SkillSpec(
            name="research_skill",
            instructions="Use a tool that doesn't exist.",
            tools=["does_not_exist"],
        )
    }

    with pytest.raises(ConfigurationError, match="Unknown tool"):
        validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)


def test_validate_specification_rejects_hierarchical_team_without_manager(tmp_path):
    spec = _basic_spec(mode="hierarchical")

    with pytest.raises(ConfigurationError, match="manager"):
        validate_specification(spec, source=tmp_path / "workflow.yaml")


def test_knowledge_base_spec_omits_vector_only_fields_for_local_folder():
    kb = KnowledgeBaseSpec(name="docs", path="./docs", embedding_model="fake:8", score_threshold=0.5)

    raw = kb.to_raw()

    assert raw["type"] == "local_folder"
    assert "embedding_model" not in raw
    assert "score_threshold" not in raw


def test_generate_specification_returns_first_valid_spec(tmp_path):
    valid = _basic_spec()
    model = _FakeArchitectChatModel(responses=[valid])

    spec = generate_specification(model, "We need help answering customer questions.", source=tmp_path / "workflow.yaml")

    assert spec == valid


def test_generate_specification_retries_after_validation_error(tmp_path):
    invalid = _basic_spec(agent_tools=["does_not_exist"])
    valid = _basic_spec()
    model = _FakeArchitectChatModel(responses=[invalid, valid])

    spec = generate_specification(model, "We need help answering customer questions.", source=tmp_path / "workflow.yaml")

    assert spec == valid


def test_generate_specification_raises_after_max_attempts(tmp_path):
    invalid = _basic_spec(agent_tools=["does_not_exist"])
    model = _FakeArchitectChatModel(responses=[invalid])

    with pytest.raises(ConfigurationError, match="could not produce a valid specification"):
        generate_specification(
            model, "We need help answering customer questions.", source=tmp_path / "workflow.yaml", max_attempts=2
        )
