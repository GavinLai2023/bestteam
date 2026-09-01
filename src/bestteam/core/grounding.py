"""Grounding-lite: does an agent's answer cite what its searches returned?

A knowledge-base tool hands the model excerpts tagged ``[source: <label>]``
and asks it to cite them with the same tag. Nothing stops a model from
writing a tag the search never returned -- a plausible filename, a page
number that does not exist. This module compares the tags in an agent's
final text with the citation labels its own knowledge-base searches produced
during the same turn, and reports the counts. It records; it never changes
the answer. The adapter (``adapters/langgraph_adapter.py``) runs it once per
turn for an agent that has a knowledge-base tool bound and emits the result
as a ``grounding_checked`` trace event.

Verification rule: a tag is verified when its label equals a returned
citation exactly (after whitespace normalisation), or when it equals the
filename of a returned document. Both are set membership over what the
search reported -- **nothing here takes a label apart**. A tag carrying a
page or heading that matches no returned citation is unverified -- a
fabricated locator is precisely what this exists to show. Filenames are
case-sensitive, so the comparison is too.

⚠️ **The filenames have to arrive as their own field** (the tool's
``citation_documents``, next to ``citations``), and that is not a
formality. Splitting a label at the first ``, p.`` or `` § `` to recover
the filename has no notion of where the filename actually ends, so a
document legitimately named ``report, p.2.pdf`` was misread both ways: a
correct full citation looked like a fabricated locator, and a bare
``[source: report]`` was accepted for a document of another name. Under
``refuse`` those are a wrong refusal and a missed one -- not the trace
noise they were while the check only recorded.

For the same reason a citation never contains ``]``:
``knowledge_base._citation`` replaces it, so ``CITATION_TAG`` stopping at
the first one cannot truncate a label the search really returned (a section
titled "Item [2]" used to).

``documents`` defaults to empty, which only a hand-written custom
knowledge-base tool can produce (all three built-in types report the field).
Such a tool loses the filename-only rule -- strictly stricter, never wrong.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

_logger = logging.getLogger(__name__)

#: The tag the tool tells the model to quote (see
#: ``knowledge_base.format_results``): ``[source: handbook.pdf, p.3 § Refunds]``.
CITATION_TAG = re.compile(r"\[source:\s*([^\]]*?)\s*\]")

#: Bounds on what the event records. An unverified label is model-written
#: text, so it gets the same length bound a traced query has, and a list of
#: them must not turn one event into a wall.
MAX_UNVERIFIED = 10
MAX_LABEL_CHARS = 200

#: What an agent's turn does with a failed check (`Agent.grounding_policy`).
#: `observe` records only -- byte-for-byte the pre-policy behaviour and the
#: default; `retry` makes one corrective model call; `refuse` retries once
#: and, still failing, returns `GROUNDING_REFUSAL_TEXT` instead of the answer.
GROUNDING_POLICIES = ("observe", "retry", "refuse")

#: The corrective instruction a `retry`/`refuse` turn appends as a user
#: message. The turn's search results are already in the conversation's tool
#: messages, so the model needs no new searches -- only a rewrite.
GROUNDING_RETRY_INSTRUCTION = (
    "Your previous answer failed a citation check. Rewrite it using ONLY "
    "information from the search results earlier in this conversation, and "
    "cite each claim with the exact [source: ...] tag the search returned. "
    "If the search results do not contain the answer, say the knowledge base "
    "does not contain the answer -- do not guess."
)

#: What a `refuse` turn returns when the retried answer still fails --
#: deterministic, so downstream consumers can recognise a refused answer.
GROUNDING_REFUSAL_TEXT = (
    "I can't provide this answer: it could not be verified against the "
    "knowledge base's search results. Please rephrase the question, or "
    "consult the source documents directly."
)

#: How deep the grounding check goes (`Agent.grounding_level`). `citation`
#: is the pre-existing set-membership check; `claim` additionally has an LLM
#: grader split the answer into factual claims and judge each against the
#: turn's own search results.
GROUNDING_LEVELS = ("citation", "claim")

#: Bound on how many grader-reported claims are read; anything past it is
#: model output nobody asked for, not evidence.
MAX_CLAIMS = 20

_CLAIM_GRADER_SYSTEM_PROMPT = (
    "You are a strict fact-checking grader. Extract every factual or business "
    "assertion from the ANSWER (numbers, dates, durations, policies, "
    "conditions, capabilities stated as fact), quoting each claim's text from "
    "the answer. Judge each claim ONLY against the EVIDENCE text: supported "
    "means the evidence states it or directly implies it. Respond with ONLY a "
    'JSON object of the form {"claims": [{"text": "...", "supported": true}]}. '
    "Use an empty list if the answer makes no factual claims (for example, it "
    "only says the knowledge base has no answer). No prose outside the JSON."
)


def claim_retry_instruction(unsupported: Sequence[str]) -> str:
    """The corrective instruction for a turn that failed at claim level: the
    citation instruction plus the grader-named claims to delete or re-ground.
    The rest of the answer is explicitly kept -- the customer gets the
    verifiable part, not an empty hand."""
    claims = "\n".join(f"- {claim}" for claim in unsupported)
    return (
        f"{GROUNDING_RETRY_INSTRUCTION}\n\n"
        "These statements in your previous answer are NOT supported by the "
        f"search results:\n{claims}\n"
        "Remove each of them, or rewrite it to state only what the search "
        "results support. Keep the rest of the answer."
    )


@dataclass(frozen=True)
class ClaimGrading:
    """What the grader made of one answer against one turn's evidence."""

    #: Factual claims the grader extracted (bounded by MAX_CLAIMS).
    claims: int
    #: Of those, judged supported by the evidence.
    supported: int
    #: The rest, bounded like `GroundingResult.unverified`.
    unsupported: List[str]

    @property
    def passes(self) -> bool:
        """No unsupported claims. Zero claims passes: an honest "the knowledge
        base does not contain the answer" has nothing to support and must not
        be refused."""
        return not self.unsupported


