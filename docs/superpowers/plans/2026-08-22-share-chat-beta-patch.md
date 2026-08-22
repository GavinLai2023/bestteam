# Share-link chat beta patch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the anonymous share-link chat and its org-side panels up to the customer-UI standard (bilingual, token colours, mobile-safe, multi-line composer, cap/expiry UI) and let an admin diagnose a shared-chat turn.

**Architecture:** Pure frontend work on `pages/ShareChatPage.tsx` and the two My-teams panels, a `LanguageSelect` component extracted from `Layout` so the public route (outside `<Layout/>`) gets the same switcher, plus two narrow backend changes: `_share_link_dict` emits offset-aware timestamps and `diagnose_run` only refuses autonomous *email* runs. No SDK change, no schema change, no new dependency.

**Tech Stack:** React 19 + Vite + TypeScript, react-i18next (`locales/en.ts` is the key source of truth, `zh-CN.ts: Resources` makes a missing key a `tsc` error), vitest/jsdom + Testing Library; FastAPI + SQLAlchemy, pytest (`integration` marker).

**Spec:** `docs/superpowers/specs/2026-08-22-share-chat-beta-patch-design.md`

## Global Constraints

- Branch `fix/share-chat-beta-patch` (off `main` @ 69adf90). The three pre-existing modified `docs/deployment.md`, `docs/email-smoke-test.md`, `docs/ui-testing-guide.md` in the working tree are **not ours** — never `git add` them; always add files by name.
- British spelling in copy and comments ("organisation", "colour"); code comments in English.
- No cost/model words on any customer surface (`RequireOrgMember`): the share page, `ShareLinksPanel`, `SharedSessionsPanel`.
- Components read CSS tokens (`var(--accent)` …); never declare a colour only inside a media block.
- Helpers that are not components live in `lib/`, never exported from a component file.
- The English `share.*` values must stay verbatim to today's strings — `ShareChatPage.test.tsx` matches `/type a message/i`, `/something went wrong producing a reply/i`, `/couldn't start a reply/i`.
- Python via `.\.venv\Scripts\python.exe`; frontend commands from `ui/frontend`. Frontend test runner: `npm test -- --run <file>` (vitest). Never `-n auto` on `tests/e2e/`.
- Every new pytest file needs a `pytestmark`; we only add tests to existing files here.
- Commit after each task; message style `type(scope): summary`, trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Extract `LanguageSelect` from `Layout`

**Files:**
- Create: `ui/frontend/src/components/LanguageSelect.tsx`
- Create: `ui/frontend/src/components/LanguageSelect.css`
- Create: `ui/frontend/src/components/LanguageSelect.test.tsx`
- Modify: `ui/frontend/src/components/Layout.tsx` (imports, lines 5 and 12; the `<select>` block at 71-82)
- Modify: `ui/frontend/src/components/Layout.css:76-92` (move `.language-select` rules out)

**Interfaces:**
- Produces: `export default function LanguageSelect(): JSX.Element` — renders `<select className="language-select" aria-label={t('nav.language')}>` with one `<option>` per `SUPPORTED_LANGUAGES` entry; changing it calls `setLanguage`. Task 3 renders it on the share page.

- [ ] **Step 1: Write the failing test**

```tsx
// ui/frontend/src/components/LanguageSelect.test.tsx
import { afterEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import LanguageSelect from './LanguageSelect'
import { setLanguage } from '../lib/i18n'

afterEach(() => {
  setLanguage('en')
})

describe('LanguageSelect', () => {
  it('offers every supported language, each labelled in its own language', () => {
    render(<LanguageSelect />)
    const select = screen.getByRole('combobox', { name: /language/i })
    expect(select).toHaveValue('en')
    expect(screen.getByRole('option', { name: 'English' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '中文' })).toBeInTheDocument()
  })

  it('switches the active language', () => {
    render(<LanguageSelect />)
    fireEvent.change(screen.getByRole('combobox', { name: /language/i }), { target: { value: 'zh-CN' } })
    expect(document.documentElement.lang).toBe('zh-CN')
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ui/frontend && npm test -- --run src/components/LanguageSelect.test.tsx`
Expected: FAIL — cannot resolve `./LanguageSelect`.

- [ ] **Step 3: Create the component and its stylesheet (a pure move)**

```tsx
// ui/frontend/src/components/LanguageSelect.tsx
import { useTranslation } from 'react-i18next'
import { SUPPORTED_LANGUAGES, setLanguage, type LanguageCode } from '../lib/i18n'
import './LanguageSelect.css'

// The language switcher, shared by the authenticated nav (Layout.tsx) and the
// public share page (pages/ShareChatPage.tsx), which sits outside <Layout/>
// and would otherwise have no way to leave English. Each option is labelled
// in its own language and never translated (see lib/i18n.ts).
export default function LanguageSelect() {
  const { t, i18n } = useTranslation()
  return (
    <select
      className="language-select"
      aria-label={t('nav.language')}
      value={i18n.resolvedLanguage}
      onChange={(e) => setLanguage(e.target.value as LanguageCode)}
    >
      {SUPPORTED_LANGUAGES.map((lang) => (
        <option key={lang.code} value={lang.code}>
          {lang.label}
        </option>
      ))}
    </select>
  )
}
```

```css
/* ui/frontend/src/components/LanguageSelect.css */
/* Pill-shaped so it matches the nav links it sits beside (Layout.css) rather
   than looking like a form control that wandered in; the share page reuses
   the same look in its header. */
.language-select {
  font: inherit;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-soft);
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  cursor: pointer;
}

.language-select:hover {
  background: var(--hover);
}
```

Then in `Layout.css` delete the comment and the two `.language-select` rules (lines 76-92). In `Layout.tsx`: replace line 5 with `import LanguageSelect from './LanguageSelect'`, replace the whole `<select …>…</select>` block (lines 71-82) with `<LanguageSelect />`, and change `const { t, i18n } = useTranslation()` to `const { t } = useTranslation()` **only if** `grep -n "i18n\." src/components/Layout.tsx` shows no other use (otherwise leave it).

- [ ] **Step 4: Run the test and the Layout tests**

Run: `npm test -- --run src/components/LanguageSelect.test.tsx src/components/Layout.test.tsx` (if `Layout.test.tsx` exists; otherwise just the first) then `npm run lint`
Expected: PASS, lint clean (no unused imports).

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/components/LanguageSelect.tsx ui/frontend/src/components/LanguageSelect.css ui/frontend/src/components/LanguageSelect.test.tsx ui/frontend/src/components/Layout.tsx ui/frontend/src/components/Layout.css
git commit -m "refactor(ui): extract LanguageSelect so a page outside Layout can switch language"
```

---

### Task 2: `share.*` keys; `friendlyStatusFor` returns keys; fallback-reply literals

**Files:**
- Modify: `ui/frontend/src/locales/en.ts` (add `share`, `shareLinks`, `sharedSessions` namespaces after `myTeams`, before `modelCatalog`)
- Modify: `ui/frontend/src/locales/zh-CN.ts` (same position)
- Modify: `ui/frontend/src/lib/shareTraceEvents.ts`
- Modify: `ui/frontend/src/lib/shareTraceEvents.test.ts`

**Interfaces:**
- Produces: `friendlyStatusFor(events: TraceEvent[]): string` now returns an **i18n key** such as `'share.status.working'`; `FALLBACK_REPLY` and `DISPATCH_FAILED_REPLY` string constants; `fallbackReplyKey(content: string): string | null` returning `'share.fallbackReply'` / `'share.dispatchFailedReply'` / `null`. All three namespaces' keys exactly as written below — Tasks 3, 6, 7 use them.

- [ ] **Step 1: Rewrite the helper test to assert keys**

```ts
// ui/frontend/src/lib/shareTraceEvents.test.ts
import { describe, it, expect } from 'vitest'
import { DISPATCH_FAILED_REPLY, FALLBACK_REPLY, fallbackReplyKey, friendlyStatusFor } from './shareTraceEvents'
import type { TraceEvent } from './types'

describe('friendlyStatusFor', () => {
  it('returns the sending key with no events yet', () => {
    expect(friendlyStatusFor([])).toBe('share.status.sending')
  })

  it('maps the most recent known event type to a status key', () => {
    const events: TraceEvent[] = [
      { type: 'run_started', agent: undefined, data: null },
      { type: 'tool_started', agent: 'a', data: { tool: 'web_search' } },
    ]
    expect(friendlyStatusFor(events)).toBe('share.status.working')
  })

  it('never leaks a raw tool or agent name', () => {
    const events: TraceEvent[] = [{ type: 'tool_started', agent: 'a', data: { tool: 'email_find' } }]
    expect(friendlyStatusFor(events)).not.toMatch(/email_find/)
  })

  it('falls back to the default key for an unmapped event type', () => {
    const events: TraceEvent[] = [{ type: 'some_future_event', agent: undefined, data: null }]
    expect(friendlyStatusFor(events)).toBe('share.status.default')
  })
})

describe('fallbackReplyKey', () => {
  it('recognises the two replies the backend persists in English', () => {
    expect(fallbackReplyKey(FALLBACK_REPLY)).toBe('share.fallbackReply')
    expect(fallbackReplyKey(DISPATCH_FAILED_REPLY)).toBe('share.dispatchFailedReply')
  })

  it('leaves a real reply alone', () => {
    expect(fallbackReplyKey('Here is your answer.')).toBeNull()
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm test -- --run src/lib/shareTraceEvents.test.ts`
Expected: FAIL — `fallbackReplyKey` not exported; status strings, not keys.

- [ ] **Step 3: Add the locale keys**

In `en.ts`, insert after the `myTeams: { … },` block:

```ts
  // The public share-link chat (pages/ShareChatPage.tsx). The English values
  // are verbatim what the page showed before it was translated -- tests find
  // the composer and the fallback replies by these phrases.
  share: {
    placeholder: 'Type a message…',
    send: 'Send',
    sendHint: 'Enter to send · Shift+Enter for a new line',
    unavailable: 'This share link is no longer available.',
    loadFailed: "Couldn't load this conversation.",
    rateLimited: "Today's message limit has been reached — try again tomorrow.",
    tooLong: 'That message is too long. Please keep it under {{max}} characters.',
    pendingTurn: 'Please wait for the previous reply to finish.',
    sendFailed: 'Something went wrong sending your message. Please try again.',
    recovered: 'Something went wrong. Please try sending your message again.',
    // The backend persists these two replies in English (share_transcript.py
    // `_FALLBACK_REPLY`, share_chat.py `_DISPATCH_FAILED_MESSAGE`); the page
    // recognises them by value and renders these instead (lib/shareTraceEvents.ts).
    fallbackReply: 'Sorry, something went wrong producing a reply.',
    dispatchFailedReply: "Couldn't start a reply just now. Please try sending your message again.",
    // Deliberately generic -- never a tool or agent name (lib/shareTraceEvents.ts).
    status: {
      sending: 'Sending your message…',
      starting: 'Getting started…',
      working: 'Working on your question…',
      checking: 'Checking with the team…',
      composing: 'Putting together a reply…',
      default: 'Working on it…',
    },
  },
  // The org-side "Share" panel on each deployed team card (components/ShareLinksPanel.tsx).
  shareLinks: {
    toggle: 'Share',
    generate: 'Generate a new link',
    messagesPerDay: 'Messages per day',
    expiresOn: 'Expires on (optional)',
    active: 'Active',
    revoked: 'Revoked',
    perDay: '{{n}} messages per day',
    expires: 'Expires {{when}}',
    noExpiry: 'No expiry',
    copyLink: 'Copy link',
    copied: 'Copied!',
    revoke: 'Revoke',
    close: 'Close',
    copyFailed: "Couldn't copy the link automatically. Select and copy it by hand.",
  },
  // The read-only audit view beside it (components/SharedSessionsPanel.tsx).
  sharedSessions: {
    back: 'Back',
    none: 'No share links for this team yet.',
    activeLink: 'Active link',
    revokedLink: 'Revoked link',
    lastActive: 'Last active {{when}}',
    turnsToday: '{{n}} turns today',
    viewTranscript: 'View transcript',
  },
```

In `zh-CN.ts`, insert after its `myTeams: { … },` block:

```ts
  share: {
    placeholder: '输入消息…',
    send: '发送',
    sendHint: 'Enter 发送 · Shift+Enter 换行',
    unavailable: '这个分享链接已失效。',
    loadFailed: '无法加载这段对话。',
    rateLimited: '今天的消息数已达上限，请明天再试。',
    tooLong: '消息太长了，请控制在 {{max}} 个字符以内。',
    pendingTurn: '请等上一条回复完成。',
    sendFailed: '发送消息时出了点问题，请重试。',
    recovered: '出了点问题，请重新发送你的消息。',
    fallbackReply: '抱歉，生成回复时出了点问题。',
    dispatchFailedReply: '刚才没能开始回复，请重新发送你的消息。',
    status: {
      sending: '正在发送…',
      starting: '正在开始…',
      working: '正在处理你的问题…',
      checking: '正在和团队确认…',
      composing: '正在整理回复…',
      default: '处理中…',
    },
  },
  shareLinks: {
    toggle: '分享',
    generate: '生成新链接',
    messagesPerDay: '每日消息数',
    expiresOn: '过期日期（可选）',
    active: '有效',
    revoked: '已撤销',
    perDay: '每日 {{n}} 条消息',
    expires: '{{when}} 过期',
    noExpiry: '永不过期',
    copyLink: '复制链接',
    copied: '已复制！',
    revoke: '撤销',
    close: '关闭',
    copyFailed: '无法自动复制链接，请手动选中并复制。',
  },
  sharedSessions: {
    back: '返回',
    none: '这个团队还没有分享链接。',
    activeLink: '有效链接',
    revokedLink: '已撤销的链接',
    lastActive: '最近活跃 {{when}}',
    turnsToday: '今日 {{n}} 轮',
    viewTranscript: '查看对话记录',
  },
```

(`{{n}}` rather than `{{count}}` on purpose: i18next treats `count` as a plural selector and would look for `_one`/`_other` keys.)

- [ ] **Step 4: Rewrite `lib/shareTraceEvents.ts`**

```ts
// ui/frontend/src/lib/shareTraceEvents.ts
import type { TraceEvent } from './types'

// A visitor chat page shows a short, non-technical progress line instead of
// the raw trace `lib/traceEvents.ts` renders for the logged-in Activity
// page -- deliberately generic (never a raw tool/agent name), since a
// colleague using a shared link shouldn't see the team's internal wiring.
// Values are i18n keys under `share.status` (locales/en.ts); the page
// translates them, so this module stays free of react-i18next.
const FRIENDLY_STATUS: Record<string, string> = {
  run_queued: 'share.status.sending',
  run_started: 'share.status.starting',
  agent_started: 'share.status.working',
  agent_progress: 'share.status.working',
  tool_started: 'share.status.working',
  tool_completed: 'share.status.working',
  delegation_started: 'share.status.checking',
  subagent_started: 'share.status.checking',
  subagent_completed: 'share.status.checking',
  delegation_completed: 'share.status.composing',
  agent_completed: 'share.status.composing',
}

const DEFAULT_STATUS = 'share.status.default'
const INITIAL_STATUS = 'share.status.sending'

export function friendlyStatusFor(events: TraceEvent[]): string {
  if (events.length === 0) return INITIAL_STATUS
  const last = events[events.length - 1]
  return FRIENDLY_STATUS[last.type] ?? DEFAULT_STATUS
}

// The backend persists these two assistant replies in English:
// share_transcript.py `_FALLBACK_REPLY` (a failed/cancelled/crashed run) and
// share_chat.py `_DISPATCH_FAILED_MESSAGE` (the executor refused the run).
// They come back verbatim in GET .../messages, so the page recognises them
// by value and renders the visitor's language instead. A deliberate,
// brittle string-equality coupling -- change either literal in lockstep
// with the backend (docs/STATUS.md, Known issues).
export const FALLBACK_REPLY = 'Sorry, something went wrong producing a reply.'
export const DISPATCH_FAILED_REPLY = "Couldn't start a reply just now. Please try sending your message again."

const FALLBACK_REPLY_KEYS: Record<string, string> = {
  [FALLBACK_REPLY]: 'share.fallbackReply',
  [DISPATCH_FAILED_REPLY]: 'share.dispatchFailedReply',
}

export function fallbackReplyKey(content: string): string | null {
  return FALLBACK_REPLY_KEYS[content] ?? null
}
```

- [ ] **Step 5: Run the helper test and the type check**

Run: `npm test -- --run src/lib/shareTraceEvents.test.ts && npm run build`
Expected: helper test PASS; build PASS (`zh-CN.ts` has every new key). `ShareChatPage.test.tsx` now shows raw keys — that's Task 3's job; don't run it yet.

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/locales/en.ts ui/frontend/src/locales/zh-CN.ts ui/frontend/src/lib/shareTraceEvents.ts ui/frontend/src/lib/shareTraceEvents.test.ts
git commit -m "i18n(share): add the share-link namespaces; status helper returns keys"
```

---

### Task 3: Translate `ShareChatPage`, add the header with `LanguageSelect`

**Files:**
- Modify: `ui/frontend/src/pages/ShareChatPage.tsx` (whole file)
- Modify: `ui/frontend/src/pages/ShareChatPage.css` (append header rules)
- Modify: `ui/frontend/src/pages/ShareChatPage.test.tsx` (add tests; import `setLanguage`; `afterEach` reset)

**Interfaces:**
- Consumes: `LanguageSelect` (Task 1); `friendlyStatusFor`, `fallbackReplyKey`, `FALLBACK_REPLY` (Task 2); `share.*`, `nav.brand`, `common.*` keys.
- Produces: the page as rewritten below; Task 4 edits the composer and adds copy on top of it.

- [ ] **Step 1: Add the failing tests**

At the top of `ShareChatPage.test.tsx` add `import { setLanguage } from '../lib/i18n'` and, inside `describe`, after the existing `afterEach`, add:

```tsx
  afterEach(() => {
    setLanguage('en')
  })
```

Append these tests inside the `describe`:

```tsx
  it('switches the page to Chinese from its own language control', async () => {
    renderPage()
    await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(screen.getByRole('combobox', { name: /language/i }), { target: { value: 'zh-CN' } })
    await waitFor(() => expect(screen.getByPlaceholderText('输入消息…')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: '发送' })).toBeInTheDocument()
  })

  it("renders the backend-persisted fallback reply in the visitor's language", async () => {
    // The backend stores its fallback reply in English; a Chinese visitor
    // must not see an English sentence in the middle of their conversation.
    mockedApi.getMessages.mockResolvedValue({
      messages: [
        { role: 'user', content: 'hi', turn_number: 1 },
        { role: 'assistant', content: 'Sorry, something went wrong producing a reply.', turn_number: 2 },
      ],
    })
    setLanguage('zh-CN')
    renderPage()
    expect(await screen.findByText('抱歉，生成回复时出了点问题。')).toBeInTheDocument()
  })

  it('shows the live status line in the visitor language', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    setLanguage('zh-CN')
    renderPage()
    const input = await screen.findByPlaceholderText('输入消息…')
    fireEvent.change(input, { target: { value: '你好' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('正在发送…')).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- --run src/pages/ShareChatPage.test.tsx`
Expected: the three new tests FAIL (no combobox; English fallback; English status), and several old ones FAIL too because the page still renders English literals while the helper now returns keys.

- [ ] **Step 3: Rewrite `ShareChatPage.tsx`**

```tsx
// ui/frontend/src/pages/ShareChatPage.tsx
import { FormEvent, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import LanguageSelect from '../components/LanguageSelect'
import { shareChatApi } from '../lib/shareChatApi'
import { FALLBACK_REPLY, fallbackReplyKey, friendlyStatusFor } from '../lib/shareTraceEvents'
import type { ShareMessage, TraceEvent } from '../lib/types'
import './ShareChatPage.css'

const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled']

const MAX_MESSAGE_LENGTH = 4000 // matches share_chat.py's own cap

// The public, anonymous, multi-turn counterpart to MonitorPage's one-shot
// "Run a team" -- a colleague reaches this page via a link an org member
// generated (ShareLinksPanel), never logs in, and gets a real back-and-forth
// conversation. See docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md and, for this page's
// bilingual/mobile pass, 2026-08-22-share-chat-beta-patch-design.md.
export default function ShareChatPage() {
  const { t } = useTranslation()
  const { token = '' } = useParams<{ token: string }>()
  const [messages, setMessages] = useState<ShareMessage[]>([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [liveEvents, setLiveEvents] = useState<TraceEvent[]>([])
  // i18n keys, translated at render so a language switch re-renders them.
  const [unavailableKey, setUnavailableKey] = useState<string | null>(null)
  const [rateLimited, setRateLimited] = useState(false)
  // Already-translated text: set at the moment of failure, transient, and a
  // 409's detail comes from the backend as a sentence rather than a key.
  const [notice, setNotice] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  // Set inside onmessage's own terminal-event branches; onclose checks it to
  // tell "the stream ended normally, after a terminal event" apart from the
  // backend's real close-without-terminal-event paths (share_chat.py: an
  // evicted subscriber queue, or the link/org going inactive mid-stream) --
  // without this a visitor is left staring at a "Working on it..." line
  // forever with no way to recover short of reloading (review finding).
  const terminalSeenRef = useRef(false)
  // Set at the start of handleSend, before this effect's fetch can possibly
  // resolve. The initial history fetch is a snapshot taken at mount time --
  // if the visitor sends a message while it's still in flight, its (now
  // stale) resolution must not overwrite the optimistic message/live reply
  // handleSend has already put in state (Codex review finding).
  const hasSentRef = useRef(false)

  useEffect(() => {
    let ignore = false
    shareChatApi
      .getMessages(token)
      .then((data) => {
        if (ignore || hasSentRef.current) return
        setMessages(data.messages)
      })
      .catch((e: Error & { status?: number }) => {
        if (ignore || hasSentRef.current) return
        setUnavailableKey(e.status === 404 ? 'share.unavailable' : 'share.loadFailed')
      })
    return () => {
      ignore = true
    }
  }, [token])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, liveEvents])

  useEffect(() => {
    return () => {
      wsRef.current?.close()
    }
  }, [])

  const handleSend = async (event: FormEvent) => {
    event.preventDefault()
    const content = draft.trim()
    if (!content || sending) return

    hasSentRef.current = true
    setSending(true)
    setRateLimited(false)
    setNotice(null)
    setDraft('')
    // Held by reference so any send failure below can take it back off screen
    // -- the server never persisted it, so leaving it there showed the visitor
    // a message that silently vanishes on the next reload.
    const optimisticUserMessage: ShareMessage = {
      role: 'user',
      content,
      turn_number: messages.length + 1,
    }
    setMessages((prev) => [...prev, optimisticUserMessage])
    setLiveEvents([])
    terminalSeenRef.current = false

    try {
      const { run_id: runId } = await shareChatApi.sendMessage(token, content)
      const ws = new WebSocket(shareChatApi.streamUrl(token, runId))
      wsRef.current = ws
      ws.onmessage = (msg: MessageEvent<string>) => {
        const traceEvent = JSON.parse(msg.data) as TraceEvent
        setLiveEvents((prev) => [...prev, traceEvent])
        if (traceEvent.type === 'run_completed') {
          terminalSeenRef.current = true
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: String(traceEvent.data ?? ''), turn_number: prev.length + 1 },
          ])
          setSending(false)
        } else if (TERMINAL_TYPES.includes(traceEvent.type)) {
          // run_failed / run_cancelled: the backend has already persisted its
          // own friendly fallback reply for this turn, so show the same thing
          // now instead of leaving the visitor's message looking unanswered
          // until they happen to reload the page. Stored as the backend's
          // English literal (what a reload would return) and translated at
          // render by fallbackReplyKey, same as a reloaded one.
          terminalSeenRef.current = true
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: FALLBACK_REPLY, turn_number: prev.length + 1 },
          ])
          setSending(false)
        }
      }
      ws.onerror = () => setSending(false)
      // onclose always fires, including right after a clean terminal event
      // onmessage already handled -- only act when the socket closed
      // WITHOUT one (evicted queue, or the link/org going inactive
      // mid-stream both close with no terminal event at all). The run and
      // this turn still exist server-side either way, and a reply may
      // already have landed there while this socket was down -- refetch
      // instead of just re-enabling the input, or the visitor's real answer
      // (or the friendly fallback the backend already recorded) stays
      // invisible until they happen to reload the page (Codex review
      // finding).
      ws.onclose = () => {
        if (terminalSeenRef.current) return
        shareChatApi
          .getMessages(token)
          .then((data) => setMessages(data.messages))
          .catch(() => {})
          .finally(() => {
            setSending(false)
            setNotice(t('share.recovered'))
          })
      }
    } catch (e) {
      const status = (e as Error & { status?: number }).status
      setSending(false)
      if (status === 500) {
        // Unlike every other failure here, the backend persists BOTH the
        // user's message and a fallback assistant reply even when dispatch
        // itself fails (share_chat.py's executor.submit failure path) --
        // rolling the optimistic bubble back and letting the visitor retype
        // would duplicate a turn that's already recorded server-side.
        // Refetch instead, so what's on screen matches the server exactly
        // (Codex review finding).
        shareChatApi
          .getMessages(token)
          .then((data) => setMessages(data.messages))
          .catch(() => {})
        return
      }
      // Every other failure means nothing was persisted for this send, so
      // the optimistic bubble must come back off screen -- otherwise what's
      // rendered disagrees with server state until a reload silently drops
      // it.
      setMessages((prev) => prev.filter((m) => m !== optimisticUserMessage))
      if (status === 429) {
        setRateLimited(true)
      } else if (status === 404) {
        setUnavailableKey('share.unavailable')
      } else if (status === 409) {
        // The message was never persisted/no run was created for it -- the
        // backend's own detail text already says what to do.
        setNotice((e as Error).message || t('share.pendingTurn'))
      } else if (status === 422) {
        // The backend's length cap (Pydantic validation) -- its own detail is
        // a validation-error structure, not a sentence a visitor can read.
        setNotice(t('share.tooLong', { max: MAX_MESSAGE_LENGTH }))
      } else {
        setNotice(t('share.sendFailed'))
      }
      setDraft(content)
    }
  }

  if (unavailableKey) {
    return (
      <div className="share-chat">
        <p className="share-chat-unavailable">{t(unavailableKey)}</p>
      </div>
    )
  }

  return (
    <div className="share-chat">
      <header className="share-chat-header">
        <span className="share-chat-brand">{t('nav.brand')}</span>
        <LanguageSelect />
      </header>
      <div className="share-chat-messages">
        {messages.map((m, i) => {
          const key = m.role === 'assistant' ? fallbackReplyKey(m.content) : null
          return (
            <div key={i} className={`share-chat-bubble ${m.role}`}>
              {key ? t(key) : m.content}
            </div>
          )
        })}
        {sending && <div className="share-chat-bubble status">{t(friendlyStatusFor(liveEvents))}</div>}
        <div ref={messagesEndRef} />
      </div>
      {rateLimited && <p className="share-chat-bubble status">{t('share.rateLimited')}</p>}
      {notice && <p className="share-chat-bubble status">{notice}</p>}
      <form className="share-chat-form" onSubmit={handleSend}>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder={t('share.placeholder')}
          disabled={sending || rateLimited}
        />
        <button type="submit" disabled={sending || rateLimited || !draft.trim()}>
          {t('share.send')}
        </button>
      </form>
    </div>
  )
}
```

Append to `ShareChatPage.css`:

```css
.share-chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 0.75rem;
}

