# bestteam UI — Manual Testing Guide

**App URL:** http://localhost:5173

## Prerequisites

- **Frontend** running: `cd ui/frontend && npm run dev`.
- **Backend** running **with the demo workflows enabled**, so the Monitor
  dropdown has something to pick on a fresh database:

  ```bash
  BESTTEAM_DEMO_WORKFLOWS=1 BESTTEAM_SECRET_KEY=dev-only-secret-change-me-for-real-use \
    python -m uvicorn ui.backend.main:app --port 8000
  ```

  `BESTTEAM_DEMO_WORKFLOWS` is off by default (the bundled workflows are demo
  fixtures, not tenant data); this guide needs them. See `docs/deployment.md`.

- **Two accounts.** There is **no public registration** — accounts are
  operator-provisioned. The two pages under test need different account types:

  | Account | Password | Type | Use for |
  |---|---|---|---|
  | `demo` | `demo-pass-123` | org user (`default` org) | Monitor (`/`), Wizard |
  | `op` | `op-pass-123` | platform admin (no org) | Advanced (`/advanced`), Memory |

  The Monitor page and Wizard are **org-user** surfaces — a platform admin
  (org-less) gets 403 there. The Advanced and Memory pages are **admin-only** —
  an org user is redirected to `/`. So switch account by page.

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
3. **Expected:** Logged in and redirected to `/` (Monitor page).

### T1-2 · Log out and log back in
1. Click **Log out** in the top-right nav.
2. **Expected:** Redirected to `/login`.
3. Log in again as `demo`.
4. **Expected:** Redirected to Monitor page.

### T1-3 · Wrong password
1. On the login page, enter `demo` with a wrong password.
2. **Expected:** An error banner appears — you stay on the login page.

### T1-4 · Auth guard
1. While logged out, navigate directly to http://localhost:5173/advanced.
2. **Expected:** Redirected to `/login`.

---

## T2 · Monitor Page (`/`) — as `demo`

### T2-1 · Workflow list loads
1. Log in as `demo` and go to `/` (Monitor).
2. **Expected:** The **Workflow** dropdown is populated (e.g. `code_review`,
   `research_brief`). No `workflows.map` error in the console.
   *If it's empty, the backend was started without `BESTTEAM_DEMO_WORKFLOWS=1`
   and this org has no deployed workflows yet.*

### T2-2 · Run a workflow
1. Select `code_review` from the dropdown.
2. Type a short input, e.g. `def add(a, b): return a + b`.
3. Click **Run**.
4. **Expected:**
   - The **Run** button changes to **Running…** and is disabled.
   - Events appear live in the **Live trace** section (`▶ started` … `● completed`).
   - After completion a **Final output** section appears with the response.

### T2-3 · Unreachable backend banner
1. Stop the backend (`Ctrl+C` in the backend terminal).
2. Refresh the Monitor page.
3. **Expected:** A red banner: `Can't reach the backend at http://127.0.0.1:8000...`.
4. Restart the backend and refresh — the banner should disappear.

---

## T3 · Advanced Page (`/advanced`) — as `op`

Log out and log in as `op` / `op-pass-123` (an org user is redirected away
from `/advanced`).

### T3-1 · Navigate between resource types
1. Go to `/advanced`.
2. Click each tab in the left nav: **Workflows**, **Skills**,
   **Knowledge bases**, **Tools**, **Model catalog**.
3. **Expected:** Each click reloads the list panel. Empty lists show `"None yet."`.
   There are no Agents/Teams tabs — a deployed workflow carries its agents and
   teams inline in its own JSON.

### T3-2 · Organization selector
1. On an org-scoped tab (Workflows / Knowledge bases), note the **Organization**
   selector in the page header; it lists the orgs (e.g. `Default Organization`).
2. On the **Skills** tab, the selector also offers **Platform (built-ins)** —
   the default — for the platform skill tier.
3. **Tools** and **Model catalog** show no selector (they aren't org-scoped).

### T3-3 · Switching org clears the editor (regression guard)
1. On the **Workflows** tab, select any existing workflow so its JSON loads in
   the editor.
2. Change the **Organization** selector to a different org (or, on **Skills**,
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

### T3-11 · Create a Workflow via Advanced
1. Click the **Workflows** tab; set the **Organization** selector to
   `Default Organization`.
2. In the **New name** input, type `test_workflow`. Click **New**, then enter:
   ```json
   {
     "name": "test_workflow",
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
     "workflow": { "steps": ["solo_team"] }
   }
   ```
3. Click **Save** → green `"Saved."` banner.
4. Log out, log in as `demo`, go to the Monitor page (`/`).
5. **Expected:** `test_workflow` appears in the Workflow dropdown (it was created
   in `default`, and `demo` belongs to `default`). Note this doesn't need the
   demo flag — it's a real DB workflow, not a bundled demo.

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
   `/wizard/<id>/preview` after ~30–60s.

### T4-3 · Preview page — Meet your team
1. **Expected:** A team diagram of agent cards (name, role, description).
2. Enter a test task and click **Run this through your team**.
3. **Expected:** An activity feed shows agents working, ending in a completed card.

### T4-4 · Confirm page — Apply feedback
1. From Preview, click **Continue** → Confirm shows the team diagram.
2. Type feedback, pick a model, click **Apply this change**.
3. **Expected:** The diagram refreshes; the feedback note appears under **Adjustments so far**.

### T4-5 · Deploy page — Launch the team
1. From Confirm, continue to deploy → **"Ready to go live?"** with the workflow name.
2. Click **Launch my team**.
3. **Expected:** `"Your team is live 🎉"` and a **Talk to your team** button.

### T4-6 · Talk to your team (post-deploy)
1. Click **Talk to your team**.
2. **Expected:** Redirected to Monitor with the new workflow pre-selected.
3. Enter an input and Run → output in the Live trace.

---

## T5 · Edge Cases & Error Handling

### T5-1 · 401 redirect on cleared token
1. DevTools → Application → Local Storage → delete `bestteam_token`.
2. Navigate to `/advanced`.
3. **Expected:** Redirected to `/login`.

### T5-2 · Duplicate workflow name is an upsert (as `op`)
1. In Advanced → Workflows (org `Default Organization`), create `duplicate_test`.
2. Create another with the same name.
3. **Expected:** The list has exactly one `duplicate_test` — the PUT upserts.

### T5-3 · Monitor with no input (as `demo`)
1. On Monitor, select a workflow but leave Input blank.
2. **Expected:** The **Run** button is disabled.

---

## Quick-reference: pages and routes

| URL | Page | Account | What to test |
|-----|------|---------|--------------|
| `/login` | Login | — | Auth, wrong password, redirect |
| `/` | Monitor | `demo` (org user) | Workflow list, run, live trace |
| `/advanced` | Advanced config | `op` (platform admin) | Workflows/Skills/KB/Tools/Model catalog CRUD, org selector |
| `/memory` | Memory admin | `op` (platform admin) | Per-user memory (if `BESTTEAM_MEMORY_DB` set) |
| `/wizard` | Team Builder | `demo` (org user) | Intent → Preview → Confirm → Deploy |
