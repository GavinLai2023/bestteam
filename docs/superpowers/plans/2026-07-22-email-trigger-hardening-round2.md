# Email trigger hardening, round 2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 5 real findings from an independent reviewer's second pass on
PR #22 (`feature/email-trigger-autonomous-runs`): the mailbox-replacement
race + operator-CLI shutdown gap, an unguarded dispatch-submission failure,
conflated mailbox/workflow error health, unvalidated trigger env values, and
a frontend that hides fetch failures.

**Architecture:** Six small, independently-testable backend/frontend/docs
changes on the existing branch, each closing exactly one finding (or, for
finding #1, its one same-cycle-race root cause plus its one CLI-parity gap
together, since they're the same defect surfaced twice). No new
infrastructure (no locking subsystem, no watchdog, no frontend test runner) —
see the design spec's "explicitly not built" notes for why each of those was
rejected as disproportionate.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2.0 / Alembic (backend),
React + Vite (frontend), pytest, `python -m ui.backend.admin` (operator CLI).

## Global Constraints

- Every commit must keep the full backend suite green:
  `.\.venv\Scripts\python.exe -m pytest`
- Frontend changes must keep `npm run lint` and `npm run build` green (run
  from `ui/frontend/`).
- No behavior change to anything not named in a task below (Surgical
  Changes — see root `CLAUDE.md`).
- Customer-facing error copy never leaks internals (env var names, OS error
  codes, tracebacks) — matches the existing pattern throughout
  `email_trigger.py`/`email_trigger_api.py`.
- All commits land on `feature/email-trigger-autonomous-runs` (extends the
  open PR #22 — this is the branch the reviewer examined).
- Design spec: `docs/superpowers/specs/2026-07-22-email-trigger-hardening-round2-design.md`.

---

### Task 1: Close the mailbox-replacement race (finding #1)

Two changes to the same root cause: (a) `poll_org` currently fetches
credentials once for `check_mailbox`, then `_start_triggered_run` →
`build_trigger_workflow` → `build_org_imap_backend` fetches them **again**
independently — if a credential change lands between those two fetches, a
run can detect mail on the old mailbox but read/draft against the new one.
(b) `org_settings.py`'s self-service `set_email`/`delete_email` disable an
enabled trigger on mailbox change/disconnect; `admin.py`'s `set-email`/
`clear-email` (the operator CLI path) don't.

**Files:**
- Modify: `ui/backend/email_trigger.py`
- Modify: `ui/backend/org_settings.py`
- Modify: `ui/backend/admin.py`
- Test: `tests/test_email_trigger.py`
- Test: `tests/test_admin_cli.py`

**Interfaces:**
- Produces: `email_trigger.disable_trigger(db, org_id) -> None`,
  `email_trigger.disable_trigger_on_identity_change(db, org_id, new_host, new_username, prior_identity) -> None`
  (`prior_identity` is `tuple[str, str] | None`) — used by `org_settings.py`,
  `admin.py`, and later tasks in this plan.
- Produces: `email_trigger.build_trigger_workflow(name, db, org_id, allowed_uids, backend)`
  — `backend` is now a **required** 5th positional/keyword argument (was 4
  args before this task). Any `get_workflow`-shaped callable used with
  `poll_org`/`_start_triggered_run` (including test fakes) must accept it.

- [ ] **Step 1: Write the failing tests**

Open `tests/test_email_trigger.py`. Update the four existing fake
`get_workflow` callables to accept the new `backend` parameter (they don't
need to use it), and add a new regression test proving the same backend
object flows through both calls.

Replace:
```python
def _no_workflow(name, db, org_id, allowed_uids):  # must NOT be called
    raise AssertionError("build_trigger_workflow should not be called in this test")
```
with:
```python
def _no_workflow(name, db, org_id, allowed_uids, backend):  # must NOT be called
    raise AssertionError("build_trigger_workflow should not be called in this test")
```

Replace:
```python
def _fake_workflow_getter(calls):
    def build(name, db, org_id, allowed_uids):
        calls.append((name, org_id, set(allowed_uids)))
        return object()
    return build
```
with:
```python
def _fake_workflow_getter(calls):
    def build(name, db, org_id, allowed_uids, backend):
        calls.append((name, org_id, set(allowed_uids)))
        return object()
    return build
```

In `test_poll_org_build_failure_advances_nothing`, replace:
```python
    def _boom(name, db_, org_id, allowed_uids):
        raise ValueError("No team named 'triage'")
```
with:
```python
    def _boom(name, db_, org_id, allowed_uids, backend):
        raise ValueError("No team named 'triage'")
```

In `test_poll_org_workflow_load_failure_recorded_not_raised`, replace:
```python
    def _missing(name, db_, org_id, allowed_uids):
        raise Exception("Unknown workflow 'triage'")
```
with:
```python
    def _missing(name, db_, org_id, allowed_uids, backend):
        raise Exception("Unknown workflow 'triage'")
```

Then add this new test right after `test_poll_org_new_mail_starts_one_run`:
```python
def test_poll_org_reuses_the_same_backend_for_workflow_build(db, monkeypatch):
    # Fix 1: poll_org must not let workflow-building re-fetch credentials --
    # a credential change mid-cycle must not produce a run that detects mail
    # on one mailbox and builds tools against another.
    org, trigger = _org_with_trigger(db, last_uid=41)
    seen_check_backends = []

    def fake_check_mailbox(backend, last_uid):
        seen_check_backends.append(backend)
        return (3, 45, [42, 45])

    monkeypatch.setattr(email_trigger, "check_mailbox", fake_check_mailbox)
    recorder = _SubmitRecorder()
    monkeypatch.setattr(email_trigger, "_executor", recorder)

    seen_workflow_backends = []

    def build(name, db_, org_id, allowed_uids, backend):
        seen_workflow_backends.append(backend)
        return object()

    poll_org(db, trigger, build)

    assert len(seen_check_backends) == 1
    assert len(seen_workflow_backends) == 1
    assert seen_workflow_backends[0] is seen_check_backends[0]
```

Now open `tests/test_admin_cli.py` and add these three tests at the end of
the file (after `test_set_email_test_flag_rejects_bad_login`):
```python
# ---------------------------------------------------------------------------
# Trigger disable on mailbox change (mirrors org_settings.py's coverage,
# applied to the operator CLI path -- finding #1's CLI-parity gap)
# ---------------------------------------------------------------------------

def test_set_email_disables_trigger_on_host_change(session_local, secrets_key, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
    from ui.backend.db.orgs import get_org_by_name

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)

    admin_cli.main(["set-email", "acme", "--host", "imap.other.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        assert get_email_trigger(db, org.id).enabled is False


def test_set_email_keeps_trigger_enabled_on_password_only_rotation(session_local, secrets_key, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
    from ui.backend.db.orgs import get_org_by_name

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch, "old-pw")
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)

    _patch_password(monkeypatch, "new-pw")
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        assert get_email_trigger(db, org.id).enabled is True


def test_clear_email_disables_trigger(session_local, secrets_key, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
    from ui.backend.db.orgs import get_org_by_name

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)

    admin_cli.main(["clear-email", "acme"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        assert get_email_trigger(db, org.id).enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py tests/test_admin_cli.py -v`
Expected: the updated/new tests FAIL — the signature-mismatch tests fail
with `TypeError` (too many/too few args passed to the fake callables once
step 3 changes `poll_org`'s call site... actually before step 3 they'll fail
because `_start_triggered_run`/`build_trigger_workflow` still call with 4
args while the fakes now require 5), and the three new admin CLI tests fail
with `AttributeError`/assertion failures since `admin.py` has no trigger-
disable logic yet.

- [ ] **Step 3: Implement**

In `ui/backend/email_trigger.py`:

Remove the now-unused import (line ~36):
```python
from .email_tools import build_org_imap_backend
```

Add this import alongside the other `.db.*` imports:
```python
from .db.email_triggers import get_email_trigger
```

Add these two functions right before `def poll_org(...)`:
```python
def disable_trigger(db: Session, org_id: int) -> None:
    """Disable an org's trigger if it's currently enabled (mailbox cleared or
    replaced). Shared by the self-service API (org_settings.py) and the
    operator CLI (admin.py) so both mailbox-change paths behave the same."""
    trigger = get_email_trigger(db, org_id)
    if trigger is not None and trigger.enabled:
        trigger.enabled = False
        db.commit()


def disable_trigger_on_identity_change(
    db: Session,
    org_id: int,
    new_host: str,
    new_username: str,
    prior_identity,
) -> None:
    """disable_trigger(...) iff `prior_identity` (a prior (host, username)
    tuple, or None if there was no prior mailbox) differs from the new one. A
    port-only or password-only change is a rotation, not a replacement, and
    leaves the trigger enabled."""
    if prior_identity is not None and prior_identity != (new_host, new_username):
        disable_trigger(db, org_id)
```

Replace `build_trigger_workflow`:
```python
def build_trigger_workflow(name: str, db: Session, org_id: int, allowed_uids):
    """An UNCACHED workflow for (org_id, name) whose email tools are confined to
    `allowed_uids`. Mirrors main._get_workflow's DB-record build but substitutes
    scoped email tools; not cached because the UID set is per-run. Raises on a
    missing/invalid team or a missing mailbox connection."""
    record = db.query(WorkflowRecord).filter_by(name=name, org_id=org_id).one_or_none()
    if record is None:
        raise ValueError(f"No team named '{name}' for org {org_id}")
    backend = build_org_imap_backend(db, org_id)
    if backend is None:
        raise ValueError("No mailbox is connected for this org")
    source = _WORKFLOWS_DIR / f"{name}.yaml"
    kb_tools = load_knowledge_base_tools(db, record.config, source, org_id=org_id)
    email_tools = make_email_tools(backend, allowed_uids=allowed_uids)
    skills = load_skills(db, org_id)
    config = contain_workflow_config_for_load(record.config)
    ensure_workflow_cache_paths_for_source(config, source)
    return _build_workflow(
        config,
        source=source,
        extra_tools={**kb_tools, **email_tools},
        extra_skills=skills,
    )
```
with:
```python
def build_trigger_workflow(name: str, db: Session, org_id: int, allowed_uids, backend):
    """An UNCACHED workflow for (org_id, name) whose email tools are confined to
    `allowed_uids`. Mirrors main._get_workflow's DB-record build but substitutes
    scoped email tools; not cached because the UID set is per-run. `backend` is
    the IMAP backend the caller already resolved for this poll cycle -- reused
    rather than re-fetched, so a mailbox swap mid-cycle can't produce a run
    that detects mail on one mailbox and reads/drafts on another. Raises on a
    missing/invalid team."""
    record = db.query(WorkflowRecord).filter_by(name=name, org_id=org_id).one_or_none()
    if record is None:
        raise ValueError(f"No team named '{name}' for org {org_id}")
    source = _WORKFLOWS_DIR / f"{name}.yaml"
    kb_tools = load_knowledge_base_tools(db, record.config, source, org_id=org_id)
    email_tools = make_email_tools(backend, allowed_uids=allowed_uids)
    skills = load_skills(db, org_id)
    config = contain_workflow_config_for_load(record.config)
    ensure_workflow_cache_paths_for_source(config, source)
    return _build_workflow(
        config,
        source=source,
        extra_tools={**kb_tools, **email_tools},
        extra_skills=skills,
    )
```

In `_start_triggered_run`, change the signature and the `get_workflow` call:
```python
def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow) -> None:
```
to:
```python
def _start_triggered_run(db: Session, trigger: EmailTrigger, new_uids, get_workflow, backend) -> None:
```
and:
```python
        workflow = get_workflow(trigger.workflow_name, db, trigger.org_id, set(batch))
```
to:
```python
        workflow = get_workflow(trigger.workflow_name, db, trigger.org_id, set(batch), backend)
```

In `poll_org`, change the final call:
```python
    _start_triggered_run(db, trigger, new_uids, get_workflow)
```
to:
```python
    _start_triggered_run(db, trigger, new_uids, get_workflow, backend)
```
(`backend` is already a local variable in `poll_org`, built earlier in the
same function for `check_mailbox`.)

In `ui/backend/org_settings.py`, replace the import:
```python
from .db.email_triggers import get_email_trigger
```
with:
```python
from .email_trigger import disable_trigger, disable_trigger_on_identity_change
```

Replace `set_email`'s trailing block:
```python
    if prior_identity is not None and prior_identity != (req.host, req.username):
        trigger = get_email_trigger(db, org.id)
        if trigger is not None and trigger.enabled:
            trigger.enabled = False
            db.commit()
    return {"connected": True, "host": req.host, "username": req.username}
```
with:
```python
    disable_trigger_on_identity_change(db, org.id, req.host, req.username, prior_identity)
    return {"connected": True, "host": req.host, "username": req.username}
```

Replace `delete_email`:
```python
@router.delete("/email", status_code=204)
def delete_email(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
):
    """Disconnect the org's mailbox."""
    clear_email_credentials(db, org.id)
    trigger = get_email_trigger(db, org.id)
    if trigger is not None and trigger.enabled:
        trigger.enabled = False
        db.commit()
```
with:
```python
@router.delete("/email", status_code=204)
def delete_email(
    db: Session = Depends(get_db), org: Organization = Depends(get_current_org)
):
    """Disconnect the org's mailbox."""
    clear_email_credentials(db, org.id)
    disable_trigger(db, org.id)
```

In `ui/backend/admin.py`, add to the imports:
```python
from .db.email_credentials import clear_email_credentials, set_email_credentials
```
becomes:
```python
from . import email_trigger
from .db.email_credentials import clear_email_credentials, get_email_credentials, set_email_credentials
```

In the `set-email` command handler, insert a `prior_identity` capture before
the `set_email_credentials` call and a disable call after it:
```python
        if args.command == "set-email":
            org = get_org_by_name(db, args.org)
            if org is None:
                parser.error(f"Unknown organization '{args.org}'. Create it first with create-org.")
            password = _prompt_password(parser)
            if args.test:
```
stays the same through the `--test` block; then replace:
```python
            try:
                set_email_credentials(
                    db, org.id, host=args.host, username=args.user, password=password,
                    port=args.port, drafts_folder=args.drafts,
                )
            except Exception as exc:  # noqa: BLE001 -- surface a clear CLI error (e.g. missing key)
                parser.error(str(exc))
            print(f"Connected mailbox '{args.user}' for organization '{args.org}'.")
            return 0
```
with:
```python
            prior = get_email_credentials(db, org.id)
            prior_identity = (prior.host, prior.username) if prior is not None else None
            try:
                set_email_credentials(
                    db, org.id, host=args.host, username=args.user, password=password,
                    port=args.port, drafts_folder=args.drafts,
                )
            except Exception as exc:  # noqa: BLE001 -- surface a clear CLI error (e.g. missing key)
                parser.error(str(exc))
            email_trigger.disable_trigger_on_identity_change(
                db, org.id, args.host, args.user, prior_identity
            )
            print(f"Connected mailbox '{args.user}' for organization '{args.org}'.")
            return 0
```

Replace the `clear-email` command handler:
```python
        if args.command == "clear-email":
            org = get_org_by_name(db, args.org)
            if org is None:
                parser.error(f"Unknown organization '{args.org}'.")
            removed = clear_email_credentials(db, org.id)
            print(
                f"Disconnected mailbox for '{args.org}'." if removed
                else f"No mailbox was connected for '{args.org}'."
            )
            return 0
```
with:
```python
        if args.command == "clear-email":
            org = get_org_by_name(db, args.org)
            if org is None:
                parser.error(f"Unknown organization '{args.org}'.")
            removed = clear_email_credentials(db, org.id)
            email_trigger.disable_trigger(db, org.id)
            print(
                f"Disconnected mailbox for '{args.org}'." if removed
                else f"No mailbox was connected for '{args.org}'."
            )
            return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py tests/test_admin_cli.py tests/test_org_settings.py -v`
Expected: all PASS, including the pre-existing `test_password_rotation_keeps_trigger_enabled` /
`test_mailbox_host_change_disables_trigger` / `test_disconnect_disables_trigger`
in `test_org_settings.py` (unchanged behavior, now delegated to the shared helpers).

- [ ] **Step 5: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all PASS (no regressions elsewhere).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/email_trigger.py ui/backend/org_settings.py ui/backend/admin.py tests/test_email_trigger.py tests/test_admin_cli.py
git commit -m "fix(trigger): one backend per poll cycle, operator CLI disables trigger too"
```

---

### Task 2: Separate mailbox vs. workflow error health (finding #4)

`last_error` currently serves both a mailbox-connectivity signal and a
workflow/dispatch-fault signal. A successful mailbox check is proof the
former is resolved but proves nothing about the latter (workflow-building
only happens when there's new mail) — today neither is distinguished, so a
resolved mailbox outage still shows "error" indefinitely.

**Files:**
- Create: `alembic/versions/e85b2230b950_add_email_trigger_error_kind.py`
- Modify: `ui/backend/db/models.py`
- Modify: `ui/backend/email_trigger.py`
- Modify: `ui/backend/email_trigger_api.py`
- Test: `tests/test_email_trigger.py`

**Interfaces:**
- Consumes: nothing new from Task 1.
- Produces: `EmailTrigger.last_error_kind: str | None` (`"mailbox"` |
  `"workflow"` | `None`) — read by later tasks (Task 3 sets it to
  `"workflow"` on a dispatch-submission failure).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_email_trigger.py`, right after
`test_poll_org_mailbox_failure_stores_friendly_error`:
```python
def test_poll_org_mailbox_error_clears_on_next_successful_check(db, monkeypatch):
    # A resolved mailbox outage must not keep showing "error" forever -- only
    # a *workflow*-kind error must survive an empty poll (F5, unchanged).
    org, trigger = _org_with_trigger(db, last_uid=41)

    def _fail(backend, last_uid):
        raise OSError("[WinError 10060] connection attempt failed")

    monkeypatch.setattr(email_trigger, "check_mailbox", _fail)
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is not None
    assert trigger.last_error_kind == "mailbox"

    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 41, []))
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is None
    assert trigger.last_error_kind is None


def test_poll_org_workflow_error_kind_survives_empty_poll(db, monkeypatch):
    org, trigger = _org_with_trigger(db, last_uid=41)
    trigger.last_error = "Couldn't start the team 'triage' -- it may have been removed."
    trigger.last_error_kind = "workflow"
    db.commit()
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 41, []))  # no new mail
    poll_org(db, trigger, _no_workflow)
    assert trigger.last_error is not None
    assert trigger.last_error_kind == "workflow"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -k error_kind -v`
Expected: FAIL with `AttributeError: 'EmailTrigger' object has no attribute 'last_error_kind'`.

- [ ] **Step 3: Implement**

In `ui/backend/db/models.py`, in the `EmailTrigger` class, insert a new
column right after `last_error`:
```python
    last_error: Mapped[Optional[str]] = mapped_column(nullable=True)
```
becomes:
```python
    last_error: Mapped[Optional[str]] = mapped_column(nullable=True)
    # "mailbox" (connectivity/credentials -- auto-clears on the next
    # successful check) | "workflow" (dispatch/build fault -- persists until
    # a real successful dispatch, F5) | None (no error, or a pre-migration
    # row whose kind is unknown -- treated conservatively as sticky).
    last_error_kind: Mapped[Optional[str]] = mapped_column(nullable=True)
```

Create `alembic/versions/e85b2230b950_add_email_trigger_error_kind.py`:
```python
"""add email_triggers.last_error_kind (mailbox vs workflow health)

Revision ID: e85b2230b950
Revises: f2a3b4c5d6e7
Create Date: 2026-07-22 09:00:00.000000

Separates a mailbox-connectivity fault (auto-clears on the next successful
check) from a workflow/dispatch fault (persists until a real successful
dispatch). NULL on existing rows -- treated as sticky (today's behavior),
same as an unrecognized kind.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the column when
this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e85b2230b950'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "email_triggers", "last_error_kind"):
        op.add_column("email_triggers", sa.Column("last_error_kind", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema (drops the mailbox/workflow error distinction)."""
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "email_triggers", "last_error_kind"):
        op.drop_column("email_triggers", "last_error_kind")
```

In `ui/backend/email_trigger.py`, add constants right after `TRIGGER_USERNAME`:
```python
TRIGGER_USERNAME = "email-trigger"
```
becomes:
```python
TRIGGER_USERNAME = "email-trigger"

_ERROR_KIND_MAILBOX = "mailbox"
_ERROR_KIND_WORKFLOW = "workflow"
```

In `poll_org`, update both failure branches. Replace:
```python
    except (InvalidToken, secret_store.SecretsKeyError) as exc:
        _logger.warning("email trigger: cannot decrypt credentials for org %s: %s",
                        trigger.org_id, exc)
        trigger.last_error = (
            "The mailbox connection can't be read right now -- reconnect it to "
            "resume automatic runs."
        )
        trigger.last_checked_at = _utcnow()
        db.commit()
        return
    except Exception as exc:  # noqa: BLE001 -- a poll failure must never kill the loop
        _logger.warning("email trigger: poll failed for org %s: %s", trigger.org_id, exc)
        trigger.last_error = _friendly_poll_error(exc)
        trigger.last_checked_at = _utcnow()
        db.commit()
        return
```
with:
```python
    except (InvalidToken, secret_store.SecretsKeyError) as exc:
        _logger.warning("email trigger: cannot decrypt credentials for org %s: %s",
                        trigger.org_id, exc)
        trigger.last_error = (
            "The mailbox connection can't be read right now -- reconnect it to "
            "resume automatic runs."
        )
        trigger.last_error_kind = _ERROR_KIND_MAILBOX
        trigger.last_checked_at = _utcnow()
        db.commit()
        return
    except Exception as exc:  # noqa: BLE001 -- a poll failure must never kill the loop
        _logger.warning("email trigger: poll failed for org %s: %s", trigger.org_id, exc)
        trigger.last_error = _friendly_poll_error(exc)
        trigger.last_error_kind = _ERROR_KIND_MAILBOX
        trigger.last_checked_at = _utcnow()
        db.commit()
        return
```

Replace the successful-check comment block:
```python
    trigger.last_checked_at = _utcnow()
    # NOTE: do NOT clear last_error here -- a workflow fault must persist across
    # empty polls (F5). last_error is cleared only on a successful dispatch
    # (below) or on (re-)enable (the API).
```
with:
```python
    trigger.last_checked_at = _utcnow()
    # A successful mailbox check is direct proof connectivity/credentials are
    # fine, so a *mailbox*-kind error can auto-clear here. A *workflow*-kind
    # error (or a legacy/unknown-kind row) must persist across empty polls
    # (F5) -- an empty poll never rebuilds the workflow, so it proves nothing
    # about whether the team still builds. Cleared only on a successful
    # dispatch (below) or on (re-)enable (the API).
    if trigger.last_error_kind == _ERROR_KIND_MAILBOX:
        trigger.last_error = None
        trigger.last_error_kind = None
```

In `_start_triggered_run`'s build-failure branch, replace:
```python
    except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since enabling
        _logger.warning("email trigger: cannot build workflow %r for org %s: %s",
                        trigger.workflow_name, trigger.org_id, exc)
        trigger.last_error = (
            f"Couldn't start the team '{trigger.workflow_name}' -- it may have "
            "been changed or removed. Re-enable automatic runs from its page."
        )
        db.commit()  # NB: last_uid / runs_today deliberately NOT advanced
        return
```
with:
```python
    except Exception as exc:  # noqa: BLE001 -- team deleted/invalid since enabling
        _logger.warning("email trigger: cannot build workflow %r for org %s: %s",
                        trigger.workflow_name, trigger.org_id, exc)
        trigger.last_error = (
            f"Couldn't start the team '{trigger.workflow_name}' -- it may have "
            "been changed or removed. Re-enable automatic runs from its page."
        )
        trigger.last_error_kind = _ERROR_KIND_WORKFLOW
        db.commit()  # NB: last_uid / runs_today deliberately NOT advanced
        return
```

Still in `_start_triggered_run`, replace:
```python
    trigger.last_error = None  # a run is going out: clear any prior fault
    db.commit()
```
with:
```python
    trigger.last_error = None  # a run is going out: clear any prior fault
    trigger.last_error_kind = None
    db.commit()
```

In `ui/backend/email_trigger_api.py`'s `set_trigger`, replace:
```python
    # This enable just proved the mailbox reachable -- don't let a stale poll
    # failure keep reporting "error" until the next cycle clears it.
    trigger.last_error = None
    db.commit()
    return _payload(trigger)
```
with:
```python
    # This enable just proved the mailbox reachable -- don't let a stale poll
    # failure keep reporting "error" until the next cycle clears it.
    trigger.last_error = None
    trigger.last_error_kind = None
    db.commit()
    return _payload(trigger)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -v`
Expected: all PASS, including the pre-existing
`test_poll_org_workflow_error_survives_empty_poll` (its trigger row never
sets `last_error_kind`, so it defaults to `None` — the conservative,
unchanged sticky behavior — no edit needed to that test).

- [ ] **Step 5: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/e85b2230b950_add_email_trigger_error_kind.py ui/backend/db/models.py ui/backend/email_trigger.py ui/backend/email_trigger_api.py tests/test_email_trigger.py
git commit -m "fix(trigger): separate mailbox vs workflow error health (last_error_kind)"
```

---

### Task 3: Guard the dispatch-submission failure (finding #3)

`_start_triggered_run` commits the durable run row, UID baseline, and cap
advance, then calls `_executor.submit(...)` with no failure handling. If
`submit` raises synchronously, both the registry entry and the `Run` row are
left `"running"` forever, and the overlap guard then blocks every later poll
for that org. (A process crash between commit and submit is *not* a wedge —
the in-memory registry is empty after restart, so the guard already
self-recovers; this task only closes the submit-raises case.)

**Files:**
- Modify: `ui/backend/email_trigger.py`
- Test: `tests/test_email_trigger.py`

**Interfaces:**
- Consumes: `EmailTrigger.last_error_kind` (Task 2).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_email_trigger.py`, right after
`test_poll_org_new_mail_starts_one_run`:
```python
def test_start_triggered_run_marks_run_failed_when_submit_raises(db, monkeypatch):
    from ui.backend.db.models import Run as RunRow
    from ui.backend.runtime import registry

    org, trigger = _org_with_trigger(db, last_uid=41)
    monkeypatch.setattr(email_trigger, "check_mailbox", lambda b, u: (3, 45, [42, 45]))

    class _BoomExecutor:
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(email_trigger, "_executor", _BoomExecutor())

    poll_org(db, trigger, _fake_workflow_getter([]))

    run_id = trigger.last_run_id
    assert run_id is not None
    assert registry.get(run_id).status == "failed"
    assert db.get(RunRow, run_id).status == "failed"

    # The overlap guard must not wedge on this run afterward.
    calls = []

    def _track(b, u):
        calls.append(1)
        return (3, 45, [])

    monkeypatch.setattr(email_trigger, "check_mailbox", _track)
    poll_org(db, trigger, _no_workflow)
    assert calls == [1]  # mailbox was actually checked -- guard didn't wedge
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -k submit_raises -v`
Expected: FAIL — `RuntimeError: cannot schedule new futures after shutdown`
propagates out of `poll_org` uncaught (it's only caught one level up, in
`poll_once`, which this test calls `poll_org` directly, bypassing).

- [ ] **Step 3: Implement**

In `ui/backend/email_trigger.py`, add these imports at the top:
```python
from __future__ import annotations

import asyncio
```
becomes:
```python
from __future__ import annotations

import asyncio
import dataclasses
```
and add, alongside the other `bestteam.*` imports:
```python
from bestteam.core.loader import _build_workflow
```
becomes:
```python
from bestteam.core.loader import _build_workflow
from bestteam.core.trace import TraceEvent
```

Replace the tail of `_start_triggered_run`:
```python
    run = registry.create(
        trigger.workflow_name, input_text, org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )
    # Durable activity record before dispatch (worker updates its terminal
    # status; run_in_background reuses this row rather than re-inserting).
    db.add(Run(
        id=run.id, workflow=trigger.workflow_name, input=input_text,
        status="running", org_id=trigger.org_id, username=TRIGGER_USERNAME,
    ))
    trigger.last_uid = max(batch)
    trigger.runs_today += 1
    trigger.last_run_id = run.id
    trigger.last_error = None  # a run is going out: clear any prior fault
    trigger.last_error_kind = None
    db.commit()
    _executor.submit(
        run_in_background, run.id, workflow, input_text,
        engine=db.get_bind(), org_id=trigger.org_id, username=TRIGGER_USERNAME,
    )
```
with:
```python
    run = registry.create(
        trigger.workflow_name, input_text, org_id=trigger.org_id,
        username=TRIGGER_USERNAME,
    )
    # Durable activity record before dispatch (worker updates its terminal
    # status; run_in_background reuses this row rather than re-inserting).
    run_row = Run(
        id=run.id, workflow=trigger.workflow_name, input=input_text,
        status="running", org_id=trigger.org_id, username=TRIGGER_USERNAME,
    )
    db.add(run_row)
    trigger.last_uid = max(batch)
    trigger.runs_today += 1
    trigger.last_run_id = run.id
    trigger.last_error = None  # a run is going out: clear any prior fault
    trigger.last_error_kind = None
    db.commit()
    try:
        _executor.submit(
            run_in_background, run.id, workflow, input_text,
            engine=db.get_bind(), org_id=trigger.org_id, username=TRIGGER_USERNAME,
        )
    except Exception:  # noqa: BLE001 -- submission itself must never wedge the trigger
        # The batch/cap were already consumed by the commit above (the same
        # accepted commit-then-crash window already disclosed for a process
        # kill at this point). Without this, both the registry entry and the
        # Run row would stay "running" forever and the overlap guard would
        # block every later poll for this org.
        _logger.exception("email trigger: failed to dispatch run %s for org %s",
                          run.id, trigger.org_id)
        message = "Couldn't start the automatic run. It will retry on the next new message."
        registry.publish(run.id, dataclasses.asdict(
            TraceEvent(type="run_failed", workflow=trigger.workflow_name, data=message)
        ))
        run_row.status = "failed"
        run_row.output = message
        trigger.last_error = message
        trigger.last_error_kind = _ERROR_KIND_WORKFLOW
        db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/email_trigger.py tests/test_email_trigger.py
git commit -m "fix(trigger): mark the run failed instead of wedging when dispatch submission fails"
```

---

### Task 4: Validate trigger env values at startup (finding #5)

`poll_seconds()`/`daily_cap()`/`batch_size()` parse env strings with no
bounds checking. A non-numeric or non-positive value raises inside
`poll_forever`'s loop, which only catches `asyncio.TimeoutError` — the
exception is unhandled, the loop dies, and automatic runs silently stop for
every org on the deployment with no supervision or restart.

**Files:**
- Modify: `ui/backend/email_trigger.py`
- Modify: `ui/backend/main.py`
- Test: `tests/test_email_trigger.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `email_trigger.validate_trigger_env() -> None` (raises
  `RuntimeError` on an invalid value) — called once from `main.py` at import
  time.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_email_trigger.py`, after
`test_poll_forever_sleeps_first_and_respects_kill_switch`:
```python
# --- validate_trigger_env -----------------------------------------------------


def test_validate_trigger_env_accepts_unset_and_valid_values(monkeypatch):
    monkeypatch.delenv("BESTTEAM_TRIGGER_POLL_SECONDS", raising=False)
    monkeypatch.delenv("BESTTEAM_TRIGGER_DAILY_CAP", raising=False)
    monkeypatch.delenv("BESTTEAM_TRIGGER_BATCH_SIZE", raising=False)
    email_trigger.validate_trigger_env()  # must not raise

    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "60")
    monkeypatch.setenv("BESTTEAM_TRIGGER_DAILY_CAP", "10")
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "5")
    email_trigger.validate_trigger_env()  # must not raise


def test_validate_trigger_env_rejects_non_numeric_poll_seconds(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "soon")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_POLL_SECONDS"):
        email_trigger.validate_trigger_env()


def test_validate_trigger_env_rejects_zero_batch_size(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "0")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_BATCH_SIZE"):
        email_trigger.validate_trigger_env()


def test_validate_trigger_env_rejects_negative_daily_cap(monkeypatch):
    monkeypatch.setenv("BESTTEAM_TRIGGER_DAILY_CAP", "-5")
    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_DAILY_CAP"):
        email_trigger.validate_trigger_env()
```

Add to `tests/test_auth.py`, right after `test_secret_key_guard_allows_custom_secret_without_env`:
```python
def test_trigger_env_guard_fires_on_bad_value(monkeypatch):
    monkeypatch.delenv("BESTTEAM_ENV", raising=False)
    monkeypatch.setenv("BESTTEAM_TRIGGER_POLL_SECONDS", "not-a-number")

    with pytest.raises(RuntimeError, match="BESTTEAM_TRIGGER_POLL_SECONDS"):
        importlib.reload(backend_main)

    # Restore a valid value and reload again so later tests in this process
    # (which import backend_main.app directly) see a working app.
    monkeypatch.delenv("BESTTEAM_TRIGGER_POLL_SECONDS", raising=False)
    importlib.reload(backend_main)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py -k validate_trigger_env -v tests/test_auth.py -k trigger_env_guard -v`
Expected: FAIL — `AttributeError: module 'ui.backend.email_trigger' has no attribute 'validate_trigger_env'`.

- [ ] **Step 3: Implement**

In `ui/backend/email_trigger.py`, add this function right after `batch_size()`:
```python
def validate_trigger_env() -> None:
    """Fail fast at startup on a malformed trigger env var, rather than
    letting the poller task die silently mid-loop later (poll_forever only
    catches asyncio.TimeoutError, so a bad value would otherwise kill
    automatic runs for every org with no supervision or restart)."""
    for env_name, getter in (
        (POLL_SECONDS_ENV, poll_seconds),
        (DAILY_CAP_ENV, daily_cap),
        (BATCH_SIZE_ENV, batch_size),
    ):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            value = getter()
        except ValueError:
            raise RuntimeError(f"{env_name}={raw!r} is not a valid number.") from None
        if value <= 0:
            raise RuntimeError(f"{env_name}={raw!r} must be a positive number.")
```

In `ui/backend/main.py`, add the call right after the existing
`BESTTEAM_SECRET_KEY` guard:
```python
if auth.is_insecure_secret_key(auth.SECRET_KEY):
    raise RuntimeError(
        "BESTTEAM_SECRET_KEY is unset or still a known placeholder value. "
        "Set BESTTEAM_SECRET_KEY to a long random value before starting this service "
        "-- generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
```
becomes:
```python
if auth.is_insecure_secret_key(auth.SECRET_KEY):
    raise RuntimeError(
        "BESTTEAM_SECRET_KEY is unset or still a known placeholder value. "
        "Set BESTTEAM_SECRET_KEY to a long random value before starting this service "
        "-- generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

email_trigger.validate_trigger_env()
```
(`email_trigger` is already imported in `main.py` — `from . import email_trigger`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py tests/test_auth.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/email_trigger.py ui/backend/main.py tests/test_email_trigger.py tests/test_auth.py
git commit -m "fix(trigger): validate BESTTEAM_TRIGGER_* env values at startup"
```

---

### Task 5: Frontend — surface fetch failures distinctly (finding #8)

`EmailTriggerActivity.jsx` collapses `getEmailTrigger()`/
`emailTriggerActivity()` fetch failures into the same rendered state as
"trigger is off" / "no runs yet" — indistinguishable to the customer — and
never renders `last_checked_at`, though the backend payload already includes
it.

Note: this repo has no frontend test runner configured (`ui/frontend/package.json`
has no `test` script, no `vitest`/`@testing-library` dependency, no existing
`*.test.jsx` file anywhere in the tree). Introducing one is a separate
infrastructure decision, out of scope for this bugfix — verify this task with
`npm run lint` + `npm run build` (the project's existing verification
commands for frontend changes) plus a manual dev-server check instead of new
automated tests.

**Files:**
- Modify: `ui/frontend/src/components/EmailTriggerActivity.jsx`

**Interfaces:** none (leaf UI component, no other file imports its internals
beyond the default export already used by `DeployPage.jsx`/`SessionsPage.jsx`,
unchanged).

- [ ] **Step 1: Replace the component**

Replace the entire contents of `ui/frontend/src/components/EmailTriggerActivity.jsx`:
```jsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'

const STATUS_LABELS = {
  active: 'Active — watching for new email',
  paused_cap: 'Paused — daily limit reached (resumes tomorrow)',
  error: 'Problem checking the mailbox',
  disabled: 'Paused by the operator',
}

// Org-level automatic-runs status + recent autonomous activity, shown on
// "My teams". Renders nothing while loading or while automatic runs are off
// (most teams never turn this on); a fetch failure gets its own banner so it
// is never mistaken for either of those.
export default function EmailTriggerActivity() {
  const [trigger, setTrigger] = useState(undefined) // undefined = still loading
  const [statusFailed, setStatusFailed] = useState(false)
  const [runs, setRuns] = useState([])
  const [activityFailed, setActivityFailed] = useState(false)

  useEffect(() => {
    api.getEmailTrigger().then(setTrigger).catch(() => setStatusFailed(true))
    api
      .emailTriggerActivity()
      .then((d) => setRuns(d.runs.filter((r) => r.autonomous).slice(0, 10)))
      .catch(() => setActivityFailed(true))
  }, [])

  if (statusFailed) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <p className="banner banner-error">
          Couldn't load automatic-run status. Refresh the page to try again.
        </p>
      </div>
    )
  }

  if (trigger === undefined) return null // still loading -- avoid a flash
  if (!trigger.enabled) return null // genuinely off -- nothing to show

  return (
    <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
      <h3>Automatic runs — "{trigger.workflow_name}"</h3>
      <p className="subtitle">{STATUS_LABELS[trigger.status] ?? trigger.status}</p>
      {trigger.last_checked_at && (
        <p className="hint">
          Last checked: {new Date(trigger.last_checked_at).toLocaleString()}
        </p>
      )}
      {trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}
      {activityFailed ? (
        <p className="hint">Couldn't load recent activity. Refresh the page to try again.</p>
      ) : runs.length === 0 ? (
        <p className="hint">No automatic runs yet — they'll show up here when new email arrives.</p>
      ) : (
        <ul className="session-list">
          {runs.map((r) => (
            <li key={r.id} className="hint">
              <span className="status-badge">{r.status}</span>{' '}
              {r.started_at ? new Date(r.started_at).toLocaleString() : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Lint and build**

Run (from `ui/frontend/`): `npm run lint`
Expected: no errors.

Run (from `ui/frontend/`): `npm run build`
Expected: build succeeds.

- [ ] **Step 3: Manual verification**

Start the backend (`BESTTEAM_SECRET_KEY`/`BESTTEAM_SECRETS_KEY` set) and
`npm run dev`, log in as an org user with automatic runs enabled, and confirm
on "My teams": the card renders with a "Last checked: ..." line. Then stop
the backend and reload the page — confirm the card shows the "Couldn't load
automatic-run status" banner instead of silently disappearing or showing
"No automatic runs yet".

- [ ] **Step 4: Commit**

```bash
git add ui/frontend/src/components/EmailTriggerActivity.jsx
git commit -m "fix(trigger): distinguish fetch failures from off/empty in the activity card"
```

---

### Task 6: Docs

Record what round 2 closed, keep the residual known-issues list accurate
(only #2 RunRegistry eviction and #6 shutdown thread-stop remain deferred —
both unchanged from the prior round's explicit decision), and reflect the
new operator-CLI/error-kind/env-validation behavior in the directory-scoped
reference doc.

**Files:**
- Modify: `ui/backend/CLAUDE.md`
- Modify: `docs/deployment.md`
- Modify: `docs/STATUS.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `ui/backend/CLAUDE.md`**

In the "Autonomous email trigger" section, the paragraph currently ends with:
```
...Batch size: `BESTTEAM_TRIGGER_BATCH_SIZE` (default 20).
```
Append a new paragraph directly after it:
```
Round-2 hardening (independent-reviewer follow-up on PR #22): `poll_org`
resolves the IMAP backend once per cycle and threads it into
`build_trigger_workflow` instead of letting it re-fetch credentials
independently -- closes a race where a mid-cycle mailbox swap could detect
mail on one mailbox and build tools against another. `admin.py`'s
`set-email`/`clear-email` now call the same `email_trigger.disable_trigger`/
`disable_trigger_on_identity_change` helpers as `org_settings.py`, so the
operator CLI path disables the trigger on mailbox change too (previously
only the wizard path did). `EmailTrigger.last_error_kind` (`"mailbox" |
"workflow" | None`) distinguishes a connectivity fault, which auto-clears on
the next successful mailbox check, from a workflow/dispatch fault, which
still persists until a real successful dispatch (F5, unchanged). A dispatch-
submission failure now marks the run failed instead of leaving the overlap
guard wedged. `BESTTEAM_TRIGGER_*` env values are validated at startup
(`email_trigger.validate_trigger_env()`, called from `main.py` beside the
`BESTTEAM_SECRET_KEY` guard) instead of being able to silently kill the
poller mid-loop. Deferred: `RunRegistry` eviction and awaiting in-flight
polling threads on shutdown (see `docs/STATUS.md`, Known issues).
```

- [ ] **Step 2: Update `docs/deployment.md`**

In the "Automatic runs (autonomous email trigger)" subsection, the paragraph
currently ends with:
```
Leader election is future work; until then, one worker.
```
Append:
```
An invalid `BESTTEAM_TRIGGER_*` value (non-numeric or non-positive) refuses
startup with a clear error instead of silently stopping the poller later.
```

- [ ] **Step 3: Update `docs/STATUS.md`**

Insert a new "Done" bullet right after the existing "Autonomous email-trigger
correctness fixes" bullet (the one ending
`...Remaining P2 hardening (env validation, shutdown thread-stop, run-source enum, RunRegistry eviction) tracked in Known issues.`):
```markdown
- Autonomous email-trigger hardening round 2 (independent-reviewer follow-up
  on PR #22): mailbox-replacement race closed (one IMAP backend resolved per
  poll cycle, threaded through to workflow-building instead of re-fetched);
  operator CLI (`admin set-email`/`clear-email`) now disables the trigger on
  mailbox change/disconnect too, matching the wizard path; dispatch-
  submission failures mark the run failed instead of wedging the overlap
  guard; mailbox connectivity errors and workflow/dispatch errors are
  tracked separately (`last_error_kind`) so a resolved mailbox outage clears
  instead of showing "error" forever; `BESTTEAM_TRIGGER_*` env values
  validated at startup (fail-fast, matching the `BESTTEAM_SECRET_KEY` guard);
  "My teams" activity card distinguishes a failed status/activity fetch from
  "off" or "no runs yet" and now shows `last_checked_at`. `RunRegistry`
  eviction and shutdown thread-stop remain deferred (see Known issues).
```

Replace the "Autonomous trigger residuals" bullet under "Known issues / tech debt":
```markdown
- **Autonomous trigger residuals:** invalid `BESTTEAM_TRIGGER_*` env values
  aren't validated at startup (a bad value can stop/spin the poller);
  `asyncio.to_thread` poll cycles aren't awaited on shutdown; a process killed
  between a trigger's state commit and dispatch orphans a `runs` row (overlap
  guard self-recovers on restart; no reconciliation sweep yet); `RunRegistry`
  never evicts terminal runs, so autonomous volume grows process memory.
```
with:
```markdown
- **Autonomous trigger residuals:** `asyncio.to_thread` poll cycles aren't
  awaited on shutdown, so a mailbox check/commit/dispatch already in flight
  can keep running briefly after the ASGI shutdown handler returns; a process
  killed between a trigger's state commit and dispatch orphans a `runs` row
  (overlap guard self-recovers on restart; no reconciliation sweep yet);
  `RunRegistry` never evicts terminal runs, so autonomous volume grows
  process memory.
```

- [ ] **Step 4: Commit**

```bash
git add ui/backend/CLAUDE.md docs/deployment.md docs/STATUS.md
git commit -m "docs(trigger): record round-2 hardening fixes and updated residuals"
```

---

## Self-Review

**Spec coverage:** Fix 1 (a)+(b) → Task 1. Fix 2 (mailbox/workflow error
separation, spec section "Fix 3") → Task 2. Fix 2's "unhandled dispatch
failure" (spec section "Fix 2") → Task 3 — note the spec's fix numbering and
this plan's task numbering diverge because Task 2 (error kind) must land
before Task 3 (submit guard) so `last_error_kind` already exists when the
submit-failure branch sets it; the design intent is unchanged either way.
Fix 4 (env validation) → Task 4. Fix 5 (frontend) → Task 5. Docs → Task 6.
"Out of scope" items (#2, #6, #7) intentionally have no task — confirmed
against the design spec's explicit dispositions.

**Placeholder scan:** No TBD/TODO; every step has complete, copy-pasteable
code; no "similar to Task N" shortcuts.

**Type consistency:** `build_trigger_workflow`/`get_workflow`'s 5-arg shape
(`name, db, org_id, allowed_uids, backend`) is introduced in Task 1 and used
identically by `poll_org`/`_start_triggered_run` for the rest of the plan.
`disable_trigger`/`disable_trigger_on_identity_change` (Task 1) are called
with the same argument order in `org_settings.py` and `admin.py`.
`last_error_kind` (Task 2) is set with the same two string constants
(`_ERROR_KIND_MAILBOX`/`_ERROR_KIND_WORKFLOW`) everywhere it's written,
including Task 3's new call site.
