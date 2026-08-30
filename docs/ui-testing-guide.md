# bestteam UI — Manual Testing Guide

**App URL:** http://localhost:5173

## Prerequisites

- **Frontend** running: `cd ui/frontend && npm run dev`.
- **Backend** running **with the demo pipelines enabled**, so the "Run a team"
  dropdown has something to pick on a fresh database:

  ```bash
  BESTTEAM_DEMO_PIPELINES=1 BESTTEAM_SECRET_KEY=dev-only-secret-change-me-for-real-use \
    python -m uvicorn ui.backend.main:app --port 8000
  ```

  `BESTTEAM_DEMO_PIPELINES` is off by default (the bundled pipelines are demo
  fixtures, not tenant data); this guide needs them. See `docs/deployment.md`.

- **Two accounts.** There is **no public registration** — accounts are
  operator-provisioned. The customer and admin surfaces need different account
  types, so you will switch between these two throughout:

  | Account | Password | Type | Use for |
  |---|---|---|---|
  | `demo` | `demo-pass-123` | org user (`default` org) | Dashboard, Build a team, My teams, Run a team |
  | `op` | `op-pass-123` | platform admin (no org) | Accounts, Advanced, Memory, Trace |

  The two account types see **disjoint navs**. The customer pages are
  **org-user** surfaces — a platform admin (org-less) is redirected to
  `/advanced`. The admin pages are **admin-only** — an org user is redirected
  to `/`. So switch account by page.

  Note that `/` is **not** a page: it is a router that sends an org user to
  `/activity` (the Dashboard), or to `/wizard` if their org has nothing
  deployed yet, and an admin to `/advanced`. "Run a team" lives at `/run`.

  Create them if missing (they exist in the dev DB already):

  ```bash
  python -m ui.backend.admin create-user demo --org default   # prompts for password
  python -m ui.backend.admin create-user op --platform
  python -m ui.backend.admin promote op
  ```

---

## T1 · Authentication

