"""Tests for `core/embeddings.py`'s batched, retrying document embedding."""

from typing import List, Optional, Sequence

import pytest
from langchain_core.embeddings import Embeddings

from bestteam.core.embeddings import _EMBED_BATCH_SIZE, embed_documents_in_batches
from bestteam.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


def _vector_for(text: str) -> List[float]:
    """A deterministic one-dimensional vector, so a test can tell which text a
    returned vector was computed from and therefore assert batch ordering."""
    return [float(sum(ord(char) for char in text))]


class _RecordingEmbeddings(Embeddings):
    """Records every `embed_documents` call, and can be told to fail.

    `fail_on_text` marks a batch: a call whose texts include it raises, up to
    `fail_times` times in total. `short_by` makes every call return that many
    vectors fewer than it was given texts.
    """

    def __init__(
        self,
        *,
        fail_on_text: Optional[str] = None,
        fail_times: int = 0,
        short_by: int = 0,
    ) -> None:
        self.calls: List[List[str]] = []
        self._fail_on_text = fail_on_text
        self._fail_times = fail_times
        self._failures = 0
        self._short_by = short_by

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        batch = list(texts)
        self.calls.append(batch)
        if self._fail_on_text in batch and self._failures < self._fail_times:
            self._failures += 1
            raise RuntimeError(f"provider exploded (failure {self._failures})")
        vectors = [_vector_for(text) for text in batch]
        return vectors[: len(vectors) - self._short_by] if self._short_by else vectors

    def embed_query(self, text: str) -> List[float]:  # pragma: no cover - unused
        return _vector_for(text)


def _texts(count: int) -> List[str]:
    return [f"chunk number {i}" for i in range(count)]


def test_batches_split_by_size_and_preserve_order():
    texts = _texts(250)
    fake = _RecordingEmbeddings()

    vectors = embed_documents_in_batches(fake, texts)

    assert [len(call) for call in fake.calls] == [100, 100, 50]
    assert _EMBED_BATCH_SIZE == 100
    assert [text for call in fake.calls for text in call] == texts
    assert vectors == [_vector_for(text) for text in texts]


def test_only_the_failing_batch_is_retried():
    texts = _texts(150)
    fake = _RecordingEmbeddings(fail_on_text=texts[100], fail_times=1)
    sleeps: List[float] = []

    vectors = embed_documents_in_batches(fake, texts, sleep=sleeps.append)

    # First batch embedded once; the second batch failed once and was retried.
    assert [len(call) for call in fake.calls] == [100, 50, 50]
    assert sum(1 for call in fake.calls if texts[0] in call) == 1
    assert sleeps == [1.0]
    assert vectors == [_vector_for(text) for text in texts]


def test_gives_up_after_three_attempts_and_raises_the_last_error():
    texts = _texts(3)
    fake = _RecordingEmbeddings(fail_on_text=texts[0], fail_times=99)
    sleeps: List[float] = []

    with pytest.raises(RuntimeError, match="provider exploded \\(failure 3\\)"):
        embed_documents_in_batches(fake, texts, sleep=sleeps.append)

    assert len(fake.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_short_batch_result_is_rejected_without_retry():
    texts = _texts(3)
    fake = _RecordingEmbeddings(short_by=1)
    sleeps: List[float] = []

    with pytest.raises(ConfigurationError, match="returned 2 vectors for 3 texts"):
        embed_documents_in_batches(fake, texts, sleep=sleeps.append)

    assert len(fake.calls) == 1
    assert sleeps == []


def test_empty_input_makes_no_call():
    fake = _RecordingEmbeddings()

    assert embed_documents_in_batches(fake, []) == []
    assert fake.calls == []
