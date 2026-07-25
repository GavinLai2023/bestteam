# Typed dependency records (P1-04) — design

**Date:** 2026-07-25
**Finding:** P1-04 (Dependencies are soft name references), Data Architecture Review
(`docs/DATA_ARCHITECTURE_REVIEW_REPORT.md`).
**Batch:** the keystone the P1-06/07/08 and versioning-keystone batches deferred to.

## Problem

A deployed workflow's dependencies exist only as raw name-strings buried in JSON
(`agents[*].skills`, `agents[*].tools`). The database can't enforce these relationships or
answer "what depends on this resource?". Four separate config-walkers each re-parse the same
JSON with *inconsistent* normalization:

- `crud._deployed_workflows_referencing` — the skill/KB delete guard (reverse scan).
- `knowledge_bases.load_knowledge_base_tools` — forward resolver.
- `knowledge_bases.kb_name_collisions` / `deploy_validation.find_kb_tool_collisions` — deploy-time collision check.
- `email_tools.spec_uses_email` — email-usage detection.

The delete guard does a raw `name in refs` membership scan of *every* deployed workflow's
live `config` on every skill/KB delete — the "raw-name matching that can over-block" the
triage (`docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`) and `ui/backend/CLAUDE.md` explicitly
flag as needing P1-04's durable fix.

## Goal

Materialize each **skill and standalone-KB** dependency of a published workflow version into
a typed DB row carrying a stable `resource_id`, populated atomically at deploy inside
`publish_workflow_version`. Rewire the skill/KB delete guard to a precise `resource_id`
query against those rows instead of scanning JSON.

## Scope (locked with the user)

- **Kinds recorded:** skills + standalone KBs only — the two deletable standalone resources
  the delete guard protects. Built-in tools (undeletable) and model specs (catalog entries)
  are **not** recorded. The table schema stays general (a `resource_kind` column) so those
  kinds can be added later with no migration, but there are no rows/tests for them now
  (YAGNI — no consumer).
- **Delete-block scope:** current deployed config only — behaviorally identical to today's
  guard, just sourced from typed rows. Non-regressing, least surprising.
- **Backend / data-model only.** No frontend changes. No SQLite FK-enforcement toggle
  (P1-13 stays deferred; the new FK is advisory like every other FK in the schema).

## Design

### New table `workflow_dependencies`

`ui/backend/db/models.py` — `WorkflowDependency`:

- `id` PK.
- `workflow_version_id` FK→`workflow_versions.id`, `nullable=False`.
- `resource_kind: str` — `"skill"` | `"knowledge_base"`.
- `resource_name: str`.
- `resource_id: Optional[int]` — the id of the resolved `SkillRecord`/`KnowledgeBaseRecord`.
  Nullable: a backfilled row for a name that no longer resolves stays NULL — harmless, it
  simply can't block a delete, and such a reference can't build a workflow anyway.
- `UniqueConstraint("workflow_version_id", "resource_kind", "resource_name",
  name="uq_workflow_dependencies_version_kind_name")`.

Rows are written once at publish (a version is immutable) — never updated in place. Keyed to
the *version*, so the table is honest about each immutable snapshot's dependencies and is
forward-compatible with per-version querying; the delete guard this batch ships only reads
the *current* version's rows.

### Populate at deploy — `ui/backend/db/dependencies.py` (new)

`record_version_dependencies(db, *, version_id, org_id, raw) -> None`:

- Walk `raw["agents"][*]` defensively (skip non-dict agents; `list(agent.get(field) or [])`
  in a `try/except TypeError`, mirroring the existing scanner), collecting `skills` names and
  `tools` names.
- **skills:** resolve against `SkillRecord` where `name in skill_names AND (org_id == :org
  OR org_id IS NULL)`, with an **org row shadowing a same-named platform built-in** (mirrors
  `skills.load_skills` precedence). Record a `skill` row for every referenced skill name
  (`resource_id` set when resolved).
- **KBs:** resolve against `KnowledgeBaseRecord` where `name in tool_names AND org_id ==
  :org` (KBs are org-scoped, no platform tier — mirrors
  `knowledge_bases.load_knowledge_base_tools`). Record a `knowledge_base` row **only for tool
  names that resolve to a standalone KB record** — a built-in tool, an email tool, or an
  inline KB (which has no standalone row) is not a standalone-KB dependency and is skipped.

Called from `db/workflows.py::publish_workflow_version`, immediately after
`db.add(version); db.flush()` (so `version.id` exists), with `version_id=version.id,
org_id=org_id, raw=config`. Both deploy points — `crud.upsert_workflow_config` and
`builder.deploy_session` — already funnel through `publish_workflow_version` under
`component_mutation_lock` and own the single commit, so no other deploy-site change is
needed.

### Rewire the delete guard — the payoff

`ui/backend/db/dependencies.py`:

