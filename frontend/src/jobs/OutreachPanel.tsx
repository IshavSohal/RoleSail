import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiRequestError, api } from '../api'
import type { Job, OutreachRecipient } from '../types'

type Draft = OutreachRecipient & { selected: boolean }

export function OutreachPanel({ job }: { job: Job }) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['outreach', job.url],
    queryFn: () => api.outreach(job.url),
    retry: (count, error) => !(error instanceof ApiRequestError && error.status === 404) && count < 2,
    refetchInterval: ({ state }) => {
      const status = state.data?.batch.status
      return status && ['queued', 'preparing', 'sending', 'scheduled', 'partial_failed'].includes(status)
        ? (['scheduled', 'partial_failed'].includes(status) ? 30_000 : 2_500)
        : false
    },
  })
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [notice, setNotice] = useState('')
  const [redraftOpen, setRedraftOpen] = useState(false)
  const [redraftFeedback, setRedraftFeedback] = useState('')

  useEffect(() => {
    setDrafts((query.data?.batch.recipients ?? []).map((recipient) => ({
      ...recipient,
      selected: recipient.status === 'ready' || recipient.status === 'failed',
    })))
  }, [query.data])

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['outreach', job.url] }),
      queryClient.invalidateQueries({ queryKey: ['jobs'] }),
    ])
  }

  const action = useMutation({
    mutationFn: ({ name, body }: { name: string; body: unknown }) => api.outreachAction(name, body),
    onSuccess: async (_, variables) => {
      setNotice(variables.name === 'gmail-drafts'
        ? 'Gmail drafts created. Review and schedule them in Gmail.'
        : variables.name === 'approve'
          ? 'Emails scheduled for direct delivery.'
          : variables.name === 'redraft'
            ? 'Emails redrafted.'
            : 'Outreach updated.')
      await refresh()
    },
  })
  const redrafting = action.isPending && action.variables?.name === 'redraft'

  const prepare = () => {
    if (!window.confirm('Prepare outreach for this application? Finding verified work emails may use Apollo enrichment credits.')) return
    action.mutate({ name: 'prepare', body: { job_url: job.url } })
  }

  if (query.isPending) return <div className="state-message">Loading outreach…</div>
  if (query.error instanceof ApiRequestError && query.error.status === 404) {
    return job.applied ? (
      <div className="empty-state">
        <h2>No outreach batch</h2>
        <p>Prepare personalized emails for relevant employees at this company.</p>
        <button className="button primary" disabled={action.isPending} onClick={prepare}>Prepare outreach emails</button>
        <MutationMessage mutation={action} />
      </div>
    ) : <div className="empty-state">Outreach becomes available after you apply.</div>
  }
  if (query.isError || !query.data) return <div className="callout error">{errorMessage(query.error, 'Could not load outreach.')}</div>

  const { batch, gmail_account: gmailAccount } = query.data
  const editable = ['ready_for_review', 'failed', 'partial_failed'].includes(batch.status)
  const editableRecipients = drafts.some((recipient) => ['ready', 'needs_edit', 'failed'].includes(recipient.status))
  const selected = drafts.filter((recipient) => recipient.selected).map((recipient) => ({
    id: recipient.id,
    subject: recipient.subject?.trim() ?? '',
    body_text: recipient.body_text?.trim() ?? '',
  }))

  const createDrafts = () => {
    if (!selected.length) return setNotice('Select at least one recipient.')
    if (!gmailAccount) return setNotice('Connect Gmail from the CLI, then reload this page.')
    if (!window.confirm(`Create ${selected.length} unsent Gmail draft${selected.length === 1 ? '' : 's'} in ${gmailAccount}?`)) return
    action.mutate({
      name: 'gmail-drafts',
      body: { batch_id: batch.id, recipients: selected, confirmed_account: gmailAccount },
    })
  }

  const sendDirectly = () => {
    if (!selected.length) return setNotice('Select at least one recipient.')
    if (!window.confirm(`Send ${selected.length} selected email${selected.length === 1 ? '' : 's'} directly through Apollo? RoleSail will use your configured delivery schedule.`)) return
    action.mutate({
      name: 'approve',
      body: { batch_id: batch.id, recipients: selected, confirmed: true },
    })
  }

  const redraft = () => {
    setRedraftOpen(false)
    action.mutate({
      name: 'redraft',
      body: { batch_id: batch.id, feedback: redraftFeedback.trim() },
    }, {
      onSuccess: () => {
        setRedraftFeedback('')
      },
    })
  }

  return (
    <div className="outreach-panel">
      <div className="section-heading">
        <div><h2>Employee outreach</h2><p className="muted">Status: {batch.status}{batch.company_domain ? ` · ${batch.company_domain}` : ''}</p></div>
        <button className="button subtle" onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button>
      </div>
      {batch.error && <div className="callout error">{batch.error}</div>}
      {batch.status === 'drafting' && <div className="callout warning">Gmail draft creation was interrupted or uncertain. Check Gmail Drafts before retrying.</div>}
      {drafts.some((recipient) => recipient.status === 'scheduled') && <div className="callout warning">Selected emails are scheduled for direct delivery through Apollo.</div>}
      <div className="recipient-list">
        {drafts.map((recipient) => (
          <RecipientCard
            key={recipient.id}
            recipient={recipient}
            editable={editable}
            busy={action.isPending}
            onChange={(next) => setDrafts((current) => current.map((item) => item.id === next.id ? next : item))}
            onReset={() => {
              if (window.confirm('Confirm only after checking Gmail and finding no matching draft. Continue?')) {
                action.mutate({ name: 'reset-gmail-draft', body: { recipient_id: recipient.id, confirmed_no_draft: true } })
              }
            }}
            onSuppress={() => action.mutate({ name: 'suppress', body: { recipient_id: recipient.id, reason: 'user' } })}
            onRestore={() => action.mutate({ name: 'restore-suppressed', body: { recipient_id: recipient.id } })}
          />
        ))}
        {!drafts.length && <div className="empty-state">No eligible recipients are available yet.</div>}
      </div>
      <div className="button-row wrap">
        {editable && editableRecipients && (
          <SendMenu
            busy={action.isPending}
            gmailAccount={gmailAccount}
            onCreateDrafts={createDrafts}
            onSendDirectly={sendDirectly}
          />
        )}
        {editable && !gmailAccount && <span className="muted">Connect Gmail with <code>rolesail gmail-connect</code>.</span>}
        {drafts.some((recipient) => recipient.status === 'drafted') && <a className="button" href={`https://mail.google.com/mail/?authuser=${encodeURIComponent(drafts.find((item) => item.gmail_account_email)?.gmail_account_email ?? gmailAccount ?? '')}#drafts`} target="_blank" rel="noreferrer">Open Gmail Drafts</a>}
        {['ready_for_review', 'cancelled'].includes(batch.status) && drafts.length > 0 && (
          <button className="button" disabled={action.isPending} onClick={() => setRedraftOpen(true)}>
            {redrafting && <span className="button-spinner" aria-hidden="true" />}
            {redrafting ? 'Redrafting…' : 'Redraft emails'}
          </button>
        )}
        {['failed', 'partial_failed'].includes(batch.status) && !drafts.length && <button className="button" disabled={action.isPending} onClick={() => action.mutate({ name: 'retry', body: { batch_id: batch.id } })}>Retry preparation</button>}
        {drafts.some((recipient) => recipient.status === 'scheduled') && <ConfirmAction danger disabled={action.isPending} label="Cancel remaining sends" prompt="Cancel every outreach email that has not started sending?" onConfirm={() => action.mutate({ name: 'cancel-pending', body: { batch_id: batch.id } })} />}
        {!['sending', 'drafting', 'drafted', 'completed', 'cancelled'].includes(batch.status) && <ConfirmAction danger disabled={action.isPending} label="Cancel batch" prompt="Cancel this unsent outreach batch?" onConfirm={() => action.mutate({ name: 'cancel', body: { batch_id: batch.id } })} />}
        {batch.status === 'cancelled' && <ConfirmAction danger disabled={action.isPending} label="Clear outreach" prompt="Permanently clear this cancelled outreach batch?" onConfirm={() => action.mutate({ name: 'clear', body: { batch_id: batch.id } })} />}
      </div>
      {notice && <p className="status-line" role="status">{notice}</p>}
      <MutationMessage mutation={action} />
      {redraftOpen && (
        <RedraftDialog
          feedback={redraftFeedback}
          busy={action.isPending}
          onFeedbackChange={setRedraftFeedback}
          onClose={() => setRedraftOpen(false)}
          onSubmit={redraft}
        />
      )}
    </div>
  )
}

