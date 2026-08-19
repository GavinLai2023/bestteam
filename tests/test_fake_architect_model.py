"""Tests for the fake-architect: model spec (see
docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md)."""
import pytest

from bestteam import Requirements, Specification
from bestteam.adapters.langgraph_adapter import _resolve_model

pytestmark = pytest.mark.unit


def test_fake_architect_resolves_to_a_chat_model():
    model = _resolve_model("fake-architect:e2e")
    result = model.invoke("hello")
    assert result.content  # a non-empty AIMessage, like an ordinary fake: model


def test_fake_architect_with_structured_output_returns_canned_specification():
    model = _resolve_model("fake-architect:e2e")
    spec = model.with_structured_output(Specification).invoke([])
    assert isinstance(spec, Specification)
    assert spec.agents
    assert spec.teams
    assert spec.pipeline.steps


def test_fake_architect_with_structured_output_returns_canned_requirements():
    model = _resolve_model("fake-architect:e2e")
    req = model.with_structured_output(Requirements).invoke([])
    assert isinstance(req, Requirements)
    assert req.summary


def test_fake_architect_rejects_unknown_schema():
    model = _resolve_model("fake-architect:e2e")

    class SomeOtherSchema:
        pass

    with pytest.raises(NotImplementedError):
        model.with_structured_output(SomeOtherSchema)


def test_fake_architect_specification_agents_use_a_deployable_model():
    """The canned Specification's own agents must carry a model that's
    exempt from catalog validation on its own (deploy_validation.py) --
    independent of whatever re-pinning a caller does afterward."""
    model = _resolve_model("fake-architect:e2e")
    spec = model.with_structured_output(Specification).invoke([])
    assert all(agent.model.startswith("fake:") for agent in spec.agents)
