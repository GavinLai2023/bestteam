# Built-in skills: drop the `_vN` name suffixes, lock the platform tier

Date: 2026-09-02
Status: approved (user rulings: full migration incl. the v1+v2 merge; platform
tier locked, customisation via org-tier copies; seeding updates unconditionally)

## Problem

Built-in skill names carry a version suffix (`property_maintenance_intake_v2`,
`contractor_sourcing_v1`) because seeding is insert-only: editing an existing
`DEFAULT_SKILLS` entry never reaches an already-seeded database, so every
behaviour change had to ship under a new name. Meanwhile `skill_versions`
already provides real per-skill version history, and deploy-time pinning
(`pipeline_dependencies.resource_version_id`) already isolates deployed teams
from head changes. The suffix is redundant versioning in the worst place — the
identifier — and it accretes: one dead row per generation, forever.

The root cause is a pair: (1) content is copied from code into the DB exactly
once, and (2) admins may edit the platform-tier row in place, which makes the
row unsafe for seeding ever to touch. Fixing either alone is not enough.

## Decisions

1. **Full migration.** Rename the platform built-ins to suffix-free names and
   merge `property_maintenance_intake_v1` + `_v2` into one skill whose
   `skill_versions` history holds both contents. Rewrite the old names in all
   stored data (details below).
2. **Lock the platform tier.** The admin Advanced surface no longer edits
   platform built-ins in place; customisation is a copy into the org tier,
   where the existing same-name shadowing in `load_skills` takes over. With
   the platform tier pristine, seeding may update it unconditionally.
3. **Seeding updates.** `seed_default_skills` becomes: insert when absent;
   when present and the head config differs from the code's canonical config,
   append a version (`created_by=None`) and move the head. Deployed teams are
   pinned to `skill_versions` rows and are unaffected until their next deploy.

## Name mapping

| Old (platform tier only) | New |
|---|---|
| `email_triage_reply` | unchanged |
| `email_input_security_core_v1` | `email_input_security_core` |
| `property_maintenance_intake_v1` + `property_maintenance_intake_v2` | `property_maintenance_intake` (merged) |
| `property_maintenance_response_v1` | `property_maintenance_response` |
| `contractor_sourcing_v1` | `contractor_sourcing` |

Org-tier rows are never renamed — an org's own `*_v1` naming is the
customer's business.

## Migration (`w0x1y2z3a4b5`, follows `v9w0x1y2z3a4`)

Runtime invariant that dictates the scope: agents are built from the pipeline
**head record's** config while pinned skills are keyed by
`pipeline_dependencies.resource_name` — the two must agree. So the rename must
be applied consistently wherever an old name is stored as data:

1. `skills.name` — platform rows only (`org_id IS NULL`).
2. `pipelines.config` — every `agents[*].skills` list (JSON-aware rewrite,
   not text substitution).
3. `pipeline_versions.config` — same rewrite.
4. `pipeline_dependencies.resource_name`.
5. `skill_versions.config` internal `name` — cosmetic (runtime overrides it
   with `resource_name`) but kept consistent.
6. Builder-session drafts, if they embed skill names (verify the stored
   shape during implementation; rewrite if present).

**Merge:** keep the `_v2` row, rename it to `property_maintenance_intake`.
Re-point the `_v1` row's `skill_versions` to the merged row and renumber
`version_number` by `created_at` (snapshot **ids are untouched**, so pinned
`resource_version_id` references stay valid). Re-point
`pipeline_dependencies.resource_id` values from the `_v1` row to the merged
row. Delete the empty `_v1` row.

**Human-edited platform rows:** the migration appends the code's canonical
config as a new version and moves the head; the human edit survives as a
historical version (viewable, and still served to teams pinned to it).

Code updated in the same change: `DEFAULT_SKILLS` (one intake entry, latest
content), `ui/backend/pipelines/property_maintenance_inbox_demo.yaml`,
comments/docs that teach the `_vN` convention.

## Seeding (`ui/backend/skills.py`)

```
for spec in DEFAULT_SKILLS:
    absent        -> publish_skill_version(created_by=None)   # as today
    present, head config != canonical config (normalised JSON compare)
                  -> publish_skill_version(created_by=None)   # append + move head
    present, equal -> no-op (idempotent)
```

Safe because the platform tier can no longer be hand-edited. Release of a new
skill definition reaches every deployment on its next start-up, with the old
definition retained in history.

## Platform tier locked (`ui/backend/crud.py` + Advanced UI)

- PUT on a platform-tier skill returns 409 (same style as the existing
  built-in delete guard). GET unaffected.
- The Advanced page shows platform skills read-only and offers **"Copy to
  organisation"**: pre-fills the editor with the platform config, admin picks
  the org, saves through the existing create path. The same-named org skill
  shadows the built-in via the existing fold order in `load_skills` — no
  loader change.

Out of scope: a platform-wide override tier (customisation that should apply
to *all* orgs has no home other than code; accepted), rollback buttons,
org-tier versioning changes.

## Version browsing + references (admin, read-only)

Two endpoints under the existing admin-only `/api/config` router:

- `GET /api/config/skills/{name}/versions?org=` →
  `[{version_number, created_by, created_at, config}]`, newest first.
- `GET /api/config/skills/{name}/references?org=` → join
  `pipeline_dependencies` → `pipeline_versions` → `pipelines` →
  `organizations`; rows
  `{org_name, org_display_name, pipeline_name, pinned_version_number,
  is_current_deploy, org_active}`. All pins are listed (not just current
  deploys) so an admin can judge whether a definition may still be served.

Advanced page, Skills tab detail pane:

- A version dropdown where `· vN` renders today; default = head. Selecting an
  older version shows that snapshot read-only with a "historical version
  (read-only)" note.
- A "Referenced by" section: one line per pin — org · team · pinned vN ·
  current/superseded; empty state when nothing references the skill.

## Organisation dropdown filter (frontend only)

`AdminOrg.active` is already returned. The org dropdowns on AdvancedPage and
TracePage default to `active` orgs with a "Show deactivated" checkbox beside
them. A currently-selected deactivated org stays visible in the list so the
selection cannot silently vanish. AccountsPage is untouched — it is where
deactivated orgs are managed and must always show them.

## Testing

- Migration test: fixture with a deployed team referencing `_v2`, pinned
  dependencies, and a hand-edited platform row; after upgrade, assert both
  `load_skills` paths (catalogue and pinned) resolve correctly and pinned
  snapshot ids are unchanged.
- Seeding: content change appends a version and moves the head; identical
  content is a no-op; absent rows still insert.
- Integration: versions/references endpoints; platform PUT → 409.
- Frontend: version dropdown, references list, org filter checkbox.

## Rollout

VPS: backup (existing cron), `alembic upgrade head`, then start the new code.
The migration rewrites live customer data; the migration test's fixture
mirrors the VPS shape (deployed demo-pipeline teams) as closely as practical.
