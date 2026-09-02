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
    # Structural validation (_build_pipeline) owns non-dict agents; not our job.
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


def test_validate_agent_models_exempts_fake_architect():
    raw_spec = {"agents": [{"name": "a", "model": "fake-architect:e2e"}]}
    assert validate_agent_models(raw_spec, catalog_specs=[]) == []


# ---------------------------------------------------------------------------
# Phase 0 (0.6): email access must not be combined with an egress tool.
#
# The draft-only toolkit's containment story is "the worst outcome is a bad
# draft a human reviews" -- there is no send verb. That only holds while the
# agent reading attacker-controlled email has no other route out. `http_get`
# is exactly such a route, and prompt-level defences are not a boundary.
# ---------------------------------------------------------------------------

from ui.backend.deploy_validation import EMAIL_TOOL_NAMES, find_email_egress_conflicts


def test_email_plus_http_get_on_one_agent_is_flagged():
    problems = find_email_egress_conflicts([("triager", {"email_read", "http_get"})])
    assert len(problems) == 1
    assert "triager" in problems[0] and "http_get" in problems[0]


def test_email_plus_web_search_is_flagged():
    problems = find_email_egress_conflicts([("triager", {"email_find", "web_search"})])
    assert len(problems) == 1 and "web_search" in problems[0]


def test_both_egress_tools_are_named_in_one_problem():
    problems = find_email_egress_conflicts(
        [("triager", {"email_read", "http_get", "web_search"})]
    )
    assert len(problems) == 1
    assert "http_get" in problems[0] and "web_search" in problems[0]


def test_email_alone_is_fine():
    assert find_email_egress_conflicts([("triager", {"email_read", "calculator"})]) == []


def test_egress_alone_is_fine():
    assert find_email_egress_conflicts([("researcher", {"http_get", "web_search"})]) == []


def test_the_capabilities_split_across_separate_agents_is_still_rejected():
    # Splitting was the ORIGINAL remedy and it does not work: `_agent_node`
    # passes each agent's output into the next agent's context, so an injected
    # instruction the mail agent read arrives in the egress agent's prompt.
    problems = find_email_egress_conflicts([
        ("triager", {"email_read", "email_draft_reply"}),
        ("researcher", {"http_get"}),
    ])
    assert len(problems) == 1
    assert "triager" in problems[0] and "researcher" in problems[0]


def test_attachment_reading_conflicts_with_an_egress_tool():
    # An injected attachment could otherwise direct the model to put mailbox
    # content into a URL it fetches -- exfiltration without ever sending mail.
    problems = find_email_egress_conflicts(
        [("triager", {"email_read_attachment", "http_get"})]
    )
    assert problems


def test_every_tool_the_email_toolkit_returns_is_treated_as_an_email_tool():
    # Structural, so the NEXT email tool cannot repeat Phase 4b's near-miss:
    # a tool absent from EMAIL_TOOL_NAMES is silently exempt from the
    # egress-conflict rule, with nothing failing anywhere.
    from bestteam.tools.email_client import make_email_tools

    class _Backend:
        pass

    assert set(make_email_tools(_Backend())) <= EMAIL_TOOL_NAMES


def test_the_conflict_is_reported_once_for_the_whole_pipeline():
    # One problem, not one per agent: the fault is the pipeline holding both
    # capabilities, and naming every pairing would just be noise.
    problems = find_email_egress_conflicts([
        ("a", {"email_read", "http_get"}),
        ("b", {"calculator"}),
        ("c", {"email_draft_reply", "web_search"}),
    ])
    assert len(problems) == 1
    assert "http_get" in problems[0] and "web_search" in problems[0]


# ---------------------------------------------------------------------------
# A shared instance must not let a customer's team read the server's disk.
#
# `parse_file` reads whatever local path it is given, with no sandbox, and the
# org isolation guarantee covers database rows, not the filesystem -- every
# org's uploads sit under one directory inside one container. The tool stays
# available to SDK/YAML users, who chose the paths; a deployed customer team
# is refused it.
# ---------------------------------------------------------------------------

from ui.backend.deploy_validation import LOCAL_FILE_TOOL_NAMES, find_local_file_tools


def test_parse_file_on_an_agent_is_flagged():
    problems = find_local_file_tools([("reader", {"parse_file", "calculator"})])
    assert len(problems) == 1
    assert "reader" in problems[0] and "parse_file" in problems[0]


def test_agents_without_a_local_file_tool_pass():
    assert find_local_file_tools([("a", {"calculator"}), ("b", {"web_search", "http_get"})]) == []


def test_every_agent_carrying_parse_file_is_named():
    problems = find_local_file_tools([("a", {"parse_file"}), ("b", {"parse_file"}), ("c", set())])
    assert len(problems) == 2
    assert "a" in problems[0] and "b" in problems[1]


def test_local_file_tool_names_are_registered_names():
    # The deploy gate and the architect catalog both key on this set, so it
    # has to spell the tool exactly as `REGISTRY` does.
    from bestteam.tools import REGISTRY

    assert LOCAL_FILE_TOOL_NAMES <= set(REGISTRY)
