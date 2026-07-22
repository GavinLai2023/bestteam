# Email trigger hardening, round 2

**Status:** approved (self-approved per explicit user authorization — "make
the most suitable decisions by yourself" — no interactive Q&A round for this
cycle).

## Context

An independent reviewer examined `feature/email-trigger-autonomous-runs`
(PR #22, commits `3a8ae0d`→`d3b9601` — the branch that already closed the
first external review's correctness cluster) and returned 8 findings. Each
was verified against the live code before this spec was written (see the
conversation's review-reception pass). Verdict per finding:

| # | Sev | Finding | Disposition |
|---|-----|---------|--------------|
| 1 | P1 | Mailbox replacement race + operator CLI bypasses trigger shutdown | **Fix** — real, scoped fix identified |
| 2 | P1 | `RunRegistry` unbounded growth | **Defer** — already disclosed, already deferred by prior decision; nothing new |
| 3 | P2 | Unhandled `_executor.submit` failure can wedge the trigger | **Fix** — real, narrow, cheap |
| 4 | P2 | Recovered mailbox errors stay falsely "unhealthy" | **Fix** — real, distinct from the already-shipped F5 fix |
| 5 | P2 | Trigger env values unvalidated; bad value silently kills the poller forever | **Fix** — real, plausible operator mistake, no supervision exists |
| 6 | P2 | Shutdown doesn't stop in-flight polling threads | **Defer** — already disclosed (original review's #8), already deferred |
| 7 | P2 | Sentinel username collision on "existing databases" | **Drop** — no deployment of this codebase can hit this; the reservation ships in the same release as the concept it protects |
| 8 | P2 | Frontend collapses fetch failures into "off"/"empty" states | **Fix** — real, matches this project's standing bar for non-technical UX clarity |

This round closes #1, #3, #4, #5, #8. #2 and #6 remain on the already-agreed
P2-hardening backlog (unbounded registry growth, thread-based shutdown) —
nothing here changes their disposition. #7 needs no code change; noted as
considered-and-declined.

All work lands as new commits on the existing `feature/email-trigger-autonomous-runs`
branch, extending the currently-open PR #22 — the reviewer who filed these
findings was reviewing that PR, so the fix should land where they'll look for it.

## Fix 1 — mailbox-replacement race (closes finding #1)

Two independent gaps, both closed here:

**(a) Same-poll-cycle credential re-fetch (the real correctness bug).**
`poll_org` fetches credentials once to build a `backend` for `check_mailbox`.
When new mail is found, `_start_triggered_run` → `build_trigger_workflow` →
`build_org_imap_backend` fetches credentials **again**, independently. If a
credential change lands in that window, the run detects UIDs against the old
mailbox but scopes its tools to the new one — UID numbers that mean something
else entirely in the replacement mailbox. The existing UIDVALIDITY guard
doesn't catch this because it only runs once, at the top of the cycle, before
the second fetch.

Fix: fetch credentials/build the backend exactly once per poll cycle in
`poll_org`, and thread that same `backend` through `_start_triggered_run` into
workflow-building instead of letting `build_trigger_workflow` re-resolve it.
`build_trigger_workflow(name, db, org_id, allowed_uids, backend)` becomes a
required parameter — the caller (`poll_org`) already guarantees a connected
mailbox by the time it's reached (a missing mailbox already raised
`ConfigurationError` earlier in the same function), so no behavior is lost.

**(b) Operator CLI bypasses trigger shutdown entirely.** `org_settings.py`'s
`set_email`/`delete_email` (the self-service HTTP path) disable an enabled
trigger on host/username change or on disconnect. `admin.py`'s `set-email`/
`clear-email` (the operator CLI path, used "mainly for onboarding on the
customer's behalf" per `docs/deployment.md`) have no such logic at all — an
operator rotating a customer's mailbox on their behalf silently leaves a
stale UID baseline pointed at the old mailbox.

Fix: extract the disable logic into two shared helpers in `email_trigger.py`
(the module that already owns trigger business logic):

```python
def disable_trigger(db: Session, org_id: int) -> None:
    """Disable an org's trigger if enabled."""

def disable_trigger_on_identity_change(
    db: Session, org_id: int, new_host: str, new_username: str,
    prior_identity: tuple[str, str] | None,
) -> None:
    """disable_trigger(...) iff prior_identity is set and differs from (new_host, new_username)."""
```

`org_settings.py` is refactored to call these (removing the duplicated inline
logic — the duplication is exactly what let the CLI path drift out of sync).
`admin.py`'s `set-email` captures `prior_identity` before calling
`set_email_credentials` (mirroring `org_settings.py`) and calls
`disable_trigger_on_identity_change` after; `clear-email` calls
`disable_trigger` unconditionally after a successful clear.

**Explicitly not fixed:** the reviewer's prescription of a full
serialize/versioned-CAS layer across all per-org polling, credential, and
trigger mutations. Once (a) removes the double-fetch, the only remaining
window is a credential change landing *between* the trigger-disable-on-
mailbox-change scenario across two different poll cycles — and mailboxes
almost never share a UIDVALIDITY, so the existing cross-mailbox re-baseline
guard (`if trigger.uidvalidity != uidvalidity: re-baseline, don't reprocess`)
already covers that case in the overwhelming majority of real IMAP servers.
A full locking subsystem for a residual, already-mitigated edge case is
disproportionate to this project's scale (small number of orgs, operator-
provisioned, no public registration).

## Fix 2 — unhandled dispatch failure (closes finding #3)

`_start_triggered_run` commits the durable run row, UID baseline, and cap
advance, then calls `_executor.submit(...)` with no failure handling. If
`submit` raises synchronously (its one realistic trigger: the executor is
mid-shutdown), the registry entry and `Run` row are both left `"running"`
forever — the overlap guard's `registry.get(...).status == "running"` check
then blocks every future poll cycle for that org indefinitely.

(A process **crash** between commit and submit is *not* a wedge: the registry
is in-memory and empty after restart, so the overlap guard sees no running
entry and proceeds. That scenario was already the point of the earlier
registry-based overlap-guard fix.)

Fix: wrap the `submit` call. On failure, publish a `run_failed` terminal
event to the registry (mirrors `runtime.py::run_in_background`'s own
CR-003-style catch-all) and mark the `Run` row `"failed"`, so the overlap
guard unblocks on the next cycle. The consumed batch/cap is not retried
(accepted — identical in kind to the already-disclosed commit→submit crash
window in the PR description).

## Fix 3 — mailbox vs. workflow error separation (closes finding #4)

`last_error` currently serves two purposes that need opposite clearing
behavior:
- **Mailbox connectivity errors** (login/DNS/timeout, undecryptable
  credentials) — a *successful* `check_mailbox` call is direct proof the
  problem is gone, so it should auto-clear.
- **Workflow/dispatch errors** (deleted/invalid team) — an empty successful
  poll proves nothing about whether the team still builds (workflow-building
  only happens when there's new mail), so this must persist until an actual
  successful dispatch (this is the existing, correct F5 behavior from the
  first hardening round — not touched here).

Today both share one field and neither the "successful mailbox check" branch
nor the reviewer's report distinguishes them, so a resolved mailbox outage
still shows "error" until unrelated new mail eventually arrives and
dispatches successfully.

Fix: add a nullable `last_error_kind` column to `email_triggers`
(`"mailbox" | "workflow" | NULL`, guarded Alembic migration matching the
existing `_has_column` pattern in `c9d0e1f2a3b4`). Every site that sets
`last_error` also sets `last_error_kind`:
- credential-decrypt failure, generic poll failure → `"mailbox"`
- workflow-build failure in `_start_triggered_run` → `"workflow"`
- successful dispatch, and the enable API's re-enable → clear both to `None`

After a successful `check_mailbox`, clear `last_error`/`last_error_kind`
**only if** `last_error_kind == "mailbox"`. `NULL`/`"workflow"` are left
alone — conservative by construction: a pre-migration row (kind `NULL`) keeps
today's sticky behavior exactly, and a workflow error still requires an
explicit fix (redeploy the team, re-enable) exactly as F5 intended.

## Fix 4 — trigger env validation (closes finding #5)

`poll_seconds()`/`daily_cap()`/`batch_size()` parse env strings with `float()`/
`int()` and no bounds checking. A non-numeric or non-positive value raises
inside `poll_forever`'s loop body, which only catches `asyncio.TimeoutError` —
the exception is unhandled, the `while True` loop dies, and the `poller` task
created in `main._lifespan` silently stops forever with no supervision or
restart. This kills automatic runs for **every** org on the deployment from
one operator typo, discoverable only by noticing polling stopped.

Fix: `email_trigger.validate_trigger_env()` — called once at `main.py` import
time, next to the existing `BESTTEAM_SECRET_KEY` startup guard — reads each of
`BESTTEAM_TRIGGER_POLL_SECONDS`/`_DAILY_CAP`/`_BATCH_SIZE` (skipping unset
ones, which already have safe defaults) and raises `RuntimeError` naming the
offending variable and value if it's non-numeric or ≤ 0. This turns a silent,
delayed, hard-to-diagnose failure into an immediate, actionable startup
refusal — consistent with the existing guard pattern in this file.

**Explicitly not built:** a watchdog that restarts `poll_forever` if it dies
unexpectedly. After this fix, the only known way the loop could die is
removed; adding supervisory infrastructure for a failure mode with no
remaining known cause is speculative hardening this project's guidelines
explicitly discourage. Noted here so the reviewer sees the reasoning rather
than an unexplained gap.

## Fix 5 — frontend error/loading states (closes finding #8)

`EmailTriggerActivity.jsx` collapses `getEmailTrigger()`/`emailTriggerActivity()`
fetch failures into the same state as "trigger is off" / "no runs yet" —
indistinguishable to the customer, and `last_checked_at` (already in the API
payload) is never rendered.

Fix: track fetch failure separately from "not yet loaded" and from "loaded,
genuinely off":
- `trigger === undefined` (initial) → render nothing (unchanged — avoids a
  loading flash on a page most teams don't use).
- status fetch failed → a distinct error banner.
- loaded, `enabled === false` → render nothing (unchanged, deliberate: a team
  that never turned this on shouldn't see an empty automatic-runs card).
- loaded, enabled → render the card as today, plus a `last_checked_at` line,
  plus an activity-fetch-failure message distinct from "no runs yet."

Add component-level tests (Vitest + Testing Library, matching whatever the
frontend's existing test setup is — confirmed by the implementing subagent)
covering: loading → nothing, status-error → banner, off → nothing,
active → card with `last_checked_at`, activity-error → distinct message.

## Out of scope (confirmed dispositions)

- **#2 (RunRegistry unbounded growth), #6 (shutdown doesn't stop in-flight
  threads):** unchanged from the prior round's explicit "defer as a separate
  hardening follow-up" decision. This review didn't surface anything new
  about either.
- **#7 (sentinel username collision):** no deployment of this codebase can
  have a human `email-trigger` account predating the reservation, since the
  username and its reservation ship in the same commit range. Considered and
  declined — no code change.

## Testing

Each fix task follows red→green TDD (watch the test fail for the right
reason, then the minimal fix). Full backend suite + frontend lint/build must
stay green at every task boundary and at the branch tip.
