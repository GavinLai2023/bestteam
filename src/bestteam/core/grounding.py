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
citation exactly (after whitespace normalisation), or when the tag names
only a filename and that filename is the document of some returned citation.
A tag carrying a page or heading that matches no returned citation is
unverified -- a fabricated locator is precisely what this exists to show.
Filenames are case-sensitive, so the comparison is too.
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

# What ``knowledge_base._citation`` appends after the filename: a page
# (``, p.3``) and/or a heading (`` § Refunds``). A label with neither is a
# bare filename.
_LOCATOR_MARKERS = (", p.", " § ")


def _normalise(label: str) -> str:
    """Collapse internal whitespace and strip the ends -- applied to both sides."""
    return " ".join(label.split())


def _filename(label: str) -> str:
    """The document part of a citation label: everything before the first locator."""
    cut = len(label)
    for marker in _LOCATOR_MARKERS:
        index = label.find(marker)
        if index != -1 and index < cut:
            cut = index
    return label[:cut]


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
    searches: int,
    hit_count: int,
) -> GroundingResult:
    """Compare the ``[source: …]`` tags in ``text`` with ``citations``.

    ``citations`` is every label the agent's knowledge-base searches returned
    this turn, in full (not the bounded ``sources`` a trace event keeps).
    ``searches`` and ``hit_count`` are carried through unchanged so the
    result is the whole story of the turn in one object.
    """
    returned = {_normalise(citation) for citation in citations}
    returned_files = {_filename(citation) for citation in returned}

    # dict.fromkeys de-duplicates while keeping first-appearance order.
    labels = [
        label
        for label in dict.fromkeys(_normalise(match) for match in CITATION_TAG.findall(text or ""))
        if label
    ]

    verified = 0
    unverified: List[str] = []
    for label in labels:
        filename_only = _filename(label) == label
        if label in returned or (filename_only and label in returned_files):
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
