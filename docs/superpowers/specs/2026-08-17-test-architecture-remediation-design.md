# Test Architecture Remediation — Design

## Context

A performance assessment of the test suite (16 Aug 2026) started from a
subjective report that "running the tests feels slow" and found that the
felt slowness had almost nothing to do with the test *tiering* introduced by
`2026-08-13-e2e-and-ci-test-tiering-design.md`, and almost everything to do
with two things that tiering cannot fix: a per-test fixed cost that dwarfs
the tests themselves, and a CI signal that has been red on `main` for weeks.

Measured on the local Windows dev box, 1385 backend tests (`-m "not e2e"`):

| Configuration | Wall clock |
|---|---|
| As-is (serial, PBKDF2 260k) | **13m09s** |
| `-n auto` only (pytest-xdist) | 4m24s |
| Cheap PBKDF2 in tests only, still serial | 4m06s |
| Both | **2m45s** |

The headline: **543 of 789 seconds (69%) of the suite is spent computing
PBKDF2 password hashes.** `ui/backend/auth.py` uses
`_PBKDF2_ITERATIONS = 260_000`, deliberately expensive (~0.76s per hash on
this box). Function-scoped fixtures such as `test_org_isolation.py`'s `rig`
register and log in three users per test, which is the entire explanation
for that file's 3.3s-per-test `setup` cost. Nothing about the test logic is
slow; the suite is paying a production security parameter 1400 times.

The secondary finding is that the CI has been failing on `main` for most of
the last month, while the same suite passes locally. The failures are a
small set of genuinely flaky concurrency/isolation tests, not product
regressions. A permanently red CI trains everyone to distrust it and to
re-run the full suite locally instead — which is the most plausible
mechanism by which the felt slowness arose in the first place.

## Goals

- Cut the local and CI backend suite from ~13 minutes to under 3 minutes
  without weakening any assertion or any production security parameter.
- Get CI green on `main` and keep it green: fix the flaky tests rather than
  retrying or muting them.
- Make the test suite safe to run in parallel, and run it that way in CI.
- Stop spending CI on jobs that a given change cannot possibly affect.
- Make `backend-full` earn its place instead of re-running 97.7% of what the
  PR gate already ran.

## Non-goals

- **No per-test-selection engine.** The original request asked whether
  different sizes of change should run different amounts of test. They
  should, but once the suite is under 3 minutes the marginal value of
  mapping changed source files to individual test IDs is small and the
  machinery is a permanent maintenance cost. Change-scope awareness is
  delivered at the *CI job* level (see D6), which captures most of the
  benefit for a fraction of the complexity.
- **No change to what any test asserts.** Flaky tests get fixed at their
  actual race, not by loosening assertions or adding retries.
- **No change to production authentication behaviour.** See D1 — the
  remediation deliberately touches zero production code.
- No revisiting of the marker taxonomy (`unit`/`integration`/`e2e`/`slow`/
  `optional`) or the six-job shape from the 13 Aug design; this revises how
  those jobs are triggered and what `backend-full` runs, not the taxonomy.

## Design

### D1. Cheap password hashing in tests, with zero production surface

The obvious implementation — make the iteration count configurable via an
environment variable — creates a permanent security liability: a production
deployment could be misconfigured to a weak value, and nothing would notice.
That tradeoff is avoidable entirely.

`hash_password` reads the module-level `_PBKDF2_ITERATIONS` at call time, and
`verify_password` reads the iteration count back out of the stored hash
string. So a test process can simply lower the module attribute, and both
halves stay self-consistent. `tests/conftest.py` — which already exists to
neutralise import-time side effects — sets:

```python
from ui.backend import auth
auth._PBKDF2_ITERATIONS = 1_000
```

Properties this buys:

- **Production code is not touched at all.** No new env var, no new config
  key, no new branch in `auth.py`. There is no misconfiguration to make.
- **E2E tests keep the real cost.** `tests/e2e/` drives a `uvicorn`
  subprocess that never imports our `conftest.py`, so the eight e2e
  scenarios continue to exercise genuine 260k-iteration hashing. That is the
  right place to keep it: it is the only tier that tests the real process.
- **The weakening cannot leak.** A new `unit` test asserts that the value
  *as defined in the source module* is still 260_000, so if anyone ever
  lowers the production default this fails loudly. The guard reads the
  literal from `auth.py`'s source rather than the (deliberately patched)
  runtime attribute.

Expected saving: ~543s of 789s, serial.

### D2. `test_marker_completeness` runs one collection, not two

`test_every_item_has_a_ci_marker` costs **54.1s** — 7% of the entire suite
in a single test — because it spawns two full `pytest --collect-only`
subprocesses (~25s each) to learn a selected count and a total count.

