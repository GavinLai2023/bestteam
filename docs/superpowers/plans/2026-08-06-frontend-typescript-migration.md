# Frontend TypeScript Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `ui/frontend` (currently 100% JavaScript/JSX, no TS toolchain at all) to TypeScript end to end — every `.js`/`.jsx` source file becomes `.ts`/`.tsx` with real type coverage, `tsc --noEmit` is clean, and the app/tests/build behave identically to today.

**Architecture:** This is a behavior-preserving migration, not new feature work — there's no new user-facing behavior to test-drive. Each task adds TypeScript tooling and/or converts a cohesive group of files (rename + add types), then verifies via `tsc --noEmit`, `npm run lint`, `npm test`, and (final task only) `npm run build`, instead of a red/green TDD cycle. The existing Vitest suite is the regression safety net — its *assertions* must not change, only its file extension and, where it mocks `../lib/api`, the mock's typing.

**Tech Stack:** React 19, Vite 8, Vitest 4, React Router 7, ESLint 10 (flat config) — adding `typescript` + `typescript-eslint`. `@types/react`/`@types/react-dom` are already installed.

## Global Constraints

- Every task's diff must leave `npm run lint`, `npm test`, and `tsc --noEmit` (once Task 1 lands) clean before it's considered done.
- No runtime behavior changes. If a type surfaces a real bug, stop and flag it in the task's summary rather than silently changing behavior — fixing it is out of scope unless the user asks.
- `tsconfig.json` uses `strict: true`. Prefer precise types; use `unknown` (not `any`) for genuinely dynamic data (e.g. `AdvancedPage`'s raw JSON editor, WS event `data` payloads) and narrow with the existing runtime checks the code already does.
- Shared domain types live in `src/lib/types.ts` (created in Task 2) and are imported everywhere they're needed — don't redeclare `Specification`/`BuilderSession`/etc. per file.
- Component prop types are inline `interface <Component>Props { ... }` above the component, not a separate file.
- Test files mocking `../lib/api` via `vi.mock('../lib/api', () => ({ api: { ... } }))`: type the mocked functions as `Mock` (from `vitest`) or wrap the imported `api` with `vi.mocked(api)` at call sites — pick whichever the file already leans toward and stay consistent within that file. Do not change any assertion, only the types needed to make it compile.
- Every `.jsx`/`.js` file converted in a task must have its old file removed as part of the same git mv/rename — no leftover dead JS files.
- Commit after each task with a `refactor(frontend):` prefix.

---

### Task 1: TypeScript toolchain and config

**Files:**
- Modify: `ui/frontend/package.json`
- Create: `ui/frontend/tsconfig.json`
- Modify: `ui/frontend/vite.config.js` → rename to `ui/frontend/vite.config.ts`
- Modify: `ui/frontend/eslint.config.js`

**Interfaces:**
- Produces: `npm run typecheck` (new script), a `tsconfig.json` all later tasks' files must satisfy, and an ESLint config that lints `**/*.{ts,tsx}` with `typescript-eslint`'s recommended rules.

- [ ] **Step 1: Install TypeScript and typescript-eslint**

```powershell
cd ui/frontend
npm install -D typescript typescript-eslint
```

- [ ] **Step 2: Create `ui/frontend/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "noEmit": true,
    "isolatedModules": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "allowImportingTsExtensions": true,
    "types": ["vite/client"]
  },
  "include": ["src", "vite.config.ts"]
}
```

- [ ] **Step 3: Rename `vite.config.js` to `vite.config.ts`**

Content is unchanged (it's already valid TS — `defineConfig`/`react()` import, no JS-only syntax):

```ts
/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
  },
})
```

(Note the `setupFiles` path already anticipates Task 2 renaming `src/test/setup.js` → `.ts`.)

- [ ] **Step 4: Update `package.json` scripts and devDependencies**

Add a `typecheck` script and make `build` fail on type errors before bundling:

```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "lint": "eslint .",
    "typecheck": "tsc --noEmit",
    "preview": "vite preview",
    "test": "vitest run"
  }
}
```

`typescript` and `typescript-eslint` will already be present in `devDependencies` from Step 1 — just confirm they're there.

- [ ] **Step 5: Update `eslint.config.js` to lint TypeScript**

```js
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
  },
])
```

This targets `**/*.{ts,tsx}` only (not `.js`/`.jsx`) since by the end of this migration there will be no JS source files left. Until later tasks land, `npm run lint` will simply have nothing to lint yet for the still-unconverted files — that's expected and fine mid-migration.

- [ ] **Step 6: Verify the toolchain**

Run: `npm run typecheck` — expect it to pass trivially (no `.ts`/`.tsx` files exist yet, so there's nothing to check).
Run: `npm run lint` — expect no errors.
Run: `npm test` — expect the existing suite to still pass unchanged (nothing converted yet).

- [ ] **Step 7: Commit**

```bash
git add ui/frontend/package.json ui/frontend/package-lock.json ui/frontend/tsconfig.json ui/frontend/eslint.config.js
git add ui/frontend/vite.config.ts
git rm ui/frontend/vite.config.js
git commit -m "refactor(frontend): add TypeScript toolchain (tsconfig, typescript-eslint, typecheck script)"
```

---

### Task 2: Domain types and `lib/` foundation

**Files:**
- Create: `ui/frontend/src/lib/types.ts`
- Modify→rename: `src/lib/api.js` → `src/lib/api.ts`
- Modify→rename: `src/lib/models.js` → `src/lib/models.ts`
- Modify→rename: `src/lib/traceEvents.js` → `src/lib/traceEvents.ts`
- Modify→rename: `src/lib/dateFormat.js` → `src/lib/dateFormat.ts`
- Modify→rename: `src/lib/useMe.js` → `src/lib/useMe.ts`
- Modify→rename: `src/lib/useModelCatalog.js` → `src/lib/useModelCatalog.ts`
- Modify→rename: `src/lib/useBuilderSession.js` → `src/lib/useBuilderSession.ts`
- Modify→rename: `src/lib/dateFormat.test.js` → `src/lib/dateFormat.test.ts`
- Modify→rename: `src/lib/api.test.js` → `src/lib/api.test.ts`
- Modify→rename: `src/test/setup.js` → `src/test/setup.ts`

**Interfaces:**
- Produces: every type later tasks import — `Me`, `ModelCatalogEntry`, `AgentSpec`, `TeamSpec`, `Specification`, `Requirements`, `FeedbackHistoryEntry`, `BuilderSession`, `TraceEvent`, `RunListItem`, `AutomationResultPayload`, `AutomationResult`, `EmailTrigger`, `OrgEmailStatus`, `AdminOrg`, `AdminUser`, `MemoryUserSummary`, `MemoryRecord`, `ConfigItem`, `WizardOutletContext` (all from `src/lib/types.ts`); `api: { ... }` fully typed (from `src/lib/api.ts`); `pickDefaultModel(entries: ModelCatalogEntry[]): string` (from `src/lib/models.ts`); `EVENT_LABELS`, `TERMINAL_TYPES`, `RESULT_LABELS`, `renderEventData(event: TraceEvent): string | null` (from `src/lib/traceEvents.ts`); `formatDateTime(input: string | Date): string` (from `src/lib/dateFormat.ts`); `useMe(): { me: Me | null; loading: boolean; isAdmin: boolean }`; `useModelCatalog(): { entries: ModelCatalogEntry[]; loading: boolean; failed: boolean; retry: () => void }`; `useBuilderSession(sessionId: string | undefined): { session: BuilderSession | null; setSession: (s: BuilderSession) => void; loading: boolean; error: string | null; refresh: () => Promise<BuilderSession | null> }`.

- [ ] **Step 1: Write `src/lib/types.ts`**

These shapes are derived from how the frontend actually consumes each response today (not a full backend-schema transcription) — id types are confirmed against `ui/backend/db/models.py` (`Run.id`/`BuilderSession.id` are `str`, `AutomationItemResult.id` is `int`) and `src/bestteam/core/memory.py`'s `MemoryRecord.id: str`.

```ts
export interface Me {
  username: string
  is_admin: boolean
  org: string | null
}

export interface ModelCatalogEntry {
  spec: string
  display_name: string
}

export type TeamMode = 'sequential' | 'parallel' | 'hierarchical'

export interface AgentSpec {
  name: string
  role?: string
  goal?: string
  display_name?: string
  friendly_description?: string
}

export interface TeamSpec {
  name: string
  display_name?: string
  friendly_description?: string
  mode: TeamMode
  manager?: string
  agents: string[]
}

export interface Specification {
  name: string
  agents: AgentSpec[]
  teams: TeamSpec[]
  workflow?: { steps: string[] }
}

export interface Requirements {
  summary: string
  pain_points: string[]
  goals: string[]
  success_criteria: string[]
  constraints: string[]
  clarifying_questions: string[]
}

export interface FeedbackHistoryEntry {
  stage: string
  note: string
}

export interface BuilderSession {
  id: string | null
  status: string
  intent_text: string
  as_is_text?: string
  requirements_json?: Requirements | null
  specification_json?: Specification | null
  feedback_history?: FeedbackHistoryEntry[]
  uses_email?: boolean
  workflow_id?: string | null
  updated_at: string
}

// Context WizardLayout hands down via <Outlet context={...}> and every
// wizard stage page reads via useOutletContext<WizardOutletContext>().
export interface WizardOutletContext {
  session: BuilderSession | null
  setSession: (session: BuilderSession) => void
  loading: boolean
  refresh: () => Promise<BuilderSession | null>
  sessionId?: string
}

export interface TraceEvent {
  type: string
  agent?: string
  data?: Record<string, unknown> | string | null
}

export interface RunListItem {
  id: string
  workflow: string
  status: string
  autonomous: boolean
  started_at: string
}

export interface AutomationResultPayload {
  priority?: string
  summary?: string
  classification?: string
  category?: string
  missing_information?: string[]
  risk_reasons?: string[]
  human_reason?: string
  extracted?: { property_address?: string }
  action?: { draft_created?: boolean }
}

export interface AutomationResult {
  id: number
  run_id: string
  status: string
  created_at: string
  payload?: AutomationResultPayload
}

export interface EmailTrigger {
  enabled: boolean
  workflow_name: string | null
  status: 'active' | 'off' | 'disabled' | 'paused_cap' | 'error'
  daily_cap: number
  last_error?: string | null
  last_checked_at?: string | null
}

export interface OrgEmailStatus {
  connected: boolean
  host?: string
  username?: string
  port?: number
  drafts?: string | null
}

export interface AdminOrg {
  name: string
  display_name?: string
  active: boolean
  member?: string | null
}

export interface AdminUser {
  username: string
  org: string | null
  is_admin: boolean
}

export interface MemoryUserSummary {
  user_id: string
  org_id: number | null
  total: number
}

export interface MemoryRecord {
  id: string
  type: string
  content: string
  created_at: string
}

// AdvancedPage's raw JSON CRUD editor is intentionally untyped past this
// point — it's a generic editor over whatever config shape the backend
// accepts, not a form with known fields.
export type ConfigItem = Record<string, unknown>
```

- [ ] **Step 2: Convert `src/lib/dateFormat.js` → `src/lib/dateFormat.ts`**

```ts
const MONTHS = [
  'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
]

// "31 JUL 2026, 2:55 PM" -- the Team Activity page's date format.
export function formatDateTime(input: string | Date): string {
  const date = new Date(input)
  const day = String(date.getDate()).padStart(2, '0')
  const month = MONTHS[date.getMonth()]
  const time = date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
  return `${day} ${month} ${date.getFullYear()}, ${time}`
}
```

- [ ] **Step 3: Rename `src/lib/dateFormat.test.js` → `src/lib/dateFormat.test.ts`**

No content changes needed — it already only imports `formatDateTime` and uses `Date`/ISO strings, both covered by the new signature.

- [ ] **Step 4: Convert `src/lib/models.js` → `src/lib/models.ts`**

```ts
import type { ModelCatalogEntry } from './types'

// "First non-fake: catalog entry, else first entry, else fake:ok" -- the
// sensible default model used across the wizard when the customer hasn't
// picked one.
export function pickDefaultModel(entries: ModelCatalogEntry[] | undefined): string {
  if (!entries?.length) return 'fake:ok'
  const preferred = entries.find((entry) => !entry.spec.startsWith('fake:')) ?? entries[0]
  return preferred.spec
}
```

- [ ] **Step 5: Convert `src/lib/traceEvents.js` → `src/lib/traceEvents.ts`**

```ts
import type { TraceEvent } from './types'

// Shared trace-event rendering for MonitorPage's live view and the Activity
// page's run-detail view (live and historical), so both render the same
// event stream identically instead of duplicating the mapping.

export const EVENT_LABELS: Record<string, string> = {
  run_queued: '⏳ queued',
  run_started: '▶ started',
  agent_started: '● agent started',
  agent_progress: '… agent progress',
  tool_started: '🔧 tool started',
  tool_completed: '🔧 tool completed',
  delegation_started: '↳ delegating',
  subagent_started: '● sub-agent started',
  subagent_completed: '✓ sub-agent done',
  delegation_completed: '↳ delegation done',
  agent_completed: '✓ agent done',
  run_completed: '● completed',
  run_failed: '✕ failed',
  run_cancelled: '■ cancelled',
  memory_recalled: '🧠 memory recalled',
  memory_recorded: '🧠 memory recorded',
  memory_failed: '🧠 memory failed',
}

export const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled']

export const RESULT_LABELS: Record<string, string> = {
  run_completed: 'Final output',
  run_failed: 'Run failed',
  run_cancelled: 'Run cancelled',
}

// Several event types carry an object `data` (agent_started, tool_started/
// completed, agent_progress, delegation_*, subagent_*) instead of a plain
// string -- render each shape sensibly rather than dumping raw JSON.
export function renderEventData(event: TraceEvent): string | null {
  const { type, data } = event
  if (data === null || data === undefined) return null
  if (typeof data !== 'object') return data
  switch (type) {
    case 'agent_started':
      return [data.role, data.goal].filter(Boolean).join(' — ')
    case 'agent_progress':
      return (data.note as string | undefined) ?? null
    case 'tool_started':
      return (data.tool as string | undefined) ?? null
    case 'tool_completed': {
      const parts = [data.tool, data.success ? 'success' : 'failed']
      if (typeof data.duration_ms === 'number') parts.push(`${data.duration_ms}ms`)
      return [parts.join(' · '), data.summary].filter(Boolean).join(' — ')
    }
    case 'delegation_started':
    case 'delegation_completed':
      return [data.to, data.task_summary ?? data.summary].filter(Boolean).join(' — ')
    case 'subagent_started':
    case 'subagent_completed':
      return ((data.task_summary ?? data.summary) as string | undefined) ?? null
    default:
      return JSON.stringify(data)
  }
}
```

Note `data.role`/`data.goal`/etc. read through `Record<string, unknown>` — `.filter(Boolean).join(...)` on an `unknown[]` needs the array itself typed loosely; if `tsc` complains about `.join` on `unknown[]`, cast the two-element arrays as `(string | undefined)[]` at each call site (e.g. `[data.role as string | undefined, data.goal as string | undefined]`). Keep the runtime behavior byte-identical either way.

- [ ] **Step 6: Convert `src/lib/api.ts`**

```ts
import type {
  AdminOrg, AdminUser, AutomationResult, BuilderSession, ConfigItem, EmailTrigger,
  Me, MemoryRecord, MemoryUserSummary, ModelCatalogEntry, OrgEmailStatus, RunListItem,
  Specification, Requirements,
} from './types'

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'
export const WS_BASE: string = import.meta.env.VITE_WS_BASE ?? 'ws://127.0.0.1:8000'

export const TOKEN_KEY = 'bestteam_token'

interface ApiError extends Error {
  status?: number
}

// `?org=<name>`, or '' when no org applies (the skills built-in tier, the
// org-less model catalog). Omitting it on an org-scoped item route is a 422.
function orgQuery(org: string | null | undefined): string {
  return org ? `?${new URLSearchParams({ org })}` : ''
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((options.headers as Record<string, string>) || {}),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(`${API_BASE}${path}`, {
    headers,
    ...options,
  })

  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.assign('/login')
    throw new Error('Not authenticated')
  }

  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      // response had no JSON body
    }
    // Carry the HTTP status so callers can tell an error *response* (backend
    // reachable, e.g. a 403) apart from a network failure (fetch rejects with
    // no status). A network failure surfaces as a TypeError from fetch above
    // and never reaches this branch.
    const error: ApiError = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    error.status = res.status
    throw error
  }

  if (res.status === 204) return null as T
  return res.json()
}

async function uploadSingleFile<T>(path: string, file: File, fields: Record<string, unknown> = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers: Record<string, string> = {}
  if (token) headers['Authorization'] = `Bearer ${token}`

  const formData = new FormData()
  formData.append('file', file)
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null) formData.append(key, String(value))
  }

  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', headers, body: formData })

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.assign('/login')
    throw new Error('Not authenticated')
  }

  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      // no JSON body
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }

  return res.json()
}

async function uploadFiles<T>(path: string, files: File[], fields: Record<string, unknown> = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers: Record<string, string> = {}
  if (token) headers['Authorization'] = `Bearer ${token}`

  const formData = new FormData()
  for (const file of files) formData.append('files', file)
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null) formData.append(key, String(value))
  }

  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', headers, body: formData })

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.assign('/login')
    throw new Error('Not authenticated')
  }

  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      // response had no JSON body
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }

  return res.json()
}

export const api = {
  // Auth
  login: (username: string, password: string) =>
    request<{ access_token: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  me: () => request<Me>('/api/auth/me'),

  // Admin: per-user memory management
  memoryUsers: () => request<{ enabled: boolean; users: MemoryUserSummary[] }>('/api/memory/users'),
  memoryRecords: (
    userId: string,
    { query, type, org }: { query?: string; type?: string; org?: string | number | null } = {},
  ) => {
    const params = new URLSearchParams()
    if (query) params.set('query', query)
    if (type) params.set('type', type)
    if (org !== null && org !== undefined) params.set('org', String(org))
    const qs = params.toString()
    return request<{ records: MemoryRecord[] }>(
      `/api/memory/users/${encodeURIComponent(userId)}/records${qs ? `?${qs}` : ''}`,
    )
  },
  deleteMemoryRecord: (id: string) =>
    request<void>(`/api/memory/records/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  clearUserMemory: (userId: string) =>
    request<{ removed: number }>(`/api/memory/users/${encodeURIComponent(userId)}`, { method: 'DELETE' }),

  // Admin: org & user management (everyday provisioning; promote/demote and
  // platform-account lifecycle stay CLI-only).
  adminOrgs: () => request<AdminOrg[]>('/api/admin/orgs'),
  createAdminOrg: (name: string, display_name: string) =>
    request<AdminOrg>('/api/admin/orgs', { method: 'POST', body: JSON.stringify({ name, display_name }) }),
  setOrgActive: (name: string, active: boolean) =>
    request<AdminOrg>(`/api/admin/orgs/${encodeURIComponent(name)}`, {
      method: 'PATCH',
      body: JSON.stringify({ active }),
    }),
  adminUsers: () => request<AdminUser[]>('/api/admin/users'),
  createAdminUser: (username: string, org: string, password: string) =>
    request<AdminUser>('/api/admin/users', { method: 'POST', body: JSON.stringify({ username, org, password }) }),
  resetAdminUserPassword: (username: string, password: string) =>
    request<void>(`/api/admin/users/${encodeURIComponent(username)}/password`, {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  moveAdminUser: (username: string, to_org: string) =>
    request<void>(`/api/admin/users/${encodeURIComponent(username)}/move`, {
      method: 'POST',
      body: JSON.stringify({ to_org }),
    }),
  deleteAdminUser: (username: string) =>
    request<void>(`/api/admin/users/${encodeURIComponent(username)}`, { method: 'DELETE' }),

  // Monitoring
  listWorkflows: () => request<{ workflows: string[] }>('/api/workflows'),
  workflowGraph: (name: string) => request<{ mermaid: string }>(`/api/workflows/${encodeURIComponent(name)}/graph`),
  createRun: (workflow: string, input: string) =>
    request<{ run_id: string }>('/api/runs', { method: 'POST', body: JSON.stringify({ workflow, input }) }),
  getRun: (id: string) => request<RunListItem>(`/api/runs/${id}`),
  // Short-lived, single-use ticket for authenticating the stream WebSocket
  // (CR-013) -- only the ticket goes in the ws URL, never the bearer token.
  createWsTicket: () => request<{ ticket: string }>('/api/runs/ws-ticket', { method: 'POST' }),
  // Cooperative cancellation -- takes effect between yielded events, not
  // instantly (see ui/backend/CLAUDE.md).
  cancelRun: (id: string) => request<void>(`/api/runs/${id}/cancel`, { method: 'POST' }),
  // Safely retry a failed/errored autonomous email-triggered run over its
  // exact original UID batch -- always a new run (see ui/backend/CLAUDE.md,
  // "Property Maintenance Inbox").
  retryRun: (id: string) => request<{ run_id: string }>(`/api/runs/${id}/retry`, { method: 'POST' }),
  listRuns: (filters: Record<string, string | number | boolean | undefined | null> = {}) => {
    const params = new URLSearchParams(
      Object.fromEntries(
        Object.entries(filters)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ),
    )
    const qs = params.toString()
    return request<{ runs: RunListItem[] }>(`/api/runs${qs ? `?${qs}` : ''}`)
  },
  getRunTrace: (id: string) => request<{ events: import('./types').TraceEvent[] }>(`/api/runs/${id}/trace`),

  // Property Maintenance Inbox: structured, org-scoped automation results.
  listAutomationResults: (filters: Record<string, string | number | boolean | undefined | null> = {}) => {
    const params = new URLSearchParams(
      Object.fromEntries(
        Object.entries(filters)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ),
    )
    const qs = params.toString()
    return request<{ results: AutomationResult[] }>(`/api/automation-results${qs ? `?${qs}` : ''}`)
  },
  automationResultsSummary: (date?: string) => {
    const params: Record<string, string | number> = { tz_offset_minutes: new Date().getTimezoneOffset() }
    if (date) params.date = date
    return request<{
      ever_used: boolean
      emails_read: number
      maintenance_related: number
      drafts_created: number
      needs_attention: number
      possible_emergency: number
      skipped_non_maintenance: number
      errors: number
    }>(`/api/automation-results/summary?${new URLSearchParams(params as Record<string, string>)}`)
  },

  // Model catalog
  modelCatalog: () => request<ModelCatalogEntry[]>('/api/model-catalog'),

  // Advanced config (CRUD). `org` is the organization an item belongs to; the
  // backend requires it on every item route except skills (where omitting it
  // means the platform built-in tier) and the org-less model catalog.
  listOrgs: () => request<AdminOrg[]>('/api/config/orgs'),
  listConfig: (kind: string, org?: string) => request<ConfigItem[]>(`/api/config/${kind}${orgQuery(org)}`),
  getConfigItem: (kind: string, name: string, org?: string) =>
    request<ConfigItem>(`/api/config/${kind}/${encodeURIComponent(name)}${orgQuery(org)}`),
  putConfigItem: (kind: string, name: string, payload: ConfigItem, org?: string) =>
    request<ConfigItem>(`/api/config/${kind}/${encodeURIComponent(name)}${orgQuery(org)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteConfigItem: (kind: string, name: string, org?: string) =>
    request<void>(`/api/config/${kind}/${encodeURIComponent(name)}${orgQuery(org)}`, { method: 'DELETE' }),
  uploadKnowledgeBaseFiles: (name: string, files: File[], org?: string) =>
    uploadFiles<{ name: string; file_count: number; chunk_count: number; config: ConfigItem }>(
      `/api/config/knowledge_bases/${encodeURIComponent(name)}/upload${orgQuery(org)}`,
      files,
    ),

  // Org self-service settings: the org's mailbox for the email tools.
  getOrgEmail: () => request<OrgEmailStatus>('/api/org/email'),
  setOrgEmail: (payload: { host: string; username: string; password: string; port: number; drafts: string | null }) =>
    request<OrgEmailStatus>('/api/org/email', { method: 'PUT', body: JSON.stringify(payload) }),
  testOrgEmail: (payload: { host: string; username: string; password: string; port: number; drafts: string | null }) =>
    request<{ ok: boolean; error?: string }>('/api/org/email/test', { method: 'POST', body: JSON.stringify(payload) }),
  clearOrgEmail: () => request<void>('/api/org/email', { method: 'DELETE' }),

  // Autonomous email trigger: org-level "run on new mail" opt-in + activity.
  getEmailTrigger: () => request<EmailTrigger>('/api/org/email-trigger'),
  setEmailTrigger: (payload: { workflow_name: string; enabled: boolean }) =>
    request<EmailTrigger>('/api/org/email-trigger', { method: 'PUT', body: JSON.stringify(payload) }),
  emailTriggerActivity: () => request<unknown>('/api/org/email-trigger/activity'),

  // Interview recording transcription
  transcribeInterview: (file: File, model: string) =>
    uploadSingleFile<{ intent_text: string; as_is_text: string; transcript: string }>(
      '/api/builder/interview/transcribe',
      file,
      { model },
    ),

  // Builder wizard sessions
  createSession: (intent_text: string, as_is_text: string) =>
    request<BuilderSession>('/api/builder/sessions', {
      method: 'POST',
      body: JSON.stringify({ intent_text, as_is_text }),
    }),
  getSession: (id: string) => request<BuilderSession>(`/api/builder/sessions/${id}`),
  listSessions: () => request<{ sessions: BuilderSession[] }>('/api/builder/sessions'),
  deleteSession: (id: string) => request<void>(`/api/builder/sessions/${id}`, { method: 'DELETE' }),
  submitRequirements: (id: string, payload: { model?: string; feedback?: string; requirements?: Requirements }) =>
    request<BuilderSession>(`/api/builder/sessions/${id}/requirements`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  submitSpecification: (id: string, payload: { model: string }) =>
    request<BuilderSession>(`/api/builder/sessions/${id}/specification`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  submitSolution: (id: string, payload: { feedback: string; model: string }) =>
    request<BuilderSession>(`/api/builder/sessions/${id}/solution`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  createTestRun: (id: string, input: string) =>
    request<{ run_id: string }>(`/api/builder/sessions/${id}/test-runs`, {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  deploySession: (id: string) => request<BuilderSession>(`/api/builder/sessions/${id}/deploy`, { method: 'POST' }),
}
```

`import('./types').TraceEvent` inline is used once for `getRunTrace` purely to avoid a second named import line — feel free to hoist it into the top `import type { ... }` block instead if you prefer; behavior is identical either way.

- [ ] **Step 7: Rename `src/lib/api.test.js` → `src/lib/api.test.ts`**

The file mocks `globalThis.fetch = vi.fn().mockResolvedValue({...})` and reads `err.status` off a caught error. Add a local narrowing where needed, e.g.:

```ts
const err = await api.listWorkflows().catch((e) => e as { status?: number; message: string })
```

Keep every existing assertion; only add the types necessary to compile under `strict`.

- [ ] **Step 8: Convert `src/lib/useMe.ts`**

```ts
import { useEffect, useState } from 'react'
import { api } from './api'
import type { Me } from './types'

// Fetch the current user's identity/role once. Used by the nav shell and the
// admin route guard to show/hide the admin-only pages. Frontend gating is
// cosmetic -- the backend enforces admin on every /api/config and /api/memory
// call regardless.
export function useMe() {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    api
      .me()
      .then((data) => active && setMe(data))
      .catch(() => active && setMe(null))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  return { me, loading, isAdmin: Boolean(me?.is_admin) }
}
```

- [ ] **Step 9: Convert `src/lib/useModelCatalog.ts`**

```ts
import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type { ModelCatalogEntry } from './types'

// Fetches `/api/model-catalog` (the non-admin read endpoint -- the wizard
// runs as an org member). Used by wizard stages that let the customer pick
// (or default to) a model the Solution Architect should use.
// `failed`/`retry` let a caller that silently picks a default model (via
// `pickDefaultModel`) tell "still loading" and "fetch failed" apart from "the
// catalog is genuinely empty" -- the first two must never fall through to a
// `fake:` default in production, only the third legitimately can.
export function useModelCatalog() {
  const [entries, setEntries] = useState<ModelCatalogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    api
      .modelCatalog()
      .then(setEntries)
      .catch(() => {
        setEntries([])
        setFailed(true)
      })
      .finally(() => setLoading(false))
  }, [attempt])

  const retry = useCallback(() => {
    setLoading(true)
    setFailed(false)
    setAttempt((n) => n + 1)
  }, [])

  return { entries, loading, failed, retry }
}
```

- [ ] **Step 10: Convert `src/lib/useBuilderSession.ts`**

```ts
import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type { BuilderSession } from './types'

// Loads a builder session by id and exposes a `refresh()` to re-fetch after
// any wizard-stage mutation (requirements/specification/solution/deploy).
export function useBuilderSession(sessionId: string | undefined) {
  const [session, setSession] = useState<BuilderSession | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback((): Promise<BuilderSession | null> => {
    if (!sessionId) {
      setLoading(false)
      return Promise.resolve(null)
    }
    setLoading(true)
    setError(null)
    return api
      .getSession(sessionId)
      .then((data) => {
        setSession(data)
        return data
      })
      .catch((e: Error) => {
        setError(e.message)
        return null
      })
      .finally(() => setLoading(false))
  }, [sessionId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount/sessionId-change
    refresh()
  }, [refresh])

  return { session, setSession, loading, error, refresh }
}
```

- [ ] **Step 11: Rename `src/test/setup.js` → `src/test/setup.ts`**

Content unchanged: `import '@testing-library/jest-dom/vitest'`.

- [ ] **Step 12: Verify**

Run: `npm run typecheck` — expect clean (only `src/lib/**` and `src/test/setup.ts` exist as `.ts` so far; nothing else references them yet except each other).
Run: `npm run lint` — expect clean.
Run: `npm test -- src/lib` — expect `dateFormat.test.ts` and `api.test.ts` to pass unchanged.

- [ ] **Step 13: Commit**

```bash
git add ui/frontend/src/lib ui/frontend/src/test
git commit -m "refactor(frontend): convert src/lib to TypeScript, add shared domain types"
```

---

### Task 3: App entry and routing

**Files:**
- Modify→rename: `src/main.jsx` → `src/main.tsx`
- Modify→rename: `src/App.jsx` → `src/App.tsx`
- Modify→rename: `src/App.test.jsx` → `src/App.test.tsx`
- Modify: `index.html` (script `src` reference)

**Interfaces:**
- Consumes: `useMe` from `src/lib/useMe.ts` (Task 2).
- Produces: `RequireAdmin`, `RequireOrgMember` exported from `App.tsx` (used only by `App.test.tsx`).

- [ ] **Step 1: Convert `src/main.tsx`**

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'

const rootElement = document.getElementById('root')
if (!rootElement) throw new Error('#root element not found')

createRoot(rootElement).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
```

(`document.getElementById` is `HTMLElement | null` under `strict`, unlike the old JS where the non-null result was assumed — this null check is required to satisfy `createRoot`'s signature, not a behavior change: the app already requires `#root` to exist.)

- [ ] **Step 2: Update `index.html`**

Change `<script type="module" src="/src/main.jsx"></script>` to `<script type="module" src="/src/main.tsx"></script>`.

- [ ] **Step 3: Convert `src/App.tsx`**

Same structure as the current `App.jsx` — component bodies are unchanged, just add the file extension and, since this file has no props/state needing annotation, no type changes are needed beyond the `.tsx` extension and updated imports (`./components/Layout.tsx`, etc. — actually keep extension-less imports exactly as today, e.g. `from './components/Layout'`, since that's how the rest of the app imports and matches `moduleResolution: bundler`).

- [ ] **Step 4: Rename `src/App.test.jsx` → `src/App.test.tsx`**

Add typing to the `vi.mock('./lib/useMe', () => ({ useMe: vi.fn() }))` call site: `useMe.mockReturnValue(...)` needs `vi.mocked(useMe).mockReturnValue({ me: { is_admin: true, username: 'x', org: null }, loading: false, isAdmin: true })` (the `me` object must satisfy the full `Me` interface — add `username`/`org` fields the JS version didn't bother with). Keep every assertion identical.

- [ ] **Step 5: Verify**

Run: `npm run typecheck`, `npm run lint`, `npm test -- src/App.test.tsx` — all clean.
Run: `npm run dev` briefly (or `npm run build`) to confirm `index.html` resolves `main.tsx` correctly — Vite dev server should start with no console errors; stop it after confirming.

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/index.html ui/frontend/src/main.tsx ui/frontend/src/App.tsx ui/frontend/src/App.test.tsx
git rm ui/frontend/src/main.jsx ui/frontend/src/App.jsx ui/frontend/src/App.test.jsx
git commit -m "refactor(frontend): convert app entry and routing to TypeScript"
```

---

### Task 4: Layout and auth pages

**Files:**
- Modify→rename: `src/components/Layout.jsx` → `src/components/Layout.tsx`
- Modify→rename: `src/components/Layout.test.jsx` → `src/components/Layout.test.tsx`
- Modify→rename: `src/pages/LoginPage.jsx` → `src/pages/LoginPage.tsx`

**Interfaces:**
- Consumes: `useMe` (Task 2), `api.login` (Task 2).

- [ ] **Step 1: Convert `src/components/Layout.tsx`**

No props. Only change: file extension, and (if `tsc` flags it) explicit typing on the inline arrow functions already typed by React Router's own types (`NavLinkProps['className']` accepts a function — no annotation needed, `react-router-dom`'s types cover `({ isActive }) => ...` already).

- [ ] **Step 2: Rename `src/components/Layout.test.tsx`**

Same `vi.mock('../lib/useMe', ...)` treatment as `App.test.tsx` (Task 3) — `useMe.mockReturnValue({ me: { is_admin, username: 'x', org: null }, loading: false, isAdmin })`.

- [ ] **Step 3: Convert `src/pages/LoginPage.tsx`**

Type the one non-trivial piece — the submit handler and the caught error:

```tsx
const submit = async (e: React.FormEvent<HTMLFormElement>) => {
  e.preventDefault()
  if (!username.trim() || !password || submitting) return
  setSubmitting(true)
  setError(null)
  try {
    const { access_token } = await api.login(username.trim(), password)
    localStorage.setItem('bestteam_token', access_token)
    navigate('/')
  } catch (e) {
    setError((e as Error).message)
    setSubmitting(false)
  }
}
```

`useState` calls: `useState('')` for `username`/`password` infer `string` correctly already; `useState<string | null>(null)` for `error`.

- [ ] **Step 4: Verify**

Run: `npm run typecheck`, `npm run lint`, `npm test -- src/components/Layout.test.tsx` — clean.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/components/Layout.tsx ui/frontend/src/components/Layout.test.tsx ui/frontend/src/pages/LoginPage.tsx
git rm ui/frontend/src/components/Layout.jsx ui/frontend/src/components/Layout.test.jsx ui/frontend/src/pages/LoginPage.jsx
git commit -m "refactor(frontend): convert Layout and LoginPage to TypeScript"
```

---

### Task 5: Wizard chrome and shared components

**Files:**
- Modify→rename: `src/components/WizardLayout.jsx` → `.tsx`
- Modify→rename: `src/components/WizardProgress.jsx` → `.tsx`
- Modify→rename: `src/components/BulletEditor.jsx` → `.tsx`
- Modify→rename: `src/components/ModelPicker.jsx` → `.tsx`
- Modify→rename: `src/components/EmployeeCard.jsx` → `.tsx`
- Modify→rename: `src/components/TeamFlow.jsx` → `.tsx`

**Interfaces:**
- Consumes: `useBuilderSession` (Task 2), `useModelCatalog`/`pickDefaultModel` (Task 2), `BuilderSession`/`AgentSpec`/`TeamSpec`/`Specification`/`ModelCatalogEntry`/`WizardOutletContext` (Task 2 `types.ts`).
- Produces: `WizardProgressProps { session: BuilderSession | null }`, `BulletEditorProps { items: string[]; onChange: (items: string[]) => void; placeholder?: string }`, `ModelPickerProps { value: string; onChange: (value: string) => void; label?: string }`, `EmployeeCardProps { agent?: AgentSpec }`, `TeamFlowProps { specification?: Specification | null }` — these prop interfaces are what Task 6 imports and relies on.

- [ ] **Step 1: Convert `WizardLayout.tsx`**

```tsx
import { Outlet, useParams } from 'react-router-dom'
import { useBuilderSession } from '../lib/useBuilderSession'
import WizardProgress from './WizardProgress'
import './WizardLayout.css'

// Shared chrome for all six wizard stages: loads the builder session (if any)
// once, shows the progress bar, and hands `{ session, setSession, refresh,
// sessionId }` down to the active stage page via `useOutletContext()`.
export default function WizardLayout() {
  const { sessionId } = useParams()
  const { session, setSession, loading, error, refresh } = useBuilderSession(sessionId)

  return (
    <div className="wizard">
      <header className="wizard-header">
        <h1>Build your AI team</h1>
        <p>Answer a few questions and we'll design, test, and launch a custom AI team for you.</p>
      </header>

      <WizardProgress session={session} />

      {error && <p className="banner banner-error">Couldn't load this session: {error}</p>}

      <Outlet context={{ session, setSession, loading, refresh, sessionId }} />
    </div>
  )
}
```

- [ ] **Step 2: Convert `WizardProgress.tsx`**

```tsx
import { Link, useLocation, useParams } from 'react-router-dom'
import type { BuilderSession } from '../lib/types'
import './WizardProgress.css'

interface WizardProgressProps {
  session: BuilderSession | null
}

const STEPS = [
  { stage: 'intent', label: 'Your challenge' },
  { stage: 'preview', label: 'Meet your team' },
  { stage: 'confirm', label: 'Confirm' },
  { stage: 'deploy', label: 'Go live' },
]

function pathFor(stage: string, sessionId: string | undefined) {
  if (stage === 'intent') return '/wizard'
  return `/wizard/${sessionId}/${stage}`
}

// Renders the four-stage progress bar. `session` (may be null while the
// Intent stage hasn't created one yet) determines which later stages are
// reachable -- a customer can always look back, but can't skip ahead of
// what's actually been generated.
export default function WizardProgress({ session }: WizardProgressProps) {
  const { sessionId } = useParams()
  const location = useLocation()

  const currentStage = location.pathname === '/wizard' ? 'intent' : location.pathname.split('/').pop()

  const unlocked: Record<string, boolean> = {
    intent: true,
    preview: Boolean(session?.specification_json),
    confirm: Boolean(session?.specification_json),
    deploy: Boolean(session?.specification_json),
  }

  return (
    <ol className="wizard-progress">
      {STEPS.map((step, index) => {
        const isCurrent = step.stage === currentStage
        const isReachable = unlocked[step.stage]
        const className = `wizard-step${isCurrent ? ' current' : ''}${isReachable ? '' : ' locked'}`

        return (
          <li key={step.stage} className={className}>
            {isReachable && !isCurrent ? (
              <Link to={pathFor(step.stage, sessionId)}>
                <span className="wizard-step-number">{index + 1}</span>
                <span className="wizard-step-label">{step.label}</span>
              </Link>
            ) : (
              <span>
                <span className="wizard-step-number">{index + 1}</span>
                <span className="wizard-step-label">{step.label}</span>
              </span>
            )}
          </li>
        )
      })}
    </ol>
  )
}
```

- [ ] **Step 3: Convert `BulletEditor.tsx`**

```tsx
interface BulletEditorProps {
  items: string[]
  onChange: (items: string[]) => void
  placeholder?: string
}

// A simple editable list of short text items (pain points, goals, etc.)
// used by the Requirements stage's "summary card".
export default function BulletEditor({ items, onChange, placeholder }: BulletEditorProps) {
  const update = (index: number, value: string) => {
    const next = [...items]
    next[index] = value
    onChange(next)
  }

  const remove = (index: number) => onChange(items.filter((_, i) => i !== index))

  const add = () => onChange([...items, ''])

  return (
    <div className="bullet-editor">
      {items.map((item, index) => (
        <div className="bullet-editor-row" key={index}>
          <input
            type="text"
            value={item}
            placeholder={placeholder}
            onChange={(e) => update(index, e.target.value)}
          />
          <button type="button" className="bullet-editor-remove" onClick={() => remove(index)} aria-label="Remove">
            ×
          </button>
        </div>
      ))}
      <button type="button" className="btn-link" onClick={add}>
        + add
      </button>
    </div>
  )
}
```

- [ ] **Step 4: Convert `ModelPicker.tsx`**

```tsx
import { useEffect } from 'react'
import { useModelCatalog } from '../lib/useModelCatalog'
import { pickDefaultModel } from '../lib/models'

interface ModelPickerProps {
  value: string
  onChange: (value: string) => void
  label?: string
}

// Lets the customer pick which model the AI "team builder" agents should use
// for this generation step. Defaults to the first non-`fake:` catalog entry
// (a real model) once the catalog loads, falling back to the first entry.
export default function ModelPicker({ value, onChange, label = 'Model' }: ModelPickerProps) {
  const { entries } = useModelCatalog()

  useEffect(() => {
    if (value || !entries.length) return
    onChange(pickDefaultModel(entries))
  }, [entries, value, onChange])

  if (!entries.length) return null

  return (
    <div className="field">
      <label htmlFor="model-picker">{label}</label>
      <select id="model-picker" value={value} onChange={(e) => onChange(e.target.value)}>
        {entries.map((entry) => (
          <option key={entry.spec} value={entry.spec}>
            {entry.display_name}
          </option>
        ))}
      </select>
    </div>
  )
}
```

- [ ] **Step 5: Convert `EmployeeCard.tsx`**

```tsx
import type { AgentSpec } from '../lib/types'

interface EmployeeCardProps {
  agent?: AgentSpec
}

// A "virtual employee" card -- avatar placeholder + friendly job title +
// one-line description. Falls back to the technical agent fields
// (`name`/`role`/`goal`) if the Solution Architect didn't fill in the
// friendly `display_name`/`friendly_description`.
export default function EmployeeCard({ agent }: EmployeeCardProps) {
  if (!agent) return null

  const name = agent.display_name || agent.name
  const description = agent.friendly_description || agent.goal
  const initial = name.trim().charAt(0).toUpperCase() || '?'

  return (
    <div className="employee-card">
      <div className="employee-avatar">{initial}</div>
      <div className="employee-name">{name}</div>
      <div className="employee-role">{agent.role}</div>
      <p className="employee-description">{description}</p>
    </div>
  )
}
```

- [ ] **Step 6: Convert `TeamFlow.tsx`**

```tsx
import { Fragment } from 'react'
import EmployeeCard from './EmployeeCard'
import type { Specification, TeamMode } from '../lib/types'

interface TeamFlowProps {
  specification?: Specification | null
}

const MODE_LABELS: Record<TeamMode, string> = {
  sequential: 'Step by step',
  parallel: 'All at once',
  hierarchical: 'Led by a manager',
}

// Renders a customer-friendly "how your team works together" diagram from a
// Specification: one block per team (in workflow-step order), showing the
// manager (for hierarchical teams) and the agents who do the work, with no
// technical jargon -- just job titles and one-line descriptions.
export default function TeamFlow({ specification }: TeamFlowProps) {
  if (!specification) return null

  const agentsByName = Object.fromEntries((specification.agents ?? []).map((agent) => [agent.name, agent]))
  const teamsByName = Object.fromEntries((specification.teams ?? []).map((team) => [team.name, team]))
  const steps = specification.workflow?.steps ?? []

  return (
    <div className="team-flow">
      {steps.map((stepName, index) => {
        const team = teamsByName[stepName]
        if (!team) return null

        const memberNames = team.manager ? team.agents.filter((name) => name !== team.manager) : team.agents

        return (
          <div key={stepName}>
            {index > 0 && <div className="team-flow-arrow">↓</div>}
            <div className="team-block">
              <div className="team-block-header">
                <h3>{team.display_name || team.name}</h3>
                <span className="team-mode-badge">{MODE_LABELS[team.mode] ?? team.mode}</span>
              </div>
              {team.friendly_description && <p className="team-block-description">{team.friendly_description}</p>}

              {team.manager && (
                <>
                  <div className="team-manager-row">
                    <EmployeeCard agent={agentsByName[team.manager]} />
                  </div>
                  {memberNames.length > 0 && <div className="team-flow-arrow">↓</div>}
                </>
              )}

              <div className={`team-members-row ${team.mode === 'sequential' ? 'sequential' : ''}`}>
                {memberNames.map((name, i) => (
                  <Fragment key={name}>
                    {team.mode === 'sequential' && i > 0 && <span className="sequential-arrow">→</span>}
                    <EmployeeCard agent={agentsByName[name]} />
                  </Fragment>
                ))}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
```

- [ ] **Step 7: Verify**

Run: `npm run typecheck`, `npm run lint` — clean (these components have no dedicated test files; App.test.tsx / Layout.test.tsx from earlier tasks still pass).
Run: `npm test` — full suite still green (nothing here has tests of its own, but confirm no import breaks anything already converted).

- [ ] **Step 8: Commit**

```bash
git add ui/frontend/src/components/WizardLayout.tsx ui/frontend/src/components/WizardProgress.tsx ui/frontend/src/components/BulletEditor.tsx ui/frontend/src/components/ModelPicker.tsx ui/frontend/src/components/EmployeeCard.tsx ui/frontend/src/components/TeamFlow.tsx
git rm ui/frontend/src/components/WizardLayout.jsx ui/frontend/src/components/WizardProgress.jsx ui/frontend/src/components/BulletEditor.jsx ui/frontend/src/components/ModelPicker.jsx ui/frontend/src/components/EmployeeCard.jsx ui/frontend/src/components/TeamFlow.jsx
git commit -m "refactor(frontend): convert wizard chrome and shared components to TypeScript"
```

---

### Task 6: Wizard stage pages

**Files:**
- Modify→rename: `src/pages/wizard/IntentPage.jsx` → `.tsx`
- Modify→rename: `src/pages/wizard/PreviewPage.jsx` → `.tsx`
- Modify→rename: `src/pages/wizard/ConfirmPage.jsx` → `.tsx`
- Modify→rename: `src/pages/wizard/DeployPage.jsx` → `.tsx`
- Modify→rename: `src/pages/wizard/SessionsPage.jsx` → `.tsx`
- Modify→rename: `src/pages/wizard/SessionsPage.test.jsx` → `.tsx`

**Interfaces:**
- Consumes: `WizardOutletContext` (Task 2 `types.ts`), `TeamFlow`/`BulletEditor`/`ModelPicker` prop types (Task 5), `EmailConnect`/`EmailTriggerToggle` prop types (Task 8 — see note below), `api`/`WS_BASE` (Task 2), `formatDateTime` (Task 2), `TraceEvent` (Task 2).

**Note on ordering:** `PreviewPage` imports `EmailConnect` and `DeployPage` imports `EmailConnect`/`EmailTriggerToggle`, which aren't converted until Task 8. That's fine — TypeScript/Vite resolve `.jsx` and `.tsx` modules interchangeably during the migration (both are valid inputs to the bundler and to `tsc` with `allowJs` implicitly off but import resolution keyed by specifier, not extension, under `moduleResolution: bundler` — a plain `from '../../components/EmailConnect'` resolves to whichever of `EmailConnect.tsx`/`.jsx` exists on disk). Only issue: until Task 8 lands, `EmailConnect`/`EmailTriggerToggle` are still untyped (implicit `any` props from the `.jsx` file), which is legal even under `strict` since `.jsx` files aren't type-checked by `tsc` at all (they're outside `include`). No action needed here — Task 8 tightens those imports' types.

- [ ] **Step 1: Convert `IntentPage.tsx`**

Type the file-upload handler and the two async flows; state hooks get explicit types where inference is ambiguous:

```tsx
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'

const STAGE_LABELS: Record<string, string> = {
  creating: 'Setting things up…',
  requirements: 'Getting to know your business…',
  specification: 'Putting your team together…',
}

const UPLOAD_LABELS: Record<string, string> = {
  transcribing: 'Transcribing interview…',
  extracting: 'Extracting key points…',
}

const ACCEPTED_AUDIO = '.mp3,.mp4,.m4a,.wav,.webm,.mpeg,.mpga'

type Stage = null | 'creating' | 'requirements' | 'specification'
type UploadStage = null | 'transcribing' | 'extracting' | 'done'

export default function IntentPage() {
  const navigate = useNavigate()
  const { entries, loading: catalogLoading, failed: catalogFailed, retry: retryCatalog } = useModelCatalog()
  const catalogUnavailable = catalogFailed || (!catalogLoading && entries.length === 0)
  const [intentText, setIntentText] = useState('')
  const [asIsText, setAsIsText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [stage, setStage] = useState<Stage>(null)
  const [error, setError] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const [uploadStage, setUploadStage] = useState<UploadStage>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [transcript, setTranscript] = useState<string | null>(null)

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file || catalogLoading || catalogUnavailable) return
    e.target.value = '' // reset so the same file can be re-selected

    setUploadError(null)
    setUploadStage('transcribing')

    // After 5 s, advance the label to hint the extraction phase is underway.
    const timer = setTimeout(
      () => setUploadStage((s) => (s === 'transcribing' ? 'extracting' : s)),
      5000,
    )
    try {
      const result = await api.transcribeInterview(file, pickDefaultModel(entries))
      setIntentText(result.intent_text)
      setAsIsText(result.as_is_text)
      setTranscript(result.transcript)
      setSessionId(null) // force a fresh session with the new intent text
      setUploadStage('done')
    } catch (err) {
      setUploadError((err as Error).message)
      setUploadStage(null)
    } finally {
      clearTimeout(timer)
    }
  }

  const buildSpecification = async (id: string) => {
    setStage('specification')
    try {
      await api.submitSpecification(id, { model: pickDefaultModel(entries) })
      navigate(`/wizard/${id}/preview`)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
      setStage(null)
    }
  }

  const start = async () => {
    if (!intentText.trim() || submitting || catalogLoading || catalogUnavailable) return
    setSubmitting(true)
    setError(null)
    const model = pickDefaultModel(entries)

    let id = sessionId
    if (!id) {
      setStage('creating')
      try {
        const session = await api.createSession(intentText.trim(), asIsText.trim())
        id = session.id
        setSessionId(id)
      } catch (e) {
        setError((e as Error).message)
        setSubmitting(false)
        setStage(null)
        return
      }
    }

    if (!id) return

    // Best-effort: the Requirements summary is a nice-to-have internal
    // artifact. /specification degrades gracefully (falls back to the raw
    // intent/as-is text) if this fails, so don't block on it.
    setStage('requirements')
    try {
      await api.submitRequirements(id, { model })
    } catch {
      // ignored — non-blocking
    }

    await buildSpecification(id)
  }

  const retry = () => {
    if (catalogLoading || catalogUnavailable) return
    setError(null)
    setSubmitting(true)
    if (sessionId) {
      buildSpecification(sessionId)
    } else {
      start()
    }
  }

  const isUploading = uploadStage === 'transcribing' || uploadStage === 'extracting'

  return (
    // ...unchanged JSX from the current IntentPage.jsx...
  )
}
```

The only logic addition versus the current file is the `if (!id) return` guard after `createSession` (`session.id` is typed `string | null` per `BuilderSession`, and `buildSpecification` requires a `string`) — `createSession`'s real response always has an id, so this is dead code at runtime, not a behavior change, just satisfying `strict` null-checking. Keep the full JSX body exactly as in the current file (only the wrapping function signature and hooks above change).

- [ ] **Step 2: Convert `PreviewPage.tsx`**

Key types: `useOutletContext<WizardOutletContext>()`, `events: TraceEvent[]`, `wsRef: React.RefObject<WebSocket | null>`, `status: 'idle' | 'running' | 'completed' | 'failed'`.

```tsx
import { useEffect, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import EmailConnect from '../../components/EmailConnect'
import TeamFlow from '../../components/TeamFlow'
import { WS_BASE, api } from '../../lib/api'
import type { TraceEvent, WizardOutletContext } from '../../lib/types'

type Status = 'idle' | 'running' | 'completed' | 'failed'

export default function PreviewPage() {
  const { session, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()

  const [input, setInput] = useState('')
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => () => wsRef.current?.close(), [])

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Meet your team</h2>
        <p className="subtitle">We need a bit more information first.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            Start over
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json
  const agentsByName = Object.fromEntries((spec.agents ?? []).map((a) => [a.name, a]))

  const friendlyName = (agentName: string) => {
    const agent = agentsByName[agentName]
    return agent?.display_name || agentName
  }

  const titleFor = (event: TraceEvent) => {
    switch (event.type) {
      case 'run_started':
        return 'Your team got started'
      case 'agent_completed':
        return `${friendlyName(event.agent ?? '')} finished their part`
      case 'run_completed':
        return 'All done!'
      case 'run_failed':
        return 'Something went wrong'
      default:
        return event.type
    }
  }

  const run = async () => {
    if (!input.trim() || status === 'running') return
    setEvents([])
    setStatus('running')
    setError(null)
    wsRef.current?.close()

    try {
      const { run_id: runId } = await api.createTestRun(sessionId!, input.trim())

      const { ticket } = await api.createWsTicket()
      const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
      wsRef.current = ws
      ws.onmessage = (message: MessageEvent<string>) => {
        const event = JSON.parse(message.data) as TraceEvent
        setEvents((prev) => [...prev, event])
        if (event.type === 'run_completed') setStatus('completed')
        if (event.type === 'run_failed') setStatus('failed')
      }
      ws.onerror = () => {
        setStatus('failed')
        setError('Lost connection to the backend while your team was working. Please try again.')
      }
      ws.onclose = () => {
        setStatus((current) => {
          if (current === 'running') {
            setError('Lost connection to the backend while your team was working. Please try again.')
            return 'failed'
          }
          return current
        })
      }
    } catch (e) {
      setError((e as Error).message)
      setStatus('idle')
    }
  }

  return (
    // ...unchanged JSX from the current PreviewPage.jsx, `event.data` interpolation
    // (`{event.data}`) is fine since TraceEvent['data'] is renderable (string | object);
    // if tsc flags rendering an object, wrap with `renderEventData(event)` from
    // lib/traceEvents instead of raw `{event.data}` -- check current behavior first...
  )
}
```

`sessionId!` — `WizardOutletContext.sessionId` is optional because `IntentPage` (the only stage without a URL `:sessionId`) doesn't route through a context with one, but `PreviewPage` only renders under `/wizard/:sessionId/preview`, so it's always present here; non-null assertion documents that same guarantee the JS version relied on implicitly.

**Check `event.data` rendering:** the current JSX has `{event.data && <p className="activity-body">{event.data}</p>}` — since `TraceEvent.data` can be `Record<string, unknown>`, React can't render a plain object as a child. Look at the actual current file to confirm whether this ever receives an object in practice for PreviewPage's simplified inline event list (it does not use `renderEventData`, unlike MonitorPage/RunDetail) — if `tsc`/React's types complain, use `{typeof event.data === 'string' ? event.data : JSON.stringify(event.data)}` to preserve current runtime behavior (which already silently stringifies via React's own coercion for primitives, but would runtime-error on a real object — this is a pre-existing latent bug the type system will now surface; do not fix the underlying behavior, just make the types compile by mirroring what today's code does at runtime for the string case, and flag the object case in your task summary rather than changing behavior).

- [ ] **Step 3: Convert `ConfirmPage.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import BulletEditor from '../../components/BulletEditor'
import ModelPicker from '../../components/ModelPicker'
import TeamFlow from '../../components/TeamFlow'
import { api } from '../../lib/api'
import type { Requirements, WizardOutletContext } from '../../lib/types'

const EMPTY_REQUIREMENTS: Requirements = {
  summary: '',
  pain_points: [],
  goals: [],
  success_criteria: [],
  constraints: [],
  clarifying_questions: [],
}

export default function ConfirmPage() {
  const { session, setSession, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()

  const [model, setModel] = useState('')
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [showRequirements, setShowRequirements] = useState(false)
  const [reqDraft, setReqDraft] = useState<Requirements>(EMPTY_REQUIREMENTS)
  const [reqModel, setReqModel] = useState('')
  const [reqFeedback, setReqFeedback] = useState('')
  const [reqBusy, setReqBusy] = useState(false)
  const [reqError, setReqError] = useState<string | null>(null)

  useEffect(() => {
    if (session?.requirements_json) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- sync editable draft when AI (re)generates requirements
      setReqDraft({ ...EMPTY_REQUIREMENTS, ...session.requirements_json })
    }
  }, [session?.requirements_json])

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Confirm your team</h2>
        <p className="subtitle">We need a bit more information first.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            Start over
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json
  const history = (session.feedback_history ?? []).filter((entry) => entry.stage === 'solution')

  const applyFeedback = async () => {
    if (!model || !feedback.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.submitSolution(sessionId!, { feedback: feedback.trim(), model })
      setSession(updated)
      setFeedback('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const saveRequirements = async () => {
    if (reqBusy) return
    setReqBusy(true)
    setReqError(null)
    try {
      const updated = await api.submitRequirements(sessionId!, { requirements: reqDraft })
      setSession(updated)
    } catch (e) {
      setReqError((e as Error).message)
    } finally {
      setReqBusy(false)
    }
  }

  const regenerateRequirements = async () => {
    if (!reqModel || !reqFeedback.trim() || reqBusy) return
    setReqBusy(true)
    setReqError(null)
    try {
      const updated = await api.submitRequirements(sessionId!, { model: reqModel, feedback: reqFeedback.trim() })
      setSession(updated)
      setReqFeedback('')
    } catch (e) {
      setReqError((e as Error).message)
    } finally {
      setReqBusy(false)
    }
  }

  return (
    // ...unchanged JSX from the current ConfirmPage.jsx...
  )
}
```

- [ ] **Step 4: Convert `DeployPage.tsx`**

```tsx
import { useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import EmailConnect from '../../components/EmailConnect'
import EmailTriggerToggle from '../../components/EmailTriggerToggle'
import { api } from '../../lib/api'
import type { WizardOutletContext } from '../../lib/types'

export default function DeployPage() {
  const { session, setSession, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [emailConnected, setEmailConnected] = useState(false)

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Go live</h2>
        <p className="subtitle">Design your team first, then come back here to launch it.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            Start over
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json

  const deploy = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.deploySession(sessionId!)
      setSession(updated)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    // ...unchanged JSX from the current DeployPage.jsx...
  )
}
```

- [ ] **Step 5: Convert `SessionsPage.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/dateFormat'
import type { BuilderSession, EmailTrigger } from '../../lib/types'
import '../../components/WizardLayout.css'
import './SessionsPage.css'

const RESUMABLE_STATUSES = new Set(['spec', 'solution', 'testing', 'deployed'])
const STATUS_ORDER = ['deployed', 'in_progress']

const STATUS_LABELS: Record<string, string> = {
  deployed: 'Live',
  in_progress: 'In Progress',
}

const STATUS_EXPLANATIONS: Record<string, string> = {
  deployed: 'Live -- this team is deployed and ready for your organization to use.',
  in_progress: "Still being built -- you're designing, reviewing, or trying out this team before making it live.",
}

const AUTOMATION_STATUS_LABELS: Record<string, string> = {
  active: 'Automation on — watching for new email',
  paused_cap: 'Automation paused — daily limit reached',
  error: 'Automation problem — checking mailbox',
  disabled: 'Automation paused',
}

function bucketFor(status: string) {
  return status === 'deployed' ? 'deployed' : 'in_progress'
}

function resumePathFor(session: BuilderSession) {
  if (session.id == null) {
    return `/?workflow=${encodeURIComponent(session.specification_json?.name ?? '')}`
  }
  return session.status === 'deployed' ? `/wizard/${session.id}/deploy` : `/wizard/${session.id}/confirm`
}

function descriptionFor(session: BuilderSession) {
  return session.specification_json?.teams?.[0]?.friendly_description || session.intent_text
}

export default function SessionsPage() {
  const navigate = useNavigate()
  const [sessions, setSessions] = useState<BuilderSession[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [trigger, setTrigger] = useState<EmailTrigger | null>(null)
  const [openStatus, setOpenStatus] = useState<string | null>(null)

  useEffect(() => {
    api
      .listSessions()
      .then((data) => setSessions(data.sessions.filter((s) => RESUMABLE_STATUSES.has(s.status))))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
    api.getEmailTrigger().then(setTrigger).catch(() => {})
  }, [])

  const statusGroups = STATUS_ORDER.map((bucket) => ({
    status: bucket,
    sessions: sessions.filter((s) => bucketFor(s.status) === bucket),
  })).filter((group) => group.sessions.length > 0)

  const handleDelete = async (session: BuilderSession) => {
    const label = session.specification_json?.name ?? session.intent_text
    if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return
    try {
      await api.deleteSession(session.id!)
      setSessions((prev) => prev.filter((s) => s.id !== session.id))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    // ...unchanged JSX from the current SessionsPage.jsx...
  )
}
```

`session.id!` in `handleDelete` — the delete button only renders when `session.workflow_id == null`, which in practice never coincides with a null `session.id` (a synthetic no-session workflow entry has no delete button at all per the existing JSX condition); the assertion documents that existing invariant rather than changing it.

- [ ] **Step 6: Convert `SessionsPage.test.tsx`**

Rename only; update any `vi.mock('../../lib/api', ...)` mock typing the same way as other test tasks (`vi.mocked(api.xxx).mockResolvedValue(...)`), keeping every assertion identical.

- [ ] **Step 7: Verify**

Run: `npm run typecheck`, `npm run lint`, `npm test -- src/pages/wizard` — all clean, `SessionsPage.test.tsx` passes unchanged.

- [ ] **Step 8: Commit**

```bash
git add ui/frontend/src/pages/wizard
git rm ui/frontend/src/pages/wizard/IntentPage.jsx ui/frontend/src/pages/wizard/PreviewPage.jsx ui/frontend/src/pages/wizard/ConfirmPage.jsx ui/frontend/src/pages/wizard/DeployPage.jsx ui/frontend/src/pages/wizard/SessionsPage.jsx ui/frontend/src/pages/wizard/SessionsPage.test.jsx
git commit -m "refactor(frontend): convert wizard stage pages to TypeScript"
```

---

### Task 7: Monitor page and run detail

**Files:**
- Modify→rename: `src/pages/MonitorPage.jsx` → `.tsx`
- Modify→rename: `src/pages/MonitorPage.test.jsx` → `.tsx`
- Modify→rename: `src/components/RunDetail.jsx` → `.tsx`
- Modify→rename: `src/components/RunDetail.test.jsx` → `.tsx`

**Interfaces:**
- Consumes: `TraceEvent`, `AutomationResult` (Task 2 `types.ts`), `EVENT_LABELS`/`RESULT_LABELS`/`TERMINAL_TYPES`/`renderEventData` (Task 2 `traceEvents.ts`).
- Produces: `RunDetailProps { runId: string; status: string; autonomous: boolean; onRetried?: (newRunId: string) => void }` — Task 8's `ActivityPage` relies on this exact shape.

- [ ] **Step 1: Convert `MonitorPage.tsx`**

```tsx
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { API_BASE, WS_BASE, api } from '../lib/api'
import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import type { TraceEvent } from '../lib/types'
import './MonitorPage.css'

const NON_PROGRESS_TYPES = ['run_queued', 'run_started']
const STALE_HINT_SECONDS = 20

type Status = 'idle' | 'running' | 'completed' | 'failed' | 'cancelled' | 'unreachable'
type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'disconnected'

function MonitorPage() {
  const [searchParams] = useSearchParams()
  const [workflows, setWorkflows] = useState<string[]>([])
  const [selected, setSelected] = useState('')
  const [input, setInput] = useState('')
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('idle')
  const [cancelling, setCancelling] = useState(false)
  const [hasRunId, setHasRunId] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [secondsSinceLastEvent, setSecondsSinceLastEvent] = useState(0)
  const wsRef = useRef<WebSocket | null>(null)
  const runIdRef = useRef<string | null>(null)
  const runStartedAtRef = useRef<number | null>(null)
  const lastEventAtRef = useRef<number | null>(null)

  useEffect(() => {
    api.listWorkflows()
      .then((data) => {
        setWorkflows(data.workflows)
        const preferred = searchParams.get('workflow')
        if (preferred && data.workflows.includes(preferred)) {
          setSelected(preferred)
        } else if (data.workflows.length) {
          setSelected(data.workflows[0])
        }
      })
      .catch((err: { status?: number; message: string }) => {
        if (err?.status !== undefined) {
          setError(err.message)
        } else {
          setStatus('unreachable')
        }
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => () => wsRef.current?.close(), [])

  useEffect(() => {
    if (status !== 'running') return undefined
    const id = setInterval(() => {
      if (runStartedAtRef.current) {
        setElapsedSeconds(Math.max(0, Math.floor((Date.now() - runStartedAtRef.current) / 1000)))
      }
      if (lastEventAtRef.current) {
        setSecondsSinceLastEvent(Math.max(0, Math.floor((Date.now() - lastEventAtRef.current) / 1000)))
      }
    }, 1000)
    return () => clearInterval(id)
  }, [status])

  const startRun = async () => {
    if (!selected || !input.trim() || status === 'running') return

    setEvents([])
    setStatus('running')
    setError(null)
    setConnectionStatus('connecting')
    setCancelling(false)
    setElapsedSeconds(0)
    setSecondsSinceLastEvent(0)
    setHasRunId(false)
    wsRef.current?.close()
    runIdRef.current = null
    runStartedAtRef.current = Date.now()
    lastEventAtRef.current = Date.now()

    try {
      const { run_id: runId } = await api.createRun(selected, input)
      runIdRef.current = runId
      setHasRunId(true)

      const { ticket } = await api.createWsTicket()
      const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
      wsRef.current = ws
      ws.onopen = () => setConnectionStatus('connected')
      ws.onmessage = (message: MessageEvent<string>) => {
        const event = JSON.parse(message.data) as TraceEvent
        lastEventAtRef.current = Date.now()
        setEvents((prev) => [...prev, event])
        if (event.type === 'run_completed') setStatus('completed')
        if (event.type === 'run_failed') setStatus('failed')
        if (event.type === 'run_cancelled') setStatus('cancelled')
      }
      ws.onerror = () => {
        setConnectionStatus('disconnected')
        setStatus('unreachable')
      }
      ws.onclose = () => {
        setConnectionStatus('disconnected')
        setStatus((current) => (current === 'running' ? 'unreachable' : current))
      }
    } catch (e) {
      setError((e as Error).message)
      setStatus('idle')
      setConnectionStatus('idle')
    }
  }

  const cancelRun = async () => {
    if (!runIdRef.current || cancelling) return
    setCancelling(true)
    try {
      await api.cancelRun(runIdRef.current)
    } catch (e) {
      setError((e as Error).message)
      setCancelling(false)
    }
  }

  const finalEvent = events.find((e) => TERMINAL_TYPES.includes(e.type))
  const isWaitingForFirstProgress = status === 'running' && !events.some((e) => !NON_PROGRESS_TYPES.includes(e.type))

  return (
    // ...unchanged JSX from the current MonitorPage.jsx...
  )
}

export default MonitorPage
```

- [ ] **Step 2: Convert `MonitorPage.test.tsx`**

The `FakeWebSocket` test double needs a class shape TypeScript accepts as assignable to `global.WebSocket`. Simplest fix that changes nothing about test behavior: type it as its own class (not literally implementing the `WebSocket` interface) and cast at the assignment point where it's installed as the global, e.g. `global.WebSocket = FakeWebSocket as unknown as typeof WebSocket` (find that assignment in the file's setup — it's not shown in the first 80 lines read during planning; locate it before editing). Keep `vi.mock('../lib/api', () => ({ ... api: { listWorkflows: vi.fn(), ... } }))`'s inner functions as `vi.fn()` (untyped mocks are fine there — TS infers `Mock<...>` and the `.mockResolvedValue(...)` calls in each test typecheck against that inference without needing `vi.mocked()`).

- [ ] **Step 3: Convert `RunDetail.tsx`**

```tsx
import { useEffect, useRef, useState } from 'react'
import { WS_BASE, api } from '../lib/api'
import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import type { AutomationResult, TraceEvent } from '../lib/types'
import '../pages/MonitorPage.css'

interface RunDetailProps {
  runId: string
  status: string
  autonomous: boolean
  onRetried?: (newRunId: string) => void
}

type RetryState = 'idle' | 'retrying' | 'error'

export default function RunDetail({ runId, status, autonomous, onRetried }: RunDetailProps) {
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [automationResults, setAutomationResults] = useState<AutomationResult[]>([])
  const [retryState, setRetryState] = useState<RetryState>('idle')
  const [retryError, setRetryError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const finalEventType = events.find((e) => TERMINAL_TYPES.includes(e.type))?.type
  useEffect(() => {
    let ignore = false
    api
      .listAutomationResults({ run_id: runId })
      .then((data) => {
        if (!ignore) setAutomationResults(data.results)
      })
      .catch(() => {})
    return () => {
      ignore = true
    }
  }, [runId, finalEventType])

  const retry = async () => {
    setRetryState('retrying')
    setRetryError(null)
    try {
      const { run_id: newRunId } = await api.retryRun(runId)
      setRetryState('idle')
      onRetried?.(newRunId)
    } catch (e) {
      setRetryState('error')
      setRetryError((e as Error).message)
    }
  }

  useEffect(() => {
    if (status === 'running') {
      let cancelled = false
      ;(async () => {
        try {
          const { ticket } = await api.createWsTicket()
          if (cancelled) return
          const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
          wsRef.current = ws
          ws.onmessage = (message: MessageEvent<string>) => {
            const event = JSON.parse(message.data) as TraceEvent
            setEvents((prev) => [...prev, event])
          }
          ws.onerror = () => setError("Couldn't stream this run.")
        } catch (e) {
          if (!cancelled) setError((e as Error).message)
        }
      })()
      return () => {
        cancelled = true
        wsRef.current?.close()
      }
    }

    let ignore = false
    api
      .getRunTrace(runId)
      .then((data) => {
        if (!ignore) setEvents(data.events)
      })
      .catch((e: Error) => {
        if (!ignore) setError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [runId, status])

  const finalEvent = events.find((e) => e.type === finalEventType)

  return (
    // ...unchanged JSX from the current RunDetail.jsx...
  )
}
```

- [ ] **Step 4: Convert `RunDetail.test.tsx`**

Rename only; apply the same `../lib/api` mock-typing approach as `MonitorPage.test.tsx`.

- [ ] **Step 5: Verify**

Run: `npm run typecheck`, `npm run lint`, `npm test -- src/pages/MonitorPage.test.tsx src/components/RunDetail.test.tsx` — all clean, all assertions pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/pages/MonitorPage.tsx ui/frontend/src/pages/MonitorPage.test.tsx ui/frontend/src/components/RunDetail.tsx ui/frontend/src/components/RunDetail.test.tsx
git rm ui/frontend/src/pages/MonitorPage.jsx ui/frontend/src/pages/MonitorPage.test.jsx ui/frontend/src/components/RunDetail.jsx ui/frontend/src/components/RunDetail.test.jsx
git commit -m "refactor(frontend): convert MonitorPage and RunDetail to TypeScript"
```

---

### Task 8: Activity page and automation components

**Files:**
- Modify→rename: `src/pages/ActivityPage.jsx` → `.tsx`
- Modify→rename: `src/pages/ActivityPage.test.jsx` → `.tsx`
- Modify→rename: `src/components/EmailTriggerToggle.jsx` → `.tsx`
- Modify→rename: `src/components/EmailConnect.jsx` → `.tsx`
- Modify→rename: `src/components/EmailTriggerActivity.jsx` → `.tsx`
- Modify→rename: `src/components/EmailTriggerActivity.test.jsx` → `.tsx`
- Modify→rename: `src/components/MaintenanceInboxSummary.jsx` → `.tsx`
- Modify→rename: `src/components/MaintenanceInboxSummary.test.jsx` → `.tsx`
- Modify→rename: `src/components/NeedsAttentionList.jsx` → `.tsx`
- Modify→rename: `src/components/NeedsAttentionList.test.jsx` → `.tsx`

**Interfaces:**
- Consumes: `RunDetailProps` (Task 7), `RunListItem`/`EmailTrigger`/`OrgEmailStatus`/`AutomationResult` (Task 2 `types.ts`).

- [ ] **Step 1: Convert `EmailTriggerToggle.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { EmailTrigger } from '../lib/types'

interface EmailTriggerToggleProps {
  workflowName: string
}

export default function EmailTriggerToggle({ workflowName }: EmailTriggerToggleProps) {
  const [trigger, setTrigger] = useState<EmailTrigger | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.getEmailTrigger().then(setTrigger).catch((e: Error) => setError(e.message))
  }, [])

  if (!trigger) return error ? <p className="banner banner-error">{error}</p> : null

  const onForThis = trigger.enabled && trigger.workflow_name === workflowName

  const toggle = async () => {
    setBusy(true)
    setError(null)
    try {
      setTrigger(await api.setEmailTrigger({ workflow_name: workflowName, enabled: !onForThis }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    // ...unchanged JSX from the current EmailTriggerToggle.jsx...
  )
}
```

- [ ] **Step 2: Convert `EmailConnect.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { OrgEmailStatus } from '../lib/types'

interface EmailConnectProps {
  onChange?: () => void
  onStatusChange?: (connected: boolean) => void
}

interface EmailForm {
  host: string
  username: string
  password: string
  port: number
  drafts: string
}

export default function EmailConnect({ onChange, onStatusChange }: EmailConnectProps) {
  const [status, setStatus] = useState<OrgEmailStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState<EmailForm>({ host: '', username: '', password: '', port: 993, drafts: '' })
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'' | 'test' | 'save' | 'clear'>('')
  const [showAdvanced, setShowAdvanced] = useState(false)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const s = await api.getOrgEmail()
      setStatus(s)
      onStatusChange?.(!!s.connected)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const field = (k: keyof EmailForm) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: e.target.value })

  const payload = () => ({
    host: form.host.trim(),
    username: form.username.trim(),
    password: form.password,
    port: Number(form.port) || 993,
    drafts: form.drafts.trim() || null,
  })

  const test = async () => {
    setBusy('test'); setError(null); setTestResult(null)
    try {
      setTestResult(await api.testOrgEmail(payload()))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const save = async () => {
    setBusy('save'); setError(null)
    try {
      await api.setOrgEmail(payload())
      setEditing(false)
      setForm({ host: '', username: '', password: '', port: 993, drafts: '' })
      setTestResult(null)
      await refresh()
      onChange?.()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const disconnect = async () => {
    setBusy('clear'); setError(null)
    try {
      await api.clearOrgEmail()
      await refresh()
      onChange?.()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const startReconnect = () => {
    if (!status) return
    setForm({
      host: status.host || '',
      username: status.username || '',
      password: '',
      port: status.port || 993,
      drafts: status.drafts || '',
    })
    setShowAdvanced(Boolean((status.port && status.port !== 993) || status.drafts))
    setEditing(true)
  }

  if (loading) return <p className="hint">Checking mailbox…</p>

  if (status === null) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb' }}>
        <h3>Connect your mailbox</h3>
        {error && <p className="banner banner-error">{error}</p>}
        <div className="wizard-actions">
          <button className="btn btn-secondary" onClick={refresh}>Retry</button>
        </div>
      </div>
    )
  }

  const canSubmit = form.host.trim() && form.username.trim() && form.password

  return (
    // ...unchanged JSX from the current EmailConnect.jsx, using `field('host')` etc.
    // for onChange handlers as before, `field`'s type above covers every input...
  )
}
```

Note the `field()` port input is `type="number"` bound to `form.port` (typed `number` in `EmailForm`) but `field()` writes `e.target.value` (a `string`) directly into that slot — the current JS code has this same quirk (`setForm({ ...form, port: e.target.value })` stores a string until `payload()`'s `Number(form.port)` coerces it back). Under `strict`, `EmailForm.port: number` being assigned a `string` via generic `field('port')` will not typecheck. Fix by typing `EmailForm.port: number | string` (matches actual runtime values across the input's lifecycle) rather than forcing a behavior change — update the interface above to `port: number | string` and adjust `payload()`'s `Number(form.port)` call site (already handles both). Keep `startReconnect`'s `port: status.port || 993` (a real `number` from `OrgEmailStatus`) assigning fine either way.

- [ ] **Step 3: Convert `EmailTriggerActivity.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { EmailTrigger } from '../lib/types'

interface EmailTriggerActivityProps {
  onViewRuns?: () => void
}

const STATUS_META: Record<string, { badge: string; text: string }> = {
  active: { badge: 'Active', text: 'Watching for new email.' },
  off: { badge: 'Off', text: 'Automatic runs are turned off.' },
  disabled: { badge: 'Paused', text: 'Paused by the operator.' },
  paused_cap: { badge: 'Paused', text: "Today's run limit was reached -- resumes tomorrow." },
  error: { badge: 'Problem', text: 'Problem checking the mailbox.' },
}

const REFRESH_INTERVAL_MS = 30_000

export default function EmailTriggerActivity({ onViewRuns }: EmailTriggerActivityProps) {
  const [trigger, setTrigger] = useState<EmailTrigger | undefined>(undefined)
  const [statusFailed, setStatusFailed] = useState(false)

  useEffect(() => {
    const load = () => {
      api.getEmailTrigger().then(setTrigger).catch(() => setStatusFailed(true))
    }
    load()
    const id = setInterval(load, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
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

  if (trigger === undefined) return null

  if (!trigger.workflow_name) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <h3>Automatic runs</h3>
        <p className="hint">
          No automatic runs configured yet. Connect a mailbox and turn one on from a team's
          Deploy page in the Team Builder.
        </p>
      </div>
    )
  }

  const meta = STATUS_META[trigger.status] ?? { badge: trigger.status, text: '' }

  return (
    // ...unchanged JSX from the current EmailTriggerActivity.jsx...
  )
}
```

- [ ] **Step 4: Convert `EmailTriggerActivity.test.tsx`**

Rename + mock-type per the established pattern.

- [ ] **Step 5: Convert `MaintenanceInboxSummary.tsx`**

State type: `useState<{ ever_used: boolean; emails_read: number; maintenance_related: number; drafts_created: number; needs_attention: number; possible_emergency: number; skipped_non_maintenance: number; errors: number } | undefined>(undefined)` — reuse `Awaited<ReturnType<typeof api.automationResultsSummary>>` instead of hand-writing the shape twice:

```tsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'

type Summary = Awaited<ReturnType<typeof api.automationResultsSummary>>

function _todayLocal(): string {
  const now = new Date()
  const yyyy = now.getFullYear()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd}`
}

const REFRESH_INTERVAL_MS = 30_000

export default function MaintenanceInboxSummary() {
  const [summary, setSummary] = useState<Summary | undefined>(undefined)
  const [error, setError] = useState(false)

  useEffect(() => {
    const load = () => {
      api
        .automationResultsSummary(_todayLocal())
        .then((data) => {
          setSummary(data)
          setError(false)
        })
        .catch(() => setError(true))
    }
    load()
    const id = setInterval(load, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  if (error) return null
  if (summary === undefined) return null
  if (!summary.ever_used) return null

  return (
    // ...unchanged JSX from the current MaintenanceInboxSummary.jsx...
  )
}
```

Use this same `Awaited<ReturnType<typeof api.xxx>>` pattern anywhere else in this task a component's state exactly mirrors one `api.*` call's resolved value, instead of re-declaring the shape in `types.ts`.

- [ ] **Step 6: Convert `MaintenanceInboxSummary.test.tsx`**

Rename + mock-type per the established pattern.

- [ ] **Step 7: Convert `NeedsAttentionList.tsx`**

```tsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { AutomationResult } from '../lib/types'

interface NeedsAttentionListProps {
  onOpenRun?: (runId: string) => void
}

const REFRESH_INTERVAL_MS = 30_000

const PRIORITY_LABELS: Record<string, string> = {
  possible_emergency: 'Possible emergency',
  priority: 'Priority',
  routine: 'Routine',
  unknown: 'Unknown',
}

export default function NeedsAttentionList({ onOpenRun }: NeedsAttentionListProps) {
  const [results, setResults] = useState<AutomationResult[] | undefined>(undefined)
  const [error, setError] = useState(false)

  useEffect(() => {
    const load = () => {
      api
        .listAutomationResults({ needs_attention: true, limit: 20 })
        .then((data) => {
          setResults(data.results)
          setError(false)
        })
        .catch(() => setError(true))
    }
    load()
    const id = setInterval(load, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  if (error) {
    return <p className="banner banner-error">Couldn't load the needs-attention list. Refresh the page to try again.</p>
  }
  if (results === undefined) return null
  if (results.length === 0) return null

  return (
    // ...unchanged JSX from the current NeedsAttentionList.jsx...
  )
}
```

- [ ] **Step 8: Convert `NeedsAttentionList.test.tsx`**

Rename + mock-type per the established pattern.

- [ ] **Step 9: Convert `ActivityPage.tsx`**

```tsx
import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import EmailTriggerActivity from '../components/EmailTriggerActivity'
import MaintenanceInboxSummary from '../components/MaintenanceInboxSummary'
import NeedsAttentionList from '../components/NeedsAttentionList'
import RunDetail from '../components/RunDetail'
import type { RunListItem } from '../lib/types'
import '../components/WizardLayout.css'
import './ActivityPage.css'

const STATUS_OPTIONS = ['running', 'completed', 'failed', 'cancelled']
const RUN_POLL_INTERVAL_MS = 5000

interface Filters {
  workflow: string
  manual: '' | 'true' | 'false'
  status: string
}

interface SelectedRun {
  id: string
  status: string
  autonomous: boolean
}

function runsQueryParams(filters: Filters) {
  const params: Record<string, string | boolean> = {}
  if (filters.workflow) params.workflow = filters.workflow
  if (filters.manual === 'true') params.manual = true
  if (filters.manual === 'false') params.manual = false
  if (filters.status) params.status = filters.status
  return params
}

export default function ActivityPage() {
  const [tab, setTab] = useState<'automations' | 'runs'>('automations')
  const [workflows, setWorkflows] = useState<string[]>([])
  const [runs, setRuns] = useState<RunListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filters, setFilters] = useState<Filters>({ workflow: '', manual: '', status: '' })
  const [selectedRun, setSelectedRun] = useState<SelectedRun | null>(null)
  const hasRunningRun = runs.some((run) => run.status === 'running')
  const runDetailRef = useRef<HTMLElement>(null)

  useEffect(() => {
    if (selectedRun) runDetailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [selectedRun])

  useEffect(() => {
    api
      .listWorkflows()
      .then((d) => setWorkflows(d.workflows))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (tab !== 'runs') return undefined
    let ignore = false
    api
      .listRuns(runsQueryParams(filters))
      .then((d) => {
        if (ignore) return
        setRuns(d.runs)
        setError(null)
      })
      .catch((e: Error) => {
        if (!ignore) setError(e.message)
      })
      .finally(() => {
        if (!ignore) setLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [tab, filters])

  useEffect(() => {
    if (tab !== 'runs' || !hasRunningRun) return undefined
    let ignore = false
    const id = setInterval(() => {
      api
        .listRuns(runsQueryParams(filters))
        .then((d) => {
          if (!ignore) setRuns(d.runs)
        })
        .catch(() => {})
    }, RUN_POLL_INTERVAL_MS)
    return () => {
      ignore = true
      clearInterval(id)
    }
  }, [tab, filters, hasRunningRun])

  return (
    // ...unchanged JSX from the current ActivityPage.jsx; the inline onOpenRun
    // callback passed to NeedsAttentionList and the RunDetail onRetried prop
    // both already match SelectedRun's shape...
  )
}
```

- [ ] **Step 10: Convert `ActivityPage.test.tsx`**

Rename + mock-type per the established pattern (this file also mocks `../components/RunDetail` — check whether it stubs it with `vi.mock('../components/RunDetail', () => ({ default: () => <div /> }))` or similar; if so, ensure the stub's default export type-satisfies `RunDetailProps` usage at the call site, typically by simply not needing explicit typing since JSX call sites don't require the stub itself to be typed beyond being a valid component).

- [ ] **Step 11: Verify**

Run: `npm run typecheck`, `npm run lint`, `npm test -- src/pages/ActivityPage.test.tsx src/components/EmailTriggerActivity.test.tsx src/components/MaintenanceInboxSummary.test.tsx src/components/NeedsAttentionList.test.tsx` — all clean.

- [ ] **Step 12: Commit**

```bash
git add ui/frontend/src/pages/ActivityPage.tsx ui/frontend/src/pages/ActivityPage.test.tsx ui/frontend/src/components/EmailTriggerToggle.tsx ui/frontend/src/components/EmailConnect.tsx ui/frontend/src/components/EmailTriggerActivity.tsx ui/frontend/src/components/EmailTriggerActivity.test.tsx ui/frontend/src/components/MaintenanceInboxSummary.tsx ui/frontend/src/components/MaintenanceInboxSummary.test.tsx ui/frontend/src/components/NeedsAttentionList.tsx ui/frontend/src/components/NeedsAttentionList.test.tsx
git rm ui/frontend/src/pages/ActivityPage.jsx ui/frontend/src/pages/ActivityPage.test.jsx ui/frontend/src/components/EmailTriggerToggle.jsx ui/frontend/src/components/EmailConnect.jsx ui/frontend/src/components/EmailTriggerActivity.jsx ui/frontend/src/components/EmailTriggerActivity.test.jsx ui/frontend/src/components/MaintenanceInboxSummary.jsx ui/frontend/src/components/MaintenanceInboxSummary.test.jsx ui/frontend/src/components/NeedsAttentionList.jsx ui/frontend/src/components/NeedsAttentionList.test.jsx
git commit -m "refactor(frontend): convert ActivityPage and automation components to TypeScript"
```

---

### Task 9: Admin pages

**Files:**
- Modify→rename: `src/pages/AccountsPage.jsx` → `.tsx`
- Modify→rename: `src/pages/AccountsPage.test.jsx` → `.tsx`
- Modify→rename: `src/pages/MemoryPage.jsx` → `.tsx`
- Modify→rename: `src/pages/MemoryPage.test.jsx` → `.tsx`
- Modify→rename: `src/pages/AdvancedPage.jsx` → `.tsx`

**Interfaces:**
- Consumes: `AdminOrg`, `AdminUser`, `MemoryUserSummary`, `MemoryRecord`, `ConfigItem` (Task 2 `types.ts`).

- [ ] **Step 1: Convert `AccountsPage.tsx`**

Key types: `orgs: AdminOrg[]`, `users: AdminUser[]`, `drafts: Record<string, { username: string; password: string; confirm: string }>`, and the `run<T>(promise: Promise<T>, okMessage?: string): Promise<boolean>` helper generic over whatever the caller passes:

```tsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { AdminOrg, AdminUser } from '../lib/types'
import '../components/WizardLayout.css'
import './AdvancedPage.css'
import './AccountsPage.css'

interface UserDraft {
  username: string
  password: string
  confirm: string
}

export default function AccountsPage() {
  const [orgs, setOrgs] = useState<AdminOrg[]>([])
  const [users, setUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const [newOrgName, setNewOrgName] = useState('')
  const [newOrgDisplay, setNewOrgDisplay] = useState('')
  const [drafts, setDrafts] = useState<Record<string, UserDraft>>({})

  const reload = () =>
    Promise.all([api.adminOrgs(), api.adminUsers()]).then(([o, u]) => {
      setOrgs(o)
      setUsers(u)
    })

  useEffect(() => {
    reload()
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const run = (promise: Promise<unknown>, okMessage?: string): Promise<boolean> => {
    setError(null)
    setMessage(null)
    return promise.then(
      () =>
        reload().then(
          () => {
            if (okMessage) setMessage(okMessage)
            return true
          },
          () => {
            setError('The change was saved, but the list could not be refreshed — reload the page to see it.')
            return true
          },
        ),
      (e: Error) => {
        setError(e.message)
        return false
      },
    )
  }

  const createOrg = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!newOrgName.trim()) return
    run(api.createAdminOrg(newOrgName.trim(), newOrgDisplay.trim()), `Created '${newOrgName.trim()}'.`).then(
      (ok) => {
        if (ok) {
          setNewOrgName('')
          setNewOrgDisplay('')
        }
      },
    )
  }

  const toggleActive = (org: AdminOrg) => {
    if (org.active && !window.confirm(`Deactivate '${org.name}'? Its user won't be able to log in.`)) return
    run(api.setOrgActive(org.name, !org.active))
  }

  const emptyDraft: UserDraft = { username: '', password: '', confirm: '' }
  const draftFor = (org: string) => drafts[org] || emptyDraft
  const setDraft = (org: string, patch: Partial<UserDraft>) =>
    setDrafts((d) => ({ ...d, [org]: { ...draftFor(org), ...patch } }))

  const createUser = (e: React.FormEvent<HTMLFormElement>, org: string) => {
    e.preventDefault()
    const { username, password, confirm } = draftFor(org)
    if (!username.trim() || !password) return
    if (password !== confirm) {
      setMessage(null)
      setError('Passwords do not match.')
      return
    }
    run(api.createAdminUser(username.trim(), org, password)).then((ok) => {
      if (ok) setDrafts((d) => ({ ...d, [org]: emptyDraft }))
    })
  }

  const resetPassword = (username: string) => {
    const pw = window.prompt(`New password for '${username}'`)
    if (!pw) return
    run(api.resetAdminUserPassword(username, pw), `Password reset for '${username}'.`)
  }

  const moveUser = (username: string) => {
    const to = window.prompt(`Move '${username}' to which organization?`)
    if (!to || !to.trim()) return
    run(api.moveAdminUser(username, to.trim()))
  }

  const removeUser = (username: string) => {
    if (!window.confirm(`Delete user '${username}'? This also purges their memory.`)) return
    run(api.deleteAdminUser(username), `Deleted '${username}'.`)
  }

  const platformAccounts = users.filter((u) => u.org === null)

  if (loading) return null

  return (
    // ...unchanged JSX from the current AccountsPage.jsx...
  )
}
```

- [ ] **Step 2: Convert `AccountsPage.test.tsx`**

Rename + mock-type per the established pattern.

- [ ] **Step 3: Convert `MemoryPage.tsx`**

Key types: an `Identity { user_id: string; org_id: number | null }` local type (matches `MemoryUserSummary`'s two id fields), `records: MemoryRecord[]`.

```tsx
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { MemoryRecord, MemoryUserSummary } from '../lib/types'
import '../components/WizardLayout.css'
import './AdvancedPage.css'

const TYPES = ['episodic', 'semantic', 'procedural']

interface Identity {
  user_id: string
  org_id: number | null
}

export default function MemoryPage() {
  const [enabled, setEnabled] = useState(true)
  const [users, setUsers] = useState<MemoryUserSummary[]>([])
  const [selected, setSelected] = useState<Identity | null>(null)
  const [records, setRecords] = useState<MemoryRecord[]>([])
  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const loadUsers = () => {
    setLoading(true)
    setError(null)
    api
      .memoryUsers()
      .then((data) => {
        setEnabled(data.enabled)
        setUsers(data.users)
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    loadUsers()
  }, [])

  const loadRecords = (identity: Identity | null, opts: { query?: string; type?: string } = {}) => {
    if (!identity) return
    setError(null)
    api
      .memoryRecords(identity.user_id, {
        query: opts.query ?? query,
        type: opts.type ?? typeFilter,
        org: identity.org_id == null ? 'legacy' : identity.org_id,
      })
      .then((data) => setRecords(data.records))
      .catch((e: Error) => setError(e.message))
  }

  const selectIdentity = (identity: Identity) => {
    setSelected(identity)
    setMessage(null)
    setError(null)
    setQuery('')
    setTypeFilter('')
    loadRecords(identity, { query: '', type: '' })
  }

  const filterByType = (type: string) => {
    setTypeFilter(type)
    loadRecords(selected, { type })
  }

  const submitSearch = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    loadRecords(selected)
  }

  const sameIdentity = (a: Identity | null, b: Identity | null) =>
    a !== null && b !== null && a.user_id === b.user_id && a.org_id === b.org_id
  const scopeLabel = (orgId: number | null) => (orgId == null ? 'legacy (no org)' : `org ${orgId}`)

  const deleteRecord = async (id: string) => {
    if (!window.confirm('Delete this memory record? This cannot be undone.')) return
    setError(null)
    setMessage(null)
    try {
      await api.deleteMemoryRecord(id)
      setRecords((prev) => prev.filter((r) => r.id !== id))
      setMessage('Record deleted.')
      loadUsers()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const clearUser = async () => {
    if (!selected) return
    const name = selected.user_id
    if (!window.confirm(`Clear ALL memory for "${name}" (every organization)? This cannot be undone.`))
      return
    setError(null)
    setMessage(null)
    try {
      const result = await api.clearUserMemory(name)
      setRecords([])
      setMessage(`Cleared ${result.removed} record(s) for ${name}.`)
      setSelected(null)
      loadUsers()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (!loading && !enabled) {
    return (
      <div className="advanced">
        <header>
          <h1>Per-user memory</h1>
        </header>
        <p className="banner banner-error">
          Memory is not enabled on this deployment. Set <code>BESTTEAM_MEMORY_DB</code> to a database
          path to enable per-user memory, then restart the backend.
        </p>
      </div>
    )
  }

  return (
    // ...unchanged JSX from the current MemoryPage.jsx...
  )
}
```

- [ ] **Step 4: Convert `MemoryPage.test.tsx`**

Rename + mock-type per the established pattern.

- [ ] **Step 5: Convert `AdvancedPage.tsx`**

Key types: `KINDS`' `orgScope: 'required' | 'optional' | 'none'` as a literal union, `items: ConfigItem[]`.

```tsx
import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { AdminOrg, ConfigItem } from '../lib/types'
import '../components/WizardLayout.css'
import './AdvancedPage.css'

const PLATFORM_TIER = '__platform__'

interface Kind {
  key: string
  label: string
  idField: string
  editableField: string | null
  orgScope: 'required' | 'optional' | 'none'
  readOnly?: boolean
}

const KINDS: Kind[] = [
  { key: 'workflows', label: 'Workflows', idField: 'name', editableField: 'config', orgScope: 'required' },
  { key: 'skills', label: 'Skills', idField: 'name', editableField: 'config', orgScope: 'optional' },
  { key: 'knowledge_bases', label: 'Knowledge bases', idField: 'name', editableField: 'config', orgScope: 'required' },
  { key: 'tools', label: 'Tools', idField: 'name', editableField: null, orgScope: 'none', readOnly: true },
  { key: 'model-catalog', label: 'Model catalog', idField: 'spec', editableField: null, orgScope: 'none' },
]

function itemId(kind: Kind, item: ConfigItem): string {
  return String(item[kind.idField])
}

function defaultOrgFor(kind: Kind, orgs: AdminOrg[]): string | null {
  if (kind.orgScope === 'none') return null
  if (kind.orgScope === 'optional') return PLATFORM_TIER
  return orgs.length ? orgs[0].name : null
}

function editableJson(kind: Kind, item: ConfigItem): ConfigItem {
  if (kind.editableField) return (item[kind.editableField] as ConfigItem) ?? {}
  const rest = { ...item }
  delete rest[kind.idField]
  return rest
}

export default function AdvancedPage() {
  const [activeKey, setActiveKey] = useState(KINDS[0].key)
  const [items, setItems] = useState<ConfigItem[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [jsonText, setJsonText] = useState('')
  const [newId, setNewId] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [createMode, setCreateMode] = useState<'manual' | 'upload'>('manual')
  const [uploadFiles, setUploadFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  const [orgs, setOrgs] = useState<AdminOrg[]>([])
  const [org, setOrg] = useState<string | null>(null)

  const kind = KINDS.find((k) => k.key === activeKey)!
  const activeKeyRef = useRef(activeKey)
  const loadSeq = useRef(0)

  const apiOrg = kind.orgScope === 'none' || org === PLATFORM_TIER ? undefined : (org ?? undefined)

  const visibleItems =
    kind.orgScope === 'optional' && org === PLATFORM_TIER
      ? items.filter((it) => it.org == null)
      : items
  const selectedItem = visibleItems.find((it) => itemId(kind, it) === selectedId)

  useEffect(() => {
    activeKeyRef.current = activeKey
  }, [activeKey])

  const loadItems = () => {
    const seq = ++loadSeq.current
    setLoading(true)
    setError(null)
    api
      .listConfig(activeKey, apiOrg)
      .then((data) => {
        if (seq === loadSeq.current) setItems(data)
      })
      .catch((e: Error) => {
        if (seq === loadSeq.current) setError(e.message)
      })
      .finally(() => {
        if (seq === loadSeq.current) setLoading(false)
      })
  }

  useEffect(() => {
    api
      .listOrgs()
      .then((data) => {
        setOrgs(data)
        setOrg((current) => current ?? defaultOrgFor(kind, data))
      })
      .catch((e: Error) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (kind.orgScope === 'required' && !org) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on tab/org change
    loadItems()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, org])

  const resetSelection = () => {
    setSelectedId(null)
    setJsonText('')
    setMessage(null)
    setError(null)
    setNewId('')
    setCreateMode('manual')
    setUploadFiles([])
  }

  const selectKind = (k: Kind) => {
    if (k.key === activeKey) return
    setActiveKey(k.key)
    setOrg(defaultOrgFor(k, orgs))
    resetSelection()
  }

  const selectOrg = (value: string) => {
    if (value === org) return
    setOrg(value)
    resetSelection()
  }

  const select = (id: string) => {
    const item = visibleItems.find((it) => itemId(kind, it) === id)
    setSelectedId(id)
    setMessage(null)
    setError(null)
    setJsonText(JSON.stringify(item ? editableJson(kind, item) : {}, null, 2))
  }

  const startNew = () => {
    if (!newId.trim()) return
    setSelectedId(newId.trim())
    setNewId('')
    setMessage(null)
    setError(null)
    setJsonText('{\n  \n}')
  }

  const uploadNew = async () => {
    if (!newId.trim() || uploadFiles.length === 0) return
    const startedFor = activeKey
    setUploading(true)
    setError(null)
    setMessage(null)
    try {
      const result = await api.uploadKnowledgeBaseFiles(newId.trim(), uploadFiles, apiOrg)
      setMessage(`Created '${result.name}' — ${result.file_count} file(s), ${result.chunk_count} chunk(s) indexed.`)
      setNewId('')
      setUploadFiles([])
      setSelectedId(result.name)
      setJsonText(JSON.stringify(result.config, null, 2))
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUploading(false)
    }
  }

  const save = async () => {
    let parsed: ConfigItem
    try {
      parsed = JSON.parse(jsonText)
    } catch {
      setError('Not valid JSON')
      return
    }

    const startedFor = activeKey
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.putConfigItem(activeKey, selectedId!, parsed, apiOrg)
      setMessage('Saved.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!selectedId) return
    if (!window.confirm(`Delete "${selectedId}" from ${kind.label}? This cannot be undone.`)) return
    const startedFor = activeKey
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.deleteConfigItem(activeKey, selectedId, apiOrg)
      setSelectedId(null)
      setJsonText('')
      setMessage('Deleted.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    // ...unchanged JSX from the current AdvancedPage.jsx...
  )
}
```

`kind = KINDS.find(...)!` — `activeKey` is always initialized from and only ever set to a `KINDS[i].key`, so the lookup always succeeds; the assertion documents that invariant. This file has no dedicated test file today, so there's no test-conversion step.

- [ ] **Step 6: Verify**

Run: `npm run typecheck`, `npm run lint`, `npm test -- src/pages/AccountsPage.test.tsx src/pages/MemoryPage.test.tsx` — all clean.

- [ ] **Step 7: Commit**

```bash
git add ui/frontend/src/pages/AccountsPage.tsx ui/frontend/src/pages/AccountsPage.test.tsx ui/frontend/src/pages/MemoryPage.tsx ui/frontend/src/pages/MemoryPage.test.tsx ui/frontend/src/pages/AdvancedPage.tsx
git rm ui/frontend/src/pages/AccountsPage.jsx ui/frontend/src/pages/AccountsPage.test.jsx ui/frontend/src/pages/MemoryPage.jsx ui/frontend/src/pages/MemoryPage.test.jsx ui/frontend/src/pages/AdvancedPage.jsx
git commit -m "refactor(frontend): convert admin pages to TypeScript"
```

---

### Task 10: Final verification and docs

**Files:**
- Modify: `ui/frontend/CLAUDE.md`
- No source changes expected — this task is a full-repo check plus doc extension updates.

- [ ] **Step 1: Confirm no JS source files remain**

Run: `find ui/frontend/src -name '*.js' -o -name '*.jsx'` (or `Get-ChildItem -Recurse -Include *.js,*.jsx` under `ui/frontend/src` in PowerShell) — expect zero results. If any remain, they were missed by an earlier task; convert them following that task's pattern before continuing.

- [ ] **Step 2: Update `ui/frontend/CLAUDE.md` extension references**

This doc names specific files with their old `.jsx`/`.js` extensions throughout (e.g. `main.jsx`, `components/Layout.jsx`, `components/WizardLayout.jsx`, `components/WizardProgress.jsx`, `components/TeamFlow.jsx`/`EmployeeCard.jsx`, `pages/wizard/*.jsx`, `components/EmailTriggerActivity.jsx`, `components/MaintenanceInboxSummary.jsx`, `components/NeedsAttentionList.jsx`, `components/RunDetail.jsx`, `pages/AdvancedPage.jsx`, `pages/AccountsPage.jsx`, `pages/MemoryPage.jsx`, `App.jsx`, `lib/api.js`, `lib/useBuilderSession.js`, `lib/useModelCatalog.js`, `lib/useMe.js`). Update every one of these to its new `.tsx`/`.ts` extension. Do not touch `.css` filenames (unchanged) or any other content/wording — this is an extension-only find/replace across the file, since the doc's structure and explanations remain accurate.

- [ ] **Step 3: Full verification pass**

Run, in order, from `ui/frontend`:

```powershell
npm run typecheck
npm run lint
npm test
npm run build
```

All four must succeed. `npm run build` in particular catches any remaining type error across the whole project at once (it's `tsc --noEmit && vite build` per Task 1) and confirms the production bundle still builds.

- [ ] **Step 4: Manual smoke check**

Run: `npm run dev`, open the app in a browser, and confirm: the login page renders, and (if you have a dev backend running per the root `CLAUDE.md` commands) at least one page navigates without a console error. If no backend is available, it's sufficient to confirm the dev server starts cleanly and the login page renders with no console errors — note in your task summary which was possible. Stop the dev server after checking.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/CLAUDE.md
git commit -m "docs(frontend): update CLAUDE.md file extensions after TypeScript migration"
```

---

## Post-plan note

`AdvancedPage`'s raw-JSON editor and `api.ts`'s config-CRUD methods deliberately stay loosely typed (`ConfigItem = Record<string, unknown>`) — that page's whole purpose is editing arbitrary backend-accepted JSON, so precise field types there would fight the design rather than help it. If a future task wants stronger typing for a specific config kind (e.g. a `WorkflowConfig` interface), that's a separate, scoped follow-up, not part of this migration.
