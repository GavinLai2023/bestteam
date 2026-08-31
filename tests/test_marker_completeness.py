"""Guards that every collected test carries at least one CI-selecting
marker, so a new test file can't silently fall outside every CI job's
`-m` selection (see docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md).

The check reads what THIS pytest process collected, recorded by
`conftest.py`'s `pytest_collection_modifyitems`. It used to shell out to a
`--collect-only` subprocess and parse the summary line, which cost a second
full collection -- 39 s, 6% of the suite's runtime -- to learn something the
running process already knew.

The trade-off that buys: running this file on its own only checks the items
collected alongside it, where the subprocess always checked all of them. CI is
the enforcement point either way (every backend job collects the whole suite),
and in exchange a failure can now name the offending node ids instead of
reporting a count.
"""
import pytest

pytestmark = pytest.mark.unit

_CI_MARKERS = {"unit", "integration", "e2e", "optional"}

# Set by `conftest.py`'s `pytest_collection_modifyitems`; a literal on both
# sides so neither file imports the other.
_COLLECTED_MARKERS_ATTR = "bestteam_collected_markers"


def collected_markers(config):
    """The (node id, marker names) pairs `conftest.py` recorded at collection.

    Raises rather than defaulting to an empty list: a missing recording means
    the hook stopped running, and an empty list would make every assertion
    below pass vacuously -- the exact failure this guard exists to prevent.
    """
    recorded = getattr(config, _COLLECTED_MARKERS_ATTR, None)
    if recorded is None:
        raise AssertionError(
            "conftest.py's pytest_collection_modifyitems did not record the "
            "collected items, so marker completeness cannot be checked."
        )
    return recorded


def _unmarked_ids(collected):
    """The node ids in `collected` that carry none of `_CI_MARKERS`.

    `collected` is the (node id, marker names) pairs recorded by the
    collection hook in `conftest.py`. Returning the ids -- rather than a
    count -- is what lets the failure name the files to fix.
    """
    return [nodeid for nodeid, names in collected if not (names & _CI_MARKERS)]


def test_every_item_has_a_ci_marker(request):
    unmarked = _unmarked_ids(collected_markers(request.config))

    assert not unmarked, (
        f"{len(unmarked)} test item(s) carry none of {_CI_MARKERS} -- add a "
        "pytestmark so they're covered by a CI job:\n  "
        + "\n  ".join(unmarked[:20])
    )


def test_unmarked_ids_names_the_items_that_carry_no_ci_marker():
    collected = [
        ("tests/test_a.py::test_one", {"unit"}),
        ("tests/test_b.py::test_two", {"slow"}),
        ("tests/test_c.py::test_three", {"slow", "integration"}),
        ("tests/test_d.py::test_four", set()),
    ]

    assert _unmarked_ids(collected) == [
        "tests/test_b.py::test_two",
        "tests/test_d.py::test_four",
    ]


def test_collection_records_every_item_with_its_markers(request):
    """The guard above reads what pytest actually collected, so the recording
    hook is the load-bearing part: if it stopped running, the guard would pass
    vacuously on an empty list."""
    collected = collected_markers(request.config)

    ids = {nodeid for nodeid, _ in collected}
    assert request.node.nodeid in ids
    assert dict(collected)[request.node.nodeid] >= {"unit"}
