import pytest

from bestteam import Agent
from bestteam.exceptions import ConfigurationError



pytestmark = pytest.mark.unit


def test_agent_requires_role_and_goal():
    with pytest.raises(ConfigurationError):
        Agent(name="bot", role="", goal="do things")

    with pytest.raises(ConfigurationError):
        Agent(name="bot", role="Helper", goal="")


def test_agent_system_prompt_includes_identity():
    agent = Agent(
        name="researcher",
        role="Research Analyst",
        goal="find facts",
        backstory="Ex-journalist",
    )
    prompt = agent.system_prompt()

    assert "researcher" in prompt
    assert "Research Analyst" in prompt
    assert "find facts" in prompt
    assert "Ex-journalist" in prompt


def test_agent_grounding_policy_defaults_to_observe():
    agent = Agent(name="bot", role="Helper", goal="do things")
    assert agent.grounding_policy == "observe"


def test_agent_rejects_an_unknown_grounding_policy():
    with pytest.raises(ConfigurationError) as exc:
        Agent(name="bot", role="Helper", goal="do things", grounding_policy="enforce")
    message = str(exc.value)
    assert "grounding_policy" in message
    assert "observe" in message and "retry" in message and "refuse" in message
