import type {
  AdminOrg, AdminUser, AutomationResult, BuilderSession, ConfigItem, EmailBudget, EmailBudgetInput,
  EmailFilterSettings, EmailTrigger, FilteredMessage,
  IngestionJobStatus, KnowledgeBaseCapabilities, KnowledgeBaseSearchResponse,
  Me, MemoryRecord, MemoryUserSummary, ModelAnalyticsSummary,
  ModelCatalogEntry, NotificationList, NotificationSettings, NotificationSettingsPayload,
  OrgEmailConnectPayload, OrgEmailStatus, OrgExportBundle, OrgKnowledgeBase, RetentionSettings, RunListItem,
  Requirements, ShareLink, ShareMessage, ShareSessionSummary,
  UsageRecord, PipelineAnalyticsDetail, PipelineAnalyticsSummary,
} from './types'

// `localhost`, NOT `127.0.0.1` -- do not "simplify" this back. The anonymous
// share-chat visitor cookie (`share_chat.py`, SameSite=Lax) is only sent back
// on same-SITE requests, and a browser treats `localhost` and `127.0.0.1` as
// different sites. Vite's dev server serves this app on `localhost:5173`, so
// pointing the API at `127.0.0.1:8000` means the cookie is set but never
// returned: every message silently starts a brand-new session (no continuous
// chat at all) and the WS handshake carries no cookie, so it closes 4404.
// Same-site cares about the registrable domain, not the port, so
// `localhost:5173` -> `localhost:8000` is fine.
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'
export const WS_BASE: string = import.meta.env.VITE_WS_BASE ?? 'ws://localhost:8000'

export const TOKEN_KEY = 'bestteam_token'

interface ApiError extends Error {
  status?: number
}

// `?org=<name>`, or '' when no org applies (the skills built-in tier, the
// org-less model catalog). Omitting it on an org-scoped item route is a 422.
function orgQuery(org: string | null | undefined): string {
  return org ? `?${new URLSearchParams({ org })}` : ''
}

interface ValidationErrorItem {
  loc?: unknown[]
  msg?: unknown
}

function isValidationErrors(detail: unknown): detail is ValidationErrorItem[] {
  return Array.isArray(detail) && detail.every((d) => d !== null && typeof d === 'object' && 'msg' in d)
}

