# Data Architecture Review — Triage Register

Date: 2026-07-24
Scope: `docs/DATA_ARCHITECTURE_REVIEW_REPORT.md` (external architecture audit, 18
Phase 1 + 17 Phase 2 findings) — validation and disposition.

## How this review was scoped

The report itself frames Phase 1 and Phase 2 as sequential, multi-week program
work (its own "Delivery priority" section), not a single review cycle: its
recommended target model introduces ~25 new entity types (immutable AI Team
versioning, a generic tool-connection framework, KB document/ingestion
versioning, `OrganizationMembership`/RBAC, a PostgreSQL migration, audit/
billing subsystems). Implementing all of it in one pass would contradict this
project's simplicity/surgical-change principles and — for several findings —
would silently reverse decisions already made deliberately elsewhere in the
project, with their own documented rationale.

Per the user's direction, this pass scoped to: validate every finding's
technical accuracy, then implement only the narrow, low-risk items that are
genuine defects (not already-decided tradeoffs), and record disposition for
everything else so a future initiative can pick it up deliberately rather than
rediscovering it.

## Findings that conflict with a prior explicit decision (not implemented)

These were spot-verified as *accurate descriptions of current state*, but each
restates a tradeoff this project already made on purpose, with its own
rationale on record:

| ID | Finding | Prior decision on record |
|---|---|---|
| P1-13 | SQLite foreign keys not enabled | `ui/backend/db/CLAUDE.md` ("Known limitation: persistent run state") lists this as "Still deferred to Phase 5," bundled with trace persistence and registry rehydration — because enabling it forces a CASCADE/RESTRICT/SET NULL decision across every relationship the report itself flags as orphanable. Also on record in `docs/CODE_REVIEW_TRIAGE.md` CR-012 (2026-07-14): "SQLite FK enforcement intentionally not toggled." |
| P1-16 | Trace persistence unfinished | Same Phase 5 bundle; reaffirmed this session (2026-07-22/23) when an independent reviewer and this session's implementer agreed full DB-backed trace persistence was disproportionate for current scale, scoping `RunRegistry` to bounded in-memory eviction instead (`docs/superpowers/specs/2026-07-22-run-registry-bounded-eviction-design.md`). |
| P1-17 | Crash recovery / registry lifecycle incomplete | Same bundle; `ui/backend/db/models.py`'s `Run` docstring states directly: "full restart-survival is deferred to Phase 5." |
| P2-01, P2-02 | One member per org / no Membership-RBAC model | Deliberate, not an oversight: one-member-per-org has its own design spec (`docs/superpowers/specs/2026-07-18-wizard-email-connect-design.md`) and is called out explicitly in `ui/backend/CLAUDE.md`/`docs/STATUS.md`: "Deliberately built without a per-org admin role: one member per org is enforced instead." |

Reopening any of these is a real option — but it's a decision to make explicitly,
not a silent side effect of "implement all findings."

## Implemented this pass