function SendMenu({ busy, gmailAccount, onCreateDrafts, onSendDirectly }: {
  busy: boolean
  gmailAccount?: string | null
  onCreateDrafts: () => void
  onSendDirectly: () => void
}) {
  const detailsRef = useRef<HTMLDetailsElement>(null)
  const [openAbove, setOpenAbove] = useState(false)
  const select = (event: React.MouseEvent<HTMLButtonElement>, action: () => void) => {
    event.currentTarget.closest('details')?.removeAttribute('open')
    action()
  }
  const positionMenu = () => {
    const details = detailsRef.current
    if (!details?.open) return
    window.requestAnimationFrame(() => {
      const trigger = details.querySelector('summary')?.getBoundingClientRect()
      const popover = details.querySelector<HTMLElement>('.action-menu-popover')
      if (!trigger || !popover) return
      const menuHeight = popover.getBoundingClientRect().height
      const spaceBelow = window.innerHeight - trigger.bottom
      const spaceAbove = trigger.top
      setOpenAbove(menuHeight > spaceBelow && spaceAbove > spaceBelow)
    })
  }
  return (
    <details ref={detailsRef} className={`action-menu ${openAbove ? 'open-above' : ''}`} onToggle={positionMenu}>
      <summary className="button primary" aria-disabled={busy} onClick={(event) => busy && event.preventDefault()}>Send <span aria-hidden="true">▾</span></summary>
      <div className="action-menu-popover" role="menu">
        <button role="menuitem" disabled={busy || !gmailAccount} onClick={(event) => select(event, onCreateDrafts)}>
          <strong>Create Gmail drafts</strong>
          <small>{gmailAccount ? `Save unsent drafts in ${gmailAccount}` : 'Connect Gmail to use this option'}</small>
        </button>
        <button role="menuitem" disabled={busy} onClick={(event) => select(event, onSendDirectly)}>
          <strong>Send directly</strong>
          <small>Deliver automatically through Apollo</small>
        </button>
      </div>
    </details>
  )
}

