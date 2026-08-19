# bestteam

A commercial multi-agent framework wrapper around LangGraph (package name
`bestteam`). Philosophy: **把复杂留给自己，把简单留给客户** — wrap LangGraph's
power behind a simple, business-friendly surface so clients write workflows,
not orchestration code.

## Architecture (three layers)

1. **SDK** (`src/bestteam/`) — `Agent`/`Team`/`Workflow` business-facing
   dataclasses, decoupled from the engine via an `EngineAdapter` ABC
   (`adapters/base.py`). The only implementation today is
   `LangGraphAdapter` (`adapters/langgraph_adapter.py`); the seam exists so a
   CrewAI (or other) adapter could be added later without touching the
   public API. Details: `src/bestteam/CLAUDE.md`.
2. **CLI** (`src/bestteam/cli/`) — Typer + Rich. Commands: `init` (scaffold a
   project), `run` (execute a YAML workflow), `graph` (render Mermaid).
   Thin wrappers over `load_workflow()` + `Workflow.run()/.visualize()`.
3. **UI** (`ui/`) — runtime monitoring dashboard *and* a guided "Team
   Builder" wizard for non-technical customers. FastAPI + WebSocket backend
   (`ui/backend/`) reuses the SDK directly (no duplicated logic); React +
   Vite frontend (`ui/frontend/`) streams live agent trace events and drives
   the wizard. Details: `ui/backend/CLAUDE.md`, `ui/frontend/CLAUDE.md`.

Workflows are declarative YAML (parsed by `core/loader.py`) — see
`ui/backend/workflows/*.yaml` for examples of sequential and parallel
collaboration modes.

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

# CLI: scaffold / run / visualize a workflow
.\.venv\Scripts\python.exe -m bestteam init my_project
.\.venv\Scripts\python.exe -m bestteam run workflow.yaml "some input"
.\.venv\Scripts\python.exe -m bestteam graph workflow.yaml

# Launch checklist for a deployment's environment (FAIL/WARN/OK per variable)
.\.venv\Scripts\python.exe -m ui.backend.admin check-env