def grade_claims(
    text: str, evidence: Sequence[str], model: Any
) -> Tuple[Optional[ClaimGrading], Optional[Any]]:
    """One LLM call that splits `text` into factual claims and judges each
    against `evidence` (the turn's knowledge-base tool results, verbatim).

    Returns `(grading, raw_response)`. NEVER raises: an invoke failure returns
    `(None, None)`; a response that parses to nothing usable returns
    `(None, response)` -- the call was billed either way, so the caller can
    still meter it (`expand_query`'s shape). `None` grading means the check
    was not performed and the caller falls back to citation-level -- a grader
    failure is never a reason to retry or refuse.

    Deliberately NOT `with_structured_output`: `fake:` models don't support
    it (which would make this untestable at $0), and reasoning-mode models
    reject the forced `tool_choice` behind `function_calling` -- a plain
    invoke plus tolerant JSON parsing sidesteps both, exactly like
    `fusion.expand_query`.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # Shared deliberately, like `_MARKDOWN_HEADING_RE` from `file_parser`:
    # one definition of "tolerant JSON reply parsing", not two drifting ones.
    from .fusion import _parse_expansion

    haystack = text if isinstance(text, str) else ""
    evidence_block = "\n\n---\n\n".join(evidence) if evidence else "(no search results)"
    try:
        response = model.invoke(
            [
                SystemMessage(content=_CLAIM_GRADER_SYSTEM_PROMPT),
                HumanMessage(content=f"EVIDENCE:\n{evidence_block}\n\nANSWER:\n{haystack}"),
            ]
        )
    except Exception:  # noqa: BLE001 -- no call succeeded, nothing billable
        _logger.warning("Claim grading call failed; falling back to citation-level", exc_info=True)
        return None, None

    content = response.content if hasattr(response, "content") else str(response)
    parsed = _parse_expansion(content if isinstance(content, str) else "")
    raw_claims = parsed.get("claims") if parsed else None
    if not isinstance(raw_claims, list):
        _logger.warning("Claim grading response had no usable 'claims' list; falling back to citation-level")
        return None, response

    claims = 0
    supported = 0
    unsupported: List[str] = []
    for entry in raw_claims:
        if claims >= MAX_CLAIMS:
            break
        if not isinstance(entry, dict):
            continue
        claim_text = entry.get("text")
        verdict = entry.get("supported")
        if not isinstance(claim_text, str) or not claim_text.strip() or not isinstance(verdict, bool):
            continue
        claims += 1
        if verdict:
            supported += 1
        else:
            unsupported.append(" ".join(claim_text.split())[:MAX_LABEL_CHARS])
    return ClaimGrading(claims=claims, supported=supported, unsupported=unsupported[:MAX_UNVERIFIED]), response

def _normalise(label: str) -> str:
    """Collapse internal whitespace and strip the ends -- applied to both sides."""
    return " ".join(label.split())


@dataclass(frozen=True)
class GroundingResult:
    """What one agent turn's citations looked like against its own searches."""

    #: Knowledge-base tool calls that completed this turn.
    searches: int
    #: Passages those searches returned, summed over the calls.
    hit_count: int
    #: Distinct ``[source: …]`` labels in the final text.
    cited: int
    #: Of those, labels the searches actually returned.
    verified: int
    #: The rest, in order of first appearance, bounded.
    unverified: List[str]

    @property
    def passes(self) -> bool:
        """The policy bar: at least one citation, every one of them verified.
        No tags at all fails too -- an uncited answer from a knowledge-base
        agent is exactly what `retry`/`refuse` exist to correct."""
        return self.cited > 0 and not self.unverified

    def as_trace_data(self) -> Dict[str, Any]:
        """The ``grounding_checked`` event's ``data`` -- a fresh dict, fresh list."""
        return {
            "searches": self.searches,
            "hit_count": self.hit_count,
            "cited": self.cited,
            "verified": self.verified,
            "unverified": list(self.unverified),
        }


