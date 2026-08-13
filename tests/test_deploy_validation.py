import pytest

from ui.backend.deploy_validation import validate_agent_models

pytestmark = pytest.mark.unit


def _spec(*models):
    return {"agents": [{"name": f"a{i}", "role": "r", "goal": "g", "model": m}
                       for i, m in enumerate(models)]}


def test_unknown_catalog_model_flagged():
    problems = validate_agent_models(_spec("openai:gpt-x"), {"openai:gpt-4o"})
    assert len(problems) == 1 and "openai:gpt-x" in problems[0]


def test_catalog_model_passes():
    assert validate_agent_models(_spec("openai:gpt-4o"), {"openai:gpt-4o"}) == []


def test_fake_specs_exempt_even_with_empty_catalog():
    assert validate_agent_models(_spec("fake:hi", "fake:ok"), set()) == []


def test_multiple_problems_aggregated():
    problems = validate_agent_models(_spec("m1", "m2"), set())
    assert len(problems) == 2


def test_missing_none_empty_and_nonstring_models_are_flagged():
    # Each agent's model is invalid; each yields one problem and 42 does not crash.
    spec = {"agents": [
        {"name": "missing", "role": "r", "goal": "g"},               # no model key
        {"name": "none", "role": "r", "goal": "g", "model": None},
        {"name": "empty", "role": "r", "goal": "g", "model": ""},
        {"name": "int", "role": "r", "goal": "g", "model": 42},
    ]}
    problems = validate_agent_models(spec, {"openai:gpt-4o"})
    assert len(problems) == 4
    assert all("has no model set" in p for p in problems)


def test_non_dict_agents_left_to_structural_validation():
    # Structural validation (_build_workflow) owns non-dict agents; not our job.
    assert validate_agent_models({"agents": [42]}, set()) == []
    assert validate_agent_models({}, set()) == []


from ui.backend.deploy_validation import find_kb_tool_collisions


def test_inline_kb_name_shadowing_builtin_flagged():
    raw = {"knowledge_bases": [{"name": "calculator", "path": "x"}], "agents": []}
    assert find_kb_tool_collisions(raw, [], {"calculator", "web_search"}) == ["calculator"]


def test_standalone_kb_name_shadowing_builtin_flagged():
    assert find_kb_tool_collisions({}, ["calculator"], {"calculator"}) == ["calculator"]


def test_non_colliding_kb_names_pass():
    raw = {"knowledge_bases": [{"name": "product_docs", "path": "x"}], "agents": []}
    assert find_kb_tool_collisions(raw, ["returns_policy"], {"calculator", "web_search"}) == []


def test_collisions_sorted_and_deduped():
    raw = {"knowledge_bases": [{"name": "web_search"}], "agents": []}
    assert find_kb_tool_collisions(raw, ["calculator", "web_search"], {"calculator", "web_search"}) == ["calculator", "web_search"]
