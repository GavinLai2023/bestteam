# Platform-operator role-aware routing — design

## Problem

A **platform operator** (`users.is_admin = TRUE`, `org_id IS NULL` — the two are
mutually exclusive per CR-030) has no organization. Every customer-facing page
is org-scoped and therefore returns `403` for an operator:

- **Talk to your team** (`/`, `MonitorPage`) → `GET /api/workflows` 403.
- **My teams** (`/teams`, `SessionsPage`) → builder/workflows 403.
- **Build a team** (`/wizard`) → builder 403.

Today an operator logs in, lands on `/` (a customer page), and is greeted by red
error banners on all three customer pages. Meanwhile the two pages that *are*
theirs — **Advanced** (config CRUD across orgs) and **Memory** (per-user memory
admin) — work correctly. The experience is backwards: the operator is shown
customer tooling they structurally cannot use, and their actual tooling is
tucked behind nav links appended after the customer ones.

`GET /api/auth/me` already returns `{username, is_admin, org}`, so the frontend
already knows who is an operator. The backend already enforces authorization on
every endpoint; nothing here changes that. This is a **client-side routing/nav**
problem only.

## Goal

Treat platform operators as *not customers*: hide the customer pages from an
operator's navigation and land them on their admin home (`/advanced`), while org
members keep exactly today's customer experience and never see the admin pages.
Purely frontend; no backend change.

## Non-goals (YAGNI / deferred)

- **Operate-on-behalf-of-org (impersonation).** Letting an org-less operator use
  the customer pages within a chosen org's context would require backend support
  for org-scoped access by an admin across workflows/runs/builder plus an org
  switcher. Explicitly out of scope.
- **A dedicated operator home page.** Reuse the existing `/advanced` page as the
  operator's landing rather than building a new `/operator` route.
- **Hoisting `useMe()` into a shared context** to dedupe the `/api/auth/me`
  request. Two concurrent `/me` calls (one guard + `Layout`) is the current
  status quo and introduces no regression; a context refactor is unnecessary.
- **Operator provisioning of orgs/users from the UI.** Orgs, users, and admins
  stay CLI-only (`python -m ui.backend.admin`).

## Architecture

All changes are in `ui/frontend/src/`. The role split is expressed as two
React Router route guards plus role-gated nav links. It mirrors the existing
`RequireAdmin` guard (which already sends non-admins away from admin routes),
adding its symmetric counterpart for customer routes.

Frontend gating here is **cosmetic** — identical in spirit to the existing
`RequireAdmin`. The backend remains the real authority: a tampered client that
forces its way onto a customer surface still gets `403`, and an operator forced
onto `/advanced` is still admitted only because the backend authorizes them.

### Components

1. **`RequireOrgMember`** — a new guard in `App.jsx`, the mirror of
   `RequireAdmin`:
   - Reads `useMe()`.
   - While `loading` → render `null` (prevents a customer-page flash before the
     role is known — the same pattern `RequireAdmin` uses).
   - If `isAdmin` (operator) → `<Navigate to="/advanced" replace />`.
   - Else → `<Outlet />`.

2. **Route tree** (`App.jsx`) — wrap the customer routes in `RequireOrgMember`:
   - `/` → `MonitorPage`
   - `/teams` → `SessionsPage`
   - `/wizard` (+ children) → `WizardLayout`

   The admin routes stay under `RequireAdmin`. The `*` catch-all stays **outside
   both guards**, directly under `Layout`, as `<Navigate to="/" replace />`, so
   an unknown path routes to `/` and the role guard there decides the
   destination (operator → `/advanced`), rather than the catch-all being
   shadowed by a guard.

3. **Nav gating** (`Layout.jsx`) — render the three customer links (Build a team
   / My teams / Talk to your team) only when `!isAdmin`; the Advanced / Memory
   links only when `isAdmin` (unchanged). "Log out" always renders. Net result:
   - **Org member:** Build a team · My teams · Talk to your team · Log out.
   - **Operator:** Advanced · Memory · Log out.

`LoginPage` is unchanged: it still `navigate('/')` after login, and the guard on
`/` becomes the single source of redirect truth.

## Data flow

1. Login → token stored → `navigate('/')`.
2. `/` is under `RequireOrgMember`, which calls `useMe()` → `GET /api/auth/me`.
3. Org member (`is_admin: false`) → renders `MonitorPage`.
4. Operator (`is_admin: true`) → `<Navigate to="/advanced">` → `RequireAdmin`
   admits them → `AdvancedPage`.

## Error handling / edge cases

- **`/me` still loading:** guard renders `null` — no premature redirect and no
  flash of a customer page for an operator. Matches `RequireAdmin`.
- **`/me` fails (non-401):** `me` is `null` → `isAdmin` is `false` → user is
  treated as an org member (sees customer pages). Safe default; the backend
  still enforces org scoping. This matches the existing `RequireAdmin`
  null-handling (a null `me` is treated as non-admin).
- **401:** handled upstream in `lib/api.js` — the token is cleared and the user
  is redirected to `/login`.
- **No redirect loops:** `is_admin` and org membership are mutually exclusive, so
  the two guards partition all users. An org member hitting `/advanced` →
  `RequireAdmin` → `/` → `RequireOrgMember` admits (one hop). An operator hitting
  `/` → `RequireOrgMember` → `/advanced` → `RequireAdmin` admits (one hop).
  Neither guard can bounce a user back into the other's territory.

## Testing

Uses the vitest + jsdom + testing-library harness (`npm test`).

- **`RequireOrgMember`** (via a small route tree of **stub elements** — not the
  real `AdvancedPage`/`MonitorPage`, which fetch — rendered in `MemoryRouter`,
  with `useMe`/`api.me` mocked to isolate guard behavior):
  - operator (`is_admin: true`) → redirected to `/advanced` (assert the
    `/advanced` content renders, the customer outlet does not).
  - org member (`is_admin: false`) → the customer outlet renders.
  - loading → nothing renders (neither the outlet nor a redirect target).
- **`RequireAdmin`** regression (no existing test today):
  - org member → redirected away from `/advanced` to `/`.
  - operator → `/advanced` content renders.
- **`Layout` nav** (mock `useMe`):
  - operator → Advanced and Memory links present; Build a team / My teams /
    Talk to your team absent.
  - org member → the three customer links present; Advanced / Memory absent.

Gates: `npm test` (all pass), `npm run lint` (clean), `npm run build`
(succeeds).

## Files

- **Modify** `ui/frontend/src/App.jsx` — add `RequireOrgMember`; wrap customer
  routes; keep `*` outside the guards.
- **Modify** `ui/frontend/src/components/Layout.jsx` — gate the customer nav
  links on `!isAdmin`.
- **Create** `ui/frontend/src/App.test.jsx` — guard behavior (`RequireOrgMember`
  + `RequireAdmin` regression).
- **Create** `ui/frontend/src/components/Layout.test.jsx` — role-gated nav.
- **Modify** `ui/frontend/CLAUDE.md` — document the operator/org-member routing
  split (the admin-pages note already describes `RequireAdmin`; extend it).

## Branch / methodology note

Branched off `fix/monitor-403-mislabel` (PR #34) because this work requires the
vitest harness introduced there. The PR for this branch targets that branch (a
clean stacked diff); once #34 merges to `main`, this retargets to `main`.
