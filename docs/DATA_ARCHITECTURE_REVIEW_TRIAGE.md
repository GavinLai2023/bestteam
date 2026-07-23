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
| P1-09 | `AgentRecord`/`TeamRecord` are vestigial: tables exist, but no CRUD route and no runtime loader ever reads them | Confirmed via grep: referenced only in `models.py`/`db/__init__.py`, no writers anywhere (no route ever created a row) | Removed both model classes; Alembic migration `57b13700d5df` drops the tables (guarded, and reversible — downgrade recreates them matching the final pre-drop schema) | `tests/test_db.py` and `tests/test_crud_api.py` updated (dead-class references removed/rebased onto `SkillRecord`/`WorkflowRecord`); full suite (593 passed). Migration verified upgrade→downgrade→upgrade round-trip on a scratch DB |

This reverses the "kept (empty) rather than migrated away" note that used to be
in `ui/backend/db/CLAUDE.md` for these two tables — unlike the four findings
above, that note documented a fact ("we didn't write the migration"), not a
reasoned tradeoff, so removing genuinely dead, zero-data schema was judged
in scope for a "small, low-risk fixes" pass.

## Everything else: validated-accurate, out of scope for this pass

The remaining 29 findings (all of Phase 2 except P2-01/P2-02 above, and the
rest of Phase 1: P1-01 through P1-08, P1-10 through P1-12, P1-15, P1-18) were
spot-checked where practical and found to be accurate descriptions of the
current, intentionally-scoped MVP data model — not implemented here, since
each requires new schema/entities and cross-cutting execution-path changes
well beyond a "narrow, low-risk fix." Each should go through this project's
normal brainstorm → spec → plan cycle as its own sub-project before any code
is written, the same pattern used for "Org multi-tenancy — sub-project 1"
earlier in this project, rather than being absorbed wholesale into one pass.
