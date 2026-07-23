# Design: Autonomous email-triggered runs (new-mail polling)

## Context

Today every run is started by a human: the wizard's test run, or "Talk to your
team" on the dashboard. The business target is **ambient autonomy**: a customer
deploys an email team, and it reads new mail and drafts replies on its own —
no prompt, no click. The code has always named this as planned-but-missing
("ambient run-on-new-mail triggering", `src/bestteam/tools/CLAUDE.md`).

Everything this feature needs already exists: per-org encrypted mailbox
credentials (PR #17), customer self-service mailbox connection (PR #18/#21),
draft-only IMAP tools with SSRF/TLS hardening, `run_in_background(org_id=)`,
and a persisted `runs` row per run (CR-012). The missing piece is one
subsystem: **detect new mail per org and start that org's deployed email team.**

Decisions locked with the user (2026-07-19):

- **Email-only MVP** — no generic trigger framework, no timer schedules (YAGNI;
  grow a framework later if a second trigger type is ever needed).
- **Polling**, not IMAP IDLE or provider webhooks — simplest, reuses the
  existing IMAP backend, no long-lived sockets; minutes of latency is fine for
  email triage.
- **Customer opt-in in the wizard** at the Deploy step, off by default,
  toggleable later — matches the self-service philosophy.
- **Daily run cap per org** — bounds worst-case autonomous spend; paused until
  midnight when hit.
- **Activity list on the team page** — lightweight history read from the
  already-persisted `runs` rows, so silent failure is visible. Full Phase-5
  trace persistence stays out of scope.
- **In-process poller** — an asyncio background task in the existing ASGI
  lifespan, not a separate worker process or external cron.

## Architecture

One new subsystem (`ui/backend/email_trigger.py`) with three parts:

1. **Trigger store** — a new `email_triggers` table holding per-org opt-in and
   poller state.
2. **Poll loop** — an asyncio task started from `main.py`'s `_lifespan`,
   checking each enabled org's mailbox every `BESTTEAM_TRIGGER_POLL_SECONDS`
   (default 120).
3. **Org API + wizard/teams UI** — `/api/org/email-trigger` endpoints and the
   customer-facing toggle + activity list.

### 1. Data model

New table `email_triggers` (Alembic migration, guarded/idempotent in the
project style; SQLAlchemy model in `ui/backend/db/models.py`, CRUD in
`ui/backend/db/email_triggers.py` mirroring `db/email_credentials.py`):

- `org_id` — unique FK; **at most one auto-running team per org**, mirroring
  one-mailbox-per-org and preventing two teams fighting over one inbox.
- `workflow_name` — the deployed team to run.
- `enabled` — the customer's opt-in flag.
- `last_uid`, `uidvalidity` — dedup baseline (see §2).
- `runs_today`, `runs_date` — daily-cap state.
- `last_checked_at`, `last_error` — health surfaced to the UI.

### 2. Detection & dedup (UID baseline)

Per enabled org per cycle: connect with the org's stored credentials — the
existing `_ImapBackend` with `restrict_to_public=True`, run via
`asyncio.to_thread` (imaplib is synchronous) — select INBOX read-only, and
UID-search for messages **above `last_uid`**.

- **UIDs, not UNSEEN, are the dedup key.** The toolkit deliberately never
  marks mail seen (`BODY.PEEK`), so UNSEEN would re-trigger on the same
  messages forever.
- **First enable:** baseline `last_uid` = the mailbox's current max UID.
  Only mail arriving *after* enabling triggers runs — never a storm through
  the existing backlog.
- **`UIDVALIDITY` change** (mailbox rebuilt/migrated): re-baseline to the new
  current max and store the new validity; do not reprocess.
- New UIDs found → advance `last_uid` **before** starting the run (a crashed
  run must not cause re-triggering; the activity list shows the failure
  instead).

### 3. Triggering a run

- **One run per poll cycle**, covering all new messages found in that cycle —
  a burst of 5 emails is one triage run, not 5 (kinder to the cap; one draft
  pass). Not one-run-per-message; not a redesign of the triage skill.
- Run input names the specific new message IDs, e.g. `3 new emails arrived
  (ids: 101, 102, 103). Read each one and triage it, drafting replies where
  needed.` The agent `email_read`s exactly those IDs — no skill changes, and
  older unseen mail doesn't get duplicate drafts.
- Started through the existing `run_in_background(org_id=...)` path (same
  workflow cache, same per-org email tools, same usage metering), attributed
  to the sentinel username **`email-trigger`** in the persisted `runs` row so
  autonomous runs are distinguishable from human ones.
- **Overlap guard:** if the org's previous triggered run is still executing,
  skip this cycle (UIDs simply accumulate for the next one).

### 4. Safety

- **Daily cap:** `BESTTEAM_TRIGGER_DAILY_CAP` (default **50** runs/org/day).
  At the cap, that org's polling pauses until `runs_date` rolls over;
  status shows "paused — daily limit reached". Checked before connecting
  (cheap skip).
- **Operator kill switch:** `BESTTEAM_TRIGGERS_DISABLED=1` stops all polling
  platform-wide, instantly, without touching per-org rows.
- **Resilience:** each org's poll is wrapped in try/except — an IMAP failure
  logs a warning, stores `last_error` (+`last_checked_at`) for the UI, and
  retries next cycle. The loop itself never dies and never auto-disables a
  trigger (a mail-server blip must not kill autonomy).
- **Hard floor:** the email toolkit remains draft-only — the worst autonomous
  outcome is a bad draft a human reviews in their own mail client.

### 5. API & UI

Org-scoped endpoints on the existing `/api/org` router (`get_current_org`
guard — platform operators 403, cross-org isolation for free):

- `GET /api/org/email-trigger` → `{enabled, workflow_name, status
  (active|paused_cap|error|disabled), runs_today, daily_cap, last_checked_at,
  last_error}`.
- `PUT /api/org/email-trigger` `{workflow_name, enabled}` — enabling
  validates: the workflow exists for this org, is deployed, uses email
  (`spec_uses_email`), and a mailbox is connected. Enabling (re)sets the UID
  baseline (§2).
- `GET /api/org/email-trigger/activity` → recent runs for the org from the
  persisted `runs` table (newest first, capped at 50): started-at, status,
  and whether it was autonomous (username == `email-trigger`).

Frontend:

- **Wizard Deploy page:** after the mailbox is connected, an off-by-default
  toggle — "**Run automatically when new email arrives**" — with one line of
  copy explaining the daily cap in plain language.
- **Teams page:** trigger status chip (active / paused — daily limit /
  error / off), last-checked time, and the activity list.

### 6. Configuration

- `BESTTEAM_TRIGGER_POLL_SECONDS` — default 120.
- `BESTTEAM_TRIGGER_DAILY_CAP` — default 50.
- `BESTTEAM_TRIGGERS_DISABLED` — operator kill switch, default unset.
- Documented in `.env.example` + `docs/deployment.md`.

## Error handling summary

| Failure | Behavior |
|---|---|
| Mailbox unreachable / login fails during poll | Log warning, store `last_error`, retry next cycle; never auto-disable |
| Triggered run crashes | `runs` row records failure (existing behavior); UIDs already advanced, no re-trigger loop; visible in activity list |
| Daily cap reached | Pause that org until date rollover; visible status |
| Backend restart | Poller restarts with the app; UID baseline + cap state persist in DB; mail that arrived while down is caught by the next poll |
| Secrets key missing/rotated | Existing startup guard already refuses to serve; poller never runs with undecryptable credentials |
| Previous triggered run still running | Skip cycle; new UIDs accumulate |

## Known limitations (accepted for MVP)

- **Single-process poller.** Fine for the current one-uvicorn deployment; if
  the backend ever scales to multiple workers, the poller needs a leader
  lock. Noted, not built.
- **Poll latency** (minutes, tunable) — inherent to the chosen approach.
- **`last_uid` advance-before-run** means a crashed run's messages are not
  retried automatically; the customer sees the failed run in the activity
  list and can talk to the team manually.
- INBOX only (matches the triage toolkit's scope).

## Critical files

- Create: `ui/backend/email_trigger.py` (poll loop + trigger logic),
  `ui/backend/db/email_triggers.py` (CRUD), `alembic/versions/*_email_triggers.py`,
  `tests/test_email_trigger.py`, `tests/test_email_trigger_api.py`.
- Modify: `ui/backend/db/models.py` (model), `ui/backend/main.py` (start/stop
  task in `_lifespan`), `ui/backend/org_settings.py` (or a sibling router
  module) for the API, `ui/frontend/src/pages/wizard/DeployPage.jsx`,
  `ui/frontend/src/pages/wizard/SessionsPage.jsx` (the "My teams" page),
  `ui/frontend/src/lib/api.js`,
  `.env.example`, `docs/deployment.md`, and the CLAUDE.md files for
  `ui/backend`, `ui/backend/db`, `src/bestteam/tools` (drop the
  "not yet implemented" note), plus `docs/STATUS.md`.
- Reuse: `_ImapBackend` (+`restrict_to_public`), `load_email_tools`,
  `spec_uses_email`, `run_in_background`, persisted `runs` rows, guarded
  Alembic migration convention, `get_current_org`.

## Verification

All TDD, against a fake IMAP backend and controllable clock:

- UID dedup: same messages never trigger twice; baseline set on enable (no
  backlog storm); UIDVALIDITY change re-baselines without reprocessing.
- One run per cycle with new mail; input contains exactly the new IDs;
  attributed to `email-trigger`; overlap guard skips while a run is active.
- Cap: run 50 → paused; date rollover resets; cheap-skip before connecting.
- Kill switch stops polling; per-org errors don't kill the loop; `last_error`
  recorded and cleared on next success.
- API: enable requires deployed + uses-email + mailbox connected (each
  failure case); cross-org isolation; platform operator 403; activity list
  returns org's runs only.
- Full backend suite green; frontend lint + build.

## Out of scope (later sub-projects)

- IMAP IDLE / Microsoft Graph or Gmail push webhooks.
- Timer/schedule triggers and any generic trigger framework.
- Batch-prompt redesign of the triage skill.
- Multiple auto-run teams per org.
- Phase-5 full trace persistence / run-history detail pages.
- Multi-worker leader election.
- Retry/replay of messages whose triggered run failed.
