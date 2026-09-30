import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api, artifactUrl, logoUrl } from '../api'
import { Description } from '../components/Description'
import { JsonValue } from '../components/JsonView'
import { PdfViewer } from '../components/PdfViewer'
import type { Job, JobBucket, TailoringStatus } from '../types'
import { OutreachPanel } from './OutreachPanel'

const buckets: Array<{ key: JobBucket; label: string }> = [
  { key: 'jobs', label: 'Jobs' },
  { key: 'tailored', label: 'Tailored' },
  { key: 'needs_drafts', label: 'Needs drafts' },
  { key: 'drafts_done', label: 'Drafts done / legacy' },
]

const detailTabs = ['details', 'match', 'resume', 'report', 'outreach'] as const
type DetailTab = typeof detailTabs[number]
const emptyJobs: Job[] = []

export function JobsPage() {
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [pipelineRunId, setPipelineRunId] = useState<string | null>(() => {
    const current = sessionStorage.getItem('rolesailPipelineRun')
    const legacy = sessionStorage.getItem('applypilotPipelineRun')
    if (!current && legacy) sessionStorage.setItem('rolesailPipelineRun', legacy)
    return current ?? legacy
  })
  const jobsQuery = useQuery({ queryKey: ['jobs'], queryFn: api.jobs, staleTime: 3_000 })
  const tailoringQuery = useQuery({
    queryKey: ['tailoring-status'],
    queryFn: api.tailoringStatus,
    refetchInterval: ({ state }) => tailoringActive(state.data) ? 1_000 : false,
  })
  const discoveryQuery = useQuery({
    queryKey: ['discovery-status'],
    queryFn: api.discoveryStatus,
    refetchInterval: ({ state }) => state.data?.status === 'running' ? 1_000 : false,
  })
  const pipelineQuery = useQuery({
    queryKey: ['pipeline-status', pipelineRunId],
    queryFn: () => api.pipelineStatus(pipelineRunId),
    refetchInterval: ({ state }) => state.data?.run?.status === 'running' ? 1_000 : false,
  })
  const previousActivity = useRef('')

  const activity = `${tailoringQuery.data?.status}:${discoveryQuery.data?.status}:${pipelineQuery.data?.run?.status}`
  useEffect(() => {
    const previous = previousActivity.current
    previousActivity.current = activity
    if (previous && previous !== activity && (
      tailoringQuery.data?.status === 'idle'
      || ['complete', 'error'].includes(discoveryQuery.data?.status ?? '')
      || (pipelineQuery.data?.run && pipelineQuery.data.run.status !== 'running')
    )) void queryClient.invalidateQueries({ queryKey: ['jobs'] })
  }, [activity, discoveryQuery.data?.status, pipelineQuery.data?.run, queryClient, tailoringQuery.data?.status])

  const jobs = jobsQuery.data?.jobs ?? emptyJobs
  const bucket = validBucket(params.get('bucket'))
  const score = params.get('score') ?? 'all'
  const search = params.get('q') ?? ''
  const tab = validTab(params.get('tab'))
  const counts = useMemo(() => Object.fromEntries(buckets.map(({ key }) => [key, jobs.filter((job) => matchesBucket(job, key) && matchesScore(job, score)).length])) as Record<JobBucket, number>, [jobs, score])
  const companies = useMemo(() => {
    const countsByCompany = new Map<string, number>()
    jobs.filter((job) => matchesBucket(job, bucket) && matchesScore(job, score)).forEach((job) => countsByCompany.set(job.company, (countsByCompany.get(job.company) ?? 0) + 1))
    return [...countsByCompany.entries()].sort(([left], [right]) => left.localeCompare(right))
  }, [bucket, jobs, score])
  const companyParam = `company_${bucket}`
  const selectedCompanies = useMemo(() => params.has(companyParam)
    ? params.getAll(companyParam).filter(Boolean)
    : companies.map(([company]) => company), [companies, companyParam, params])
  const filteredJobs = useMemo(() => jobs.filter((job) => {
    const haystack = `${job.title} ${job.company} ${job.location}`.toLowerCase()
    return matchesBucket(job, bucket)
      && matchesScore(job, score)
      && (!search || haystack.includes(search.toLowerCase()))
      && selectedCompanies.includes(job.company)
  }), [bucket, jobs, score, search, selectedCompanies])
  const selectedUrl = params.get('job')
  const selectedJob = filteredJobs.find((job) => job.url === selectedUrl) ?? filteredJobs[0] ?? null

  useEffect(() => {
    if (selectedJob && selectedJob.url !== selectedUrl) {
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.set('job', selectedJob.url)
        return next
      }, { replace: true })
    } else if (!selectedJob && selectedUrl) {
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.delete('job')
        return next
      }, { replace: true })
    }
  }, [selectedJob, selectedUrl, setParams])

  const updateParam = (key: string, value?: string) => setParams((current) => {
    const next = new URLSearchParams(current)
    if (value && value !== 'all' && !(key === 'bucket' && value === 'jobs')) next.set(key, value)
    else next.delete(key)
    if (key !== 'job' && !['tab', 'q'].includes(key)) next.delete('job')
    return next
  })

  const setCompanySelection = (values: string[]) => setParams((current) => {
    const next = new URLSearchParams(current)
    next.delete(companyParam)
    if (values.length === companies.length && companies.every(([company]) => values.includes(company))) {
      // Absence of a tab-specific parameter means every company is selected.
    } else if (values.length) values.forEach((value) => next.append(companyParam, value))
    else next.append(companyParam, '')
    next.delete('job')
    return next
  })

  const chooseCompany = (company: string, checked: boolean) => {
    const selections = new Set(selectedCompanies)
    if (checked) selections.add(company)
    else selections.delete(company)
    setCompanySelection([...selections])
  }

  const chooseAllCompanies = () => setCompanySelection(companies.map(([company]) => company))

  const startPipeline = useMutation({
    mutationFn: api.startPipeline,
    onSuccess: (result) => {
      setPipelineRunId(result.id)
      sessionStorage.setItem('rolesailPipelineRun', result.id)
      void queryClient.invalidateQueries({ queryKey: ['pipeline-status'] })
    },
  })
  const startDiscovery = useMutation({ mutationFn: api.startDiscovery, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['discovery-status'] }) })
  const startBulkTailoring = useMutation({ mutationFn: api.startBulkTailoring, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['tailoring-status'] }) })

  return (
    <div className="jobs-page">
      <div className="workspace" style={{ '--inbox-width': `${getInboxWidth()}px` } as React.CSSProperties}>
        <JobInbox
          jobs={filteredJobs}
          allJobs={jobs}
          selected={selectedJob}
          bucket={bucket}
          counts={counts}
          score={score}
          search={search}
          companies={companies}
          selectedCompanies={selectedCompanies}
          onBucket={(value) => updateParam('bucket', value)}
          onScore={(value) => updateParam('score', value)}
          onSearch={(value) => updateParam('q', value)}
          onSelect={(job) => updateParam('job', job.url)}
          onCompany={chooseCompany}
          onSelectAllCompanies={chooseAllCompanies}
          onClearCompanies={() => setCompanySelection([])}
          onStartDiscovery={() => startDiscovery.mutate()}
          onStartTailoring={() => startBulkTailoring.mutate()}
          discoveryBusy={startDiscovery.isPending || discoveryQuery.data?.status === 'running'}
          tailoringBusy={startBulkTailoring.isPending || tailoringActive(tailoringQuery.data)}
        />
        <Resizer />
        <main className="job-workspace">
          <JobToolbar
            job={selectedJob}
            pipeline={pipelineQuery.data?.run ?? null}
            busy={startPipeline.isPending || pipelineQuery.data?.run?.status === 'running'}
            onRun={() => startPipeline.mutate()}
          />
          <ActivityStrip discovery={discoveryQuery.data} tailoring={tailoringQuery.data} pipeline={pipelineQuery.data?.run ?? null} errors={[startPipeline.error, startDiscovery.error, startBulkTailoring.error]} />
          {selectedJob ? (
            <>
              <nav className="detail-tabs" aria-label="Selected job">
                {detailTabs.map((value) => <button key={value} className={tab === value ? 'active' : ''} onClick={() => updateParam('tab', value)}>{value[0].toUpperCase() + value.slice(1)}</button>)}
              </nav>
              <JobDetail job={selectedJob} tab={tab} tailoring={tailoringQuery.data} onChooseBucket={(value) => updateParam('bucket', value)} />
            </>
          ) : <div className="empty-state grow"><h2>No jobs match these filters</h2><p>Adjust the inbox filters, run discovery, or add a job URL.</p></div>}
        </main>
      </div>
    </div>
  )
}

