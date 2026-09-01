import pytest

from bestteam.core.grounding import (
    MAX_LABEL_CHARS,
    MAX_UNVERIFIED,
    GroundingResult,
    check_grounding,
)

pytestmark = pytest.mark.unit


def test_exact_label_is_verified():
    result = check_grounding(
        "Refunds take 14 days [source: handbook.pdf, p.3 § Refunds].",
        ["handbook.pdf, p.3 § Refunds", "policies.md"],
        searches=1,
        hit_count=2,
    )
    assert result == GroundingResult(searches=1, hit_count=2, cited=1, verified=1, unverified=[])


def test_whitespace_differences_do_not_make_a_label_unverified():
    result = check_grounding(
        "See [source:  handbook.pdf,  p.3 §  Refunds ].",
        ["handbook.pdf, p.3 § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.cited == 1
    assert result.verified == 1
    assert result.unverified == []


def test_filename_only_tag_is_verified_when_that_document_was_returned():
    result = check_grounding(
        "As the handbook says [source: handbook.pdf].",
        ["handbook.pdf, p.3 § Refunds"],
        documents=["handbook.pdf"],
        searches=1,
        hit_count=1,
    )
    assert result.verified == 1
    assert result.unverified == []


def test_filename_only_tag_needs_the_reported_document_names():
    # `documents` defaults to empty, so a knowledge-base tool that reports
    # `citations` but not `citation_documents` (only a hand-written custom one
    # can) loses the filename-only rule rather than having the filename
    # guessed out of a label. Stricter, and never wrong.
    result = check_grounding(
        "As the handbook says [source: handbook.pdf].",
        ["handbook.pdf, p.3 § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.unverified == ["handbook.pdf"]


def test_tag_with_an_unreturned_page_is_unverified():
    result = check_grounding(
        "See [source: handbook.pdf, p.99].",
        ["handbook.pdf, p.3 § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.cited == 1
    assert result.verified == 0
    assert result.unverified == ["handbook.pdf, p.99"]


def test_tag_with_an_unreturned_heading_is_unverified():
    result = check_grounding(
        "See [source: policies.md § Holidays].",
        ["policies.md § Refunds"],
        searches=1,
        hit_count=1,
    )
    assert result.unverified == ["policies.md § Holidays"]


def test_filename_only_tag_for_a_document_never_returned_is_unverified():
    result = check_grounding(
        "See [source: invented.pdf].",
        ["handbook.pdf, p.3"],
        documents=["handbook.pdf"],
        searches=1,
        hit_count=1,
    )
    assert result.unverified == ["invented.pdf"]


def test_filename_match_is_case_sensitive():
    result = check_grounding(
        "See [source: Handbook.pdf].",
        ["handbook.pdf, p.3"],
        documents=["handbook.pdf"],
        searches=1,
        hit_count=1,
    )
    assert result.unverified == ["Handbook.pdf"]


def test_a_filename_containing_a_locator_marker_is_verified_when_cited_in_full():
    # An upload legitimately named "report, p.2.pdf". The check used to split
    # the label at the first ", p." and treat the remainder as a page it never
    # returned, so a perfectly correct citation was reported unverified --
    # which, under `refuse`, is a wrong refusal rather than trace noise.
    result = check_grounding(
        "See [source: report, p.2.pdf § Summary].",
        ["report, p.2.pdf § Summary"],
        documents=["report, p.2.pdf"],
        searches=1,
        hit_count=1,
    )
    assert result.verified == 1
    assert result.unverified == []


def test_a_bare_filename_prefix_of_a_locator_named_document_is_unverified():
    # The other half of the same defect: "report" is not the document's name,
    # it is the part before the marker the old split guessed at.
    result = check_grounding(
        "See [source: report].",
        ["report, p.2.pdf § Summary"],
        documents=["report, p.2.pdf"],
        searches=1,
        hit_count=1,
    )
    assert result.verified == 0
    assert result.unverified == ["report"]


def test_repeated_label_counts_once_and_keeps_first_appearance_order():
    text = (
        "A [source: b.md, p.2]. B [source: a.md]. C [source: b.md, p.2]. D [source: c.md]."
    )
    result = check_grounding(text, ["a.md"], searches=1, hit_count=1)
    assert result.cited == 3
    assert result.verified == 1
    assert result.unverified == ["b.md, p.2", "c.md"]


def test_no_tags_is_zero_cited_even_with_hits():
    result = check_grounding("Plain answer.", ["handbook.pdf"], searches=1, hit_count=1)
    assert result == GroundingResult(searches=1, hit_count=1, cited=0, verified=0, unverified=[])


def test_empty_text_and_no_citations_is_valid():
    result = check_grounding("", [], searches=0, hit_count=0)
    assert result == GroundingResult(searches=0, hit_count=0, cited=0, verified=0, unverified=[])


def test_tags_with_no_search_are_all_unverified():
    result = check_grounding("[source: a.md] [source: b.md]", [], searches=0, hit_count=0)
    assert result.cited == 2
    assert result.verified == 0
    assert result.unverified == ["a.md", "b.md"]


def test_empty_tag_is_ignored():
    result = check_grounding("[source: ] [source:   ]", ["a.md"], searches=1, hit_count=1)
    assert result.cited == 0
    assert result.unverified == []


def test_unverified_is_capped_and_each_label_truncated():
    labels = [f"doc{i}.pdf, p.{i}" for i in range(15)]
    long_label = "x" * 500
    text = " ".join(f"[source: {label}]" for label in [*labels, long_label])
    result = check_grounding(text, [], searches=0, hit_count=0)
    assert result.cited == 16
    assert len(result.unverified) == MAX_UNVERIFIED == 10
    assert result.unverified == labels[:10]

    only_long = check_grounding(f"[source: {long_label}]", [], searches=0, hit_count=0)
    assert only_long.unverified == ["x" * MAX_LABEL_CHARS]
    assert MAX_LABEL_CHARS == 200


def test_non_string_content_is_treated_as_no_text_not_an_exception():
    # Some providers hand back response.content as a list of content blocks
    # instead of a plain str. `text or ""` would not catch this (a non-empty
    # list is truthy), so check_grounding must guard the type explicitly
    # rather than let re.findall raise TypeError and fail the whole run.
    blocks = [{"type": "text", "text": "See [source: handbook.pdf]."}]
    result = check_grounding(blocks, ["handbook.pdf, p.3"], searches=1, hit_count=1)
    assert result == GroundingResult(searches=1, hit_count=1, cited=0, verified=0, unverified=[])


def test_as_trace_data_shape():
    result = check_grounding("[source: a.md] [source: z.md]", ["a.md"], searches=2, hit_count=5)
    assert result.as_trace_data() == {
        "searches": 2,
        "hit_count": 5,
        "cited": 2,
        "verified": 1,
        "unverified": ["z.md"],
    }
    # A fresh list each time -- callers must not be able to mutate the result.
    assert result.as_trace_data()["unverified"] is not result.unverified


def test_passes_requires_a_citation_and_no_unverified_labels():
    def result(cited, verified, unverified):
        return GroundingResult(
            searches=1, hit_count=3, cited=cited, verified=verified, unverified=unverified
        )

    assert result(2, 2, []).passes is True
    assert result(0, 0, []).passes is False, "an answer with no citations fails"
    assert result(2, 1, ["made-up.pdf"]).passes is False, "a fabricated tag fails"


def _pipeline_yaml(policy_line: str) -> str:
    return (
        "name: demo\n"
        "agents:\n"
        "  - name: a\n"
        "    role: Helper\n"
        "    goal: Answer\n"
        '    model: "fake:done"\n'
        f"    {policy_line}\n"
        "teams:\n"
        "  - name: t\n"
        "    agents: [a]\n"
        "    mode: sequential\n"
        "pipeline:\n"
        "  steps: [t]\n"
    )


def test_yaml_grounding_policy_reaches_the_agent(tmp_path):
    from bestteam.core.loader import load_pipeline

    path = tmp_path / "p.yaml"
    path.write_text(_pipeline_yaml("grounding_policy: retry"), encoding="utf-8")

    agent = load_pipeline(path).steps[0].agents[0]
    assert agent.grounding_policy == "retry"


def test_yaml_unknown_grounding_policy_is_a_configuration_error(tmp_path):
    from bestteam.core.loader import load_pipeline
    from bestteam.exceptions import ConfigurationError

    path = tmp_path / "p.yaml"
    path.write_text(_pipeline_yaml("grounding_policy: enforce"), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="grounding_policy"):
        load_pipeline(path)


def test_yaml_grounding_level_and_model_reach_the_agent(tmp_path):
    from bestteam.core.loader import load_pipeline

    path = tmp_path / "p.yaml"
    path.write_text(
        _pipeline_yaml('grounding_level: claim\n    grounding_model: "fake:ok"'),
        encoding="utf-8",
    )

    agent = load_pipeline(path).steps[0].agents[0]
    assert agent.grounding_level == "claim"
    assert agent.grounding_model == "fake:ok"


def test_yaml_unknown_grounding_level_is_a_configuration_error(tmp_path):
    from bestteam.core.loader import load_pipeline
    from bestteam.exceptions import ConfigurationError

    path = tmp_path / "p.yaml"
    path.write_text(_pipeline_yaml("grounding_level: strict"), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="grounding_level"):
        load_pipeline(path)


# ---------------------------------------------------------------------------
# Claim-level grading (grade_claims): one plain LLM call splits the answer
# into factual claims and judges each against the turn's search results.
# ---------------------------------------------------------------------------

from langchain_core.language_models.fake_chat_models import FakeListChatModel  # noqa: E402

from bestteam.core.grounding import (  # noqa: E402
    GROUNDING_RETRY_INSTRUCTION,
    MAX_CLAIMS,
    ClaimGrading,
    claim_retry_instruction,
    grade_claims,
)


def _grader(response_text):
    return FakeListChatModel(responses=[response_text])


_EVIDENCE = ["[source: handbook.pdf, p.3 § Refunds]\nRefunds are processed within 14 days."]


def test_grade_claims_parses_a_clean_json_response():
    grading, response = grade_claims(
        "Refunds take 14 days.",
        _EVIDENCE,
        _grader('{"claims": [{"text": "Refunds take 14 days.", "supported": true}]}'),
    )
    assert grading == ClaimGrading(claims=1, supported=1, unsupported=[])
    assert grading.passes is True
    assert response is not None


def test_grade_claims_reports_unsupported_claims():
    grading, _ = grade_claims(
        "Refunds take 14 days. Shipping is free.",
        _EVIDENCE,
        _grader(
            '{"claims": [{"text": "Refunds take 14 days.", "supported": true},'
            ' {"text": "Shipping is free.", "supported": false}]}'
        ),
    )
    assert grading.claims == 2
    assert grading.supported == 1
    assert grading.unsupported == ["Shipping is free."]
    assert grading.passes is False


def test_grade_claims_tolerates_code_fences_and_prose():
    grading, _ = grade_claims(
        "Answer.",
        _EVIDENCE,
        _grader('Here you go:\n```json\n{"claims": []}\n```'),
    )
    assert grading == ClaimGrading(claims=0, supported=0, unsupported=[])
    assert grading.passes is True, "an answer with no factual claims passes"


def test_grade_claims_skips_malformed_entries():
    grading, _ = grade_claims(
        "Answer.",
        _EVIDENCE,
        _grader(
            '{"claims": ["not a dict", {"supported": true}, {"text": "", "supported": false},'
            ' {"text": "Real claim.", "supported": "yes"}, {"text": "Good.", "supported": true}]}'
        ),
    )
    assert grading == ClaimGrading(claims=1, supported=1, unsupported=[])


def test_grade_claims_caps_the_claim_list():
    entries = ", ".join(f'{{"text": "c{i}", "supported": false}}' for i in range(30))
    grading, _ = grade_claims("Answer.", _EVIDENCE, _grader(f'{{"claims": [{entries}]}}'))
    assert grading.claims == MAX_CLAIMS == 20
    assert len(grading.unsupported) == 10, "unsupported reuses the MAX_UNVERIFIED bound"


def test_grade_claims_truncates_long_claim_texts():
    long_claim = "x" * 500
    grading, _ = grade_claims(
        "Answer.",
        _EVIDENCE,
        _grader(f'{{"claims": [{{"text": "{long_claim}", "supported": false}}]}}'),
    )
    assert grading.unsupported == ["x" * 200]


def test_grade_claims_unparseable_response_returns_none_with_the_response():
    grading, response = grade_claims("Answer.", _EVIDENCE, _grader("I cannot answer that."))
    assert grading is None
    assert response is not None, "the call was billed, so the caller must be able to meter it"


def test_grade_claims_invoke_error_returns_none_none():
    class _Boom:
        def invoke(self, messages):
            raise RuntimeError("provider down")

    grading, response = grade_claims("Answer.", _EVIDENCE, _Boom())
    assert grading is None
    assert response is None


def test_grade_claims_non_string_text_is_treated_as_empty():
    grading, _ = grade_claims(
        [{"type": "text", "text": "blocks"}],
        _EVIDENCE,
        _grader('{"claims": []}'),
    )
    assert grading == ClaimGrading(claims=0, supported=0, unsupported=[])


def test_claim_retry_instruction_names_the_unsupported_claims():
    instruction = claim_retry_instruction(["Shipping is free.", "Returns cost nothing."])
    assert instruction.startswith(GROUNDING_RETRY_INSTRUCTION)
    assert "Shipping is free." in instruction
    assert "Returns cost nothing." in instruction
