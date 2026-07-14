"""Packaging / entry-point contract tests.

These guard the fixes for CR-006 (Alembic in the image), CR-007 (provider
extras installed in the official image), CR-014 (httpx in the aggregate tools
extra) and CR-015 (`python -m bestteam`). They assert declarations rather than
building the container, so they run in the deterministic suite; the container
`alembic current` / import smoke checks remain CI concerns.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _pyproject():
    tomllib = pytest.importorskip("tomllib")
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _extras():
    return _pyproject()["project"]["optional-dependencies"]


def test_tools_extra_declares_httpx():
    # CR-014: http_get imports httpx at call time; the aggregate tools extra
    # must therefore declare it so `bestteam[tools]` can run every built-in.
    assert any(dep.startswith("httpx") for dep in _extras()["tools"])


def test_providers_openai_extra_declares_langchain_and_openai():
    # CR-007: real-model string resolution needs langchain + langchain-openai,
    # and interview transcription needs openai.
    deps = " ".join(_extras()["providers-openai"])
    assert "langchain" in deps
    assert "langchain-openai" in deps
    assert "openai" in deps


def test_dockerfile_ships_alembic_and_providers():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # CR-006: Alembic assets copied so the documented upgrade command works.
    assert "alembic.ini" in dockerfile
    assert "COPY alembic" in dockerfile
    # CR-007: official image installs the provider extra.
    assert "providers-openai" in dockerfile


def test_readme_quickstart_installs_provider_extra():
    # CR-007/CR-017: the quick start uses openai: models, so it must install a
    # provider extra, not just [tools]/[ui].
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "providers-openai" in readme


def test_python_dash_m_bestteam_entry_point():
    # CR-015: the documented `python -m bestteam` invocation must work.
    result = subprocess.run(
        [sys.executable, "-m", "bestteam", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout
