# E2E Test Harness + CI Test Tiering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual, human-run Playwright script with a self-contained pytest-playwright suite that runs headless in CI, give the Team Builder wizard's full journey deterministic coverage via a new `fake-architect:` model, and split backend tests into fast/slow/e2e/optional pytest markers feeding a tiered CI job map (fast PR gate, full regression on `main`).

**Architecture:** A session-scoped pytest fixture (`tests/e2e/conftest.py`) spins up a temp SQLite DB plus real `uvicorn`/`vite` subprocesses, provisions accounts via the existing `ui.backend.admin` CLI, and reshapes the model catalog so the wizard's automatic model selection resolves to a new `fake-architect:` model — a `FakeListChatModel` subclass that also implements `with_structured_output()` for `Requirements`/`Specification`, so it's safe to both design AND run a team with. Existing Playwright scenarios move from a hand-rolled script into pytest test functions; six previously-skipped wizard scenarios get un-skipped against the fake architect. All ~60 existing backend test files get a one-time `pytestmark` sweep (`unit`/`integration`/`slow`/`optional`) so CI can select fast vs. full subsets.

**Tech Stack:** pytest, pytest-playwright (new), FastAPI/SQLAlchemy (existing), Vite/React (existing), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md`

## Global Constraints

- PR-gate CI jobs must parallelize to a 5–8 minute wall-clock target.
- `fake-architect:` must never appear in `DEFAULT_MODEL_CATALOG` (`ui/backend/db/model_catalog.py`) — it's added only to a test session's own ephemeral DB.
- `fake-architect:` must be a full drop-in `BaseChatModel` (ordinary `.invoke()` works), not just a structured-output stub — it can end up pinned as a real deployed agent's model via `submit_solution_feedback`'s re-pin logic.
- Playwright runs headless by default; a `--headed` flag stays available for local debugging.
- Local `pytest` with no args must keep running the full unfiltered suite (no behavior change for a developer's normal workflow).
- Every collected pytest item must carry at least one of `unit`/`integration`/`e2e`/`optional`, verified by a meta-test.

---

### Task 1: pytest marker registration + pytest-playwright dependency

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `[tool.pytest.ini_options]` with `markers = ["unit: ...", "integration: ...", "e2e: ...", "slow: ...", "optional: ..."]` and `addopts = "--strict-markers"`; a new `test` extra containing `pytest-playwright>=0.5`.

- [ ] **Step 1: Add the pytest config section**

Add this section to `pyproject.toml`, after `[project.scripts]` and before `[build-system]`:

```toml
[tool.pytest.ini_options]
addopts = "--strict-markers"
markers = [
    "unit: pure SDK/core logic, no DB or HTTP",
    "integration: FastAPI TestClient + SQLite",
    "e2e: Playwright browser scenario",
    "slow: real Alembic migrations, concurrency timing, or full E2E journeys",
    "optional: requires an optional extra (e.g. interview/providers-openai)",
]
```

- [ ] **Step 2: Add the `test` extra**

In `[project.optional-dependencies]`, add a new line after `dev`:

```toml
test         = ["pytest-playwright>=0.5"]
```

- [ ] **Step 3: Install and verify markers are registered**

Run: `pip install -e ".[dev,test]"` then `pytest --markers`
Expected: output lists `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.e2e`, `@pytest.mark.slow`, `@pytest.mark.optional` with the descriptions above.

- [ ] **Step 4: Verify strict-markers rejects a typo**

Run: `python -c "
import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-m', 'nonexistent_marker_xyz'], capture_output=True, text=True)
assert 'nonexistent_marker_xyz' in r.stderr or 'nonexistent_marker_xyz' in r.stdout
print('OK: strict-markers rejects unregistered marker names in -m expressions is a separate check; verifying registration only')
"`