```python
def workflows_referencing(db, *, kind, resource_id) -> list[str]:
    q = (db.query(WorkflowRecord.name)
         .join(WorkflowVersion, WorkflowVersion.id == WorkflowRecord.current_version_id)
         .join(WorkflowDependency, WorkflowDependency.workflow_version_id == WorkflowVersion.id)
         .filter(WorkflowRecord.status == "deployed",
                 WorkflowDependency.resource_kind == kind,
                 WorkflowDependency.resource_id == resource_id))
    return sorted({name for (name,) in q})
```

Matching on `resource_id` (not name) makes the platform-skill cross-org case fall out
naturally: an org workflow that references a platform built-in stores that built-in's *global*
id, so deleting the built-in (org omitted, `item.id`) finds every org's references with no
all-orgs name scan. The `current_version_id` join reproduces "current deployed config only".

`crud.delete_item` replaces `_deployed_workflows_referencing(db, org_id, kind, item_name)`
with `workflows_referencing(db, kind=kind, resource_id=item.id)` (same 409 message, still
inside `component_mutation_lock`), and the now-orphaned `_deployed_workflows_referencing` is
removed (our change is its only caller).

### Clean up dep rows on workflow-head hard-delete

`crud.delete_workflow_config` already deletes a never-run head's `workflow_versions` rows
(FK enforcement is off → no DB cascade). Add, before deleting the versions and inside the
existing `component_mutation_lock` block, a delete of the `WorkflowDependency` rows for those
versions so no orphan dep rows survive:

```python
version_ids = [v for (v,) in db.query(WorkflowVersion.id).filter_by(workflow_id=item.id)]
db.query(WorkflowDependency).filter(
    WorkflowDependency.workflow_version_id.in_(version_ids)).delete(synchronize_session=False)
```

### Migration (new head, `down_revision = "c3f5a1b8e2d4"`)

Follows the established guarded/idempotent pattern (`create_all` runs before `upgrade`, so
every step is inspect-guarded; `op.batch_alter_table` for SQLite; local `_has_column`
helper). `alembic/versions/<new>_workflow_dependencies.py`:

1. Guarded `create_table("workflow_dependencies", ...)` (skip if present), same columns +
   named unique constraint as the ORM.
2. **Backfill (Python, idempotent):** for each `workflows` row with `current_version_id IS
   NOT NULL` whose current version has no dep rows yet, `json.loads` its `config`, extract
   skill/tool names, resolve ids via small raw-SQL resolvers (skill: `org_id == :o` then
   fall back to `org_id IS NULL`; KB: `org_id` match, emit a row only when a standalone KB
   resolves), and insert dep rows for the current version. Skip any version id already
   present in `workflow_dependencies` (idempotent). Resolving the id (not just the name) is
   what keeps the rewired guard non-regressing for pre-migration deployed workflows.
3. Guarded `downgrade()` drops the table.

## Files

- `ui/backend/db/models.py` — `WorkflowDependency`.
- `ui/backend/db/dependencies.py` (new) — `record_version_dependencies`, `workflows_referencing`.
- `ui/backend/db/workflows.py` — call `record_version_dependencies` in `publish_workflow_version`.
- `ui/backend/crud.py` — `delete_item` uses `workflows_referencing`; remove
  `_deployed_workflows_referencing`; `delete_workflow_config` cascades dep rows.
- `alembic/versions/<new>_workflow_dependencies.py` — table + Python backfill.
- Tests: `tests/test_db.py`, `tests/test_migrations.py`, `tests/test_dependencies.py` (new),
  `tests/test_crud_api.py`.
- Docs: `ui/backend/db/CLAUDE.md`, `ui/backend/CLAUDE.md`,
  `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`, `docs/STATUS.md`.

## Verification

- **Unit** (`test_dependencies.py`): a deploy records one `skill` row per referenced skill
  (org skill and platform built-in both resolve; org shadows built-in) and one
  `knowledge_base` row per referenced standalone KB; an inline KB, a built-in tool, and an
  email tool produce **no** row; `resource_id` populated.
- **Guard** (`test_crud_api.py`): deleting a skill/KB referenced by a deployed workflow →
  409 naming the team (existing P1-07 tests stay green); deleting a platform built-in skill
  referenced by an org's workflow → 409 (cross-org via id); redeploying a workflow that
  drops a skill, then deleting that skill → 204 (current-config semantics); unreferenced →
  204.
- **Head-delete cascade**: hard-deleting a never-run workflow removes its dep rows; a
  run-referenced head still 409s (unchanged).
- **Migration** (`test_migrations.py`): `create_all → upgrade head` idempotent; backfill
  creates dep rows with resolved `resource_id` for an existing deployed workflow's current
  version; re-upgrade adds nothing.
- Full suite green; frontend unaffected.

## Out of scope (deferred, documented)

- Recording model / built-in-tool dependencies (schema supports it; no consumer yet).
- Any-published-version delete strictness (chosen: current-config only).
- Pinning skill/KB **content/versions** to freeze behavior — P1-05 (needs Skill/KB
  versioning, P2-11); standalone-component drift stays a documented P1-05 limitation.
- Unifying the forward config-walkers — they do live tool-building, not integrity; left as-is.
- Soft-delete/archive lifecycle and the in-flight-run delete race (deletion-lifecycle
  sub-project); SQLite FK enforcement (P1-13).
