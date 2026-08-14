# E2E Test Harness + CI Test Tiering — Design

## Context

A re-assessment of the test suite (backend: 1,194 tests / 6m19s / 2 warnings;
frontend: 137 tests / ~65s, several React `act(...)` warnings; 20 Playwright
browser scenarios) found the suite has grown substantially since the last
review (backend 982→1,194, frontend 114→137) but is still "thick backend
tests, thin real-user acceptance": the Playwright layer exists but isn't a
CI gate, needs a human to hand-start services and seed accounts, and
explicitly skips the six T4 scenarios that cover the Team Builder wizard —
arguably the product's single most cross-layer, highest-risk user journey.

Of the seven items on the resulting priority list, item #1 ("wire `npm test`
into CI") is already done (commit `d8a7658`, on `main`). This document
covers items #2–#4: a self-contained Playwright harness, fake-architect
coverage of the full Team Builder journey, and a pytest marker / CI job
split (folding in one directly-related gap found during design: the
`interview`/`providers-openai` extras aren't installed in CI, so
`test_interview_api.py`'s 16 tests silently skip there).

Items #5 (warning cleanup), #6 (nightly real-model eval), and #7 (Docker
smoke test) are out of scope for this design — separate future work.

## Goals

- Playwright scenarios run unattended in CI, headless, against a fully
  self-provisioned environment (temp DB, temp accounts, temp services) with
  automatic teardown and failure-artifact collection.
- The Team Builder's full journey (intent → AI-generate → Preview → test
  run → Confirm → Deploy → run) gets deterministic automated coverage
  without calling a paid real model in CI.
- Backend tests split into fast (`unit`/`integration`) vs `slow`/`e2e`
  tiers so PRs get a 5–8 minute gate while `main` still gets full
  regression.
- The `interview`/`providers-openai` extras gap is closed: those 16 tests
  run in every PR, not just on a developer's fuller local environment.

## Non-goals

- No change to what the 1,194 existing pytest tests assert — this is
  about *tiering and running* them, not rewriting their logic.
- No real-model/external-service testing (nightly eval layer is separate,
  later work).
- No Docker-level smoke test (separate, later work).
- No change to the production Team Builder — `fake-architect:` is
  reachable only because `_resolve_model()` accepts arbitrary spec
  strings; it is deliberately excluded from the model catalog customers
  select from.

## Design

### 1. CI job map

Replaces today's 2 jobs (`backend`, `frontend`) with 6:

| Job | Runs | Trigger |
|---|---|---|
| `backend-unit-integration` | `pytest -m "not e2e and not slow and not optional"` | every PR/push |
| `backend-optional-deps` | `pip install .[ui,dev,tools,interview]`; `pytest -m optional` | every PR/push |
| `frontend` | lint, `npm test`, build (unchanged) | every PR/push |
| `e2e-smoke` | `pytest -m "e2e and not slow"`, headless, self-contained | every PR/push |
| `backend-full` | `pip install .[ui,dev,tools,interview]`; `pytest -m "not e2e"` | push to `main` only |
| `e2e-full` | `pytest -m e2e` (all scenarios, incl. all 6 T4 branches) | push to `main` only |

`backend-full` installs the `interview` extra so all non-e2e tests
(including `test_interview_api.py`) genuinely run, rather than silently
skipping again outside the dedicated `backend-optional-deps` job; `e2e` is
excluded because those tests need the Playwright harness (browsers,
spawned frontend/backend servers), which this job doesn't set up.

Target: the four PR-gate jobs run in parallel and land in a 5–8 minute
wall-clock window. Local `pytest` with no args stays unfiltered — a
developer running the suite normally sees no behavior change (it still
needs the `interview` extra installed locally to avoid those 16 skips,
same as today).

### 2. Self-contained Playwright harness

`docs/run_ui_tests.py` (a hand-rolled `sync_playwright` script with its own
pass/fail tracker, requiring a human to pre-start both servers and seed the
`demo`/`op` accounts) is rewritten as `tests/e2e/*.py` using
`pytest-playwright` (new dependency, added as a `test` extra in
`pyproject.toml`).

A session-scoped fixture owns the environment:

- A temp file-based SQLite DB (`tempfile.mkdtemp()` + `BESTTEAM_DB_PATH`) —
  file-based, not `:memory:`, because the backend runs in a separate
  subprocess from the test process and the two must share state.
