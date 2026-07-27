# bestteam — `ui/frontend/` (React + Vite frontend)

Directory-scoped notes for the monitoring dashboard and Team Builder wizard
frontend. See the root `CLAUDE.md` for project overview, architecture, and
commands; see `ui/backend/CLAUDE.md` for the API this frontend talks to.

> **Note:** the section below still describes the original six-stage wizard
> (`/wizard/:sessionId/{requirements|team|refine|test|deploy}`). A later
> commit (`0d2490a`, "reorder Team Builder wizard from 6 stages to 4
> customer-facing stages") changed the routes to `PreviewPage`/
> `ConfirmPage`/etc. This file was split out of the root `CLAUDE.md`
> verbatim; refreshing it to describe the current 4-stage flow is a separate
> follow-up.

## Frontend — wizard UI (Phase 4, `ui/frontend/src/`)

`react-router-dom` (`main.jsx` wraps `<App/>` in `<BrowserRouter>`) drives
three areas, all under a shared `<Layout/>` nav shell (`components/Layout.jsx`):

- **`/`** — `pages/MonitorPage.jsx` (the original runtime-monitoring
  dashboard, unchanged apart from reading an optional `?workflow=` query
  param via `useSearchParams` to pre-select a workflow).
- **`/advanced`** — `pages/AdvancedPage.jsx`, raw-JSON CRUD over
  `/api/config/{workflows|skills|knowledge_bases|model-catalog}` plus a
  read-only `tools` tab — the operator-only "advanced view" for direct edits.
  Tabs run whole-then-parts (Workflows → Skills → Knowledge bases → Tools →
  Model catalog). Each `KINDS` entry carries an `orgScope` mirroring the
  backend: `required` (`?org=` or 422), `optional` (skills — omitted means the
  platform built-in tier), `none` (org-less). **A workflow is what the wizard
  and customer UI call an "AI team"**; this page uses the technical noun
  because it matches the JSON keys the operator is editing.
- **`/wizard`** (+ `/wizard/:sessionId/{requirements|team|refine|test|deploy}`)
  — the six-stage Team Builder wizard, `components/WizardLayout.jsx` as the
  shared chrome:
  - `lib/api.js` — shared `fetch` wrapper (`API_BASE`/`WS_BASE` point at
    `http://127.0.0.1:8000`) exposing every backend endpoint as `api.*`
    methods.
  - `lib/useBuilderSession.js` / `lib/useModelCatalog.js` — fetch-on-mount
    hooks; `WizardLayout` calls `useBuilderSession(sessionId)` once and hands
    `{session, setSession, loading, refresh, sessionId}` to the active stage
    page via `useOutletContext()`.
  - `components/WizardProgress.jsx` — the 6-step progress bar. A step is
    "unlocked" based on **data presence** (`session.requirements_json` /
    `session.specification_json`), not the session's `status` string, so
    revisiting earlier stages after a `solution`/`testing`/`deployed` status
    doesn't relock later steps.
  - `pages/wizard/*.jsx` — one page per stage (`IntentPage` has no
    `sessionId` yet and creates the session via `api.createSession()`;
    `RequirementsPage`/`TeamPage`/`RefinePage` each support both a "generate
    with `model` (+ optional `feedback`)" path and a "confirm/edit the
    drafted JSON directly" path via `BulletEditor`/raw field edits;
    `TestPage` runs `api.createTestRun()` then streams the same
    `/api/runs/{id}/stream` WebSocket as `MonitorPage`; `DeployPage` calls
    `api.deploySession()` and links to `/?workflow=<name>` for "Talk to your
    team").
  - `components/TeamFlow.jsx` + `EmployeeCard.jsx` — the customer-facing
    "meet your team" diagram: renders `Specification.teams`/`agents` as
    grouped "virtual employee" cards (avatar-initial + `display_name` +
    `friendly_description`, falling back to `name`/`role`/`goal`), laid out
    per `team.mode` (sequential = arrows between cards, parallel =
    side-by-side, hierarchical = manager card above member cards). No
    Mermaid — pure CSS/HTML, since the audience is non-technical.
  - All wizard pages/components share styles from
    `components/WizardLayout.css` (cards, fields, buttons, banners, bullet
    editor, team-flow/employee-card, activity feed).

## Auth and login UI

`lib/api.js` stores a bearer token in `localStorage` (key `bestteam_token`),
attaches `Authorization: Bearer <token>` to every request, and on a `401`
(except from `/api/auth/*`, to avoid masking login errors) clears the token
and redirects to `/login`. `pages/LoginPage.jsx` is the username/password
form; `App.jsx`'s `RequireAuth` route guard redirects to `/login` when no
token is present, and `components/Layout.jsx` has a "Log out" button.

**Role-aware routing.** A platform operator (`is_admin`, `org_id IS NULL`) and
an org member see disjoint UIs, partitioned by two symmetric `App.jsx` guards
that both read `lib/useMe.js` (one `GET /api/auth/me` → `{username, is_admin,
org}`) and render `null` while it loads:

- `RequireAdmin` wraps `/advanced` + `/memory`; non-admins are sent to `/`.
- `RequireOrgMember` wraps the customer routes (`/`, `/teams`, `/wizard/*`);
  operators are sent to `/advanced`, since every org-scoped surface 403s an
  org-less operator. The `*` catch-all stays **outside** both guards so an
  unknown path routes to `/`, where `RequireOrgMember` picks the destination.

Because `is_admin` and org membership are mutually exclusive (CR-030), the two
guards can't bounce a user between them — each redirect terminates in one hop.
`Layout.jsx` mirrors this: the **Advanced**/**Memory** links show only when
`isAdmin`, the **Build a team**/**My teams**/**Talk to your team** links only
when `!isAdmin`. `pages/MemoryPage.jsx` is the admin per-user memory manager
(user list with counts + search/type-filter + per-record delete + clear-all,
and a "memory not enabled" state). All of this gating is cosmetic — the backend
enforces admin on every `/api/config` and `/api/memory` call and org scoping on
every customer surface, so a tampered client still gets 403.
`API_BASE`/`WS_BASE` are configurable via `VITE_API_BASE`/`VITE_WS_BASE`
(see `ui/frontend/.env.example`), falling back to the `127.0.0.1:8000`
defaults above for local dev.
