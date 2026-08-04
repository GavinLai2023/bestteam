# Property Maintenance Inbox — Release 1A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution note for this session:** implemented inline (single session, full
> codebase context already loaded) rather than via fresh per-task subagents —
> re-deriving the architecture notes below in each fresh subagent would cost
> more than it saves. Tasks are still committed one at a time.

**Goal:** Ship a working, tested vertical slice of Release 1A from
`docs/superpowers/specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md`
— structured, auditable, org-scoped results for property-maintenance email
triage runs, draft-only, with a Needs-attention surface and safe retry.

**Architecture:** Two-agent SEQUENTIAL workflow (Maintenance Intake Analyst →
Maintenance Response Coordinator) built from three new versioned platform
Skills on top of the existing email-trigger/draft-only toolkit. The
coordinator's final message is a strict JSON envelope; a new server-side
normalization step (`ui/backend/automation_results.py`) parses and validates
it against the run's persisted `trigger_context` (the UID batch the poller
detected) and writes one immutable `automation_item_results` row per UID —
never trusting the model for org/run/UID identity. Activity/Run Detail surface
these results without exposing raw email bodies.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), React + Vite
(frontend), Pydantic v2 for envelope validation, existing LangGraph adapter.

## Global Constraints (from the spec)

- No `Case`/`WorkOrder`/state machine — results are immutable rows (spec §5).
- No automatic send; only `email_draft_reply` (draft-only) may run (spec §7, §9.5).
- Response agent gets no `email_find`/`email_read`; Intake agent gets no draft tool (spec §7 WP1 acceptance).
- Model output never decides `org_id`/`run_id`/`source_key` — server-generated only (spec §10.1).
- `source_key` format: `mailbox:<credential-id>:uidvalidity:<value>:uid:<uid>` (spec §5.3).
- Every UID in a batch gets exactly one result row, including on invalid JSON / missing / duplicate / out-of-batch model output (spec §10.1, WP2 acceptance).
- `possible_emergency` and `unknown` priority always imply `needs_attention=true` (spec §9.3).
- Email tool `tool_completed` trace events must not carry subject/body text (spec §15.2).
- All new queries/results filtered by the caller's `org_id`; cross-org is 404, never revealing existence (established backend convention, `ui/backend/CLAUDE.md`).
- Alembic migrations must be `create_all`-safe (guard every op by inspection) — see `tests/test_migrations.py`.
- Regression commands (must pass): `./.venv/Scripts/python.exe -m pytest`, `cd ui/frontend && npm run lint && npm run build`.

## Explicitly out of scope for this PR

- **WP0** (real customer discovery / offline eval dataset) — needs live
  customers and real mailboxes; not something a coding session can do.
- **WP6** (attachment reading, per-org Microsoft Graph/OAuth) — spec marks
  these conditional on WP0's findings, Phase 1B.
- The guided Policy-Skill wizard form (spec §13.3) — an org's
  `<org_slug>_maintenance_policy_v1` skill can already be authored today via
  the existing Advanced Skills CRUD (`/api/config/skills`); a dedicated wizard
  step is a UX enhancement, not required for the backend/data contracts to be
  correct and testable.
- SDK-level declarative `output_schema` (spec §10 explicitly defers this;
  "version-pinned prompt + server Pydantic validation" is the stated 1A approach).

## File Structure