- `uvicorn` (backend) and the frontend dev/preview server are spawned as
  subprocesses on fixed ports (8000/5173 — safe in CI since each job gets
  its own clean runner), with `BESTTEAM_DEMO_WORKFLOWS=1` and a generated
  `BESTTEAM_SECRET_KEY`. The fixture polls both until healthy before
  yielding.
- The `demo`/`op` accounts are auto-provisioned via the existing
  `ui.backend.admin` CLI (already documented in the current script's
  header) instead of requiring a human to have pre-seeded them.
- Teardown kills both subprocesses and deletes the temp DB directory.
- On any test failure, Playwright's built-in tracing
  (`--tracing=retain-on-failure`) captures screenshot/video/trace, uploaded
  as a CI artifact.

Headless is the default (`headless=True`); a `--headed` flag remains
available for local interactive debugging, replacing today's hardcoded
`headless=False, slow_mo=150`.

### 3. Fake-architect mechanism

`generate_specification()` (`src/bestteam/core/specification.py`)
currently *rejects* the existing `fake:` model spec outright:
`invoke_structured()` raises `NotImplementedError` for a model without
structured-output support, which becomes a customer-facing
`ConfigurationError` ("...for example, a demo 'fake:' model. Choose a real
model..."). A deterministic E2E architect therefore needs a genuinely new
mechanism, not a redirect to the existing `fake:` spec.

**It also needs to be a fully runnable chat model, not just a
structured-output stub** — this was the key thing research for the
implementation plan corrected. `submit_solution_feedback` (Stage 4,
`ui/backend/builder.py:545-546`) unconditionally re-pins every agent's model
to the literal architect model string used for that call
(`for agent in spec.agents: agent.model = req.model`), overwriting whatever
the architect itself assigned. Combined with the wizard's fully-automatic
model selection (below), a stub that only implements
`with_structured_output()` would end up assigned as a deployed agent's model
— then break the moment that team is actually run (a "test run" on the
Preview page, or a real Monitor run), since ordinary agent execution calls
plain `.invoke()`, not structured output.

So `fake-architect:<name>` resolves (`_resolve_model()`,
`src/bestteam/adapters/langgraph_adapter.py`) to a small `FakeListChatModel`
subclass that is a complete drop-in: ordinary `.invoke()` behaves exactly
like `fake:` (a canned string response, safe to run as a real agent), and
`.with_structured_output(schema, **kwargs)` is additionally overridden to
return one canned instance keyed by schema identity — `Requirements` (for
Stage 2, `generate_requirements`) or `Specification` (for Stages 3/4,
`generate_specification`) — mirroring the shape already proven in
`tests/test_builder_api.py`'s `_FakeArchitectChatModel`, but reachable as a
real code path (that test reaches its version only via `monkeypatch` inside
the test process; Playwright drives a real, separately-running backend
process). Any other schema raises `NotImplementedError`, matching how a real
model would fail on an unexpected structured-output request. The canned
`Specification`'s agents carry `"fake:ok"` models internally, but since
Stage 4 can overwrite that with the literal `"fake-architect:e2e"` string
regardless, `deploy_validation.validate_agent_models`
(`ui/backend/deploy_validation.py:37`) gains a second exemption prefix
alongside the existing `fake:` one, so a `fake-architect:`-pinned agent
deploys and runs cleanly too.

`fake-architect:` is deliberately **not** added to `DEFAULT_MODEL_CATALOG`
(`db/model_catalog.py`), so it never appears in a real deployment's catalog.
`IntentPage` and `DocumentsPage` have **no model-picker UI** for their
generation steps (Stage 1→2's Requirements call, Stage 3's Specification
call) — both call `pickDefaultModel(entries)` (`lib/models.ts:6-10`)
automatically, client-side: "the first catalog entry whose `spec` doesn't
start with `fake:`, else the first entry, else `fake:ok`" — with zero user
interaction. `ConfirmPage`, however, *does* render a real `<select>`
(`components/ModelPicker.tsx`, populated from `/api/model-catalog`) for its
two optional actions — "Which assistant should your team use?" (Stage 4
solution feedback, `ConfirmPage.tsx:150`) and "Which assistant should redo
this?" (Requirements regeneration, `ConfirmPage.tsx:247`) — so a T4 scenario
exercising those picks `fake-architect:e2e` from that dropdown by its
`display_name`/`spec`, same as picking any other option. `db_session.py:41`
auto-seeds the full `DEFAULT_MODEL_CATALOG` (including real provider specs
like `openai:gpt-4o-mini`) into every fresh DB at backend startup via
`seed_default_catalog()`.

So the E2E fixture reshapes the catalog before any wizard scenario runs,
authenticated as the auto-provisioned `op` account: delete every auto-seeded
entry whose `spec` does **not** start with `fake:` (via
`DELETE /api/config/model-catalog/{spec}`), then
`PUT /api/config/model-catalog/fake-architect:e2e` to add the new entry with
a friendly `display_name` (same CRUD endpoints `docs/run_ui_tests.py`'s
`T3-8` already exercises through the Advanced page, called directly here
instead of through the browser). What's left is `fake:ok` (ignored by
`pickDefaultModel`'s non-`fake:` filter) plus `fake-architect:e2e` (the only
qualifying entry, and the only non-`fake:ok` option in `ConfirmPage`'s
dropdown) — so every automatic generation step resolves to the fake
architect, and any scenario that explicitly opens the dropdown also has
exactly one real choice to make. Because it's a full drop-in chat model, not
just a structured-output stub, every subsequent test-run/deploy/
production-run of the resulting team works too. This only touches that test
session's own ephemeral DB; a real deployment's seed data is untouched.

