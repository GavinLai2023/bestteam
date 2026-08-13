from __future__ import annotations

import logging
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, List, NamedTuple, Optional

from ..exceptions import ConfigurationError
from ..tools import parse_file
from .reranking import (
    _MAX_RERANK_CANDIDATE_K,
    _resolve_candidate_k,
    Reranker,
    resolve_reranker,
)
from .text_tokenize import significant_terms, tokenize

_logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log",
    ".pdf", ".xlsx", ".xlsm", ".docx", ".xml",
}


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
        rerank_model: Any = None,
        candidate_k: Optional[int] = None,
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
        self._reranker = resolve_reranker(rerank_model) if rerank_model is not None else None
        if candidate_k is not None and (candidate_k < top_k or candidate_k > _MAX_RERANK_CANDIDATE_K):
            raise ConfigurationError(
                f"Knowledge base '{name}': candidate_k ({candidate_k}) must be "
                f"between top_k ({top_k}) and {_MAX_RERANK_CANDIDATE_K}"
            )
        self._candidate_k = _resolve_candidate_k(candidate_k, top_k)

        self._chunks = _load_document_chunks(self.path, chunk_size, chunk_overlap)
        if not self._chunks:
            raise ConfigurationError(
                f"Knowledge base '{name}' has no readable documents in {self.path}"
            )

        self._chunk_tokens = [tokenize(chunk.text) for chunk in self._chunks]
        self._chunk_terms = [significant_terms(tokens) for tokens in self._chunk_tokens]
        self._bm25 = BM25Okapi(self._chunk_tokens)

    def query(self, query: str, top_k: Optional[int] = None) -> str:
        top_k = top_k or self.default_top_k
        query_tokens = tokenize(query)
        query_terms = significant_terms(query_tokens)
        scores = self._bm25.get_scores(query_tokens)

        matches = [
            (len(query_terms & chunk_terms), score, chunk)
            for score, chunk, chunk_terms in zip(scores, self._chunks, self._chunk_terms)
            if query_terms & chunk_terms
        ]
        matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
        fetch_k = _rerank_fetch_k(top_k, self._candidate_k, self._reranker)
        results = [(score, chunk) for _overlap, score, chunk in matches[:fetch_k]]
        results = _rerank_candidates(query, results, self._reranker, top_k)

        if not results:
            return f"No results found in knowledge base '{self.name}' for: {query}"

        lines = [f"Knowledge base '{self.name}' results for: {query}\n"]
        for i, (_score, chunk) in enumerate(results, 1):
            lines.append(f"{i}. [source: {chunk.source}]")
            lines.append(chunk.text.strip())
            lines.append("")
        return "\n".join(lines)


_DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]

_MARKDOWN_SEPARATORS = ["\n# ", "\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " ", ""]


def _separators_for_suffix(suffix: str) -> List[str]:
    return _MARKDOWN_SEPARATORS if suffix == ".md" else _DEFAULT_SEPARATORS


def _pack_pieces(pieces: List[str], chunk_size: int, fallback_separators: List[str]) -> List[str]:
    """Greedily merge adjacent pieces up to chunk_size; recurse into
    fallback_separators for any individual piece that's still too large on
    its own."""
    results: List[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                results.append(current)
            if len(piece) > chunk_size:
                results.extend(_recursive_split(piece, fallback_separators, chunk_size))
                current = ""
            else:
                current = piece
    if current:
        results.append(current)
    return results


def _recursive_split(text: str, separators: List[str], chunk_size: int) -> List[str]:
    """Split text on the coarsest separator that fits chunk_size, recursing
    into finer separators only for pieces that are still too large."""
    if len(text) <= chunk_size:
        return [text] if text else []
    if not separators:
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    sep, *rest = separators
    raw_pieces = text.split(sep) if sep else list(text)
    # Re-attach the separator to every piece but the first, so a piece that
    # ends up starting its own chunk still carries its own marker (e.g. a
    # Markdown "## Heading" or an XML tag) instead of silently losing it.
    pieces = [raw_pieces[0]] + [sep + p for p in raw_pieces[1:]]
    return _pack_pieces(pieces, chunk_size, rest)


def _apply_overlap(pieces: List[str], chunk_overlap: int, chunk_size: int) -> List[str]:
    """Prepend each chunk (after the first) with up to chunk_overlap trailing
    characters of the previous chunk, capped so no chunk ever exceeds
    chunk_size -- the piece's own content is never trimmed, only how much
    cross-boundary context gets borrowed shrinks (down to zero) when a piece
    is already at or near chunk_size."""
    if chunk_overlap <= 0 or len(pieces) <= 1:
        return pieces
    result = [pieces[0]]
    for prev, piece in zip(pieces, pieces[1:]):
        available = max(0, min(chunk_overlap, chunk_size - len(piece)))
        result.append(prev[-available:] + piece if available else piece)
    return result


def _chunk_text(text: str, chunk_size: int, chunk_overlap: int, suffix: str = "") -> List[str]:
    """Split text into chunks, preferring the document's own structure
    (paragraphs, sentences, words) over blind fixed-size character cuts."""
    text = text.strip()
    if not text:
        return []
    pieces = _recursive_split(text, _separators_for_suffix(suffix), chunk_size)
    pieces = [p for p in pieces if p.strip()]
    return _apply_overlap(pieces, chunk_overlap, chunk_size)


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


def _rerank_fetch_k(top_k: int, candidate_k: int, reranker: Optional[Reranker]) -> int:
    """How many pre-rerank candidates to fetch for this call. `candidate_k`
    is fixed at construction time from the constructor's default `top_k`, but
    a per-call `query(..., top_k=N)` can ask for more results than that --
    never fetch fewer than the effective `top_k`, or reranking would silently
    truncate below what was requested. Still bounded by
    `_MAX_RERANK_CANDIDATE_K`, so a very large per-call `top_k` cannot blow
    up the reranker batch size; in that case fewer than `top_k` results may
    come back, same as any other cost-bounded truncation."""
    if reranker is None:
        return top_k
    return min(max(top_k, candidate_k), _MAX_RERANK_CANDIDATE_K)


def _rerank_candidates(
    query: str,
    candidates: List["tuple[float, _Chunk]"],
    reranker: Optional[Reranker],
    top_k: int,
) -> List["tuple[float, _Chunk]"]:
    """Never mutates `candidates`. `candidates` is already sorted by
    retrieval score and sliced to `candidate_k` by the caller. Empty input
    or no reranker configured is a pure slice -- no model call, no logging.
    Any exception during scoring (including a `_RerankScoringError` contract
    violation) falls back to the pre-rerank `candidates[:top_k]` slice,
    logged as a warning: rerank is a quality layer, never a reason the
    knowledge base query itself fails."""
    if reranker is None or not candidates:
        return candidates[:top_k]
    try:
        rerank_scores = reranker.score(query, [chunk.text for _score, chunk in candidates])
    except Exception:
        _logger.warning("Rerank failed; falling back to retrieval order", exc_info=True)
        return candidates[:top_k]
    order = sorted(range(len(candidates)), key=lambda i: (-rerank_scores[i], i))
    return [candidates[i] for i in order[:top_k]]


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
