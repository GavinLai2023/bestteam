from __future__ import annotations

from pathlib import Path
from typing import List

_PIPELINE_TEMPLATE = """\
# bestteam pipeline definition — agents, teams, and the pipeline that chains them.
#
# Model specs are resolved lazily through langchain's `init_chat_model`, so any
# provider it supports works here ("openai:gpt-4o-mini", "anthropic:claude-sonnet-4-6", ...).
# Install the matching provider package (langchain-openai, langchain-anthropic, ...)
# and set the relevant API key (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...) before running.

name: code_review_pipeline

agents:
  - name: reviewer
    role: Senior Code Reviewer
    goal: Find correctness bugs and missing edge-case handling in the given code
    backstory: Has caught countless production incidents caused by unhandled edge cases
    model: "openai:gpt-4o-mini"

  - name: fixer
    role: Bug Fix Engineer
    goal: Patch the issues raised in the review with minimal, targeted changes
    backstory: Prefers small, safe diffs over rewrites
    model: "openai:gpt-4o-mini"

teams:
  - name: qa_team
    agents: [reviewer, fixer]
    mode: sequential   # sequential | parallel  (hierarchical/debate: coming soon)

pipeline:
  steps: [qa_team]
"""

_GITIGNORE_TEMPLATE = """\
.venv/
__pycache__/
.env
"""


def create_project(target: Path) -> List[Path]:
    """Write a starter project to `target`, returning the files created.

    Raises FileExistsError if `target` already exists — we never silently
    overwrite a customer's existing work.
    """
    if target.exists():
        raise FileExistsError(target)

    target.mkdir(parents=True)
    files = {
        target / "pipeline.yaml": _PIPELINE_TEMPLATE,
        target / ".gitignore": _GITIGNORE_TEMPLATE,
    }
    for file_path, content in files.items():
        file_path.write_text(content, encoding="utf-8")

    return list(files.keys())
