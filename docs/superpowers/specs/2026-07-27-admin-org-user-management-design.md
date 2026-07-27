# Admin UI for organizations & users — design

## Problem

Provisioning customer organizations and their user accounts is **CLI-only**
today (`python -m ui.backend.admin`), hitting `db/orgs.py` / `db/users.py`
directly. There is no HTTP surface for it and no UI — onboarding a customer
means shelling into the deployment. Platform admins already have web tooling for
config (`/advanced`) and memory (`/memory`), but not for the accounts those
depend on.

## Goal

Give platform admins a web page to do everyday org/user provisioning, while
keeping the most privileged operations out-of-band in the CLI. Add one new
capability the CLI lacks: **deactivating an organization** (a reversible full
suspend), used to offboard/pause a customer without destroying data.

## Scope

**In scope (UI + API):**
- **Organizations:** list, create, deactivate/reactivate. *No hard delete* (the
  CLI has none; deletion would cascade into workflows/runs/memory).
- **Org users** (the single customer login per org): create, reset password,
  move to another org, delete — full parity with the CLI for org members.
- **Passwords:** the admin types an initial/reset password in the form (parity
  with the CLI prompt; no self-service change exists yet).

**Out of scope (stays CLI-only / deferred, YAGNI):**
- Granting/revoking **admin** (`promote`/`demote`) — privilege escalation stays
  out-of-band.
- Creating/deleting **platform operators/admins** — their whole lifecycle stays
  in the CLI. The UI *shows* them read-only so an admin can see who has access.
- **Mailbox** connection — already has two surfaces (CLI + the org's
  self-service `/api/org/email` page); not duplicated here.
- Org **rename/hard-delete**, self-service password change, invite-link flows.
- First-admin **bootstrap** stays CLI (chicken-and-egg: you must already be an
  admin to reach the page).

## Security posture

This is the app's most security-sensitive surface: it mints logins, sets
passwords, and deletes accounts. Principles:

- Every route is `get_current_admin`-guarded (admin = `is_admin AND org_id IS
  NULL`), the same guard as `/api/config` and `/api/memory`.
- The UI can never escalate privilege: no route grants admin, and no route
  mutates a platform operator/admin account (create/delete/reset/move all
  **refuse** a non-org-member target with `409`).
- Passwords are never logged and never returned in any response.
- Destructive actions (delete user, deactivate org) require a confirmation in
  the UI.
- A `/security-review` pass runs on the branch before it is review-ready.

## Data model — org deactivation

Add one column:

- `organizations.active: bool` — `NOT NULL`, server default `true`.

Alembic migration (down_revision `d4e6b2c9f1a7`, the current head): add the
column with `server_default=sa.true()`, backfilling every existing org to
`active = true` so an upgrade suspends nothing. `Organization` model gains
`active: Mapped[bool] = mapped_column(default=True)`.

### What "deactivated" enforces (full suspend) — four points

1. **Login** (`auth_api.py::login`): after `authenticate_user` succeeds, if the
   user is an org member (`org_id` set) whose org is inactive, return `403`
   ("This organization has been deactivated.") instead of a token. Platform
   operators/admins (`org_id IS NULL`) are never affected.
2. **`get_current_org`** (`auth_api.py`): return `403` when the resolved org is
   inactive. This one dependency already gates every org-scoped surface
   (workflows list/graph, `POST /api/runs`, builder, org self-service), so a
   still-valid token issued before deactivation is stopped here too.
3. **Autonomous email trigger** (`db/email_triggers.py::list_enabled_triggers`):
   join `organizations` and filter to `active = true`, so a suspended org's
   trigger stops auto-running. (Filtering the query keeps the poller loop in
   `email_trigger.py::poll_once` unchanged.)
4. **Admin cross-org surfaces** (`/api/config?org=`, `/api/memory`): **not**
   blocked — admins keep full access to a deactivated org's data to manage,
   export, or reactivate it.

## Backend — new `ui/backend/admin_api.py`

Router `APIRouter(prefix="/api/admin", tags=["admin"],
dependencies=[Depends(get_current_admin)])`, registered in `main.py` beside the
config/memory routers.

