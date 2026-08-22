# Email automation — one mailbox factory, orphan claims, and mailbox-scoped claims (design)

Date: 2026-08-22
Status: approved, implementation in this branch
  (`fix/email-poller-oauth-and-claim-scoping`)
Prompted by: an external architecture review of the email capability, which
raised three P0 correctness items. Each was re-verified against the code before
being accepted; one of the three was materially overstated and is narrowed here.

## Context

The review's verdict — "controlled trial-run ready, single-instance low-
concurrency beta, not a high-availability commercial SaaS promise" — matches
what `docs/STATUS.md` already says about this subsystem. Its three P0s are
about *data correctness*, not scale, which is why they are worth fixing now
rather than at the Postgres/queue stage the review's later phases describe.

What this design does **not** do, and why: lease-based claims, a transactional
outbox, a persistent queue, leader election, Postgres, KMS envelope encryption,
RBAC and a `Case`/SLA model are all out of scope. Two of them conflict with
rulings already taken (no Postgres before GA; no `Case`/work-item entity in
Phase 1 — `docs/DECISIONS.md`), and the rest are multi-process concerns on a
poller that is deliberately single-process (`docs/STATUS.md`: horizontal
scale-out is blocked on the SQLite→Postgres migration, not on this module).
A lease adds a second liveness authority to a process that already *is* the
liveness authority.

## The three defects, as verified

### 1. The poller builds its IMAP backend with a second, OAuth-blind factory

`email_trigger._make_backend` (`ui/backend/email_trigger.py`) ignores
`OrgEmailCredential.auth_type` and always passes `password=`. For a
`microsoft_oauth` credential, `password_encrypted` holds the Entra **client
secret**, so the poller attempts an IMAP `LOGIN` with a client secret as the
password. Both automatic paths use it: `poll_org` and `retry_triggered_run`.

`email_tools.build_org_imap_backend` is the correct implementation and is what
the manual tools use. `org_settings._backend_for` is a *third* construction of
the same object, whose docstring says it is "deliberately kept in step with"
the second one — a comment-enforced invariant, which is exactly what failed
here. Phase 2's own note in `ui/backend/CLAUDE.md` ("Nothing in
`email_trigger.py` changed") records the assumption that produced the gap: the
design believed the poller went through the shared factory.

Consequences, precisely: an M365 org can save credentials, pass the connection
test and run the manual tools, while every automatic poll and every automatic
retry fails authentication. It is **not silent** — the failure surfaces as a
`mailbox`-kind `last_error` and a health alert — but M365 automation is
entirely non-functional, which is the whole product for that customer.

Why no test caught it: `tests/test_email_trigger.py`'s autouse `offline_backend`
fixture replaces `_make_backend` for the entire module, and the one test that
exercises the real factory (`test_make_backend_keeps_ssrf_validation_on`)
asserts only the password-shaped kwargs.

### 2. A claim can outlive the process before its run row exists

`runtime.fail_interrupted_runs`, called from `main._lifespan`, already resolves
every `running` run to `failed` and calls `release_events` for it — so the
review's claim that startup recovery ignores email runs is **wrong** (the
`main.py` line it cites is the one-member-per-org guard).

The real window is narrower and does exist. `_start_triggered_run` commits the
claim on its own (so a build failure can release it penalty-free), then builds
the pipeline, and only then writes the `runs` row:

```
claim_events(...); db.commit()      # rows are `claimed`, run_id set
pipeline = get_pipeline(...)        # <-- process killed here
db.add(run_row); db.commit()
```

A kill inside the build leaves `claimed` rows whose `run_id` names a `runs` row
that was never inserted. `fail_interrupted_runs` selects `Run.status ==
"running"`, so it cannot see them, and nothing else ever will: they are
`claimed` forever, invisible to `claim_events` and to `has_pending_events`.
The mail is not lost at the IMAP layer, but it is never processed, which is the
same outcome for the customer.

### 3. Claims are scoped to the org, not to the mailbox or its generation

`claim_events` and `has_pending_events` filter on `org_id` and `status` alone.
The mailbox identity and generation are recorded on every row and read by
nothing.

The reachable sequence: an org's mailbox is replaced (or rebuilt, changing
UIDVALIDITY) while `pending` rows from the previous mailbox/generation remain.
`disable_trigger_on_identity_change` disables the trigger and the UIDVALIDITY
branch re-baselines the cursor, so no *new* wrong rows are created — but the old
ones survive. Once automation is re-enabled, the first cycle with no new mail
reaches `has_pending_events`, finds them, and claims them: the run's
`trigger_context` then carries the **new** mailbox identity with **old** UIDs.

Severity, stated accurately: this is **not** cross-tenant. `org_id` still
isolates, and an org has at most one mailbox. Against a genuinely different
mailbox the stale UIDs will usually not resolve (`not_found`). The case that
does real damage is a rebuilt or migrated mailbox where UIDs were reissued:
UID 7 exists, is a different message, and gets read and possibly replied to.
The review's "cross-mailbox data confusion" holds; "cross-organisation" does
not.

## Design

### D1. One credential→connector implementation, three call sites

Two primitives in `email_tools.py`, and every path uses both:

```python
def token_provider_for(auth_type, *, tenant_id, client_id, client_secret):
    """None for a password mailbox; the M365 app-only provider otherwise."""

def build_imap_backend(*, host, username, port, drafts,
                       password=None, token_provider=None):
    """_ImapBackend with restrict_to_public=True — the ONE place it is set."""
```

