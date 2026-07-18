# Self-service mailbox connection in the Team Builder wizard

**Status:** Approved design, ready for implementation planning.
**Date:** 2026-07-18

## Context

PR #17 shipped the foundation: encrypted per-org mailbox credentials
(`org_email_credentials` + `secret_store`), resolved at run time by
`email_tools.load_email_tools`, entered by the operator via `admin set-email`.
The remaining goal was **customer self-service** — a customer connects their
own inbox without the operator in the loop.

Two scope decisions from brainstorming reshaped the original "C+D" (per-org
admin role + settings UI):

1. **One user per org at this stage.** That single user *is* the org's admin,
   so there is no one to distinguish them from — **sub-project C (a per-org
   admin role) is dropped.** Access control is the existing `get_current_org`
   (resolves the requesting user's org; 403s platform operators). A role
   hierarchy is deferred until an org ever has a second user.
2. **Intent-driven, not a settings page.** Not every customer runs email teams,
   so the mailbox connection must not be a standing "Settings" page everyone
   sees. It surfaces **inside the Team Builder wizard, and only when the team
   being built actually uses the email tools.** This matches the platform
   philosophy (把复杂留给自己，把简单留给客户): the customer states an intent, and the
   mailbox ask appears as a natural step only when their team needs it.

Net: this sub-project is a **wizard-integrated, just-in-time mailbox
connection** over the PR #17 store — no new role, no standalone settings page.

## Trigger — detect email usage

After the Solution Architect generates the `Specification`, the team uses email
iff any agent's **resolved** tools include `email_find` / `email_read` /
`email_draft_reply` — resolved because the `email_triage_reply` built-in skill
pulls those tools in via `AgentSpec.skills`, not always via `AgentSpec.tools`
directly. A helper `spec_uses_email(db, spec, org_id)` gathers each agent's
`tools` plus the tools of each referenced skill (`load_skills(db, org_id)`) and
tests for intersection with the three email tool names. Computed server-side
and exposed to the frontend as an `uses_email` boolean on the builder session /
preview response, so the UI doesn't need to resolve skills itself.

## Placement — soft at Preview, hard gate at Deploy

- **Preview (soft):** when `uses_email`, render a "Connect the mailbox your team
  will work in" card. Connecting lets the Preview **test-run** exercise the
  customer's real inbox (drafts only, nothing sent — the moment the value is
  tangible). Skippable; if skipped, the test-run's email tools return the
  PR #17 "no mailbox connected" message. **No fake sample inbox is built.**
- **Deploy (hard gate):** `deploy_session` refuses an email-using team when the
  org has no connected mailbox — 400 with "connect a mailbox before going
  live". This is the guarantee; Preview is a convenience.

## Backend — org-scoped endpoints

New router `ui/backend/org_settings.py` at `/api/org`, every route guarded by
`Depends(get_current_org)` (the org's user manages only their own mailbox):

- `GET /api/org/email` → `{connected: bool, host, username, port, drafts}` —
  **never the password** (write-only; status only).
- `PUT /api/org/email` → set/rotate (`host`, `username`, `password`, optional
  `port`/`drafts`); encrypts + stores via PR #17 `set_email_credentials`.
- `POST /api/org/email/test` → attempt an IMAP login with the posted (unsaved)
  credentials and report success/failure; reuses the `admin set-email --test`
  path (`_ImapBackend._connect`). Does not save.
- `DELETE /api/org/email` → `clear_email_credentials`.

`builder.py::deploy_session` gains the `spec_uses_email` + connected-mailbox
gate. The builder session response gains the `uses_email` field.

### SSRF guard on the test/connect host

The test and set endpoints now connect to a **customer-supplied** host, so a
provisioned org user could point them at an internal address to probe the
network (a mild SSRF surface — an IMAP-over-TLS handshake). Reuse the
resolve-and-reject-private-IP logic in
`src/bestteam/tools/http_client.py::_check_host_allowed` — extract a
host+port variant (that function takes a URL) into a shared helper — and reject
hosts resolving to private/loopback/link-local/reserved addresses before
connecting, in both `PUT` (before store) and `POST /test`.

## Frontend

One reusable `ui/frontend/src/components/EmailConnect.jsx`: a form (IMAP host,
username, app-password, optional port/drafts) + **Test connection** button +
status (`connected as x@y · reconnect / disconnect`). `lib/api.js` gains
`getOrgEmail` / `setOrgEmail` / `testOrgEmail` / `clearOrgEmail`. Rendered on
`PreviewPage` (when `session.uses_email`, soft) and `DeployPage` (gate; block
the launch button until connected, with the reason shown). No new route, no nav
entry — it lives entirely within the wizard.

## Critical files

- Create: `ui/backend/org_settings.py`,
  `ui/frontend/src/components/EmailConnect.jsx`, tests.
- Modify: `ui/backend/main.py` (include the new router),
  `ui/backend/builder.py` (`spec_uses_email`, deploy gate, `uses_email` in the
  session response), `src/bestteam/tools/http_client.py` (extract the host
  SSRF check) or a small shared helper, `ui/frontend/src/lib/api.js`,
  `ui/frontend/src/pages/wizard/PreviewPage.jsx`,
  `ui/frontend/src/pages/wizard/DeployPage.jsx`, docs
  (`deployment.md` note that customers self-connect; `ui/backend/CLAUDE.md`).
- Reuse: `db/email_credentials.py` (set/get/clear from PR #17),
  `secret_store`, `get_current_org`, `_ImapBackend._connect` (test),
  `load_skills` (skill→tool resolution), `http_client._check_host_allowed`.

## Verification

- `spec_uses_email` true for a team with the `email_triage_reply` skill or a
  direct `email_*` tool, false otherwise.
- Endpoints: set stores encrypted (not plaintext) and never returns the
  password; test reports login success/failure without saving; delete removes;
  all 403 for a platform operator (no org) and for an unauthenticated caller.
- SSRF: a host resolving to a private/loopback address is rejected by both
  `PUT` and `POST /test`.
- Deploy gate: deploying an email-using team with no mailbox → 400; with a
  mailbox → 200. A non-email team deploys regardless.
- Cross-org: org A's user can neither read nor write org B's mailbox.
- Frontend: `EmailConnect` shows status, tests, connects, disconnects; Preview
  shows it only when `uses_email`; Deploy blocks launch until connected.
  `npm run lint` + `build` clean.

## Out of scope (later)

- Per-org **admin role** + multi-user orgs (revisit when an org gets a second
  user).
- Per-org LLM credentials; per-org Microsoft Graph / OAuth "connect your inbox";
  in-place key rekey; a general standing settings page.
