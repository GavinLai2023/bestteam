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
  workflow_id?: number | null
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
  team_display_name?: string | null
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
