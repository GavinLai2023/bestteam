# Versioning keystone — immutable workflow versions + stable team identity

**Date:** 2026-07-25
**Findings:** P1-01 (team identity ambiguous), P1-02 (multiple sessions →
one deployed object), P1-03 (no immutable workflow versions), from
`docs/DATA_ARCHITECTURE_REVIEW_REPORT.md`.
**Branch:** `feat/versioning-keystone` off `main`.

## Problem

The three highest-priority Phase-1 findings are one entangled defect: a
deployed team has **no stable identity and no version history**.

- `WorkflowRecord` is keyed `(org_id, name)` and every deploy **overwrites
  `config` in place** — `builder.py::deploy_session` (record.config = raw) and
  `crud.py::upsert_workflow_config` (item.config = raw). Prior configurations
  are lost; there is no rollback point and no immutable execution snapshot.
- A `BuilderSession` links to its deployed `WorkflowRecord` only by the name
  string inside `specification_json`. Two sessions with the same spec name
  silently deploy to (and clobber) the same row while both appear as separate
  "teams" in the wizard's session list (P1-02, confirmed in code:
  `SessionsPage.jsx` lists sessions keyed by `session.id`, deploy upserts by
  `(org_id, spec.name)`).
- A `Run` records only the workflow **name** (`runtime.py`, `models.py:234`).
  It cannot identify the exact configuration it executed. `Run.builder_session_id`
  exists but is never populated; there is no `workflow_version_id`.

## Goal

Give a deployed team a stable identity with an append-only, immutable version
history; make deploy **publish a new version** instead of overwriting; and
stamp each production Run with the exact version it executed — so configs are
no longer lost and runs become auditable/reproducible against a frozen
snapshot.

## Scope decisions (locked with the user)

1. **Repurpose `WorkflowRecord` as the stable team head**, and add a
   `workflow_versions` child table — *not* new `AITeam`/`AITeamVersion`
   tables. `WorkflowRecord` already has an unexposed integer PK and an
   `(org_id, name)` unique constraint; it already *is* the deployable
   aggregate, distinct from both the SDK `Team` and `BuilderSession`. This
   keeps all external addressing name-based, so **no frontend changes** are
   needed.
2. **A "version" freezes the `config` blob and links the Run.** Standalone
   Skills, KBs, per-org email tools, and model specs are still resolved *by
   name at load time* (`main.py::_get_workflow`); freezing those is P1-04
   (typed dependency records) and remains a documented P1-05 limitation —
   **out of scope here**.
3. **Backend / data-model only.** Version-history / rollback UI and the P1-01
   UI-terminology cleanup ("My teams" lists sessions, not teams) are deferred
   to a follow-up.

## Design

### New table: `workflow_versions` (immutable snapshots)

`ui/backend/db/models.py` — new `WorkflowVersion`:

```python
class WorkflowVersion(Base):
    """An immutable published snapshot of a WorkflowRecord's config.

    Deploy appends one row (never updates an existing one) and points the
    parent WorkflowRecord.current_version_id at it. A Run references the exact
    version it executed. Standalone Skills/KBs/models are still resolved by
    name at load (P1-04/P1-05), so this freezes the inline config blob, not
    the resolved dependency graph."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version_number",
                         name="uq_workflow_versions_workflow_id_version_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"))
    version_number: Mapped[int]
    config: Mapped[dict[str, Any]] = mapped_column(JSON)  # never updated after insert
    created_by: Mapped[Optional[str]] = mapped_column(nullable=True)  # deploying username, else NULL
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

### `WorkflowRecord` becomes the team head

Add one column; keep `config` as a **mirror of the current version** so every
existing reader (`_get_workflow`, the `(org_id, name)` cache, the admin CRUD
list) is untouched. Immutable history lives in `workflow_versions`; `config`
is just "current published".

```python
current_version_id: Mapped[Optional[int]] = mapped_column(
    ForeignKey("workflow_versions.id"), nullable=True
)
```

(The FK pair `workflows.current_version_id ↔ workflow_versions.workflow_id` is
circular; harmless here because SQLite FK enforcement is off — the columns are
advisory, like every other FK in this schema.)

### Deploy = publish a new version (not overwrite)

New module `ui/backend/db/workflows.py`:

```python
def publish_workflow_version(db, *, org_id, name, config,
                             workflow_id=None, created_by=None):
    """Publish `config` as the next immutable version of a team head, moving
    its current-version pointer. Returns (WorkflowRecord, WorkflowVersion).

    workflow_id given  -> that existing head (rename-safe: record.name = name).
    workflow_id None   -> resolve-or-create the head by (org_id, name)."""
    if workflow_id is not None:
        record = db.get(WorkflowRecord, workflow_id)
        record.name = name
        record.config = config
        record.status = "deployed"
    else:
        record = db.query(WorkflowRecord).filter_by(name=name, org_id=org_id).one_or_none()
        if record is None:
            record = WorkflowRecord(name=name, config=config, status="deployed", org_id=org_id)
            db.add(record)
        else:
            record.config = config
            record.status = "deployed"
    db.flush()  # record.id
    next_number = ((db.query(func.max(WorkflowVersion.version_number))
                      .filter_by(workflow_id=record.id).scalar()) or 0) + 1
    version = WorkflowVersion(workflow_id=record.id, version_number=next_number,
                              config=config, created_by=created_by)
    db.add(version)
    db.flush()  # version.id
    record.current_version_id = version.id
    return record, version


