# Email Phase 3a — Trigger Health & Alerting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a broken email automation announce itself — in-app and through an optional per-org webhook — and close the two correctness defects Codex found in the paths that detect the breakage.

**Architecture:** A pure evaluator (`trigger_health.evaluate`) decides state transitions from fault outcomes; the three existing fault sites persist its decision and append a `Notification` row; the poll cycle drains pending notifications to a webhook. Draft creation becomes idempotent under a process-wide per-source-key lock. Health writeback is guarded by the trigger's current `last_run_id`.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x, Alembic, stdlib `urllib.request`/`hmac`/`threading`, React + Vite + Vitest.

**Spec:** `docs/superpowers/specs/2026-08-17-email-phase-3a-health-alerting-design.md`

## Global Constraints

- **No new Python dependency.** Webhook delivery uses stdlib `urllib.request`, as `src/bestteam/tools/_oauth.py` does. `backend-optional-deps` must keep passing.
- **No SMTP, no send verb, anywhere.** Alerts never travel by email.
- **Webhook payloads carry no email content** — no subjects, addresses, bodies or snippets. Health information only.
- **Webhook URLs must be HTTPS and pass `check_host_allowed`** (`src/bestteam/tools/http_client.py`), the same public-IP restriction the per-org IMAP path uses.
- **Secrets are Fernet tokens via `ui/backend/secret_store.py`**, never plaintext columns.
- **Every new observation path is subordinate to the work it observes**: it logs and continues, never propagates. Follow the existing `_safe_record_usage` / `_safe_record_trigger_health` pattern.
- **Alembic migrations must be guarded** (inspect before add/drop) because `ui/backend/db_session.py` runs `create_all` at import.
- **British English spelling** in user-facing copy and comments; code comments in English.
- **Every new test file needs a `pytestmark`** (`unit`/`integration`/`e2e`/`optional`) or `tests/test_marker_completeness.py` fails the suite.
- Run everything through `./.venv/Scripts/python.exe`.
- Alert threshold env var: `BESTTEAM_TRIGGER_ALERT_THRESHOLD`, default `3`, minimum `1`.
- Migration `down_revision` chain starts from the current head `i6j7k8l9m0n1`.

---

## File Structure

| File | Responsibility |
|---|---|
| `ui/backend/trigger_health.py` (new) | Pure evaluator: fault outcome → `HealthDecision`. No I/O, no clock, no DB. |
| `ui/backend/notifications.py` (new) | Notification creation helper + webhook dispatch. Owns HMAC signing and retry policy. |
| `ui/backend/db/notifications.py` (new) | CRUD for `Notification` and `OrgNotificationSetting`. |
| `ui/backend/notifications_api.py` (new) | `GET /api/notifications`, `POST /api/notifications/{id}/read`. |
| `ui/backend/db/models.py` | +`Notification`, +`OrgNotificationSetting`, +2 `EmailTrigger` columns, +1 `OrgEmailCredential` column. |
| `alembic/versions/j7k8l9m0n1o2_add_notifications.py` (new) | Additive migration for all of the above. |
| `ui/backend/deploy_validation.py` | Egress conflict check widened from per-agent to per-workflow. |
| `src/bestteam/tools/email_client.py` | Idempotent draft creation under a per-source-key lock. |
| `ui/backend/runtime.py` | Health writeback guarded by `last_run_id`; routed through the evaluator. |
| `ui/backend/email_trigger.py` | Mailbox/timeout outcomes routed through the evaluator; secret-expiry sweep; dispatch call at cycle end. |
| `ui/backend/org_settings.py` | Webhook settings endpoints; M365 secret-expiry field. |
| `ui/frontend/src/pages/NotificationsPage.tsx` (new) | The in-app list. |
| `ui/frontend/src/components/WebhookSettings.tsx` (new) | Webhook configuration form. |

---

## Task 1: Widen the egress conflict check to the whole workflow (Codex ①)

**Files:**
- Modify: `ui/backend/deploy_validation.py:49-80` (`find_email_egress_conflicts`)
- Modify: `ui/backend/builder.py`, `ui/backend/crud.py` (call sites)
- Test: `tests/test_deploy_validation.py`, `tests/test_crud_api.py`