function JobInbox(props: {
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

function JobToolbar({ job, pipeline, busy, onRun }: { job: Job | null; pipeline: { status: string; current_stage?: string | null; usage?: { cost_usd?: number } } | null; busy: boolean; onRun: () => void }) {
  return (
    <>
      <header className="workspace-toolbar">
        <div><h1>{job?.title ?? 'Select a job'}</h1><p>{job ? `${job.company} · ${job.location} · ${job.status}` : 'Choose a job from the inbox'}</p></div>
        <div className="button-row">
          {job && <a className="button" href={job.application_url} target="_blank" rel="noreferrer">Open posting ↗</a>}
          <button className="button primary" disabled={busy} onClick={onRun}>{busy ? 'Pipeline running…' : 'Run pipeline'}</button>
        </div>
      </header>
      {pipeline && pipeline.status !== 'complete' && <div className={`pipeline-strip ${pipeline.status}`}><span>{pipeline.status === 'running' ? `Running ${pipeline.current_stage ?? 'pipeline'}…` : `Last run: ${pipeline.status}`}</span><span>Run cost ${money(pipeline.usage?.cost_usd)}</span></div>}
    </>
  )
}

function ActivityStrip({ discovery, tailoring, pipeline, errors }: { discovery?: { status: string; error?: string | null; result?: Record<string, unknown> | null }; tailoring?: TailoringStatus; pipeline: { status: string } | null; errors: unknown[] }) {
  const messages: string[] = []
  if (discovery?.status === 'running') messages.push('Discovering and scoring jobs…')
  if (tailoringActive(tailoring)) {
    const waiting = tailoring?.queued.length ?? 0
    messages.push(`Tailoring resumes${waiting ? ` · ${waiting} queued` : ''}…`)
  }
  if (pipeline?.status === 'running') messages.push('Pipeline is running…')
  const error = errors.find(Boolean) ?? (discovery?.error ? new Error(discovery.error) : null)
  if (!messages.length && !error) return null
  return <div className={`activity-strip ${error ? 'error' : ''}`} role="status">{error ? getError(error) : messages.join(' ')}</div>
}

function JobDetail({ job, tab, tailoring, onChooseBucket }: { job: Job; tab: DetailTab; tailoring?: TailoringStatus; onChooseBucket: (bucket: JobBucket) => void }) {
  const queryClient = useQueryClient()
  const [notice, setNotice] = useState('')
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['jobs'] })
  const applied = useMutation({ mutationFn: () => api.markApplied(job.url, !job.applied), onSuccess: async (result) => { await invalidate(); onChooseBucket(!job.applied ? (String(result.applied_tab ?? 'needs_drafts') as JobBucket) : (job.has_tailored ? 'tailored' : 'jobs')) } })
  const remove = useMutation({ mutationFn: () => api.deleteJob(job.url), onSuccess: async () => { await invalidate(); setNotice('Job deleted.') } })
  const tailor = useMutation({ mutationFn: () => api.startJobTailoring(job.url, job.has_tailored), onSuccess: async () => { setNotice('Tailoring request queued.'); await queryClient.invalidateQueries({ queryKey: ['tailoring-status'] }) } })
  const cancelTailor = useMutation({ mutationFn: () => api.cancelTailoring(job.url), onSuccess: async () => { setNotice('Tailoring cancellation requested.'); await queryClient.invalidateQueries({ queryKey: ['tailoring-status'] }) } })
  const clear = useMutation({ mutationFn: () => api.clearTailored(job.url), onSuccess: async () => { setNotice('Tailored resume removed.'); await invalidate() } })
  const jobQueued = [tailoring?.current, ...(tailoring?.queued ?? [])].some((request) => request?.target_url === job.url)
  const busy = applied.isPending || remove.isPending || tailor.isPending || cancelTailor.isPending || clear.isPending
  const error = [applied.error, remove.error, tailor.error, cancelTailor.error, clear.error].find(Boolean)

  return (
    <div className="workspace-body">
      <div className="job-actionbar">
        <button className="button" disabled={busy} onClick={() => applied.mutate()}>{job.applied ? 'Mark active' : 'Mark applied'}</button>
        <button className="button danger" disabled={busy} onClick={() => window.confirm(`Delete “${job.title}” from RoleSail?`) && remove.mutate()}>Delete job</button>
        {notice && <span className="status-line">{notice}</span>}{error && <span className="status-line error">{getError(error)}</span>}
      </div>
      {tab === 'details' && <div className="detail-grid"><article className="card"><h2>About this role</h2><Description text={job.description} /></article><aside className="card action-card"><h2>Actions</h2>{job.salary && <p><strong>{job.salary}</strong></p>}<p className="muted">{job.posted_label} · {job.site}</p>{jobQueued ? <button className="button danger" disabled={busy} onClick={() => cancelTailor.mutate()}>Cancel tailoring</button> : <button className="button primary" disabled={busy || (!job.can_tailor && !job.can_retailor)} onClick={() => tailor.mutate()}>{job.has_tailored ? 'Tailor again' : 'Tailor resume'}</button>}{!job.can_tailor && !job.can_retailor && !job.has_tailored && <p className="muted">A full description is required. Tailoring stops after five attempts.</p>}{job.detail_error && <p className="callout warning">{job.detail_error}</p>}</aside></div>}
      {tab === 'match' && <article className="card"><h2>Why this role matches</h2><Description text={job.reasoning} empty="This job has not been scored yet." /></article>}
      {tab === 'resume' && <ResumePanel job={job} busy={busy} onTailor={() => tailor.mutate()} onClear={() => window.confirm('Delete this tailored resume and report?') && clear.mutate()} />}
      {tab === 'report' && <ReportPanel job={job} />}
      {tab === 'outreach' && <OutreachPanel job={job} />}
    </div>
  )
}

