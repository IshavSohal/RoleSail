import type { Job, JobBucket, TailoringStatus } from '../types'

export const buckets: Array<{ key: JobBucket; label: string }> = [
  { key: 'jobs', label: 'Jobs' },
  { key: 'tailored', label: 'Tailored' },
  { key: 'needs_drafts', label: 'Needs drafts' },
  { key: 'drafts_done', label: 'Drafts done / legacy' },
]

export const detailTabs = ['details', 'match', 'resume', 'report', 'outreach'] as const
export type DetailTab = typeof detailTabs[number]

export function matchesBucket(job: Job, bucket: JobBucket): boolean {
  return bucket === 'needs_drafts' || bucket === 'drafts_done'
    ? job.applied_tab === bucket
    : !job.applied && (bucket === 'tailored' ? job.has_tailored : !job.has_tailored)
}

export function matchesScore(job: Job, score: string): boolean {
  return score === 'all' || job.score === Number(score)
}

export function validBucket(value: string | null): JobBucket {
  return buckets.some((bucket) => bucket.key === value) ? value as JobBucket : 'jobs'
}

export function validTab(value: string | null): DetailTab {
  return detailTabs.includes(value as DetailTab) ? value as DetailTab : 'details'
}

export function tailoringActive(status?: TailoringStatus): boolean {
  return status?.status === 'running' || Boolean(status?.queued.length)
}

export function getInboxWidth(): number {
  const current = localStorage.getItem('rolesailJobInboxWidth')
  const legacy = localStorage.getItem('applypilotJobInboxWidth')
  if (!current && legacy) localStorage.setItem('rolesailJobInboxWidth', legacy)
  const saved = current ?? legacy
  if (saved === null) return 340
  const value = Number(saved)
  return Number.isFinite(value) ? Math.max(260, Math.min(600, value)) : 340
}
