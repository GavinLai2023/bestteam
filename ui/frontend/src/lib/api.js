export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'
export const WS_BASE = import.meta.env.VITE_WS_BASE ?? 'ws://127.0.0.1:8000'

export const TOKEN_KEY = 'bestteam_token'

// `?org=<name>`, or '' when no org applies (the skills built-in tier, the
// org-less model catalog). Omitting it on an org-scoped item route is a 422.
function orgQuery(org) {
  return org ? `?${new URLSearchParams({ org })}` : ''
}

async function request(path, options = {}) {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
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
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      // response had no JSON body
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }

  if (res.status === 204) return null
  return res.json()
}

async function uploadSingleFile(path, file, fields = {}) {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers = {}
  if (token) headers['Authorization'] = `Bearer ${token}`

  const formData = new FormData()
  formData.append('file', file)
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null) formData.append(key, value)
  }

  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', headers, body: formData })

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.assign('/login')
    throw new Error('Not authenticated')
  }

  if (!res.ok) {
    let detail = res.statusText
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

async function uploadFiles(path, files, fields = {}) {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers = {}
  if (token) headers['Authorization'] = `Bearer ${token}`

  const formData = new FormData()
  for (const file of files) formData.append('files', file)
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null) formData.append(key, value)
  }

  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', headers, body: formData })

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.assign('/login')
    throw new Error('Not authenticated')
  }

  if (!res.ok) {
    let detail = res.statusText
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
  login: (username, password) =>
    request('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  me: () => request('/api/auth/me'),

  // Admin: per-user memory management
  memoryUsers: () => request('/api/memory/users'),
  memoryRecords: (userId, { query, type } = {}) => {
    const params = new URLSearchParams()
    if (query) params.set('query', query)
    if (type) params.set('type', type)
    const qs = params.toString()
    return request(`/api/memory/users/${encodeURIComponent(userId)}/records${qs ? `?${qs}` : ''}`)
  },
  deleteMemoryRecord: (id) => request(`/api/memory/records/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  clearUserMemory: (userId) => request(`/api/memory/users/${encodeURIComponent(userId)}`, { method: 'DELETE' }),

  // Monitoring
  listWorkflows: () => request('/api/workflows'),
  workflowGraph: (name) => request(`/api/workflows/${encodeURIComponent(name)}/graph`),
  createRun: (workflow, input) =>
    request('/api/runs', { method: 'POST', body: JSON.stringify({ workflow, input }) }),
  getRun: (id) => request(`/api/runs/${id}`),
  // Short-lived, single-use ticket for authenticating the stream WebSocket
  // (CR-013) -- only the ticket goes in the ws URL, never the bearer token.
  createWsTicket: () => request('/api/runs/ws-ticket', { method: 'POST' }),

  // Model catalog
  modelCatalog: () => request('/api/model-catalog'),

  // Advanced config (CRUD). `org` is the organization an item belongs to; the
  // backend requires it on every item route except skills (where omitting it
  // means the platform built-in tier) and the org-less model catalog.
  listOrgs: () => request('/api/config/orgs'),
  listConfig: (kind, org) => request(`/api/config/${kind}${orgQuery(org)}`),
  getConfigItem: (kind, name, org) =>
    request(`/api/config/${kind}/${encodeURIComponent(name)}${orgQuery(org)}`),
  putConfigItem: (kind, name, payload, org) =>
    request(`/api/config/${kind}/${encodeURIComponent(name)}${orgQuery(org)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteConfigItem: (kind, name, org) =>
    request(`/api/config/${kind}/${encodeURIComponent(name)}${orgQuery(org)}`, { method: 'DELETE' }),
  uploadKnowledgeBaseFiles: (name, files, org) =>
    uploadFiles(`/api/config/knowledge_bases/${encodeURIComponent(name)}/upload${orgQuery(org)}`, files),

  // Org self-service settings: the org's mailbox for the email tools.
  getOrgEmail: () => request('/api/org/email'),
  setOrgEmail: (payload) => request('/api/org/email', { method: 'PUT', body: JSON.stringify(payload) }),
  testOrgEmail: (payload) =>
    request('/api/org/email/test', { method: 'POST', body: JSON.stringify(payload) }),
  clearOrgEmail: () => request('/api/org/email', { method: 'DELETE' }),

  // Interview recording transcription
  transcribeInterview: (file, model) =>
    uploadSingleFile('/api/builder/interview/transcribe', file, { model }),

  // Builder wizard sessions
  createSession: (intent_text, as_is_text) =>
    request('/api/builder/sessions', { method: 'POST', body: JSON.stringify({ intent_text, as_is_text }) }),
  getSession: (id) => request(`/api/builder/sessions/${id}`),
  listSessions: () => request('/api/builder/sessions'),
  submitRequirements: (id, payload) =>
    request(`/api/builder/sessions/${id}/requirements`, { method: 'POST', body: JSON.stringify(payload) }),
  submitSpecification: (id, payload) =>
    request(`/api/builder/sessions/${id}/specification`, { method: 'POST', body: JSON.stringify(payload) }),
  submitSolution: (id, payload) =>
    request(`/api/builder/sessions/${id}/solution`, { method: 'POST', body: JSON.stringify(payload) }),
  createTestRun: (id, input) =>
    request(`/api/builder/sessions/${id}/test-runs`, { method: 'POST', body: JSON.stringify({ input }) }),
  deploySession: (id) => request(`/api/builder/sessions/${id}/deploy`, { method: 'POST' }),
}
