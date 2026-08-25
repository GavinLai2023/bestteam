# bestteam — `ui/frontend/` (React + Vite frontend)

Directory-scoped notes for the monitoring dashboard and Team Builder wizard
frontend. See the root `CLAUDE.md` for project overview, architecture, and
commands; see `ui/backend/CLAUDE.md` for the API this frontend talks to.

## Internationalisation (`lib/i18n.ts`, `locales/`)

The UI is bilingual, **English by default**, switchable from the nav at any
time. Three things about the design are load-bearing:

- **No `navigator.language` detection.** Resolution is
  `localStorage['bestteam_lang']` then `'en'`, full stop. Auto-detecting would
  make the default drift with each visitor's browser and would make both the
  Vitest suite and the Playwright E2E run depend on the locale of whatever
  machine executes them. It is also what let this land without touching the
  suite's ~600 English assertions or `tests/e2e`'s 69 `has-text()` selectors.
- **`locales/en.ts` is the key source of truth** and is deliberately *not*
  `as const`. Under `as const` every value becomes its own literal type, so
  `zhCN: Resources` would demand the English string at each key -- exactly
  backwards. Plain inference keeps the key structure required while letting
  values differ, so a missing translation fails `tsc` (TS2741) instead of
  rendering a raw key at a customer.
- **The switcher labels each language in its own language** and never
  translates them: someone who has landed in a language they cannot read has
  to recognise their own by sight to get out.

`src/test/setup.ts` imports `lib/i18n` so `t()` returns real copy in tests;
it does **not** pin a language, because English is already the default.

Two shared modules exist so copy cannot drift between surfaces again:
`lib/runStatus.ts` (a run's wire status to a readable label; an unrecognised
status renders as-is rather than being hidden) and `lib/traceEvents.ts`'s
`useFriendlyEventTitle` (the customer-facing narration, alongside the existing
technical `EVENT_LABELS`).

## Styling (`index.css`)

The palette lives as CSS custom properties on `:root`, with dark redefined
under `prefers-color-scheme` **and** under `[data-theme="dark"]`. Components
read tokens and must never declare a colour inside a media block -- a colour
whose only definition sits behind one never applies in the un-stamped state.
One-off semantic hues (the per-event-type left borders in the trace) stay as
literals on purpose.

Two tokens exist because a *pair* has to stay coordinated, not just a single
colour: **`--accent-contrast`** is the foreground for anything sitting on
`--accent` (a literal `white` reads at 6.3:1 on the light accent and 2.6:1 on
the pale dark one), and **`--info-text`** completes the info trio, since
`--danger`/`--success` already double as their own text colour and info had
none. The rule this encodes: whenever a background comes from a token, its
foreground must come from one too, or the pair only survives in one theme.

## Confirmations (`lib/useConfirm.tsx`)

There is no `window.confirm` in this app. `useConfirm()` returns
`[node, confirm]` and is promise-shaped so a call site reads
`if (!(await confirm({...}))) return`, matching the shape it replaced.
`ConfirmDialog` uses `<dialog showModal()>` for focus trapping and Escape;
tests answer it via `test/confirmDialog.ts`, which finds the dialog by its
Cancel button because jsdom has no `showModal()`.

An optional `alternateLabel` adds a **third** answer, rendered between Cancel
and the confirm button, and the promise then resolves to `'alternate'` instead
of a boolean (`ConfirmResult`). It exists because the documents upload has
three answers to one question -- cancel, add to the existing collection,
replace it -- and asking that as two dialogs in sequence makes the second one
read like a trick. `'alternate'` is truthy and is unreachable without passing
`alternateLabel`, so every existing `if (!ok)` call site keeps working
untouched. `answerAlternate()` in `test/confirmDialog.ts` clicks it;
`answerConfirm(true)` still clicks the last button, which is still the confirm
one.

## Frontend — wizard UI (`ui/frontend/src/`)

The Team Builder wizard is **six steps**
(`components/WizardProgress.tsx`'s `STEPS`): Your challenge (`/wizard`,
`IntentPage`), A few questions (`questions`, `QuestionsPage` — the analyst's
clarifying questions, one textarea each; IntentPage routes here only when the
generated requirements carry any, so a `fake:`-catalog deployment and a
failed requirements call still go straight to Documents), Your documents
(`documents`), Meet your team (`preview`), Confirm (`confirm`), Go live
(`deploy`). A step is unlocked by **data presence** (`session.requirements_json` / `specification_json`), not by the
session's `status` string, so revisiting an earlier stage never relocks a
later one.

