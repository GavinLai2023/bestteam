# Admin Manual — Running a `bestteam` Deployment

A living reference for **platform admins/operators** using the four
admin-only pages (**Accounts**, **Advanced**, **Memory**, **Trace**) and the
operator CLI day to day. Update this file whenever an admin-only surface is
added or changed — treat it the same way `docs/STATUS.md` is kept current.

This is the *how do I use it* companion to
[`docs/ADMIN_GUIDE.md`](ADMIN_GUIDE.md), which is the deep-dive reference for
provisioning orgs/users via `python -m ui.backend.admin`. This manual doesn't
repeat that CLI reference — it links to it (§6) and focuses on the web UI
plus anything the CLI doesn't cover.

Who gets these pages: a **platform admin** (`is_admin=true`, org-less — see
`ADMIN_GUIDE.md` §1). An org member (a customer) never sees any of them.

---

## 1. Where everything lives

| Page | Route | What it's for |
|------|-------|----------------|
| **Accounts** | `/accounts` | Create/deactivate organizations; create/reset-password/move/delete their users. Web-UI subset of the operator CLI (§6). |
| **Advanced** | `/advanced` | Deploy and edit config: pipelines ("AI teams"), skills, knowledge bases, tools (read-only), the model catalog. |
| **Memory** | `/memory` | Browse, search, and delete a user's per-user memory; org-level erasure. Only present when the deployment has memory enabled. |
| **Trace** | `/trace` | Run history and analytics across every org (superset of a customer's own Activity page); **diagnostic re-run** (§2.5) lives here. |

All four share an **organisation selector** defaulting to "All organisations"
(admins are cross-org by design) and reuse the same visual conventions
(`.advanced` layout, `wizard-card` list rows).

---

## 2. The Trace page

### 2.1 Runs tab

Filter by pipeline name and status, browse the (cross-org, paginated) run
list, and click a run to open its detail panel — the same
`AdminRunDetail`/`useRunTrace` machinery a customer's own run detail view
uses, but with the full raw event payload always available and no per-agent
redaction beyond what §2.3/§2.5 describe.

A running run's detail panel streams live over the same WebSocket the
monitoring dashboard uses; a finished run's detail is read from the
persisted `trace_events`/`usage_records` tables, so history survives a
backend restart.

### 2.2 Reading a trace: event types

Every event in the timeline has a label (`EVENT_LABELS` in
`lib/traceEvents.ts`) and a one-line summary, with the full JSON payload
available inline (or, for diagnostic-only events, behind a "Full payload"
toggle — see §2.5). The event types you'll see on an ordinary run:

| Event | What it tells you |
|-------|--------------------|
| `run_started` / `run_completed` / `run_failed` / `run_cancelled` | Run-level bookends. |
| `agent_started` / `agent_completed` | An agent's turn began/ended — role, goal, and (on completion) its output plus per-model-call token usage. |
| `tool_started` / `tool_completed` | A tool call — name, success/failure, duration, and a business-safe `summary` (for a knowledge-base tool: query, hit count, citation labels — **never the retrieved excerpt text**, by design; see `src/bestteam/core/CLAUDE.md`). |
| `agent_progress` | "iteration N of MAX" — the agent asked for another tool round. |
| `subagent_started` / `subagent_completed` | A HIERARCHICAL manager delegating to a subordinate. |
| `memory_recalled` / `memory_recorded` / `memory_failed` | Per-user memory read/write, when memory is enabled. |

### 2.3 Analytics tab

A pipeline-level aggregate view for spotting *patterns* across many runs,
rather than one run's detail: a table of (org, pipeline) rows — run count,
success rate, average duration, total input/output tokens, total cost
estimate (only for models with catalogue pricing). Click a row to open
**per-agent**, **per-model**, and **common failure points** breakdowns for
that pipeline — the last one is the fastest way to see *which agent, which
event type* accounts for most of a pipeline's failures before you drill into
one run.

### 2.4 By model tab

The same total-tokens/total-cost view, but grouped by model spec instead of
pipeline — useful for "which model is actually driving spend this month",
across every pipeline that uses it.

### 2.5 Diagnosing one poor run, step by step

**When to use this:** a customer reports that a run's output was wrong or
unsatisfying (e.g. a knowledge-base chatbot gave a bad answer) and you need
to find *which step* went wrong — not run a batch quality evaluation
(there is no such framework yet; see `docs/STATUS.md`).

1. **Find the run.** Runs tab → filter by org/pipeline/status → open its
   detail panel.
2. **Click "Diagnose this run"**, shown at the top of the panel when the run
   qualifies. It's hidden or refused when:
   - the run is itself a diagnostic re-run (diagnose the *original* instead
     — the banner on a diagnostic run's own detail panel has an "Open
     original run" button back to it);
   - the run carries a `trigger_context` — an autonomous email run or a
     shared-chat turn — because a re-run would actually reach the org's
     live mailbox or the visitor's session (400, with an explanatory
     message);
   - the run's content was purged by the org's retention policy (409 — no
     input left to re-run);
   - the run is still `running`.
3. **A new run starts.** It re-executes the *original input* against the
   **team as currently deployed** — not the pinned version the original run
   used. If the team was redeployed since, the panel shows "The team was
   redeployed after the original run; this diagnoses the current version"
   (`version_changed`) — the problem may no longer reproduce. No per-user
   memory is recalled or written for a diagnostic run (the admin must not
   act as the customer), and spend is metered to the org like any other run.
4. **Read the extra trace detail.** Only on a diagnostic run's trace, you
   additionally see (collapsed behind "Full payload" — a whole prompt or
   tool result can be long):
   - **📝 prompt** (`agent_prompt`) — the exact system prompt and input
     text each agent's first model call received.
   - **💬 model turn** (`model_turn`) — one event per model call (not just
     the final one), with the model's full text and the tool calls it
     requested.
   - **Tool args and results** — `tool_started` gains the call's arguments,
     `tool_completed` gains the full string the model actually read back
     (for a knowledge-base tool, this is the retrieved excerpts themselves —
     the one thing an ordinary trace deliberately omits).
   - Every diagnostic string is capped at 20,000 characters (recursively,
     including inside nested tool arguments) so one oversized call can't
     blow up a trace row.
   - **The email tools stay redacted even in diagnostic mode** —
     `email_read`/`email_draft_reply`/etc. never gain `args` or `result`,
     because that's the customer's real mail content. This is a hard
     boundary, not a v1 gap.
5. **Localize the failure.** Walk the timeline in order:
   - Bad retrieval? Check the KB tool's `result` — did it actually surface
     the relevant passage?
   - Right passage, wrong answer? Compare that `result` against the
     following `model_turn.content` — did the model ignore or misread it?
   - Prompting problem? Check `agent_prompt.system_prompt` for a missing or
     conflicting instruction.
   - Wrong tool usage? Check `tool_started.args` — e.g. a mis-formed query.

**Deliberately out of scope for v1** (see `docs/STATUS.md`): rebuilding the
run's *original* pinned version rather than the current one; relevance
scores on knowledge-base hits (rank order and the excerpt text only); a
per-run "purge this diagnostic run" action (org retention still applies to
it like any other run); reproducing the original run's memory context; any
batch/golden-set answer-quality evaluation.

---

## 3. The Advanced page — configuration

Five tabs (`KINDS` in `AdvancedPage.tsx`), ordered "the deployable unit, then
what it's built from, then read-only reference":

| Tab | Org-scoped? | Editable? |
|-----|:-----------:|:---------:|
| **Pipelines** | yes (`?org=`) | Deploy/edit the JSON config directly — this *is* what the Team Builder wizard and a customer's "My teams" produce. |
| **Skills** | optional (platform-tier if omitted) | Yes. |
| **Knowledge bases** | yes | Yes — either edit JSON directly, or **create from files**: upload documents and the page polls the resulting ingestion job (up to ~1 minute) until it's ready or fails. |
| **Tools** | none | Read-only reference list of the built-in tools. |
| **Model catalog** | none | Edit pricing/availability entries; this is where a model becomes selectable in the wizard and in raw pipeline config. |

Use the organisation selector at the top to scope pipelines/knowledge bases
to a specific org, or the platform tier (skills only) when `orgScope` is
`optional`/`none`.

Editing a pipeline's config here has the same effect as a customer
redeploying it, or the Team Builder wizard's Deploy step — including
invalidating the cached compiled graph, and (relevant to §2.5) changing
what "the currently deployed version" means for a future diagnostic re-run.

---

## 4. The Memory page

Only shown when the deployment has per-user memory enabled
(`BESTTEAM_MEMORY_DB`) — otherwise the page explains how to enable it and
stops there.

- Pick a user (memory is scoped by `(user_id, org_id)`, so a moved user can
  have more than one identity — a "legacy" identity with `org_id = null` is
  pre-org-scoping memory, see `ADMIN_GUIDE.md` §6).
- Search their `episodic`/`semantic`/`procedural` records, filter by type.
- **Delete one record**, or **clear all memory for that user** (across every
  org) — both destructive and confirmed before executing.

There is no manual add/edit of a memory record, and no retention/quota
policy on memory yet (see `docs/STATUS.md`).

---

## 5. The Accounts page

The day-to-day subset of the operator CLI: create/deactivate organizations;
create, reset the password of, move, or delete their users. It cannot grant
or revoke admin, manage platform-operator accounts, or bootstrap the first
admin — those are CLI-only (§6).

---

## 6. Operator CLI

The full account/org/mailbox provisioning surface — including the things the
Accounts page can't do (`promote`/`demote`, bootstrapping the first admin,
connecting an org's mailbox) — is `python -m ui.backend.admin`, documented in
full in **[`docs/ADMIN_GUIDE.md`](ADMIN_GUIDE.md)**. Don't duplicate that
reference here; update it there and link to it.

---

## 7. Customer self-service settings (context, not admin-managed)

These live on the customer's own **Activity** page (`/activity`, inside
`RequireOrgMember`, not an admin surface) — a customer manages them for
their own org. Worth knowing as an admin so you can guide/support a
customer, or reason about why a run behaved a certain way:

