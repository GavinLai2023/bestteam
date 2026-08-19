"""CJK-aware keyword tokenizer shared by BM25-backed search components.

Both the local-folder knowledge base (`core/knowledge_base.py`) and the
per-user memory store (`core/memory.py`) rank free text with `rank-bm25` and
need the same tokenization behavior — stemmed English word tokens plus a
bigram fallback for CJK runs, and a stopword set to keep tiny corpora from
matching on function words. Keeping it in one place avoids two copies
drifting apart.

English stemming needs `snowballstemmer` (in the `tools-rag` and `tools`
extras). The import is soft: without the package `_stem` is the identity, so
every caller still tokenizes symmetrically on the index and the query side
within one process — retrieval is simply less forgiving of inflections. The
soft import also keeps `core/embeddings.py`, which imports this module only
for `_CJK_RUN_RE`, working in an install without the extra.
"""

from __future__ import annotations

import functools
import re
import threading
from typing import List, Set

try:
    import snowballstemmer
except ImportError:  # pragma: no cover - exercised only without the extra
    snowballstemmer = None  # type: ignore[assignment]

# A Snowball stemmer object carries per-call mutable state (`stemWord` sets
# the current word on the instance, stems it, then reads it back), so one
# shared instance would corrupt results across the backend's concurrent
# worker threads. One instance per thread, created on first use.
_STEMMER_LOCAL = threading.local()


# Stemming a token costs roughly 150x tokenizing it, and both sides of BM25
# reuse a small vocabulary constantly (every chunk of a corpus at index time,
# every query at search time), so memoize. Bounded rather than unbounded: the
# backend process is long-lived and these strings come from customer
# documents, so an unbounded cache would be a slow leak. Safe to share across
# threads even though the stemmer object is not -- the function is pure, and
# the worst a concurrent miss does is stem the same token twice.
@functools.lru_cache(maxsize=100_000)
def _stem(token: str) -> str:
    """The English stem of `token`, or `token` itself without the extra.

    Digit tokens pass through Snowball unchanged, so no special-casing is
    needed for them.
    """
    if snowballstemmer is None:  # pragma: no cover - exercised without the extra
        return token
    stemmer = getattr(_STEMMER_LOCAL, "stemmer", None)
    if stemmer is None:
        stemmer = _STEMMER_LOCAL.stemmer = snowballstemmer.stemmer("english")
    return stemmer.stemWord(token)


_TOKEN_RE = re.compile(r"[a-z0-9]+")

# CJK (Chinese/Japanese/Korean) text has no whitespace between words, so the
# word-boundary `_TOKEN_RE` above never matches it -- every all-CJK query
# tokenizes to nothing and silently never matches any document. There's no
# segmentation library here (by design -- see core/CLAUDE.md's "no API key,
# no vector store" rationale for this knowledge base type), so each maximal
# run of CJK characters is split into overlapping bigrams (a lone character
# becomes its own single-character token) -- a common cheap fallback for
# keyword-overlap search without a proper word segmenter. Plain
# single-character unigrams were tried first and rejected: they make
# unrelated chunks that merely share one common character (e.g. a return
# policy and a shipping policy both containing the character for "goods")
# match as readily as truly related ones. Ranges: CJK Unified Ideographs
# (U+4E00-U+9FFF), Extension A (U+3400-U+4DBF), Compatibility Ideographs
# (U+F900-U+FAFF), Hiragana and Katakana (U+3040-U+30FF), and Hangul
# Syllables (U+AC00-U+D7AF). Japanese mixes kanji and kana inside one word,
# so those are deliberately one character class rather than two: a run is cut
# at the script boundary only if the scripts are in different classes, and a
# bigram straddling that boundary carries more signal than the two fragments
# it would otherwise be split into.
# Written as `\u` escapes, never as literal characters: an NFC-normalising
# editor once rewrote the literal U+F900 (a compatibility ideograph) as its
# unified equivalent U+8C48 and silently widened the class to
# U+8C48-U+FAFF. `tests/test_text_tokenize.py` pins every range endpoint.
_CJK_RUN_RE = re.compile(
    "[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+"
)


def _cjk_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


# Common English function words, ignored when deciding whether a chunk is
# relevant to a query. Without this, tiny corpora (a handful of documents)
# can match every chunk on words like "and"/"the", and BM25's IDF term is
# too unstable at this scale to rely on alone. Stemmed with the same stemmer
# `tokenize` uses, otherwise a stemmed query token ("does" -> "doe") would no
# longer match its raw entry here and function words would survive the gate.
_RAW_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
    "does", "for", "from", "has", "have", "how", "i", "if", "in", "into",
    "is", "it", "its", "of", "on", "or", "our", "should", "that", "the",
    "their", "this", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "will", "with", "you", "your",
}
_STOPWORDS = frozenset(_stem(word) for word in _RAW_STOPWORDS)


def tokenize(text: str) -> List[str]:
    """Lowercase `text` into stemmed English word tokens plus CJK bigram
    tokens."""
    text = text.lower()
    return [_stem(token) for token in _TOKEN_RE.findall(text)] + _cjk_tokens(text)


def significant_terms(tokens: List[str]) -> Set[str]:
    """The set of non-stopword tokens, used for keyword-overlap gating."""
    return {t for t in tokens if t not in _STOPWORDS}