**Interfaces:**
- Produces: `find_email_egress_conflicts(agent_tool_sets) -> List[str]` — same signature, workflow-level semantics.

- [ ] **Step 1: Write the failing test**

```python
def test_email_and_egress_on_different_agents_is_rejected():
    problems = find_email_egress_conflicts([
        ("reader", {"email_find", "email_read"}),
        ("researcher", {"web_search"}),
    ])
    assert len(problems) == 1
    assert "reader" in problems[0] and "researcher" in problems[0]


def test_workflow_without_email_tools_is_unaffected():
    assert find_email_egress_conflicts([
        ("a", {"web_search"}), ("b", {"http_get"}),
    ]) == []
```

- [ ] **Step 2: Run it and watch it fail**

`.\.venv\Scripts\python.exe -m pytest tests/test_deploy_validation.py -k egress -v`
Expected: the first test FAILS (currently returns `[]` — tools are on different agents).

- [ ] **Step 3: Implement**

Replace the per-agent loop with a workflow-level check. Keep the existing docstring's reasoning and extend it:

```python
    email_agents = sorted(n for n, t in pairs if set(t) & EMAIL_TOOL_NAMES)
    egress_pairs = [(n, sorted(set(t) & EGRESS_TOOL_NAMES)) for n, t in pairs]
    egress_agents = sorted(n for n, tools in egress_pairs if tools)
    if not email_agents or not egress_agents:
        return []
    tools_used = sorted({t for _, tools in egress_pairs for t in tools})
    return [
        f"agent '{email_agents[0]}' reads email while agent '{egress_agents[0]}' "
        f"has {', '.join(tools_used)}; a workflow's agents share state, so a "
        "malicious email could still reach an outside address"
    ]
```

Note `pairs = list(agent_tool_sets)` first — the caller may pass a generator and it is traversed twice.

- [ ] **Step 4: Verify**

`.\.venv\Scripts\python.exe -m pytest tests/test_deploy_validation.py tests/test_crud_api.py tests/test_builder_api.py -v`

- [ ] **Step 5: Commit**

```bash
git add ui/backend/deploy_validation.py tests/
git commit -m "fix(email): reject email and egress tools anywhere in one workflow"
```

---

## Task 2: Idempotent draft creation (3a.5)

