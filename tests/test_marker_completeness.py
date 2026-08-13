"""Guards that every collected test carries at least one CI-selecting
marker, so a new test file can't silently fall outside every CI job's
`-m` selection (see docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md)."""
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_CI_MARKERS = {"unit", "integration", "e2e", "optional"}


def test_every_item_has_a_ci_marker():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
        capture_output=True, text=True, cwd=".",
    )
    # Re-collect with each marker excluded in turn and diff against the full
    # set would be slow; instead ask pytest directly for markers per item.
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
    for line in output.splitlines():
        if line.strip().endswith(("test collected", "tests collected")):
            return int(line.strip().split()[0])
    return 0
