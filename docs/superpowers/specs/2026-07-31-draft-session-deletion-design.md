# Deleting a never-deployed draft team from My Teams — design

## Problem

The My Teams page (`ui/frontend/src/pages/wizard/SessionsPage.jsx`) lists
every `builder_sessions` row a customer has reached the Specification stage
on or further (`spec` / `solution` / `testing` / `deployed`), now grouped by
status with counts (2026-07-31 change). There is no way to remove one. A
customer who abandons a draft mid-wizard — tries a design, doesn't like it,
starts over — leaves that session sitting in its status section forever,
and its on-disk workspace directory (`ui/backend/data/builder_sessions/<id>/`)
accumulates with it. Grouping by status makes this more visible, not less:
a customer now sees an ever-growing "Spec (7)" pile with no way to clear it.

## Goal

Let a customer delete, from My Teams, a session that was **never deployed**
— i.e. has no live team depending on it. Self-service, no admin involved.

## Scope

**In scope:** deleting a `builder_sessions` row (any of `spec`/`solution`/
`testing` — the statuses visible on My Teams) whose `workflow_id` is `NULL`,
plus its on-disk workspace directory.

**Out of scope (deferred to a follow-up sub-project on deployed-team
lifecycle):**
- Deactivating or deleting a **deployed** team (`WorkflowRecord`). A hard
  delete already exists (`DELETE /api/config/workflows/{name}`) but is
  admin-only and refuses if the team was ever run — no self-service path,
  and no "pause without losing config" concept exists at the per-team level
  (only at the org level, `organizations.active`).
- "Discard changes" on a session that's re-editing an already-deployed team
  (status regressed to `spec`/`solution`/`testing` but `workflow_id` is
  still set — see "Why `workflow_id`, not `status`" below). Doing this well
  needs the deployed version's *friendly* fields (`display_name`/
  `friendly_description`), which today aren't stored anywhere —
  `workflow_versions.config` is `Specification.to_raw()`, which strips them.
  Reverting a session to that shape would regress the team card back to no
  description. That's either a schema change (store the full friendly spec
  at deploy time) or an accepted lossy revert — a real decision, not one to
  fold into this smaller change. Until sub-project 2 designs it, a
  `workflow_id`-linked session simply gets no delete affordance at all.
- Anything touching automation triggers or run history — neither can exist
  for a session that was never deployed.

### Why `workflow_id`, not `status`

A session's `status` can move backward: editing an already-deployed team
(going back into Confirm to tweak the design) resets that same session's
`status` to `spec`/`solution`/`testing` again, while `workflow_id` stays
pointed at the still-live `WorkflowRecord` (`builder.py::deploy_session`
sets `status="deployed"` and `workflow_id` together; earlier stage endpoints
reset `status` without touching `workflow_id`). So `status != "deployed"`
does **not** mean "never deployed" — `workflow_id IS NULL` does. That's the
only safe guard: it can never be true for a session that has ever gone live.

## Backend

### Data
No schema change. `builder_sessions.workflow_id` already exists
(nullable FK) — it's just never been exposed to the frontend.

### API

`DELETE /api/builder/sessions/{id}` in `ui/backend/builder.py`:

- Fetch via the existing `_get_session_or_404(db, session_id, org.id)` —
  unknown or another org's session is a `404`, same as every other session
  route (no existence oracle).
- If `session.workflow_id is not None`: `409` — `"This team is live — it
  can't be deleted from here yet."` (defense in depth; the frontend won't
  offer the button for this case, but the API must not trust that alone).
- Otherwise: delete the row, commit, then best-effort
  `shutil.rmtree(_SESSIONS_DIR / session_id, ignore_errors=False)` wrapped
  in `try/except` that logs a warning on failure rather than raising —
  mirrors the KB-delete ordering already in `crud.py` (commit the DB change
  first, then clean up the filesystem, so a filesystem failure can't leave
  a rolled-back row pointing at deleted files, and a filesystem failure
  can't leave an orphaned-but-undeleted DB row either since the commit
  already happened).
- Response: `204` on success.

`_session_to_dict` (`builder.py`) gains one field: `"workflow_id":
session.workflow_id`. Plain nullable-int exposure, consistent with how
session/org ids are already returned elsewhere in this API.

`ui/frontend/src/lib/api.js` gains `deleteSession: (id) =>
request(\`/api/builder/sessions/${id}\`, { method: 'DELETE' })`.

## Frontend

`SessionsPage.jsx`:

- Each card's footer gets a "Delete" button, rendered **only** when
  `session.workflow_id == null`.
- `onClick`: `event.stopPropagation()` (the card itself is a `<button>`
  that navigates to resume — this must not trigger that), then
  `window.confirm('Delete "<team name or intent text>"? This can't be
  undone.')` — matches the existing confirm pattern used for every other
  destructive action in this app (`AccountsPage`, `AdvancedPage`,
  `MemoryPage` all use bare `window.confirm`).
- On confirm: `api.deleteSession(session.id)`, then remove it from local
  `sessions` state on success (no full refetch) — its status section's
  count decrements, and the section itself disappears if it was the last
  session in that group (already true of the existing `statusGroups`
  filter, no new logic needed there).
- On failure: show the error in the page's existing `error` /
  `banner banner-error` slot; the card stays in the list untouched. Covers
  both a generic failure and the race where the session was deployed in
  another tab between page load and the click (backend now returns `409`
  with a customer-readable message in that case, shown as-is).

## Testing plan

Backend (new tests alongside the existing builder-session route tests):
- Deleting a `workflow_id IS NULL` session succeeds (`204`); it's gone from
  a subsequent `GET /api/builder/sessions`; its workspace directory no
  longer exists.
- Deleting a session with `workflow_id` set returns `409`; the row and its
  workspace directory are untouched.
- Deleting an unknown id, or another org's session, returns `404`.

Frontend (extend `SessionsPage.test.jsx`):
- Delete button renders for a `workflow_id: null` session, not for one with
  `workflow_id` set.
- Clicking Delete without confirming (`window.confirm` mocked to return
  `false`) leaves the session in the list and never calls the API.
- Confirming calls `api.deleteSession` and removes the card without a
  refetch.
- A `409`/error response surfaces the banner and keeps the card.
