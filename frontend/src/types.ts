export type AppliedBucket = 'active' | 'needs_drafts' | 'drafts_done'
export type JobBucket = 'jobs' | 'tailored' | 'needs_drafts' | 'drafts_done'

export interface OutreachSummary {
  batch_id: string
  status: string
  ready: number
  sent: number
  failed: number
  review_required: boolean
}

export interface Job {
  url: string
  title: string
  company: string
  company_logo: string
  location: string
  site: string
  strategy: string
  salary: string
  posted_at: string
  posted_label: string
  score: number | null
  reasoning: string
  description: string
  detail_error: string
  application_url: string
  applied: boolean
  applied_at: string
  applied_label: string
  applied_tab: AppliedBucket
  status: string
  priority: boolean
  has_tailored: boolean
  has_pdf: boolean
  has_tex: boolean
  has_report: boolean
  can_tailor: boolean
  can_retailor: boolean
  tailor_attempts: number
  outreach_summary: OutreachSummary | null
}

export interface JobsResponse { jobs: Job[] }
export interface ApiError { error: string }

export interface QueueRequest {
  id: string
  kind: 'batch' | 'job'
  target_url?: string | null
  status: string
  queue_position?: number
  result?: Record<string, unknown> | null
  error?: string | null
  cancel_requested?: boolean
}

export interface TailoringStatus {
  status: 'idle' | 'running'
  current: QueueRequest | null
  queued: QueueRequest[]
  recent: QueueRequest[]
}

export interface DiscoveryStatus {
  status: 'idle' | 'running' | 'complete' | 'error'
  started_at?: string | null
  finished_at?: string | null
  result?: Record<string, unknown> | null
  error?: string | null
}

export interface PipelineRun {
  id: string
  status: string
  current_stage?: string | null
  error_summary?: string | null
  usage?: { cost_usd?: number; requests?: number }
}

export interface PipelineStatus { run: PipelineRun | null }

export interface DashboardSettings {
  profile: Record<string, unknown>
  searches: Record<string, unknown>
  profile_path?: string
  searches_path?: string
  password_configured?: boolean
}

export interface ResumePayload {
  filename?: string
  content: string
  exists?: boolean
  updated_at?: string | null
  pdf_available?: boolean
  compiled_at?: string | null
}

export interface OutreachRecipient {
  id: string
  first_name?: string
  last_name?: string
  title?: string
  linkedin_url?: string
  email?: string
  email_status?: string
  relevance_reason?: string
  subject?: string
  body_text?: string
  status: string
  error?: string | null
  scheduled_for?: string | null
  gmail_draft_id?: string | null
  gmail_account_email?: string | null
}

export interface OutreachBatch {
  id: string
  status: string
  company_domain?: string | null
  error?: string | null
  recipients: OutreachRecipient[]
}

export interface OutreachResponse {
  batch: OutreachBatch
  gmail_account?: string | null
}

export interface UsageValue {
  cost_usd: number
  requests: number
  input_tokens?: number
  output_tokens?: number
  reported_requests?: number
  estimated_requests?: number
  unavailable_requests?: number
}

export interface UsageSummary {
  today: UsageValue
  month?: UsageValue
  all_time: UsageValue
  by_stage: Array<UsageValue & { name: string }>
  by_provider: Array<UsageValue & { name: string }>
  by_model: Array<UsageValue & { name: string }>
  pricing_version?: string
}

export interface PricingSettings {
  overrides: Record<string, unknown>
  [key: string]: unknown
}