`react-router-dom` (`main.tsx` wraps `<App/>` in `<BrowserRouter>`, itself
inside `components/ErrorBoundary.tsx` — the one render-error boundary, so a
thrown render shows "Something went wrong" + Reload instead of a blank page;
it never renders the raw error text) drives
several areas, all under a shared `<Layout/>` nav shell (`components/Layout.tsx`;
customer nav is Dashboard / Build a team / My teams / Run a team):

- **`/`** — `pages/LandingPage.tsx`: not a page but a router. It forwards an
  org member to `/activity` (the Dashboard), or to `/wizard` if the org has no
  deployed pipeline yet; `RequireOrgMember` sends an admin to `/advanced`
  before it renders. "Run a team" is a deliberate destination (`/run`), not
  the daily home.
- **`/run`** — `pages/MonitorPage.tsx`, "Run a team" (renamed from "Talk to
  your team"): reads an optional `?pipeline=` query param via
  `useSearchParams` to pre-select a team, shows a running timer/WS
  connection status/"waiting for your team" hint/stale-run banner while a
  run is in flight, and a Stop button (`POST /api/runs/{id}/cancel`) gated
  on the new run's id having actually arrived, so an early click can't
  silently no-op or target the previous run. Live events render via the
  shared `lib/traceEvents.ts` helpers (`EVENT_LABELS`/`RESULT_LABELS`/
  `TERMINAL_TYPES`/`renderEventData`), also used by `components/RunDetail.tsx`.
- **`/activity`** — `pages/ActivityPage.tsx`. **Which tab opens depends on
  whether the org actually uses automation** (`getEmailTrigger()`): otherwise
  a customer who has never connected a mailbox landed on a feature they don't
  use, with their own runs a click away. The tab strip renders immediately
  (each panel keys off `tab === '...'`, so an undecided tab shows none) and
  the late-arriving default is applied through `setTab(current => current ??
  ...)` so it can only fill the undecided case -- a customer who clicked while
  the request was in flight must not be pulled off their choice. The tabs: an
  Automations tab
  (`components/EmailTriggerActivity.tsx`, plus — for the Property Maintenance
  Inbox vertical template, see `ui/backend/CLAUDE.md` — `components/
  MaintenanceInboxSummary.tsx` fetching `GET /api/automation-results/summary`
  and `components/NeedsAttentionList.tsx` fetching `GET /api/automation-results
  ?needs_attention=true`; both render nothing for an org that isn't using this
  template, both refresh on the same 30s cadence while the tab is open (rather
  than only on mount), and `NeedsAttentionList`'s "View run" jumps to the Runs
  tab and opens that run's detail -- `ActivityPage`'s `onOpenRun` looks up the
  run's real, persisted status via `GET /api/runs?run_id=` (org-scoped, DB-backed,
  unlike `GET /api/runs/{id}`'s in-memory-registry-only route) before opening it,
  falling back to `completed` only if that lookup itself fails; a needs-attention
  item's run is not guaranteed to have completed -- a dispatch failure still
  synthesizes needs_attention error rows for its UIDs -- so hardcoding `completed`
  used to permanently hide the Retry button for one that actually failed (Codex
  review finding)) and a Runs tab (`GET /api/runs`, filterable by
  team/manual-or-automatic/status; polls every 5s while a listed row is still
  `running`, guarded against a stale poll response clobbering a
  since-changed filter's results). Clicking a run opens
  `components/RunDetail.tsx` in a panel: a `running` run streams live over
  the same WebSocket `MonitorPage` uses, anything else fetches
  `GET /api/runs/{id}/trace` once (no live/historical merge); `RunDetail`
  also fetches that run's `GET /api/automation-results?run_id=` (renders
  nothing for a run with none, and refetches when a live run's terminal event
  arrives, since `normalize_run_result` only writes results after the run
  finishes -- and now always writes/publishes in that order server-side,
  closing a previous race where the refetch could arrive before the rows
  existed; results include `classification`/`category`/`missing_information`/
  `risk_reasons`, not just status/priority/summary/address/reason/draft) and,
  for a `failed` run OR one whose live event stream just emitted `run_failed`
  (the `status` prop alone is set once at click time by `ActivityPage` and
  never updates while the panel stays open, so a run that fails mid-view
  needs this second signal or Retry wouldn't appear until the panel is
  closed and reopened) **and** is autonomous (`autonomous` prop, threaded
  from `GET /api/runs`' own `autonomous` flag through `ActivityPage`'s
  `selectedRun` -- a manual run has no `trigger_context` and always 400s from
  `POST /api/runs/{id}/retry`, so Retry must not even render for one), shows
  a Retry button that calls the `onRetried(newRunId)` prop on success --
  `ActivityPage.tsx` wires this to select the newly created run (always
  itself autonomous) with the same `setTab('runs')`/`setSelectedRun()`
  pattern `NeedsAttentionList`'s "View run" uses (which also always passes
  `autonomous: true`, since every automation result belongs to an autonomous
  run by construction). See `ui/backend/CLAUDE.md` ("Granular trace events,
  cancellation, and run history", "Property Maintenance Inbox").
  Email Phase 4a's two settings panels, `components/EmailFilterSettings.tsx`
  (the `skip_bulk` checkbox and the three pattern lists, one entry per line)
  and `components/EmailBudgetSettings.tsx` (the daily message cap, shown with
  what has been used against it today), live on the **deployed email team's
  own Deploy page** (`pages/wizard/DeployPage.tsx`, gated on `session.uses_email`
  alongside the existing `EmailConnect`/`EmailTriggerToggle`), not on the
  Activity page — they configure that team, the same way those two already
  do; a customer looking for "how does my email team behave" shouldn't have
  to go hunting in a monitoring tab for it. (They used to sit under
  `EmailTriggerActivity` on the Automations tab; moved because both are
  settings, not activity — audit finding, 2026-08-20.) `EmailFilterSettings`'s
  own copy points back to the Automations tab's "Mail we skipped" list by
  name rather than "above" it, since the two are no longer on the same page.
  Three things there are deliberate. **Neither panel renders a form when its
  initial GET failed** (unlike `WebhookSettings`, whose skeleton they
  otherwise follow): saving an empty form here is a data-loss path, since empty
  textareas replace real filter rules with none and an empty cap box sends
  `null` and removes a real cap. **An empty cap box sends `null`, not
  `0`** — `lib/budgetCaps.ts`'s `parseCap` returns `null` for empty,
  `undefined` for something that is not a number (which `save()` refuses with
  an error line rather than letting `NaN` serialise to `null`, i.e. "no
  limit"), and a literal `0` stays `0`, because a cap of zero is a real
  setting; it lives in `lib/` with its own tests because exporting a helper
  from a component file is a `react-refresh/only-export-components` lint
  error. **The monthly spend cap (`monthly_cost_cap`) has no field here at
  all** — like the model actually used and exactly how much has been spent
  (`spent_this_month`/`unpriced_models`/`unpriced_runs_this_month`, still
  absent from the API-response fields this panel renders), a dollar figure is
  admin-only; `EmailBudgetSettings` still loads `monthly_cost_cap` from
  `GET`, purely so `save()` can send it back unedited rather than the PUT's
  full-replace semantics silently clearing an existing cap.

  `EmailTriggerActivity` gained a second card, **"Mail we skipped"**
  (`GET /api/org/email-trigger/filtered`), listing each filtered message's
  reason, UID and detection time with a Release button. The reason is the API's
  `describe()` sentence; the raw `decision` appears only as the element's
  `title` attribute, since a customer should never be shown `bulk:list-id`.
  It identifies a message by **UID, not sender and subject** (which the design
  sketched): `inbox_events` holds an IMAP UID, the customer's own mailbox
  identity, a status and a decision — and no message content at all, which is
  exactly why that table needs no retention purge —
  showing who a skipped message was from would mean storing the sender on it.
  So an admin releases on the strength of the rule that fired, not of the
  message.
  **A release updates local state rather than refetching** — `release()` awaits
  the API then drops that row from `filtered`, and a `releasedRef` set of ids
  released this session also filters the results of the component's existing
  30s poll, so a response already in flight when Release was clicked cannot put
  the row back on screen. Note the section lives *inside* `EmailTriggerActivity`,
  after its two early returns, so it renders only when a trigger with a
  `pipeline_name` is configured: right today (filtered rows cannot exist before
  a trigger has polled), but historical filtered rows become unreachable if a
  customer later disconnects the mailbox.

  A fourth **Alerts** tab (email Phase 3a) holds
  `components/NotificationsPanel.tsx` (the org's alert history, read-only by
  design — these are raised by the system, and a delete verb would only let
  someone erase the record of a fault they never fixed) and
  `components/WebhookSettings.tsx` (one optional webhook per org). The tab
  label carries the unread count, which `ActivityPage` **fetches itself** (and
  polls, `ALERT_POLL_INTERVAL_MS`) as well as taking from the panel's
  `onUnreadChange`. Both, not just the callback: the panel is mounted only
  once the Alerts tab is open, so a callback-only badge could never appear
  before the user had already gone looking — the one thing it exists to save
  them.
  `WebhookSettings` **omits `webhook_secret` from the payload entirely** when
  the field wasn't retyped — the API never returns it, so resending an empty
  string would wipe the stored one. The explicit "remove the stored secret"
  checkbox is how an org goes back to unsigned delivery: the API takes an
  empty string for that and a blank field can't mean two things.

  A fifth **Data** tab (email Phase 3b) holds
  `components/DataRetentionPanel.tsx`: the org's run-history retention period,
  a JSON export, and an immediate "Delete now" behind a typed confirmation.
  Two things there are deliberate. The "will remove N runs" line is driven by
  the **saved** period, not the currently selected one — the backend computes
  `purgeable_now` from stored policy, so showing it against an unsaved
  selection would print a false number on the one screen that must not lie; an
  unsaved selection says "Not saved yet." And "Delete now" is disabled under
  "Keep forever", because `POST /api/org/retention/purge` requires an explicit
  window and sending `0` there would silently mean *everything*.

  A purged run must never render as an empty timeline — that reads as a bug.
  `RunDetail` shows "the content of this run was removed on <date> by your data
  retention settings" from `content_purged_at` (threaded through
  `lib/useRunTrace.ts`'s existing fetch, not a second request), and both
  automation-result lists render a purged item's empty payload as "Content
  removed" rather than blank fields — including suppressing the "No draft
  created" line, which would assert something false: `source_key`/`status`
  survive a purge precisely to record that a draft does exist.
- **`/advanced`** — `pages/AdvancedPage.tsx`, raw-JSON CRUD over
  `/api/config/{pipelines|skills|knowledge_bases|model-catalog}` plus a
  read-only `tools` tab — the operator-only "advanced view" for direct edits.
  Tabs run whole-then-parts (Pipelines → Skills → Knowledge bases → Tools →
  Model catalog). Each `KINDS` entry carries an `orgScope` mirroring the
  backend: `required` (`?org=` or 422), `optional` (skills — omitted means the
  platform built-in tier), `none` (org-less). **A pipeline is what the wizard
  and customer UI call an "AI team"**; this page uses the technical noun
  because it matches the JSON keys the operator is editing.
  Skill rows show their current immutable version; saving appends a version,
  while already-deployed teams keep their pinned version until redeployed.
  The item list carries a display-only filter (`filteredItems` derives from
  `visibleItems`, which remains the org-scoping decision, so a hidden row can
  never become a mutation target; it clears on tab/org change alongside the
  selection). Delete is outlined-danger rather than sharing Save's weight.
- **`/wizard`** (+ `/wizard/:sessionId/{questions|documents|preview|confirm|deploy}`)
  — the six-step Team Builder wizard, `components/WizardLayout.tsx` as the
  shared chrome:
  - `lib/api.ts` — shared `fetch` wrapper (`API_BASE`/`WS_BASE` default to
    `http://localhost:8000`) exposing every backend endpoint as `api.*`
    methods.
  - `lib/useBuilderSession.ts` / `lib/useModelCatalog.ts` — fetch-on-mount
    hooks; `WizardLayout` calls `useBuilderSession(sessionId)` once and hands
    `{session, setSession, loading, refresh, sessionId}` to the active stage
    page via `useOutletContext()`.
  - `components/WizardProgress.tsx` — the 5-step progress bar. A step is
    "unlocked" based on **data presence** (`session.requirements_json` /
    `session.specification_json`), not the session's `status` string, so
    revisiting earlier stages after a `solution`/`testing`/`deployed` status
    doesn't relock later steps. A `busy` prop suspends *every* step link:
    `WizardLayout` owns that flag and a stage page raises it through the
    outlet context's `setNavBusy` while a long request is in flight. It exists
    because "Go live" unlocks on the specification merely existing, so it
    stayed lit while the Architect was redesigning that very specification --
    one click mid-update and the customer publishes a team they have not seen.
    The top nav is deliberately **not** blocked: these actions have no cancel,
    so a hung request would otherwise trap the customer with no way out, and
    leaving costs nothing anyway (`/refine` commits in one transaction and
    `useBuilderSession` refetches on return).
  - `pages/wizard/*.tsx` — one page per step. `IntentPage` has no
    `sessionId` yet and creates the session via `api.createSession()`;
    `QuestionsPage` is the interview (spec:
    `docs/superpowers/specs/2026-08-24-clarifying-questions-design.md`):
    Continue needs at least one non-blank answer, "Skip these questions" is
    always available, and both send the full paired batch to
    `POST /requirements`' `answers` field — a blank answer is a deliberate
    skip the analyst converts into an `Assumed:` constraint. Revisiting with
    no open questions shows a short card straight through to Documents;
    `DocumentsPage` uploads a knowledge base (or skips) and then generates --
    or, revisiting after a spec exists, *refines* -- the Specification;
    `PreviewPage` renders `TeamFlow` and runs a test run over the same
    `/api/runs/{id}/stream` WebSocket as `MonitorPage`; `ConfirmPage` is
    **two stages stacked on one page** and is ordered cause-before-effect:
    the Requirements panel ("what we understood about your business",
    expanded by default) sits above the Specification one ("Your team"),
    because the team is derived from that understanding. It has exactly
    **one action** -- "Update the team", below both panels, calling
    `POST /refine` -- and that action carries *both* of the customer's
    inputs: the Requirements fields they edited by hand and whatever they
    described in the single free-text box. There is deliberately no separate
    save button and no second change box. Until 2026-08-23 there were three
    buttons ("Save this summary" / "Regenerate summary" / "Update the team")
    whose effects a customer could not tell apart, and two of which could
    destroy the third's work: regenerating discarded unsaved field edits, and
    a saved summary left the deployed team unaware of it with nothing on
    screen saying so. The fields stay directly editable -- adding a goal is a
    precise act that a natural-language round trip does worse -- they just
    have no button of their own. The button is never gated on the text box
    being non-empty (a customer may only have edited a field). Open
    clarifying questions render inside the Requirements panel as
    per-question textareas whose **non-blank** answers also ride the one
    action (`/refine`'s `answers` field) — a blank answer there is not a
    skip, the question just stays open; drafted answers reset whenever
    `requirements_json` regenerates, since that round retires or replaces
    the questions they belonged to. While that
    request is in flight the page disables everything it owns -- both
    textareas, every `BulletEditor` (which took a `disabled` prop for this),
    the upload link, Back to preview and Continue to deploy -- and shows one
    honest waiting line asking the customer to stay put. One line, not
    `DocumentsPage`'s staged labels: `/refine` is a single request, so the
    page genuinely cannot see the Analyst hand over to the Architect, and
    faking that handover would be inventing progress. There is no
    model picker: the page owns the catalog and the default model, since
    which model runs the Architect is a platform choice the customer never
    sees. `DeployPage` calls `api.deploySession()` and links
    to `/run?pipeline=<name>`.
  - `components/TeamFlow.tsx` + `EmployeeCard.tsx` — the customer-facing
    "meet your team" diagram: renders `Specification.teams`/`agents` as
    grouped "virtual employee" cards (avatar-initial + `display_name` +
    `friendly_description`, falling back to `name`/`role`/`goal`), laid out
    per `team.mode` (sequential = arrows between cards, parallel =
    side-by-side, hierarchical = manager card above member cards). No
    Mermaid — pure CSS/HTML, since the audience is non-technical.
  - All wizard pages/components share styles from
    `components/WizardLayout.css` (cards, fields, buttons, banners, bullet
    editor, team-flow/employee-card, activity feed).

## My documents panel (`components/KnowledgeBasesPanel.tsx`)

The **"My documents"** section at the bottom of **My teams**
(`pages/wizard/SessionsPage.tsx`), listing the org's own
knowledge bases from `GET /api/org/knowledge-bases`: one derived status line
per row (never indexed / processing / ready with a document count and an
expandable list of skipped files / failed with the job's own error, plus "an
earlier version is still in use" when `servable`), which teams use it, and
Delete behind a `window.confirm`. Delete is disabled with an explanatory
`title` while an upload is processing or a live team depends on it — the
backend 409s either way, this only saves a pointless click — and a refused
delete's message renders on the row it belongs to, since the 409 names *that*
collection's teams. It polls every 3s **only while some row is processing**
(the Activity page's poll-while-running rule) and hides itself entirely when
the org has no knowledge bases, so it costs an org that never uploaded
anything one request.

Each row also lists its documents behind a **"Show N documents"** toggle
(`kb.documents` from the summary: name, size, and "couldn't be read" for a
`failed` one), each with its own **Remove** — `DELETE
/api/org/knowledge-bases/{name}/documents/{filename}` behind a confirm that
names the teams using the collection (removal is allowed while they do, as
adding is; the reader should know whose answers change). Remove is disabled
with the reason in its `title` while an upload is processing and for the only
document (delete the collection instead — the backend 409s both). A success
re-fetches the list so the row shows "Processing…" and the existing poll picks
up the new generation; a refusal's message renders on the row, like a refused
delete's. The mock factory in `KnowledgeBasesPanel.test.tsx` lists
`removeOwnKnowledgeBaseDocument` for the same reason it lists
`searchOwnKnowledgeBase`.

Each row also has **"Restore previous upload"** beside "Try a search" —
`POST /api/org/knowledge-bases/{name}/restore` behind a confirm that lists the
filenames coming back (`kb.previous_generation.filenames`) and the teams using
the collection. Disabled with the reason in its `title` while an upload is
processing and when `previous_generation` is null (one upload so far, or the
files are gone). A success marks the row `queued` from the 202 and re-fetches,
like a removal; a refusal's message renders on the row. The mock factory lists
`restoreOwnKnowledgeBase`. The panel's copy is still English-only literals
(the F1 long tail).

A row whose latest job **failed** also shows a **"Retry"** button —
`POST /api/org/knowledge-bases/{name}/ingestion-jobs/{job_id}/retry`, no
confirm (re-running the same staged files destroys nothing). Enabled only
when the summary's `latest_job.retryable` is true (the staged files are still
on the server); disabled otherwise with the reason in its `title`. A success
marks the row `queued` from the 202 (the retry keeps the same job id) and
re-fetches; a refusal's message renders on the row. The mock factory lists
`retryOwnKnowledgeBaseIngestion`.

Each row also carries a **"Try a search"** toggle beside Delete, opening
`components/KnowledgeBaseSearch.tsx` underneath it: one query box, and the
passages `POST /api/org/knowledge-bases/{name}/search` returns, each under the
same citation label the agent's own tool output cites. It is the only place a
customer can check retrieval before a team is built on these documents rather
than after it starts answering oddly. The toggle is enabled only for
`kb.servable && !isProcessing(kb)` (`searchBlockedReason`, the same
save-a-pointless-click role `deleteBlockedReason` plays), with the reason in
its `title`. It cannot pre-empt every refusal: a legacy collection served from
disk reports `servable`, and the endpoint refuses *that* one only when the
search actually runs — so the box shows the backend's own message inline, the
same way a refused delete does. A search that matched nothing renders "No
matching passages." rather than an empty list, `text` arrives capped at 1,500
characters server-side and is rendered `pre-wrap` (a chunk keeps the
document's own line breaks), and the button is disabled while a search is in
flight so one click is one search — each one can cost a query embedding. Note
`KnowledgeBasesPanel.test.tsx`'s `../lib/api` mock factory must list
`searchOwnKnowledgeBase`: the toggle renders this child, which calls it.

## Anonymous team sharing (`/share/:token`)

The one **public, unauthenticated route** in this app (outside `RequireAuth`
entirely): `pages/ShareChatPage.tsx`, a multi-turn chat a colleague reaches
via a link an org member generated. Design:
`docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md`;
backend: `ui/backend/CLAUDE.md`.

- `lib/shareChatApi.ts` is a **separate client from `lib/api.ts`** and must
  stay one: it sends no bearer token and instead passes
  `credentials: 'include'` so the backend's signed `share_auth` session
  cookie round-trips. `lib/api.ts`'s `request` never needs cookies at all.
  Both share `API_BASE`/`WS_BASE`, which default to **`localhost`, not
  `127.0.0.1`** — the visitor cookie is `SameSite=Lax` and a browser treats
  those as different sites, so a mismatch with Vite's own `localhost:5173`
  silently breaks continuous chat entirely.
- `lib/shareTraceEvents.ts`'s `friendlyStatusFor` maps a run's event stream
  to one short non-technical line — it returns an i18n key under
  `share.status.*` (a literal union, because `t()` is typed against the
  locale keys), and the page translates it. Cosmetic only — the backend
  already strips everything but the event `type` (plus the final answer)
  before it reaches this socket, so devtools show nothing more than the UI
  does. The same module holds `FALLBACK_REPLY`/`DISPATCH_FAILED_REPLY` — the
  two replies the backend persists in English — and `fallbackReplyKey`, so
  the page renders them in the visitor's language by string equality (a
  deliberate, brittle coupling; see docs/STATUS.md Known issues).
- The page is bilingual via the `share.*` namespace and carries its own
  `components/LanguageSelect.tsx` in a header bar — the same switcher
  `Layout.tsx` renders, extracted because this route is outside `<Layout/>`.
  Same `bestteam_lang` key, so a visitor's choice sticks. The composer is a
  `<textarea>`: Enter sends, Shift+Enter is a newline, and Enter during IME
  composition (`isComposing` / keyCode 229) is ignored so a Chinese visitor
  never sends half a sentence. Each assistant bubble has a Copy control
  (outside the bubble, so tests still match the bubble's text alone).
  Colours come from tokens (`--accent`/`--accent-contrast`) and the page is
  `100dvh` so a phone's collapsing address bar can't hide the composer.
  Transient notices are stored as i18n keys (never the backend's detail) and
  translated at render; the header/language control also renders on the
  "link unavailable" page. `lib/dateFormat.ts::formatDateTime` is
  locale-aware (keyed on `i18n.resolvedLanguage`; Chinese dates in Chinese)
  for every panel that shows a date, and `endOfLocalDay` lives there too.
  Spec: `docs/superpowers/specs/2026-08-22-share-chat-beta-patch-design.md`.
- **The reply streams** (step 2, 2026-08-23; spec:
  `docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md`).
  `reply_delta` events append to a `streamedReply` string that renders in an
  assistant bubble with a caret, replacing the status line while it fills;
  `reply_reset` clears it. It is only ever a **preview** -- `run_completed`
  discards it and appends the authoritative text, so nothing partial is ever
  kept. Deltas deliberately never join `liveEvents`, so the progress
  indicator keeps counting agents rather than tokens.
  `components/ShareProgress.tsx` renders "Step n of N" from the count of
  `agent_completed` events (which reach this page type-only) against the
  `steps` the team endpoint supplies, clamped, or an anonymous pulse when
  `steps` is null -- a position, never a name, a role or a model. The header
  shows the team's name, falling back to the brand if that fetch fails: a
  failure there costs the header and the count, never the chat. While a turn
  is in flight the Send button becomes **Stop**, gated on the run id having
  actually arrived (the same rule `MonitorPage`'s Stop follows).
- `components/MarkdownText.tsx` renders an assistant reply as markdown
  (`react-markdown` + `remark-gfm`, **no `rehype-raw`** -- model output is not
  trusted markup, so raw HTML stays inert text, which is the reason this is a
  library rather than hand-rolled). It is shared with
  `SharedSessionsPanel`'s transcript so an admin sees exactly what the visitor
  saw. A visitor's own message stays plain text: their typing is not markup.
- `lib/shareTraceEvents.ts` now carries a **third** backend-persisted English
  literal, `STOPPED_REPLY`, for a stopped turn. The live view used to render
  the generic failure line for `run_cancelled` while the backend stored the
  "stopped" one, so a reload contradicted what the visitor had just seen.
- Org side, both on **My teams**' team cards only (`pages/wizard/
  SessionsPage.tsx`, gated on `session.pipeline_id != null` — a YAML-only
  demo pipeline has no `PipelineRecord.id` to hang a share link off):
  `components/ShareLinksPanel.tsx` is the click-to-expand "Share" panel
  (generate/copy/revoke links for that team — a link's daily message cap and
  optional expiry are set at creation, `shareLinks.*` strings; to change
  them, revoke and regenerate), and
  `components/SharedSessionsPanel.tsx` is the read-only audit view beside it
  (list the team's visitor sessions, read a session's transcript;
  `sharedSessions.*` strings), behind its
  own "Shared sessions" toggle — `SessionsPage`'s own `openAudit` state, not
  `SharedSessionsPanel`'s concern, so a page listing many teams doesn't fire a
  `listShareLinks`/`listShareSessions` fetch per card on load (same reasoning
  as `ShareLinksPanel`'s own collapse-by-default "Share" button). Both used to
  also render on **Run a team** (`pages/MonitorPage.tsx`) — removed as
  redundant with My teams (audit finding, 2026-08-21) — and, before that, a
  separate **Shared** tab on `pages/ActivityPage.tsx` (the Dashboard) — also
  removed, same reasoning, one round earlier.

## Auth and login UI

`lib/api.ts` stores a bearer token in `localStorage` (key `bestteam_token`),
attaches `Authorization: Bearer <token>` to every request, and on a `401`
(except from `/api/auth/*`, to avoid masking login errors) clears the token
and redirects to `/login`. `pages/LoginPage.tsx` is the username/password
form; `App.tsx`'s `RequireAuth` route guard redirects to `/login` when no
token is present, and `components/Layout.tsx` has "Change password" and
"Log out" buttons.

**The login page is the one screen outside `Layout`**, which is why it renders
its own `BrandMark` and its own `LanguageSelect`: before the `login.*`
namespace existed it was hardcoded English with no language control on it at
all, so a Chinese customer's very first impression was untranslatable however
bilingual the rest of the app was. Two-panel layout (brand + three shipped
capabilities on the left, form on the right) collapsing to one under 820px,
where the bullets are hidden rather than stacked — they are decoration, and on
a phone they would push the form below the fold. The slogan is
`nav.tagline`, **one key for the whole app** — the login page and
`WizardLayout`'s `<h1>` both read it. It used to be three near-copies that had
already drifted ("best AI team out" / "your best AI team out" / "best AI teams
out") while the Chinese side had one string all along; `README.md`'s copy is
the fourth and, being outside the bundle, is the one to keep in step by hand.
`components/BrandMark.tsx` is a pure extraction of the SVG
that was inline in `Layout.tsx`, with its two fills moved out of `Layout.css`
alongside it.

`#username`, `#password`, `button[type=submit]` and `.banner-error` on that
page are a contract with `tests/e2e/test_smoke.py`, which drives the real page
through exactly those selectors; `LoginPage.test.tsx` asserts each of them so
a rename fails in the unit tier instead of the e2e one.

**`components/ChangePasswordDialog.tsx`** is the self-service change, built on
`<dialog showModal()>` like `ConfirmDialog` (same jsdom `showModal` fallback).
It posts to `POST /api/auth/password` and **swaps the returned token into
`localStorage` immediately** — the change revokes the old one, so any request
made before the swap would 401. Its success state stays on screen rather than
closing, because the customer needs telling that every other session has just
been signed out. Client-side it only blocks the obvious (empty current, new
under 8 characters, a mismatched confirmation); the backend is the authority
on all three.

**Role-aware routing.** A platform operator (`is_admin`, `org_id IS NULL`) and
an org member see disjoint UIs, partitioned by two symmetric `App.tsx` guards
that both read `lib/useMe.ts` (one `GET /api/auth/me` → `{username, is_admin,
org}`) and render `null` while it loads:

- `RequireAdmin` wraps `/accounts` + `/advanced` + `/memory` + `/trace` + `/feedback`; non-admins are sent to `/`.
  On `/trace` (`pages/TracePage.tsx`), `components/AdminRunDetail.tsx` offers
  **"Diagnose this run"** on a finished, non-diagnostic run
  (`api.diagnoseRun` → `POST /api/runs/{id}/diagnose`, see
  `ui/backend/CLAUDE.md`); the page then selects the new run, which streams
  into the same panel under a banner naming the original (with an "Open
  original run" button and a redeployed-since notice when `version_changed`).
  `lib/traceEvents.ts` carries labels/summaries for the diagnostic-only
  `agent_prompt`/`model_turn` events, and the detail view collapses those
  payloads (and a `tool_completed.result`) behind a `<details>` so one long
  prompt doesn't bury the timeline. The run list badges diagnostic runs; the
  customer's Activity page never receives one (filtered server-side).
- `RequireOrgMember` wraps the customer routes (`/`, `/run`, `/teams`, `/activity`, `/wizard/*`);
  operators are sent to `/advanced`, since every org-scoped surface 403s an
  org-less operator. The `*` catch-all stays **outside** both guards so an
  unknown path routes to `/`, where `RequireOrgMember` picks the destination.

Because `is_admin` and org membership are mutually exclusive (CR-030), the two
guards can't bounce a user between them — each redirect terminates in one hop.
`Layout.tsx` mirrors this: the **Accounts**/**Advanced**/**Memory**/**Trace**/
**Feedback** links show only when `isAdmin`, the **Dashboard**/**Build a team**/
**My teams**/**Run a team** links only when `!isAdmin`. `pages/AccountsPage.tsx` is the admin org/user
manager (create orgs, deactivate/reactivate them, and create/reset-password/
move/delete each org's member; platform accounts are shown read-only — the
`/api/admin` surface keeps promote/demote and platform-account lifecycle in the
CLI). `pages/MemoryPage.tsx` is the admin per-user memory manager
(user list with counts + search/type-filter + per-record delete + clear-all,
and a "memory not enabled" state). `pages/FeedbackPage.tsx` is the admin
feedback triage list (status/kind filters, expandable rows, status + note
saved via `api.patchFeedback`); bodies render as plain text only — visitor
text is untrusted. The submit side is `components/FeedbackModal.tsx`, one
bilingual `<dialog>` shared by two entry points that own their own POST:
the `Layout` nav button (org members only — an admin's nav has the triage
NavLink under the same `nav.feedback` label instead) posting
`api.submitFeedback` with `{page, locale}` context, and the share-chat
header (`ShareChatPage`) posting `shareChatApi.sendFeedback` with the last
dispatched `run_id` added. All of this gating is cosmetic — the backend
enforces admin on every `/api/config` and `/api/memory` call and org scoping on
every customer surface, so a tampered client still gets 403.
`API_BASE`/`WS_BASE` are configurable via `VITE_API_BASE`/`VITE_WS_BASE`
(see `ui/frontend/.env.example`), falling back to `localhost:8000` for local
dev — `localhost`, not `127.0.0.1`, for the SameSite reason above.