| ID | Finding | Validation | Fix | Verification |
|---|---|---|---|---|
| P1-14 | Deployment is not a single atomic transaction — `deploy_session` committed the `WorkflowRecord` write, then called `update_session` (its own separate commit); a failure between them could leave a "deployed" Workflow with its BuilderSession still un-deployed | Confirmed: `ui/backend/builder.py` had `db.commit()` immediately after the `WorkflowRecord` write, then a second, independent `db.commit()` inside `db/builder_sessions.py::update_session` | Removed the intermediate `db.commit()`; both writes now share `update_session`'s single commit (`get_db`'s `finally: db.close()` rolls back the whole pending transaction if `update_session` raises first) | New test `test_deploy_is_atomic_across_workflow_and_session_updates` (`tests/test_builder_api.py`) — RED under the old code (WorkflowRecord persisted despite the overall call raising), GREEN after the fix. Full `test_builder_api.py` (27 passed) and full suite (593 passed) |
| P1-09 | `AgentRecord`/`TeamRecord` are vestigial: no runtime loader reads them, and their `/api/config` routes were removed in `036e1d6` | Confirmed no *current* reader/writer. Note: writable CRUD routes DID exist historically (`78c7a8a`..`036e1d6`), so we cannot assume every deployment's tables are empty — both are empty only in the deployments we operate | Removed both model classes; Alembic migration `57b13700d5df` drops the tables. **Guarded and non-destructive**: it refuses (raising with export guidance) if either table holds rows, and only drops when empty. A drop is *not* data-reversible — downgrade recreates the tables empty | `tests/test_migrations.py` — `create_all → upgrade head` idempotency + drop-refuses-when-populated / drops-when-empty (RED before the guards, GREEN after); `tests/test_db.py` / `tests/test_crud_api.py` rebased off the dead classes; full suite green |
| P1-06 | Workflow lifecycle status is decorative: `WorkflowRecord.status` was an unrestricted string, `_get_workflow()` didn't require `deployed`, and `/api/workflows` listed every record regardless of status — a draft could be run as production | Confirmed: `_get_workflow`/`list_workflows` (`ui/backend/main.py`) ignored `status`; `crud.upsert_workflow_config` created rows as `status="draft"` and never promoted them | `_get_workflow` and `GET /api/workflows` now filter to `status == "deployed"` (a non-deployed record is treated as unknown — same 404 as absent, no existence oracle); `crud.upsert_workflow_config` now writes `status="deployed"` on both insert and update, i.e. **save is deploy** (matching the wizard's `deploy_session`, which already wrote `deployed`) — `/api/config/workflows` (the admin CRUD list) stays unfiltered by design. Migration `b1d7e4f2a9c8` adds a CHECK bounding `status` to `('draft','ready_for_testing','deployed')` (guarded/idempotent, `op.batch_alter_table` for SQLite) and backfills every pre-existing non-`deployed` row to `deployed` so upgrading a deployment doesn't retroactively hide previously-runnable workflows | `tests/test_crud_api.py::test_only_deployed_workflows_are_listed_and_runnable`; `tests/test_builder_api.py::test_deployed_workflow_can_be_run_via_get_workflow`; `tests/test_migrations.py::test_existing_non_deployed_workflow_backfilled_to_deployed` / `test_status_check_rejects_invalid_value`; full suite green |
| P1-11 | Deployment validation is incomplete: `_build_workflow()` resolves tools/skills/KB references but not that each agent's model is one the platform actually offers, so a bad model spec passed deploy and only failed at first run | Confirmed: neither `builder.py::deploy_session` nor `crud.py::upsert_workflow_config` checked `AgentSpec.model` beyond structural validation | New `ui/backend/deploy_validation.py::validate_agent_models(raw_spec, catalog_specs)` — a pure function (no DB/FastAPI imports) returning a problem string for **each** agent whose model isn't a non-empty string that is either a `fake:` spec (exempt) or a catalog member; i.e. it rejects missing / `None` / empty / non-string / unavailable models. (F1 review hardening: the CRUD path builds `Agent(**spec)` directly and `Agent.model` is `ModelSpec \| None`, so this is the only guard against a falsy/non-string model, which previously deployed and failed at first run — or, for a non-string like `42`, crashed the check.) Called at both deploy points (`deploy_session`, `crud.upsert_workflow_config`) right after the existing spec validation, raising `HTTPException(400)` listing every problem together (not first-fail) | `tests/test_deploy_validation.py` (unit: unknown catalog model flagged, catalog model passes, `fake:` exempt with an empty catalog, multiple problems aggregated, missing/`None`/empty/non-string models flagged, non-dict agents left to structural validation); `tests/test_crud_api.py::test_workflow_put_rejects_agent_model_not_in_catalog` and `::test_workflow_put_rejects_agent_with_missing_none_empty_or_nonstring_model`; `tests/test_builder_api.py::test_deploy_rejects_agent_model_not_in_catalog` and `::test_deploy_rejects_agent_with_empty_model`; full suite green (613) |

This reverses the "kept (empty) rather than migrated away" note that used to be
in `ui/backend/db/CLAUDE.md` for these two tables — unlike the four findings
above, that note documented a fact ("we didn't write the migration"), not a
reasoned tradeoff, so removing this dead schema was judged in scope for a
"small, low-risk fixes" pass — with the drop guarded to refuse rather than
destroy data in the unlikely event a deployment populated the tables via the
historical CRUD routes.

P1-06 and P1-11 were implemented together in a second pass ("deploy is the
gate") once the user confirmed both were in scope as narrow, low-risk fixes
of the same shape as P1-09/P1-14: a bounded status enum + backfill (not a new
entity/workflow-versioning model) and a deterministic catalog-membership
check (not a provider/API-key-dependent constructibility check). Design:
`docs/superpowers/specs/2026-07-24-deploy-is-the-gate-design.md`.

## Everything else: validated-accurate, out of scope for this pass

The remaining 26 findings (all of Phase 2 except P2-01/P2-02 above, and the
rest of Phase 1: P1-01 through P1-05, P1-07, P1-08, P1-10, P1-12, P1-15,
P1-18) were spot-checked where practical and found to be accurate
descriptions of the current, intentionally-scoped MVP data model — not
implemented here, since each requires new schema/entities and cross-cutting
execution-path changes well beyond a "narrow, low-risk fix." Each should go
through this project's normal brainstorm → spec → plan cycle as its own
sub-project before any code is written, the same pattern used for "Org
multi-tenancy — sub-project 1" earlier in this project, rather than being
absorbed wholesale into one pass.