pytest's filtered collection summary already reports both:
`"590/1393 tests collected (803 deselected) in 15.28s"`. The existing code
deliberately avoided depending on that form; the fix is to depend on it
*safely* rather than to pay for a second process: parse the `S/T` form when
present, and treat the unfiltered `"N tests collected"` form as `S == T == N`
(which is precisely what it means — nothing was deselected). The existing
`_COLLECTED_RE` already matches both forms, so this is a parsing change, not
a regex change.

The subprocess also gains `-p no:cacheprovider`, so it cannot contend with
the parent process's `.pytest_cache` when the suite runs under xdist (see
D4).

Expected saving: ~27s.

### D3. Fix the flaky tests

Five tests fail intermittently. All were confirmed against real CI logs on
`main` and/or a local `-n auto` run. None is a product regression; each is a
race in the test itself or in a test fixture.

| Test | Symptom | Seen in |
|---|---|---|
| `test_builder_api::test_deploy_mailbox_gate_and_skill_pin_share_one_lock_snapshot` | `KeyError: 'tools'` | CI `backend-full`, local `-n auto` |
| `test_builder_api::test_delete_session_with_in_flight_sandbox_run_does_not_disrupt_the_run` | `assert 200 == 404` | CI `backend-unit-integration` |
| `test_share_chat_api::test_second_visitor_gets_an_isolated_session` | `InvalidRequestError: Could not refresh instance '<ShareSession>'` | CI `backend-full` |
| `test_marker_completeness::test_every_item_has_a_ci_marker` | subprocess collection fails | local `-n auto` |
| `tests/e2e/test_smoke.py::test_smoke_journey` | `Page.goto: Navigation ... interrupted by another navigation to "/login"` | CI `e2e-smoke` |
| `test_crud_api::test_reupload_never_leaves_kb_without_a_live_version` | intermittent | local `-n auto` |
| `test_org_knowledge_bases::test_ingestion_job_status_404_for_another_orgs_job` | intermittent | local `-n auto` |

The e2e one is fully diagnosed. `test_smoke.py:229-230` reads:

```python
page.goto(BASE_URL + "/advanced")
page.wait_for_url("**/login", timeout=5000)
```

The next line states the intent plainly — this navigation is *expected* to be
redirected. But `page.goto()` waits for `load` by default, and the frontend's
auth guard redirects client-side before that fires, so Playwright raises
"interrupted by another navigation". Whether it raises is a pure race between
the redirect and the load event, which is why `e2e-full` passed the same test
in the same CI run that `e2e-smoke` failed it. The fix is to stop waiting for
a `load` that will never arrive, and let the already-present
`wait_for_url` be the real assertion.

Two more surfaced while verifying D4, and only under `-n auto` —
`test_crud_api::test_reupload_never_leaves_kb_without_a_live_version` and
`test_org_knowledge_bases::test_ingestion_job_status_404_for_another_orgs_job`
— which is the clearest possible argument for running the gate in parallel:
nothing else was going to find them before a customer did.

### D3a. The common root cause: `make_engine(":memory:")` shares one transaction

Diagnosis of the two `test_builder_api` failures found a single defect that
explains the whole class, and it is in the harness, not the product.

`make_engine(":memory:")` *has* to use a `StaticPool` — a second SQLite
connection to `:memory:` would be a second, empty database. The consequence is
that one DBAPI connection backs **every** `Session` in the process, so two
concurrent Sessions do not merely share a database, they share a single
transaction:

```
T1 (request A)                  T2 (request B / worker thread)
--------------------------      -------------------------------------
flush()  -> UPDATE/DELETE
                                close() / pool check-in -> ROLLBACK
                                  ... discards A's flushed statements
commit() -> COMMIT (no-op)
  -> endpoint answers 200/204 having written nothing
```

The write is lost **silently** — the endpoint still returns success — so it
never fails where it happened. It surfaces as an unrelated assertion failing
much later, intermittently, which is exactly the shape of all five flakes.
`ui/backend/runtime.py:340` and `ui/backend/ingestion.py:95` both open a
`Session(engine)` on a worker thread, so any test driving a run or an
ingestion while making requests is exposed. Production is not: a file
database's default `QueuePool` gives each Session its own connection and
therefore its own transaction.

D1 made this worse before it made it better — compressing request latency
widened the overlap between request and worker phases.

The fix is one shared helper, `tests/helpers.py::make_concurrent_safe_engine`,
returning a file-backed engine under `tmp_path` with `PRAGMA synchronous=OFF`
(durability buys nothing for a database that dies with the test; transaction
visibility and locking, the entire point, are untouched). Every test file that
can have two Sessions live at once migrates to it.

It is applied well beyond the files with observed failures, because the failure
mode is silent and "no observed flake" is not evidence of safety. But it is not
applied indiscriminately either — a file DB costs fixture churn and a little
time, and some tests depend on the shared connection deliberately.

