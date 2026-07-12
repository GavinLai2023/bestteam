"""CJK-aware keyword tokenizer shared by BM25-backed search components.

Both the local-folder knowledge base (`core/knowledge_base.py`) and the
per-user memory store (`core/memory.py`) rank free text with `rank-bm25` and
need the same tokenization behavior — English word tokens plus a bigram
fallback for CJK runs, and a stopword set to keep tiny corpora from matching
on function words. Keeping it in one place avoids two copies drifting apart.
"""

from __future__ import annotations

import re
from typing import List, Set

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
# match as readily as truly related ones. Ranges (via \\u escapes to avoid
# any source-encoding ambiguity with raw CJK literals): CJK Unified
# Ideographs (U+4E00-U+9FFF), Extension A (U+3400-U+4DBF), and Compatibility
# Ideographs (U+F900-U+FAFF).
_CJK_RUN_RE = re.compile("[一-鿿㐀-䶿豈-﫿]+")


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
# too unstable at this scale to rely on alone.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
    "does", "for", "from", "has", "have", "how", "i", "if", "in", "into",
    "is", "it", "its", "of", "on", "or", "our", "should", "that", "the",
    "their", "this", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "will", "with", "you", "your",
})


def tokenize(text: str) -> List[str]:
    """Lowercase `text` into English word tokens plus CJK bigram tokens."""
    text = text.lower()
    return _TOKEN_RE.findall(text) + _cjk_tokens(text)


def significant_terms(tokens: List[str]) -> Set[str]:
    """The set of non-stopword tokens, used for keyword-overlap gating."""
    return {t for t in tokens if t not in _STOPWORDS}
