# Share-link chat: beta patch (obvious defects only)

Date: 2026-08-22. Status: approved design (brainstorming + plan-mode review;
the user delegated the remaining decisions and asked for unattended
execution followed by a Codex review).

## Context

The anonymous share-link chat (`/share/:token`, spec
`docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md`)
works end to end, but the visitor page was never brought up to the standard
the rest of the customer UI reached in the F1–F15 audit: it is English-only
with no way to switch language, hardcodes two colours, breaks on a phone,
offers a single-line composer against a 4,000-character cap, and the org-side
panel that mints links cannot set the cap or expiry the API already supports.
Separately, an admin cannot diagnose a poor shared-chat answer even though
`docs/BETA_NOTES.md` promises diagnosis for everything but email runs.

Decisions taken in brainstorming (2026-08-22):

- **Positioning: practical — help a colleague get something done.** Low
  latency, readability, copy. No decorative chrome.
- **Two steps.** This spec is step 1: a beta patch that fixes *only obvious
  defects* — pure frontend plus two narrow backend changes. Step 2 (real
  token streaming for the final agent, anonymous "step n of N" progress
  dots, a visitor Stop button, the team name on the visitor page, markdown
  rendering of replies) is SDK-level work and gets its own spec; it is
  recorded here only as a roadmap note.
- **Diagnostic re-run of a shared-chat turn: allow it now.** The refusal's
  stated reason is false by the diagnostic spec's own construction (§3).

## Defects fixed, each against the project's own documents

| Defect | Evidence |
|---|---|
| `ShareChatPage.tsx` is hardcoded English; no `share.*` namespace in `locales/en.ts`; the route is outside `<Layout/>` so there is no language control | `docs/BETA_NOTES.md` "The interface": the whole customer-facing app is translated |
| `ShareLinksPanel.tsx` / `SharedSessionsPanel.tsx` (customer surfaces on My teams) hardcoded English | same |
| `ShareChatPage.css` hardcodes `#2563eb` / `white` for the user bubble | `ui/frontend/CLAUDE.md`: components read tokens, never declare colours |
| `.share-chat { height: 100vh }` | mobile browser chrome pushes the composer off-screen |
| `ShareLinksPanel` calls `createShareLink(pipelineId, {})`; `daily_cap` / `expires_at` have no UI and existing links don't show either | a link that cannot be given an expiry cannot be governed |
| Single-line `<input>` against a 4,000-character cap; no Shift+Enter | unreadable past one line |
| `_share_link_dict` serialises `expires_at`/`created_at` as **naive** ISO strings while `_share_session_dict` in the same file already uses `iso_utc` | `new Date('2026-09-01T00:00:00')` parses as *local* time in a browser, so an expiry displays hours off |
| `POST /api/runs/{id}/diagnose` returns 400 for a shared-chat turn | the new diagnostic Run row carries no `trigger_context`, so the share-reply path is a no-op — the refusal protects nothing; `BETA_NOTES.md` promises diagnosis for all but email runs |

## 1. Frontend

### 1.1 `share.*` locale namespace and a language control on the visitor page

- Add a `share` namespace to `locales/en.ts`; `zh-CN.ts` follows (its
  `Resources` typing turns a missing key into a `tsc` error). Keys:
  `share.placeholder`, `share.send`, `share.sendHint`, `share.unavailable`,
  `share.loadFailed`, `share.rateLimited`, `share.tooLong` (`{{max}}`),
  `share.pendingTurn`, `share.sendFailed`, `share.recovered`,
  `share.fallbackReply`, `share.dispatchFailedReply`, and
  `share.status.{sending,starting,working,checking,composing,default}`.
  The English values stay **verbatim** to today's strings ("Type a message…",
  "Sorry, something went wrong producing a reply.", "Couldn't start a reply
  just now. Please try sending your message again.") — `ShareChatPage.test.tsx`
  locates the composer and the fallbacks by those phrases.
- `lib/shareTraceEvents.ts`'s `friendlyStatusFor(events)` returns an **i18n
  key** (`'share.status.working'` …) instead of an English sentence; the page
  calls `t(key)`. `lib/shareTraceEvents.test.ts` asserts keys.
- **Persisted fallback replies.** `share_transcript._FALLBACK_REPLY` and
  `share_chat._DISPATCH_FAILED_MESSAGE` are written into `share_messages` in
  English. The backend is unchanged; the page translates them **at render
  time by string equality**: if `m.content` equals one of the two literals,
  render `t('share.fallbackReply')` / `t('share.dispatchFailedReply')`. That
  covers both the live path and a reload, so the screen never disagrees with
  itself. The two literals live in `lib/shareTraceEvents.ts` with a comment
  naming the backend sources. This is a deliberate, brittle coupling —
  recorded in `docs/STATUS.md` Known issues; the proper fix (a stable code on
  `ShareMessage`) is out of scope.
