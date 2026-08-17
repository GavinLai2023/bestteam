# Email automation — Phase 3a: trigger health, alerting, and two correctness fixes (design)

Date: 2026-08-17
Status: approved, not yet implemented

## Context

Phases 0–2 are merged to `main`. Phase 0 made a failing trigger *visible*
(`EmailTrigger.last_error`); nothing ever *tells* anyone. A customer whose
automation stopped a week ago finds out by opening the dashboard, which is
exactly the failure mode the capability was meant to remove.

The joint review's "Phase 3" bundled two unrelated subsystems — retention
governance and alerting. They share no problem: retention is about the data's
lifetime, alerting is about the system's liveness. They are split here.

**This spec is Phase 3a: does the automation run correctly, and do you find
out when it doesn't.** Retention/deletion/export becomes Phase 3b and gets its
own cycle.

A Codex review of the Phase 0–2 branch raised three findings. Two belong here
because they are liveness/correctness defects in the same code paths; the
third (egress validation across agent handoffs) is a security-boundary fix that
lands first as its own small change, before this spec's work begins.

## Problems being fixed

| # | Problem | Reachable how |
|---|---|---|
| 3a.1 | A failing trigger notifies nobody | any repeated workflow failure |
| 3a.2 | A mailbox that stops authenticating notifies nobody | credential rotation, expiry, permission change |
| 3a.3 | An M365 client secret expires with no warning; the failure then looks like a wrong password | Entra secrets expire in ≤2 years, always |
| 3a.4 | A watchdog release notifies nobody | any model/network hang |
| 3a.5 | (Codex P1) The watchdog can expose an actively-executing run for retry, producing duplicate drafts | timeout while a draft's `APPEND` is in flight |
| 3a.6 | (Codex P2) A superseded run's outcome overwrites the current trigger's health | watchdog release, then the old run finishes |

### 3a.5 in detail