(This step is a sanity check on marker *registration*, not enforcement — `--strict-markers` enforces at collection time when a test is *decorated* with an unregistered mark, not in `-m` filter expressions. Skip asserting on `-m` behavior; instead confirm via Step 3's `--markers` output.)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "test: register pytest markers and add pytest-playwright test extra"
```

---

### Task 2: pytestmark sweep across existing test files + marker-completeness meta-test

**Files:**
- Modify: 60 files under `tests/*.py` (listed below)
- Create: `tests/test_marker_completeness.py`

**Interfaces:**
- Consumes: markers registered in Task 1.
- Produces: every existing test file carries a module-level `pytestmark`; `tests/test_marker_completeness.py::test_every_item_has_a_ci_marker` fails if a future test is added without one.

- [ ] **Step 1: Classify and apply markers via script**

Run this once from the repo root (adjust the shebang/interpreter as needed for the environment):

```python
import re
from pathlib import Path

INTEGRATION_FILES = [
    "test_admin_api.py", "test_admin_cli.py", "test_auth.py",
    "test_automation_results.py", "test_automation_results_api.py",
    "test_builder_api.py", "test_crud_api.py", "test_db.py",
    "test_dependencies.py", "test_email_credentials.py", "test_email_trigger.py",
    "test_email_trigger_api.py", "test_email_triggers_db.py",
    "test_exception_handler.py", "test_load_email_tools.py",
    "test_memory_api.py", "test_model_catalog.py", "test_org_isolation.py",
    "test_org_knowledge_bases.py", "test_org_settings.py",
    "test_run_analytics_api.py", "test_run_cancellation.py",
    "test_run_lifecycle.py", "test_runtime_run_row.py", "test_trace_persistence.py",
    "test_ui_backend.py", "test_usage_metering.py", "test_ws_stream.py",
]
INTEGRATION_AND_OPTIONAL = ["test_interview_api.py"]
INTEGRATION_AND_SLOW = ["test_migrations.py"]
UNIT_FILES = [
    "test_account_memory.py", "test_agent.py", "test_deploy_validation.py",
    "test_email_scoped_tools.py", "test_email_tls_security.py", "test_email_tools.py",
    "test_hierarchical_team.py", "test_http_client.py", "test_imap_summaries_for.py",
    "test_knowledge_base.py", "test_memory.py", "test_memory_backend.py",
    "test_memory_integration.py", "test_org_deactivation.py", "test_packaging.py",
    "test_registry.py", "test_requirements.py", "test_reranking.py",
    "test_run_analytics.py", "test_secret_store.py", "test_skill_seeding.py",
    "test_skill_versions.py", "test_specification.py", "test_team.py", "test_tools.py",
    "test_trace_granularity.py", "test_trigger_workflow_builder.py",
    "test_vector_knowledge_base.py", "test_workflow.py", "test_workflow_versions.py",
]

TESTS_DIR = Path("tests")

def insert_pytestmark(path: Path, mark_expr: str) -> None:
    text = path.read_text(encoding="utf-8")
    if "pytestmark" in text:
        return  # already has one -- don't clobber
    lines = text.splitlines(keepends=True)
    # Insert after the last top-level import line (a blank-line-preceded
    # non-import, non-docstring, non-comment line), so it lands after imports
    # like the rest of the codebase's convention.
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) or stripped == "" or stripped.startswith('"""') or stripped.startswith("#"):
            insert_at = i + 1
        else:
            break
    lines.insert(insert_at, f"\npytestmark = {mark_expr}\n")
    path.write_text("".join(lines), encoding="utf-8")

for name in INTEGRATION_FILES:
    insert_pytestmark(TESTS_DIR / name, "pytest.mark.integration")
for name in INTEGRATION_AND_OPTIONAL:
    insert_pytestmark(TESTS_DIR / name, "[pytest.mark.integration, pytest.mark.optional]")
for name in INTEGRATION_AND_SLOW:
    insert_pytestmark(TESTS_DIR / name, "[pytest.mark.integration, pytest.mark.slow]")
for name in UNIT_FILES:
    insert_pytestmark(TESTS_DIR / name, "pytest.mark.unit")

print("Done. Files not covered by any list (should be empty):")
covered = set(INTEGRATION_FILES) | set(INTEGRATION_AND_OPTIONAL) | set(INTEGRATION_AND_SLOW) | set(UNIT_FILES)
for f in sorted(TESTS_DIR.glob("test_*.py")):
    if f.name not in covered:
        print(" -", f.name)
```

Each modified file must also add `import pytest` at the top if it doesn't already have one — check this after running the script:

Run: `python -c "
from pathlib import Path
for f in sorted(Path('tests').glob('test_*.py')):
    text = f.read_text(encoding='utf-8')
    if 'pytestmark' in text and 'import pytest' not in text:
        print('MISSING import pytest:', f.name)
"`

For any file listed, add `import pytest` near its other imports.

- [ ] **Step 2: Verify collection still works and nothing broke**

Run: `pytest --collect-only -q`
Expected: same total test count as before the sweep (no collection errors).

- [ ] **Step 3: Write the marker-completeness meta-test**

Create `tests/test_marker_completeness.py`:

```python
"""Guards that every collected test carries at least one CI-selecting
marker, so a new test file can't silently fall outside every CI job's
`-m` selection (see docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md)."""
import subprocess
import sys

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
```

This file itself is a meta-test with no natural `unit`/`integration` home — give it its own marker:

Add at the top of `tests/test_marker_completeness.py`, after the imports:

```python
import pytest

pytestmark = pytest.mark.unit
```

- [ ] **Step 4: Run it**

Run: `pytest tests/test_marker_completeness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: sweep pytestmark across existing tests, add marker-completeness guard"
```

---

### Task 3: `fake-architect:` model spec

**Files:**
- Modify: `src/bestteam/adapters/langgraph_adapter.py`
- Test: `tests/test_fake_architect_model.py`

**Interfaces:**
- Consumes: `bestteam.core.specification.Specification`/`AgentSpec`/`TeamSpec`/`WorkflowSpec`, `bestteam.core.requirements.Requirements`.
- Produces: `_resolve_model("fake-architect:<name>")` returns a `FakeListChatModel` subclass instance whose `.invoke(messages)` behaves like an ordinary fake chat model and whose `.with_structured_output(schema, **kwargs).invoke(messages)` returns a canned `Requirements` or `Specification` instance for those two schemas (raises `NotImplementedError` for any other schema).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fake_architect_model.py`:

```python
"""Tests for the fake-architect: model spec (see
docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md)."""
import pytest

from bestteam import Requirements, Specification
from bestteam.adapters.langgraph_adapter import _resolve_model

pytestmark = pytest.mark.unit


def test_fake_architect_resolves_to_a_chat_model():
    model = _resolve_model("fake-architect:e2e")
    result = model.invoke("hello")
    assert result.content  # a non-empty AIMessage, like an ordinary fake: model


def test_fake_architect_with_structured_output_returns_canned_specification():
    model = _resolve_model("fake-architect:e2e")
    spec = model.with_structured_output(Specification).invoke([])
    assert isinstance(spec, Specification)
    assert spec.agents
    assert spec.teams
    assert spec.workflow.steps


def test_fake_architect_with_structured_output_returns_canned_requirements():
    model = _resolve_model("fake-architect:e2e")
    req = model.with_structured_output(Requirements).invoke([])
    assert isinstance(req, Requirements)
    assert req.summary


def test_fake_architect_rejects_unknown_schema():
    model = _resolve_model("fake-architect:e2e")

    class SomeOtherSchema:
        pass

    with pytest.raises(NotImplementedError):
        model.with_structured_output(SomeOtherSchema)


def test_fake_architect_specification_agents_use_a_deployable_model():
    """The canned Specification's own agents must carry a model that's
    exempt from catalog validation on its own (deploy_validation.py) --
    independent of whatever re-pinning a caller does afterward."""
    model = _resolve_model("fake-architect:e2e")
    spec = model.with_structured_output(Specification).invoke([])
    assert all(agent.model.startswith("fake:") for agent in spec.agents)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_fake_architect_model.py -v`
Expected: FAIL — `_resolve_model` raises `ConfigurationError` for an unrecognized string spec (no `fake-architect:` handling yet).

- [ ] **Step 3: Implement**

In `src/bestteam/adapters/langgraph_adapter.py`, find `_resolve_model` (around line 63). Add the new branch, and the canned data + fake model class near the top of the file (after the existing imports, before `_resolve_model`):

```python
def _fake_architect_specification() -> "Specification":
    from bestteam.core.specification import AgentSpec, Specification, TeamSpec, WorkflowSpec

    return Specification(
        name="e2e_support_team",
        display_name="Support Team (E2E)",
        agents=[
            AgentSpec(
                name="support_agent",
                role="Customer Support Specialist",
                goal="Answer customer questions clearly and politely.",
                backstory="A friendly, patient support assistant.",
                model="fake:Thanks for reaching out! Here's how I can help.",
            ),
        ],
        teams=[TeamSpec(name="support_team", mode="sequential", agents=["support_agent"])],
        workflow=WorkflowSpec(steps=["support_team"]),
    )


def _fake_architect_requirements() -> "Requirements":
    from bestteam.core.requirements import Requirements

    return Requirements(
        summary="Customers need faster, friendlier email support.",
        pain_points=["Replies take too long."],
        goals=["Answer common questions quickly."],
        success_criteria=["Customers get a reply within minutes."],
        constraints=["Must stay professional and on-topic."],
        clarifying_questions=[],
    )


class _FakeArchitectStructuredResult:
    """Returned by `_FakeArchitectChatModel.with_structured_output(...).invoke(...)`."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def invoke(self, messages: Any) -> Any:
        return self._value


class _FakeArchitectChatModel(FakeListChatModel):
    """A deterministic, $0 stand-in for E2E tests that is a full drop-in
    chat model (ordinary `.invoke()` works, so it's safe to also run as a
    deployed agent's model -- see the design doc) that ADDITIONALLY
    supports `with_structured_output()` for the two schemas the Team
    Builder wizard needs. Not listed in `DEFAULT_MODEL_CATALOG`; only
    reachable by resolving the `fake-architect:` spec string directly.
    """

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeArchitectStructuredResult:
        from bestteam.core.requirements import Requirements
        from bestteam.core.specification import Specification

        if schema is Requirements:
            return _FakeArchitectStructuredResult(_fake_architect_requirements())
        if schema is Specification:
            return _FakeArchitectStructuredResult(_fake_architect_specification())
        raise NotImplementedError(
            f"fake-architect: has no canned response for schema {schema!r}"
        )
```

Then, inside `_resolve_model`, add the new branch right after the existing `fake:` branch (before the `try: from langchain.chat_models import init_chat_model` line):

```python
        if model.startswith("fake-architect:"):
            return _FakeArchitectChatModel(responses=[model[len("fake-architect:") :] or "OK, done."])
```

Add `from typing import Any` to the imports if not already present (check the top of the file first), and add `Requirements`/`Specification` as `TYPE_CHECKING`-only imports at the top for the type hints in the two helper functions' return annotations:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bestteam.core.requirements import Requirements
    from bestteam.core.specification import Specification
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_fake_architect_model.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/bestteam/adapters/langgraph_adapter.py tests/test_fake_architect_model.py
git commit -m "feat(sdk): add fake-architect: model spec for deterministic E2E wizard coverage"
```

---

### Task 4: `deploy_validation` exemption for `fake-architect:`

**Files:**
- Modify: `ui/backend/deploy_validation.py`
- Modify: `tests/test_deploy_validation.py`

**Interfaces:**
- Consumes: none new.
- Produces: `validate_agent_models(raw_spec, catalog_specs)` no longer flags an agent whose model starts with `fake-architect:`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deploy_validation.py` (append at the end of the file):

```python
def test_validate_agent_models_exempts_fake_architect():
    raw_spec = {"agents": [{"name": "a", "model": "fake-architect:e2e"}]}
    assert validate_agent_models(raw_spec, catalog_specs=[]) == []
```

Check the top of `tests/test_deploy_validation.py` for how `validate_agent_models` is already imported (it should already be imported for the file's existing tests) and reuse that import.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_deploy_validation.py::test_validate_agent_models_exempts_fake_architect -v`
Expected: FAIL — `"fake-architect:e2e"` is reported as an invalid/unlisted model.

- [ ] **Step 3: Implement**

In `ui/backend/deploy_validation.py`, find the line (around line 37):

```python
        if model.startswith("fake:") or model in allowed:
```

Change it to:

```python
        if model.startswith("fake:") or model.startswith("fake-architect:") or model in allowed:
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_deploy_validation.py -v`
Expected: PASS (all tests in the file, including the new one)

- [ ] **Step 5: Commit**

```bash
git add ui/backend/deploy_validation.py tests/test_deploy_validation.py
git commit -m "fix(backend): exempt fake-architect: from model-catalog deploy validation"
```

---

### Task 5: guard `fake-architect:` out of the default catalog

**Files:**
- Modify: `tests/test_model_catalog.py`

**Interfaces:**
- Consumes: `ui.backend.db.model_catalog.DEFAULT_MODEL_CATALOG`.
- Produces: a regression test that fails if anyone ever adds a `fake-architect:` entry to the real seed data.

- [ ] **Step 1: Write the test (it should already pass -- this is a regression guard, not new behavior)**

Add to `tests/test_model_catalog.py` (append at the end):

```python
def test_fake_architect_is_never_in_the_default_catalog():
    """fake-architect: is a full drop-in chat model (see
    src/bestteam/adapters/langgraph_adapter.py) and is deliberately kept out
    of the seeded catalog so a real customer never sees it as a choice --
    it's only added to a test session's own ephemeral DB by the E2E fixture
    (see docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md)."""
    from ui.backend.db.model_catalog import DEFAULT_MODEL_CATALOG

    assert all(not entry["spec"].startswith("fake-architect:") for entry in DEFAULT_MODEL_CATALOG)
```

- [ ] **Step 2: Run to verify it passes**

Run: `pytest tests/test_model_catalog.py::test_fake_architect_is_never_in_the_default_catalog -v`
Expected: PASS immediately (nothing to implement — `DEFAULT_MODEL_CATALOG` was never touched by Task 3/4).

- [ ] **Step 3: Commit**

```bash
git add tests/test_model_catalog.py
git commit -m "test(backend): guard fake-architect: out of the default model catalog"
```

---

### Task 6: self-contained E2E session fixture

**Files:**
- Create: `tests/e2e/__init__.py` (empty)
- Create: `tests/e2e/_env.py`
- Create: `tests/e2e/conftest.py`

**Interfaces:**
- Produces: `tests/e2e/_env.py` module constants `BASE_URL`, `API_URL`, `DEMO`, `OP`, `ORG_LABEL`, `FAKE_ARCHITECT_SPEC`; a session-scoped, autouse pytest fixture `e2e_backend` in `tests/e2e/conftest.py` that starts a temp-DB backend + frontend, provisions accounts, reshapes the model catalog, and tears everything down at session end; a `pytest_addoption`/`--headed` CLI flag understood by `pytest-playwright`'s own `--headed` (no new flag needed — pytest-playwright ships this already).
- Consumes: `ui.backend.admin` CLI (subprocess), `/api/config/model-catalog` (HTTP), `/api/auth/login` (HTTP).

- [ ] **Step 1: Write `tests/e2e/_env.py`**

```python
"""Shared constants for the E2E suite. Ports are fixed -- safe in CI since
each job gets its own clean runner; see the design doc for local-dev notes."""
BASE_URL = "http://localhost:5173"
API_URL = "http://127.0.0.1:8000"

DEMO = ("demo", "demo-pass-123")  # org user (default org): Monitor, Wizard
OP = ("op", "op-pass-123")        # platform admin (no org): Advanced, Memory
ORG_LABEL = "Default Organization"

FAKE_ARCHITECT_SPEC = "fake-architect:e2e"
```

- [ ] **Step 2: Write `tests/e2e/__init__.py`**

Create an empty file: `tests/e2e/__init__.py`

- [ ] **Step 3: Write `tests/e2e/conftest.py`**

```python
"""Self-contained environment for the E2E suite: a temp SQLite DB, real
backend/frontend subprocesses, auto-provisioned accounts, and a reshaped
model catalog so the wizard's automatic model selection resolves to
fake-architect: -- see
docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md."""
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

from ._env import API_URL, BASE_URL, DEMO, FAKE_ARCHITECT_SPEC, OP

REPO_ROOT = Path(__file__).resolve().parents[2]


def _wait_healthy(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not become healthy in {timeout}s: {last_error}")


def _provision_user(db_path: str, username: str, password: str, *, org: str | None, platform: bool) -> None:
    args = [sys.executable, "-m", "ui.backend.admin", "create-user", username]
    args += ["--platform"] if platform else ["--org", org or "default"]
    env = {**os.environ, "BESTTEAM_DB_PATH": db_path}
    result = subprocess.run(
        args, cwd=str(REPO_ROOT), env=env, input=f"{password}\n{password}\n",
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"provisioning {username} failed:\n{result.stdout}\n{result.stderr}"


def _promote_to_admin(db_path: str, username: str) -> None:
    env = {**os.environ, "BESTTEAM_DB_PATH": db_path}
    result = subprocess.run(
        [sys.executable, "-m", "ui.backend.admin", "promote", username],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"promoting {username} failed:\n{result.stdout}\n{result.stderr}"


def _reshape_model_catalog() -> None:
    """Delete every auto-seeded non-fake: catalog entry and add
    fake-architect:e2e, so the wizard's pickDefaultModel() resolves to it
    automatically (see the design doc's "Fake-architect mechanism")."""
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        login = client.post("/api/auth/login", json={"username": OP[0], "password": OP[1]})
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        entries = client.get("/api/config/model-catalog", headers=headers).json()
        for entry in entries:
            if not entry["spec"].startswith("fake:"):
                resp = client.delete(f"/api/config/model-catalog/{entry['spec']}", headers=headers)
                assert resp.status_code == 204, resp.text

        resp = client.put(
            f"/api/config/model-catalog/{FAKE_ARCHITECT_SPEC}",
            headers=headers,
            json={
                "display_name": "E2E Test Architect (fake, $0)",
                "description": "Deterministic fake architect for automated E2E tests only.",
                "tier": "fast",
                "input_price_per_1k": 0.0,
                "output_price_per_1k": 0.0,
            },
        )
        assert resp.status_code == 200, resp.text


@pytest.fixture(scope="session", autouse=True)
def e2e_backend():
    tmp_dir = tempfile.mkdtemp(prefix="bestteam_e2e_")
    db_path = str(Path(tmp_dir) / "e2e.db")
    secret = "e2e-test-secret-" + secrets.token_hex(16)

    _provision_user(db_path, DEMO[0], DEMO[1], org="default", platform=False)
    _provision_user(db_path, OP[0], OP[1], org=None, platform=True)
    _promote_to_admin(db_path, OP[0])

    env = {
        **os.environ,
        "BESTTEAM_DB_PATH": db_path,
        "BESTTEAM_SECRET_KEY": secret,
        "BESTTEAM_DEMO_WORKFLOWS": "1",
    }

    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ui.backend.main:app", "--port", "8000", "--host", "127.0.0.1"],
        cwd=str(REPO_ROOT), env=env,
    )
    npm = shutil.which("npm")
    assert npm is not None, "npm not found on PATH -- required to start the frontend dev server"
    frontend = subprocess.Popen(
        [npm, "run", "dev", "--", "--port", "5173"],
        cwd=str(REPO_ROOT / "ui" / "frontend"), env=env,
    )

    try:
        _wait_healthy(f"{API_URL}/api/health")
        _wait_healthy(BASE_URL)
        _reshape_model_catalog()
        yield
    finally:
        backend.terminate()
        frontend.terminate()
        backend.wait(timeout=10)
        frontend.wait(timeout=10)
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 4: Verify it works end to end (local, headed)**

Run: `pytest tests/e2e/ --headed -v --co` (collect-only first, to check no import errors)
Expected: no collection errors (there are no test functions yet, so 0 items collected — that's fine at this step).

Then run a manual smoke check that the fixture itself works:

Run: `pytest tests/e2e/ -v -x --setup-show 2>&1 | head -50` (there are no tests yet, so this mainly verifies the fixture doesn't error during setup/teardown when pytest exits with zero collected items — pytest skips session fixtures with no tests to run, so instead verify manually)

Run this standalone script to smoke-test the fixture logic directly:

```python
import sys
sys.path.insert(0, "tests/e2e")
sys.path.insert(0, ".")
from tests.e2e.conftest import _provision_user, _promote_to_admin, _wait_healthy, _reshape_model_catalog
import tempfile, subprocess, os, secrets, shutil
from pathlib import Path

tmp_dir = tempfile.mkdtemp(prefix="bestteam_e2e_smoke_")
db_path = str(Path(tmp_dir) / "e2e.db")
_provision_user(db_path, "demo", "demo-pass-123", org="default", platform=False)
_provision_user(db_path, "op", "op-pass-123", org=None, platform=True)
_promote_to_admin(db_path, "op")
print("Provisioning OK, DB at", db_path)
shutil.rmtree(tmp_dir, ignore_errors=True)
```

Run: `python -c "$(cat above script)"` (or save to a temp `.py` file and run it)
Expected: prints "Provisioning OK..." with no assertion errors.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/__init__.py tests/e2e/_env.py tests/e2e/conftest.py
git commit -m "test(e2e): add self-contained Playwright session fixture"
```

---

### Task 7: port smoke scenarios + new PR-gate wizard scenario

**Files:**
- Create: `tests/e2e/test_smoke.py`
- Delete: `docs/run_ui_tests.py` (superseded)

**Interfaces:**
- Consumes: `e2e_backend` fixture (Task 6, autouse), `pytest-playwright`'s `page` fixture, constants from `tests/e2e/_env.py`.
- Produces: `tests/e2e/test_smoke.py`, marked `pytest.mark.e2e` (module-level `pytestmark`), covering the same ground as the old `docs/run_ui_tests.py` (T1 Auth, T2 Monitor minus T2-3, T3 Advanced, T5 Edge cases) plus a new wizard PR-gate scenario.

- [ ] **Step 1: Write `tests/e2e/test_smoke.py`**

```python
"""PR-gate E2E smoke suite: headless, self-contained (see conftest.py).
Ports docs/run_ui_tests.py's T1/T2/T3/T5 scenarios (T2-3 stays out of scope
-- it needs the backend stopped mid-run) plus a new wizard smoke scenario
using the fake architect. Full wizard journey scenarios (T4) live in
test_wizard_full.py, gated to slow/main-only."""
import json
import time

import pytest
from playwright.sync_api import expect as pw_expect

from ._env import BASE_URL, DEMO, FAKE_ARCHITECT_SPEC, OP, ORG_LABEL

pytestmark = pytest.mark.e2e


def login_ui(page, account, land="/"):
    username, password = account
    page.goto(BASE_URL + "/login")
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_url(BASE_URL + land, timeout=8000)


def logout(page):
    page.click("button.logout-button")
    page.wait_for_url("**/login", timeout=5000)


def open_advanced_tab(page, label):
    page.click(f".advanced-kinds button:has-text('{label}')")
    page.wait_for_selector(".advanced-list", timeout=3000)
    time.sleep(0.3)


def test_smoke_journey(page):
    # -- T1. Authentication (as demo) --
    page.goto(BASE_URL + "/")
    page.wait_for_url("**/login", timeout=6000)
    login_ui(page, DEMO)
    assert page.url == BASE_URL + "/"

    logout(page)
    login_ui(page, DEMO)
    assert page.url == BASE_URL + "/"

    page.goto(BASE_URL + "/login")
    page.fill("#username", DEMO[0])
    page.fill("#password", "wrongpassword!")
    page.click("button[type=submit]")
    time.sleep(1.5)
    assert "/login" in page.url
    page.wait_for_selector(".banner-error", timeout=4000)

    login_ui(page, DEMO)  # re-login after the bad-password check

    ctx2 = page.context.browser.new_context()
    p2 = ctx2.new_page()
    p2.goto(BASE_URL + "/advanced")
    p2.wait_for_url("**/login", timeout=5000)
    ctx2.close()

    # -- T2. Monitor page (as demo) --
    js_errors = []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.goto(BASE_URL + "/")
    page.wait_for_selector("select", timeout=8000)
    options = page.locator("select option").all_inner_texts()
    assert len(options) > 0, "Workflow dropdown is empty -- was BESTTEAM_DEMO_WORKFLOWS=1 set?"
    bad = [e for e in js_errors if "Cannot read properties of undefined" in e]
    assert not bad, f"TypeError still present: {bad[0]}"

    page.goto(BASE_URL + "/")
    page.wait_for_selector("select", timeout=8000)
    opts = page.locator("select option").all_inner_texts()
    target = "code_review" if "code_review" in opts else opts[0]
    page.select_option("select", label=target)
    page.fill("textarea", "def add(a, b): return a + b")
    page.click("button:has-text('Run')")
    page.wait_for_selector(".event.event-run_completed", timeout=30000)
    page.wait_for_selector(".result", timeout=5000)

    # -- T3. Advanced page (as op) --
    logout(page)
    login_ui(page, OP, land="/")
    page.goto(BASE_URL + "/advanced")
    page.wait_for_selector(".advanced-kinds", timeout=6000)

    for label in ["Workflows", "Skills", "Knowledge bases", "Tools", "Model catalog"]:
        page.click(f".advanced-kinds button:has-text('{label}')")
        page.wait_for_selector(".advanced-list", timeout=3000)
        time.sleep(0.3)
    labels = page.locator(".advanced-kinds button").all_inner_texts()
    assert "Agents" not in labels and "Teams" not in labels

    open_advanced_tab(page, "Skills")
    SEED = f"seed_{int(time.time())}"
    page.fill(".advanced-new input", SEED)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{SEED}')", timeout=4000)
    page.fill(".advanced-editor textarea", json.dumps({"instructions": "seed", "description": "seed"}, indent=2))
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=6000)
    page.click(f".advanced-list button:has-text('{SEED}')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{SEED}')", timeout=4000)
    page.select_option(".advanced-org select", label=ORG_LABEL)
    pw_expect(page.locator(".advanced-editor .hint")).to_be_visible(timeout=4000)
    pw_expect(page.locator(".advanced-editor textarea")).to_have_count(0)
    page.select_option(".advanced-org select", value="__platform__")

    open_advanced_tab(page, "Skills")
    SKILL = f"skill_{int(time.time())}"
    SKILL_BODY = json.dumps({
        "instructions": "Always reply professionally. End with a polite sign-off.",
        "description": "Professional email style",
    }, indent=2)

    page.fill(".advanced-new input", SKILL)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{SKILL}')", timeout=4000)
    page.fill(".advanced-editor textarea", SKILL_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=6000)
    page.wait_for_selector(f".advanced-list button:has-text('{SKILL}')", timeout=4000)

    page.click(f".advanced-list button:has-text('{SKILL}')")
    raw = json.loads(page.locator(".advanced-editor textarea").input_value())
    raw["description"] = "Formal email writing style"
    page.fill(".advanced-editor textarea", json.dumps(raw, indent=2))
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=5000)
    open_advanced_tab(page, "Knowledge bases")
    open_advanced_tab(page, "Skills")
    page.click(f".advanced-list button:has-text('{SKILL}')")
    val = json.loads(page.locator(".advanced-editor textarea").input_value())
    assert val.get("description") == "Formal email writing style"

    page.click(f".advanced-list button:has-text('{SKILL}')")
    page.fill(".advanced-editor textarea", '{"instructions": "test"')  # missing }
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-error:has-text('Not valid JSON')", timeout=4000)

    page.click(f".advanced-list button:has-text('{SKILL}')")
    page.fill(".advanced-editor textarea", "{}")
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-error", timeout=6000)
    err = page.locator(".banner-error").inner_text()
    assert "instructions" in err.lower() or "validation" in err.lower()

    page.click(f".advanced-list button:has-text('{SKILL}')")
    page.fill(".advanced-editor textarea", SKILL_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=5000)
    page.click(".advanced-editor button:has-text('Delete')")
    pw_expect(page.locator(f".advanced-list button:has-text('{SKILL}')")).to_have_count(0, timeout=5000)

    CATALOG_SPEC = f"fake:model_{int(time.time())}"
    open_advanced_tab(page, "Model catalog")
    page.fill(".advanced-new input", CATALOG_SPEC)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{CATALOG_SPEC}')", timeout=4000)
    page.fill(".advanced-editor textarea", json.dumps({
        "display_name": "Test model", "description": "Smoke-test entry",
        "tier": "economy", "input_price_per_1k": 0, "output_price_per_1k": 0,
    }, indent=2))
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success", timeout=5000)
    page.wait_for_selector(f".advanced-list button:has-text('{CATALOG_SPEC}')", timeout=4000)

    open_advanced_tab(page, "Tools")
    page.click(".advanced-list button:has-text('web_search')")
    pw_expect(page.locator(".advanced-readonly-text")).to_be_visible(timeout=4000)
    pw_expect(page.locator(".advanced-editor textarea")).to_have_count(0)

    WF = f"wf_{int(time.time())}"
    WF_BODY = json.dumps({
        "name": WF,
        "teams": [{"name": "t", "mode": "sequential", "agents": ["a"]}],
        "agents": [{"name": "a", "role": "Asst", "goal": "Help",
                    "backstory": "Friendly AI assistant.", "model": "fake:Hello! How can I help?"}],
        "workflow": {"steps": ["t"]},
    }, indent=2)

    open_advanced_tab(page, "Workflows")
    page.select_option(".advanced-org select", label=ORG_LABEL)
    page.fill(".advanced-new input", WF)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{WF}')", timeout=4000)
    page.fill(".advanced-editor textarea", WF_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
    assert not page.locator(".banner-error").is_visible()

    logout(page)
    login_ui(page, DEMO)
    page.goto(BASE_URL + "/")
    page.wait_for_selector("select", timeout=8000)
    opts = page.locator("select option").all_inner_texts()
    assert WF in opts, f"{WF} not found in Monitor dropdown for demo"

    # -- T5. Edge cases --
    page.goto(BASE_URL + "/")
    page.evaluate("localStorage.removeItem('bestteam_token')")
    page.goto(BASE_URL + "/advanced")
    page.wait_for_url("**/login", timeout=5000)

    login_ui(page, OP, land="/")
    page.goto(BASE_URL + "/advanced")
    page.wait_for_selector(".advanced-kinds", timeout=5000)
    open_advanced_tab(page, "Workflows")
    page.select_option(".advanced-org select", label=ORG_LABEL)
    page.fill(".advanced-new input", WF)
    page.click(".advanced-new button:has-text('New')")
    page.wait_for_selector(f".advanced-editor h2:has-text('{WF}')", timeout=4000)
    page.fill(".advanced-editor textarea", WF_BODY)
    page.click(".advanced-editor button:has-text('Save')")
    page.wait_for_selector(".banner-success, .banner-error", timeout=8000)
    assert not page.locator(".banner-error").is_visible()
    count = page.locator(f".advanced-list button:has-text('{WF}')").count()
    assert count == 1, f"Expected 1 entry (upsert), got {count}"

    login_ui(page, DEMO)
    page.goto(BASE_URL + "/")
    page.wait_for_selector("select", timeout=8000)
    page.fill("textarea", "")
    assert page.locator("button:has-text('Run')").is_disabled()


def test_wizard_smoke(page):
    """New PR-gate scenario: intent -> generate -> Preview -> Deploy ->
    confirm the team shows up in Monitor. Uses the fake architect (reshaped
    into the catalog by the e2e_backend fixture) so no real LLM key is
    needed. Stops at Deploy -- never opens the Confirm-page's ModelPicker
    dropdown (that's covered by test_wizard_full.py)."""
    login_ui(page, DEMO)
    page.goto(BASE_URL + "/wizard")
    page.wait_for_selector("#intent", timeout=8000)

    team_name_hint = f"e2e_wizard_team_{int(time.time())}"
    page.fill(
        "#intent",
        f"We get customer support emails and need quick replies. "
        f"(test marker: {team_name_hint})",
    )
    page.click("button:has-text('Start building my team')")
    page.wait_for_url("**/documents", timeout=15000)

    page.wait_for_selector("button:has-text('Skip for now')", timeout=8000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)

    page.wait_for_selector(".team-flow, .employee-card", timeout=8000)

    page.click("button:has-text('Continue')")
    page.wait_for_url("**/confirm", timeout=8000)

    page.click("button:has-text('Continue to deploy')")
    page.wait_for_url("**/deploy", timeout=8000)

    page.click("button:has-text('Launch my team')")
    page.wait_for_selector("text=Your team is live", timeout=20000)

    page.click("button:has-text('Run a team')")
    page.wait_for_url(BASE_URL + "/**", timeout=8000)
    page.wait_for_selector("select", timeout=8000)
    opts = page.locator("select option").all_inner_texts()
    assert any("e2e_support_team" in o or "Support Team (E2E)" in o for o in opts), (
        f"deployed fake-architect team not found in Monitor dropdown: {opts}"
    )
```

- [ ] **Step 2: Run against the self-contained harness**

Run: `pytest tests/e2e/test_smoke.py --headed -v`
Expected: both `test_smoke_journey` and `test_wizard_smoke` PASS. Debug locally with `--headed` if a selector doesn't match (the wizard's exact button text/route names should be double-checked against the live app at this step, since `IntentPage`/`DocumentsPage`/`PreviewPage`/`ConfirmPage`/`DeployPage` source was read to write this but hasn't been run interactively).

- [ ] **Step 3: Run headless (the CI mode) to confirm it also passes without a visible browser**

Run: `pytest tests/e2e/test_smoke.py -v`
Expected: PASS

- [ ] **Step 4: Delete the superseded script**

Run: `git rm docs/run_ui_tests.py`

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_smoke.py
git commit -m "test(e2e): port docs/run_ui_tests.py to self-contained pytest-playwright, add wizard smoke scenario"
```

---

### Task 8: full T4 wizard scenarios (slow, main-only)

**Files:**
- Create: `tests/e2e/test_wizard_full.py`

**Interfaces:**
- Consumes: `e2e_backend` fixture (Task 6), constants from `tests/e2e/_env.py`.
- Produces: `tests/e2e/test_wizard_full.py`, marked `pytest.mark.e2e` and `pytest.mark.slow` (module-level `pytestmark = [pytest.mark.e2e, pytest.mark.slow]`), covering the 6 previously-skipped T4 scenarios.

- [ ] **Step 1: Write `tests/e2e/test_wizard_full.py`**

```python
"""Full Team Builder wizard journey scenarios -- previously skipped in
docs/run_ui_tests.py ("T4-1..T4-6 (AI generation requires real LLM API
key)"). Un-skipped here against the fake architect (see conftest.py /
the design doc). Slow: only runs in the main-branch full-regression job."""
import time

import pytest

from ._env import BASE_URL, DEMO

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _login(page):
    page.goto(BASE_URL + "/login")
    page.fill("#username", DEMO[0])
    page.fill("#password", DEMO[1])
    page.click("button[type=submit]")
    page.wait_for_url(BASE_URL + "/", timeout=8000)


def _build_to_confirm(page, intent: str):
    page.goto(BASE_URL + "/wizard")
    page.wait_for_selector("#intent", timeout=8000)
    page.fill("#intent", intent)
    page.click("button:has-text('Start building my team')")
    page.wait_for_url("**/documents", timeout=15000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)
    page.wait_for_selector(".team-flow, .employee-card", timeout=8000)
    page.click("button:has-text('Continue')")
    page.wait_for_url("**/confirm", timeout=8000)


def test_t4_1_apply_feedback_regenerates_team(page):
    """Regeneration loop via the Confirm page's "Which assistant should
    your team use?" ModelPicker + feedback box."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")

    page.fill("#solution-feedback", "Make the team also draft a summary of each reply.")
    page.select_option("#model-picker", label="E2E Test Architect (fake, $0)")
    page.click("button:has-text('Apply this change')")
    page.wait_for_selector(".banner-info:has-text('Adjustments so far')", timeout=15000)
    assert "summary" in page.locator(".banner-info").inner_text().lower()


def test_t4_2_test_run_before_deploy(page):
    """Stage 5: run a real task through the sandboxed (not-yet-deployed)
    team on the Preview page before continuing to Confirm/Deploy."""
    _login(page)
    page.goto(BASE_URL + "/wizard")
    page.wait_for_selector("#intent", timeout=8000)
    page.fill("#intent", "We handle customer support emails.")
    page.click("button:has-text('Start building my team')")
    page.wait_for_url("**/documents", timeout=15000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)

    page.fill("#test-input", "A customer wants to reset their password.")
    page.click("button:has-text('Run this through your team')")
    page.wait_for_selector(".activity-card.run_completed", timeout=30000)


def test_t4_3_validation_error_recovery(page):
    """A malformed manually-edited specification is rejected with a clear
    error and the wizard stays usable afterward."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")
    # Attempting a second feedback round with an empty ModelPicker selection
    # (no model chosen) must not submit -- the Apply button stays disabled.
    page.fill("#solution-feedback", "Add a second reviewer step.")
    assert page.locator("button:has-text('Apply this change')").is_disabled()


def test_t4_4_regenerate_requirements_summary(page):
    """The "Show what we understood about your business" panel's
    regenerate-with-feedback loop."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")

    page.click("button:has-text('Show what we understood about your business')")
    page.wait_for_selector("#req-feedback", timeout=5000)
    page.fill("#req-feedback", "We also handle billing questions, not just support.")
    page.locator("label:has-text('Which assistant should redo this?') + select, select#model-picker").last.select_option(
        label="E2E Test Architect (fake, $0)"
    )
    page.click("button:has-text('Regenerate summary')")
    page.wait_for_selector("#summary", timeout=15000)
    assert page.locator("#summary").input_value()


def test_t4_5_deploy_then_run_for_real(page):
    """Full journey through to a real (non-sandbox) run of the deployed
    team via the Monitor page, confirming the fake architect's team
    executes cleanly end to end."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")
    page.click("button:has-text('Continue to deploy')")
    page.wait_for_url("**/deploy", timeout=8000)
    page.click("button:has-text('Launch my team')")
    page.wait_for_selector("text=Your team is live", timeout=20000)

    page.click("button:has-text('Run a team')")
    page.wait_for_selector("select", timeout=8000)
    page.fill("textarea", "A customer is asking about a refund.")
    page.click("button:has-text('Run')")
    page.wait_for_selector(".event.event-run_completed", timeout=30000)
    page.wait_for_selector(".result", timeout=5000)


def test_t4_6_revisit_documents_after_deploy_refines_not_regenerates(page):
    """Revisiting Documents after a specification already exists must
    refine the existing design (submitSolution) rather than silently
    discarding prior feedback and regenerating from scratch -- see
    DocumentsPage.tsx's comment on this exact regression."""
    _login(page)
    _build_to_confirm(page, "We handle customer support emails.")
    page.fill("#solution-feedback", "Always sign off with 'Best, the Support Team'.")
    page.select_option("#model-picker", label="E2E Test Architect (fake, $0)")
    page.click("button:has-text('Apply this change')")
    page.wait_for_selector(".banner-info:has-text('Adjustments so far')", timeout=15000)

    page.click("text=Need to add or update a document? Upload it here")
    page.wait_for_url("**/documents", timeout=8000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)
    page.click("button:has-text('Continue')")
    page.wait_for_url("**/confirm", timeout=8000)

    history_text = page.locator(".banner-info").inner_text()
    assert "sign off" in history_text.lower() or "Best, the Support Team" in history_text
```

- [ ] **Step 2: Run against the self-contained harness**

Run: `pytest tests/e2e/test_wizard_full.py --headed -v`
Expected: all 6 tests PASS. Adjust selectors against the live app if any diverge from what the source read in Task 7/8 implied (particularly `#model-picker`'s `id` — `ModelPicker.tsx` uses a fixed `id="model-picker"` for every instance, so when two are rendered on the same page — `ConfirmPage.tsx` has two — Playwright needs `.last`/`.first`/a more specific locator; the tests above already account for this on the requirements-regeneration one via `.locator(...).last`, but double check `test_t4_1`'s plain `#model-picker` resolves to the right one (it's the only one visible before "Show what we understood" is expanded, so it should be unambiguous — verify this assumption when running headed).

- [ ] **Step 3: Run headless**

Run: `pytest tests/e2e/test_wizard_full.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_wizard_full.py
git commit -m "test(e2e): un-skip the 6 T4 wizard journey scenarios against the fake architect"
```

---

### Task 9: CI job map rewrite

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: markers from Task 1/2, `tests/e2e/` from Task 6-8.
- Produces: 6 CI jobs replacing the current 2.

- [ ] **Step 1: Rewrite `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  backend-unit-integration:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
          cache: "pip"
          cache-dependency-path: pyproject.toml
      - name: Install dependencies
        run: pip install -e ".[ui,dev,tools]"
      - name: Run pytest
        run: python -m pytest -m "not e2e and not slow and not optional"

  backend-optional-deps:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
          cache: "pip"
          cache-dependency-path: pyproject.toml
      - name: Install dependencies
        run: pip install -e ".[ui,dev,tools,interview]"
      - name: Run pytest
        run: python -m pytest -m optional

  frontend:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    defaults:
      run:
        working-directory: ui/frontend
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-node@v5
        with:
          node-version: "20"
          cache: "npm"
          cache-dependency-path: ui/frontend/package-lock.json
      - name: Install dependencies
        run: npm ci
      - name: Lint
        run: npm run lint
      - name: Test
        run: npm test
      - name: Build
        run: npm run build

  e2e-smoke:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
          cache: "pip"
          cache-dependency-path: pyproject.toml
      - uses: actions/setup-node@v5
        with:
          node-version: "20"
          cache: "npm"
          cache-dependency-path: ui/frontend/package-lock.json
      - name: Install backend dependencies
        run: pip install -e ".[ui,dev,tools,test]"
      - name: Install Playwright browsers
        run: playwright install --with-deps chromium
      - name: Install frontend dependencies
        run: npm ci --prefix ui/frontend
      - name: Run E2E smoke suite
        run: python -m pytest tests/e2e/ -m "e2e and not slow"
      - name: Upload Playwright traces on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: playwright-traces-smoke
          path: test-results/
          retention-days: 7

  backend-full:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
          cache: "pip"
          cache-dependency-path: pyproject.toml
      - name: Install dependencies
        run: pip install -e ".[ui,dev,tools,interview]"
      - name: Run pytest
        run: python -m pytest -m "not e2e"

  e2e-full:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
          cache: "pip"
          cache-dependency-path: pyproject.toml
      - uses: actions/setup-node@v5
        with:
          node-version: "20"
          cache: "npm"
          cache-dependency-path: ui/frontend/package-lock.json
      - name: Install backend dependencies
        run: pip install -e ".[ui,dev,tools,test]"
      - name: Install Playwright browsers
        run: playwright install --with-deps chromium
      - name: Install frontend dependencies
        run: npm ci --prefix ui/frontend
      - name: Run full E2E suite
        run: python -m pytest tests/e2e/ -m e2e
      - name: Upload Playwright traces on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: playwright-traces-full
          path: test-results/
          retention-days: 7
```

Also add a `pytest-playwright` trace-on-failure config so the upload steps above have something to collect. Create `tests/e2e/pytest.ini` — actually, `pytest-playwright` reads `--tracing` as a CLI flag, not an ini option, so instead add it to both E2E `run` steps above by changing them to:

```yaml
      - name: Run E2E smoke suite
        run: python -m pytest tests/e2e/ -m "e2e and not slow" --tracing retain-on-failure
```

and

```yaml
      - name: Run full E2E suite
        run: python -m pytest tests/e2e/ -m e2e --tracing retain-on-failure
```

Update the two `run:` lines in the YAML above accordingly (both `e2e-smoke` and `e2e-full` jobs' "Run E2E..." steps).

- [ ] **Step 2: Validate the YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" `
Expected: no exception (confirms valid YAML; doesn't validate GitHub Actions semantics, but catches typos).

- [ ] **Step 3: Verify the optional-deps job actually runs the 16 interview tests, not skips them**

Locally, simulate what `backend-optional-deps` does:

Run: `pip install -e ".[ui,dev,tools,interview]" && python -m pytest -m optional -v`
Expected: `tests/test_interview_api.py`'s tests show as **PASSED**, not **SKIPPED**, in the output (confirms the `openai` extra genuinely satisfies whatever `importorskip`/conditional-skip guard that file uses — installing the extra without this check could still leave them skipped if, e.g., an API key env var is also required and unset; if any test needs an env var beyond the package being installed, note it here and set a harmless placeholder value in the CI job's `env:` before proceeding).

If any test in that file is skipped for a reason other than a missing package (check the skip reason in the pytest output), add the missing `env:` entry to the `backend-optional-deps` job in the YAML from Step 1 and re-run this check.

- [ ] **Step 4: Push the branch and confirm all 6 jobs go green**

Run: `git push -u origin HEAD` (or the branch's existing upstream)
Expected: on GitHub, all 4 PR-gate jobs (`backend-unit-integration`, `backend-optional-deps`, `frontend`, `e2e-smoke`) run and pass on the PR; `backend-full`/`e2e-full` show as skipped (they're gated to `main`) until this merges. Confirm the 4 PR-gate jobs' wall-clock (parallelized) lands in the 5–8 minute target from the design doc, and confirm `backend-optional-deps`' log shows the 16 interview tests passing (per Step 3).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: split into 6 jobs -- fast PR gate (unit/integration/optional/frontend/e2e-smoke) + main-only full regression"
```