def current_version_id(db, org_id, name):
    """The current_version_id of a deployed team by (org_id, name), or None."""
    record = (db.query(WorkflowRecord)
                .filter_by(org_id=org_id, name=name, status="deployed").one_or_none())
    return record.current_version_id if record else None
```

Version-number allocation is already serialized: **both** deploy paths run
inside the process-wide `component_mutation_lock` (`builder.py::deploy_session`,
`crud.py::upsert_workflow_config`); the `(workflow_id, version_number)` unique
constraint is the backstop. (Single-process constraint, same as the email
poller — a multi-worker deployment would need a cross-process lock.)

Rewire the two deploy points to call `publish_workflow_version` instead of the
in-place overwrite:

- **`builder.py::deploy_session`** — pass `workflow_id=session.workflow_id`
  and `created_by=<deploying user>` (where the route has it); after publishing,
  set `session.workflow_id = record.id`. This is the **P1-02 fix**: a session
  remembers its team, a redeploy appends a version under the *same* head, and a
  rename keeps the head. Preserve the single-shared-commit atomicity (P1-14) —
  add no new commit; the existing `update_session` commit covers both writes.
- **`crud.py::upsert_workflow_config`** — `workflow_id=None` (no session);
  resolve-or-create by `(org_id, item_name)`; keep its explicit single
  `db.commit()`.

### Link Runs to the executed version

- Add to `Run`: `workflow_version_id: Mapped[Optional[int]] =
  mapped_column(ForeignKey("workflow_versions.id"), nullable=True)`.
- `main.py::create_run` resolves `current_version_id(db, org.id, req.workflow)`
  and passes a new `workflow_version_id=` kwarg to
  `runtime.run_in_background`, which writes it on the `Run` row (beside
  `workflow=name`).
- `email_trigger.py` deployed-run path — stamp the same way (reuses
  `current_version_id`).
- `builder.py::create_test_run` (sandbox) — leaves `workflow_version_id=None`;
  a sandbox test-run executes the *session spec*, not a published version.

### Migration (new head, `down_revision = "b1d7e4f2a9c8"`)

Follow the established guarded/idempotent + `op.batch_alter_table` pattern
(`db_session` runs `create_all` before `upgrade`, so a fresh DB already has the
new table/columns — every step must be inspect-guarded):

1. Create `workflow_versions` if absent (`sa.inspect(bind).get_table_names()`).
2. Add `workflows.current_version_id`, `builder_sessions.workflow_id`,
   `runs.workflow_version_id` — each guarded by a `_has_column` inspection,
   inside `op.batch_alter_table` (SQLite).
3. **Backfill (idempotent):** for every `workflows` row with
   `current_version_id IS NULL`, insert one `workflow_versions` row
   (`version_number=1`, `config` = the row's current `config`, `created_at` =
   the row's `created_at`) and set `current_version_id`. Re-running is a no-op
   (the NULL filter). **No** JSON-parsing backfill for
   `builder_sessions.workflow_id` or `runs.workflow_version_id` — those
   forward-populate on the next deploy / next run; historical rows stay NULL
   (deliberate: avoids fragile raw-SQL JSON matching).

## Verification

- **Unit** (`publish_workflow_version`): first deploy → v1 + `current_version_id`
  set; redeploy → v2, pointer moves, **v1.config unchanged** (immutability);
  redeploy under the same `workflow_id` with a new name → same head, `name`
  updated, v2 appended.
- **P1-02** (`test_builder_api`): two distinct sessions with the same spec name
  → both end with the *same* `session.workflow_id`; two versions exist; the
  first config is preserved as v1 (no silent clobber). One session redeployed
  twice → same `workflow_id`, versions 1 → 2.
- **Run linkage**: `POST /api/runs` on a deployed workflow → Run row's
  `workflow_version_id == current`; sandbox test-run → NULL.
- **Migration** (`test_migrations`): `create_all → upgrade head` idempotent;
  backfill creates exactly one v1 per existing workflow and sets the pointer;
  re-running upgrade adds nothing.
- Full suite green with the scratch DB
  (`BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db"
  ./.venv/Scripts/python.exe -m pytest -q`); frontend unaffected.

## Out of scope (deferred, documented)

- Freezing standalone Skill/KB/model resolution into versions — P1-04 typed
  dependency records; standalone-component drift remains a P1-05 known
  limitation.
- Version-history / rollback **UI** and the P1-01 UI-terminology cleanup.
- Rollback **execution** (activating an old version) — history is retained and
  queryable, but there is no endpoint/UI to re-publish an old version here.
- SQLite FK enforcement (P1-13, still deferred; the new FKs are advisory like
  the rest).
- Retro-linking historical runs / sessions (forward-populated only).

## Known limitation

- **Rename onto an existing team name is a hard error, not a friendly one.** If a
  session pinned to head H (name "X") redeploys with its spec renamed to "Y" and
  a *different* deployed team already owns "Y" in the org, the `record.name = "Y"`
  write in `publish_workflow_version` collides with the `(org_id, name)` unique
  constraint and surfaces as a 500. This is a narrow edge and strictly safer than
  the pre-versioning behavior (which silently clobbered the other team — the P1-02
  bug); a friendly 400 pre-check is deferred.
