# Design: Deploy is the gate — P1-06 (lifecycle enforcement) + P1-11 (model validation)

Date: 2026-07-24
Source: `docs/DATA_ARCHITECTURE_REVIEW_REPORT.md` (external architecture audit),
findings P1-06 and P1-11. Disposition tracked in
`docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.

## Context

The next batch of small, self-contained fixes from the data-architecture review.
Two findings share one theme: **a deployed AI Team should be the only thing that
runs as production, and deploy should fully validate the team so it can't fail at
first run.**

Verified current gaps:

- **P1-06 — lifecycle status is decorative.** `WorkflowRecord.status` is an
  unconstrained string. `_get_workflow` (`ui/backend/main.py:360`) and
  `list_workflows` (`ui/backend/main.py:457`) ignore it, while
  `crud.upsert_workflow_config` (`ui/backend/crud.py:491`) creates rows as
  `status="draft"` and never promotes. So an operator-created draft is listed and
  runnable as production. (Preview — the wizard's Stage-5 test-runs — already runs
  the spec in-memory *without* a `WorkflowRecord`, so preview and production are
  already separate paths; the only leak is the draft-runnable one.)
- **P1-11 — deployment validation is incomplete.** `deploy_session`
  (`ui/backend/builder.py:493`) already validates tools, skills, and KB references
  via `validate_specification`. The remaining gap is agent **model** specs: a
  bad/unavailable model passes deploy and only fails at first run.

## Decisions (locked with the user)

- **P1-11** validates each agent model by **model-catalog membership** —
  deterministic, needs no API keys or provider calls; `fake:` specs are exempt.
  (Not a constructibility/instantiation check, which would need keys and still
  couldn't verify a model name exists remotely.)
- **P1-06** makes an operator **save = deploy**: `crud.upsert_workflow_config`
  writes `status="deployed"` (it already fully validates), so there is no separate
  promote endpoint or Advanced-page button. Existing rows are backfilled to
  `deployed` so upgrade preserves current runnability.

## Design

### P1-06 — only `deployed` runs; save = deploy

- **Migration** (guarded/idempotent, matching the project's convention): a CHECK
  constraint bounding `workflows.status` to `('draft','ready_for_testing',
  'deployed')`, plus a backfill of any existing non-`deployed` row → `deployed`.
  SQLite can't add a CHECK to an existing table in place, so this uses
  `op.batch_alter_table` (move-and-copy), guarded by inspection like the other
  migrations (`create_all` runs at import, so the table already exists).
- **`_get_workflow`** (`main.py`): add `status == "deployed"` to the
  `WorkflowRecord` lookup. A non-deployed record is then treated as unknown — the
  same 404 as an absent one, preserving the no-existence-oracle property.
- **`list_workflows`** (`main.py`): filter the DB names to `status == "deployed"`.
- **`crud.upsert_workflow_config`** (`crud.py`): set `status="deployed"` on both
  insert and update.
- Wizard `deploy_session` already writes `deployed` — unchanged.

### P1-11 — deploy-time model validation (catalog membership)

- New backend helper module `ui/backend/deploy_validation.py`:
  `validate_agent_models(raw_spec, catalog_specs) -> list[str]` returns the
  unknown model specs. It collects from `raw["agents"][*]["model"]` (every
  `AgentSpec` has a required `model`, `src/bestteam/core/specification.py:87`) and
  exempts any spec starting with `fake:`.
- Catalog specs come from `db.query(ModelCatalogEntry.spec)` (the global catalog
  for now — this scopes naturally when per-org model policies land, P2-06).
- Called at **both** deploy points (`deploy_session`, `crud.upsert_workflow_config`)
  right after the existing spec validation; on any unknown model it raises
  `HTTPException(400)` listing them all together (not first-fail).
- Scope: agent chat models only. KB `embedding_model` is out of scope — it isn't a
  chat-model-catalog entry, and KB existence/readiness is already validated.

## Components and boundaries

- `deploy_validation.validate_agent_models` is a pure function over a raw spec dict
  and a set of catalog spec strings — no DB or FastAPI imports, independently unit
  testable. The two routers own the DB query and the HTTP translation.
- The status filter lives entirely in `_get_workflow` / `list_workflows`; nothing
  else reads `WorkflowRecord.status` for resolution, so the change is contained.

## Error handling

- Unknown-model deploy → `400` with a message naming the rejected specs and
  pointing at the model catalog (actionable for the operator/wizard).
- A non-deployed workflow requested for a run → existing `404 Unknown workflow`
  path (no new error surface, no existence oracle across orgs).

## Testing

- **Unit** (`validate_agent_models`): flags unknown specs; exempts `fake:`; passes
  when every model is a catalog spec; aggregates multiple unknowns.
- **API**: a `status='draft'` `WorkflowRecord` → 404 from `POST /api/runs` and
  absent from `GET /api/workflows`; `status='deployed'` runs and lists; a `crud`
  upsert yields an immediately-runnable (`deployed`) workflow; deploy / crud-upsert
  with a non-catalog, non-`fake:` model → 400 listing it, and with a catalog model
  or `fake:` → success.
- **Migration**: extend `tests/test_migrations.py` — `create_all → upgrade head`
  stays idempotent, an existing non-`deployed` row is backfilled, and the CHECK
  rejects an out-of-set status.
- **Full suite** green. Watch for existing tests that deploy a non-catalog,
  non-`fake:` model; add that spec to the seeded/test catalog or adjust the test.

## Out of scope

- P1-08 (typed tool namespace), P1-12 (`schema_version`), the versioning keystone
  cluster (P1-01/02/03/15/18), and all Phase-2 findings — each its own future
  sub-project per the triage register.
- Embedding-model validation; per-org model policies (P2-06); archived/disabled
  workflow states.

## Known limitation: model validation is deploy-time only

Agent models are validated against the catalog **when a workflow is deployed/
saved**, not when it is loaded to run. A deployed workflow keeps its stored model
spec, so if an admin later removes that model from the catalog (catalog deletion
does not revoke existing deployments), or a legacy row was promoted to `deployed`
by the migration without a deploy-time check, the invalid model surfaces at first
run rather than at load. Accepted deliberately: load-time re-validation (plus
adding catalog state to the workflow-cache freshness key) was weighed and left out
to keep this change surgical; revisit if per-org model policies (P2-06) or a
catalog-revocation requirement lands.