| Setting | What it controls |
|---------|-------------------|
| **Mailbox connection** (`PUT /api/org/email`) | The org's own IMAP mailbox for the email tools — a customer can self-serve this (mirrors `set-email` in the CLI, §6). |
| **Data retention** (`DataRetentionPanel`) | A purge period after which a run's *content* (input/output/trace/item payloads) is cleared while accounting rows (the run, usage records, an item result's status) survive. NULL = keep forever. This is also what makes a run 409 on diagnostic re-run (§2.5) once purged. |
| **Email budget** (`EmailBudgetSettings`) | Per-org daily message cap and monthly spend cap on autonomous email processing; pauses dispatch, alerts once per period, resumes automatically. Alongside — not replacing — the operator's deployment-wide `BESTTEAM_TRIGGER_DAILY_CAP`. |
| **Notification webhook** | One optional per-org webhook for in-app-style alerts on a health *transition* (not per occurrence). |
| **Email filter** | Sender/subject block/allow rules and bulk-mail skipping, evaluated before any model sees an inbound message. |

---

## 8. Keeping this manual current

When you add or change an admin-only page, route, or CLI command:

1. Update this file's relevant section (or add one).
2. If it changes provisioning/accounts behaviour specifically, update
   `docs/ADMIN_GUIDE.md` instead (or as well) — that's the CLI's source of
   truth.
3. If it's a new capability worth a line in the living kanban, add it to
   `docs/STATUS.md`.

Don't let this drift into a second copy of the CLI reference or the STATUS
kanban — link to them instead of restating their content.