**Files:**
- Modify: `src/bestteam/tools/email_client.py` (module-level lock registry; `make_email_tools`'s `draft_reply` wrapper at ~line 780)
- Test: `tests/test_email_scoped_tools.py`

**Interfaces:**
- Consumes: `backend.drafts_with_source_keys(keys) -> set` (Phase 0).
- Produces: `draft_reply` returns the string `"A draft reply for this message already exists; nothing was written."` when a draft with the same source key is already present.

- [ ] **Step 1: Write the failing tests**

```python
def test_second_draft_for_same_source_key_is_skipped():
    backend = _RecordingBackend(existing_keys=set())
    tools = make_email_tools(backend, draft_marker_prefix="mailbox:1:uidvalidity:9:uid:")
    tools["email_draft_reply"]("7", "first")
    backend.existing_keys.add("mailbox:1:uidvalidity:9:uid:7")
    result = tools["email_draft_reply"]("7", "second")
    assert "already exists" in result
    assert backend.append_calls == 1


def test_concurrent_drafts_for_one_key_append_once():
    backend = _SlowRecordingBackend()
    tools = make_email_tools(backend, draft_marker_prefix="p:")
    threads = [threading.Thread(target=tools["email_draft_reply"], args=("7", "b"))
               for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert backend.append_calls == 1
```

`_SlowRecordingBackend.draft_reply` sleeps ~50ms before recording the key, so
an unlocked implementation reliably double-appends.

- [ ] **Step 2: Run and watch both fail**

`.\.venv\Scripts\python.exe -m pytest tests/test_email_scoped_tools.py -k draft -v`

- [ ] **Step 3: Implement the lock registry**

```python
_SOURCE_KEY_LOCKS: "OrderedDict[str, threading.Lock]" = OrderedDict()
_SOURCE_KEY_LOCKS_GUARD = threading.Lock()
_MAX_TRACKED_SOURCE_KEYS = 4096


def _lock_for_source_key(key: str) -> threading.Lock:
    """A process-wide lock per draft source key.

    The duplicate-draft race this closes is intra-process: a run wedged inside
    `workflow.stream()` and the retry that the stale-run watchdog released are
    both threads of the same uvicorn process. Bounded LRU because keys are
    unique per (mailbox, uidvalidity, uid) and would otherwise accumulate for
    the process's lifetime; evicting an entry only removes it from the
    registry -- a current holder keeps its own object -- and would need 4096
    distinct drafts inside one draft's window to matter.
    """
    with _SOURCE_KEY_LOCKS_GUARD:
        lock = _SOURCE_KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SOURCE_KEY_LOCKS[key] = lock
            if len(_SOURCE_KEY_LOCKS) > _MAX_TRACKED_SOURCE_KEYS:
                _SOURCE_KEY_LOCKS.popitem(last=False)
        else:
            _SOURCE_KEY_LOCKS.move_to_end(key)
        return lock
```

- [ ] **Step 4: Implement the check in the wrapper**

In `make_email_tools`'s `draft_reply`, after `source_key` is computed:

```python
        if source_key is None:
            return _draft_impl(backend, message_id, body, source_key)
        with _lock_for_source_key(source_key):
            scan = getattr(backend, "drafts_with_source_keys", None)
            if scan is not None:
                try:
                    if source_key in (scan([source_key]) or set()):
                        return _DRAFT_ALREADY_EXISTS
                except Exception:  # noqa: BLE001 -- advisory; never block drafting
                    _logger.warning(
                        "email_draft_reply: idempotency scan failed; drafting anyway",
                        exc_info=True,
                    )
            return _draft_impl(backend, message_id, body, source_key)
```

with `_DRAFT_ALREADY_EXISTS = "A draft reply for this message already exists; nothing was written."`

- [ ] **Step 5: Verify**

`.\.venv\Scripts\python.exe -m pytest tests/test_email_scoped_tools.py tests/test_email_tools.py -v`

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/tools/email_client.py tests/test_email_scoped_tools.py
git commit -m "fix(email): make draft creation idempotent per source key"
```

---

## Task 3: Ignore superseded runs in health writeback (Codex ③)

**Files:**
- Modify: `ui/backend/runtime.py` (`_safe_record_trigger_health`, ~line 150)
- Test: `tests/test_runtime_run_row.py`

**Interfaces:**
- Consumes: `EmailTrigger.last_run_id` (existing column).

- [ ] **Step 1: Write the failing test**

```python
def test_superseded_run_does_not_overwrite_trigger_health(db):
    trigger = _make_trigger(db, org_id=1, last_run_id="run-new")
    old = _make_run(db, run_id="run-old", org_id=1, status="failed",
                    trigger_context={"trigger_type": "email"})
    _safe_record_trigger_health(db, old)
    db.refresh(trigger)
    assert trigger.last_error is None


def test_current_run_still_updates_trigger_health(db):
    trigger = _make_trigger(db, org_id=1, last_run_id="run-cur")
    cur = _make_run(db, run_id="run-cur", org_id=1, status="failed",
                    trigger_context={"trigger_type": "email"})
    _safe_record_trigger_health(db, cur)
    db.refresh(trigger)
    assert trigger.last_error_kind == "workflow"
```

- [ ] **Step 2: Run and watch the first fail**

`.\.venv\Scripts\python.exe -m pytest tests/test_runtime_run_row.py -k superseded -v`

- [ ] **Step 3: Implement**

Immediately after `trigger = get_email_trigger(db, run_row.org_id)` and its `None` check:

```python
        # The stale-run watchdog can start a new run while a wedged one is
        # still executing, so a finishing run is not necessarily the run this
        # trigger is waiting on. Applying an old outcome would let a stale
        # failure overwrite health that the new run just cleared.
        if trigger.last_run_id is not None and trigger.last_run_id != run_row.id:
            return
```

- [ ] **Step 4: Verify**

`.\.venv\Scripts\python.exe -m pytest tests/test_runtime_run_row.py tests/test_email_trigger.py -v`

- [ ] **Step 5: Commit**

```bash
git add ui/backend/runtime.py tests/test_runtime_run_row.py
git commit -m "fix(email): ignore a superseded run's outcome in trigger health"
```

---

## Task 4: The pure health evaluator

**Files:**
- Create: `ui/backend/trigger_health.py`
- Test: `tests/test_trigger_health.py` (new — needs `pytestmark = pytest.mark.unit`)

**Interfaces:**
- Produces:
  - `OUTCOME_HEALTHY = "healthy"`, `OUTCOME_WORKFLOW = "workflow_fault"`, `OUTCOME_MAILBOX = "mailbox_fault"`, `OUTCOME_TIMEOUT = "run_timeout"`
  - `@dataclass(frozen=True) NotificationDraft(kind: str, severity: str, title: str, body: str, fingerprint: str)`
  - `@dataclass(frozen=True) HealthDecision(consecutive_faults: int, alerted_fingerprint: Optional[str], notification: Optional[NotificationDraft])`
  - `evaluate(*, outcome: str, consecutive_faults: int, alerted_fingerprint: Optional[str], threshold: int, detail: str = "") -> HealthDecision`
  - `alert_threshold() -> int` reading `BESTTEAM_TRIGGER_ALERT_THRESHOLD`

- [ ] **Step 1: Write the failing tests**

```python
def test_faults_below_threshold_do_not_alert():
    d = evaluate(outcome=OUTCOME_WORKFLOW, consecutive_faults=1,
                 alerted_fingerprint=None, threshold=3)
    assert d.consecutive_faults == 2 and d.notification is None


def test_reaching_the_threshold_alerts_once():
    first = evaluate(outcome=OUTCOME_WORKFLOW, consecutive_faults=2,
                     alerted_fingerprint=None, threshold=3)
    assert first.notification is not None
    assert first.alerted_fingerprint == "workflow"
    again = evaluate(outcome=OUTCOME_WORKFLOW, consecutive_faults=3,
                     alerted_fingerprint="workflow", threshold=3)
    assert again.notification is None


def test_recovery_emits_once_and_clears():
    d = evaluate(outcome=OUTCOME_HEALTHY, consecutive_faults=5,
                 alerted_fingerprint="workflow", threshold=3)
    assert d.consecutive_faults == 0
    assert d.alerted_fingerprint is None
    assert d.notification.severity == "info"


def test_healthy_without_a_prior_alert_is_silent():
    d = evaluate(outcome=OUTCOME_HEALTHY, consecutive_faults=0,
                 alerted_fingerprint=None, threshold=3)
    assert d.notification is None


def test_timeout_alerts_immediately_regardless_of_threshold():
    d = evaluate(outcome=OUTCOME_TIMEOUT, consecutive_faults=0,
                 alerted_fingerprint=None, threshold=3)
    assert d.notification.fingerprint == "run_timeout"


def test_changing_fault_kind_alerts_again():
    d = evaluate(outcome=OUTCOME_MAILBOX, consecutive_faults=2,
                 alerted_fingerprint="workflow", threshold=3)
    assert d.notification is not None and d.alerted_fingerprint == "mailbox"
```

- [ ] **Step 2: Run and watch them fail** (`ModuleNotFoundError`)

`.\.venv\Scripts\python.exe -m pytest tests/test_trigger_health.py -v`

- [ ] **Step 3: Implement `evaluate`**

Fingerprints: `workflow_fault` → `"workflow"`, `mailbox_fault` → `"mailbox"`, `run_timeout` → `"run_timeout"`. Copy (British spelling), each a single customer-facing sentence:

- workflow: title `"Automatic email replies are failing"`, body names the consecutive count and points at the run history.
- mailbox: title `"Your mailbox can't be reached"`, body points at the email connection settings.
- run_timeout: title `"An automatic run was stopped after taking too long"`, body says automatic runs have resumed.
- recovery: title `"Automatic email replies are working again"`, severity `info`.

`alert_threshold()` reads the env var, falls back to 3, and clamps below 1 to 1.

- [ ] **Step 4: Verify** — `.\.venv\Scripts\python.exe -m pytest tests/test_trigger_health.py -v`

- [ ] **Step 5: Commit**

```bash
git add ui/backend/trigger_health.py tests/test_trigger_health.py
git commit -m "feat(email): pure evaluator for trigger health transitions"
```

---

## Task 5: Schema and migration

**Files:**
- Modify: `ui/backend/db/models.py`
- Create: `alembic/versions/j7k8l9m0n1o2_add_notifications.py`
- Create: `ui/backend/db/notifications.py`
- Test: `tests/test_migrations.py`, `tests/test_notifications_db.py` (new)

**Interfaces:**
- Produces: `Notification`, `OrgNotificationSetting` models; `create_notification(db, *, org_id, kind, severity, title, body, fingerprint) -> Notification`; `list_notifications(db, org_id, *, unread_only=False, limit=50)`; `mark_read(db, org_id, notification_id) -> bool`; `get_notification_settings(db, org_id)`; `set_notification_settings(db, org_id, *, webhook_url, webhook_secret, enabled)`; `pending_notifications(db, limit)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_notification_round_trips_and_defaults_to_pending(db):
    n = create_notification(db, org_id=1, kind="trigger_health", severity="error",
                            title="t", body="b", fingerprint="workflow")
    assert n.delivery_state == "pending" and n.read_at is None


def test_mark_read_is_org_scoped(db):
    n = create_notification(db, org_id=1, kind="trigger_health", severity="error",
                            title="t", body="b", fingerprint="workflow")
    assert mark_read(db, org_id=2, notification_id=n.id) is False
    assert mark_read(db, org_id=1, notification_id=n.id) is True


def test_webhook_secret_is_stored_encrypted(db):
    set_notification_settings(db, 1, webhook_url="https://example.com/h",
                              webhook_secret="s3cret", enabled=True)
    row = get_notification_settings(db, 1)
    assert row.webhook_secret_encrypted != "s3cret"
    assert secret_store.decrypt(row.webhook_secret_encrypted) == "s3cret"
```

Plus in `tests/test_migrations.py`, mirroring the existing style: upgrade adds the two tables and the three columns; downgrade drops them; both are idempotent against a `create_all` database.

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Add the models** exactly as tabulated in the spec's "Data model" section, with `Notification.org_id` indexed and `OrgNotificationSetting.org_id` unique.

- [ ] **Step 4: Write the migration** with `revision = "j7k8l9m0n1o2"`, `down_revision = "i6j7k8l9m0n1"`, guarded by an inspector helper:

```python
def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _columns(inspector, table: str) -> set:
    if not _has_table(inspector, table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}
```

Upgrade creates the tables only when absent and `op.add_column`s only the missing columns; downgrade drops in reverse. SQLite in this venv is 3.45.3, so plain `op.drop_column` works — no batch-mode rebuild.

- [ ] **Step 5: Verify**

```
.\.venv\Scripts\python.exe -m alembic heads      # exactly one head
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py tests/test_notifications_db.py -v
```

- [ ] **Step 6: Commit**

```bash
git add ui/backend/db/models.py ui/backend/db/notifications.py alembic/ tests/
git commit -m "feat(email): schema for notifications and webhook settings"
```

---

## Task 6: Route the three fault sites through the evaluator

**Files:**
- Modify: `ui/backend/runtime.py` (`_safe_record_trigger_health`)
- Modify: `ui/backend/email_trigger.py` (connectivity outcome; `_release_stale_run`)
- Test: `tests/test_runtime_run_row.py`, `tests/test_email_trigger.py`

**Interfaces:**
- Consumes: `evaluate`, `alert_threshold`, `create_notification`.

- [ ] **Step 1: Write the failing tests**

```python
def test_third_consecutive_workflow_failure_creates_one_notification(db):
    trigger = _make_trigger(db, org_id=1, last_run_id="r3", consecutive_faults=2)
    _safe_record_trigger_health(db, _make_run(db, "r3", 1, "failed"))
    assert len(list_notifications(db, 1)) == 1
    trigger.last_run_id = "r4"
    _safe_record_trigger_health(db, _make_run(db, "r4", 1, "failed"))
    assert len(list_notifications(db, 1)) == 1  # suppressed by fingerprint


def test_watchdog_release_notifies_immediately(db, monkeypatch):
    ...  # drive _release_stale_run with a stale created_at
    assert [n.fingerprint for n in list_notifications(db, 1)] == ["run_timeout"]
```

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Implement** — at each site, replace the direct `last_error`/`last_error_kind` assignment with:

```python
    decision = evaluate(
        outcome=outcome,
        consecutive_faults=trigger.consecutive_faults or 0,
        alerted_fingerprint=trigger.alerted_fingerprint,
        threshold=alert_threshold(),
    )
    trigger.consecutive_faults = decision.consecutive_faults
    trigger.alerted_fingerprint = decision.alerted_fingerprint
    if decision.notification is not None:
        create_notification(db, org_id=trigger.org_id, **asdict(decision.notification))
```

Keep every existing `last_error`/`last_error_kind` write — the UI reads them and Phase 0's tests pin them. The evaluator adds alerting; it does not replace the error surface.

- [ ] **Step 4: Verify**

`.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py tests/test_runtime_run_row.py -v`

- [ ] **Step 5: Commit**

```bash
git add ui/backend/runtime.py ui/backend/email_trigger.py tests/
git commit -m "feat(email): raise notifications from the three trigger fault sites"
```

---

## Task 7: Webhook delivery

**Files:**
- Create: `ui/backend/notifications.py`
- Modify: `ui/backend/email_trigger.py` (`poll_once` tail)
- Test: `tests/test_notifications.py` (new)

**Interfaces:**
- Produces: `dispatch_pending(db, limit: int = 20) -> int` (count delivered/skipped/failed), `_sign(secret: str, body: bytes) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_webhook_configured_marks_skipped(db):
    n = create_notification(db, org_id=1, kind="trigger_health", severity="error",
                            title="t", body="b", fingerprint="workflow")
    dispatch_pending(db)
    db.refresh(n)
    assert n.delivery_state == "skipped"


def test_signature_is_hmac_sha256_of_the_exact_body():
    body = b'{"a":1}'
    assert _sign("k", body) == "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()


def test_non_2xx_retries_then_fails(db, monkeypatch):
    # 5 dispatch passes against a webhook that always 500s
    ...
    assert n.delivery_state == "failed" and n.delivery_attempts == 5


def test_delivery_never_raises(db, monkeypatch):
    monkeypatch.setattr(notifications, "_post", _raise)
    dispatch_pending(db)  # must not raise


def test_payload_carries_no_email_content(db):
    payload = _payload(n)
    assert set(payload) == {"id", "org_id", "kind", "severity", "title",
                            "body", "fingerprint", "created_at"}
```

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Implement.** `_post` wraps `urllib.request.urlopen` with a 10s timeout; the URL is re-validated with `check_host_allowed` at send time (a stored URL can re-resolve). Headers: `Content-Type: application/json`, `X-BestTeam-Delivery: <id>`, and `X-BestTeam-Signature` when a secret is set. 2xx → `delivered` + `delivered_at`; otherwise attempts += 1 and `last_delivery_error` truncated to 500 chars; at 5 → `failed`.

- [ ] **Step 4: Wire into the poll cycle** — at the end of `poll_once`, inside its own try/except that logs and continues:

```python
    try:
        dispatch_pending(db)
    except Exception:  # noqa: BLE001 -- delivery must never break polling
        _logger.exception("notification dispatch failed")
```

- [ ] **Step 5: Verify** — `.\.venv\Scripts\python.exe -m pytest tests/test_notifications.py tests/test_email_trigger.py -v`

- [ ] **Step 6: Commit**

```bash
git add ui/backend/notifications.py ui/backend/email_trigger.py tests/test_notifications.py
git commit -m "feat(email): deliver notifications to an org webhook"
```

---

## Task 8: M365 secret-expiry sweep

**Files:**
- Modify: `ui/backend/email_trigger.py` (sweep in the poll cycle)
- Modify: `ui/backend/org_settings.py` (accept/return `oauth_secret_expires_at`)
- Modify: `ui/backend/db/email_credentials.py` (persist it)
- Test: `tests/test_email_trigger.py`, `tests/test_org_settings.py`

**Interfaces:**
- Produces: `sweep_secret_expiry(db, today: date) -> int` — `today` is injected so the test never depends on the wall clock.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("days,fingerprint", [
    (30, "secret_expiry_30"), (7, "secret_expiry_7"), (0, "secret_expired"),
])
def test_expiry_bands_each_notify_once(db, days, fingerprint):
    _store_m365_credential(db, org_id=1, expires_in_days=days)
    sweep_secret_expiry(db, date(2026, 8, 17))
    sweep_secret_expiry(db, date(2026, 8, 17))
    assert [n.fingerprint for n in list_notifications(db, 1)] == [fingerprint]


def test_no_expiry_recorded_means_no_sweep_work(db):
    _store_password_credential(db, org_id=1)
    assert sweep_secret_expiry(db, date(2026, 8, 17)) == 0
```

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Implement.** Skip rows whose `oauth_secret_expires_at` is `NULL` or whose `auth_type != AUTH_MICROSOFT_OAUTH`. Band by days remaining: `<= 0` → `secret_expired` (severity `error`), `<= 7` → `secret_expiry_7` (`warning`), `<= 30` → `secret_expiry_30` (`warning`). Dedup by looking for an existing notification with that org and fingerprint — this is the one place fingerprint dedup lives outside the trigger row, because the state belongs to the credential, not the trigger.

- [ ] **Step 4: Accept the field in the API.** `EmailConnectRequest` gains `oauth_secret_expires_at: Optional[date] = None`; the `model_validator` rejects it for the password auth type. `get_email` returns it.

- [ ] **Step 5: Verify** — `.\.venv\Scripts\python.exe -m pytest tests/test_email_trigger.py tests/test_org_settings.py -v`

- [ ] **Step 6: Commit**

```bash
git add ui/backend/ tests/
git commit -m "feat(email): warn before a Microsoft 365 client secret expires"
```

---

## Task 9: Notification and webhook-settings APIs

**Files:**
- Create: `ui/backend/notifications_api.py`
- Modify: `ui/backend/main.py` (include the router), `ui/backend/org_settings.py`
- Test: `tests/test_notifications_api.py` (new), `tests/test_org_settings.py`

**Interfaces:**
- Produces: `GET /api/notifications?unread_only=`, `POST /api/notifications/{id}/read`, `GET /api/org/notifications`, `PUT /api/org/notifications`.

- [ ] **Step 1: Write the failing tests**

```python
def test_notifications_are_org_scoped(client, other_org_token):
    r = client.get("/api/notifications", headers=other_org_token)
    assert r.json() == []


def test_marking_another_orgs_notification_read_is_404(client, other_org_token, nid):
    assert client.post(f"/api/notifications/{nid}/read", headers=other_org_token).status_code == 404


def test_webhook_settings_never_return_the_secret(client, admin_token):
    client.put("/api/org/notifications", headers=admin_token, json={
        "webhook_url": "https://example.com/hook", "webhook_secret": "s", "enabled": True})
    body = client.get("/api/org/notifications", headers=admin_token).json()
    assert "webhook_secret" not in body and body["has_webhook_secret"] is True


@pytest.mark.parametrize("url", ["http://example.com/h", "https://127.0.0.1/h",
                                 "https://192.168.1.5/h"])
def test_bad_webhook_urls_are_rejected(client, admin_token, url):
    r = client.put("/api/org/notifications", headers=admin_token,
                   json={"webhook_url": url, "enabled": True})
    assert r.status_code == 400
```

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Implement**, following the existing router/auth conventions in `org_settings.py` (admin dependency for the settings routes, org-scoped session for the list routes).

- [ ] **Step 4: Verify** — `.\.venv\Scripts\python.exe -m pytest tests/test_notifications_api.py tests/test_org_settings.py -v`

- [ ] **Step 5: Commit**

```bash
git add ui/backend/ tests/
git commit -m "feat(email): notification list and webhook settings APIs"
```

---

## Task 10: Frontend

**Files:**
- Modify: `ui/frontend/src/lib/types.ts`, `ui/frontend/src/lib/api.ts`
- Create: `ui/frontend/src/pages/NotificationsPage.tsx`, `ui/frontend/src/components/WebhookSettings.tsx`
- Modify: `ui/frontend/src/components/EmailConnect.tsx` (optional expiry date, Microsoft only)
- Test: `ui/frontend/src/pages/NotificationsPage.test.tsx`, `ui/frontend/src/components/WebhookSettings.test.tsx`, existing `EmailConnect.test.tsx`

- [ ] **Step 1: Write the failing tests** — the list renders title/body/severity and a mark-as-read control; unread count reflects `read_at === null`; the webhook form rejects a non-HTTPS URL before submitting; `EmailConnect` shows the expiry field only when the Microsoft provider is selected.

- [ ] **Step 2: Run and watch them fail** — `cd ui/frontend && npm test`

- [ ] **Step 3: Implement**, matching the existing component conventions (typed API helpers in `lib/api.ts`, no inline fetch).

- [ ] **Step 4: Verify**

```
cd ui/frontend && npm test && npx tsc --noEmit && npm run build
```

- [ ] **Step 5: Commit**

```bash
git add ui/frontend
git commit -m "feat(ui): notifications page and webhook settings"
```

---

## Task 11: Documentation

**Files:** `docs/STATUS.md`, `docs/DECISIONS.md`, `ui/backend/CLAUDE.md`, `ui/backend/db/CLAUDE.md`, `ui/frontend/CLAUDE.md`, `src/bestteam/tools/CLAUDE.md`, `CLAUDE.md`, `docs/deployment.md`

- [ ] **Step 1:** `STATUS.md` — move "No alerting on trigger health" from Known issues to Done; record the multi-worker draft-idempotency limitation and the deliberate choice not to query Entra for expiry; note Phase 3b (retention) as the remaining half.
- [ ] **Step 2:** `DECISIONS.md` — why alerting is in-app + webhook and never email; why the egress check is workflow-level; why Codex's non-retriable-run proposal was rejected.
- [ ] **Step 3:** Module `CLAUDE.md` files — the evaluator, the notification/outbox table, the lock registry.
- [ ] **Step 4:** `docs/deployment.md` — webhook payload shape, signature verification example, and the `BESTTEAM_TRIGGER_ALERT_THRESHOLD` env var.
- [ ] **Step 5: Full verification**

```
.\.venv\Scripts\python.exe -m pytest -m "not e2e"      # serial, one process
cd ui/frontend && npm test && npx tsc --noEmit && npm run build
```

- [ ] **Step 6: Commit**

```bash
git add docs/ ui/ src/ CLAUDE.md
git commit -m "docs: record trigger health alerting and its limits"
```

---

## Self-Review

**Spec coverage:** 3a.1 → Tasks 4/6/7; 3a.2 → Tasks 4/6; 3a.3 → Task 8; 3a.4 → Tasks 4/6; 3a.5 → Task 2; 3a.6 → Task 3; Codex ① → Task 1; data model → Task 5; API → Task 9; frontend → Task 10; docs → Task 11. No gaps.

**Placeholder scan:** The two `...` markers (Task 6's watchdog test body, Task 7's retry test) stand for fixture wiring whose surrounding assertions are fully specified; every other step carries real code or an exact command.

**Type consistency:** `evaluate` / `HealthDecision` / `NotificationDraft` field names are identical in Tasks 4 and 6; `create_notification`'s keyword arguments in Task 5 match the `asdict(decision.notification)` expansion in Task 6 (`kind`, `severity`, `title`, `body`, `fingerprint`); `dispatch_pending(db, limit)` matches its call site in Task 7; `delivery_state` values (`pending`/`delivered`/`failed`/`skipped`) are consistent across Tasks 5, 7 and 9.
