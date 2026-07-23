# Design: Autonomous email-trigger correctness redesign

## Context

The autonomous email-trigger MVP
(`docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md`,
branch `feature/email-trigger-autonomous-runs`) shipped through full
subagent-driven review, then an external review found a **correctness cluster**
that the per-task and whole-branch reviews missed because they verified the
poller mechanics in isolation and never modeled the poller ↔ triage-skill ↔
tool interaction. This spec fixes that cluster. It does **not** touch the
approved architecture (in-process asyncio poller, per-org opt-in, UID baseline,
daily cap, activity list); it corrects how a detected batch actually reaches
the agent and how state advances.

The four findings this fixes:

- **F1 — runs are not constrained to detected UIDs.** The poller names the
  detected UIDs only in the user prompt, but the seeded `email_triage_reply`
  skill (`ui/backend/skills.py:51`) instructs the agent to *"Call email_find
  with no query to list unread messages"* — and `email_find("")` returns the
  latest ~20 `UNSEEN` messages (`email_client.py`), not the detected set.
  Reads never mark mail seen and the baseline still advances, so a burst > 20
  silently skips messages and old unread mail is re-drafted every cycle.
- **F2 — state advances without a durable run.** `last_uid`/`runs_today`
  advance before the workflow is loaded; a load failure commits the advance
  with no run created — silent message loss plus consumed quota.
- **F5 — workflow errors are falsely cleared.** `poll_org` clears `last_error`
  on a successful *mailbox* poll, before the workflow is validated, so a
  broken/deleted team flickers back to `active` on any empty cycle.
- **F3 — mailbox replacement keeps a stale baseline.** The credential-write
  path never touches the trigger, so swapping to a different inbox reuses the
  old high-water mark (skips or reprocesses on a `UIDVALIDITY` collision).

Decisions locked with the user (2026-07-20):

- **Hard-bind the tools**, not instructions-only — the guarantee must hold
  regardless of LLM behavior.
- **Cap per run, carry the rest over** — a bounded batch (default 20), advance
  the baseline only through the UIDs handed to a durable run.
- **Disable on mailbox-identity change, ignore password rotation** — key on
  host + username; disconnect always disables.

## Architecture

Four changes, isolated to the trigger path and its immediate collaborators;
the interactive-run path and the workflow cache are untouched.

### 1. UID-scoped email tools (`src/bestteam/tools/email_client.py`)

`make_email_tools(backend, allowed_uids=None)` gains a scoped mode. When
`allowed_uids` is a non-empty set of IMAP UIDs:

- `email_find(query="")` **ignores its query** and returns header summaries for
  exactly the allowed UIDs (fetched read-only, in UID order), nothing else.
- `email_read(message_id)` and `email_draft_reply(message_id, body)` **reject**
  any id not in `allowed_uids`, returning a clear tool message
  (e.g. *"That message isn't part of this batch."*) instead of touching it.

`allowed_uids=None` preserves today's behavior exactly (the interactive/env
path is unchanged). The scoped tools keep the same names and docstrings the
model sees, so the seeded triage skill runs unmodified but is physically
confined to the batch.

**UID space.** The poller detects IMAP UIDs via `UID SEARCH`; the scoped tools
must read/draft in that same UID space. The implementation plan MUST verify
that `_ImapBackend.read`/`draft_reply` key on IMAP UID (the id `email_find`
returns), not the RFC822 `Message-ID` header — the entire binding depends on
that identity. If they don't, the scoped wrappers translate, or the backend
read path is corrected, before anything else in this spec is built.

### 2. Trigger-run workflow builder (`ui/backend/`)

A trigger run needs a workflow whose email tools are scoped to that run's
batch, so it cannot reuse the per-`(org, name)` cached workflow (whose email
tools are the normal per-org ones). Add a builder that mirrors the DB-record
branch of `main._get_workflow` — load the `WorkflowRecord` for `(org_id,
name)`, its KB tools and skills — but substitutes UID-scoped email tools built
from the org's stored credentials, and builds **uncached** (the UID set is
per-run; trigger runs are infrequent, ≤ cap/day).

To avoid duplicating `_get_workflow`'s body, factor its DB-record build into a
helper that accepts an optional `email_tools_override`; `_get_workflow` calls
it with `None` (cached path unchanged), the trigger path calls it with the
scoped tools (uncached). Exact refactor-vs-extract is a plan decision; the
constraint is **no behavior change to the interactive path**.

### 3. Poll cycle rewrite (`ui/backend/email_trigger.py::poll_org`)

Order of operations per enabled org per cycle:

1. Cap/date-rollover check and overlap guard (unchanged — registry-based).
2. `check_mailbox` → `(uidvalidity, max_uid, new_uids)` (all above baseline).
3. `UIDVALIDITY` change → re-baseline, commit, return (unchanged).
4. No new UIDs → commit health, return (but see §4 for `last_error`).
5. `batch = sorted(new_uids)[:BATCH_SIZE]` (default 20, `BESTTEAM_TRIGGER_BATCH_SIZE`).
6. **Build the trigger-run workflow for `batch` first.** On failure: set
   `last_error` (customer-readable), **advance nothing**, commit, return.
7. Create the durable run: `registry.create(...)` **and** persist the `runs`
   row (status `running`, `username="email-trigger"`, `org_id`) in the
   poller's session.
