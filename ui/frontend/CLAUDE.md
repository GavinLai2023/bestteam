# bestteam — `ui/frontend/` (React + Vite)

Monitoring dashboard and Team Builder wizard. Root `CLAUDE.md` for the overview;
`ui/backend/CLAUDE.md` for the API this talks to; `docs/DECISIONS.md` for
reasoning; the dated specs under `docs/superpowers/specs/` and git history for
per-feature narrative.

## Internationalisation (`lib/i18n.ts`, `locales/`)

Bilingual, **English by default**, switchable from the nav. Three load-bearing
rules:

- ⚠️ **No `navigator.language` detection.** Resolution is
  `localStorage['bestteam_lang']` then `'en'`, full stop. Auto-detecting would
  make the default drift with each visitor's browser and make both Vitest and the
  Playwright E2E run depend on the locale of whatever machine executes them.
- ⚠️ **`locales/en.ts` is the key source of truth and is deliberately NOT
  `as const`.** Under `as const` every value becomes its own literal type, so
  `zhCN: Resources` would demand the English string at each key — exactly
  backwards. Plain inference keeps the key structure required while letting values
  differ, so a missing translation fails `tsc` (TS2741) instead of rendering a raw
  key at a customer.
- **The switcher labels each language in its own language** and never translates
  them: someone who has landed in a language they cannot read has to recognise
  their own by sight to get out.

`src/test/setup.ts` imports `lib/i18n` so `t()` returns real copy in tests; it
does **not** pin a language, because English is already the default.

Two shared modules exist so copy cannot drift between surfaces:
`lib/runStatus.ts` (wire status → readable label; **an unrecognised status
renders as-is rather than being hidden**) and `lib/traceEvents.ts`, which holds **three
registers of the same event stream**: `useFriendlyEventTitle` (the collapsed
customer view: milestones only), `useDetailedEventLine` (**"Show details"** --
the same run step by step, still in the customer's words, dropping every event
that is platform machinery and returning `null` for it) and `EVENT_LABELS` +
`renderEventData` (the technical register, **admin trace page only** -- it names
tool identifiers, times calls in ms and reports memory/grounding).

## Styling (`index.css`)

The palette lives as CSS custom properties on `:root`, with dark redefined under
`prefers-color-scheme` **and** under `[data-theme="dark"]`.

⚠️ **Components read tokens and must never declare a colour inside a media
block** — a colour whose only definition sits behind one never applies in the
un-stamped state. One-off semantic hues (the per-event-type left borders in the
trace) stay as literals on purpose.

Two tokens exist because a *pair* has to stay coordinated: **`--accent-contrast`**
is the foreground for anything on `--accent` (a literal `white` reads at 6.3:1 on
the light accent and 2.6:1 on the pale dark one), and **`--info-text`** completes
the info trio, since `--danger`/`--success` already double as their own text
colour. **The rule this encodes: whenever a background comes from a token, its
foreground must come from one too, or the pair only survives in one theme.**

## Confirmations (`lib/useConfirm.tsx`)

**There is no `window.confirm` in this app.** `useConfirm()` returns
`[node, confirm]`, promise-shaped so a call site reads
`if (!(await confirm({...}))) return`. `ConfirmDialog` uses `<dialog showModal()>`
for focus trapping and Escape; tests answer it via `test/confirmDialog.ts`, which
finds the dialog **by its Cancel button** because jsdom has no `showModal()`.

An optional `alternateLabel` adds a **third** answer between Cancel and confirm,
resolving to `'alternate'` instead of a boolean. It exists because the documents
upload has three answers to one question — cancel, add, replace — and asking that
as two sequential dialogs makes the second read like a trick. **`'alternate'` is
truthy and unreachable without passing `alternateLabel`, so every existing
`if (!ok)` call site keeps working untouched.**

## Routing

`react-router-dom`; `main.tsx` wraps `<App/>` in `<BrowserRouter>`, itself inside
`components/ErrorBoundary.tsx` — the one render-error boundary, so a thrown render
shows "Something went wrong" + Reload instead of a blank page. **It never renders
the raw error text.** Everything but `/login` and `/share/:token` sits under a
shared `<Layout/>` nav shell.