function RedraftDialog({ feedback, busy, onFeedbackChange, onClose, onSubmit }: {
  feedback: string
  busy: boolean
  onFeedbackChange: (feedback: string) => void
  onClose: () => void
  onSubmit: () => void
}) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !busy && onClose()}>
      <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="redraft-title">
        <div className="dialog-head">
          <div><h2 id="redraft-title">Redraft emails</h2><p className="muted">Replace every editable subject and message with a newly generated draft.</p></div>
          <button className="icon-button" disabled={busy} onClick={onClose} aria-label="Close">×</button>
        </div>
        <label className="field">
          Feedback (optional)
          <textarea
            autoFocus
            rows={3}
            maxLength={500}
            value={feedback}
            disabled={busy}
            placeholder="For example: Make the opening more direct and emphasize my backend experience."
            onChange={(event) => onFeedbackChange(event.target.value)}
          />
          <small>Keep it to 1–2 sentences. This guidance will be added to the redrafting prompt.</small>
        </label>
        <div className="button-row end">
          <button className="button" disabled={busy} onClick={onClose}>Cancel</button>
          <button className="button primary" disabled={busy} onClick={onSubmit}>{busy ? 'Redrafting…' : 'Redraft emails'}</button>
        </div>
      </section>
    </div>
  )
}

function RecipientCard({ recipient, editable, busy, onChange, onReset, onSuppress, onRestore }: {
  recipient: Draft
  editable: boolean
  busy: boolean
  onChange: (recipient: Draft) => void
  onReset: () => void
  onSuppress: () => void
  onRestore: () => void
}) {
  const canEdit = editable && ['ready', 'needs_edit', 'failed'].includes(recipient.status)
  const name = [recipient.first_name, recipient.last_name].filter(Boolean).join(' ') || recipient.email || 'Recipient'
  return (
    <article className="card recipient-card">
      <div className="recipient-head">
        <input type="checkbox" aria-label={`Select ${name}`} checked={recipient.selected} disabled={!canEdit || busy} onChange={(event) => onChange({ ...recipient, selected: event.target.checked })} />
        <div><strong>{name}</strong><small>{recipient.title || 'Employee'} · {recipient.email} · {recipient.status}</small>{recipient.gmail_draft_id && <small>Gmail draft in {recipient.gmail_account_email} · not sent</small>}<small>{recipient.relevance_reason}</small></div>
      </div>
      <label className="field">Subject<input maxLength={200} value={recipient.subject ?? ''} disabled={!canEdit || busy} onChange={(event) => onChange({ ...recipient, subject: event.target.value })} /></label>
      <label className="field">Message<textarea rows={8} maxLength={4000} value={recipient.body_text ?? ''} disabled={!canEdit || busy} onChange={(event) => onChange({ ...recipient, body_text: event.target.value })} /></label>
      {recipient.error && <p className="callout error">{recipient.error}</p>}
      <div className="button-row">
        {recipient.status === 'drafting' && <button className="button" onClick={onReset}>I checked Gmail; no draft exists</button>}
        {canEdit && <button className="button danger" disabled={busy} onClick={onSuppress}>Never contact</button>}
        {recipient.status === 'suppressed' && <button className="button" disabled={busy} onClick={onRestore}>Undo never contact</button>}
      </div>
    </article>
  )
}

function ConfirmAction({ label, prompt, onConfirm, danger = false, disabled = false }: { label: string; prompt: string; onConfirm: () => void; danger?: boolean; disabled?: boolean }) {
  return <button className={`button ${danger ? 'danger' : ''}`} disabled={disabled} onClick={() => window.confirm(prompt) && onConfirm()}>{label}</button>
}

function MutationMessage({ mutation }: { mutation: { isError: boolean; error: unknown } }) {
  return mutation.isError ? <p className="callout error" role="alert">{errorMessage(mutation.error, 'Outreach action failed.')}</p> : null
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}
