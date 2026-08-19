"""Guards that every collected test carries at least one CI-selecting
marker, so a new test file can't silently fall outside every CI job's
`-m` selection (see docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md)."""
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_CI_MARKERS = {"unit", "integration", "e2e", "optional"}

# Strips ANSI colour escapes: pytest emits these even when stdout is
# captured (non-tty), e.g. under FORCE_COLOR.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Matches pytest's collection summary line in either of its two real forms:
#   "1229 tests collected in 5.04s"
#   "590/1229 tests collected (639 deselected) in 15.28s"
# Real pytest always appends a trailing "in N.NNs" timing suffix.
# Past 60 s pytest appends "(H:MM:SS)" to the duration -- "in 62.83s
# (0:01:02)" -- which a loaded machine under `-n auto` does reach.
_COLLECTED_RE = re.compile(
    r"^(?:(?P<selected>\d+)/(?P<total>\d+)|(?P<count>\d+)) tests? collected"
    r"(?: \(\d+ deselected\))? in [\d.]+s?(?: \(\d+:\d\d:\d\d\))?$"
)


def test_every_item_has_a_ci_marker():
    # A single collect-only pass with the CI-marker union reports both counts.
    # pytest prints "S/T tests collected (D deselected)" only when something
    # was actually deselected, and the plain "N tests collected" when nothing
    # was -- and "nothing was deselected" is precisely the passing case here,
    # so the two forms map cleanly onto (selected, total) with no ambiguity.
    #
    # This used to run a second, unfiltered pass for the total. Collecting
    # this suite costs ~25s, so that second pass made this single test 54s --
    # 7% of the entire suite's runtime -- to learn a number the first pass had
    # already reported.
    #
    # -p no:cacheprovider keeps this subprocess off the parent's .pytest_cache;
    # under `-n auto` the xdist workers are using it concurrently.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-m", " or ".join(_CI_MARKERS)],
        capture_output=True, text=True, cwd=".",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    selected, total = _collected_counts(result.stdout)

    assert selected == total, (
        f"{total - selected} test item(s) carry none of {_CI_MARKERS} -- "
        "add a pytestmark so they're covered by a CI job."
    )


def _collected_counts(output: str) -> tuple[int, int]:
    """Extract (selected, total) from pytest's collection summary line (the
    last non-blank line of `--collect-only -q` output), with or without ANSI
    colour codes.

    The filtered form ("S/T tests collected (D deselected) in T") gives both
    directly. The plain form ("N tests collected in T") is what pytest prints
    when a `-m` filter deselected nothing at all, which means every collected
    item matched -- so selected and total are both N. Raises loudly if no line
    matches, rather than falling back to a value like 0 that would make the
    caller's assertion vacuously pass regardless of what pytest reported.
    """
    for line in reversed(output.splitlines()):
        clean = _ANSI_RE.sub("", line).strip()
        if not clean:
            continue
        match = _COLLECTED_RE.match(clean)
        if match:
            if match.group("selected") is not None:
                return int(match.group("selected")), int(match.group("total"))
            count = int(match.group("count"))
            return count, count
    raise AssertionError(
        "Could not parse a pytest collection summary line (expected "
        "something like 'N tests collected in T' or 'S/T tests collected "
        f"(D deselected) in T') from output:\n{output}"
    )
