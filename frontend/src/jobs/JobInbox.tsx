import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, logoUrl } from '../api'
import type { Job, JobBucket } from '../types'
import { buckets } from './jobViewState'

export function JobInbox(props: {
  jobs: Job[]
  allJobs: Job[]
  selected: Job | null
  bucket: JobBucket
  counts: Record<JobBucket, number>
  score: string
  search: string
  companies: Array<[string, number]>
  selectedCompanies: string[]
  onBucket: (value: JobBucket) => void
  onScore: (value: string) => void
  onSearch: (value: string) => void
  onSelect: (job: Job) => void
  onCompany: (company: string, checked: boolean) => void
  onSelectAllCompanies: () => void
  onClearCompanies: () => void
  onStartDiscovery: () => void
  onStartTailoring: () => void
  discoveryBusy: boolean
  tailoringBusy: boolean
}) {
  const [showImport, setShowImport] = useState(false)
  return (
    <aside className="job-inbox">
      <header className="inbox-header">
        <div className="inbox-title"><h1>Job inbox</h1><button className="button compact primary" onClick={() => setShowImport(true)}>+ Add</button></div>
        <input type="search" placeholder="Search jobs" aria-label="Search jobs" value={props.search} onChange={(event) => props.onSearch(event.target.value)} />
        <div className="inbox-filters" aria-label="Job status">
          {buckets.map(({ key, label }) => <button key={key} className={props.bucket === key ? 'active' : ''} onClick={() => props.onBucket(key)}>{label} ({props.counts[key]})</button>)}
        </div>
        <div className="filter-row">
          <select aria-label="Filter jobs by score" value={props.score} onChange={(event) => props.onScore(event.target.value)}>
            <option value="all">Score · All</option>{[10, 9, 8, 7].map((value) => <option value={value} key={value}>Score · {value}</option>)}
          </select>
          <CompanyFilter companies={props.companies} selected={props.selectedCompanies} onChange={props.onCompany} onSelectAll={props.onSelectAllCompanies} onClear={props.onClearCompanies} />
        </div>
        <details className="inbox-actions">
          <summary>Run tools</summary>
          <button className="button" disabled={props.discoveryBusy} onClick={props.onStartDiscovery}>{props.discoveryBusy ? 'Discovering…' : 'Run discovery'}</button>
          <button className="button" disabled={props.tailoringBusy} onClick={props.onStartTailoring}>{props.tailoringBusy ? 'Tailoring…' : 'Tailor eligible jobs'}</button>
        </details>
      </header>
      <div className="job-list" aria-live="polite">
        {props.jobs.map((job) => <JobRow key={job.url} job={job} selected={props.selected?.url === job.url} onClick={() => props.onSelect(job)} />)}
        {!props.jobs.length && <div className="empty-state small">No jobs match this view.</div>}
      </div>
      {showImport && <ImportDialog onClose={() => setShowImport(false)} />}
    </aside>
  )
}

function JobRow({ job, selected, onClick }: { job: Job; selected: boolean; onClick: () => void }) {
  const [logoFailed, setLogoFailed] = useState(false)
  const initials = job.company.split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase()
  return (
    <button className={`job-row ${selected ? 'selected' : ''}`} onClick={onClick} aria-current={selected ? 'true' : undefined}>
      <span className="company-avatar">{!logoFailed && <img src={logoUrl(job.url)} alt="" loading="lazy" onError={() => setLogoFailed(true)} />}{logoFailed && initials}</span>
      <span className="job-row-copy"><strong>{job.title}</strong><small>{job.company}</small><small>{job.posted_label}</small></span>
      <span className="job-row-meta"><strong>{job.score ?? '—'}</strong><small>{job.status}</small></span>
    </button>
  )
}

function CompanyFilter({ companies, selected, onChange, onSelectAll, onClear }: { companies: Array<[string, number]>; selected: string[]; onChange: (company: string, checked: boolean) => void; onSelectAll: () => void; onClear: () => void }) {
  const [filter, setFilter] = useState('')
  const visible = companies.filter(([company]) => company.toLowerCase().includes(filter.toLowerCase()))
  const allSelected = companies.every(([company]) => selected.includes(company))
  return (
    <details className="company-filter">
      <summary>Companies · {allSelected ? 'All' : `${selected.length} selected`}</summary>
      <div className="company-menu">
        <div className="company-menu-head"><strong>Filter companies</strong><div className="company-menu-actions"><button className="text-button" onClick={onSelectAll}>Select all</button><button className="text-button" onClick={onClear}>Clear</button></div></div>
        <input type="search" placeholder="Search companies" aria-label="Search companies" value={filter} onChange={(event) => setFilter(event.target.value)} />
        <div className="company-options">{visible.map(([company, count]) => <label key={company}><input type="checkbox" checked={selected.includes(company)} onChange={(event) => onChange(company, event.target.checked)} /><span>{company}</span><small>{count}</small></label>)}</div>
      </div>
    </details>
  )
}

function ImportDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const [url, setUrl] = useState('')
  const [pendingUrl, setPendingUrl] = useState('')
  const mutation = useMutation({
    mutationFn: api.importJob,
    onSuccess: (result) => {
      setPendingUrl(String(result.url ?? url))
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
  const status = useQuery({
    queryKey: ['import-status', pendingUrl],
    queryFn: () => api.importStatus(pendingUrl),
    enabled: Boolean(pendingUrl),
    refetchInterval: ({ state }) => ['complete', 'error', 'rejected'].includes(String(state.data?.status ?? '')) ? false : 1_000,
  })
  useEffect(() => {
    if (['complete', 'error', 'rejected'].includes(String(status.data?.status ?? ''))) void queryClient.invalidateQueries({ queryKey: ['jobs'] })
  }, [queryClient, status.data?.status])
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="import-title">
        <div className="dialog-head"><h2 id="import-title">Add a job</h2><button className="icon-button" onClick={onClose} aria-label="Close">×</button></div>
        <p className="muted">Paste a public job-posting URL. RoleSail will enrich and score it in the background.</p>
        <form onSubmit={(event) => { event.preventDefault(); mutation.mutate(url) }}>
          <label className="field">Job URL<input type="url" required autoFocus value={url} placeholder="https://company.com/careers/job…" onChange={(event) => setUrl(event.target.value)} /></label>
          <div className="button-row end"><button type="button" className="button" onClick={onClose}>Close</button><button className="button primary" disabled={mutation.isPending}>{mutation.isPending ? 'Adding…' : 'Add job'}</button></div>
        </form>
        {mutation.isError && <div className="callout error">{getError(mutation.error)}</div>}
        {pendingUrl && <div className="callout" role="status">{importStatusMessage(status.data)}{status.isFetching && '…'}</div>}
      </section>
    </div>
  )
}


function importStatusMessage(status?: Record<string, unknown>): string {
  if (!status) return 'Waiting for enrichment'
  const state = String(status.status ?? 'pending')
  if (state === 'complete') return `Added and scored${status.score == null ? '' : ` · ${status.score}/10`}.`
  if (state === 'error' || state === 'rejected') return String(status.error ?? status.message ?? 'Import did not complete.')
  return state === 'scoring' ? 'Job enriched; scoring' : state === 'enriching' ? 'Fetching job details' : 'Job queued'
}

function getError(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}

