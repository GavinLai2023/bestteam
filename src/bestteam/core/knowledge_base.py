from __future__ import annotations

import re
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional, Set

from ..exceptions import ConfigurationError
from ..tools import parse_file

_SUPPORTED_SUFFIXES = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log",
    ".pdf", ".xlsx", ".xls", ".xlsm", ".docx",
}

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


class KnowledgeBase(ABC):
    """A queryable source of client documents that agents can search.

    Implementations are responsible for ingesting documents from wherever
    they live and answering free-text queries with relevant excerpts.
    """

    name: str

    @abstractmethod
    def query(self, query: str, top_k: Optional[int] = None) -> str:
        """Return formatted excerpts most relevant to the query."""


class _Chunk(NamedTuple):
    source: str
    text: str


class LocalFolderKnowledgeBase(KnowledgeBase):
    """A knowledge base backed by a folder of documents on disk.

    Documents are parsed (via :func:`bestteam.tools.parse_file`), split into
    overlapping chunks, and indexed in memory with BM25 keyword search. This
    is intentionally lightweight — no embeddings, no vector store, no API
    keys — which suits the common case of a client handing over a folder
    with a handful to a couple dozen documents.
    """

    def __init__(
        self,
        name: str,
        path: str | Path,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        top_k: int = 5,
    ) -> None:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise ConfigurationError(
                "Knowledge bases require the 'rank-bm25' package. "
                "Install it with: pip install 'bestteam[tools-rag]'"
            ) from exc

        _validate_chunk_params(name, chunk_size, chunk_overlap)

        self.name = name
        self.path = Path(path)
        self.default_top_k = top_k

        self._chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        if not self._chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents in {self.path}"
            )

        self._chunk_tokens = [self._tokenize(chunk.text) for chunk in self._chunks]
        self._chunk_terms = [self._significant_terms(tokens) for tokens in self._chunk_tokens]
        self._bm25 = BM25Okapi(self._chunk_tokens)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        text = text.lower()
        return _TOKEN_RE.findall(text) + _cjk_tokens(text)

    @staticmethod
    def _significant_terms(tokens: List[str]) -> Set[str]:
        return {t for t in tokens if t not in _STOPWORDS}

    def query(self, query: str, top_k: Optional[int] = None) -> str:
        top_k = top_k or self.default_top_k
        query_tokens = self._tokenize(query)
        query_terms = self._significant_terms(query_tokens)
        scores = self._bm25.get_scores(query_tokens)

        matches = [
            (len(query_terms & chunk_terms), score, chunk)
            for score, chunk, chunk_terms in zip(scores, self._chunks, self._chunk_terms)
            if query_terms & chunk_terms
        ]
        matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
        results = [(score, chunk) for _overlap, score, chunk in matches[:top_k]]

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"

        lines = [f"Knowledge base '{self.name}' results for: {query}\n"]
        for i, (_score, chunk) in enumerate(results, 1):
            lines.append(f"{i}. [source: {chunk.source}]")
            lines.append(chunk.text.strip())
            lines.append("")
        return "\n".join(lines)


def _chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    """Split text into overlapping fixed-size chunks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    step = chunk_size - chunk_overlap
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        start += step
    return chunks


def _validate_chunk_params(name: str, chunk_size: int, chunk_overlap: int) -> None:
    if chunk_size <= 0:
        raise ConfigurationError(
            f"Knowledge base '{name}': chunk_size must be positive, got {chunk_size}"
        )
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ConfigurationError(
            f"Knowledge base '{name}': chunk_overlap ({chunk_overlap}) must be "
            f"non-negative and less than chunk_size ({chunk_size})"
        )


def _load_document_chunks(path: Path, chunk_size: int, chunk_overlap: int) -> List[_Chunk]:
    chunks: List[_Chunk] = []
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file() or file_path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        try:
            text = parse_file(str(file_path))
        except ConfigurationError as exc:
            warnings.warn(f"Skipping unreadable file '{file_path}': {exc}", stacklevel=2)
            continue
        except Exception as exc:
            warnings.warn(f"Skipping unreadable file '{file_path}': {exc}", stacklevel=2)
            continue
        source = file_path.relative_to(path).as_posix()
        for piece in _chunk_text(text, chunk_size, chunk_overlap):
            chunks.append(_Chunk(source=source, text=piece))
    return chunks


def make_knowledge_base_tool(kb: KnowledgeBase) -> Callable[[str], str]:
    """Wrap a :class:`KnowledgeBase` as a single-argument agent tool.

    The returned callable's ``__name__`` matches ``kb.name``, so it can be
    referenced directly by name in a workflow's ``tools:`` list — exactly
    like a built-in tool.
    """

    def _tool(query: str) -> str:
        return kb.query(query)

    _tool.__name__ = kb.name
    _tool.__doc__ = (
        f"Search the '{kb.name}' knowledge base for information relevant to "
        "the query. Returns the most relevant document excerpts along with "
        "the source file each excerpt came from.\n\n"
        "Args:\n"
        "    query: The search query string.\n\n"
        "Returns:\n"
        "    Formatted text containing matching excerpts and their sources."
    )
    return _tool
