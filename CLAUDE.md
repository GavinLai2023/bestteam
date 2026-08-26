# bestteam

A commercial multi-agent framework wrapper around LangGraph (package name
`bestteam`). Slogan: **Intent in, BestTeam out** — the single copy lives in
`nav.tagline` (`ui/frontend/src/locales/`), with a hand-maintained duplicate in
`README.md`. It names the goal: wrap LangGraph's power behind a simple,
business-friendly surface so clients write pipelines, not orchestration code.

## Architecture (three layers)

1. **SDK** (`src/bestteam/`) — `Agent`/`Team`/`Pipeline` business-facing
   dataclasses, decoupled from the engine via an `EngineAdapter` ABC
   (`adapters/base.py`). `LangGraphAdapter` is the only implementation; the
   seam exists so another engine could be added without touching the public
   API.
2. **CLI** (`src/bestteam/cli/`) — Typer + Rich. `init` / `run` / `graph`, all
   thin wrappers over `load_pipeline()` + `Pipeline.run()/.visualize()`.
3. **UI** (`ui/`) — monitoring dashboard *and* a guided "Team Builder" wizard.
   FastAPI + WebSocket backend (`ui/backend/`) reuses the SDK directly; React +
   Vite frontend (`ui/frontend/`) streams live trace events and drives the
   wizard.

Pipelines are declarative YAML (`core/loader.py`); see
`ui/backend/pipelines/*.yaml`. A **Pipeline is what the customer-facing UI
calls an "AI team"** — SDK/admin docs use the technical noun because it matches
the class name, the YAML key, and the API/DB schema.

## Common commands

Run everything through the project venv (`./.venv/Scripts/python.exe` on
Windows).

```powershell
# Install / update the environment (the lockfile pins what CI and Docker use)
.\.venv\Scripts\python.exe -m pip install -c requirements.lock -e ".[ui,dev,tools,test]"
# After changing a dependency in pyproject.toml, regenerate the lockfile
uv pip compile pyproject.toml --universal --python-version 3.10 --extra ui --extra dev --extra tools --extra test --extra interview --extra providers-openai -o requirements.lock

# Tests
.\.venv\Scripts\python.exe -m pytest

# CLI: scaffold / run / visualize a pipeline
.\.venv\Scripts\python.exe -m bestteam init my_project
.\.venv\Scripts\python.exe -m bestteam run pipeline.yaml "some input"
.\.venv\Scripts\python.exe -m bestteam graph pipeline.yaml

# Launch checklist for a deployment's environment (FAIL/WARN/OK per variable)
.\.venv\Scripts\python.exe -m ui.backend.admin check-env

# Email-trigger health metrics (poll lag / backlog age / 24h failures / draft
# latency) -- run from cron; a stalled poller can't report itself in-process
.\.venv\Scripts\python.exe -m ui.backend.admin check-health

# Monitoring dashboard — needs BOTH running simultaneously
$env:BESTTEAM_SECRET_KEY = "dev-only-secret-change-me-for-real-use"
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
cd ui\frontend && npm run dev   # http://localhost:5173, talks to backend on :8000
```

## Where to find more

Claude Code auto-loads the nested `CLAUDE.md` below the first time it reads a
file in that directory — so keep them short. Deep reference material belongs in
`docs/`, which is read on demand.

| Path | Covers |
|---|---|
| `src/bestteam/CLAUDE.md` | `Agent`/`Team`/`Pipeline`, the `EngineAdapter` seam, `fake:` specs, collaboration modes |
| `src/bestteam/core/CLAUDE.md` | `Specification`/`Requirements` outputs, knowledge-base implementations, memory |
| `src/bestteam/tools/CLAUDE.md` | Built-in tools and their trust boundaries |
| `ui/backend/CLAUDE.md` | FastAPI backend: APIs, auth, metering, email trigger, ingestion |
| `ui/backend/db/CLAUDE.md` | SQLAlchemy persistence schema |
| `ui/frontend/CLAUDE.md` | React/Vite dashboard and Team Builder wizard |
| `docs/ARCHITECTURE.md` | 5-minute orientation: diagram, module map, tech stack |
| `docs/STATUS.md` | Living kanban: done / in progress / known issues / next |
| `docs/DECISIONS.md` | **Why** decisions were made, so they aren't re-litigated |
| `docs/KNOWLEDGE_BASES.md` | Full KB reference: retrieval, ingestion, metering, grounding |
| `docs/ADMIN_MANUAL.md` | The five admin-only pages; keep current when one changes |