| Route | Page |
|---|---|
| `/` | `LandingPage` — **not a page but a router**: forwards an org member to `/activity`, or `/wizard` if the org has no deployed pipeline; `RequireOrgMember` sends an admin to `/advanced` first |
| `/run` | `MonitorPage` — "Run a team" (a deliberate destination, not the daily home) |
| `/activity` | `ActivityPage` — the Dashboard |
| `/teams` | `SessionsPage` — "My teams"; each live team's Pause/Switch-back-on |
| `/wizard/*` | the six-step Team Builder |
| `/advanced`, `/accounts`, `/memory`, `/trace` | admin-only |
| `/share/:token` | the one **public, unauthenticated** route |

**`/run`** reads an optional `?pipeline=` to pre-select a team; shows a running
timer, WS connection status, a "waiting for your team" hint and a stale-run
banner; and a Stop button **gated on the new run's id having actually arrived**,
so an early click can't silently no-op or target the previous run.

## Activity page (`pages/ActivityPage.tsx`)

⚠️ **Which tab opens depends on whether the org actually uses automation**
(`getEmailTrigger()`) — otherwise a customer who never connected a mailbox lands
on a feature they don't use, with their own runs a click away. The tab strip
renders immediately (each panel keys off `tab === '...'`, so an undecided tab
shows none) and the late-arriving default is applied through
`setTab(current => current ?? ...)` **so it can only fill the undecided case** — a
customer who clicked while the request was in flight must not be pulled off their
choice.

**Automations tab** — `EmailTriggerActivity`, plus (for the Property Maintenance
Inbox template) `MaintenanceInboxSummary` and `NeedsAttentionList`. Both render
nothing for an org not using the template and refresh on the same 30s cadence
**while the tab is open**, not only on mount.

