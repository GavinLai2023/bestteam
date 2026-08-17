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

`react-router-dom` (`main.tsx` wraps `<App/>` in `<BrowserRouter>`) drives
several areas, all under a shared `<Layout/>` nav shell (`components/Layout.tsx`;
customer nav is Dashboard / Build a team / My teams / Run a team):

- **`/`** — `pages/LandingPage.tsx`: not a page but a router. It forwards an
  org member to `/activity` (the Dashboard), or to `/wizard` if the org has no
  deployed workflow yet; `RequireOrgMember` sends an admin to `/advanced`
  before it renders. "Run a team" is a deliberate destination (`/run`), not
  the daily home.
- **`/run`** — `pages/MonitorPage.tsx`, "Run a team" (renamed from "Talk to
  your team"): reads an optional `?workflow=` query param via
  `useSearchParams` to pre-select a workflow, shows a running timer/WS
  connection status/"waiting for the agent" hint/stale-run banner while a
  run is in flight, and a Stop button (`POST /api/runs/{id}/cancel`) gated
  on the new run's id having actually arrived, so an early click can't
  silently no-op or target the previous run. Live events render via the
  shared `lib/traceEvents.ts` helpers (`EVENT_LABELS`/`RESULT_LABELS`/
  `TERMINAL_TYPES`/`renderEventData`), also used by `components/RunDetail.tsx`.
- **`/activity`** — `pages/ActivityPage.tsx`: an Automations tab
  (`components/EmailTriggerActivity.tsx`, plus — for the Property Maintenance
  Inbox vertical template, see `ui/backend/CLAUDE.md` — `components/
  MaintenanceInboxSummary.tsx` fetching `GET /api/automation-results/summary`
  and `components/NeedsAttentionList.tsx` fetching `GET /api/automation-results
  ?needs_attention=true`; both render nothing for an org that isn't using this
  template, both refresh on the same 30s cadence while the tab is open (rather
  than only on mount), and `NeedsAttentionList`'s "View run" jumps to the Runs
  tab and opens that run's detail -- `ActivityPage`'s `onOpenRun` looks up the
  run's real, persisted status via `GET /api/runs?run_id=` (org-scoped, DB-backed,
  unlike `GET /api/runs/{id}`'s in-memory-registry-only route) before opening it,
  falling back to `completed` only if that lookup itself fails; a needs-attention
  item's run is not guaranteed to have completed -- a dispatch failure still
  synthesizes needs_attention error rows for its UIDs -- so hardcoding `completed`
  used to permanently hide the Retry button for one that actually failed (Codex
  review finding)) and a Runs tab (`GET /api/runs`, filterable by
  team/manual-or-automatic/status; polls every 5s while a listed row is still
  `running`, guarded against a stale poll response clobbering a
  since-changed filter's results). Clicking a run opens
  `components/RunDetail.tsx` in a panel: a `running` run streams live over
  the same WebSocket `MonitorPage` uses, anything else fetches
  `GET /api/runs/{id}/trace` once (no live/historical merge); `RunDetail`
  also fetches that run's `GET /api/automation-results?run_id=` (renders
  nothing for a run with none, and refetches when a live run's terminal event
  arrives, since `normalize_run_result` only writes results after the run
  finishes -- and now always writes/publishes in that order server-side,
  closing a previous race where the refetch could arrive before the rows
  existed; results include `classification`/`category`/`missing_information`/
  `risk_reasons`, not just status/priority/summary/address/reason/draft) and,
  for a `failed` run OR one whose live event stream just emitted `run_failed`
  (the `status` prop alone is set once at click time by `ActivityPage` and
  never updates while the panel stays open, so a run that fails mid-view
  needs this second signal or Retry wouldn't appear until the panel is
  closed and reopened) **and** is autonomous (`autonomous` prop, threaded
  from `GET /api/runs`' own `autonomous` flag through `ActivityPage`'s
  `selectedRun` -- a manual run has no `trigger_context` and always 400s from
  `POST /api/runs/{id}/retry`, so Retry must not even render for one), shows
  a Retry button that calls the `onRetried(newRunId)` prop on success --
  `ActivityPage.tsx` wires this to select the newly created run (always
  itself autonomous) with the same `setTab('runs')`/`setSelectedRun()`
  pattern `NeedsAttentionList`'s "View run" uses (which also always passes
  `autonomous: true`, since every automation result belongs to an autonomous
  run by construction). See `ui/backend/CLAUDE.md` ("Granular trace events,
  cancellation, and run history", "Property Maintenance Inbox").
  A fourth **Alerts** tab (email Phase 3a) holds
  `components/NotificationsPanel.tsx` (the org's alert history, read-only by
  design — these are raised by the system, and a delete verb would only let
  someone erase the record of a fault they never fixed) and
  `components/WebhookSettings.tsx` (one optional webhook per org). The tab
  label carries the unread count, which `ActivityPage` keeps in its own state
  via the panel's `onUnreadChange` so the badge doesn't need a second fetch.
  `WebhookSettings` **omits `webhook_secret` from the payload entirely** when
  the field wasn't retyped — the API never returns it, so resending an empty
  string would wipe the stored one.
- **`/advanced`** — `pages/AdvancedPage.tsx`, raw-JSON CRUD over
  `/api/config/{workflows|skills|knowledge_bases|model-catalog}` plus a
  read-only `tools` tab — the operator-only "advanced view" for direct edits.
  Tabs run whole-then-parts (Workflows → Skills → Knowledge bases → Tools →
  Model catalog). Each `KINDS` entry carries an `orgScope` mirroring the
  backend: `required` (`?org=` or 422), `optional` (skills — omitted means the
  platform built-in tier), `none` (org-less). **A workflow is what the wizard
  and customer UI call an "AI team"**; this page uses the technical noun
  because it matches the JSON keys the operator is editing.
  Skill rows show their current immutable version; saving appends a version,
  while already-deployed teams keep their pinned version until redeployed.
- **`/wizard`** (+ `/wizard/:sessionId/{requirements|team|refine|test|deploy}`)
  — the six-stage Team Builder wizard, `components/WizardLayout.tsx` as the
  shared chrome:
  - `lib/api.ts` — shared `fetch` wrapper (`API_BASE`/`WS_BASE` default to
    `http://localhost:8000`) exposing every backend endpoint as `api.*`
    methods.
  - `lib/useBuilderSession.ts` / `lib/useModelCatalog.ts` — fetch-on-mount
    hooks; `WizardLayout` calls `useBuilderSession(sessionId)` once and hands
    `{session, setSession, loading, refresh, sessionId}` to the active stage
    page via `useOutletContext()`.
  - `components/WizardProgress.tsx` — the 6-step progress bar. A step is
    "unlocked" based on **data presence** (`session.requirements_json` /
    `session.specification_json`), not the session's `status` string, so
    revisiting earlier stages after a `solution`/`testing`/`deployed` status
    doesn't relock later steps.
  - `pages/wizard/*.tsx` — one page per stage (`IntentPage` has no
    `sessionId` yet and creates the session via `api.createSession()`;
    `RequirementsPage`/`TeamPage`/`RefinePage` each support both a "generate
    with `model` (+ optional `feedback`)" path and a "confirm/edit the
    drafted JSON directly" path via `BulletEditor`/raw field edits;
    `TestPage` runs `api.createTestRun()` then streams the same
    `/api/runs/{id}/stream` WebSocket as `MonitorPage`; `DeployPage` calls
    `api.deploySession()` and links to `/?workflow=<name>` for "Run a
    team").
  - `components/TeamFlow.tsx` + `EmployeeCard.tsx` — the customer-facing
    "meet your team" diagram: renders `Specification.teams`/`agents` as
    grouped "virtual employee" cards (avatar-initial + `display_name` +
    `friendly_description`, falling back to `name`/`role`/`goal`), laid out
    per `team.mode` (sequential = arrows between cards, parallel =
    side-by-side, hierarchical = manager card above member cards). No
    Mermaid — pure CSS/HTML, since the audience is non-technical.
  - All wizard pages/components share styles from
    `components/WizardLayout.css` (cards, fields, buttons, banners, bullet
    editor, team-flow/employee-card, activity feed).

## Anonymous team sharing (`/share/:token`)

The one **public, unauthenticated route** in this app (outside `RequireAuth`
entirely): `pages/ShareChatPage.tsx`, a multi-turn chat a colleague reaches
via a link an org member generated. Design:
`docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md`;
backend: `ui/backend/CLAUDE.md`.

- `lib/shareChatApi.ts` is a **separate client from `lib/api.ts`** and must
  stay one: it sends no bearer token and instead passes
  `credentials: 'include'` so the backend's signed `share_auth` session
  cookie round-trips. `lib/api.ts`'s `request` never needs cookies at all.
  Both share `API_BASE`/`WS_BASE`, which default to **`localhost`, not
  `127.0.0.1`** — the visitor cookie is `SameSite=Lax` and a browser treats
  those as different sites, so a mismatch with Vite's own `localhost:5173`
  silently breaks continuous chat entirely.
- `lib/shareTraceEvents.ts`'s `friendlyStatusFor` maps a run's event stream
  to one short non-technical line. It's cosmetic only — the backend already
  strips everything but the event `type` (plus the final answer) before it
  reaches this socket, so devtools show nothing more than the UI does.
- Org side: `components/ShareLinksPanel.tsx` is the click-to-expand "Share"
  panel on **My teams** (generate/copy/revoke links for one deployed team),
  and `components/SharedSessionsPanel.tsx` backs the **Shared** audit tab on
  `pages/ActivityPage.tsx` (pick a team, list its visitor sessions, read a
  session's transcript). The Shared tab's team picker lists only workflows
  with a real DB id — a YAML-only demo can't have share links.

## Auth and login UI

`lib/api.ts` stores a bearer token in `localStorage` (key `bestteam_token`),
attaches `Authorization: Bearer <token>` to every request, and on a `401`
(except from `/api/auth/*`, to avoid masking login errors) clears the token
and redirects to `/login`. `pages/LoginPage.tsx` is the username/password
form; `App.tsx`'s `RequireAuth` route guard redirects to `/login` when no
token is present, and `components/Layout.tsx` has a "Log out" button.

**Role-aware routing.** A platform operator (`is_admin`, `org_id IS NULL`) and
an org member see disjoint UIs, partitioned by two symmetric `App.tsx` guards
that both read `lib/useMe.ts` (one `GET /api/auth/me` → `{username, is_admin,
org}`) and render `null` while it loads:

- `RequireAdmin` wraps `/accounts` + `/advanced` + `/memory` + `/trace`; non-admins are sent to `/`.
- `RequireOrgMember` wraps the customer routes (`/`, `/run`, `/teams`, `/activity`, `/wizard/*`);
  operators are sent to `/advanced`, since every org-scoped surface 403s an
  org-less operator. The `*` catch-all stays **outside** both guards so an
  unknown path routes to `/`, where `RequireOrgMember` picks the destination.

Because `is_admin` and org membership are mutually exclusive (CR-030), the two
guards can't bounce a user between them — each redirect terminates in one hop.
`Layout.tsx` mirrors this: the **Accounts**/**Advanced**/**Memory**/**Trace**
links show only when `isAdmin`, the **Dashboard**/**Build a team**/
**My teams**/**Run a team** links only when `!isAdmin`. `pages/AccountsPage.tsx` is the admin org/user
manager (create orgs, deactivate/reactivate them, and create/reset-password/
move/delete each org's member; platform accounts are shown read-only — the
`/api/admin` surface keeps promote/demote and platform-account lifecycle in the
CLI). `pages/MemoryPage.tsx` is the admin per-user memory manager
(user list with counts + search/type-filter + per-record delete + clear-all,
and a "memory not enabled" state). All of this gating is cosmetic — the backend
enforces admin on every `/api/config` and `/api/memory` call and org scoping on
every customer surface, so a tampered client still gets 403.
`API_BASE`/`WS_BASE` are configurable via `VITE_API_BASE`/`VITE_WS_BASE`
(see `ui/frontend/.env.example`), falling back to `localhost:8000` for local
dev — `localhost`, not `127.0.0.1`, for the SameSite reason above.