# Monitoring dashboard — needs BOTH running simultaneously
$env:BESTTEAM_SECRET_KEY = "dev-only-secret-change-me-for-real-use"
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
cd ui\frontend && npm run dev   # http://localhost:5173, talks to backend on :8000
```

## Where to find more

This file covers project-wide context. Claude Code automatically loads the
relevant file below the first time it reads a file in that directory:

- `src/bestteam/CLAUDE.md` — SDK core: `Agent`/`Team`/`Workflow`, the
  `EngineAdapter`/`LangGraphAdapter` seam, state-reducer / `fake:` model spec
  / error-handling design decisions, HIERARCHICAL collaboration mode.
- `src/bestteam/core/CLAUDE.md` — `Specification`/`Requirements` structured
  outputs for the Team Builder, and the `local_folder`/`vector` knowledge
  base implementations.
- `src/bestteam/tools/CLAUDE.md` — built-in tools (`web_search`,
  `local_business_search`, `parse_file`,
  `http_get`, `calculator`, and the draft-only `email_find`/`email_read`/
  `email_read_attachment`/`email_draft_reply` toolkit) and their trust
  boundaries.
- `ui/backend/CLAUDE.md` — FastAPI backend: builder/config APIs, auth, model
  catalog, usage metering, sync-to-async streaming bridge.
- `ui/backend/db/CLAUDE.md` — SQLAlchemy persistence schema.
- `ui/frontend/CLAUDE.md` — React/Vite monitoring dashboard and Team Builder
  wizard, including the login UI.
- `docs/ARCHITECTURE.md` — 5-minute orientation: architecture diagram,
  module map, tech stack and rationale.
- `docs/STATUS.md` — living kanban: done / in progress / known issues and
  tech debt / next steps.
- `docs/DECISIONS.md` — why significant decisions were made (e.g. LangGraph
  vs CrewAI, org-scoped multi-tenancy), so they aren't re-litigated.

## Known limitations / unimplemented extension points

These are intentionally abstracted behind interfaces but **not yet
implemented** — don't assume they exist:

- **Knowledge bases have no external vector store, no DMS connectors.**
  Three types are supported: `local_folder` (BM25), `vector` (cosine), and
  `hybrid` (BM25 + vector, RRF-fused). All three support opt-in query
  expansion (`query_expansion_model`/`query_expansion_count`, MultiQueryRetriever-style)
  and opt-in reranking (`rerank_model`/`candidate_k`) —
  see `src/bestteam/core/CLAUDE.md`. **KB spend is metered**: query-time
  embedding and expansion calls ride the calling agent's
  `agent_completed.usage` into `usage_records`, and a `vector`/`hybrid`
  ingestion job with a billable embedding spec (a non-`fake:` string) and a
  non-zero token estimate writes one `agent="kb:ingest"` row with a NULL
  `run_id` — a path-constructed KB embeds at load time and is not metered.
  An ad-hoc "Try a search" from the "My documents" panel is the third source:
  one `agent="kb:search"` row with **both** foreign keys NULL, written on the
  failure path too. Embedding token counts are
  *estimated* (±30%) — no provider reports them — and a local reranker is $0,
  so it is not recorded.
- **Per-user memory recall is BM25-only by default; opt-in hybrid (BM25 +
  vector, RRF-fused, with type-aware recency decay) is available via
  `BESTTEAM_MEMORY_EMBEDDING_MODEL`** — query expansion and reranking are
  opt-in too, via `BESTTEAM_MEMORY_QUERY_EXPANSION_MODEL` /
  `BESTTEAM_MEMORY_RERANK_MODEL`. Semantic records get exact-dedup on write plus
  LLM-mediated near-duplicate/update resolution; procedural records still
  have no dedup/consolidation.
  Admins can view/search/delete a user's memory via the admin-only Memory page
  (`/api/memory`); there's no manual add/edit and no retention/quota policy. Disabled by default
  (`BESTTEAM_MEMORY_DB`) — see `core/memory.py` and `src/bestteam/core/CLAUDE.md`.
- **Alerting is in-app + one optional per-org webhook, and nothing else.**
  No SMTP anywhere (deliberate: the draft-only toolkit's containment argument
  is that there is no send verb in the process), no per-user preferences, no
  digests. A trigger raises a notification on a health *transition*
  (`ui/backend/trigger_health.py`), never per occurrence.
- **Inbound mail is filtered by header rules only, and the two per-org budgets
  bound an estimate.** A pure evaluator (`ui/backend/email_filter.py`) decides
  before any model is involved whether a detected message is worth processing —
  a sender blocklist/allowlist, a subject blocklist, and a `skip_bulk` check
  over the standard bulk headers, in that fixed order, with the allowlist
  deliberately *not* exempting a sender from the bulk check. Two pattern forms
  (a full address, `*@domain`), no regular expressions, no classifier model —
  a gatekeeper model would still bill per message, still read
  attacker-controlled text, and could not be audited by the admin whose mail it
  dropped. Filtering only changes an `inbox_events` row's *status*: every
  detected message is still recorded in the commit that consumes it, and a
  false positive is released with one `filtered` → `pending` flip.
  **`skip_bulk` is on by default** (the one behaviour change on upgrade); both
  budget caps default to NULL. `org_email_budget_settings` adds a per-org daily
  *message* cap and monthly *spend* cap that pause dispatch, alert once per
  period and resume automatically, alongside — not replacing —
  `BESTTEAM_TRIGGER_DAILY_CAP`, the operator's deployment-wide runs/day rail.
  What does **not** exist: any inspection beyond headers (a human-written but
  irrelevant email is still billed) and reconciliation of `model_catalog`
  prices against a provider bill — the spend cap bounds an estimate. See
  `ui/backend/CLAUDE.md` and `docs/STATUS.md` for the rest, including the caps
  `retry_triggered_run` does not enforce.
- **Attachments are readable, as text only, and never from disk.**
  `email_read` lists a message's attachments and
  `email_read_attachment(message_id, filename)` extracts one on demand — two
  tools rather than one, so the model pays only for what it decides to read and
  each extraction is its own trace event. The tool takes **no path**: a message
  id confined to the run's batch plus a name matched against that message's own
  MIME parts, parsed from `io.BytesIO`. `parse_file`'s no-sandboxing contract is
  why — the filename is chosen by whoever sent the message, so traversal is not
  defended against but made structurally impossible. Three limits, all checked
  before parsing: 10 MB per attachment, 25 MB per message, 8,000 characters of
  extracted text; breaching one returns a sentence, never an exception.
  Readable types are exactly `parse_file`'s (`.pdf`, `.xlsx`, `.xlsm`, `.docx`,
  `.xml`, plain text). What does **not** exist: OCR or any image understanding
  (a photographed invoice is invisible), archive expansion (refused as a
  zip-bomb surface), any layout/formula/embedded-image fidelity, and fetching
  of individual MIME parts — `BODY.PEEK[]` still pulls the whole message, so a
  large attachment costs memory even when nothing reads it. The tool is in both
  `EMAIL_TOOL_NAMES` sets, so pairing it with an egress tool is refused at
  deploy validation. See `src/bestteam/tools/CLAUDE.md`.
- **Run history has retention, but erasure by data subject does not exist.**
  Each org sets a period (`org_retention_settings`, NULL = keep forever by
  default) and a purge clears *content* — `runs.input`/`output`, the run's
  `trace_events`, an `automation_item_results.payload` — while keeping
  *accounting*: the `runs` row, `usage_records`, and an item result's
  `status`/`source_key` (clearing those would make a sweep cause duplicate
  drafts on retry). Deleting everything about one email address is **not**
  offered and won't be: the address is only in free text the model may have
  paraphrased. A purge is also not a secure erase — no `VACUUM`. See
  `ui/backend/retention.py`.
- **Live run state (`RunRegistry`) isn't rehydrated from the DB on restart**:
  a killed/restarted process still loses in-flight/live run state. History no
  longer disappears, though — every `trace_events` row is now persisted per
  run alongside `usage_records`, with a read API (`GET /api/runs`,
  `GET /api/runs/{id}/trace`) and cooperative cancellation
  (`POST /api/runs/{id}/cancel`) — see `ui/backend/db/CLAUDE.md`.
- **General-purpose cache**: only local per-process caches exist — see
  `ui/backend/CLAUDE.md`.
- **CrewAI adapter, DEBATE collaboration mode, deployment templates**:
  planned, not started (HIERARCHICAL is implemented) — see
  `src/bestteam/CLAUDE.md`.

## Testing notes

- All current tests use `FakeListChatModel` / `fake:` specs — zero API cost,
  deterministic. Live-model examples (`examples/code_review_demo_live.py` and
  `ui/backend/workflows/vector_knowledge_base_demo_live.yaml`, both
  `ChatOpenAI`/OpenAI-embeddings-based) exist but require real API quota to
  run.
- Every test file needs a `pytestmark` (`unit`/`integration`/`e2e`/
  `optional`, optionally also `slow`) — `tests/test_marker_completeness.py`
  fails the suite if any collected test carries none of those markers, so a
  new test file can't silently fall outside every CI job's `-m` selection.
- Running the E2E suite requires the `test` extra
  (`pip install -e ".[ui,dev,tools,test]"`), `playwright install chromium`,
  and `npm` on PATH — `tests/e2e/conftest.py`'s fixture spawns real
  `uvicorn`/`vite` dev-server subprocesses. It needs ports 8000 and 5173
  free; it now fails loudly (naming the conflicting port) rather than
  silently attaching to a developer's own running dev stack if they're not.
- **Password hashing is deliberately cheap in-process.** `tests/conftest.py`
  lowers `ui.backend.auth._PBKDF2_ITERATIONS` to 1,000 for the test process:
  at the production 260,000 it was 543 of the suite's 789 seconds (69%).
  Production code is untouched — there is no env var or config key, so the
  real iteration count cannot be misconfigured in a deployment, and
  `test_auth.py::test_production_pbkdf2_iterations_are_unchanged` reads the
  literal out of `auth.py`'s source so a weakened default still fails.
  `tests/e2e/` drives a real uvicorn subprocess that never imports conftest,
  so that tier still exercises genuine 260k hashing.
- **`-n auto` for a fast local run** (`pytest-xdist`, in the `dev` extra):
  ~2m11s vs ~3m17s serial. Not in `addopts` on purpose — it breaks `-x`,
  `--pdb` and readable tracebacks, so plain `pytest` stays serial and
  debuggable. Never use it on `tests/e2e/` (fixed ports 8000/5173).
- CI is 6 jobs plus a `changes` job that path-filters them. Note what it
  filters against: on a **push** it compares with the previous commit, so a
  docs-only commit to `main` runs nothing; on a **pull request** it compares
  with the base branch, so the whole PR's diff decides — a docs-only commit
  on top of a PR that touches backend still runs the backend jobs, which is
  the point. The jobs: 4 PR-gate (`backend-unit-integration` — under
  `-n auto`, `backend-optional-deps`, `frontend`, `e2e-smoke`) on every
  PR/push; 2 gated to `main` (`backend-full`, `e2e-full`). Both guard on
  `github.ref == 'refs/heads/main'`, which is the *ref*, not the event — so
  `workflow_dispatch` from a feature branch does **not** enable them (verified
  empirically: dispatched on `feat/email-phase-0-hardening`, both still
  skipped). To get that coverage before merging, run the same command locally:
  `python -m pytest -m "not e2e"`, serial, no `-n auto`.
  `backend-full` runs **serially and in one process on purpose** —
  with the PR gate distributed and out of order, it is what still catches
  ordering and cross-test isolation bugs. The path filters are allowlists;
  adding a new top-level directory means adding it there too.
- `fake-architect:` is a deterministic drop-in model for E2E coverage of
  the Team Builder wizard's AI-generation steps, and is deliberately never
  present in `DEFAULT_MODEL_CATALOG`.