function ResumePanel({ job, busy, onTailor, onClear }: { job: Job; busy: boolean; onTailor: () => void; onClear: () => void }) {
  if (!job.has_tailored) return <div className="empty-state grow"><h2>No tailored resume yet</h2><p>Tailor this job to generate an in-app PDF preview.</p><button className="button primary" disabled={!job.can_tailor || busy} onClick={onTailor}>Tailor resume</button></div>
  if (!job.has_pdf) return <div className="empty-state grow"><h2>Tailored resume unavailable</h2><p>The stored resume has no PDF preview.</p><div className="button-row"><button className="button" disabled={!job.can_retailor || busy} onClick={onTailor}>Tailor again</button><button className="button danger" disabled={busy} onClick={onClear}>Delete tailored resume</button></div></div>
  return <div className="artifact-layout"><PdfViewer url={artifactUrl(job.url, 'pdf')} label={`${job.title} tailored resume`} /><aside className="card"><h2>Files</h2><a className="button" href={artifactUrl(job.url, 'pdf')} download>Download PDF</a>{job.has_tex && <a className="button" href={artifactUrl(job.url, 'tex')} download>Download LaTeX</a>}<button className="button" disabled={!job.can_retailor || busy} onClick={onTailor}>Tailor again</button><button className="button danger" disabled={busy} onClick={onClear}>Delete tailored resume</button></aside></div>
}