⚠️ `NeedsAttentionList`'s "View run" looks up the run's **real, persisted** status
via `GET /api/runs?run_id=` (org-scoped, DB-backed — unlike
`GET /api/runs/{id}`'s in-memory-registry-only route) before opening it, falling
back to `completed` only if that lookup fails. A needs-attention item's run is not
guaranteed to have completed (a dispatch failure still synthesises error rows), so
hardcoding `completed` permanently hid the Retry button for one that actually
failed.

**"Mail we skipped"** (`GET /api/org/email-trigger/filtered`) lists each filtered
message's reason, UID and detection time with Release. **The reason is the API's
`describe()` sentence; the raw `decision` appears only as the `title` attribute** —
a customer should never be shown `bulk:list-id`. It identifies a message by **UID,
not sender and subject**: `inbox_events` holds a UID, a mailbox identity, a status
and a decision, and **no message content at all** — which is exactly why that
table needs no retention purge. Showing who a skipped message was from would mean
storing the sender on it. So an admin releases on the strength of the rule that
fired, not of the message.

**A release updates local state rather than refetching** — `release()` drops the
row, and a `releasedRef` set also filters the 30s poll's results, so a response
already in flight when Release was clicked cannot put the row back on screen.
⚠️ The section lives *inside* `EmailTriggerActivity`, after its two early
returns, so it renders only when a trigger with a `pipeline_name` is configured —
right today, but **historical filtered rows become unreachable if a customer later
disconnects the mailbox**.

**Runs tab** — `GET /api/runs`, filterable by team/manual-or-automatic/status,
polling every 5s while a listed row is `running`, **guarded against a stale poll
response clobbering a since-changed filter's results**. Clicking opens
`RunDetail`: a `running` run streams live over the same WebSocket `MonitorPage`
uses; anything else fetches `GET /api/runs/{id}/trace` once (**no live/historical
merge, by design**).

`RunDetail` also fetches `GET /api/automation-results?run_id=` (nothing for a run
with none) and **refetches when a live run's terminal event arrives**, since
results are only written after the run finishes. Its Retry button needs three
conditions:

1. The run is `failed` **or** its live stream just emitted `run_failed` — ⚠️ the
   `status` prop alone is set once at click time and never updates, so a run that
   fails mid-view needs this second signal or Retry wouldn't appear until the
   panel is closed and reopened.
2. It is **autonomous** (threaded from `GET /api/runs`' own flag) — a manual run
   has no `trigger_context` and always 400s, so Retry must not even render.
3. On success it calls `onRetried(newRunId)`, which selects the new run.

**Alerts tab** — `NotificationsPanel` (**read-only by design**: these are raised
by the system, and a delete verb would only let someone erase the record of a
fault they never fixed) and `WebhookSettings`. The tab label's unread count is
**fetched by `ActivityPage` itself as well as taken from the panel's
`onUnreadChange`** — the panel mounts only once the tab is open, so a
callback-only badge could never appear before the user had already gone looking,
which is the one thing it exists to save them.

⚠️ `WebhookSettings` **omits `webhook_secret` from the payload entirely** when the
field wasn't retyped — the API never returns it, so resending an empty string
would wipe the stored one. An explicit "remove the stored secret" checkbox is how
an org goes back to unsigned delivery: **a blank field can't mean two things.**

**Data tab** — `DataRetentionPanel`: retention period, JSON export, and "Delete
now" behind a typed confirmation. ⚠️ **The "will remove N runs" line is driven by
the SAVED period, not the currently selected one** — the backend computes it from
stored policy, so showing it against an unsaved selection would print a false
number on the one screen that must not lie; an unsaved selection says "Not saved
yet." And **"Delete now" is disabled under "Keep forever"**, because the purge
endpoint requires an explicit window and sending `0` would silently mean
*everything*.

⚠️ **A purged run must never render as an empty timeline — that reads as a bug.**
`RunDetail` shows "the content of this run was removed on <date> by your data
retention settings" from `content_purged_at` (threaded through
`lib/useRunTrace.ts`'s existing fetch, not a second request), and both
automation-result lists render a purged item's empty payload as "Content removed"
rather than blank fields — **including suppressing the "No draft created" line,
which would assert something false**: `source_key`/`status` survive a purge
precisely to record that a draft does exist.

## Wizard (`/wizard`)

**Six steps** (`components/WizardProgress.tsx`'s `STEPS`): Challenge
(`IntentPage`) → Questions (`QuestionsPage`) → Documents → Your team
(`preview`) → Confirm → Go live (`deploy`). The step labels are short on
purpose -- six of them plus their chrome have to fit the wizard's 832px
content width without ellipsis.

⚠️ **A step is unlocked by DATA PRESENCE** (`session.requirements_json` /
`specification_json`), **not by the session's `status` string**, so revisiting an
earlier stage never relocks a later one.

`IntentPage` has no `sessionId` yet and creates the session. It routes to
`QuestionsPage` **only when the generated requirements carry questions**, so a
`fake:`-catalog deployment and a failed requirements call go straight to
Documents.

`QuestionsPage` is the interview (spec:
`2026-08-24-clarifying-questions-design.md`): Continue needs at least one
non-blank answer, "Skip these questions" is always available, and both send the
**full paired batch** to `POST /requirements`' `answers` field — a blank answer is
a deliberate skip the analyst converts into an `Assumed:` constraint. Revisiting
with no open questions shows a short card straight through.

`DocumentsPage` uploads a KB (or skips) then generates — or, revisiting after a
spec exists, *refines* — the Specification. `PreviewPage` renders `TeamFlow` and
runs a test run over the same stream WebSocket as `MonitorPage`.

Revisiting `DocumentsPage` for a team that already has one, no longer means a
blank name field and a 409 as the only clue what's there. `usedKbNames` derives
the team's existing collection(s) from `session.specification_json.agents[].tools`
intersected against `api.listOwnKnowledgeBases()` — exact, not a guess, since a
KB name can never collide with a built-in/skill tool name. Exactly one match
prefills the name (`effectiveLabel`, computed at render, not copied into `label`
via an effect); more than one shows a picker instead of guessing. The matched
collection's files render inline with a per-file Remove (`removeOwnKnowledgeBaseDocument`,
polled to completion like an upload). Uploading to a collection that already
existed before this visit pauses on a review panel (merged old+new file list,
one Continue button) instead of auto-advancing to spec generation — a brand-new
collection still proceeds straight through, unchanged.

Four rules there, each closing a way the page could act on the wrong thing:
**a matched existing name is used verbatim** (`resolveKbName`) — the server's
charset allows hyphens and capitals, so slugifying `support-docs` addressed a
different, non-existent collection; only free-text labels are slugified.
**Remove mirrors the endpoint's own two 409s** (processing, or the last
*readable* document) as a disabled button with the reason in `title`, like
`KnowledgeBasesPanel`. **The confirm names `used_by`** — the collection may be
shared, and removal changes what every team searching it can answer.
**"Already existed" is remembered from the 409 too**, not only from the list
fetched on load, which can fail or still be in flight. A failed removal renders
its own banner beside the list, never the page-wide `error` one — that one's
"Try Again" runs `proceed()`, i.e. billable spec generation.

### `ConfirmPage` — two stages stacked, exactly one action

Ordered cause-before-effect: the Requirements panel ("what we understood about
your business", expanded by default) sits **above** the Specification one ("Your
team"), because the team is derived from that understanding.

⚠️ **It has exactly one action — "Update the team", calling `POST /refine` — and
that action carries BOTH of the customer's inputs**: the Requirements fields they
edited by hand and whatever they described in the free-text box. There is
deliberately no separate save button and no second change box. Until 2026-08-23
there were three buttons whose effects a customer could not tell apart, two of
which could destroy the third's work. **The fields stay directly editable** —
adding a goal is a precise act a natural-language round trip does worse — they
just have no button of their own. The button is **never gated on the text box
being non-empty** (a customer may only have edited a field).

Open clarifying questions render inside the Requirements panel as per-question
textareas whose **non-blank** answers also ride the one action. ⚠️ **A blank
answer there is not a skip** — the question just stays open. Drafted answers reset
whenever `requirements_json` regenerates, since that round retires or replaces the
questions they belonged to.

While that request is in flight the page disables **everything it owns** — both
textareas, every `BulletEditor` (which took a `disabled` prop for this), the
upload link, Back and Continue — and shows **one** honest waiting line.
Deliberately one line, not `DocumentsPage`'s staged labels: `/refine` is a single
request, so the page genuinely cannot see the Analyst hand over to the Architect,
and faking that handover would be inventing progress.

**There is no model picker**: which model runs the Architect is a platform choice
the customer never sees.

### `WizardProgress`'s `busy` prop

Suspends *every* step link; `WizardLayout` owns the flag and a stage page raises
it through the outlet context's `setNavBusy`. ⚠️ It exists because **"Go live"
unlocks on the specification merely existing**, so it stayed lit while the
Architect was redesigning that very specification — one click mid-update and the
customer publishes a team they have not seen.

**The top nav is deliberately NOT blocked**: these actions have no cancel, so a
hung request would trap the customer with no way out, and leaving costs nothing
(`/refine` commits in one transaction and `useBuilderSession` refetches on
return).

### Supporting modules

- `lib/api.ts` — shared `fetch` wrapper exposing every endpoint as `api.*`.
- `lib/useBuilderSession.ts` / `lib/useModelCatalog.ts` — fetch-on-mount hooks;
  `WizardLayout` calls the former once and hands
  `{session, setSession, loading, refresh, sessionId}` to the active stage via
  `useOutletContext()`.
- `components/TeamFlow.tsx` + `EmployeeCard.tsx` — the "meet your team" diagram:
  `Specification.teams`/`agents` as grouped "virtual employee" cards
  (avatar-initial + `display_name` + `friendly_description`, falling back to
  `name`/`role`/`goal`), laid out per `team.mode`. **No Mermaid — pure CSS/HTML,
  since the audience is non-technical.**

## My documents panel (`components/KnowledgeBasesPanel.tsx`)

At the bottom of **My teams**, listing `GET /api/org/knowledge-bases`: one derived
status line per row (never indexed / processing / ready with a document count and
an expandable list of skipped files / failed with the job's own error, plus "an
earlier version is still in use" when `servable`), which teams use it, and Delete.

It polls every 3s **only while some row is processing** and **hides itself
entirely when the org has no knowledge bases**, so it costs an org that never
uploaded anything one request.

Every action follows the same shape: **disabled with the reason in its `title`
when the backend would 409 anyway** (this only saves a pointless click — the
backend is the authority), and **a refusal's message renders on the row it belongs
to**, since the 409 names *that* collection's teams.

| Action | Endpoint | Disabled when |
|---|---|---|
| Delete | `DELETE /{name}` | an upload is processing, or a live team depends on it |
| Remove one document | `DELETE /{name}/documents/{filename}` | processing, or it's the only document (delete the collection instead) |
| Restore previous upload | `POST /{name}/restore` | processing, or `previous_generation` is null |
| Retry | `POST /{name}/ingestion-jobs/{job_id}/retry` | `latest_job.retryable` is false |
| Try a search | `POST /{name}/search` | `!servable \|\| isProcessing` |

Removal and restore are **allowed while teams use the collection, as adding is** —
the confirm names those teams, because the reader should know whose answers
change. Retry has **no confirm** (re-running the same staged files destroys
nothing). A success marks the row `queued` from the 202 and re-fetches.

⚠️ **"Try a search" cannot pre-empt every refusal**: a legacy collection served
from disk reports `servable`, and the endpoint refuses *that* one only when the
search actually runs — so the box shows the backend's own message inline. A search
matching nothing renders "No matching passages." rather than an empty list; `text`
arrives capped at 1,500 chars server-side and renders `pre-wrap`; and the button
is disabled while a search is in flight so **one click is one search** — each can
cost a query embedding.

⚠️ `KnowledgeBasesPanel.test.tsx`'s `../lib/api` mock factory must list
`searchOwnKnowledgeBase`, `removeOwnKnowledgeBaseDocument`,
`restoreOwnKnowledgeBase` and `retryOwnKnowledgeBaseIngestion` — the toggles
render children that call them.

The panel's copy is still English-only literals (the F1 long tail).

## Anonymous team sharing (`/share/:token`)

The one public, unauthenticated route, outside `RequireAuth` entirely. Specs:
`2026-08-14-team-sharing-continuous-chat-design.md`,
`2026-08-22-share-chat-beta-patch-design.md`,
`2026-08-23-share-chat-streaming-design.md`.

⚠️ **`lib/shareChatApi.ts` is a separate client from `lib/api.ts` and must stay
one**: it sends no bearer token and instead passes `credentials: 'include'` so the
backend's signed `share_auth` cookie round-trips. `lib/api.ts`'s `request` never
needs cookies at all.

⚠️ **Both share `API_BASE`/`WS_BASE`, which default to `localhost`, NOT
`127.0.0.1`** — the visitor cookie is `SameSite=Lax` and a browser treats those as
different sites, so a mismatch with Vite's own `localhost:5173` **silently breaks
continuous chat entirely**. Configurable via `VITE_API_BASE`/`VITE_WS_BASE`.

`lib/shareTraceEvents.ts`'s `friendlyStatusFor` maps the event stream to one short
non-technical line, returning an i18n key under `share.status.*` (a literal union,
because `t()` is typed against locale keys). **Cosmetic only** — the backend
already strips everything but the event `type` (plus the final answer), so devtools
show nothing more than the UI does.

⚠️ The same module holds **three backend-persisted English literals** —
`FALLBACK_REPLY`, `DISPATCH_FAILED_REPLY`, `STOPPED_REPLY` — matched by **string
equality** so the page can render them in the visitor's language. A deliberate,
brittle coupling (see `docs/STATUS.md`). `STOPPED_REPLY` exists because the live
view used to render the generic failure line for `run_cancelled` while the backend
stored the "stopped" one, so a reload contradicted what the visitor had just seen.

The page is bilingual via the `share.*` namespace with its own `LanguageSelect` in
a header bar (same `bestteam_lang` key, so a visitor's choice sticks — extracted
because this route is outside `<Layout/>`). The composer is a `<textarea>`: Enter
sends, Shift+Enter is a newline, and **Enter during IME composition
(`isComposing` / keyCode 229) is ignored so a Chinese visitor never sends half a
sentence**. Colours come from tokens and the page is `100dvh` so a phone's
collapsing address bar can't hide the composer. Transient notices are stored as
i18n keys (**never the backend's detail**).

**Streaming**: `reply_delta` events append to a `streamedReply` string rendered
with a caret, replacing the status line; `reply_reset` clears it. ⚠️ **It is only
ever a preview** — `run_completed` discards it and appends the authoritative text,
so nothing partial is ever kept. **Deltas deliberately never join `liveEvents`**,
so the progress indicator keeps counting agents rather than tokens.

`components/ShareProgress.tsx` renders "Step n of N" from the count of
`agent_completed` events against the `steps` the team endpoint supplies, clamped,
**or an anonymous pulse when `steps` is null** — a position, never a name, a role
or a model. The header shows the team's name, falling back to the brand if that
fetch fails: **a failure there costs the header and the count, never the chat.**
While a turn is in flight Send becomes **Stop**, gated on the run id having
arrived (same rule as `MonitorPage`).

⚠️ `components/MarkdownText.tsx` uses `react-markdown` + `remark-gfm` and
**deliberately no `rehype-raw`** — model output is not trusted markup, so raw HTML
stays inert text. **That is the reason this is a library rather than
hand-rolled.** Shared with `SharedSessionsPanel`'s transcript so an admin sees
exactly what the visitor saw. **A visitor's own message stays plain text**: their
typing is not markup.

Org side, both on **My teams**' team cards only (gated on
`session.pipeline_id != null` — a YAML-only demo has no `PipelineRecord.id` to
hang a link off): `ShareLinksPanel` (generate/copy/revoke; daily cap and optional
expiry are set at creation — to change them, revoke and regenerate) and
`SharedSessionsPanel` (read-only audit). Both are **collapse-by-default behind
their own toggles**, with `SessionsPage` owning the audit one's open state, so a
page listing many teams doesn't fire a fetch per card on load.

⚠️ **A team with `uses_email` gets neither control** — one line of copy
(`shareLinks.notShareable`) sits where they were. The backend refuses to mint a
link for such a team, so the buttons could only fail.

## Auth and login UI

`lib/api.ts` stores a bearer token in `localStorage` (`bestteam_token`), attaches
`Authorization: Bearer`, and on a `401` **except from `/api/auth/*`** (to avoid
masking login errors) clears the token and redirects to `/login`.

**The login page is the one screen outside `Layout`**, which is why it renders its
own `BrandMark` and `LanguageSelect` — before the `login.*` namespace existed it
was hardcoded English with no language control at all, so a Chinese customer's
very first impression was untranslatable however bilingual the rest of the app
was. Two-panel layout collapsing to one under 820px, where the bullets are
**hidden rather than stacked** — they are decoration and would push the form below
the fold on a phone.

⚠️ **The slogan is `nav.tagline`, one key for the whole app** — the login page and
`WizardLayout`'s `<h1>` both read it. It used to be three near-copies that had
already drifted while the Chinese side had one string all along. `README.md`'s
copy is the fourth and, being outside the bundle, **is the one to keep in step by
hand.**

⚠️ **`#username`, `#password`, `button[type=submit]` and `.banner-error` are a
contract with `tests/e2e/test_smoke.py`**, which drives the real page through
exactly those selectors. `LoginPage.test.tsx` asserts each so a rename fails in the
unit tier instead of the e2e one.

⚠️ **Language, Change password and Log out live in `Layout`'s account menu**,
not the nav row — a closed menu renders none of them, so
`tests/e2e/test_smoke.py` clicks `button.account-trigger` before
`button.logout-button`. Escape and an outside `mousedown` both close it.

`components/ChangePasswordDialog.tsx` posts to `POST /api/auth/password` and
⚠️ **swaps the returned token into `localStorage` immediately** — the change
revokes the old one, so any request made before the swap would 401. **Its success
state stays on screen rather than closing**, because the customer needs telling
that every other session has just been signed out. Client-side it only blocks the
obvious; the backend is the authority.

### Role-aware routing

A platform operator (`is_admin`, `org_id IS NULL`) and an org member see disjoint
UIs, partitioned by two symmetric `App.tsx` guards that both read `lib/useMe.ts`
(one `GET /api/auth/me`) and **render `null` while it loads**:

- **`RequireAdmin`** wraps `/accounts` + `/advanced` + `/memory` + `/trace` +
  `/feedback`; non-admins go to `/`.
- **`RequireOrgMember`** wraps `/`, `/run`, `/teams`, `/activity`, `/wizard/*`;
  operators go to `/advanced`, since every org-scoped surface 403s an org-less
  operator.
- ⚠️ **The `*` catch-all stays OUTSIDE both guards** so an unknown path routes to
  `/`, where `RequireOrgMember` picks the destination.

Because `is_admin` and org membership are mutually exclusive, **the two guards
can't bounce a user between them — each redirect terminates in one hop.**
`Layout.tsx` mirrors this in which links it shows.

⚠️ **All of this gating is cosmetic** — the backend enforces admin on every
`/api/config` and `/api/memory` call and org scoping on every customer surface, so
a tampered client still gets 403.

**`/advanced`** (`AdvancedPage.tsx`) is raw-JSON CRUD over
`/api/config/{pipelines|skills|knowledge_bases|model-catalog}` plus a read-only
`tools` tab. Tabs run whole-then-parts. Each `KINDS` entry carries an `orgScope`
mirroring the backend: `required` (`?org=` or 422), `optional` (skills — omitted
means the platform built-in tier), `none`. **A pipeline is what the wizard and
customer UI call an "AI team"**; this page uses the technical noun because it
matches the JSON keys the operator is editing. Skill rows show their current
immutable version; saving appends a version while already-deployed teams keep
their pinned one. ⚠️ The list's display-only filter derives `filteredItems` from
`visibleItems`, **which remains the org-scoping decision, so a hidden row can never
become a mutation target**.

**`/trace`** (`TracePage.tsx`) — `AdminRunDetail` offers **"Diagnose this run"** on
a finished, non-diagnostic run; the page then selects the new run, which streams
into the same panel under a banner naming the original (with a redeployed-since
notice when `version_changed`). `lib/traceEvents.ts` carries labels for the
diagnostic-only `agent_prompt`/`model_turn` events, and the detail view collapses
those payloads behind a `<details>` so one long prompt doesn't bury the timeline.
The run list badges diagnostic runs; **the customer's Activity page never receives
one** (filtered server-side).

**`/accounts`** creates orgs, deactivates/reactivates them, and creates /
resets-password / moves / deletes each org's member. **Platform accounts are shown
read-only** — the `/api/admin` surface keeps promote/demote and platform-account
lifecycle in the CLI.

**`/feedback`** (`FeedbackPage.tsx`) is the admin triage list — status/kind
filters, expandable rows, status + note saved via `api.patchFeedback`.
⚠️ **Bodies render as plain text only; visitor text is untrusted.** The submit
side is `components/FeedbackModal.tsx`, one bilingual `<dialog>` shared by two
entry points that each own their own POST: the `Layout` nav button (org members
only — **an admin's nav carries the triage NavLink under the same `nav.feedback`
label instead**) posting `api.submitFeedback` with `{page, locale}` context, and
the share-chat header (`ShareChatPage`) posting `shareChatApi.sendFeedback` with
the last dispatched `run_id` added.

`lib/dateFormat.ts::formatDateTime` is locale-aware (keyed on
`i18n.resolvedLanguage`) for every panel showing a date; `endOfLocalDay` lives
there too.
