# bestteam UI — Manual Testing Guide

**App URL:** http://localhost:5173  
**Prerequisites:** Both backend (`uvicorn`) and frontend (`npm run dev`) must be running.

---

## T1 · Authentication

### T1-1 · Register a new account
1. Open http://localhost:5173 — you should be redirected to `/login`.
2. Click **"Don't have an account? Register"**.
3. Enter username `admin` and any password.
4. Click **Register**.
5. **Expected:** Logged in and redirected to `/` (Monitor page).

### T1-2 · Log out and log back in
1. Click **Log out** in the top-right nav.
2. **Expected:** Redirected to `/login`.
3. Enter the credentials from T1-1 and click **Login**.
4. **Expected:** Redirected to Monitor page.

### T1-3 · Wrong password
1. On the login page, enter the correct username but a wrong password.
2. **Expected:** An error banner appears — you stay on the login page.

### T1-4 · Auth guard
1. While logged out, navigate directly to http://localhost:5173/advanced.
2. **Expected:** Redirected to `/login`.

---

## T2 · Monitor Page (`/`)

### T2-1 · Workflow list loads
1. Log in and go to `/` (Monitor).
2. **Expected:** The **Workflow** dropdown is populated with workflow names (e.g. `code_review_team`). No `workflows.map` error in the browser console.

### T2-2 · Run a workflow
1. Select a workflow from the dropdown.
2. Type a short input, e.g. `Hello, please introduce yourself.`
3. Click **Run**.
4. **Expected:**
   - The **Run** button changes to **Running…** and is disabled.
   - Events appear live in the **Live trace** section (at minimum `▶ started` and `● completed`).
   - After completion a **Final output** section appears with the workflow's response.

### T2-3 · Unreachable backend banner
1. Stop the backend (`Ctrl+C` in the backend terminal).
2. Refresh the Monitor page.
3. **Expected:** A red banner: `Can't reach the backend at http://127.0.0.1:8000. Is uvicorn... running?`
4. Restart the backend and refresh — the banner should disappear.

---

## T3 · Advanced Page (`/advanced`)

### T3-1 · Navigate between resource types
1. Go to `/advanced`.
2. Click each tab in the left nav: **Agents**, **Teams**, **Knowledge bases**, **Workflows**, **Skills**, **Model catalog**.
3. **Expected:** Each click reloads the list panel. Empty lists show `"None yet."`.

### T3-2 · Create a Skill
1. Click the **Skills** tab.
2. In the **New name** input at the bottom of the list panel, type `email_reply`.
3. Click **New**.
4. **Expected:** The editor panel opens with `{ }` pre-filled and the heading `email_reply`.
5. Replace the JSON with:
   ```json
   {
     "instructions": "Always reply in a professional, concise tone. End with a polite sign-off.",
     "description": "Professional email reply style"
   }
   ```
6. Click **Save**.
7. **Expected:** A green `"Saved."` banner appears and `email_reply` appears in the list.

### T3-3 · Edit a Skill
1. Click `email_reply` in the Skills list.
2. Change `"description"` to `"Formal email writing style"`.
3. Click **Save**.
4. **Expected:** Green `"Saved."` banner.
5. Click away (e.g. switch to another tab and back), then click `email_reply` again.
6. **Expected:** The updated description is shown.

### T3-4 · Invalid JSON is rejected
1. Select `email_reply` in Skills.
2. Delete one `}` from the JSON so it's malformed.
3. Click **Save**.
4. **Expected:** A red `"Not valid JSON"` error banner. The item is NOT saved.

### T3-5 · Invalid Skill config is rejected
1. Select `email_reply` in Skills.
2. Replace the entire JSON with `{}` (no `instructions` field).
3. Click **Save**.
4. **Expected:** A red error banner from the backend (e.g. `"instructions"` is required). The item is NOT saved.

### T3-6 · Delete a Skill
1. Select `email_reply` in Skills.
2. Click **Delete**.
3. **Expected:** Editor panel clears, green `"Deleted."` banner, `email_reply` removed from the list.

### T3-7 · Create a Model Catalog entry
1. Click the **Model catalog** tab.
2. In the **New spec** input, type `fake:hello`.
3. Click **New**.
4. Enter this JSON:
   ```json
   {
     "display_name": "Test model",
     "provider": "fake",
     "tier": "economy",
     "input_cost_per_1m": 0,
     "output_cost_per_1m": 0
   }
   ```
5. Click **Save**.
6. **Expected:** Green `"Saved."` banner, `fake:hello` appears in the catalog list.

