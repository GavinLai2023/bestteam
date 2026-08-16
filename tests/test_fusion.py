"""Tests for the shared RRF-fusion and query-expansion primitives."""
from unittest.mock import patch

import pytest

from bestteam.core.fusion import expand_query, reciprocal_rank_fusion

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion
# ---------------------------------------------------------------------------

def test_reciprocal_rank_fusion_combines_two_lists():
    scores = reciprocal_rank_fusion(["a", "b", "c"], ["c", "a"])
    assert scores["a"] > scores["b"]
    assert scores["c"] > scores["b"]


def test_reciprocal_rank_fusion_respects_custom_k():
    assert reciprocal_rank_fusion(["a"], k=1)["a"] == pytest.approx(1 / 2)


def test_reciprocal_rank_fusion_empty_lists():
    assert reciprocal_rank_fusion([], []) == {}


def test_reciprocal_rank_fusion_weights_default_matches_unweighted():
    unweighted = reciprocal_rank_fusion(["a", "b"], ["b", "a"])
    explicit = reciprocal_rank_fusion(["a", "b"], ["b", "a"], weights=[1.0, 1.0])
    assert unweighted == explicit


def test_reciprocal_rank_fusion_weighted_favors_higher_weight_list():
    list_a = ["x", "y"]
    list_b = ["y", "x"]
    unweighted = reciprocal_rank_fusion(list_a, list_b)
    assert unweighted["x"] == pytest.approx(unweighted["y"])
    weighted = reciprocal_rank_fusion(list_a, list_b, weights=(1.0, 30.0))
    assert weighted["y"] > weighted["x"]


def test_reciprocal_rank_fusion_accepts_non_string_ids():
    # A knowledge base fuses by integer chunk index, not a string id.
    scores = reciprocal_rank_fusion([0, 2], [2, 0])
    assert scores[0] == pytest.approx(scores[2])
    assert set(scores) == {0, 2}


def test_reciprocal_rank_fusion_single_list_preserves_order():
    # Proof that routing a single-query, single-leg retrieval path through
    # fusion is order-preserving (the invariant the KB refactor depends on).
    fused = reciprocal_rank_fusion([5, 1, 9, 2])
    ranked = [idx for idx, _score in sorted(fused.items(), key=lambda p: p[1], reverse=True)]
    assert ranked == [5, 1, 9, 2]


# ---------------------------------------------------------------------------
# expand_query
# ---------------------------------------------------------------------------

def test_expand_query_unset_model_returns_empty():
    expansions, response = expand_query(None, "refund", 3)
    assert expansions == []
    assert response is None


def test_expand_query_count_zero_returns_empty():
    expansions, response = expand_query("fake:{\"queries\": [\"alt\"]}", "refund", 0)
    assert expansions == []
    assert response is None


def test_expand_query_parses_alternatives():
    expansions, response = expand_query(
        'fake:{"queries": ["money back", "reimbursement"]}', "refund", 3
    )
    assert expansions == ["money back", "reimbursement"]
    assert response is not None


def test_expand_query_dedupes_against_original_and_caps_count():
    canned = '{"queries": ["Refund", "refund ", "alt one", "alt one", "alt two", "alt three"]}'
    expansions, _response = expand_query(f"fake:{canned}", "refund", 2)
    assert expansions == ["alt one", "alt two"]


def test_expand_query_unparseable_response_degrades_gracefully():
    expansions, response = expand_query("fake:sorry, not JSON", "refund", 3)
    assert expansions == []
    # The call happened (a caller may still want to meter it), just nothing parsed.
    assert response is not None


def test_expand_query_invoke_error_returns_none_response():
    with patch(
        "bestteam.adapters.langgraph_adapter._resolve_model",
        side_effect=RuntimeError("boom"),
    ):
        expansions, response = expand_query("fake:ignored", "refund", 3)
    assert expansions == []
    assert response is None