- `ui/backend/db/models.py` — add `AutomationItemResult`, `Run.trigger_context`, `Run.retry_of_run_id`.
- `alembic/versions/<new>_automation_item_results.py` — new table + new `runs` columns.
- `ui/backend/automation_results.py` — **new**: Pydantic envelope models, `normalize_run_result(db, run_row)`, query helpers for the list/summary API.
- `ui/backend/email_trigger.py` — thread `mailbox_credential_id` through `_start_triggered_run`, populate `trigger_context`.
- `ui/backend/runtime.py` — call `automation_results.normalize_run_result` once a triggered run reaches a terminal state.
- `ui/backend/main.py` — `GET /api/automation-results`, `GET /api/automation-results/summary`, `POST /api/runs/{run_id}/retry`.
- `ui/backend/skills.py` — 3 new `DEFAULT_SKILLS` entries.
- `ui/backend/workflows/property_maintenance_inbox_demo.yaml` — **new** demo template (fake models, mirrors `code_review.yaml`'s "swap to a real model" convention).
- `src/bestteam/adapters/langgraph_adapter.py` — redact email-tool `tool_completed` summaries.
- `ui/frontend/src/lib/api.js` — `listAutomationResults`, `automationResultsSummary`, `retryRun`.
- `ui/frontend/src/components/MaintenanceInboxSummary.jsx`, `NeedsAttentionList.jsx` — **new**.
- `ui/frontend/src/pages/ActivityPage.jsx` — mount the new components in the Automations tab.
- `ui/frontend/src/components/RunDetail.jsx` — automation-results section.
- Tests: `tests/test_automation_results.py`, `tests/test_automation_results_api.py`, `tests/test_email_trigger.py` (extend), `tests/test_skill_seeding.py` (extend), `tests/test_trace_redaction.py` (or extend `test_trace_granularity.py`); frontend `*.test.jsx` extensions.

## Data contract

`automation_item_results` (spec §5.3):

| column | type | notes |
|---|---|---|
| id | Integer PK | |
| org_id | FK organizations, NOT NULL | |
| run_id | FK runs, NOT NULL | |
| source_type | String, default `"email"` | |
| source_key | String, NOT NULL | server-generated, see format above |
| result_type | String, NOT NULL | `"property_maintenance_email"` |
| status | String, NOT NULL | `processed \| needs_attention \| skipped \| error` |
| needs_attention | Boolean, default False | |
| payload | JSON | validated, length-capped extraction fields — never raw email body |
| created_at | DateTime | |

`UniqueConstraint(run_id, source_key)`, `Index(org_id, created_at)`, `Index(org_id, needs_attention, created_at)`.

`Run.trigger_context` (JSON, nullable): `{trigger_type, mailbox_credential_id, uidvalidity, uids: [...], folder, triggered_at}`. `Run.retry_of_run_id` (nullable FK runs.id).

## Normalization decision (scoping the "always create synthetic errors" spec language)

Spec §10.1 reads as if every `trigger_context`-bearing run gets normalized.
Taken literally that would also start creating `error` rows for *every other
org's* existing generic `email_triage_reply` trigger workflow (free-text
output, not our JSON envelope) — a correctness regression for an unrelated
feature. Decision: `normalize_run_result` only engages once the run's final
output **parses as JSON whose top-level `result_type ==
"property_maintenance_email_batch"`**. If the output isn't JSON at all (or
lacks that marker), normalization no-ops — that run was never a
maintenance-inbox run. Once the marker is present, every downstream failure
mode (bad enum, missing UID, invalid item shape) gets the full
error/needs-attention treatment the spec requires. This keeps the blast
radius to exactly the new template while satisfying the "nothing silently
disappears" requirement for anything that *did* declare itself part of this
result type.

## Tasks

- [x] Task 1 — `automation_item_results` model + `Run.trigger_context`/`retry_of_run_id` (db/models.py)
- [x] Task 2 — Alembic migration (guarded, chained after `b8c9d0e1f2a3`)
- [x] Task 3 — `automation_results.py`: envelope Pydantic models + `normalize_run_result`
- [x] Task 4 — wire trigger_context population + normalization call
- [x] Task 5 — API: list/summary/retry endpoints
- [x] Task 6 — 3 platform skills
- [x] Task 7 — demo workflow YAML
- [x] Task 8 — email tool trace redaction
- [x] Task 9 — backend tests (60 new backend tests; full suite 891 passed)
- [x] Task 10 — frontend surfaces
- [x] Task 11 — frontend tests (full suite 82 passed)
- [x] Task 12 — full regression pass (pytest 891 passed, eslint clean, vite build green)
- [x] Task 13 — docs + PR

(Checkboxes are updated as the session progresses — see the task list via TaskList for live status.)