Two scenario tiers, both relying on the reshaped catalog:

- **PR-gate scenario** (`e2e`, not `slow`): intent → generate → Preview →
  Deploy → confirm the team appears in Monitor. Stops once deployed; never
  opens the Confirm-page dropdown.
- **Full T4 scenarios** (`e2e` + `slow`, main-only): the 6 existing
  currently-skipped scenarios — including the Confirm-page feedback/
  regeneration loop (picking `fake-architect:e2e` from `ModelPicker`),
  test-run before deploy, validation-error recovery, etc. — un-skipped and
  driven against the fake architect instead of requiring a real LLM key.

### 4. pytest markers

Registered in a new `[tool.pytest.ini_options]` section in `pyproject.toml`
(doesn't exist today): `markers = ["unit", "integration", "e2e", "slow",
"optional"]`, plus `addopts = "--strict-markers"` so a typo'd marker fails
loudly rather than silently matching nothing.

Assignment is a one-time sweep via module-level `pytestmark`, not
per-test annotation:

- Most of the ~62 existing files are integration-style (FastAPI
  `TestClient` + SQLite) → `pytestmark = pytest.mark.integration`.
- True unit tests (pure SDK logic, no DB/HTTP — e.g.
  `test_specification.py`, `test_requirements.py`) → `unit`.
- `test_migrations.py` and other real-Alembic / concurrency-timing tests
  → `slow` added alongside their existing marker.
- `test_interview_api.py` → `optional`.
- New `tests/e2e/*.py` → `e2e` (plus `slow` on the full T4 files).

A meta-test (or CI step) asserts every collected test item carries at
least one of `unit`/`integration`/`e2e`/`optional`, so a test can't
silently fall outside every CI job's selection.

## Testing / verification

- Run the new `tests/e2e/` suite locally, headed, against a real dev
  checkout once, to confirm the auto-provisioning/teardown cycle actually
  works before trusting it in CI.
- Marker-completeness meta-test (above) passes.
- `backend-optional-deps` job: confirm the 16 `test_interview_api.py`
  tests go from "skipped" to "passed" (not just "job exists").
- Unit test asserting `"fake-architect:"` never appears in
  `DEFAULT_MODEL_CATALOG` (`db/model_catalog.py`), alongside tests that
  `_resolve_model("fake-architect:x")` resolves to a model that (a) answers
  `.invoke()` like an ordinary fake chat model and (b) returns the canned
  `Requirements`/`Specification` via `.with_structured_output(...)`, and that
  `deploy_validation.validate_agent_models` accepts a `fake-architect:`-pinned
  agent.
- Push the branch and confirm all 6 CI jobs go green, with the 4 PR-gate
  jobs landing in the 5–8 minute target window (parallelized).

## File-level summary of changes

- `docs/run_ui_tests.py` → `tests/e2e/*.py` (pytest-playwright,
  self-contained, headless by default)
- `src/bestteam/adapters/langgraph_adapter.py`: new `fake-architect:` spec
  in `_resolve_model()`
- `ui/backend/deploy_validation.py`: `validate_agent_models` also exempts
  the `fake-architect:` prefix
- `pyproject.toml`: `[tool.pytest.ini_options]` markers/strict-markers,
  new `test` extra (`pytest-playwright`)
- ~62 existing test files: one-time `pytestmark` sweep
- `.github/workflows/ci.yml`: 2 jobs → 6 jobs (4 PR-gate, 2 main-only)