8. Advance state in one commit: `last_uid = max(batch)`, `runs_today += 1`,
   `last_run_id = run.id`, `last_error = None`.
9. `_executor.submit(run_in_background, ...)` — with the run row already
   persisted, the worker **updates** its terminal status rather than inserting
   (removes the prior FK-timing issue).

`run_in_background` currently inserts the `runs` row itself. The trigger path
pre-inserts, so either (a) `run_in_background` gains an "already-persisted"
mode that updates instead of inserts, or (b) the trigger passes a flag. The
plan picks one; the interactive path keeps inserting as today.

**Residual window (documented, accepted):** if the process is killed between
the step-8 commit and the step-9 submit, the batch's UIDs are advanced with a
`runs` row stuck `running` and no worker. This is the same restart-orphan class
already documented for this feature; the registry-based overlap guard means it
does not wedge future cycles (a restart empties the registry). An optional
startup reconciliation sweep (mark orphaned `running` rows failed) is noted as
future work, not built here.

### 4. Error retention (`ui/backend/email_trigger.py`)

`last_error` is cleared **only** on a successful **dispatch** (step 8 above) or
on (re-)enable — never on a bare successful mailbox poll. A mailbox-level error
is still written when a poll fails and overwritten by the next mailbox outcome.
Consequence: a workflow fault (deleted/invalid team) persists as status `error`
across empty-mailbox cycles until a team successfully runs. Accepted minor
wart: a transient mailbox error can linger through empty cycles until the next
dispatch or next mailbox failure — visible, not a correctness defect.

### 5. Credential sync (`ui/backend/org_settings.py`)

The mailbox-write endpoints become trigger-aware, keyed on mailbox identity
(host + username) captured before the write:

- **`PUT /api/org/email`, host + username unchanged** (password rotation):
  leave the trigger row and baseline untouched.
- **`PUT /api/org/email`, host or username changed** (different inbox):
  set the trigger `enabled = False`. The customer re-enables through the
  existing toggle, which re-baselines against the new mailbox.
- **`DELETE /api/org/email`** (disconnect): set the trigger `enabled = False`.

Disabling (not re-baselining) on a real mailbox change is the explicit
re-consent path: autonomous runs against a newly-connected inbox require the
customer to turn them back on.

## Components and boundaries

| Unit | Responsibility | Depends on |
|---|---|---|
| `make_email_tools(backend, allowed_uids=)` | Bind the 3 email tools, optionally confined to a UID set | `_ImapBackend` (UID-keyed read/draft) |
| trigger-run workflow builder | Build an uncached workflow with scoped email tools | `WorkflowRecord`, KB/skill loaders, `make_email_tools` |
| `poll_org` | Bounded batch, build-first, durable-run-then-advance, error retention | builder, `registry`, `runs` persistence |
| credential-write endpoints | Disable/leave the trigger on mailbox change | `EmailTrigger` CRUD |

## Error handling

| Failure | Behavior |
|---|---|
| Trigger-run workflow fails to build (deleted/invalid team) | `last_error` set, **no** UID/cap advance, no run; status stays `error` across empty cycles until it builds |
| Agent tries an out-of-batch UID | Tool returns a refusal string; no read/draft occurs |
| Burst > batch size | Oldest `N` this cycle, remainder next cycle; baseline only past handled UIDs |
| Mailbox unreachable during poll | `last_error` (friendly), retry next cycle, never auto-disable (unchanged) |
| Process killed between commit and submit | Batch advanced, orphan `running` row; overlap guard self-recovers on restart (registry empty); reconciliation sweep is future work |
| Mailbox host/username changed | Trigger disabled; customer re-opts-in (fresh baseline) |
| Password rotated (same host+user) | Trigger and baseline untouched |

## Testing (all TDD, fake IMAP backend, controllable clock)

- Scoped tools: `email_find` returns only allowed UIDs regardless of query;
  `email_read`/`email_draft_reply` refuse an out-of-batch UID; `allowed_uids=None`
  behaves exactly as today.
- Trigger-run builder: produces a workflow whose email tools are the scoped
  ones; interactive `_get_workflow` output and caching are unchanged.
- `poll_org`: burst of `2*N+3` yields the oldest `N` this cycle with baseline
  at `max(batch)` (not `max(all)`), the rest next cycle, none skipped or
  duplicated; workflow-build failure advances neither `last_uid` nor
  `runs_today` and creates no run; a durable `runs` row exists before dispatch.
- Error retention: a workflow-build failure's `last_error` survives a
  subsequent empty-mailbox poll (status stays `error`); a successful dispatch
  clears it.
- Credential sync: same host+user password change leaves an enabled trigger
  enabled; host or username change disables it; disconnect disables it.
- Full backend suite green; frontend lint/build (no frontend change expected).

## Out of scope (separate P2 hardening follow-up)

Tracked from the external review, not designed here: env-value validation (#6),
per-org session rollback in `poll_once` (#7), cooperative shutdown thread-stop
(#8), explicit run-source enum for attribution (#9 — this spec only reserves
the `email-trigger` username), server-side autonomous filter for the activity
list (#10), `RunRegistry` terminal-run eviction (#4), and the operator-facing
multi-worker note in `deployment.md`. The near-trivial ones (#7, #10, and the
username reservation for #9) may be folded into this implementation
opportunistically; the rest are a distinct sub-project.