### Endpoints

| Method & path | Body | Behaviour |
|---|---|---|
| `GET /api/admin/orgs` | — | `[{name, display_name, active, member}]`, `member` = the org's user's username or `null`. |
| `POST /api/admin/orgs` | `{name, display_name?}` | Create org. Calls `ensure_email_single_org(db, creating=1)` (CR-031) then `create_org`. `409` on duplicate name. |
| `PATCH /api/admin/orgs/{name}` | `{active: bool}` | Deactivate/reactivate via `db/orgs.py::set_org_active`. `404` unknown org. |
| `GET /api/admin/users` | — | `[{username, org, is_admin}]` across all logins (includes read-only platform accounts). |
| `POST /api/admin/users` | `{username, org, password}` | Create an **org member** (`org` required). `409`/`400` on the `create_user` rules (unique username, one-per-org, reserved `email-trigger`), `404` unknown org. |
| `POST /api/admin/users/{username}/password` | `{password}` | Reset password (org members only; `409` if target is a platform account/admin). |
| `POST /api/admin/users/{username}/move` | `{to_org}` | Move org→org (`409` if destination occupied or target is an admin; reconciles legacy memory to source org first — see below). |
| `DELETE /api/admin/users/{username}` | — | Delete an org member (`409` if target is a platform operator/admin). Purges memory first — see below. |

Request/response bodies are Pydantic models local to `admin_api.py`. `ValueError`
from the `db/` helpers is caught and mapped to `409` (conflict) or `400`
(malformed), reusing the helper's message text.

### New `db/` reads/writes

- `db/orgs.py::set_org_active(db, name, active) -> Organization` (raises
  `ValueError` on unknown org).
- `db/orgs.py::list_orgs` already exists; `admin_api` joins each org to its
  member (`db.query(User).filter_by(org_id=org.id).first()`).
- No new `users.py` write helpers — `create_user`/`set_user_org`/`delete_user`
  already exist; the API adds the org-member-only guard before calling them.

### Shared account-memory lifecycle (DRY with the CLI)

`delete` and `move` must run the same memory work the CLI does, **failing
closed**. Extract the three private helpers in `admin.py`
(`_open_memory_store`, `_purge_user_memory`, `_reconcile_legacy_org`) into a new
shared module `ui/backend/account_memory.py`:

- `purge_user_memory(username) -> Optional[int]` — `store.delete_user`; `None`
  when no store is configured for the process. Raises on failure.
- `reconcile_legacy_org(username, org_id) -> Optional[int]` —
  `store.assign_legacy_to_org`. Raises on failure.

`admin.py` imports these (behavior unchanged — same fail-closed sequence). The
API calls them the same way: **DELETE** purges before releasing the username
(fail closed: on purge error, abort with `409`, user not deleted); **move**
reconciles the user's legacy rows to their *source* org before `set_user_org`
(fail closed). The web backend always runs with the server's
`BESTTEAM_MEMORY_DB`, so unlike the CLI it never hits the "no store configured"
warning path in normal operation.

## CLI — small additions (`admin.py`)

Add `activate-org <name>` / `deactivate-org <name>` (call `set_org_active`), so
the two provisioning paths stay symmetric and an operator has a shell escape
hatch. No other CLI change (the memory helpers move to `account_memory.py` but
`admin.py` keeps identical behavior by importing them).

## Frontend — new `pages/AccountsPage.jsx`

- Route `/accounts` under `RequireAdmin` (`App.jsx`); nav link **Accounts** in
  the admin-only block of `Layout.jsx` (beside Advanced/Memory).
- Fetches `GET /api/admin/orgs` and `GET /api/admin/users` on mount.
- **Organizations** section: each org shows its display name/name, an **Active /
  Deactivated** badge, and its member (or "no member yet"). A "Create
  organization" form (name + display name). A Deactivate button (confirm) /
  Reactivate button → `PATCH`.
- **Per-org user actions:** no member → "Create user" (username + password +
  confirm); has member → "Reset password", "Move to org…", "Delete" (confirm).
