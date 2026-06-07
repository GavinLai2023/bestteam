import pytest

from bestteam import Agent
from bestteam.exceptions import ConfigurationError


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
