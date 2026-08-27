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

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

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
