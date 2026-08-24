export interface Me {
  username: string
  is_admin: boolean
  org: string | null
}

export interface ModelCatalogEntry {
  spec: string
  display_name: string
  // Set by an admin to mark this entry as the platform's default (e.g. the
  // model the wizard runs the Solution Architect on) -- see `pickDefaultModel`.
  is_default?: boolean
}

// Whether the wizard's "smart search" toggle (DocumentsPage) has anything to
// turn on -- an operator opt-in (env-configured default embedding model),
// not a customer-facing choice.
export interface KnowledgeBaseCapabilities {
  smart_search_available: boolean
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
  pipeline?: { steps: string[] }
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
  pipeline_id?: number | null
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
  // A stage page raises this while a long request is in flight so the shared
  // chrome can stop offering step links (see WizardProgress's `busy`).
  setNavBusy: (busy: boolean) => void
}

export interface TraceEvent {
  type: string
  agent?: string
  data?: Record<string, unknown> | string | null
}

export interface RunListItem {
  id: string
  pipeline: string
  team_display_name?: string | null
  status: string
  autonomous: boolean
  started_at: string
  // Only meaningful for a cross-org admin listing (Trace page) -- absent/null
  // for the customer-facing Activity page, which is always one org's own runs.
  org?: string | null
  org_id?: number | null
  // Set when this run is an admin's diagnostic re-run of another run (Trace
  // page only -- the customer's Activity page never lists one).
  diagnostic_of_run_id?: string | null
  // Diagnostic runs only: the team was redeployed between the original run
  // and this re-run (derived server-side from the two pinned versions).
  version_changed?: boolean | null
}

export interface DiagnoseRunResult {
  run_id: string
  diagnostic_of_run_id: string
  // The team was redeployed since the original run: the diagnostic run
  // exercises the current version, which may no longer reproduce the problem.
  version_changed: boolean
}

export interface UsageRecord {
  agent: string | null
  model: string | null
  input_tokens: number
  output_tokens: number
  cost_estimate: number | null
}

export interface PipelineAnalyticsSummary {
  org_id: number | null
  org: string | null
  pipeline: string
  total_runs: number
  completed: number
  failed: number
  cancelled: number
  running: number
  success_rate: number | null
  avg_duration_seconds: number | null
  total_input_tokens: number
  total_output_tokens: number
  total_cost_estimate: number | null
}

export interface AgentAnalytics {
  agent: string
  run_count: number
  avg_input_tokens: number | null
  avg_output_tokens: number | null
  avg_cost_estimate: number | null
  avg_duration_seconds: number | null
}

export interface ModelAnalytics {
  model: string
  run_count: number
  avg_input_tokens: number | null
  avg_output_tokens: number | null
  avg_cost_estimate: number | null
}

export interface FailurePoint {
  agent: string | null
  event_type: string
  count: number
  pct_of_failures: number
}

export interface PipelineAnalyticsDetail {
  org_id: number | null
  pipeline: string
  per_agent: AgentAnalytics[]
  per_model: ModelAnalytics[]
  common_failure_points: FailurePoint[]
}

export interface ModelAnalyticsSummary {
  model: string
  run_count: number
  total_input_tokens: number
  total_output_tokens: number
  total_cost_estimate: number | null
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
  pipeline_name: string | null
  status: 'active' | 'off' | 'disabled' | 'paused_cap' | 'error'
  daily_cap: number
  last_error?: string | null
  last_checked_at?: string | null
}

export type OrgEmailAuthType = 'password' | 'microsoft_oauth'

export interface OrgEmailStatus {
  connected: boolean
  host?: string
  username?: string
  port?: number
  drafts?: string | null
  auth_type?: OrgEmailAuthType
  oauth_tenant_id?: string | null
  oauth_client_id?: string | null
  // Admin-entered, Microsoft 365 only. ISO date (YYYY-MM-DD) or null.
  oauth_secret_expires_at?: string | null
}

