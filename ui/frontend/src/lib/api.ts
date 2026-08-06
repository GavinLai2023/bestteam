import type {
  AdminOrg, AdminUser, AutomationResult, BuilderSession, ConfigItem, EmailTrigger,
  Me, MemoryRecord, MemoryUserSummary, ModelCatalogEntry, OrgEmailStatus, RunListItem,
  Requirements,
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
