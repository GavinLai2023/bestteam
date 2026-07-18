"""Tests for ui/backend/main.py's workflow loading/caching."""
import os

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import HTTPException

from ui.backend import main as backend_main


@pytest.fixture
def workflows_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    # This module tests the YAML-file branch itself, which is opt-in.
    monkeypatch.setenv("BESTTEAM_DEMO_WORKFLOWS", "1")
    backend_main._workflow_cache.clear()
    return tmp_path


def _write_workflow(path, name, response):
    path.write_text(
        f"""
name: {name}
agents:
  - name: helper
    role: Helper
    goal: Help
    model: "fake:{response}"
teams:
  - name: team1
    agents: [helper]
    mode: sequential
workflow:
  steps: [team1]
""",
        encoding="utf-8",
    )


def test_get_workflow_caches_until_file_changes(workflows_dir):
    p = workflows_dir / "demo.yaml"
    _write_workflow(p, "demo_v1", "first")

    wf1 = backend_main._get_workflow("demo")
    wf2 = backend_main._get_workflow("demo")
    assert wf1 is wf2
    assert wf1.name == "demo_v1"

    _write_workflow(p, "demo_v2", "second")
    new_mtime = p.stat().st_mtime + 1
    os.utime(p, (new_mtime, new_mtime))

    wf3 = backend_main._get_workflow("demo")
    assert wf3 is not wf1
    assert wf3.name == "demo_v2"


def test_get_workflow_raises_404_for_missing_file(workflows_dir):
    with pytest.raises(HTTPException) as exc_info:
        backend_main._get_workflow("does-not-exist")
    assert exc_info.value.status_code == 404