// FastAPI/Pydantic v2 request-validation failures put a list of error objects
// in `detail` (each with `loc`/`msg`) rather than a string -- dumping that
// raw as JSON is unreadable. Render each as "<field>: <message>", dropping
// the leading "body"/"query"/"path" location segment and Pydantic's "Value
// error, " prefix on custom validator messages.
function formatErrorDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (isValidationErrors(detail)) {
    return detail
      .map((d) => {
        const loc = Array.isArray(d.loc) ? d.loc.filter((s) => s !== 'body' && s !== 'query' && s !== 'path') : []
        const field = loc.join('.')
        const msg = String(d.msg).replace(/^Value error, /, '')
        return field ? `${field}: ${msg}` : msg
      })
      .join('; ')
  }
  return JSON.stringify(detail)
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
    const error: ApiError = new Error(formatErrorDetail(detail))
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
    throw new Error(formatErrorDetail(detail))
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
    // Carry the HTTP status so callers can distinguish e.g. a 409 "confirm to
    // replace" response from any other upload failure (mirrors `request()`).
    const error: ApiError = new Error(formatErrorDetail(detail))
    error.status = res.status
    throw error
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
  listPipelines: () => request<{ pipelines: string[]; pipeline_ids?: Record<string, number> }>('/api/pipelines'),
  pipelineGraph: (name: string) => request<{ mermaid: string }>(`/api/pipelines/${encodeURIComponent(name)}/graph`),
  createRun: (pipeline: string, input: string) =>
    request<{ run_id: string }>('/api/runs', { method: 'POST', body: JSON.stringify({ pipeline, input }) }),
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
    // total/limit/offset are optional here only so pre-existing test mocks
    // that predate pagination don't need updating -- the real backend always
    // sends them (see GET /api/runs in main.py).
    return request<{ runs: RunListItem[]; total?: number; limit?: number; offset?: number }>(
      `/api/runs${qs ? `?${qs}` : ''}`,
    )
  },
  getRunTrace: (id: string) =>
    // usage and content_purged_at are optional for the same reason -- always
    // present in the real response, but pre-existing test mocks predate them.
    request<{
      events: import('./types').TraceEvent[]
      usage?: UsageRecord[]
      content_purged_at?: string | null
    }>(`/api/runs/${id}/trace`),

  // Admin: cross-org pipeline-run analytics (Trace page).
  listPipelineAnalytics: (filters: Record<string, string | number | undefined | null> = {}) => {
    const params = new URLSearchParams(
      Object.fromEntries(
        Object.entries(filters)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ),
    )
    const qs = params.toString()
    return request<{ pipelines: PipelineAnalyticsSummary[] }>(`/api/admin/analytics/pipelines${qs ? `?${qs}` : ''}`)
  },
  listModelAnalytics: (filters: Record<string, string | number | undefined | null> = {}) => {
    const params = new URLSearchParams(
      Object.fromEntries(
        Object.entries(filters)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ),
    )
    const qs = params.toString()
    return request<{ models: ModelAnalyticsSummary[] }>(`/api/admin/analytics/models${qs ? `?${qs}` : ''}`)
  },
  getPipelineAnalytics: (name: string, filters: Record<string, string | number | undefined | null> = {}) => {
    const params = new URLSearchParams(
      Object.fromEntries(
        Object.entries(filters)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ),
    )
    const qs = params.toString()
    return request<PipelineAnalyticsDetail>(`/api/admin/analytics/pipelines/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`)
  },

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
    uploadFiles<{ name: string; job_id: number; status: string }>(
      `/api/config/knowledge_bases/${encodeURIComponent(name)}/upload${orgQuery(org)}`,
      files,
    ),
  // Ingestion now runs in the background -- AdvancedPage polls this after
  // uploadKnowledgeBaseFiles until status is 'completed'/'failed' (the
  // admin/`?org=` counterpart to orgKnowledgeBaseUploadJob below).
  knowledgeBaseUploadJob: (name: string, jobId: number, org?: string) =>
    request<IngestionJobStatus>(
      `/api/config/knowledge_bases/${encodeURIComponent(name)}/ingestion-jobs/${jobId}${orgQuery(org)}`,
    ),

  // Org self-service: build your own knowledge base by uploading documents
  // (the wizard's "Your documents" step). Org resolves server-side from the
  // token, unlike the admin uploadKnowledgeBaseFiles above. `smartSearch`
  // is the "Standard"/"Enhanced" toggle -- only meaningful when
  // orgKnowledgeBaseCapabilities().smart_search_available is true.
  // `description` is the customer's one sentence about what the documents
  // cover; it becomes the agent tool's own description. Left undefined, the
  // multipart helper omits the field entirely.
  uploadOwnKnowledgeBaseFiles: (
    name: string,
    files: File[],
    replace = false,
    smartSearch = false,
    description?: string,
  ) =>
    uploadFiles<{ name: string; job_id: number; status: string }>(
      `/api/org/knowledge-bases/${encodeURIComponent(name)}/upload`,
      files,
      { replace, smart_search: smartSearch, description },
    ),
  orgKnowledgeBaseCapabilities: () => request<KnowledgeBaseCapabilities>('/api/org/knowledge-bases/capabilities'),
  // Ingestion now runs in the background -- DocumentsPage polls this after
  // uploadOwnKnowledgeBaseFiles until status is 'completed'/'failed'.
  orgKnowledgeBaseUploadJob: (name: string, jobId: number) =>
    request<IngestionJobStatus>(
      `/api/org/knowledge-bases/${encodeURIComponent(name)}/ingestion-jobs/${jobId}`,
    ),
  // The org's own knowledge bases, for the "My documents" panel on My teams.
  // Deleting is refused (409) while an upload is still processing or while a
  // live team still uses the collection.
  listOwnKnowledgeBases: () => request<OrgKnowledgeBase[]>('/api/org/knowledge-bases'),
  getOwnKnowledgeBase: (name: string) =>
    request<OrgKnowledgeBase>(`/api/org/knowledge-bases/${encodeURIComponent(name)}`),
  deleteOwnKnowledgeBase: (name: string) =>
    request<void>(`/api/org/knowledge-bases/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  // "Try a search": one query against the org's own collection, returning the
  // passages an agent would have retrieved. A collection that can't answer
  // yet (still processing, last upload failed, or never uploaded through the
  // app) is a 409 whose detail says which.
  searchOwnKnowledgeBase: (name: string, query: string, topK = 5) =>
    request<KnowledgeBaseSearchResponse>(
      `/api/org/knowledge-bases/${encodeURIComponent(name)}/search`,
      { method: 'POST', body: JSON.stringify({ query, top_k: topK }) },
    ),

  // Org self-service settings: the org's mailbox for the email tools.
  getOrgEmail: () => request<OrgEmailStatus>('/api/org/email'),
  setOrgEmail: (payload: OrgEmailConnectPayload) =>
    request<OrgEmailStatus>('/api/org/email', { method: 'PUT', body: JSON.stringify(payload) }),
  testOrgEmail: (payload: OrgEmailConnectPayload) =>
    request<{ ok: boolean; error?: string }>('/api/org/email/test', { method: 'POST', body: JSON.stringify(payload) }),
  clearOrgEmail: () => request<void>('/api/org/email', { method: 'DELETE' }),

  // Alerting: the in-app list, and where else the org wants it delivered.
  // `limit` is for callers that want only the unread *count* (the Activity
  // page's tab badge) -- the response carries it whatever the page size.
  listNotifications: (unreadOnly = false, limit?: number) =>
    request<NotificationList>(
      `/api/notifications?unread_only=${unreadOnly}${limit ? `&limit=${limit}` : ''}`,
    ),
  markNotificationRead: (id: number) =>
    request<{ ok: boolean; unread: number }>(`/api/notifications/${id}/read`, { method: 'POST' }),
  getNotificationSettings: () => request<NotificationSettings>('/api/org/notifications'),
  setNotificationSettings: (payload: NotificationSettingsPayload) =>
    request<NotificationSettings>('/api/org/notifications', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  // Run-history retention (Phase 3b): how long the org keeps run content,
  // taking a copy out before it goes, and removing it on demand. A cleanup
  // clears content and keeps accounting -- see ui/backend/retention.py.
  getRetention: () => request<RetentionSettings>('/api/org/retention'),
  // null turns the policy off (keep forever). Saving removes nothing by
  // itself -- the sweep does, on its next cycle.
  setRetention: (days: number | null) =>
    request<RetentionSettings>('/api/org/retention', {
      method: 'PUT',
      body: JSON.stringify({ run_retention_days: days }),
    }),
  // The window is always explicit, never defaulted from the stored policy:
  // this is a destructive action and the request must say what it removes.
  purgeRuns: (olderThanDays: number) =>
    request<{ purged: number }>('/api/org/retention/purge', {
      method: 'POST',
      body: JSON.stringify({ older_than_days: olderThanDays }),
    }),
  purgeRun: (runId: string) =>
    request<{ purged: boolean }>(`/api/runs/${runId}/purge`, { method: 'POST' }),
  // Fetches the bundle and hands it to the browser as a file. This is the
  // real app, not a sandboxed artifact, so an object URL + <a download>
  // works. Returns the bundle so the caller can report its size and whether
  // the server's cap truncated it.
  exportOrgData: async (days?: number | null): Promise<OrgExportBundle> => {
    const bundle = await request<OrgExportBundle>(
      `/api/org/export${days == null ? '' : `?days=${days}`}`,
    )
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' }),
    )
    const link = document.createElement('a')
    link.href = url
    link.download = `bestteam-export-${bundle.exported_at.slice(0, 10)}.json`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    return bundle
  },

  // Org self-service: share a deployed team with colleagues via a
  // revocable, anonymous link (see docs/superpowers/specs/
  // 2026-08-14-team-sharing-continuous-chat-design.md).
  createShareLink: (pipelineId: number, payload: { daily_cap?: number; expires_at?: string | null }) =>
    request<ShareLink>(`/api/pipelines/${pipelineId}/share-links`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listShareLinks: (pipelineId: number) =>
    request<ShareLink[]>(`/api/pipelines/${pipelineId}/share-links`),
  patchShareLink: (
    linkId: number,
    payload: { active?: boolean; daily_cap?: number; expires_at?: string | null; clear_expiry?: boolean },
  ) =>
    request<ShareLink>(`/api/share-links/${linkId}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  listShareSessions: (linkId: number) =>
    request<ShareSessionSummary[]>(`/api/share-links/${linkId}/sessions`),
  getShareSessionMessages: (linkId: number, sessionId: number) =>
    request<ShareMessage[]>(`/api/share-links/${linkId}/sessions/${sessionId}/messages`),

  // Autonomous email trigger: org-level "run on new mail" opt-in + activity.
  getEmailTrigger: () => request<EmailTrigger>('/api/org/email-trigger'),
  setEmailTrigger: (payload: { pipeline_name: string; enabled: boolean }) =>
    request<EmailTrigger>('/api/org/email-trigger', { method: 'PUT', body: JSON.stringify(payload) }),
  emailTriggerActivity: () => request<unknown>('/api/org/email-trigger/activity'),

  // Pre-LLM mail filter and per-org automation budgets (Phase 4a).
  getEmailFilter: () => request<EmailFilterSettings>('/api/org/email-filter'),
  setEmailFilter: (payload: EmailFilterSettings) =>
    request<EmailFilterSettings>('/api/org/email-filter', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  getEmailBudget: () => request<EmailBudget>('/api/org/email-budget'),
  // A cap of null is "no cap"; 0 would be a cap of zero -- automation off.
  setEmailBudget: (payload: EmailBudgetInput) =>
    request<EmailBudget>('/api/org/email-budget', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  // Mail the filter skipped, newest first, and handing one back for the next
  // poll cycle -- a rule-based filter has false positives, and the cost of one
  // has to be a click, not a lost enquiry.
  listFilteredMessages: (limit?: number) =>
    request<{ filtered: FilteredMessage[] }>(
      `/api/org/email-trigger/filtered${limit ? `?limit=${limit}` : ''}`,
    ),
  releaseFilteredMessage: (id: number) =>
    request<{ released: boolean }>(`/api/org/email-trigger/filtered/${id}/release`, {
      method: 'POST',
    }),

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
  submitSpecification: (id: string, payload: { model: string; feedback?: string }) =>
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
