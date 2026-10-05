import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { vi } from 'vitest'
import type { Job, OutreachRecipient } from '../types'
import { server } from '../test/server'
import { OutreachPanel } from './OutreachPanel'

const job = {
  url: 'https://example.com/jobs/1', applied: true,
} as Job

const recipient: OutreachRecipient = {
  id: 'recipient-1', first_name: 'Morgan', last_name: 'Manager',
  email: 'morgan@example.com', title: 'Engineering Manager', status: 'suppressed',
  subject: 'Platform Engineer application', body_text: 'Hello Morgan',
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <OutreachPanel job={job} />
    </QueryClientProvider>,
  )
}

describe('OutreachPanel', () => {
  it('lets a user undo never contact', async () => {
    let status = 'suppressed'
    let requestedRecipient = ''
    server.use(
      http.get('/api/outreach', () => HttpResponse.json({
        batch: { id: 'batch-1', status: status === 'suppressed' ? 'completed' : 'ready_for_review', recipients: [{ ...recipient, status }] },
        gmail_account: 'me@example.com',
      })),
      http.post('/api/outreach/restore-suppressed', async ({ request }) => {
        const body = await request.json() as { recipient_id: string }
        requestedRecipient = body.recipient_id
        status = 'needs_edit'
        return HttpResponse.json({ batch: { id: 'batch-1', status: 'ready_for_review', recipients: [{ ...recipient, status }] } })
      }),
    )
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: 'Undo never contact' }))

    expect(requestedRecipient).toBe('recipient-1')
    expect(await screen.findByRole('button', { name: 'Never contact' })).toBeInTheDocument()
  })

  it('offers Gmail drafts and direct delivery from the Send menu', async () => {
    let approved: Record<string, unknown> | undefined
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    server.use(
      http.get('/api/outreach', () => HttpResponse.json({
        batch: { id: 'batch-1', status: 'ready_for_review', recipients: [{ ...recipient, status: 'ready' }] },
        gmail_account: 'me@example.com',
      })),
      http.post('/api/outreach/approve', async ({ request }) => {
        approved = await request.json() as Record<string, unknown>
        return HttpResponse.json({ batch: { id: 'batch-1', status: 'scheduled', recipients: [] } })
      }),
    )
    renderPanel()

    await userEvent.click(await screen.findByText('Send', { selector: 'summary' }))
    expect(screen.getByRole('menuitem', { name: /Create Gmail drafts/ })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('menuitem', { name: /Send directly/ }))

    expect(approved).toMatchObject({ batch_id: 'batch-1', confirmed: true })
    expect(approved?.recipients).toEqual([{
      id: 'recipient-1', subject: 'Platform Engineer application', body_text: 'Hello Morgan',
    }])
    confirm.mockRestore()
  })

  it('opens the Send menu above when there is not enough room below', async () => {
    const animationFrame = vi.spyOn(window, 'requestAnimationFrame')
      .mockImplementation((callback) => { callback(0); return 0 })
    server.use(
      http.get('/api/outreach', () => HttpResponse.json({
        batch: { id: 'batch-1', status: 'ready_for_review', recipients: [{ ...recipient, status: 'ready' }] },
        gmail_account: 'me@example.com',
      })),
    )
    renderPanel()

    const send = await screen.findByText('Send', { selector: 'summary' })
    vi.spyOn(send, 'getBoundingClientRect').mockReturnValue({
      top: 700, bottom: 736, left: 0, right: 90, width: 90, height: 36, x: 0, y: 700, toJSON: () => ({}),
    })
    const menu = send.parentElement?.querySelector<HTMLElement>('.action-menu-popover')
    vi.spyOn(menu!, 'getBoundingClientRect').mockReturnValue({
      top: 0, bottom: 150, left: 0, right: 280, width: 280, height: 150, x: 0, y: 0, toJSON: () => ({}),
    })

    await userEvent.click(send)

    expect(await screen.findByText('Create Gmail drafts')).toBeVisible()
    await vi.waitFor(() => expect(send.parentElement).toHaveClass('open-above'))
    animationFrame.mockRestore()
  })

  it('adds optional feedback when redrafting emails', async () => {
    let redraftRequest: Record<string, unknown> | undefined
    let finishRedraft!: () => void
    const redraftPending = new Promise<void>((resolve) => { finishRedraft = resolve })
    server.use(
      http.get('/api/outreach', () => HttpResponse.json({
        batch: { id: 'batch-1', status: 'ready_for_review', recipients: [{ ...recipient, status: 'ready' }] },
        gmail_account: 'me@example.com',
      })),
      http.post('/api/outreach/redraft', async ({ request }) => {
        redraftRequest = await request.json() as Record<string, unknown>
        await redraftPending
        return HttpResponse.json({ batch: { id: 'batch-1', status: 'ready_for_review', recipients: [{ ...recipient, status: 'ready' }] } })
      }),
    )
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: 'Redraft emails' }))
    const dialog = screen.getByRole('dialog', { name: 'Redraft emails' })
    await userEvent.type(within(dialog).getByLabelText(/Feedback/), 'Make the opening more direct.')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Redraft emails' }))

    expect(screen.queryByRole('dialog', { name: 'Redraft emails' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Redrafting…' })).toBeDisabled()
    expect(screen.getByText('Send', { selector: 'summary' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('button', { name: 'Cancel batch' })).toBeDisabled()
    expect(redraftRequest).toEqual({
      batch_id: 'batch-1', feedback: 'Make the opening more direct.',
    })

    finishRedraft()
    expect(await screen.findByText('Emails redrafted.')).toBeInTheDocument()
  })
})