.share-chat-brand {
  font-weight: 700;
  color: var(--text);
}
```

- [ ] **Step 4: Run the page tests**

Run: `npm test -- --run src/pages/ShareChatPage.test.tsx`
Expected: all PASS (old and new).

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/pages/ShareChatPage.tsx ui/frontend/src/pages/ShareChatPage.css ui/frontend/src/pages/ShareChatPage.test.tsx
git commit -m "i18n(share): translate the visitor chat page and give it a language control"
```

---

### Task 4: Tokens, `100dvh`, textarea with IME guard, copy reply

**Files:**
- Modify: `ui/frontend/src/pages/ShareChatPage.tsx` (composer, `handleSend` signature, copy button)
- Modify: `ui/frontend/src/pages/ShareChatPage.css` (lines 7, 27-31, 53-58; new rules)
- Modify: `ui/frontend/src/pages/ShareChatPage.test.tsx` (add tests)

**Interfaces:**
- Consumes: the page from Task 3.
- Produces: nothing downstream.

- [ ] **Step 1: Add the failing tests**

Append inside the `describe`:

```tsx
  it('sends on Enter and keeps Shift+Enter for a new line', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()
    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'line one' } })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(mockedApi.sendMessage).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalledWith('tok', 'line one'))
  })

  it('does not send on the Enter that confirms an IME candidate', async () => {
    // A Chinese/Japanese IME uses Enter to commit the composed text; that
    // keydown must not fire a send, or the visitor sends half a sentence.
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()
    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: '你好' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
    expect(mockedApi.sendMessage).not.toHaveBeenCalled()
  })

  it('offers to copy an assistant reply, but not a user message', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    mockedApi.getMessages.mockResolvedValue({
      messages: [
        { role: 'user', content: 'hi', turn_number: 1 },
        { role: 'assistant', content: 'hello!', turn_number: 2 },
      ],
    })
    renderPage()
    await screen.findByText('hello!')
    const copyButtons = screen.getAllByRole('button', { name: /^copy$/i })
    expect(copyButtons).toHaveLength(1)
    fireEvent.click(copyButtons[0])
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('hello!'))
    expect(await screen.findByRole('button', { name: /copied/i })).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- --run src/pages/ShareChatPage.test.tsx`
