# bestteam — `ui/backend/db/` (persistence layer)

Directory-scoped notes for the SQLAlchemy persistence layer. See the root
`CLAUDE.md` for project overview, architecture, and commands; see
`ui/backend/CLAUDE.md` for the API layer that uses this schema.

## Persistence layer

Per-deployment SQLite database via SQLAlchemy 2.0 (`pip install
'bestteam[ui]'`). `db/models.py` defines the full Phase 1 schema from
`docs/team_builder_methodology.md`:

- `agents` / `teams` / `knowledge_bases` / `skills` / `workflows` — each row's `config`
  is a JSON `raw` dict (the technical fields from `AgentSpec`/`TeamSpec`/
  `KnowledgeBaseSpec`/`SkillSpec`/`Specification.to_raw()`, see `core/specification.py`);
  `workflows.status` tracks `draft` / `ready_for_testing` / `deployed`.
- `builder_sessions` — the wizard's session state machine. `status` is one
  of `intent | requirements | spec | solution | testing | deployed`
  (`db/builder_sessions.py::STATUSES`); `requirements_json`/
  `specification_json` hold the Business Analyst / Solution Architect
  agents' structured outputs; `feedback_history` is an append-only JSON list
  recording each round of customer feedback.
- `runs` / `trace_events` — persisted replacement for `RunRegistry`'s
  in-memory state (wired up in Phase 5).
- `model_catalog` — maps a model `spec` string (e.g. `"openai:gpt-4o-mini"`,
  `"fake:ok"`) to a customer-friendly `display_name`, complexity `tier`
  (`fast`/`balanced`/`advanced`), and per-1K-token input/output pricing
  (Phase 3). Seeded with `DEFAULT_MODEL_CATALOG` (`db/model_catalog.py`) on
  first use of the production engine via `seed_default_catalog()`
  (idempotent — no-op if the table is non-empty).
- `usage_records` — per-agent token usage per run, plus a `cost_estimate`
  computed from `model_catalog` pricing where the model's spec matches an
  entry (Phase 3, `db/usage.py::record_usage`).
- `users` — simple per-deployment login (Phase 3, `db/users.py` +
  `ui/backend/auth.py`/`auth_api.py`).

`db/database.py` provides `make_engine(db_path)` (`":memory:"` uses a
`StaticPool` so all connections share one database — needed for tests/dry
runs), `init_db(engine)` (`Base.metadata.create_all`), and
`session_factory(engine)`. `db/builder_sessions.py` has the
`builder_sessions` CRUD (`create_session`/`get_session`/`update_session`/
`append_feedback`); CRUD for `agents`/`teams`/`knowledge_bases`/`workflows`
lives in `ui/backend/crud.py` (Phase 2, see `ui/backend/CLAUDE.md`).
`ui/backend/db_session.py` wires up the per-deployment engine (default
`ui/backend/data/bestteam.db`, override with `BESTTEAM_DB_PATH`) and a
`get_db()` FastAPI dependency.

## Known limitation: persistent run state

`ui/backend/registry.py`'s `RunRegistry` is still the authoritative in-process
live layer — runs vanish on restart and there's no run-history API. As of
CR-012, `ui/backend/runtime.py::run_in_background` does persist one `runs` row
per run (committed before any usage record and updated to its terminal
status/output), so `usage_records`/`trace_events` foreign keys reference a real
row rather than a phantom id. Still deferred to Phase 5: persisting
`trace_events`, rehydrating `RunRegistry` from the DB across restarts, a
history API, and enabling SQLite foreign-key enforcement.