### T3-8 · Create a Workflow via Advanced
1. Click the **Workflows** tab.
2. In the **New name** input, type `test_workflow`.
3. Click **New**, then enter:
   ```json
   {
     "name": "test_workflow",
     "teams": [
       {
         "name": "solo_team",
         "mode": "sequential",
         "agents": ["helper"]
       }
     ],
     "agents": [
       {
         "name": "helper",
         "role": "Assistant",
         "goal": "Answer the user helpfully",
         "backstory": "You are a friendly AI assistant.",
         "model": "fake:I am your helper. How can I assist you today?"
       }
     ],
     "workflow": {
       "steps": ["solo_team"]
     }
   }
   ```
4. Click **Save**.
5. **Expected:** Green `"Saved."` banner.
6. Navigate to the Monitor page (`/`).
7. **Expected:** `test_workflow` appears in the Workflow dropdown.

---

## T4 · Team Builder Wizard (`/wizard`)

> **Note:** The wizard's AI generation steps (Intent → Preview) call a real language model. If you have no API key configured, these steps will fail with a `502` error. You can still test **T4-1** through **T4-4** navigation, and **T4-6** (deploy of a manually-configured workflow) without an API key.

### T4-1 · Empty intent is blocked
1. Go to `/wizard`.
2. Leave the "What do you want help with?" field empty.
3. **Expected:** The **Start building my team** button is disabled.

### T4-2 · Submit an intent (requires API key)
1. Go to `/wizard`.
2. Fill in:
   - **What do you want help with?** `We receive customer emails and need to reply quickly and professionally.`
   - **How do you handle this today?** (optional) `One person reads every email and replies manually.`
3. Click **Start building my team**.
4. **Expected:**
   - Button changes through: `"Setting things up…"` → `"Getting to know your business…"` → `"Putting your team together…"`
   - After ~30–60 seconds, you are navigated to `/wizard/<id>/preview`.

### T4-3 · Preview page — Meet your team
1. After T4-2 completes, you are on the Preview page.
2. **Expected:** A team diagram shows agent cards (name, role, description). Cards should reflect the customer email scenario.
3. Enter a test task: `A customer is upset that their order hasn't arrived after 2 weeks.`
4. Click **Run this through your team**.
5. **Expected:** An activity feed appears showing agents working through the task, ending with a completed card.

### T4-4 · Confirm page — Apply feedback
1. From Preview, click **Continue**.
2. **Expected:** Confirm page shows the team diagram again.
3. Type feedback: `Add an agent that checks our refund policy before replying.`
4. Select a model from the **Which assistant should make this change?** picker.
5. Click **Apply this change**.
6. **Expected:** `"Updating…"` while loading, then the team diagram refreshes with the updated design. The feedback note appears under **Adjustments so far**.

### T4-5 · Confirm page — View requirements
1. On the Confirm page, click **Show what we understood about your business**.
2. **Expected:** The Requirements panel expands, showing Summary, Pain points, Goals, etc.
3. Edit the Summary field text.
4. Click **Save changes**.
5. **Expected:** Changes saved without error.

### T4-6 · Deploy page — Launch the team
1. From Confirm, click **Continue to deploy**.
2. **Expected:** Deploy page shows `"Ready to go live?"` with the workflow name.
3. Click **Launch my team**.
4. **Expected:**
   - Button shows `"Launching…"` then the page shows `"Your team is live 🎉"` with a green success banner.
   - A **Talk to your team** button appears.

### T4-7 · Talk to your team (post-deploy)
1. On the Deploy success screen, click **Talk to your team**.
2. **Expected:** Redirected to Monitor page (`/`) with the newly deployed workflow pre-selected in the dropdown.
3. Enter an input and click **Run**.
4. **Expected:** The workflow runs and produces output in the Live trace.

---

## T5 · Edge Cases & Error Handling

### T5-1 · 401 redirect on expired token
1. Open browser DevTools → Application → Local Storage.
2. Delete the `bestteam_token` entry.
3. Navigate to `/advanced` without refreshing.
4. **Expected:** Automatically redirected to `/login`.

### T5-2 · Duplicate workflow name rejected
1. In Advanced → Workflows, create a workflow named `duplicate_test` (any valid JSON).
2. Try to create another one with the same name.
3. **Expected:** Backend returns an error; red banner displayed in editor. (Or the PUT upserts it — verify the list only has one `duplicate_test` entry.)

### T5-3 · Monitor with no input
1. On the Monitor page, select a workflow but leave the Input field blank.
2. **Expected:** The **Run** button is disabled.

---

## Quick-reference: pages and routes

| URL | Page | What to test |
|-----|------|--------------|
| `/login` | Login / Register | Auth, wrong password, redirect |
| `/` | Monitor | Workflow list, run, live trace |
| `/advanced` | Advanced config | All 6 resource types: CRUD |
| `/wizard` | Team Builder — Intent | Describe a challenge |
| `/wizard/:id/preview` | Team Builder — Preview | Team diagram, test run |
| `/wizard/:id/confirm` | Team Builder — Confirm | Feedback, requirements |
| `/wizard/:id/deploy` | Team Builder — Deploy | Launch, redirect to Monitor |
