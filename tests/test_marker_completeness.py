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
_COLLECTED_RE = re.compile(
    r"^(?:(?P<selected>\d+)/\d+|(?P<count>\d+)) tests? collected"
    r"(?: \(\d+ deselected\))? in [\d.]+s?$"
)


def test_every_item_has_a_ci_marker():
    # One collect-only pass with the CI-marker union gives us the selected
    # count (and, via "S/T ... deselected", pytest already tells us T too)
    # -- but relying on that would couple us to the deselected form's
    # presence, so we still do a second pass for an unambiguous total.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-m", " or ".join(_CI_MARKERS)],
        capture_output=True, text=True, cwd=".",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    selected = _count_collected(result.stdout)

    result_all = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True, text=True, cwd=".",
    )
    assert result_all.returncode == 0, result_all.stdout + result_all.stderr
    total = _count_collected(result_all.stdout)

    assert selected == total, (
        f"{total - selected} test item(s) carry none of {_CI_MARKERS} -- "
        "add a pytestmark so they're covered by a CI job."
    )


def _count_collected(output: str) -> int:
    """Extract the collected-item count from pytest's collection summary
    line (the last non-blank line of `--collect-only -q` output). Handles
    both the plain form ("N tests collected in T") and the filtered form
    ("S/T tests collected (D deselected) in T"), with or without ANSI
    colour codes. Raises loudly if no line matches, rather than falling
    back to a value like 0 that would make the caller's assertion
    vacuously pass regardless of what pytest actually reported.
    """
    for line in reversed(output.splitlines()):
        clean = _ANSI_RE.sub("", line).strip()
        if not clean:
            continue
        match = _COLLECTED_RE.match(clean)
        if match:
            if match.group("selected") is not None:
                return int(match.group("selected"))
            return int(match.group("count"))
    raise AssertionError(
        "Could not parse a pytest collection summary line (expected "
        "something like 'N tests collected in T' or 'S/T tests collected "
        f"(D deselected) in T') from output:\n{output}"
    )