The first attempt at drawing that line screened files by keyword
(`threading`, `ThreadPoolExecutor`, `run_in_background`, `Thread(`) and missed
`test_share_chat_ws.py`, which CI then failed with `cannot commit - no
transaction is active`. The reliable criterion is a call-graph audit instead:
the backend opens a second `Session` in exactly four places —
`runtime.py:340` (run worker), `ingestion.py:95` (ingestion worker),
`main.py:958` (per-event re-auth in the WS stream loop) and
`share_chat.py:401` — and spawns threads from one, `runtime._executor`, reached
via `POST /api/runs`, the builder's test-runs, `email_trigger` and
`share_chat`. Each test file is then checked against the routes it actually
exercises.

That yields sixteen migrated files and ten deliberately left on `:memory:`,
each with a recorded reason (read-only aggregation, link CRUD, a sync
transcription endpoint, and `test_email_trigger.py`'s intentional dependence
on the shared connection) rather than an absence of observed failures.

The rule for any residual race is unchanged: find the actual window and close
it, never add a sleep, a retry, or a loosened assertion. If one turns out to
be a genuine product bug rather than a test bug, that is a finding to
escalate, not to paper over.

### D4. Run the backend suite in parallel in CI

`pytest-xdist` is added to the `dev` extra. The PR-gate backend job runs
`-n auto`.

Local default behaviour is deliberately unchanged: the 13 Aug design
committed to "local `pytest` with no args stays unfiltered", and putting
`-n auto` into `addopts` would also break `-x`, `--pdb`, and readable
tracebacks for anyone debugging. `-n auto` is documented as an opt-in local
idiom in `CLAUDE.md` instead. After D1 the serial local suite is ~4 minutes,
so the incremental 80 seconds parallelism would buy locally does not justify
degrading the debugging experience by default.

D3 is a hard prerequisite: parallelism is what surfaced two of the five
flaky tests, and turning it on before they are fixed would convert an
occasional red into a frequent one.

### D5. `backend-full` becomes a genuinely different run

Today `backend-full` (`-m "not e2e"`, 1385 tests) re-runs 1353 of the 1353
tests the PR gate already ran, to gain 32. Since GitHub evaluates PR checks
on the merge ref, that is very nearly the same tree twice.

Rather than delete the job, it is repurposed into the one thing the PR gate
will no longer do once D4 lands: **run the whole suite serially, in a single
process, in deterministic order.** That makes it a real test-isolation and
ordering check — the class of bug that xdist's distribution actively hides —
instead of a duplicate. It keeps its `interview` extra and its `-m "not e2e"`
selection; only the absence of `-n auto` and the job's documented purpose
change.

This is a deliberate decision to keep a job that looks redundant. The
justification is that D4 makes the PR gate's execution order nondeterministic,
and something has to still assert the suite is order-independent.

### D6. Path-filtered CI jobs

Every push currently runs all applicable jobs regardless of what changed: a
documentation-only commit spins up six runners, installs Python, Node,
Playwright and Chromium, and runs 1385 backend tests.

A `changes` job using `dorny/paths-filter` publishes boolean outputs
(`backend`, `frontend`, `e2e`, `ci`), and each downstream job gates on the
relevant one. Filters are written to fail safe: any change to
`pyproject.toml`, `.github/workflows/**`, or the filter definition itself
marks *everything* as changed, so an infrastructure edit can never
accidentally skip its own validation.

`main` has no branch protection and therefore no required status checks
(verified via the GitHub API), so skipped jobs cannot block a merge. Should
branch protection be introduced later, the standard remedy is an always-run
aggregating job; that is noted here but not built now.

## Testing / verification

Every claim in this document was produced by measurement, and the
remediation is verified the same way:

- Re-run the full backend suite serially and confirm ~4 minutes and 1385
  passed, with the D1 guard test present and passing.
- Re-run with `-n auto` and confirm ~2m45s and 1385 passed — in particular
  that the two tests which failed under parallelism now pass.
- Run each previously-flaky test in a repeat loop (≥20 iterations, and under
  `-n auto`) to demonstrate the race is actually closed rather than made less
  likely.
- Confirm the e2e suite still passes with real 260k-iteration hashing, which
  is what proves D1 did not silently weaken the tier that runs a real server.
- Push the branch and confirm all PR-gate jobs go green.
- Confirm a docs-only commit skips the backend, frontend and e2e jobs.

## File-level summary of changes

- `tests/conftest.py` — lower `auth._PBKDF2_ITERATIONS` for the test process
- `tests/test_auth.py` — new guard test pinning the production default at 260_000
- `tests/test_marker_completeness.py` — single collection pass; `-p no:cacheprovider`
- `tests/test_builder_api.py` — two flaky concurrency tests fixed
- `tests/test_share_chat_api.py` — one flaky session-isolation test fixed
- `tests/e2e/test_smoke.py` — redirect-race fix
- `pyproject.toml` — `pytest-xdist` added to the `dev` extra
- `.github/workflows/ci.yml` — `changes` path-filter job; `-n auto` on the PR
  gate; `backend-full` documented and kept serial
- `CLAUDE.md` — document the `-n auto` local idiom and the conftest hashing patch
