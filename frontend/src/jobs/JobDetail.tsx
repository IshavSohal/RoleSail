import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, artifactUrl } from '../api'
import { Description } from '../components/Description'
import { JsonValue } from '../components/JsonView'
import { PdfViewer } from '../components/PdfViewer'
import type { Job, JobBucket, TailoringStatus } from '../types'
import { OutreachPanel } from './OutreachPanel'
import type { DetailTab } from './jobViewState'

export function JobDetail({ job, tab, tailoring, onChooseBucket }: { job: Job; tab: DetailTab; tailoring?: TailoringStatus; onChooseBucket: (bucket: JobBucket) => void }) {
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


function getError(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}