Expected: the three new tests FAIL (Enter does nothing on an `<input>` inside a form in jsdom — no submit; no Copy button).

- [ ] **Step 3: Change the composer and add copy**

In `ShareChatPage.tsx`:

1. Change the import to `import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'` and `handleSend`'s signature to `const handleSend = async (event: { preventDefault(): void }) => {` (drop `FormEvent` from the import if it is now unused).
2. Add state `const [copiedIndex, setCopiedIndex] = useState<number | null>(null)` and, after `handleSend`:

```tsx
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter' || e.shiftKey) return
    // An IME (Chinese, Japanese…) uses Enter to commit the candidate text;
    // that keydown arrives with isComposing set (keyCode 229 in older
    // browsers) and must not send a half-composed message.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return
    e.preventDefault()
    void handleSend(e)
  }

  const handleCopy = async (index: number, content: string) => {
    // `navigator.clipboard` rejects (or is undefined) in a non-secure
    // context -- any HTTP origin that isn't localhost -- so this can't be
    // left unguarded (same as ShareLinksPanel).
    try {
      await navigator.clipboard.writeText(content)
      setCopiedIndex(index)
      setTimeout(() => setCopiedIndex(null), 2000)
    } catch {
      setNotice(t('common.copyFailed'))
    }
  }
```