`_release_stale_run` marks a timed-out run `failed` and hands its messages
back, because a node executing inside `workflow.stream()` cannot be
interrupted. The original worker is still alive. A retry can therefore scan for
confirmed drafts, find none (the original's `APPEND` has not landed yet),
draft for the same UIDs, and then the original completes its `APPEND` too —
two drafts for one message.

Codex proposed keeping the run non-retriable until the worker acknowledges
cancellation. **Rejected**: the worker never acknowledges — being wedged is the
premise — so this reinstates the permanent trigger blockage that Phase 0's
item 0.4 exists to fix. Trading a rare duplicate draft for a guaranteed silent
stop is the wrong trade.

The defect is not in *when we plan a retry*; it is that **`APPEND` is not
idempotent**. The fix belongs at the point of writing.

Decisive detail: both racers are **in the same process**. The wedged worker is
a thread in the run thread-pool; the retry is another thread in the same
uvicorn process. A process-wide lock keyed on the draft's source key therefore
closes the window completely under this deployment model, rather than merely
narrowing it. The codebase already has this pattern (`component_mutation_lock`,
`ui/backend/component_lock.py`).

## Judgement calls

**M365 secret expiry is admin-entered, not queried from Graph.** Reading an
app registration's `passwordCredentials.endDateTime` requires
`Application.Read.All` — a directory-wide read over *every* app registration in
the customer's tenant. That is drastically broader than the
`IMAP.AccessAsApp` permission Phase 2 asks for, which an Application Access
Policy can pin to a single mailbox. Asking a customer's IT for
tenant-wide directory read in order to power a reminder is a request they
should refuse. The expiry date is therefore an optional field the admin fills
in when connecting the mailbox — they are reading it off the Azure portal page
at that exact moment. No expiry entered means no expiry alerts, and nothing
else changes.

**Alerts fire on state transitions, not on occurrences.** The most common way
an alerting system fails is not missing an event; it is generating enough noise
that people mute it. A trigger holds the fingerprint of the alert most recently
sent; a condition already alerted for does not alert again until it clears.
Recovery emits one notification and clears the fingerprint.

**One `Notification` table serves both the in-app list and the webhook
outbox.** Webhook delivery needs retry state; the in-app list needs the same
rows. Two tables would immediately produce "shown in the app but never
delivered" inconsistencies with no single place to reconcile them.

**Webhook URLs are validated with `check_host_allowed` and must be HTTPS.**
An org admin configuring a webhook is not the same trust level as an agent
choosing a URL — the target is fixed by a human, so this does not weaken
Phase 0's injection containment (item 0.6), which is about *model-chosen*
destinations. But in a multi-tenant deployment a tenant admin is still not
trusted to reach the host's internal network, so the same public-IP restriction
the per-org IMAP path uses applies here. Self-hosted internal webhook endpoints
are explicitly unsupported.

**No new dependency.** Webhook delivery uses stdlib `urllib.request`, exactly
as Phase 2's token provider does, so the per-org email path still needs no
optional extra and `backend-optional-deps` keeps passing.

**The threshold applies to workflow and mailbox faults, not to the watchdog.**
A hung run is already a sustained 30-minute symptom by the time the watchdog
sees it; requiring three of them before saying anything would mean 90 minutes
of silence.

## Design

### Data model

`EmailTrigger` gains two columns:

- `consecutive_faults: int = 0` — consecutive fault cycles of any kind; reset
  to 0 by any healthy outcome (a completed run, or a successful mailbox check).
- `alerted_fingerprint: Optional[str]` — the fingerprint of the alert most
  recently *sent*, `NULL` when healthy.

`OrgEmailCredential` gains one column:

- `oauth_secret_expires_at: Optional[datetime]` — admin-entered, M365 only.

New table `notifications`:

| column | type | note |
|---|---|---|
| `id` | int pk | |
| `org_id` | FK organizations.id, indexed | org-scoped like everything else |
| `kind` | str | `trigger_health` \| `secret_expiry` |
| `severity` | str | `error` \| `warning` \| `info` |
| `title` | str | one line, customer-facing |
| `body` | str | what happened and what to do |
| `fingerprint` | str | dedup/threading key |
| `created_at` | datetime | |
| `read_at` | Optional[datetime] | in-app read state |
| `delivery_state` | str | `pending` \| `delivered` \| `failed` \| `skipped` |
| `delivery_attempts` | int = 0 | |
| `delivered_at` | Optional[datetime] | |
| `last_delivery_error` | Optional[str] | truncated |

`skipped` means the org has no webhook configured — the notification is
in-app only, which is not a failure.

New table `org_notification_settings` (one row per org):

| column | note |
|---|---|
| `org_id` | unique FK |
| `webhook_url` | Optional, HTTPS, validated |
| `webhook_secret_encrypted` | Optional, Fernet via `secret_store` |
| `enabled` | bool = True |
| `created_at` / `updated_at` | |

The webhook secret lives in the same encrypted-at-rest scheme as mailbox
credentials, so `ensure_secrets_key_for_stored_credentials` covers it too.

### Health evaluation (pure)

`ui/backend/trigger_health.py` — a new module holding one pure function and no
I/O:

```python
def evaluate(
    *,
    outcome: str,              # "healthy" | "workflow_fault" | "mailbox_fault" | "run_timeout"
    consecutive_faults: int,
    alerted_fingerprint: Optional[str],
    threshold: int,
) -> HealthDecision
```

`HealthDecision` carries the new `consecutive_faults`, the new
`alerted_fingerprint`, and an optional `NotificationDraft` (kind, severity,
title, body, fingerprint). Rules:

- `healthy` → faults 0; if a fingerprint was set, emit a recovery notification
  and clear it.
- `workflow_fault` / `mailbox_fault` → faults + 1; when it reaches `threshold`
  and the fingerprint differs from the one already alerted, emit and set it.
- `run_timeout` → emit immediately (fingerprint `run_timeout`), regardless of
  the count.

Being pure, the whole noise-control policy is testable by passing a sequence of
outcomes and asserting which notifications come out — no database, no clock, no
mailbox.

Threshold: `BESTTEAM_TRIGGER_ALERT_THRESHOLD`, default 3, minimum 1, validated
at startup beside the other trigger env vars.

### Wiring

Two call sites already own the two fault kinds, and both currently write
`last_error`/`last_error_kind` directly:

- `runtime._safe_record_trigger_health` — workflow outcomes.
- `email_trigger`'s connectivity check — mailbox outcomes.
- `email_trigger._release_stale_run` — the `run_timeout` outcome.

Each now calls `evaluate` and persists the decision in the same transaction it
already uses, appending a `Notification` row when one is produced. All three
keep their existing isolation (a health write must never fail a run).

### 3a.6 — superseded runs

`_safe_record_trigger_health` returns early unless
`trigger.last_run_id == run_row.id`. `last_run_id` is already the trigger's
notion of "the run I am waiting on", set at dispatch, so after a watchdog
release starts a new run the old run's outcome is correctly ignored.

### 3a.5 — idempotent draft creation

In `email_client.py`, when a draft marker prefix is configured:

1. Acquire a process-wide lock keyed on the draft's source key
   (a module-level `dict[str, Lock]` guarded by a meta-lock, mirroring
   `component_lock.py`).
2. Inside the lock, search Drafts for that source key
   (`drafts_with_source_keys`, added in Phase 0).
3. If found, skip the `APPEND` and report `outcome: "draft_exists"`.
4. Otherwise `APPEND`, then release.

The tool result gains the `draft_exists` outcome so the trace records the
skip honestly rather than claiming a draft was created.

**Limitation, recorded deliberately:** with more than one worker process the
lock degrades to per-process and the window reopens. The email capability is a
single-instance MVP by design (`STATUS.md`), and making the overlap guard
DB-authoritative is already tracked as the next step for multi-worker safety.

### Secret expiry checks

A daily sweep inside the existing poller cycle (no new thread, no new
scheduler): for each org with `oauth_secret_expires_at` set, compute days
remaining and emit at 30 days, 7 days, and on expiry — fingerprints
`secret_expiry_30`, `secret_expiry_7`, `secret_expired` so each fires exactly
once. The check is skipped entirely when the column is `NULL`.

### Webhook delivery

`ui/backend/notifications.py`:

- `dispatch_pending(db, limit)` — drains `pending` notifications, called at
  the end of each poll cycle (`poll_once`), so no new thread is introduced.
- Per notification: no webhook configured → `skipped`. Otherwise POST JSON
  over stdlib `urllib.request`, 10s timeout, with
  `X-BestTeam-Signature: sha256=<hmac>` computed over the exact request body
  using the org's decrypted secret, plus `X-BestTeam-Delivery: <id>`.
- 2xx → `delivered`. Otherwise increment attempts, record a truncated error;
  at 5 attempts → `failed`. A `failed` notification stays visible in-app —
  the customer still learns, just not through the webhook.
- Delivery never raises into the poll loop.

The payload is the notification's own fields (kind, severity, title, body,
fingerprint, org id, created_at). **No email content of any kind** — subjects,
addresses and bodies never leave the deployment through this path.

