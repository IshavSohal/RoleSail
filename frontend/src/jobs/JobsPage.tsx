import type React from 'react'
import type { Job, TailoringStatus } from '../types'
import { JobDetail } from './JobDetail'
import { JobInbox } from './JobInbox'
import {
  detailTabs,
  getInboxWidth,
  tailoringActive,
} from './jobViewState'
import { useJobsWorkspace } from './useJobsWorkspace'

export function JobsPage() {
  const state = useJobsWorkspace()
  const pipeline = state.pipelineQuery.data?.run ?? null

  return (
    <div className="jobs-page">
      <div className="workspace" style={{ '--inbox-width': `${getInboxWidth()}px` } as React.CSSProperties}>
        <JobInbox
          jobs={state.filteredJobs}
          allJobs={state.jobs}
          selected={state.selectedJob}
          bucket={state.bucket}
          counts={state.counts}
          score={state.score}
          search={state.search}
          companies={state.companies}
          selectedCompanies={state.selectedCompanies}
          onBucket={(value) => state.updateParam('bucket', value)}
          onScore={(value) => state.updateParam('score', value)}
          onSearch={(value) => state.updateParam('q', value)}
          onSelect={(job) => state.updateParam('job', job.url)}
          onCompany={state.chooseCompany}
          onSelectAllCompanies={() => state.setCompanySelection(state.companies.map(([company]) => company))}
          onClearCompanies={() => state.setCompanySelection([])}
          onStartDiscovery={() => state.startDiscovery.mutate()}
          onStartTailoring={() => state.startBulkTailoring.mutate()}
          discoveryBusy={state.startDiscovery.isPending || state.discoveryQuery.data?.status === 'running'}
          tailoringBusy={state.startBulkTailoring.isPending || tailoringActive(state.tailoringQuery.data)}
        />
        <Resizer />
        <main className="job-workspace">
          <JobToolbar
            job={state.selectedJob}
            pipeline={pipeline}
            busy={state.startPipeline.isPending || pipeline?.status === 'running'}
            onRun={() => state.startPipeline.mutate()}
          />
          <ActivityStrip
            discovery={state.discoveryQuery.data}
            tailoring={state.tailoringQuery.data}
            pipeline={pipeline}
            errors={[state.startPipeline.error, state.startDiscovery.error, state.startBulkTailoring.error]}
          />
          {state.selectedJob ? (
            <>
              <nav className="detail-tabs" aria-label="Selected job">
                {detailTabs.map((value) => <button key={value} className={state.tab === value ? 'active' : ''} onClick={() => state.updateParam('tab', value)}>{value[0].toUpperCase() + value.slice(1)}</button>)}
              </nav>
              <JobDetail job={state.selectedJob} tab={state.tab} tailoring={state.tailoringQuery.data} onChooseBucket={(value) => state.updateParam('bucket', value)} />
            </>
          ) : <div className="empty-state grow"><h2>No jobs match these filters</h2><p>Adjust the inbox filters, run discovery, or add a job URL.</p></div>}
        </main>
      </div>
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

function getError(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}

function money(value?: number): string {
  const amount = Number(value ?? 0)
  return `$${amount.toFixed(amount > 0 && amount < .01 ? 4 : 2)}`
}