3. Replace the message rendering with a wrapper for assistant turns so the copy control sits under the bubble, not inside it (the bubble keeps its text alone, which is what tests match on):

```tsx
        {messages.map((m, i) => {
          if (m.role === 'user') {
            return (
              <div key={i} className="share-chat-bubble user">
                {m.content}
              </div>
            )
          }
          const key = fallbackReplyKey(m.content)
          return (
            <div key={i} className="share-chat-assistant">
              <div className="share-chat-bubble assistant">{key ? t(key) : m.content}</div>
              {!key && (
                <button type="button" className="btn-link share-chat-copy" onClick={() => void handleCopy(i, m.content)}>
                  {copiedIndex === i ? t('common.copied') : t('common.copy')}
                </button>
              )}
            </div>
          )
        })}
```

4. Replace the `<input …/>` with:

```tsx
        <textarea
          rows={2}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder={t('share.placeholder')}
          disabled={sending || rateLimited}
        />
```

and add, directly after the closing `</form>`: `<p className="hint share-chat-hint">{t('share.sendHint')}</p>`.

In `ShareChatPage.css`:

- line 7: replace `height: 100vh;` with
  ```css
  /* dvh tracks the mobile browser's collapsing address bar; vh first as the
     fallback for browsers without it. */
  height: 100vh;
  height: 100dvh;
  ```