function ReportPanel({ job }: { job: Job }) {
  const report = useQuery({ queryKey: ['report', job.url], queryFn: () => api.artifactReport(job.url), enabled: job.has_report, retry: false })
  if (!job.has_report) return <div className="empty-state grow"><h2>No tailoring report</h2><p>A report will appear after this resume is tailored.</p></div>
  if (report.isPending) return <div className="state-message">Loading report…</div>
  if (report.isError) return <div className="callout error">{getError(report.error)}</div>
  return <article className="card report"><div className="section-heading"><h2>Complete report</h2><a className="button" href={artifactUrl(job.url, 'report')} target="_blank" rel="noreferrer">View raw JSON</a></div><JsonValue value={report.data} /></article>
}

function Resizer() {
  const begin = (event: React.PointerEvent<HTMLDivElement>) => {
    const workspace = event.currentTarget.parentElement
    if (!workspace) return
    const startX = event.clientX
    const startWidth = Number.parseInt(getComputedStyle(workspace).getPropertyValue('--inbox-width'), 10) || getInboxWidth()
    const move = (moveEvent: PointerEvent) => {
      const width = Math.max(260, Math.min(600, startWidth + moveEvent.clientX - startX))
      workspace.style.setProperty('--inbox-width', `${width}px`)
      localStorage.setItem('rolesailJobInboxWidth', String(Math.round(width)))
    }
    const end = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', end) }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', end)
  }
  return <div className="workspace-resizer" role="separator" aria-label="Resize job inbox" tabIndex={0} onPointerDown={begin} />
}