- **Platform accounts** section: read-only list of `org == null` logins with an
  "admin" badge and a "managed via the CLI" note.
- `lib/api.js` gains `api.adminOrgs`, `api.createAdminOrg`, `api.setOrgActive`,
  `api.adminUsers`, `api.createAdminUser`, `api.resetAdminUserPassword`,
  `api.moveAdminUser`, `api.deleteAdminUser`.
- Errors surface via the existing banner pattern; the API wrapper already
  carries `.status` + `.message` (from the #34 fix).

## Error handling

- Conflict rules (`ValueError` from `db/`) → `409` with the helper's message,
  shown verbatim in the page banner.
- Guard violations (mutating a platform account) → `409` "manage platform
  accounts via the CLI".
- Deactivating an already-inactive org (or reactivating an active one) is
  idempotent (`200`, no error).
- A deactivated org's member is still fully manageable by the admin (reset /
  move / delete), so an admin can clean up a suspended customer.

## Testing

**Backend (`tests/`, pytest, `fake:` + in-memory DB):**
- `GET /orgs` / `GET /users` shapes, including `active` and `member`.
- Create org (dup → 409; `ensure_email_single_org` guard honored).
- Deactivate org → member login `403`; a token minted before deactivation →
  `get_current_org` `403`; `list_enabled_triggers` excludes it; admin
  `/api/config?org=<inactive>` still works. Reactivate restores login.
- Create org user (unique / one-per-org / reserved-name → 4xx; unknown org →
  404).
- Reset password (login works with the new password, fails with the old; target
  is an admin → 409).
- Move org→org (destination occupied → 409; legacy memory reconciled to source
  org — assert via the store; admin target → 409).
- Delete org user (memory purged — assert via the store; purge failure → user
  not deleted; platform/admin target → 409).
- Every route `get_current_admin`-guarded: anon → 401, org member → 403.
- Migration: existing orgs backfill `active = true`.

**Frontend (vitest):**
- `AccountsPage` renders orgs (with active/deactivated badge + member) and the
  platform-accounts read-only list; the create-org and per-user actions call the
  right `api.*` methods (mocked).
- `Layout` shows the **Accounts** link only for admins.

**Gates:** backend `pytest` green; frontend `npm test` / `npm run lint` /
`npm run build` green; `/security-review` on the branch.

## Files

- **Create** `ui/backend/admin_api.py` — the router.
- **Create** `ui/backend/account_memory.py` — shared purge/reconcile helpers.
- **Create** `alembic/versions/<rev>_add_org_active.py` — the `active` column.
- **Modify** `ui/backend/db/models.py` — `Organization.active`.
- **Modify** `ui/backend/db/orgs.py` — `set_org_active`.
- **Modify** `ui/backend/db/email_triggers.py` — `list_enabled_triggers` active filter.
- **Modify** `ui/backend/auth_api.py` — login + `get_current_org` inactive-org checks.
- **Modify** `ui/backend/main.py` — register the admin router.
- **Modify** `ui/backend/admin.py` — `activate-org`/`deactivate-org`; import moved memory helpers.
- **Create** `ui/frontend/src/pages/AccountsPage.jsx` (+ `.test.jsx`).
- **Modify** `ui/frontend/src/App.jsx` — `/accounts` route.
- **Modify** `ui/frontend/src/components/Layout.jsx` (+ its test) — Accounts nav link.
- **Modify** `ui/frontend/src/lib/api.js` — `api.admin*` methods.
- **Modify** docs: `ui/backend/CLAUDE.md`, `ui/frontend/CLAUDE.md`, `docs/ADMIN_GUIDE.md` (note the UI now does everyday provisioning; CLI still owns escalation + bootstrap).

## Branch / methodology note

Branched off `feat/operator-role-routing` (PR #35, itself stacked on #34) for
the vitest harness and the current admin nav structure. The PR for this branch
targets `feat/operator-role-routing`; as #34 then #35 merge, GitHub retargets
this toward `main`. Merge order: #34 → #35 → this.