def check_grounding(
    text: str,
    citations: Sequence[str],
    *,
    documents: Sequence[str] = (),
    searches: int,
    hit_count: int,
) -> GroundingResult:
    """Compare the ``[source: …]`` tags in ``text`` with ``citations``.

    ``citations`` is every label the agent's knowledge-base searches returned
    this turn, in full (not the bounded ``sources`` a trace event keeps), and
    ``documents`` is the filename each of those came from -- reported as its
    own field rather than parsed back out of a label (see the module
    docstring). ``searches`` and ``hit_count`` are carried through unchanged
    so the result is the whole story of the turn in one object.

    ``text`` is contractually a ``str`` (a model's final answer), but some
    providers hand back ``response.content`` as a list of content blocks
    instead. This function never raises over that: a non-``str`` ``text`` is
    treated as carrying no citation tags at all (``cited: 0``), rather than
    letting ``re.findall`` blow up with a ``TypeError`` and fail the whole
    run -- the check records, it never blocks.
    """
    returned = {_normalise(citation) for citation in citations}
    returned_files = {_normalise(document) for document in documents}

    haystack = text if isinstance(text, str) else ""
    # dict.fromkeys de-duplicates while keeping first-appearance order.
    labels = [
        label
        for label in dict.fromkeys(_normalise(match) for match in CITATION_TAG.findall(haystack))
        if label
    ]

    verified = 0
    unverified: List[str] = []
    for label in labels:
        if label in returned or label in returned_files:
            verified += 1
        else:
            unverified.append(label[:MAX_LABEL_CHARS])

    return GroundingResult(
        searches=searches,
        hit_count=hit_count,
        cited=len(labels),
        verified=verified,
        unverified=unverified[:MAX_UNVERIFIED],
    )
