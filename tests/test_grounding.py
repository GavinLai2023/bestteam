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
        searches=1,
        hit_count=1,
    )
    assert result.verified == 1
    assert result.unverified == []


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
        searches=1,
        hit_count=1,
    )
    assert result.unverified == ["invented.pdf"]


def test_filename_match_is_case_sensitive():
    result = check_grounding("See [source: Handbook.pdf].", ["handbook.pdf, p.3"], searches=1, hit_count=1)
    assert result.unverified == ["Handbook.pdf"]


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
