# Org-based multi-tenancy (sub-project 1: org model + isolation) — design

## Context

The business anticipates one **shared hosted platform** serving several
customer organisations, where each customer org may have **multiple employee
accounts** and each org uses its own external services (e.g. mail servers).
This reverses the Accepted "per-customer instance, no multi-tenancy"
decision in `docs/DECISIONS.md` — which this sub-project formally
supersedes.

Today there is **zero cross-customer isolation**: every resource (workflows,
teams, agents, knowledge bases, skills, builder sessions, runs, usage) is
global to the deployment. Worse, two surfaces have no ownership checks even
per-user: `GET /api/runs/{run_id}` and the run-stream WebSocket accept any
authenticated user for any run, and any authenticated user can read any
builder session. A shared platform is impossible until this is fixed.

**Program decomposition** (each its own spec → plan → implementation):

1. **Org model + row-level isolation** — this spec.
2. Encrypted per-org secrets store — later.
3. Per-org integration settings (email first) — later.
4. Infra hardening (Postgres, backups, rate limiting) — only when real
   usage numbers demand it.

## Decisions (confirmed with user)

| Decision | Choice | Why |
|---|---|---|
| Users per org | **Multiple** | Customers will want accounts for several employees; per-person login, memory, and audit; org owns shared resources |
| Provisioning | **Operator-only** — public registration removed | High-touch B2B onboarding; no self-service attack surface; org-admin self-service can come later |
| Database | **Stay SQLite, design DB-agnostic** | Scale unknown; SQLAlchemy/Alembic keep Postgres available as a data-migration-only step |
| Isolation mechanism | **`org_id` column + API-layer scoping** | One DB, one migration, works on SQLite and Postgres, easy to test; Postgres RLS can be layered on the same columns later |

## Data model

- New `organizations` table: `id`, `name` (unique), `display_name`,
  `created_at`.
- `users.org_id` — nullable FK. **NULL = platform operator** (the existing
  `is_admin` accounts belong to the platform, not to a customer). Usernames
  stay **globally unique**: JWT `sub` and per-user memory both key on the
  username.
- Org-owned (gain `org_id`): `agents`, `teams`, `knowledge_bases`,
  `workflows`, `builder_sessions`, `runs`, `usage_records`. Memory needs no
  schema change (already per-user; users belong to orgs).
- `skills.org_id` nullable: NULL = platform built-in (e.g.
  `email_triage_reply`) visible to every org; non-null = that org's own
  skill. An org's own skill shadows a same-named built-in.
- Global (unscoped): `model_catalog`, demo workflow YAMLs in
  `WORKFLOWS_DIR` (read-only, visible to all orgs).
- Unique constraints on the five named component tables change from `name`
  to **`(org_id, name)`** — two customers can both have a `support_triage`
  workflow.

## Enforcement

- New `get_current_org` dependency (builds on `get_current_user`): resolves
  the requesting user's org; **403 for platform operators** on org-user
  surfaces (operators act via the admin `/api/config` surface).
- Scoping is centralized in shared helpers (`load_skills(db, org_id)`,
  `load_knowledge_base_tools(..., org_id)`, org-filtered queries) — not
  sprinkled ad hoc.
- **Cross-org access returns 404, not 403** — existence is not revealed.
- Runs: record `org_id` (+ `username` informationally) at creation; run GET
  and the WebSocket stream check the run's org against the requester's
  (WS refusal uses the same close code as unknown-run, 4404 — no existence
  oracle). Platform admins get read passthrough for debugging.
- `_workflow_cache` re-keyed by `(org_id, name)`; YAML demos cached shared
  under `(None, name)`.
- KB upload directories become `knowledge_base_uploads/<org_id>/<name>`
  (existing path-containment guards kept; legacy dirs keep working since KB
  configs embed absolute paths).
- Builder sessions: org-scoped (colleagues within an org share sessions);
  Solution Architect catalogs (skills/KBs) are built from built-ins + the
  session org's records only.
- Usage records carry a denormalized `org_id` — the future per-customer
  billing dimension.
- Admin surfaces (`/api/config`, `/api/memory`) stay operator-only and
  cross-org: lists label each item's org and accept `?org=` filters;
  mutations target an explicit `?org=<name>` (skills: omitted = built-in
  tier).
- Email tools' env-var configuration must **not** be set on a shared
  instance (one process-level mailbox would be shared across orgs);
  per-org email credentials are sub-project 3.

## Provisioning

- `POST /api/auth/register` is **removed** (login stays public).
- Operator CLI (`python -m ui.backend.admin`) gains: `create-org <name>`,
  `create-user <username> --org <name>` (interactive password prompt;
  `--platform` creates an org-NULL operator), `list-orgs` — alongside the
  existing `promote`/`demote`/`list`.

## Migration & rollout

One Alembic revision: create `organizations` → seed a `default` org → add
`org_id` columns (SQLite batch mode for the FK/unique swaps) → backfill
(non-admin users and all org-owned rows → `default`; admins and built-in
skills stay NULL) → swap unique constraints to `(org_id, name)`.

An existing single-customer deployment upgrades in place and behaves
identically (one org). **Per-customer instances and the shared platform run
the same code** — the difference is the number of orgs. `DECISIONS.md`'s
old entry is superseded by a new one recording exactly that.

## Out of scope (deferred)

Org-admin roles & invites · secrets store (sub-2) · per-org email/LLM
credentials (sub-3) · Postgres · quotas/rate limiting · org self-service ·
`trace_events` persistence (Phase 5 of the run-state roadmap — gets
`org_id` when it's wired up).

## Testing

TDD throughout; the signature matrix (per resource: list-scoped / cross-org
get 404 / cross-org mutate 404; runs: GET + WS refusal; same-name-two-orgs
succeeds) lives in a dedicated `tests/test_org_isolation.py`. Migration
verified against a copy of a real database. Full suite green after every
phase.