- **`LanguageSelect`.** Extract the `<select className="language-select">`
  block from `components/Layout.tsx` into `components/LanguageSelect.tsx`
  (same `SUPPORTED_LANGUAGES` / `setLanguage` / `i18n.resolvedLanguage`, same
  `aria-label={t('nav.language')}`) and move the `.language-select` rules from
  `Layout.css` into `LanguageSelect.css`. A pure move: same class, same
  aria-label, no behaviour change on the authenticated pages; no effect that
  mirrors the language into React state (`react-hooks/set-state-in-effect`
  is on). `ShareChatPage` renders it in a small header bar
  (`.share-chat-header`: `t('nav.brand')` left, `LanguageSelect` right). The
  same `bestteam_lang` localStorage key means a visitor's choice sticks.

### 1.2 Tokens, mobile height, textarea, copy

- `.share-chat-bubble.user { background: var(--accent); color: var(--accent-contrast); }`.
  `index.css` already gives `input/select/textarea` surface+text tokens.
  `white-space: pre-wrap` stays on the base `.share-chat-bubble` —
  `SharedSessionsPanel` reuses these classes for the audit transcript.
- `.share-chat { height: 100vh; height: 100dvh; }` (fallback first;
  `index.html` already has the viewport meta).
- The composer becomes `<textarea rows={2} maxLength={4000}>` (the
  `.share-chat-form input` selector follows). `onKeyDown`: Enter without
  Shift sends; Shift+Enter inserts a newline. **IME guard:** Enter is ignored
  while `e.nativeEvent.isComposing` is true (or `keyCode === 229`) — otherwise
  confirming a Chinese IME candidate with Enter would send a half-composed
  message. `handleSend` takes `{ preventDefault(): void }` so the form submit
  and the keydown path share it. A one-line `.hint` under the form shows
  `share.sendHint`.
- **Copy reply:** a `btn-link` under each real assistant bubble using
  `common.copy` / `common.copied` / `common.copyFailed`, with the clipboard
  guarded exactly as `ShareLinksPanel.handleCopy` does (rejects on a
  non-secure origin). The two fallback replies ("something went wrong…")
  deliberately get no Copy control — there is nothing useful to copy. The
  one nice-to-have kept, because copy was named as a priority.
- Transient notices (recovered / pending turn / too long / send failed /
  copy failed) are stored as i18n keys and translated at render, so a
  language switch re-renders them; a 409's backend detail is never echoed on
  this public page. `ShareLinksPanel`'s error banner does the same — its own
  messages are keys, a message the API returned stays as text because it
  cannot be translated. The header with the language control also renders on the
  "link unavailable" page. The composer has an accessible label and is
  described by the keyboard hint; status and notices live in polite live
  regions. (All four from the Codex review of the first cut.)
- `lib/dateFormat.ts::formatDateTime` is locale-aware (Chinese dates in
  Chinese — it was interpolating "02 JAN 2030" into translated sentences),
  and `endOfLocalDay` lives there too.

### 1.3 `ShareLinksPanel` — cap/expiry + i18n; `SharedSessionsPanel` — i18n

- Create form above the list: "Messages per day" `<input type="number"
  min=1 max=1000 step=1>` (default 30, matching `ShareLinkCreate`) and
  "Expires on (optional)" `<input type="date">`. A real `<form onSubmit>`, so
  the browser's constraint validation refuses 10.5 / 0 / 1001 before the
  handler; the handler's own integer check catches the one case validation
  lets through — an empty field, which is valid because it isn't `required`. `expires_at` is sent as the end of that
  *local* day via `toISOString()` (offset-aware `…Z`; the backend's
  `_to_naive_utc` normalises it and `share_chat._is_expired` compares naive
  UTC). No edit-in-place of an existing link — revoke and regenerate; `PATCH`
  stays revoke-only. Nothing in the form is labelled with the word "Share"
  (the collapsed toggle is found by `/share/i` in tests).
- Each link row shows status · `shareLinks.perDay` (`{{count}}`) · expiry
  (`shareLinks.expires` with `formatDateTime`, or `shareLinks.noExpiry`).
- Namespaces `shareLinks.*` and `sharedSessions.*` hold every string of both
  panels. No cost or model words anywhere on these customer surfaces.

## 2. Backend: `_share_link_dict` emits offset-aware timestamps

`ui/backend/share_links_api.py::_share_link_dict` uses `iso_utc()`
(`db/models.py`) for `expires_at` and `created_at`, as `_share_session_dict`
in the same file already does. `tests/test_share_links_api.py` asserts the
`+00:00`.