### API

- `GET /api/notifications` — org-scoped, newest first, `unread_only` filter.
- `POST /api/notifications/{id}/read` — marks read; 404 across orgs.
- `GET /api/org/notifications` / `PUT /api/org/notifications` — webhook
  settings, admin-only, never returns the secret.

### Frontend

- An unread count on the existing nav, and a Notifications page listing
  title/body/severity/time with a mark-as-read action.
- Webhook settings in the org settings area, beside the email connection:
  URL, optional secret, enabled toggle, and a plain statement that only
  health information is sent, never email content.
- The M365 secret-expiry field is added to the existing `EmailConnect` form,
  visible only for the Microsoft provider and clearly optional.

## Data flow

```
run finishes ─→ _safe_record_trigger_health ─┐
mailbox check ─→ connectivity outcome ───────┼─→ evaluate() ─→ HealthDecision
watchdog fires ─→ _release_stale_run ────────┘                      │
                                                                    ↓
                                            trigger columns + Notification row
                                                                    │
poll cycle end ─→ dispatch_pending ─→ webhook (or skipped) ─────────┘
                                                                    │
                                            GET /api/notifications ─┘
```

## Error handling

Every new path is subordinate to the work it observes. A notification write
that fails must not fail the run; a webhook that fails must not fail the poll
cycle; a malformed webhook URL must not stop health tracking. Each site keeps
the existing `_safe_*` isolation pattern, logging and continuing.

The one exception: `PUT /api/org/notifications` validates the URL
synchronously and rejects a bad one with a 400, because there the customer is
present to fix it.

## Testing

- `tests/test_trigger_health.py` (new) — the pure evaluator: fault sequences,
  threshold boundary, fingerprint suppression, recovery, timeout bypass.
- `tests/test_notifications.py` (new) — delivery: skipped without a webhook,
  HMAC signature correctness, non-2xx retry then `failed`, never raises.
- `tests/test_email_trigger.py` — watchdog emits a `run_timeout` notification;
  mailbox faults count and alert at the threshold; secret-expiry sweep fires
  once per band.
- `tests/test_runtime_run_row.py` — a superseded run's outcome is ignored
  (3a.6); a current run's is applied.
- `tests/test_email_scoped_tools.py` — a second draft for the same source key
  is skipped with `outcome: "draft_exists"`; concurrent drafting from two
  threads produces exactly one `APPEND` (3a.5).
- `tests/test_org_settings.py` — webhook settings round-trip; secret never
  returned; non-HTTPS and private-IP URLs rejected.
- `tests/test_migrations.py` — the new columns/tables are additive and the
  downgrade drops them.
- Frontend: notification list rendering and unread state; webhook settings
  form; the M365 expiry field appears only for the Microsoft provider.

Success criteria: every new test fails before its change and passes after; the
full non-e2e suite stays green serially in one process; `tsc --noEmit` and the
frontend suite stay clean.

## Explicitly out of scope

- **Retention/deletion/export** — Phase 3b, the other half of the original
  Phase 3.
- Alert channels beyond in-app and webhook (no SMTP anywhere, by design).
- Per-user notification preferences, digests, or quiet hours — one org-level
  webhook, and a threshold. Anything finer is speculative until a customer
  asks.
- Querying Entra for secret expiry (see Judgement calls).
- Multi-worker-safe draft idempotency (needs the DB-authoritative overlap
  guard, already tracked).
- Draft outcome observation (sent/edited/discarded) — still unaddressed,
  still Phase 4+.