## Known limitations — don't assume these exist

These are abstracted behind interfaces but **not implemented**. Each line links
to where the detail and the reasoning live.

- **Knowledge bases**: no external vector store, no DMS connectors. Types are
  `local_folder` (BM25), `vector` (cosine), `hybrid` (RRF-fused); query
  expansion and reranking are opt-in. Ingestion is incremental, spend is
  metered, and grounding is *checked; enforcement is the opt-in per-agent
  `grounding_policy: observe|retry|refuse`* (default observe = record only) —
  full reference in `docs/KNOWLEDGE_BASES.md`.
- **Per-user memory**: BM25-only by default (opt-in hybrid via
  `BESTTEAM_MEMORY_EMBEDDING_MODEL`). Semantic records get dedup; procedural
  records do not. Admin view/search/delete only — no manual add/edit, no
  retention or quota policy. Disabled unless `BESTTEAM_MEMORY_DB` is set.
- **Alerting**: in-app plus one optional per-org webhook, nothing else. **No
  SMTP anywhere**, deliberately — see `docs/DECISIONS.md`.
- **Inbound mail**: header rules only, no classifier model; the two per-org
  budget caps bound an *estimate*, not a provider bill — see
  `docs/DECISIONS.md`.
- **Attachments**: text only, never from disk, no OCR and no archive
  expansion — see `docs/DECISIONS.md`.
- **Retention**: a purge clears run *content* and keeps *accounting*. Erasure
  by data subject does not exist and won't; a purge is not a secure erase (no
  `VACUUM`). See `ui/backend/retention.py` and `docs/DECISIONS.md`.
- **Live run state**: `RunRegistry` is not rehydrated from the DB on restart,
  so in-flight runs are lost (swept to `failed` by
  `runtime.fail_interrupted_runs`). History itself persists.
- **Caching**: local per-process caches only.
- **Not started**: CrewAI adapter, DEBATE mode, deployment templates.
  (HIERARCHICAL *is* implemented.)

## Testing notes

- All tests use `FakeListChatModel` / `fake:` specs — zero API cost,
  deterministic. The two live-model examples need real quota to run.
- **Every test file needs a `pytestmark`** (`unit`/`integration`/`e2e`/
  `optional`, optionally `slow`). `tests/test_marker_completeness.py` fails the
  suite otherwise, so a new file can't fall outside every CI job's `-m`.
- **E2E** needs the `test` extra, `playwright install chromium`, `npm` on PATH,
  and ports 8000/5173 free (it fails loudly naming the conflict).
- **Password hashing is deliberately cheap in tests.** `tests/conftest.py`
  lowers `_PBKDF2_ITERATIONS` to 1,000 — it was 69% of suite runtime at the
  production 260,000. There is deliberately **no env var or config key**, so
  the real count cannot be misconfigured in a deployment;
  `test_production_pbkdf2_iterations_are_unchanged` reads the literal out of
  `auth.py`'s source. `tests/e2e/` never imports conftest, so it still
  exercises genuine 260k hashing.
- **`-n auto`** (pytest-xdist) for a fast local run: ~2m11s vs ~3m17s. Kept out
  of `addopts` on purpose — it breaks `-x`, `--pdb` and readable tracebacks.
  **Never** use it on `tests/e2e/` (fixed ports).
- **CI path filters compare differently per event**: on a *push*, against the
  previous commit (so a docs-only commit to `main` runs nothing); on a *pull
  request*, against the base branch (so the whole PR's diff decides). The
  filters are allowlists — a new top-level directory must be added there too.
- **`backend-full` and `e2e-full` are gated on `github.ref == 'refs/heads/main'`
  — the *ref*, not the event.** `workflow_dispatch` from a feature branch does
  **not** enable them (verified empirically). To get that coverage before
  merging, run it locally: `python -m pytest -m "not e2e"`, serial, no
  `-n auto`. `backend-full` runs serially in one process on purpose — that is
  what catches ordering and cross-test isolation bugs.
- `fake-architect:` is a deterministic model for E2E coverage of the wizard's
  AI-generation steps, and is deliberately never in `DEFAULT_MODEL_CATALOG`.