// The mailbox connect/test body. `password` and the three OAuth fields are
// mutually exclusive; the backend rejects a mix.
export interface OrgEmailConnectPayload {
  auth_type: OrgEmailAuthType
  host: string
  username: string
  password: string | null
  client_secret: string | null
  oauth_tenant_id: string | null
  oauth_client_id: string | null
  oauth_secret_expires_at: string | null
  port: number
  drafts: string | null
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

// The wizard's DocumentsPage polls this after uploadOwnKnowledgeBaseFiles --
// the upload endpoint now queues ingestion asynchronously and returns
// immediately with a job id. `config` is only populated once `status ==
// 'completed'`.
export interface IngestionJobStatus {
  job_id: number
  status: 'queued' | 'running' | 'completed' | 'failed'
  file_count: number
  documents_succeeded: number
  documents_failed: number
  chunk_count: number
  errors: { filename: string | null; error: string }[]
  config: ConfigItem | null
}

// One of the org's own knowledge bases, as the customer-facing "My
// documents" panel sees it (GET /api/org/knowledge-bases). `latest_job` is
// the newest ingestion attempt of any status, with `config` deliberately
// stripped server-side -- it carries the server's absolute upload path.
export interface OrgKnowledgeBase {
  name: string
  description: string | null
  // The *serving* generation: the type of the latest completed ingestion job,
  // falling back to the requested config when none has completed yet -- not
  // the pending config.
  type: string
  updated_at: string
  used_by: string[]
  // Whether the knowledge base can answer questions right now: a failed
  // re-upload leaves the previous generation live, so this is not simply
  // "the latest job succeeded".
  servable: boolean
  latest_job: Omit<IngestionJobStatus, 'config'> | null
  // The live generation's documents, by name. Empty until a first upload
  // completes. A `failed` one could not be read but is still in the
  // collection's files, so it can be removed like any other.
  documents: OrgKnowledgeBaseDocument[]
}

export interface OrgKnowledgeBaseDocument {
  filename: string
  status: 'chunked' | 'failed' | string
  size_bytes: number
}

// One passage returned by the "Try a search" panel
// (POST /api/org/knowledge-bases/{name}/search). `citation` is the same label
// an agent's own tool output cites, so the panel and the model name the same
// passage; `text` is capped server-side at 1500 characters -- this is a spot
// check on retrieval, not a document reader.
export interface KnowledgeBaseSearchResult {
  citation: string
  source: string
  page: number | null
  heading: string | null
  text: string
  // Identity and scores, the same ones a run's KB trace event carries. Not
  // rendered by the panel today; here so the type matches the wire shape.
  chunk_id: number | null
  document_id: number | null
  fused_score: number
  leg_scores: Record<string, number>
  rerank_score: number | null
}

export interface KnowledgeBaseSearchResponse {
  query: string
  hit_count: number
  // The completed ingestion job the collection was built from; null for a
  // collection that was not uploaded through the app.
  ingestion_job_id: number | null
  results: KnowledgeBaseSearchResult[]
}

export interface ShareLink {
  id: number
  pipeline_id: number
  token: string
  active: boolean
  daily_cap: number
  expires_at: string | null
  created_at: string
}

export interface ShareSessionSummary {
  id: number
  created_at: string
  last_active_at: string
  turns_today: number
}

export interface ShareMessage {
  role: 'user' | 'assistant'
  content: string
  turn_number: number
  created_at?: string
}

// GET /api/share/{token}/team. `steps` is null when no honest denominator
// exists (a hierarchical team emits one completion however many subordinates
// its manager delegates to) -- the page shows a pulse instead of a count.
export interface ShareTeamInfo {
  name: string
  steps: number | null
}

// --- notifications ----------------------------------------------------------

export type NotificationSeverity = 'error' | 'warning' | 'info'

// "skipped" means no webhook is configured -- in-app only, not a failure.
export type NotificationDeliveryState = 'pending' | 'delivered' | 'failed' | 'skipped'

export interface AppNotification {
  id: number
  kind: string
  severity: NotificationSeverity
  title: string
  body: string
  fingerprint: string
  created_at: string | null
  read: boolean
  delivery_state: NotificationDeliveryState
}

export interface NotificationList {
  notifications: AppNotification[]
  unread: number
}

// The webhook secret is write-only: the API reports only whether one is
// stored, so the UI can never echo it back.
export interface NotificationSettings {
  webhook_url: string | null
  has_webhook_secret: boolean
  enabled: boolean
}

export interface NotificationSettingsPayload {
  webhook_url: string | null
  // Omitted entirely to keep the stored secret; '' clears it.
  webhook_secret?: string | null
  enabled: boolean
}

// --- run-history retention and export (Phase 3b) ----------------------------

// `run_retention_days` null means keep forever -- the default, so nothing is
// ever removed until the customer chooses a period. `purgeable_now` is what
// the SAVED policy would remove on the next cleanup, not a preview of an
// unsaved selection (the API computes it from the stored value).
export interface RetentionSettings {
  run_retention_days: number | null
  last_swept_at: string | null
  last_purged_count: number
  purgeable_now: number
}

// Everything a cleanup would remove. `truncated` is explicit: a partial
// export that looked complete would be worse than no export at all.
export interface OrgExportBundle {
  org_id: number
  exported_at: string
  truncated: boolean
  oldest_included: string | null
  runs: unknown[]
}

// --- pre-LLM mail filter and automation budgets (Phase 4a) ------------------

// Every pattern is a literal, never a regular expression: a sender entry is a
// full address or `*@domain`, a subject entry is a word or phrase the subject
// must contain. See ui/backend/email_filter.py.
export interface EmailFilterSettings {
  skip_bulk: boolean
  sender_blocklist: string[]
  sender_allowlist: string[]
  subject_blocklist: string[]
}

// null means no cap -- the default. It is NOT the same as 0, which would be a
// cap of zero, i.e. automation switched off.
export interface EmailBudgetInput {
  daily_message_cap: number | null
  monthly_cost_cap: number | null
}

// `spent_this_month` is null when nothing this month could be priced at all --
// not zero, and it must never be shown as a measured $0.00.
// `unpriced_models`/`unpriced_runs_this_month` are the spend cap's blind spot:
// a model with no catalogue price contributes nothing to the total, so the
// figure is a floor rather than the full amount. Advisory, never an error.
export interface EmailBudget extends EmailBudgetInput {
  messages_today: number
  spent_this_month: number | null
  unpriced_runs_this_month: number
  unpriced_models: string[]
}

export interface DailyRunCount {
  date: string
  count: number
}

export interface TeamCount {
  pipeline: string
  count: number
  // The team has since been deleted. Its work is still counted -- marked,
  // not hidden, so the rows keep adding up to completed_count.
  deleted: boolean
}

// The customer-facing Activity Overview tab: how often this org's teams ran,
// and how much they completed. Deliberately no model name or cost anywhere
// here -- see ui/backend/activity_overview.py.
export interface ActivityOverview {
  sessions: number
  active_days: number
  current_streak: number
  longest_streak: number
  peak_hour: number | null
  daily_counts: DailyRunCount[]
  completed_count: number
  team_counts: TeamCount[]
}

// One message the filter skipped before any model read it. `reason` is the
// sentence to show; `decision` is the raw rule code, for debugging only.
export interface FilteredMessage {
  id: number
  external_id: string
  decision: string | null
  reason: string
  detected_at: string | null
}
