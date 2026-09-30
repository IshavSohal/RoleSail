import type {
  DashboardSettings,
  DiscoveryStatus,
  JobsResponse,
  OutreachResponse,
  PipelineStatus,
  PricingSettings,
  ResumePayload,
  TailoringStatus,
  UsageSummary,
} from './types'

export class ApiRequestError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const contentType = response.headers.get('content-type') ?? ''
  const payload = contentType.includes('application/json')
    ? await response.json()
    : await response.text()
  if (!response.ok) {
    const message = typeof payload === 'object' && payload && 'error' in payload
      ? String(payload.error)
      : `Request failed (${response.status})`
    throw new ApiRequestError(message, response.status)
  }
  return payload as T
}

function json(method: 'POST' | 'PUT', body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export const api = {
  jobs: () => request<JobsResponse>('/api/jobs'),
  importJob: (url: string) => request<Record<string, unknown>>('/api/jobs', json('POST', { url })),
  importStatus: (url: string) => request<Record<string, unknown>>(`/api/jobs/status?url=${encodeURIComponent(url)}`),
  markApplied: (url: string, applied: boolean) => request<Record<string, unknown>>('/api/jobs/applied', json('POST', { url, applied })),
  deleteJob: (url: string) => request<Record<string, unknown>>('/api/jobs/delete', json('POST', { url })),
  clearTailored: (url: string) => request<Record<string, unknown>>('/api/jobs/tailored/clear', json('POST', { url })),
  startDiscovery: () => request<DiscoveryStatus>('/api/discovery', json('POST', { workers: 3 })),
  discoveryStatus: () => request<DiscoveryStatus>('/api/discovery/status'),
  startPipeline: () => request<{ id: string }>('/api/pipeline', json('POST', { workers: 3 })),
  pipelineStatus: (runId?: string | null) => request<PipelineStatus>(`/api/pipeline/status${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`),
  startBulkTailoring: () => request<Record<string, unknown>>('/api/tailoring', json('POST', { min_score: 7, limit: 20, validation_mode: 'normal' })),
  startJobTailoring: (url: string, replaceExisting: boolean) => request<Record<string, unknown>>('/api/tailoring/job', json('POST', { url, validation_mode: 'normal', replace_existing: replaceExisting })),
  cancelTailoring: (url: string) => request<Record<string, unknown>>('/api/tailoring/cancel', json('POST', { url })),
  tailoringStatus: () => request<TailoringStatus>('/api/tailoring/status'),
  settings: () => request<DashboardSettings>('/api/settings'),
  saveProfile: (profile: Record<string, unknown>) => request<Record<string, unknown>>('/api/settings/profile', json('PUT', { profile })),
  saveSearches: (searches: Record<string, unknown>) => request<Record<string, unknown>>('/api/settings/searches', json('PUT', { searches })),
  resume: (format: 'txt' | 'tex') => request<ResumePayload>(`/api/resume?format=${format}`),
  saveResume: (filename: string, content: string, removeComments: boolean) => request<ResumePayload>('/api/resume', json('PUT', { filename, content, remove_comments: removeComments })),
  outreach: (jobUrl: string) => request<OutreachResponse>(`/api/outreach?job_url=${encodeURIComponent(jobUrl)}`),
  outreachAction: (action: string, body: unknown) => request<OutreachResponse | { batch: OutreachResponse['batch'] }>(`/api/outreach/${action}`, json('POST', body)),
  usage: () => request<UsageSummary>('/api/usage/summary'),
  pricing: () => request<PricingSettings>('/api/settings/pricing'),
  savePricing: (overrides: Record<string, unknown>) => request<PricingSettings>('/api/settings/pricing', json('PUT', { overrides })),
  artifactReport: (jobUrl: string) => request<Record<string, unknown>>(`/api/jobs/artifact?url=${encodeURIComponent(jobUrl)}&kind=report`),
}

export function artifactUrl(jobUrl: string, kind: 'pdf' | 'tex' | 'report'): string {
  return `/api/jobs/artifact?url=${encodeURIComponent(jobUrl)}&kind=${kind}`
}

export function logoUrl(jobUrl: string): string {
  return `/api/jobs/company-logo?url=${encodeURIComponent(jobUrl)}`
}
