from ui.backend.deploy_validation import validate_agent_models


def _spec(*models):
    return {"agents": [{"name": f"a{i}", "role": "r", "goal": "g", "model": m}
                       for i, m in enumerate(models)]}


def test_unknown_model_flagged():
    assert validate_agent_models(_spec("openai:gpt-x"), {"openai:gpt-4o"}) == ["openai:gpt-x"]


def test_catalog_model_passes():
    assert validate_agent_models(_spec("openai:gpt-4o"), {"openai:gpt-4o"}) == []


def test_fake_specs_exempt_even_with_empty_catalog():
    assert validate_agent_models(_spec("fake:hi", "fake:ok"), set()) == []


def test_multiple_unknowns_aggregated_and_deduped():
    assert validate_agent_models(_spec("m1", "m2", "m1"), set()) == ["m1", "m2"]


def test_missing_or_malformed_agents_ignored():
    assert validate_agent_models({}, set()) == []
    assert validate_agent_models({"agents": [42, {"name": "a"}]}, set()) == []