- lines 27-31 (`.share-chat-bubble.user`): `background: var(--accent); color: var(--accent-contrast);`
- line 53 selector `.share-chat-form input` → `.share-chat-form textarea`, and add `resize: none; font: inherit; line-height: 1.4;` to that rule.
- append:
  ```css
  .share-chat-assistant {
    align-self: flex-start;
    max-width: 80%;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.15rem;
  }

  .share-chat-assistant .share-chat-bubble {
    max-width: none;
  }

  .share-chat-copy {
    font-size: 0.8rem;
    padding: 0 0.25rem;
  }

  .share-chat-hint {
    margin: 0.25rem 0 0;
    font-size: 0.75rem;
  }
  ```

- [ ] **Step 4: Run the page tests, lint and build**

Run: `npm test -- --run src/pages/ShareChatPage.test.tsx && npm run lint && npm run build`
Expected: all PASS. (Existing tests still find the composer by placeholder — a `<textarea>` has one too — and `maxlength` is still `4000`.)

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/pages/ShareChatPage.tsx ui/frontend/src/pages/ShareChatPage.css ui/frontend/src/pages/ShareChatPage.test.tsx
git commit -m "fix(share): token colours, dvh height, multi-line composer with IME guard, copy reply"
```

---

### Task 5: `_share_link_dict` emits offset-aware timestamps

**Files:**
- Modify: `ui/backend/share_links_api.py:62-72`
- Test: `tests/test_share_links_api.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_share_links_api.py`:

```python
def test_share_link_timestamps_carry_a_utc_offset(client):
    """SQLite hands `expires_at`/`created_at` back tzinfo-naive; plain
    `.isoformat()` then omits the offset and `ShareLinksPanel` would show
    the expiry in browser-local time. `_share_session_dict` already uses
    `iso_utc` for exactly this reason; the link dict must too."""
    pipeline_id = _deploy_team()
    created = client.post(
        f"/api/pipelines/{pipeline_id}/share-links", json={"expires_at": "2030-01-02T23:59:59Z"}
    )
    assert created.status_code == 201, created.text

    # A fresh request = a fresh session, so the row is read back from SQLite.
    listed = client.get(f"/api/pipelines/{pipeline_id}/share-links").json()
    link = next(item for item in listed if item["id"] == created.json()["id"])
    assert link["expires_at"].endswith("+00:00"), link["expires_at"]
    assert link["created_at"].endswith("+00:00"), link["created_at"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_links_api.py::test_share_link_timestamps_carry_a_utc_offset -q`
Expected: FAIL on `expires_at` (no offset).

- [ ] **Step 3: Use `iso_utc`**

`iso_utc` is already imported in `share_links_api.py` (used by `_share_session_dict`). Change `_share_link_dict` to:

```python
def _share_link_dict(link: ShareLink) -> dict:
    return {
        "id": link.id,
        "pipeline_id": link.pipeline_id,
        "token": link.token,
        "active": link.active,
        "daily_cap": link.daily_cap,
        # iso_utc, not .isoformat(): SQLite round-trips these naive and the
        # frontend's `new Date(...)` would read an offset-less string as
        # browser-local time (same fix `_share_session_dict` already has).
        "expires_at": iso_utc(link.expires_at) if link.expires_at else None,
        "created_at": iso_utc(link.created_at),
    }
```

- [ ] **Step 4: Run the share-link API tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_links_api.py -q`
Expected: all PASS (`iso_utc` is idempotent on an already-aware datetime, so the existing `+00:00` assertion at line 150 still holds).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/share_links_api.py tests/test_share_links_api.py
git commit -m "fix(share): serialise share-link timestamps with a UTC offset"
```

---

### Task 6: `ShareLinksPanel` — cap/expiry form, per-link display, i18n

**Files:**
- Modify: `ui/frontend/src/components/ShareLinksPanel.tsx` (whole file)
- Modify: `ui/frontend/src/components/ShareLinksPanel.test.tsx`

**Interfaces:**
- Consumes: `shareLinks.*` keys (Task 2); `api.createShareLink(pipelineId, { daily_cap?: number; expires_at?: string | null })` (`lib/api.ts:492`); `formatDateTime` (`lib/dateFormat.ts`).

- [ ] **Step 1: Update and add tests**

In `ShareLinksPanel.test.tsx`, change the `creates a new link on click` test to:

```tsx
  it('creates a new link with the chosen daily cap and expiry', async () => {
    mockedApi.createShareLink.mockResolvedValue({
      id: 2, pipeline_id: 5, token: 'newtoken', active: true, daily_cap: 10, expires_at: '2030-01-02T23:59:59+00:00', created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    fireEvent.change(await screen.findByLabelText(/messages per day/i), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText(/expires on/i), { target: { value: '2030-01-02' } })
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))
    await waitFor(() =>
      expect(mockedApi.createShareLink).toHaveBeenCalledWith(5, {
        daily_cap: 10,
        // End of the chosen day in the browser's own time zone, sent with an offset.
        expires_at: new Date(2030, 0, 2, 23, 59, 59).toISOString(),
      }),
    )
  })

  it('creates a link with the default cap and no expiry when the form is left alone', async () => {
    mockedApi.createShareLink.mockResolvedValue({
      id: 2, pipeline_id: 5, token: 'newtoken', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    fireEvent.click(await screen.findByRole('button', { name: /generate/i }))
    await waitFor(() => expect(mockedApi.createShareLink).toHaveBeenCalledWith(5, { daily_cap: 30 }))
  })

  it("shows each link's daily cap and expiry", async () => {
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, pipeline_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
      { id: 2, pipeline_id: 5, token: 'def456token', active: true, daily_cap: 5, expires_at: '2030-01-02T23:59:59+00:00', created_at: '2026-08-14T00:00:00+00:00' },
    ])
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    expect(await screen.findByText('30 messages per day')).toBeInTheDocument()
    expect(screen.getByText('No expiry')).toBeInTheDocument()
    expect(screen.getByText('5 messages per day')).toBeInTheDocument()
    expect(screen.getByText(/^Expires .*2030/)).toBeInTheDocument()
  })
```

Also change the first test's `getByText(/active/i)` to `getAllByText(/^active$/i)` is **not** needed — keep it; only one element says exactly "Active".

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- --run src/components/ShareLinksPanel.test.tsx`
Expected: the three new/changed tests FAIL (no labelled inputs; payload `{}`; no cap text).

- [ ] **Step 3: Rewrite the panel**

```tsx
// ui/frontend/src/components/ShareLinksPanel.tsx
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { ShareLink } from '../lib/types'

interface ShareLinksPanelProps {
  pipelineId: number
}

const DEFAULT_DAILY_CAP = 30 // mirrors share_links_api.ShareLinkCreate's default

function shareUrlFor(token: string): string {
  return `${window.location.origin}/share/${token}`
}

// "2030-01-02" from <input type="date"> -> the last second of that day in the
// browser's own time zone. Sent via toISOString() (an offset-aware instant),
// which the backend normalises to naive UTC for `share_chat._is_expired`.
function endOfLocalDay(date: string): Date {
  const [year, month, day] = date.split('-').map(Number)
  return new Date(year, month - 1, day, 23, 59, 59)
}

// Lets the org's one user generate/revoke anonymous, continuous-chat links
// for a deployed team (see docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md). Rendered inline on
// each deployed team's card in "My teams" (SessionsPage.tsx). Collapsed by
// default -- SessionsPage can list many teams, and this keeps the page from
// firing a share-links fetch per card on every load; the list only loads
// once the user opts in by clicking "Share". A link's daily cap and expiry
// are set at creation only: to change them, revoke and generate another.
export default function ShareLinksPanel({ pipelineId }: ShareLinksPanelProps) {
  const { t } = useTranslation()
  const [links, setLinks] = useState<ShareLink[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<number | null>(null)
  const [dailyCap, setDailyCap] = useState(String(DEFAULT_DAILY_CAP))
  const [expiresOn, setExpiresOn] = useState('')

  const refresh = () => {
    api
      .listShareLinks(pipelineId)
      .then(setLinks)
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    if (open) refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const handleCreate = async () => {
    // Clamp to the API's own 1..1000 range so a stray value is a sensible
    // link rather than a 422 the user has to decode.
    const cap = Math.min(1000, Math.max(1, Number(dailyCap) || DEFAULT_DAILY_CAP))
    const payload: { daily_cap: number; expires_at?: string } = { daily_cap: cap }
    if (expiresOn) payload.expires_at = endOfLocalDay(expiresOn).toISOString()
    try {
      await api.createShareLink(pipelineId, payload)
      refresh()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleRevoke = async (linkId: number) => {
    try {
      await api.patchShareLink(linkId, { active: false })
      refresh()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleCopy = async (link: ShareLink) => {
    // `navigator.clipboard` rejects (or is undefined) in a non-secure
    // context -- any HTTP origin that isn't localhost -- so this can't be
    // left unguarded.
    try {
      await navigator.clipboard.writeText(shareUrlFor(link.token))
      setCopiedId(link.id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {
      setError(t('shareLinks.copyFailed'))
    }
  }

  if (!open) {
    return (
      <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)}>
        {t('shareLinks.toggle')}
      </button>
    )
  }

  return (
    <div className="share-links-panel" onClick={(e) => e.stopPropagation()}>
      {error && <p className="banner banner-error">{error}</p>}
      <div className="share-links-form">
        <label>
          {t('shareLinks.messagesPerDay')}
          <input type="number" min={1} max={1000} value={dailyCap} onChange={(e) => setDailyCap(e.target.value)} />
        </label>
        <label>
          {t('shareLinks.expiresOn')}
          <input type="date" value={expiresOn} onChange={(e) => setExpiresOn(e.target.value)} />
        </label>
        <button type="button" className="btn btn-primary" onClick={handleCreate}>
          {t('shareLinks.generate')}
        </button>
      </div>
      <ul>
        {links.map((link) => (
          <li key={link.id}>
            <span>{link.active ? t('shareLinks.active') : t('shareLinks.revoked')}</span>
            <span>{t('shareLinks.perDay', { n: link.daily_cap })}</span>
            <span>
              {link.expires_at
                ? t('shareLinks.expires', { when: formatDateTime(link.expires_at) })
                : t('shareLinks.noExpiry')}
            </span>
            {link.active && (
              <>
                <button type="button" className="btn btn-secondary" onClick={() => handleCopy(link)}>
                  {copiedId === link.id ? t('shareLinks.copied') : t('shareLinks.copyLink')}
                </button>
                <button type="button" className="btn btn-danger-outline" onClick={() => handleRevoke(link.id)}>
                  {t('shareLinks.revoke')}
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
      <button type="button" className="btn-link" onClick={() => setOpen(false)}>
        {t('shareLinks.close')}
      </button>
    </div>
  )
}
```

Find where `.share-links-panel` is styled (`grep -rn "share-links-panel" ui/frontend/src --include=*.css`) and add beside it:

```css
.share-links-form {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 0.75rem;
  margin-bottom: 0.75rem;
}

.share-links-form label {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.85rem;
  color: var(--text-soft);
}

.share-links-form input {
  padding: 0.4rem 0.6rem;
  border-radius: 0.5rem;
  border: 1px solid var(--border-strong);
}
```

If no stylesheet styles `.share-links-panel` yet, append these rules to `ui/frontend/src/pages/wizard/SessionsPage.css` (the page that renders the panel) — check that file exists first with `ls ui/frontend/src/pages/wizard/`.

- [ ] **Step 4: Run the panel tests, lint, build**

Run: `npm test -- --run src/components/ShareLinksPanel.test.tsx && npm run lint && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/components/ShareLinksPanel.tsx ui/frontend/src/components/ShareLinksPanel.test.tsx <the css file you edited>
git commit -m "feat(share): set a link's daily cap and expiry when generating it; translate the panel"
```

---

### Task 7: `SharedSessionsPanel` i18n

**Files:**
- Modify: `ui/frontend/src/components/SharedSessionsPanel.tsx` (strings only)
- Test: `ui/frontend/src/components/SharedSessionsPanel.test.tsx` (add one test)

- [ ] **Step 1: Add a failing test**

Append inside the `describe` (add `import { setLanguage } from '../lib/i18n'` at the top and an `afterEach(() => setLanguage('en'))` next to the existing `beforeEach`):

```tsx
  it('renders in the active language', async () => {
    setLanguage('zh-CN')
    render(<SharedSessionsPanel pipelineId={5} />)
    expect(await screen.findByText('有效链接')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看对话记录' })).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- --run src/components/SharedSessionsPanel.test.tsx`
Expected: the new test FAILS (English text).

- [ ] **Step 3: Replace the literals**

In `SharedSessionsPanel.tsx`: add `import { useTranslation } from 'react-i18next'`, `const { t } = useTranslation()` as the first line of the component, and replace:
- `Back` → `{t('sharedSessions.back')}`
- `No share links for this team yet.` → `{t('sharedSessions.none')}`
- `{link.active ? 'Active link' : 'Revoked link'}` → `{link.active ? t('sharedSessions.activeLink') : t('sharedSessions.revokedLink')}`
- `Last active {formatDateTime(session.last_active_at)}` → `{t('sharedSessions.lastActive', { when: formatDateTime(session.last_active_at) })}`
- `{session.turns_today} turns today` → `{t('sharedSessions.turnsToday', { n: session.turns_today })}`
- `View transcript` → `{t('sharedSessions.viewTranscript')}`

- [ ] **Step 4: Run the tests**

Run: `npm test -- --run src/components/SharedSessionsPanel.test.tsx`
Expected: all PASS (the existing `getByText(/3/)`, `/7/`, `/view/i` matchers still match the English defaults).

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/components/SharedSessionsPanel.tsx ui/frontend/src/components/SharedSessionsPanel.test.tsx
git commit -m "i18n(share): translate the shared-sessions audit panel"
```

---

### Task 8: Let an admin diagnose a shared-chat turn

**Files:**
- Modify: `ui/backend/main.py:788-819` (`diagnose_run` docstring + the refusal)
- Modify: `tests/test_diagnostic_rerun.py:157-168` (+ new test, + import)
- Modify: `docs/ADMIN_MANUAL.md:96-99`, `docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md:73`, `ui/backend/CLAUDE.md:1129-1133`, `docs/STATUS.md:1716-1718`

- [ ] **Step 1: Write the failing test**

In `tests/test_diagnostic_rerun.py` change the models import to `from ui.backend.db.models import Run, ShareMessage`, rename `test_autonomous_or_shared_chat_runs_are_refused` to `test_autonomous_email_runs_are_refused` (body unchanged), and add after it:

```python
def test_a_shared_chat_turn_can_be_diagnosed_without_touching_the_visitor_session(rig):
    """A share-link turn is a regular run stamped with
    trigger_context["share_session_id"]. Its diagnostic re-run is a NEW row
    with no trigger_context, so share_transcript.record_share_reply is a
    no-op and nothing reaches the visitor's transcript -- the blanket
    refusal was protecting nothing (spec 2026-08-22)."""
    client, headers = rig
    _deploy(client, headers)
    org_id = get_org_id("org_a")
    with open_test_db() as db:
        db.add(Run(id="share-1", pipeline="wf", input="<user>hi</user>", status="completed", org_id=org_id,
                   username="share-link",
                   trigger_context={"share_link_id": 1, "share_session_id": 1, "turn_number": 1}))
        db.commit()

    resp = client.post("/api/runs/share-1/diagnose", headers=headers["op"])

    assert resp.status_code == 200, resp.text
    new_id = resp.json()["run_id"]
    assert _wait_finished(client, headers, new_id)["status"] == "completed"
    with open_test_db() as db:
        new_row = db.get(Run, new_id)
        assert new_row.diagnostic_of_run_id == "share-1"
        assert new_row.trigger_context is None
        assert db.query(ShareMessage).count() == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_diagnostic_rerun.py -q -k "shared_chat or email_runs"`
Expected: the new test FAILS with 400; the renamed one PASSES.

- [ ] **Step 3: Narrow the refusal**

In `main.py` replace lines 812-819 with:

```python
    if run_row.trigger_context is not None and "share_session_id" not in run_row.trigger_context:
        raise HTTPException(
            status_code=400,
            detail="Autonomous email runs can't be diagnosed: a re-run would reach the org's mailbox.",
        )
```

and replace the docstring paragraph that begins `Refused for a run with a \`trigger_context\`` with:

```
    Refused for an autonomous email run (a `trigger_context` without a
    `share_session_id`): it would reach the org's live mailbox with unscoped
    tools. A shared-chat turn IS allowed: the new row below carries no
    `trigger_context`, so `runtime`'s share-reply path is a no-op, the
    visitor's WebSocket (which keys on `share_session_id`) can't subscribe
    to it, and `runs.input` is the formatted transcript the turn actually
    saw. Also refused for a run that is itself a diagnostic run (diagnose
    the original instead) and for a purged run (no input left to re-run).
    No `user_id` is passed, so per-user memory is neither recalled nor
    written: the admin must not act as the customer. Spend is metered to the
    run's org like any other run of that team.
    Design: docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md,
    amended by 2026-08-22-share-chat-beta-patch-design.md §3.
```

- [ ] **Step 4: Run the whole file**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_diagnostic_rerun.py -q`
Expected: all PASS.

- [ ] **Step 5: Update the four docs**

- `docs/ADMIN_MANUAL.md` lines 96-99 → 
  ```
     - the run is an autonomous email run (it carries a `trigger_context`
       without a `share_session_id`) — a re-run would actually reach the
       org's live mailbox (400, with an explanatory message). A shared-chat
       turn *can* be diagnosed: the re-run never touches the visitor's
       session;
  ```
- `docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md` line 73: append ` **Amended 2026-08-22:** only autonomous email runs are refused; a shared-chat turn is allowed because step 5's "no trigger_context" already makes the share-reply path a no-op (see 2026-08-22-share-chat-beta-patch-design.md §3).`
- `ui/backend/CLAUDE.md` lines 1129-1133: replace `**Refused with 400 for a run carrying \`trigger_context\`**: an autonomous email run would reach the org's live mailbox with unscoped \`email_*\` tools, and a shared-chat turn would append a reply to the visitor's session (\`_safe_record_share_reply\` keys off that context).` with `**Refused with 400 for an autonomous email run** (a \`trigger_context\` without a \`share_session_id\`): it would reach the org's live mailbox with unscoped \`email_*\` tools. A shared-chat turn is allowed -- the diagnostic row has no \`trigger_context\`, so \`_safe_record_share_reply\` is a no-op and the visitor WS can't subscribe to it.`
- `docs/STATUS.md` lines 1716-1718: `Refused for autonomous/shared-chat runs (they would reach the mailbox / the visitor),` → `Refused for autonomous email runs (they would reach the mailbox; shared-chat turns are allowed since 2026-08-22 -- the re-run has no trigger_context and cannot touch the visitor's session),`

- [ ] **Step 6: Commit**

```bash
git add ui/backend/main.py tests/test_diagnostic_rerun.py docs/ADMIN_MANUAL.md docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md ui/backend/CLAUDE.md docs/STATUS.md
git commit -m "fix(diagnose): allow an admin to diagnose a shared-chat turn"
```

---

### Task 9: Frontend CLAUDE.md + STATUS done/known-issues/roadmap

**Files:**
- Modify: `ui/frontend/CLAUDE.md:335-369` ("Anonymous team sharing")
- Modify: `docs/STATUS.md` (Done: new entry before `## In Progress` at ~1721; Known issues: new bullet after the heading at ~1725; Roadmap: new bullet after `## Next steps / roadmap` at ~2115)

- [ ] **Step 1: `ui/frontend/CLAUDE.md`**

After the `lib/shareTraceEvents.ts` bullet (line ~351-354) replace it with:

```
- `lib/shareTraceEvents.ts`'s `friendlyStatusFor` maps a run's event stream
  to one short non-technical line — it returns an i18n key under
  `share.status.*`, and the page translates it. Cosmetic only — the backend
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
  never sends half a sentence. Each assistant bubble has a Copy control.
  Colours come from tokens (`--accent`/`--accent-contrast`) and the page is
  `100dvh` so a phone's collapsing address bar can't hide the composer.
```

In the org-side bullet, after `(generate/copy/revoke links for that team)` add `— a link's daily message cap and optional expiry are set at creation (`shareLinks.*` strings); to change them, revoke and regenerate`, and after `read a session's transcript)` add ` (`sharedSessions.*` strings)`.

- [ ] **Step 2: `docs/STATUS.md`**

Done (insert just before `## In Progress`):

```
- **Share-link chat beta patch** (2026-08-22, spec
  `docs/superpowers/specs/2026-08-22-share-chat-beta-patch-design.md`). The
  visitor page is bilingual (`share.*`) with its own language control
  (`components/LanguageSelect.tsx`, extracted from `Layout`), reads colour
  tokens, is `100dvh` on phones, has a multi-line composer with an IME guard
  and a per-reply Copy; the My-teams "Share" panel sets a link's daily cap
  and expiry at creation and shows both per link, and the audit panel is
  translated too. Backend: `_share_link_dict` emits offset-aware timestamps,
  and an admin can now diagnose a shared-chat turn (`POST /api/runs/{id}/
  diagnose` refuses only autonomous email runs — the diagnostic row has no
  `trigger_context`, so it cannot touch the visitor's session).
```

Known issues (first bullet under the heading):

```
- **The two share-chat fallback replies are persisted in English.**
  `share_transcript._FALLBACK_REPLY` and `share_chat._DISPATCH_FAILED_MESSAGE`
  go into `share_messages` verbatim; `ShareChatPage` recognises them by
  string equality (`lib/shareTraceEvents.ts::fallbackReplyKey`) and renders
  the visitor's language. Change either literal in lockstep, or replace the
  coupling with a stable code on `ShareMessage`.
```

Roadmap (first bullet under `## Next steps / roadmap`):

```
- **Share-link chat, step 2** (decided 2026-08-22 with the beta patch;
  needs its own spec): real token streaming for the *last* agent only —
  `model.stream()` in `langgraph_adapter`, deltas published to the in-memory
  `RunRegistry` and **never** written to `trace_events`, `stream_usage` so
  metering stays whole, cancel checkpoints between deltas; anonymous
  "step n of N" progress dots only alongside streaming (SEQUENTIAL shows a
  denominator from a new public `GET /api/share/{token}/team` count,
  PARALLEL shows n lit at once, HIERARCHICAL has no denominator and falls
  back to a pulse — names never leave `visitor_safe_event`); a visitor Stop
  button (new public cancel endpoint); the team name on the visitor page
  (a disclosure decision); markdown rendering of replies shared with the
  audit transcript (new dependency, decided against for the beta bundle).
```

- [ ] **Step 3: Commit**

```bash
git add ui/frontend/CLAUDE.md docs/STATUS.md
git commit -m "docs: record the share-link chat beta patch and the deferred streaming step"
```

---

### Task 10: All four gates, then push and open the PR

- [ ] **Step 1: Frontend gates**

Run from `ui/frontend`: `npm run lint && npm run build && npm test -- --run`
Expected: all green.

- [ ] **Step 2: Backend gate (serial, as `backend-full` does)**

Run from the repo root: `.\.venv\Scripts\python.exe -m pytest -m "not e2e" -q`
Expected: all green (≈3–4 min).

- [ ] **Step 3: E2E smoke (ports 8000/5173 must be free)**

Run: `.\.venv\Scripts\python.exe -m pytest tests/e2e/ -m "e2e and not slow" -q`
Expected: green. If a port is busy the fixture fails loudly naming it — stop whatever holds it and rerun.

- [ ] **Step 4: Push and open the PR (do not merge)**

```bash
git push -u origin fix/share-chat-beta-patch
gh pr create --base main --title "fix(share): share-link chat beta patch — bilingual visitor page, cap/expiry UI, diagnosable share turns" --body "$(cat <<'EOF'
## Summary
- Visitor share page: bilingual (`share.*`) with its own language control (`LanguageSelect`, extracted from Layout), token colours, `100dvh`, multi-line composer with IME guard, per-reply Copy.
- My teams: the Share panel sets a link's daily cap and expiry at creation and shows both; the audit panel is translated.
- Backend: `_share_link_dict` emits offset-aware timestamps; an admin can diagnose a shared-chat turn (only autonomous email runs are refused — the diagnostic row has no `trigger_context`).
- Docs: spec + plan, ADMIN_MANUAL, CLAUDE.md files, STATUS (done / known issue / step-2 roadmap).

Spec: `docs/superpowers/specs/2026-08-22-share-chat-beta-patch-design.md`

## Test plan
- [x] `npm run lint && npm run build && npm test`
- [x] `pytest -m "not e2e"` serial
- [x] `pytest tests/e2e/ -m "e2e and not slow"`
- [ ] Manual: `/share/<token>` in 中文, dark mode, phone viewport; Trace → Diagnose a share turn

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
