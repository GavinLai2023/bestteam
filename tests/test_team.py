import pytest

from bestteam import Agent, CollaborationMode, Team
from bestteam.exceptions import ConfigurationError



pytestmark = pytest.mark.unit


def _agent(name="a"):
    return Agent(name=name, role="role", goal="goal")


def test_team_requires_at_least_one_agent():
    with pytest.raises(ConfigurationError):
        Team(name="team", agents=[])


def test_hierarchical_team_requires_manager():
    with pytest.raises(ConfigurationError):
        Team(name="team", agents=[_agent()], mode=CollaborationMode.HIERARCHICAL)

    # Should not raise once a manager is supplied.
    Team(
        name="team",
        agents=[_agent()],
        mode=CollaborationMode.HIERARCHICAL,
        manager=_agent("boss"),
    )
