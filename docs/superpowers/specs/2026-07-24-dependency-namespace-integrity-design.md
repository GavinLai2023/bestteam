# Design: Dependency-namespace integrity — P1-07 (deletion safety) + P1-08 (tool-name collision detection)

Date: 2026-07-24
Source: `docs/DATA_ARCHITECTURE_REVIEW_REPORT.md`, findings P1-07 and P1-08.
Disposition tracked in `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.

## Context

Next small batch of data-architecture fixes, extending the merged "deploy is the
gate" work (P1-06/P1-11) by protecting the tool/skill/KB namespace that
**deployed** workflows depend on. Two self-contained, backend-only findings at
the deploy/CRUD surface:

- **P1-07 — resource deletion can orphan deployed workflows.**
  `crud._make_component_router.delete_item` (`ui/backend/crud.py:278-296`) deletes
  a Skill/KB unconditionally and only invalidates the workflow cache. Nothing
  checks whether a deployed workflow references it, so the delete orphans that
  workflow — it fails the next time `_get_workflow` builds it.
- **P1-08 — tool namespace collisions.** Every tool (built-in `REGISTRY`,
  standalone/inline KBs, per-org email) resolves through **one flat name→tool
  lookup** (`core/loader.py::_build_agent`, lines 145-152; the backend merges
  `{**REGISTRY, **kb_tools, **email_tools}`). A KB named after a built-in
  silently shadows it, and behaviour then depends on resolution order.

## Decisions (locked with the user)

- **P1-08 = collision detection**, not the reviewer's literal typed-namespace
  rename. A rename to `builtin:`/`kb:`/`connection:` would touch the loader, every
  spec/skill/KB reference, the model-facing tool names, and tool discovery — and
  break every existing YAML and deployed `WorkflowRecord`, needing a config
  migration. Detecting and rejecting a colliding KB name at deploy achieves the
  actual safety goal (no silent shadowing) with no rename and no back-compat break.
- **P1-10** (KB inline-vs-standalone ownership) is a separate aggregate-boundary
  refactor and gets its own next sub-project.

## Design

### P1-07 — refuse deleting a referenced skill/KB

- New helper in `crud.py`:
  `_deployed_workflows_referencing(db, org_id, kind, name) -> list[str]` returns
  the names of `status="deployed"` `WorkflowRecord`s in the org whose `config`
  references `name`:
  - `kind="skill"` → `name` appears in any `agents[*].skills`.
  - `kind="knowledge_base"` → `name` appears in any `agents[*].tools` (a standalone
    KB is referenced by name in an agent's `tools`, per `load_knowledge_base_tools`).
- In `delete_item` (the component router, `crud.py:278`), for
  `name in ("skills", "knowledge_bases")`: compute references **before** any
  deletion or KB directory `rmtree`; if non-empty raise `HTTPException(409)`
  naming the teams ("Can't delete '<x>': it's used by deployed team(s): <names>.
  Update or remove those teams first."). Unreferenced → the existing 204 path.
- Scope: deployed workflows only (the production surface, consistent with P1-06).

### P1-08 — reject KB names that shadow built-in tools at deploy

- New helper in `ui/backend/deploy_validation.py` (beside `validate_agent_models`):
  `find_kb_tool_collisions(raw_spec, kb_tool_names, builtin_names) -> list[str]`
  returns the sorted KB names — inline (`raw["knowledge_bases"][*].name`) ∪ built
  standalone (`kb_tool_names`) — that are also `builtin_names`.
- Call at both deploy points after building `kb_tools`
  (`builder.py::deploy_session`, `crud.py::upsert_workflow_config`); reject 400
  ("A knowledge base can't reuse a built-in tool name: <names>. Rename the
  knowledge base."). `builtin_names = set(bestteam.tools.REGISTRY)`.
- Only **KB** names are checked, so the intentional per-org email-tool override
  (email tools override `REGISTRY`'s `email_*` by name, by design) never
  false-positives.

## Components and boundaries

- `_deployed_workflows_referencing` and `find_kb_tool_collisions` are small,
  pure-ish readers over `WorkflowRecord.config` / a raw spec dict; the routers own
  the HTTP translation (409 / 400). No SDK/loader change — collision detection
  lives at the backend deploy points where the multi-source namespace is assembled
  and the email-override intent is known.

## Error handling

- Referenced skill/KB delete → `409 Conflict` naming the deployed teams
  (actionable; no deletion or `rmtree` happens first).
- KB name shadowing a built-in → `400` at deploy/upsert naming the offending KB.

## Testing

- **Unit** (`find_kb_tool_collisions`): flags an inline KB name and a standalone
  `kb_tool_names` entry equal to a built-in; empty when none collide.
- **API P1-07**: deploy a workflow using skill `S` (an agent's `skills`) and a KB
  `K` (an agent's `tools`); `DELETE` the skill/KB → 409 naming the team and the
  resource still exists; delete an unreferenced skill/KB → 204.
- **API P1-08**: deploy (`deploy_session`) and upsert (`/api/config/workflows`) a
  workflow whose KB is named `calculator` (a built-in) → 400; a normally-named KB
  → success.
- **Full suite** green (`BESTTEAM_DB_PATH` scratch DB); frontend unaffected.

## Out of scope

- P1-10 (KB ownership consolidation) — its own next sub-project.
- The full typed-namespace rename (`builtin:`/`kb:`/`connection:`).
- Catalog-entry deletion revocation — model validation is deploy-time-only, a
  documented known limitation.
