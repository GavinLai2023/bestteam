# Code review fixes: cache staleness, WS session leak, frontend/KB minors

## Context

A full-project `/code-review` pass (4 parallel read-only audit agents over
`ui/backend/`, `src/bestteam/`, `ui/frontend/`, and a cleanup/conventions
sweep) surfaced a number of candidate findings. Each candidate was
independently re-verified by reading the actual source (not just trusting
the audit agents' claims) — several were refuted on inspection (documented
intentional behavior, already-existing validation, mutually-exclusive code
paths misread as duplicate calls, etc.; see "Refuted findings" below for the
record). Seven held up. The user reviewed all seven and asked to fix all of
them.

This spec covers those seven fixes, grouped into three independent units of
work.

## Scope

In scope:
- `ui/backend/main.py::_get_workflow` — cache-key staleness + a redundant
  `load_skills` call on cache hits (findings 1 and 3, fixed together since
  both touch the same function).
- `ui/backend/main.py::stream_run` — DB session held open for the whole
  WebSocket connection (finding 2).
- `ui/frontend/src/pages/AdvancedPage.jsx` — tab-switch race with an
  in-flight save/delete/upload (finding 4).
- `ui/frontend/src/pages/MonitorPage.jsx` — missing error handling in
  `startRun()` (finding 5).
- `ui/frontend/src/pages/wizard/IntentPage.jsx` — missing `disabled` guard
  on the "Try again" button (finding 6).
- `src/bestteam/core/knowledge_base.py` — silently swallowed
  `ConfigurationError` during KB document loading (finding 7).

Out of scope: anything in "Refuted findings" below, and any other part of
the codebase not touched by these seven items.

## Design

### A. Cache correctness in `_get_workflow` (findings 1 + 3)

**Current behavior** (`ui/backend/main.py`, current lines ~100-130):
`skill_lookup = load_skills(db)` runs unconditionally, before the
cache-hit/miss check, and `cache_key = ("db", record.updated_at)` only
tracks the workflow row's own timestamp.

**Fix:**
1. Move the `load_skills(...)` call (both the `db is not None` and `db is
   None` branches) inside the `if cached is None or cached[1] != cache_key:`
   block, next to where `kb_tools` is already built — mirroring the exact
   restructuring already done for `kb_tools` in commit `08db97b`. This
   fixes finding 3: skills are now loaded only when actually rebuilding the
   workflow.
2. Extend `cache_key` to include the freshness of every `SkillRecord` and
   every `KnowledgeBaseRecord`, not just the workflow row. Add a small
   helper (e.g. `_max_updated_at(db, model_cls) -> Optional[datetime]`,
   or inline two `db.query(func.max(Model.updated_at)).scalar()` calls) and
   fold both values into the tuple:
   `cache_key = ("db", record.updated_at, skills_max_updated_at, kb_max_updated_at)`.

   This deliberately uses a **global** max across all skills/KBs rather
   than only the ones this specific workflow references. Computing the
   referenced-only set for cache-key purposes would require duplicating the
   "which names does this workflow's config reference" extraction that
   `load_knowledge_base_tools` already does internally for a different
   reason (avoiding unnecessary KB *builds*) — here we only need a cheap
   timestamp comparison, not a build, so the simpler global-max approach is
   correct (worst case: a workflow's cache invalidates when an unrelated
   skill/KB is edited) and avoids introducing a second dependency-tracking
   mechanism for one call site. Two `MAX()` aggregate queries are
   negligible cost next to the workflow lookup that's already happening on
   every cache-key computation.

   These two queries run on every call (cheap aggregates), same as the
   existing `record.updated_at` check — they are the cache-key computation
   itself, not work that needs to be skipped on a hit.

### B. WebSocket session fix in `stream_run` (finding 2)

**Current behavior:** the route depends on `db: Session = Depends(get_db)`,
used once (`get_user_by_username(db, username)`) before
`await websocket.accept()`. FastAPI's generator-based dependency keeps that
session open until the coroutine returns, i.e. for the entire streaming
connection.

**Fix (corrected during implementation):** keep `db: Session =
Depends(get_db)` on the route signature -- dropping it in favor of a
direct `SessionLocal()` call would bypass FastAPI's dependency-override
mechanism, which is how tests substitute an in-memory database; `SessionLocal`
itself stays bound to the real production engine regardless of test
overrides. Instead, call `db.close()` immediately after the
`get_user_by_username(db, ...)` check (and on both early-return branches),
releasing the connection well before the long-lived streaming loop instead
of leaving it open until the WebSocket closes.

### C. Four independent minor fixes (findings 4-7)

