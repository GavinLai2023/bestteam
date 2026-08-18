"""Tests for the shared BM25 tokeniser (`core/text_tokenize.py`).

The same `tokenize` runs on both the index and the query side of the
local-folder knowledge base, the hybrid knowledge base and per-user memory
recall, so anything asserted here holds for all three.
"""
import pytest

from bestteam.core.text_tokenize import significant_terms, tokenize

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# English stemming
# ---------------------------------------------------------------------------

def test_english_inflections_share_a_stem():
    """A query for "refund" must reach a document that only says
    "refunds"/"refunded" -- the whole point of stemming."""
    stems = {tokenize(word)[0] for word in ("refund", "refunds", "refunded")}

    assert len(stems) == 1, stems


def test_stopwords_are_filtered_after_stemming():
    """The stopword set is stemmed with the same stemmer, otherwise a stemmed
    query token ("does" -> "doe") no longer matches the raw stopword entry and
    function words start surviving into the overlap gate."""
    assert significant_terms(tokenize("does it")) == set()


def test_digits_pass_through_unchanged():
    assert tokenize("order 30 days by 2026") == ["order", "30", "day", "by", "2026"]


# ---------------------------------------------------------------------------
# CJK runs: Han, kana and Hangul
# ---------------------------------------------------------------------------

def test_kana_and_hangul_runs_become_bigrams():
    """Kana and Hangul have no whitespace word boundaries either, so they get
    the same overlapping-bigram treatment as Han characters."""
    assert tokenize("ひらがな") == ["ひら", "らが", "がな"]
    assert tokenize("カタカナ") == ["カタ", "タカ", "カナ"]
    assert tokenize("한국어") == ["한국", "국어"]


def test_kanji_and_kana_form_one_run():
    """Japanese mixes kanji and kana inside a single word, so the run must not
    be cut at the script boundary -- "返品する" yields the boundary-straddling
    bigram "品す" rather than two separate runs."""
    assert tokenize("返品する") == ["返品", "品す", "する"]


# ---------------------------------------------------------------------------
# Index/query symmetry
# ---------------------------------------------------------------------------

def test_tokenize_is_symmetric_for_index_and_query():
    """Snowball's stemmer object is stateful, so tokenising other text in
    between must not change what a repeated string tokenises to -- index-side
    and query-side calls are the same function at different times."""
    text = "Refunds are issued within 30 days 返品する"

    first = tokenize(text)
    tokenize("unrelated shipping enquiries 配送時間")

    assert tokenize(text) == first