### T1-1 · Log in
1. Open http://localhost:5173 — you should be redirected to `/login`.
2. Enter `demo` / `demo-pass-123` and click **Login**.
3. **Expected:** Logged in, and `/` forwards you to `/activity` ("Team
   activity") — or to `/wizard` if `default` has no deployed pipeline yet.
   The nav shows **Dashboard / Build a team / My teams / Run a team**.

### T1-2 · An admin lands somewhere else
1. Log out, then log in as `op` / `op-pass-123`.
2. **Expected:** You land on `/advanced`, and the nav shows
   **Accounts / Advanced / Memory / Trace** — no customer links.
3. Navigate to `/run`.
4. **Expected:** Bounced back to `/advanced` (an org-less admin has no org
   to run anything in).

### T1-2a · Log out and log back in
1. Click **Log out** in the top-right nav.
2. **Expected:** Redirected to `/login`.
3. Log in again as `demo`.
4. **Expected:** The same landing destination as T1-1.

### T1-3 · Wrong password
1. On the login page, enter `demo` with a wrong password.
2. **Expected:** An error banner appears — you stay on the login page.

### T1-4 · Auth guard
1. While logged out, navigate directly to http://localhost:5173/advanced.
2. **Expected:** Redirected to `/login`.

---

## T2 · Run a team (`/run`) — as `demo`

### T2-1 · Team list loads
1. Log in as `demo` and click **Run a team** in the nav (`/run`).
2. **Expected:** The **Team** dropdown is populated (e.g. `code_review`,
   `research_brief`). No list-rendering error in the console.
   *If it's empty, the backend was started without `BESTTEAM_DEMO_PIPELINES=1`
   and this org has no deployed pipelines yet.*

### T2-2 · Run a team
1. Select `code_review` from the dropdown.
2. Type a short input, e.g. `def add(a, b): return a + b`.
3. Click **Run**.
4. **Expected:**
   - The **Run** button changes to **Running…** and is disabled.
   - Events appear live in the **Live trace** section (`▶ started` … `● completed`).
   - After completion a **Final output** section appears with the response.

### T2-3 · Unreachable backend banner
1. Stop the backend (`Ctrl+C` in the backend terminal).
2. Refresh `/run`.
3. **Expected:** A red banner reading *"Can't reach the backend at
   http://localhost:8000. Is `uvicorn ui.backend.main:app` running?"* — the
   host is whatever `VITE_API_BASE` is set to, defaulting to `localhost`,
   **not** `127.0.0.1` (the share-chat cookie is `SameSite=Lax` and browsers
   treat those as different sites).
4. Restart the backend and refresh — the banner should disappear.

---

## T3 · Advanced Page (`/advanced`) — as `op`

Log out and log in as `op` / `op-pass-123` (an org user is redirected away
from `/advanced`).

### T3-1 · Navigate between resource types
1. Go to `/advanced`.
2. Click each tab in the left nav: **Pipelines**, **Skills**,
   **Knowledge bases**, **Tools**, **Model catalog**.
3. **Expected:** Each click reloads the list panel. Empty lists show `"None yet."`.
   There are no Agents/Teams tabs — a deployed pipeline carries its agents and
   teams inline in its own JSON.

### T3-2 · Organisation selector
1. On an org-scoped tab (Pipelines / Knowledge bases), note the **Organisation**
   selector in the page header; it lists the orgs (e.g. `Default Organization`).
2. On the **Skills** tab, the selector also offers **Platform (built-ins)** —
   the default — for the platform skill tier.
3. **Tools** and **Model catalog** show no selector (they aren't org-scoped).

### T3-3 · Switching org clears the editor (regression guard)
1. On the **Pipelines** tab, select any existing pipeline so its JSON loads in
   the editor.
2. Change the **Organisation** selector to a different org (or, on **Skills**,
   between an org and **Platform (built-ins)**).
3. **Expected:** The editor **clears** back to "Select an item…" — the previous
   org's item and JSON must not linger. (Otherwise a Save would write it into
   the newly selected org.)

### T3-4 · Create a Skill
1. Click the **Skills** tab (leave the selector on **Platform (built-ins)**).
2. In the **New name** input at the bottom of the list panel, type `email_reply`.
3. Click **New**.
4. **Expected:** The editor opens with `{ }` pre-filled and the heading `email_reply`.
5. Replace the JSON with:
   ```json
   {
     "instructions": "Always reply in a professional, concise tone. End with a polite sign-off.",
     "description": "Professional email reply style"
   }
   ```
6. Click **Save**.
7. **Expected:** A green `"Saved."` banner appears and `email_reply` appears in the list.

### T3-5 · Edit a Skill
1. Click `email_reply` in the Skills list.
2. Change `"description"` to `"Formal email writing style"`.
3. Click **Save** → green `"Saved."` banner.
4. Switch to another tab and back, then click `email_reply` again.
5. **Expected:** The updated description is shown.

### T3-6 · Invalid JSON is rejected (client-side)
1. Select `email_reply` in Skills.
2. Delete one `}` so it's malformed. Click **Save**.
3. **Expected:** A red `"Not valid JSON"` banner. Nothing is saved.

### T3-7 · Invalid Skill config is rejected (backend)
1. Select `email_reply`. Replace the whole JSON with `{}` (no `instructions`).
2. Click **Save**.
3. **Expected:** A red backend error banner mentioning `instructions`. Nothing is saved.

### T3-8 · Delete a Skill
1. Restore valid JSON and Save, then click **Delete**.
2. **Expected:** The editor clears and `email_reply` disappears from the list.

### T3-9 · Read-only Tools tab
1. Click the **Tools** tab, then a tool (e.g. `web_search`).
2. **Expected:** Its description shows as read-only text — no editor textarea,
   no Save/Delete, no "New" row.

### T3-10 · Create a Model Catalog entry
1. Click the **Model catalog** tab.
2. In the **New spec** input, type `fake:hello`. Click **New**.
3. Enter this JSON:
   ```json
   {
     "display_name": "Test model",
     "description": "Smoke-test entry",
     "tier": "economy",
     "input_price_per_1k": 0,
     "output_price_per_1k": 0
   }
   ```
4. Click **Save**.
5. **Expected:** Green `"Saved."` banner; `fake:hello` appears in the list.

### T3-11 · Create a Pipeline via Advanced
1. Click the **Pipelines** tab; set the **Organisation** selector to
   `Default Organization`.
2. In the **New name** input, type `test_pipeline`. Click **New**, then enter:
   ```json
   {
     "name": "test_pipeline",
     "teams": [{ "name": "solo_team", "mode": "sequential", "agents": ["helper"] }],
     "agents": [
       {
         "name": "helper",
         "role": "Assistant",
         "goal": "Answer the user helpfully",
         "backstory": "You are a friendly AI assistant.",
         "model": "fake:I am your helper. How can I assist you today?"
       }
     ],
     "pipeline": { "steps": ["solo_team"] }
   }
   ```
3. Click **Save** → green `"Saved."` banner.
4. Log out, log in as `demo`, go to **Run a team** (`/run`).
5. **Expected:** `test_pipeline` appears in the Team dropdown (it was created
   in `default`, and `demo` belongs to `default`). Note this doesn't need the
   demo flag — it's a real DB pipeline, not a bundled demo.

---

## T4 · Team Builder Wizard (`/wizard`) — as `demo`

> **Note:** The wizard's AI generation steps call a real language model. With no
> API key configured they fail with `502`. You can still test T4-1 navigation
> without a key. Run these as `demo` (the wizard is an org-user surface).

### T4-1 · Empty intent is blocked
1. Go to `/wizard`.
2. Leave the challenge field empty.
3. **Expected:** The start button is disabled.

### T4-2 · Submit an intent (requires API key)
1. Fill in the challenge and (optionally) the "how do you handle this today?" field.
2. Click **Start building my team**.
3. **Expected:** The button steps through its loading labels, then navigates to
   `/wizard/<id>/documents` — step 2 of 5, **"Add your documents"**.

### T4-2a · Documents step is skippable
1. On **Add your documents**, click **Skip for now**.
2. **Expected:** You reach `/wizard/<id>/preview` without uploading anything —
   documents are optional.
3. To test the upload path instead, name a knowledge base (e.g.
   `Product policies`), attach a file, and click **Continue**.
4. **Expected:** The button shows ingestion progress labels while the upload
   job is polled, then navigates to Preview once it completes.

### T4-3 · Preview page — Meet your team
1. **Expected:** A team diagram of agent cards (name, role, description).
2. Enter a test task and click **Run this through your team**.
3. **Expected:** An activity feed shows agents working, ending in a completed card.

### T4-4 · Confirm page — Apply feedback
1. From Preview, click **Continue** → Confirm shows the team diagram.
2. Type feedback, pick a model, click **Apply this change**.
3. **Expected:** The diagram refreshes; the feedback note appears under **Adjustments so far**.

### T4-5 · Deploy page — Launch the team
1. From Confirm, continue to deploy → **"Ready to go live?"** with the team name.
2. Click **Launch my team**.
3. **Expected:** `"Your team is live 🎉"` and a **Run a team** button.

### T4-6 · Run a team (post-deploy)
1. Click **Run a team**.
2. **Expected:** Redirected to `/run?pipeline=<name>` with the new team
   pre-selected.
3. Enter an input and Run → output in the Live trace.

### T4-7 · Wizard progress bar shows five steps
1. On any wizard page, look at the progress bar.
2. **Expected:** **Your challenge → Your documents → Meet your team →
   Confirm → Go live**. Steps unlock on data presence, so revisiting an
   earlier step after deploying must not re-lock the later ones.

---

## T5 · Dashboard (`/activity`) — as `demo`

### T5-1 · The three tabs load
1. Click **Dashboard** in the nav.
2. **Expected:** Heading **"Team activity"** with tabs **Automations**,
   **Runs**, **Shared**.

### T5-2 · Runs tab lists history and opens a run
1. Run something first (T2-2), then open the **Runs** tab.
2. **Expected:** The run is listed. Filters by team / manual-or-automatic /
   status narrow the list.
3. Click the run.
4. **Expected:** A detail panel shows its trace. A finished run fetches its
   persisted trace once; a still-`running` one streams live over the
   WebSocket.

### T5-3 · Run history survives a backend restart
1. With at least one finished run listed, restart the backend.
2. Reload the **Runs** tab.
3. **Expected:** The finished run is still listed with its trace — history is
   persisted per run. (In-flight/live run state is *not* rehydrated; that is a
   known limitation, not a bug to file.)

---

## T6 · My teams (`/teams`) — as `demo`

### T6-1 · Deployed teams are listed
1. Click **My teams** in the nav.
2. **Expected:** Heading **"My teams"**; a team deployed in T4-5 appears under
   the **Live** status.

### T6-2 · Generate and revoke a share link
1. On a deployed team, expand the **Share** panel.
2. Click **Generate a new link**, then **Copy link**.
3. Open the copied `/share/<token>` URL in a **private/incognito window**
   (it is the one public, unauthenticated route — it must work with no login).
4. **Expected:** A chat page for that team; a message gets a reply.
5. Back in the normal window, click **Revoke** on the link, then reload the
   share URL.
6. **Expected:** The revoked link no longer works.

### T6-3 · Shared sessions are auditable
1. Go to **Dashboard** → **Shared**, pick the team from T6-2.
2. **Expected:** The visitor session from step 3 above is listed, and opening
   it shows that conversation's transcript.

---

## T7 · Admin pages — as `op`

### T7-1 · Accounts page
1. Go to `/accounts`.
2. **Expected:** Heading **"Organisations & users"**; the orgs are listed with
   an Active/Deactivated state, a **Deactivate**/**Reactivate** button, and
   per-org user actions (create / reset password / move / delete). Platform
   accounts are listed **read-only** — `promote`/`demote` stay CLI-only
   (see `docs/ADMIN_GUIDE.md`).

### T7-2 · Trace page
1. Go to `/trace`.
2. **Expected:** Heading **"Trace"** with tabs **Runs**, **Analytics**,
   **Models**. The Runs tab filters by pipeline; clicking a run opens its
   detail.

### T7-3 · Memory page
1. Go to `/memory`.
2. **Expected:** With `BESTTEAM_MEMORY_DB` unset, a clear "memory not enabled"
   state — not an error. With it set, a user list with record counts,
   search/type filters, per-record delete and clear-all.

---

## T8 · Edge Cases & Error Handling

### T8-1 · 401 redirect on cleared token
1. DevTools → Application → Local Storage → delete `bestteam_token`.
2. Navigate to `/advanced`.
3. **Expected:** Redirected to `/login`.

### T8-2 · Duplicate pipeline name is an upsert (as `op`)
1. In Advanced → Pipelines (org `Default Organization`), create `duplicate_test`.
2. Create another with the same name.
3. **Expected:** The list has exactly one `duplicate_test` — the PUT upserts.

### T8-3 · Run with no input (as `demo`)
1. On `/run`, select a team but leave Input blank.
2. **Expected:** The **Run** button is disabled.

### T8-4 · Unknown path routes home
1. Navigate to `/no-such-page`.
2. **Expected:** You end up on the right home for your account — Dashboard (or
   the wizard) as `demo`, Advanced as `op` — not a dead end.

---

## Quick-reference: pages and routes

| URL | Page | Account | What to test |
|-----|------|---------|--------------|
| `/login` | Login | — | Auth, wrong password, redirect |
| `/` | *(router, not a page)* | either | Forwards to `/activity` or `/wizard` (org user), `/advanced` (admin) |
| `/activity` | Dashboard — "Team activity" | `demo` (org user) | Automations / Runs / Shared tabs, run detail, persisted history |
| `/wizard` | Team Builder | `demo` (org user) | Challenge → Documents → Preview → Confirm → Deploy |
| `/teams` | My teams | `demo` (org user) | Deployed teams, share links, resume a draft |
| `/run` | Run a team | `demo` (org user) | Team list, run, live trace, Stop |
| `/share/:token` | Public share chat | **none — logged out** | Anonymous multi-turn chat; revoked link is refused |
| `/accounts` | Organisations & users | `op` (platform admin) | Org create/deactivate, per-org user management |
| `/advanced` | Advanced config | `op` (platform admin) | Pipelines/Skills/KB/Tools/Model catalog CRUD, org selector |
| `/memory` | Memory admin | `op` (platform admin) | Per-user memory (if `BESTTEAM_MEMORY_DB` set) |
| `/trace` | Trace | `op` (platform admin) | Runs / Analytics / Models tabs |