**C1. `AdvancedPage.jsx` tab-switch race (finding 4):**
Capture the tab the action started for (e.g. `const startedFor = activeKey`
at the top of `save`/`remove`/`uploadNew`, before the `await`), and after
the await resolves, only call `loadItems()` if `activeKey === startedFor`
still holds. Since `activeKey` is read fresh from component state at the
time of the check (not from the closure), this needs the check to read the
*current* render's state — simplest correct approach is a ref
(`const activeKeyRef = useRef(activeKey)`, kept in sync via
`activeKeyRef.current = activeKey` each render, or updated in the existing
`activeKey`-keyed `useEffect`) so the post-await check sees the live value
rather than another stale closure.

**C2. `MonitorPage.jsx` missing error handling (finding 5):**
Wrap the body of `startRun()` from `await api.createRun(...)` through the
WebSocket setup in try/catch, matching `PreviewPage.run()`'s existing shape:
on failure, set an error message and reset `status` (to `'idle'` or
`'failed'`) instead of leaving it stuck on `'running'`. `MonitorPage` has no
existing `error` state — add one (`const [error, setError] = useState(null)`)
and render it the same way `PreviewPage` does, since there's currently no
way to surface this to the user at all.

**C3. `IntentPage.jsx` "Try again" button guard (finding 6):**
Add `disabled={submitting}` to the "Try again" button (`IntentPage.jsx`,
the button inside the `error &&` block), matching the guard already used
elsewhere in the same file's submit flow.

**C4. `knowledge_base.py` silent `ConfigurationError` swallow (finding 7):**
In `_load_document_chunks`, change the bare
`except ConfigurationError: continue` to also call
`warnings.warn(f"Skipping unreadable file '{file_path}': {exc}", stacklevel=2)`
before `continue` — same message shape as the `except Exception` branch
immediately below it, so both failure paths give the user the same
diagnostic.

## Refuted findings (for the record — not in scope, do not re-flag)

- `top_k = top_k or self.default_top_k` in `LocalFolderKnowledgeBase`/
  `VectorKnowledgeBase` (`top_k=0` falls back to default) — explicitly
  documented as "intentional parity" in a comment at
  `vector_knowledge_base.py:189-190`.
- HTTP redirect loop "off-by-one" in `tools/http_client.py:105-120` — traced
  manually; the loop correctly allows exactly `_MAX_REDIRECTS` redirects
  before raising.
- `chunk_overlap >= chunk_size` "infinite loop" in `knowledge_base.py` —
  already rejected by `_validate_chunk_params` (line 148) before
  `_chunk_text` ever runs.
- HIERARCHICAL team "subordinate usage lost" in `langgraph_adapter.py:221`
  — intentional, documented design (`ui/backend/CLAUDE.md`: manager and all
  delegated subordinates share one `usage_sink`, surfaced on the manager's
  single `agent_completed` event).
- `main.py`'s `source = WORKFLOWS_DIR / f"{name}.yaml"` used even for
  DB-backed workflows — documented, deliberate design from
  `docs/superpowers/specs/2026-06-19-standalone-kb-tool-resolution-design.md`'s
  "Relative paths" section, not an oversight.
- `builder.py` "`load_skills(db)` called twice per request" in
  `submit_specification`/`submit_solution_feedback` — the two call sites
  are in mutually-exclusive `if`/`elif` branches; at most one runs per
  request.
- `langgraph_adapter.py:219`'s `delegate_tools` list comprehension
  "recreated on every call" — the node function runs once per graph
  execution under this architecture; the recreation cost is negligible.

## Testing

- **A:** extend the existing workflow-cache tests (the ones already
  covering the `kb_tools` cache-miss-only fix) with: (1) a call-counter test
  proving `load_skills` is now also only invoked on a cache miss; (2) a
  test that edits a referenced `SkillRecord`'s config after a workflow has
  been cached, re-requests the workflow, and asserts the new skill content
  is reflected (not the stale cached one) — and the same for a
  `KnowledgeBaseRecord` edit.
- **B:** a test opening the WebSocket stream and asserting no session is
  held for the connection's duration is hard to assert directly through
  `TestClient`; instead, test that `stream_run` still correctly rejects an
  unknown/deleted user (existing behavior, must not regress) and verify by
  code inspection that no `db` parameter remains on the route.
- **C1:** a frontend test isn't justified (no test harness exists for this
  page today, per `ui/frontend/CLAUDE.md`); verify manually via the dev
  server — switch tabs mid-save and confirm the new tab's list isn't
  overwritten.
- **C2:** same — manual verification: trigger a `createRun` failure (e.g.
  stop the backend mid-flow) and confirm an error message appears instead
  of an infinite "Running...".
- **C3:** manual verification: rapid double-click no longer fires two
  requests (visually, button greys out immediately).
- **C4:** a new backend test: a `local_folder` KB directory containing one
  file that raises `ConfigurationError` from `parse_file` (e.g. via
  monkeypatching `parse_file` for an extension-matching file) loads
  successfully (other chunks still present) and emits a `UserWarning` via
  `pytest.warns`.