## 3. Backend: an admin may diagnose a shared-chat turn

`main.py::diagnose_run` currently refuses any run with a `trigger_context`.
New rule: refuse when `trigger_context` is present **and** has no
`share_session_id` — i.e. an autonomous email run, whose re-run really would
reach the org's mailbox. A share turn passes.

Why a share turn is safe, by construction (all existing code):

- The new diagnostic `Run` row is written with no `trigger_context`, so in
  `runtime.run_in_background` `_maybe_record_share_reply` →
  `share_transcript.record_share_reply` returns at its first guard (nothing is
  appended to `share_messages`), and `_maybe_normalize` is skipped.
- The visitor WebSocket (`share_chat.stream_share_run`) only serves a run
  whose `trigger_context.share_session_id` matches the cookie session — a
  diagnostic run has none, so no visitor can subscribe to it.
- Nothing resolves the original run's `trigger_context` through
  `diagnostic_of_run_id` (only the version comparison in `GET /api/runs`).
- `runs.input` for a share turn is the fully formatted transcript
  (`format_transcript`), so the re-run reproduces exactly the context the
  visitor's turn saw.
- `AdminRunDetail.tsx` already offers "Diagnose this run" for such a run; no
  frontend change.

Error message for the remaining refusal: "Autonomous email runs can't be
diagnosed: a re-run would reach the org's mailbox." Tests:
`test_diagnostic_rerun.py` keeps the email refusal (`"mailbox"` in detail)
and adds a share-turn case — a `Run` row inserted with
`trigger_context={"share_link_id":1,"share_session_id":1,"turn_number":1}`
→ 200, the new run completes, its row has `trigger_context is None` and
`diagnostic_of_run_id` set, and the `share_messages` count is unchanged.

Docs: `docs/ADMIN_MANUAL.md` §2.5 (drop "or a shared-chat turn"); the
2026-08-21 diagnostic spec gets a dated amendment line at its step 2;
`ui/backend/CLAUDE.md` and `docs/STATUS.md` follow. `docs/BETA_NOTES.md`
already says only email runs are excluded — now true.

## 4. Docs in the same change

- `ui/frontend/CLAUDE.md` "Anonymous team sharing": `share.*` namespace,
  `LanguageSelect`, `friendlyStatusFor` returns keys, render-time translation
  of the two persisted fallback literals, cap/expiry UI.
- `docs/STATUS.md`: Done entry; Known issue (fallback replies persisted in
  English, translated at render by string equality); Roadmap entry for step
  2 with the decisions taken today: real token streaming for the **last**
  agent only (deltas to the in-memory registry, never `trace_events`; usage
  metering via `stream_usage`; cancel checkpoints), anonymous "step n of N"
  progress dots only alongside streaming (SEQUENTIAL shows a denominator,
  PARALLEL shows n lit at once, HIERARCHICAL has none and falls back to a
  pulse), a visitor Stop button (new public cancel endpoint), the team name
  on the visitor page (new public endpoint + a disclosure decision), markdown
  rendering shared with the audit transcript.

## 5. Testing

- Frontend (vitest/jsdom, `src/test/setup.ts` initialises i18n in English):
  `ShareChatPage.test.tsx` — existing assertions unchanged; new: switching
  to `zh-CN` re-renders the placeholder (restore `setLanguage('en')` in
  `afterEach`, wrap in `waitFor`), Enter sends / Shift+Enter does not / Enter
  during composition does not, the persisted fallback literal renders
  translated under `zh-CN`, copy calls `navigator.clipboard.writeText`.
  `ShareLinksPanel.test.tsx` — `createShareLink` called with an exact
  `{ daily_cap, expires_at }`, rows show cap and expiry.
  `SharedSessionsPanel.test.tsx` — unchanged behaviour. `LanguageSelect` —
  one render test. `shareTraceEvents.test.ts` — keys.
- Backend (pytest, integration): `test_share_links_api.py` offset assertion;
  `test_diagnostic_rerun.py` split as above.
- Gates before pushing: `npm run lint`, `npm run build`, `npm test`,
  `python -m pytest -m "not e2e"` (serial), and the CI smoke command
  `python -m pytest tests/e2e/ -m "e2e and not slow"` (ports 8000/5173 free,
  never `-n auto`). No E2E test touches the share page, the Share button or
  the Shared-sessions panel, and the default language stays English.

## Out of scope (deliberately)

Token streaming; progress dots; a visitor Stop button; the team name on the
visitor page; markdown rendering (struck after review: a new runtime
dependency on the single bundle every page loads, and the audit transcript
would diverge); translating the two persisted fallback strings at the source;
editing an existing link's cap/expiry in place.
