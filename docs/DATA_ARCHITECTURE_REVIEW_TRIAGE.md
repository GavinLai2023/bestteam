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
| P1-07 | Resource deletion can orphan deployed workflows: `crud._make_component_router.delete_item` deleted a skill/KB unconditionally (only invalidating the workflow cache), so a deployed workflow still referencing it failed at its next build | Confirmed: `delete_item` (`ui/backend/crud.py`) had no reference check before deleting the record (and, for KBs, `rmtree`-ing its cache directory) | New `crud._deployed_workflows_referencing(db, org_id, kind, name) -> list[str]` returns the names of `status="deployed"` `WorkflowRecord`s whose `config` references `name` (`kind="skill"` → any agent's `skills`; `kind="knowledge_base"` → any agent's `tools`, a standalone KB's reference point); for platform skills (`org_id is None`) it scans across all orgs. `delete_item` calls it for `name in ("skills", "knowledge_bases")` **before** any deletion/`rmtree` and raises `HTTPException(409)` naming the referencing team(s); an unreferenced item still 204s. **Superseded by P1-04:** `_deployed_workflows_referencing` was removed; the guard now queries typed `workflow_dependencies` rows via `db/dependencies.py::workflows_referencing(db, kind=, resource_id=)` (see the P1-04 entry below) | `tests/test_crud_api.py::test_delete_skill_referenced_by_deployed_workflow_is_409`, `::test_delete_kb_referenced_by_deployed_workflow_is_409`, `::test_delete_unreferenced_skill_still_204`; full suite green |
| P1-08 | Tool namespace collisions: every tool (built-in `REGISTRY`, standalone/inline KBs, per-org email) resolves through one flat name→tool lookup (`core/loader.py::_build_agent`), so a KB named after a built-in silently shadows it, with behaviour then depending on resolution order | Confirmed: `core/loader.py::_build_agent` merges `{**REGISTRY, **kb_tools, **email_tools}` with no collision check | Scoped to **collision detection**, not the reviewer's literal typed-namespace rename (`builtin:`/`kb:`/`connection:` would touch the loader, every spec/skill/KB reference, and every deployed `WorkflowRecord`, needing a config migration — P1-10, KB inline-vs-standalone ownership, is split to its own sub-project instead). New pure `deploy_validation.find_kb_tool_collisions(raw_spec, standalone_kb_names, builtin_names) -> list[str]` returns the sorted, de-duped KB names (inline `knowledge_bases[*].name` ∪ referenced standalone) that collide with `builtin_names`. `knowledge_bases.kb_name_collisions(db, org_id, raw_spec)` resolves the referenced standalone KB names from the DB and calls it with `set(bestteam.tools.REGISTRY)`; called name-only (no KB build) at both deploy points (`builder.py::deploy_session`, `crud.py::upsert_workflow_config`), 400ing on a collision. Only KB names are checked, so the intentional per-org email-tool override (`email_*`) never false-positives | `tests/test_deploy_validation.py::test_inline_kb_name_shadowing_builtin_flagged`, `::test_standalone_kb_name_shadowing_builtin_flagged`, `::test_non_colliding_kb_names_pass`, `::test_collisions_sorted_and_deduped`; `tests/test_crud_api.py::test_workflow_put_rejects_kb_named_after_builtin`; `tests/test_org_settings.py::test_deploy_rejects_kb_named_after_builtin`; full suite green |

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

P1-07 and P1-08 were implemented together in a third pass ("dependency-namespace
integrity") once the user confirmed both were narrow, low-risk fixes: a
reference-count guard before delete (P1-07) and a name-collision check at the
two deploy points (P1-08, deliberately scoped to collision detection rather
than the full typed-namespace rename, which is a config-migration-sized
change; P1-10 split out as its own future sub-project). Design:
`docs/superpowers/specs/2026-07-24-dependency-namespace-integrity-design.md`.

The branch then went through **four review rounds** that progressively hardened
the deletion-safety and collision-prevention surface to the class level:
built-in names blocked at KB creation *and* fail-closed at load for both
standalone and inline KBs; the delete reference-scan uses the loader's own
`list(refs)` normalization (list/dict/string) and skips malformed rows without
500-ing; seeded platform built-in skills are undeletable; the KB delete/upload
paths serialize on the per-KB lock across their whole critical sections; and a
process-wide `component_mutation_lock` closes the delete/deploy TOCTOU (feasible
even single-worker via FastAPI's threadpool — the initial "near-impossible"
rationale was wrong). Deferred to the P1-04 typed-dependency-records sub-project
(the durable fix): raw-name matching that can over-block (fail-closed, safe),
cross-process locking, and durable upload-file cleanup. See the design spec's
per-round sections. Delivered via PR #27.

P1-01, P1-02 and P1-03 were implemented together in a fourth pass ("versioning
keystone") once the user chose them as the next batch: a deployed team had no
stable identity and no version history. `WorkflowRecord` is now the stable team
head with an immutable `workflow_versions` child table; deploy calls
`db/workflows.py::publish_workflow_version` at both deploy points
(`builder.py::deploy_session`, `crud.py::upsert_workflow_config`) to append a
new version and move `current_version_id` (keeping `config` as the current
mirror) instead of overwriting in place (P1-03). `deploy_session` links
`BuilderSession.workflow_id` to the head in the same commit, so a redeploy
versions the same head and two same-named sessions converge on one team, first
config preserved as v1 (P1-01/P1-02). Each production `Run` is stamped with
`workflow_version_id` (P1-03/P1-15-adjacent); sandbox test-runs stay NULL.
Migration `c3f5a1b8e2d4` (guarded) backfills one v1 per existing workflow.
Deliberately scoped to freezing the **inline config blob + run linkage** — a
version does NOT freeze standalone Skill/KB/model resolution (still by name at
load), so P1-05 behavioral drift and a fully-resolved dependency snapshot stay
with P1-04 (typed dependency records); version-history/rollback UI and rollback
execution are also deferred. Design:
`docs/superpowers/specs/2026-07-25-versioning-keystone-design.md`.

Data architecture review triage, fifth pass — typed dependency records
(P1-04): the versioning keystone froze the inline config blob, but standalone
Skill/KB references inside it were still resolved by name at load, and the
skill/KB delete guard (P1-07) worked by scanning deployed workflows' JSON for
those names rather than a stable identity. A new `workflow_dependencies`
table (`WorkflowDependency`) now records one typed row per (published
workflow version, skill|standalone-KB) it depends on — `resource_kind`,
`resource_name`, and a resolved `resource_id` pointing at the actual
`skills`/`knowledge_bases` row, unique per `(workflow_version_id,
resource_kind, resource_name)`. Rows are populated once at deploy in
`db/workflows.py::publish_workflow_version` via
`db/dependencies.py::record_version_dependencies`, resolving names exactly as
the loader does (org skill shadows a same-named platform built-in; KBs are
org-scoped; a built-in tool / email tool / inline KB is not a KB dependency,
so no row is written for it). The skill/KB `DELETE` guard is rewired to
`workflows_referencing(db, kind=, resource_id=item.id)`, querying these typed
rows for each workflow's **current** version instead of scanning JSON —
behaviorally non-regressing, and the stable id makes the
platform-built-in-skill cross-org case fall out with no all-orgs scan.
Migration `d4e6b2c9f1a7` creates the table and backfills each workflow's
current version. Scope is skills and standalone KBs only; deferred: model and
built-in-tool dependency rows (schema's `resource_kind` supports them, no
consumer yet) and skill/KB **content** pinning to freeze behavior, which
stays with P1-05.

## Everything else: validated-accurate, out of scope for this pass

The remaining 20 findings (all of Phase 2 except P2-01/P2-02 above, and the
rest of Phase 1: P1-05, P1-10, P1-12, P1-15,
P1-18) were spot-checked where practical and found to be accurate
descriptions of the current, intentionally-scoped MVP data model — not
implemented here, since each requires new schema/entities and cross-cutting
execution-path changes well beyond a "narrow, low-risk fix." Each should go
through this project's normal brainstorm → spec → plan cycle as its own
sub-project before any code is written, the same pattern used for "Org
multi-tenancy — sub-project 1" earlier in this project, rather than being
absorbed wholesale into one pass.

**P2-12 re-reviewed (2026-08-12):** re-confirmed accurate — still only
`KnowledgeBaseRecord` persisted, no `KnowledgeDocument`/`KnowledgeChunk`/
`IngestionJob`/`KnowledgeIndex` entities, no document ACL/retention status,
no per-org storage quota (only a self-service KB **count** cap). Still
deferred: the project is at MVP/pre-release stage with no real multi-org
customers uploading documents at volume, so there is no active
correctness/security/cost incident forcing this now. Revisit as its own
brainstorm → spec → plan sub-project once real multi-org document-upload
volume or a compliance/retention requirement makes it concrete.
