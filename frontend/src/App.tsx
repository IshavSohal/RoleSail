import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api'
import { JobsPage } from './jobs/JobsPage'
import { ProfilePage } from './profile/ProfilePage'

export function App() {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('rolesailTheme') ?? localStorage.getItem('applypilotTheme')
    if (saved === 'light' || saved === 'dark') return saved
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const [usageOpen, setUsageOpen] = useState(false)
  const usage = useQuery({ queryKey: ['usage'], queryFn: api.usage, staleTime: 15_000 })

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('rolesailTheme', theme)
  }, [theme])

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <NavLink to="/" className="brand">RoleSail</NavLink>
        <nav aria-label="Application">
          <NavLink to="/" end>Job inbox</NavLink>
          <NavLink to="/profile">Profile</NavLink>
        </nav>
        <button className="theme-button" onClick={() => setTheme((current) => current === 'dark' ? 'light' : 'dark')} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} aria-pressed={theme === 'dark'}>{theme === 'dark' ? '☀ Light' : '◐ Dark'}</button>
        <button className="spend-widget" onClick={() => setUsageOpen(true)} aria-haspopup="dialog">
          <small>AI spend · this month</small>
          <strong>{money(usage.data?.month?.cost_usd)}</strong>
          <span>{usage.data ? `${usage.data.month?.requests ?? 0} requests` : 'Loading usage…'}</span>
        </button>
      </aside>
      <div className="app-main">
        <Routes>
          <Route path="/" element={<JobsPage />} />
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
      {usageOpen && <UsageDialog onClose={() => setUsageOpen(false)} />}
    </div>
  )
}

function UsageDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const backdrop = useRef<HTMLDivElement>(null)
  const usage = useQuery({ queryKey: ['usage'], queryFn: api.usage })
  const pricing = useQuery({ queryKey: ['pricing'], queryFn: api.pricing })
  const [overrides, setOverrides] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => { if (pricing.data) setOverrides(JSON.stringify(pricing.data.overrides ?? {}, null, 2)) }, [pricing.data])
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [onClose])
  const save = useMutation({
    mutationFn: () => api.savePricing(JSON.parse(overrides) as Record<string, unknown>),
    onSuccess: async () => { setNotice('Pricing overrides saved. New rates apply to future requests.'); await queryClient.invalidateQueries({ queryKey: ['pricing'] }) },
  })
  return (
    <div className="dialog-backdrop" ref={backdrop} onMouseDown={(event) => event.target === backdrop.current && onClose()}>
      <section className="dialog usage-dialog" role="dialog" aria-modal="true" aria-labelledby="usage-title">
        <div className="dialog-head"><div><h2 id="usage-title">AI spend</h2><p className="muted">Operational estimates, not provider invoices.</p></div><button className="icon-button" onClick={onClose} aria-label="Close">×</button></div>
        {usage.isPending ? <div className="state-message">Loading usage…</div> : usage.isError ? <div className="callout error">{message(usage.error)}</div> : usage.data && <>
          <div className="usage-cards"><UsageCard label="Today" value={usage.data.today} /><UsageCard label="This month" value={usage.data.month} /><UsageCard label="All time" value={usage.data.all_time} /><div className="usage-card"><small>Tokens</small><strong>{Number((usage.data.all_time.input_tokens ?? 0) + (usage.data.all_time.output_tokens ?? 0)).toLocaleString()}</strong><span>{usage.data.all_time.reported_requests ?? 0} reported · {usage.data.all_time.estimated_requests ?? 0} estimated</span></div></div>
          <div className="usage-groups"><UsageGroup label="By stage" values={usage.data.by_stage} /><UsageGroup label="By provider" values={usage.data.by_provider} /><UsageGroup label="By model" values={usage.data.by_model} /></div>
          <p className="muted">Rate table {usage.data.pricing_version}</p>
        </>}
        <details><summary>Pricing overrides</summary><p className="muted">USD per million tokens. Overrides affect future requests only.</p><textarea className="json-editor" value={overrides} onChange={(event) => setOverrides(event.target.value)} /><button className="button primary" disabled={save.isPending} onClick={() => { try { JSON.parse(overrides); save.mutate() } catch { setNotice('Pricing overrides must be valid JSON.') } }}>Save pricing overrides</button>{(notice || save.isError) && <p className={`status-line ${save.isError ? 'error' : ''}`}>{save.isError ? message(save.error) : notice}</p>}</details>
      </section>
    </div>
  )
}

function UsageCard({ label, value }: { label: string; value?: { cost_usd: number; requests: number } }) { return <div className="usage-card"><small>{label}</small><strong>{money(value?.cost_usd)}</strong><span>{value?.requests ?? 0} requests</span></div> }
function UsageGroup({ label, values }: { label: string; values: Array<{ name: string; cost_usd: number; requests: number }> }) { return <section className="usage-group"><h3>{label}</h3>{values.length ? values.map((value) => <div key={value.name}><span>{value.name}</span><strong>{money(value.cost_usd)}</strong></div>) : <p className="muted">No usage yet.</p>}</section> }
function money(value?: number): string { const amount = Number(value ?? 0); return `$${amount.toFixed(amount > 0 && amount < .01 ? 4 : 2)}` }
function message(error: unknown): string { return error instanceof Error ? error.message : 'Something went wrong.' }
