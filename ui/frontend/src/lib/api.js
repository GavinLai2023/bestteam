export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'
export const WS_BASE = import.meta.env.VITE_WS_BASE ?? 'ws://127.0.0.1:8000'

export const TOKEN_KEY = 'bestteam_token'

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

  // Monitoring
  listWorkflows: () => request('/api/workflows'),
  workflowGraph: (name) => request(`/api/workflows/${encodeURIComponent(name)}/graph`),
  createRun: (workflow, input) =>
    request('/api/runs', { method: 'POST', body: JSON.stringify({ workflow, input }) }),
  getRun: (id) => request(`/api/runs/${id}`),

  // Model catalog
  modelCatalog: () => request('/api/config/model-catalog'),

  // Advanced config (CRUD)
  listConfig: (kind) => request(`/api/config/${kind}`),
  getConfigItem: (kind, name) => request(`/api/config/${kind}/${encodeURIComponent(name)}`),
  putConfigItem: (kind, name, payload) =>
    request(`/api/config/${kind}/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteConfigItem: (kind, name) =>
    request(`/api/config/${kind}/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  uploadKnowledgeBaseFiles: (name, files, options = {}) =>
    uploadFiles(`/api/config/knowledge_bases/${encodeURIComponent(name)}/upload`, files, options),

  // Builder wizard sessions
  createSession: (intent_text, as_is_text) =>
    request('/api/builder/sessions', { method: 'POST', body: JSON.stringify({ intent_text, as_is_text }) }),
  getSession: (id) => request(`/api/builder/sessions/${id}`),
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
