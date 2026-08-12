"""Tests for pluggable rerank (core/reranking.py)."""
import pytest

from bestteam.core.reranking import Reranker, _FakeReranker, _RerankScoringError


class _StubReranker(Reranker):
    def __init__(self, scores):
        self._scores = scores

    def _score(self, query, texts):
        return self._scores


def test_score_empty_texts_returns_empty_without_calling_score_impl():
    calls = []

    class _SpyReranker(Reranker):
        def _score(self, query, texts):
            calls.append(texts)
            return []

    assert _SpyReranker().score("q", []) == []
    assert calls == []


def test_score_rejects_count_mismatch():
    reranker = _StubReranker([1.0, 2.0])  # 2 scores for 3 texts
    with pytest.raises(_RerankScoringError, match="2 scores for 3 texts"):
        reranker.score("q", ["a", "b", "c"])


def test_score_rejects_non_finite():
    reranker = _StubReranker([1.0, float("nan")])
    with pytest.raises(_RerankScoringError, match="non-finite"):
        reranker.score("q", ["a", "b"])

    reranker = _StubReranker([1.0, float("inf")])
    with pytest.raises(_RerankScoringError, match="non-finite"):
        reranker.score("q", ["a", "b"])


def test_score_returns_floats():
    reranker = _StubReranker([1, 2])  # ints, must coerce to float
    scores = reranker.score("q", ["a", "b"])
    assert scores == [1.0, 2.0]
    assert all(isinstance(s, float) for s in scores)


def test_fake_reranker_scores_by_length_distance():
    reranker = _FakeReranker()
    scores = reranker.score("abc", ["abc", "abcdefgh", "ab"])
    # Exact match (len 3 vs query len 3) scores highest (0); farther lengths score lower.
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]


def test_fake_reranker_does_not_call_model_for_empty_input():
    assert _FakeReranker().score("q", []) == []


from bestteam.core.reranking import resolve_reranker
from bestteam.exceptions import ConfigurationError


def test_resolve_reranker_passthrough_instance():
    reranker = _FakeReranker()
    assert resolve_reranker(reranker) is reranker


def test_resolve_reranker_fake_spec():
    reranker = resolve_reranker("fake:")
    assert isinstance(reranker, _FakeReranker)


def test_resolve_reranker_unrecognized_string_spec():
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        resolve_reranker("openai:gpt-4o-mini")


def test_resolve_reranker_invalid_type():
    with pytest.raises(ConfigurationError, match="Unsupported reranker spec"):
        resolve_reranker(123)


from unittest.mock import MagicMock, patch


def test_resolve_reranker_missing_sentence_transformers():
    with patch.dict("sys.modules", {"sentence_transformers": None}):
        with pytest.raises(ConfigurationError, match="sentence-transformers"):
            resolve_reranker("cross-encoder:some-model")


def test_resolve_reranker_cross_encoder_caches_across_calls():
    pytest.importorskip("sentence_transformers")
    with patch("sentence_transformers.CrossEncoder") as mock_cls:
        mock_cls.return_value = MagicMock()
        first = resolve_reranker("cross-encoder:test-model")
        second = resolve_reranker("cross-encoder:test-model")
    assert first is second
    mock_cls.assert_called_once_with("test-model")


def test_resolve_reranker_cross_encoder_different_specs_not_shared():
    pytest.importorskip("sentence_transformers")
    with patch("sentence_transformers.CrossEncoder") as mock_cls:
        mock_cls.side_effect = lambda name: MagicMock(name=name)
        first = resolve_reranker("cross-encoder:model-a")
        second = resolve_reranker("cross-encoder:model-b")
    assert first is not second
    assert mock_cls.call_count == 2


def test_cross_encoder_reranker_scores_via_predict():
    pytest.importorskip("sentence_transformers")
    with patch("sentence_transformers.CrossEncoder") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.predict.return_value = [0.9, 0.1]
        mock_cls.return_value = mock_instance
        reranker = resolve_reranker("cross-encoder:unique-model-for-this-test")
        scores = reranker.score("q", ["a", "b"])
    assert scores == [0.9, 0.1]
    mock_instance.predict.assert_called_once_with([("q", "a"), ("q", "b")])
