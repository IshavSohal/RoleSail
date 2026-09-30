import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { MemoryRouter } from 'react-router-dom'
import type { Job } from '../types'
import { server } from '../test/server'
import { JobsPage } from './JobsPage'

const baseJob: Job = {
  url: 'https://example.com/jobs/1', title: 'Platform Engineer', company: 'Example',
  company_logo: '', location: 'Toronto', site: 'Example', strategy: '', salary: '',
  posted_at: '2026-09-19', posted_label: 'Posted Sep 19, 2026', score: 9,
  reasoning: 'Strong match', description: 'Responsibilities\nBuild systems', detail_error: '',
  application_url: 'https://example.com/jobs/1/apply', applied: false, applied_at: '',
  applied_label: '', applied_tab: 'active', status: 'Scored', priority: false,
  has_tailored: false, has_pdf: false, has_tex: false, has_report: false,
  can_tailor: true, can_retailor: false, tailor_attempts: 0, outreach_summary: null,
}

function renderPage(jobs: Job[]) {
  server.use(
    http.get('/api/jobs', () => HttpResponse.json({ jobs })),
    http.get('/api/tailoring/status', () => HttpResponse.json({ status: 'idle', current: null, queued: [], recent: [] })),
    http.get('/api/discovery/status', () => HttpResponse.json({ status: 'idle' })),
    http.get('/api/pipeline/status', () => HttpResponse.json({ run: null })),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><JobsPage /></MemoryRouter></QueryClientProvider>)
}

describe('JobsPage', () => {
  it('filters between the active and tailored buckets without a reload', async () => {
    const tailored = { ...baseJob, url: 'https://example.com/jobs/2', title: 'Frontend Engineer', status: 'Tailored', has_tailored: true, has_pdf: true, can_tailor: false, can_retailor: true }
    renderPage([baseJob, tailored])
    expect((await screen.findAllByText('Platform Engineer')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Frontend Engineer')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Tailored (1)' }))
    expect((await screen.findAllByText('Frontend Engineer')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Platform Engineer')).not.toBeInTheDocument()
  })

  it('searches the current bucket and reports empty results', async () => {
    renderPage([baseJob])
    const search = await screen.findByRole('searchbox', { name: 'Search jobs' })
    await userEvent.type(search, 'missing role')
    expect(screen.getByText('No jobs match this view.')).toBeInTheDocument()
  })

  it('selects all companies by default and allows individual companies to be excluded', async () => {
    const secondJob = { ...baseJob, url: 'https://example.com/jobs/2', title: 'Product Engineer', company: 'Acme' }
    renderPage([baseJob, secondJob])

    await userEvent.click(await screen.findByText('Companies · All'))
    expect(screen.getByRole('checkbox', { name: /Acme/ })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /Example/ })).toBeChecked()

    await userEvent.click(screen.getByRole('checkbox', { name: /Acme/ }))
    expect(screen.queryByText('Product Engineer')).not.toBeInTheDocument()
    expect((screen.getAllByText('Platform Engineer')).length).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole('button', { name: 'Select all' }))
    expect((screen.getAllByText('Product Engineer')).length).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(screen.getByText('No jobs match this view.')).toBeInTheDocument()
  })

  it('keeps company selections separate for each status tab', async () => {
    const secondJob = { ...baseJob, url: 'https://example.com/jobs/2', title: 'Product Engineer', company: 'Acme' }
    const tailored = { ...baseJob, url: 'https://example.com/jobs/3', title: 'Frontend Engineer', company: 'Contoso', status: 'Tailored', has_tailored: true, has_pdf: true, can_tailor: false, can_retailor: true }
    renderPage([baseJob, secondJob, tailored])

    await userEvent.click(await screen.findByText('Companies · All'))
    await userEvent.click(screen.getByRole('checkbox', { name: /Acme/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Tailored (1)' }))

    expect(screen.getByRole('checkbox', { name: /Contoso/ })).toBeChecked()
    expect(screen.getAllByText('Frontend Engineer').length).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole('checkbox', { name: /Contoso/ }))
    expect(screen.getByText('No jobs match this view.')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Jobs (2)' }))
    expect(screen.getByRole('checkbox', { name: /Acme/ })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /Example/ })).toBeChecked()
    expect(screen.queryByText('Product Engineer')).not.toBeInTheDocument()
  })
})
