"""Tests for ui/backend/main.py's pipeline loading/caching."""
import os

import pytest


pytestmark = pytest.mark.integration
fastapi = pytest.importorskip("fastapi")

from fastapi import HTTPException

from ui.backend import main as backend_main


@pytest.fixture
def pipelines_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    # This module tests the YAML-file branch itself, which is opt-in.
    monkeypatch.setenv("BESTTEAM_DEMO_PIPELINES", "1")
    backend_main._pipeline_cache.clear()
    return tmp_path


def _write_pipeline(path, name, response):
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
pipeline:
  steps: [team1]
""",
        encoding="utf-8",
    )


def test_get_pipeline_caches_until_file_changes(pipelines_dir):
    p = pipelines_dir / "demo.yaml"
    _write_pipeline(p, "demo_v1", "first")

    wf1 = backend_main._get_pipeline("demo")
    wf2 = backend_main._get_pipeline("demo")
    assert wf1 is wf2
    assert wf1.name == "demo_v1"

    _write_pipeline(p, "demo_v2", "second")
    new_mtime = p.stat().st_mtime + 1
    os.utime(p, (new_mtime, new_mtime))

    wf3 = backend_main._get_pipeline("demo")
    assert wf3 is not wf1
    assert wf3.name == "demo_v2"


def test_get_pipeline_raises_404_for_missing_file(pipelines_dir):
    with pytest.raises(HTTPException) as exc_info:
        backend_main._get_pipeline("does-not-exist")
    assert exc_info.value.status_code == 404