function matchesBucket(job: Job, bucket: JobBucket): boolean {
  return bucket === 'needs_drafts' || bucket === 'drafts_done'
    ? job.applied_tab === bucket
    : !job.applied && (bucket === 'tailored' ? job.has_tailored : !job.has_tailored)
}

function matchesScore(job: Job, score: string): boolean {
  return score === 'all' || job.score === Number(score)
}

function validBucket(value: string | null): JobBucket {
  return buckets.some((bucket) => bucket.key === value) ? value as JobBucket : 'jobs'
}

function validTab(value: string | null): DetailTab {
  return detailTabs.includes(value as DetailTab) ? value as DetailTab : 'details'
}

function tailoringActive(status?: TailoringStatus): boolean {
  return status?.status === 'running' || Boolean(status?.queued.length)
}

function importStatusMessage(status?: Record<string, unknown>): string {
  if (!status) return 'Waiting for enrichment'
  const state = String(status.status ?? 'pending')
  if (state === 'complete') return `Added and scored${status.score == null ? '' : ` · ${status.score}/10`}.`
  if (state === 'error' || state === 'rejected') return String(status.error ?? status.message ?? 'Import did not complete.')
  return state === 'scoring' ? 'Job enriched; scoring' : state === 'enriching' ? 'Fetching job details' : 'Job queued'
}

function getInboxWidth(): number {
  const current = localStorage.getItem('rolesailJobInboxWidth')
  const legacy = localStorage.getItem('applypilotJobInboxWidth')
  if (!current && legacy) localStorage.setItem('rolesailJobInboxWidth', legacy)
  const saved = current ?? legacy
  if (saved === null) return 340
  const value = Number(saved)
  return Number.isFinite(value) ? Math.max(260, Math.min(600, value)) : 340
}

function getError(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}

function money(value?: number): string {
  const amount = Number(value ?? 0)
  return `$${amount.toFixed(amount > 0 && amount < .01 ? 4 : 2)}`
}