and one convenience over a stored row, which is what the poller needs because
it has already fetched and decrypted:

```python
def build_backend_for_credential(cred, secret):
```

`build_org_imap_backend(db, org_id)` becomes fetch + decrypt +
`build_backend_for_credential`. `email_trigger._make_backend` is **deleted**;
`poll_org` and `retry_triggered_run` call `build_backend_for_credential(cred,
password)` with what they already hold — no second credential read.
`org_settings._token_provider_for`/`_backend_for` delegate to the two
primitives, keeping their existing shape (that module fetches the token as its
own step, on purpose, so a credential problem stays distinguishable from a
mailbox-access problem — that ordering is preserved).

`restrict_to_public=True` now has exactly one site, so the SSRF guard cannot be
dropped from one path while the others keep it.

**Why `org_settings` is included** even though it is not the defect: it is the
third copy of the same decision, and the second copy is what just broke. A
factory that two of three callers use is not a factory.

### D2. Startup releases every orphaned claim

`fail_interrupted_runs` gains a second sweep, after its existing loop and
commit: every remaining `claimed` `inbox_events` row is orphaned **by
definition** — the run executor is per-process, so no live worker can own one
at startup, and the loop above has already released the claims of every run
that had a row. The sweep groups the survivors by `run_id` and hands each to
the same `release_events` the loop uses (so `max_event_attempts` and
dead-lettering behave identically, and a row whose `run_id` names no `runs` row
at all is handled by the same code — `release_events` keys on the id string
only).

This is deliberately not a lease: the process boundary *is* the lease, and a
periodic scavenger would be a second liveness authority inside a single-process
poller. `_release_stale_run` already covers the other case (a run that is alive
but hung).

### D3. Claims are scoped to the current mailbox and generation

`claim_events` and `has_pending_events` take `mailbox_identity` and
`mailbox_generation` as **required** keyword arguments and filter on them,
alongside `org_id` and `status`. Required rather than optional: the defect is
that a caller could omit the mailbox, so the signature is what should refuse.
`poll_org` and `_start_triggered_run` pass exactly the values `record_events`
already stamps — `mailbox_identity(cred.host, cred.username)` and
`str(trigger.uidvalidity)` — from the credential the cycle already resolved.

The composite index `ix_inbox_events_org_id_status_id` still leads the query;
identity and generation are low-cardinality residual filters within one org's
rows. No migration and no schema change: both columns already exist and are
already populated on every row.

### D4. Superseded rows are abandoned, not left inert

The filter alone makes an old-generation row unclaimable, but it would then sit
`pending` forever with nothing reporting it. One helper marks them terminal:

```python
def abandon_superseded_events(db, *, org_id, mailbox_identity,
                              mailbox_generation=None) -> int
```

It sets every `pending` row for the org that does **not** match the current
mailbox (and, when a generation is given, the current generation) to `failed`
with a fixed `last_error`. It is expressed as "everything that is not the
current mailbox" rather than "everything that was the old one", so it needs no
knowledge of what the previous identity was and is self-correcting if a change
was ever missed.

Two call sites, the two moments a generation changes:

- `disable_trigger_on_identity_change`, when host/username actually changed —
  generation omitted, because the new mailbox's UIDVALIDITY is not known yet.
- `poll_org`'s UIDVALIDITY re-baseline branch — both values known.

`claimed` rows are deliberately left alone: they belong to a run that will
either complete or be released by the watchdog or D2, and racing that from here
would give one batch two owners.

**Customer notice, asymmetric on purpose.** The UIDVALIDITY branch also writes
`trigger.last_error` naming the count — a mailbox rebuild is a surprise the
customer did not cause, and dropped mail must not be silent. The
identity-change site only logs: the customer just replaced the mailbox
themselves, and the trigger is being disabled in the same breath, so an error
banner on a switch they just flipped is noise.

## What this leaves open (recorded, not fixed)

- **G7 is still required.** No test in CI can prove Exchange Online accepts the
  resulting `AUTHENTICATE XOAUTH2`; D1 makes the poller *use* the OAuth path,
  it does not verify Microsoft accepts it. `docs/email-smoke-test.md` §9 must
  be run against a live tenant before the first M365 customer.
- **The claim→run-row window still exists**; D2 recovers from it at startup
  rather than closing it. Closing it means holding the SQLite write lock across
  the whole pipeline build, which is the pattern `ingestion.py` documents as
  the reason it buffers and commits once.
- **Multi-worker is still unsupported** (`RunRegistry`, `_dispatch_lock`), and
  nothing here changes that.
- **One ordering leaves inert rows behind.** D4 runs at the moment a mailbox
  changes, and it deliberately skips `claimed` rows (they have an owner). So if
  a process dies holding a claim, the mailbox is *then* replaced, and only then
  does the process restart, D2 releases those rows to `pending` under the old
  identity — after the abandonment site has already run. They are unclaimable
  (D3 is what matters, and it holds) and invisible, but nothing will ever mark
  them terminal.

  Left as-is on purpose. Closing it costs a new write site: either an
  `abandon_superseded_events` call on every poll cycle, to tidy rows that are
  normally absent, or one on the trigger's re-enable path, which is a third
  place that has to know the mailbox. Neither buys a behaviour change — only
  the tidiness of rows no query returns. Recorded here so it is not
  rediscovered as a bug.
