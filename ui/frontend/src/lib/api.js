export const API_BASE = 'http://127.0.0.1:8000'
export const WS_BASE = 'ws://127.0.0.1:8000'

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })

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

export const api = {
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
