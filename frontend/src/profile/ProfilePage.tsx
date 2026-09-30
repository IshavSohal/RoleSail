import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { PdfViewer } from '../components/PdfViewer'

type Data = Record<string, unknown>
type SettingsTab = 'profile' | 'searches' | 'resume'

const personalFields = [
  ['Full name', 'personal.full_name'], ['Preferred name', 'personal.preferred_name'],
  ['Email', 'personal.email', 'email'], ['Phone', 'personal.phone'], ['City', 'personal.city'],
  ['Province or state', 'personal.province_state'], ['Country', 'personal.country'],
  ['Postal or ZIP code', 'personal.postal_code'], ['Street address', 'personal.address'],
  ['LinkedIn URL', 'personal.linkedin_url', 'url'], ['GitHub URL', 'personal.github_url', 'url'],
  ['Portfolio URL', 'personal.portfolio_url', 'url'], ['Website URL', 'personal.website_url', 'url'],
] as const

const tagFields = [
  ['Programming languages', 'skills_boundary.programming_languages'],
  ['Frameworks and libraries', 'skills_boundary.frameworks'],
  ['Tools and platforms', 'skills_boundary.tools'],
  ['Companies to preserve', 'resume_facts.preserved_companies'],
  ['Projects to preserve', 'resume_facts.preserved_projects'],
  ['Real metrics', 'resume_facts.real_metrics'],
] as const

const searchTags = [
  ['Allowed countries', 'allowed_countries'], ['Accepted location terms', 'location_accept'],
  ['Rejected non-remote locations', 'location_reject_non_remote'], ['Included target titles', 'include_titles'],
  ['Priority titles', 'priority_titles'], ['Excluded titles', 'exclude_titles'],
] as const

export function ProfilePage() {
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<SettingsTab>('profile')
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const [profile, setProfile] = useState<Data>({})
  const [searches, setSearches] = useState<Data>({})
  const [password, setPassword] = useState('')
  const [dirty, setDirty] = useState(false)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (settings.data && !dirty) {
      setProfile(structuredClone(settings.data.profile))
      setSearches(structuredClone(settings.data.searches))
    }
  }, [dirty, settings.data])
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault() }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirty])

  const saveProfile = useMutation({
    mutationFn: () => {
      const payload = structuredClone(profile)
      if (password) setAtPath(payload, 'personal.password', password)
      return api.saveProfile(payload)
    },
    onSuccess: async () => {
      setPassword(''); setDirty(false); setNotice('Profile saved.')
      await queryClient.invalidateQueries({ queryKey: ['settings'] })
    },
  })
  const saveSearches = useMutation({
    mutationFn: () => api.saveSearches(normalizeSearchNumbers(searches)),
    onSuccess: async () => {
      setDirty(false); setNotice('Job preferences saved.')
      await Promise.all([queryClient.invalidateQueries({ queryKey: ['settings'] }), queryClient.invalidateQueries({ queryKey: ['jobs'] })])
    },
  })

  const updateProfile = (path: string, value: unknown) => { setProfile((current) => withPath(current, path, value)); setDirty(true); setNotice('') }
  const updateSearches = (path: string, value: unknown) => { setSearches((current) => withPath(current, path, value)); setDirty(true); setNotice('') }

  if (settings.isPending || settings.isError) return (
    <div className="profile-page">
      <header className="page-header"><h1>Profile and preferences</h1><p>Manage the information ApplyPilot uses for matching, tailoring, and discovery.</p></header>
      <div className={`page-state ${settings.isError ? 'error' : ''}`}>
        {settings.isError ? message(settings.error) : 'Loading settings…'}
      </div>
    </div>
  )
  return (
    <div className="profile-page">
      <header className="page-header"><h1>Profile and preferences</h1><p>Manage the information ApplyPilot uses for matching, tailoring, and discovery.</p></header>
      <nav className="settings-tabs" aria-label="Profile settings">
        <button className={tab === 'profile' ? 'active' : ''} onClick={() => setTab('profile')}>Personal profile</button>
        <button className={tab === 'searches' ? 'active' : ''} onClick={() => setTab('searches')}>Job preferences</button>
        <button className={tab === 'resume' ? 'active' : ''} onClick={() => setTab('resume')}>Resume</button>
      </nav>
      {tab === 'profile' && <ProfileForm data={profile} password={password} passwordConfigured={Boolean(settings.data?.password_configured)} update={updateProfile} setPassword={(value) => { setPassword(value); setDirty(true) }} onSubmit={() => saveProfile.mutate()} busy={saveProfile.isPending} />}
      {tab === 'searches' && <SearchForm data={searches} update={updateSearches} onSubmit={() => saveSearches.mutate()} busy={saveSearches.isPending} />}
      {tab === 'resume' && <ResumeSettings />}
      {tab !== 'resume' && <div className="sticky-save"><span className={`status-line ${saveProfile.isError || saveSearches.isError ? 'error' : ''}`} role="status">{saveProfile.isError ? message(saveProfile.error) : saveSearches.isError ? message(saveSearches.error) : notice || (dirty ? 'Unsaved changes' : '')}</span><button className="button primary" disabled={!dirty || saveProfile.isPending || saveSearches.isPending} onClick={() => tab === 'profile' ? saveProfile.mutate() : saveSearches.mutate()}>{saveProfile.isPending || saveSearches.isPending ? 'Saving…' : tab === 'profile' ? 'Save profile' : 'Save preferences'}</button></div>}
    </div>
  )
}

function ProfileForm({ data, password, passwordConfigured, update, setPassword, onSubmit, busy }: { data: Data; password: string; passwordConfigured: boolean; update: (path: string, value: unknown) => void; setPassword: (value: string) => void; onSubmit: () => void; busy: boolean }) {
  return (
    <form className="settings-grid" onSubmit={(event) => { event.preventDefault(); onSubmit() }}>
      <SettingsCard title="Personal information" wide><div className="field-grid">{personalFields.map(([label, path, type]) => <BoundField key={path} label={label} path={path} type={type} data={data} update={update} />)}<label className="field wide">Job-site password<input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /><small>{passwordConfigured ? 'A password is configured. Leave blank to keep it.' : 'No password is configured.'}</small></label></div></SettingsCard>
      <SettingsCard title="Work authorization"><Check label="Legally authorized to work" path="work_authorization.legally_authorized_to_work" data={data} update={update} /><Check label="Requires sponsorship" path="work_authorization.require_sponsorship" data={data} update={update} /><SelectField label="Work permit type" path="work_authorization.work_permit_type" options={['', 'Citizen', 'Permanent Resident', 'Open Work Permit', 'Employer-Specific Work Permit', 'Other']} data={data} update={update} /></SettingsCard>
      <SettingsCard title="Compensation"><div className="field-grid"><BoundField label="Salary expectation" path="compensation.salary_expectation" type="number" data={data} update={update} /><SelectField label="Currency" path="compensation.salary_currency" options={['CAD', 'USD', 'EUR', 'GBP', 'AUD']} data={data} update={update} /><BoundField label="Range minimum" path="compensation.salary_range_min" type="number" data={data} update={update} /><BoundField label="Range maximum" path="compensation.salary_range_max" type="number" data={data} update={update} /></div></SettingsCard>
      <SettingsCard title="Experience"><div className="field-grid"><BoundField label="Years of experience" path="experience.years_of_experience_total" type="number" data={data} update={update} /><SelectField label="Education level" path="experience.education_level" options={['', 'High School', 'Associate Degree', "Bachelor's", "Master's", 'PhD', 'Self-taught', 'Other']} data={data} update={update} /><BoundField label="Current title" path="experience.current_title" data={data} update={update} /><BoundField label="Target role" path="experience.target_role" data={data} update={update} /></div></SettingsCard>
      <SettingsCard title="Availability"><BoundField label="Earliest start date" path="availability.earliest_start_date" data={data} update={update} /></SettingsCard>
      <SettingsCard title="Skills and resume facts" wide><div className="field-grid">{tagFields.map(([label, path]) => <TagEditor key={path} label={label} values={asStrings(getAtPath(data, path))} onChange={(value) => update(path, value)} />)}<BoundField label="School to preserve" path="resume_facts.preserved_school" data={data} update={update} /></div></SettingsCard>
      <SettingsCard title="Voluntary EEO information"><SelectField label="Gender" path="eeo_voluntary.gender" options={['Decline to self-identify', 'Woman', 'Man', 'Non-binary', 'Other']} data={data} update={update} /><SelectField label="Race or ethnicity" path="eeo_voluntary.race_ethnicity" options={['Decline to self-identify', 'American Indian or Alaska Native', 'Asian', 'Black or African American', 'Hispanic or Latino', 'Native Hawaiian or Other Pacific Islander', 'White', 'Two or More Races']} data={data} update={update} /><SelectField label="Veteran status" path="eeo_voluntary.veteran_status" options={['Decline to self-identify', 'I am a protected veteran', 'I am not a protected veteran']} data={data} update={update} /><SelectField label="Disability status" path="eeo_voluntary.disability_status" options={['Decline to self-identify', 'Yes, I have a disability', 'No, I do not have a disability']} data={data} update={update} /></SettingsCard>
      <SettingsCard title="Employee outreach" wide><p className="muted">Messages are generated after an application and are never sent without your review.</p><BoundField label="Email signature" path="outreach.signature" textarea data={data} update={update} /><label className="field">Writing samples (separate with a line containing ---)<textarea rows={12} value={asStrings(getAtPath(data, 'outreach.writing_samples')).join('\n---\n')} onChange={(event) => update('outreach.writing_samples', event.target.value.split(/^\s*---\s*$/m).map((value) => value.trim()).filter(Boolean))} /></label><h3>Legacy Apollo delivery schedule</h3><div className="field-grid"><BoundField label="Timezone" path="outreach.schedule.timezone" data={data} update={update} /><BoundField label="Send window starts" path="outreach.schedule.send_window_start" type="time" data={data} update={update} /><BoundField label="Send window ends" path="outreach.schedule.send_window_end" type="time" data={data} update={update} />{[['First-wave recipients', 'first_wave_size', 1, 5], ['Second-wave delay (business days)', 'second_wave_delay_business_days', 1, 30], ['Minimum spacing (minutes)', 'min_spacing_minutes', 1, 1440], ['ApplyPilot daily limit', 'daily_limit', 1, 100]].map(([label, path, min, max]) => <BoundField key={String(path)} label={String(label)} path={`outreach.schedule.${path}`} type="number" min={Number(min)} max={Number(max)} data={data} update={update} />)}</div><Weekdays values={(getAtPath(data, 'outreach.schedule.weekdays') as number[]) ?? []} onChange={(value) => update('outreach.schedule.weekdays', value)} /></SettingsCard>
      <button hidden type="submit" disabled={busy}>Save</button>
    </form>
  )
}

function SearchForm({ data, update, onSubmit, busy }: { data: Data; update: (path: string, value: unknown) => void; onSubmit: () => void; busy: boolean }) {
  const queries = (getAtPath(data, 'queries') as Array<{ query?: string; tier?: number }>) ?? []
  const locations = (getAtPath(data, 'locations') as Array<{ location?: string; remote?: boolean }>) ?? []
  return (
    <form className="settings-grid" onSubmit={(event) => { event.preventDefault(); onSubmit() }}>
      <SettingsCard title="Search defaults" wide><div className="field-grid"><BoundField label="Default location" path="defaults.location" data={data} update={update} />{[['Distance', 'distance'], ['Maximum posting age (hours)', 'hours_old'], ['Results per site', 'results_per_site']].map(([label, key]) => <BoundField key={key} label={label} path={`defaults.${key}`} type="number" data={data} update={update} />)}</div></SettingsCard>
      <SettingsCard title="Discovery filters">{[['Filter Greenhouse locations', 'greenhouse_location_filter'], ['Filter Ashby locations', 'ashby_location_filter'], ['Filter Lever locations', 'lever_location_filter'], ['Filter big-tech locations', 'bigtech_location_filter'], ['Accept remote jobs anywhere', 'accept_remote_anywhere'], ['Accept unknown locations', 'accept_unknown_locations']].map(([label, path]) => <Check key={path} label={label} path={path} data={data} update={update} />)}</SettingsCard>
      <SettingsCard title="Location and title filters">{searchTags.map(([label, path]) => <TagEditor key={path} label={label} values={asStrings(getAtPath(data, path))} onChange={(value) => update(path, value)} />)}</SettingsCard>
      <SettingsCard title="Search queries" wide><EditableRows rows={queries} onChange={(rows) => update('queries', rows)} fields={[{ key: 'query', placeholder: 'Job title' }, { key: 'tier', kind: 'select', options: ['1', '2', '3'] }]} add={{ query: '', tier: 1 }} /></SettingsCard>
      <SettingsCard title="Search locations" wide><EditableRows rows={locations} onChange={(rows) => update('locations', rows)} fields={[{ key: 'location', placeholder: 'City, region, or country' }, { key: 'remote', kind: 'checkbox', label: 'Remote' }]} add={{ location: '', remote: false }} /></SettingsCard>
      <SettingsCard title="Job boards"><label className="field">Boards<textarea rows={7} value={asStrings(getAtPath(data, 'boards')).join('\n')} onChange={(event) => update('boards', event.target.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean))} /><small>One item per line; blank enables direct-employer discovery only.</small></label><BoundField label="Country code or name" path="country" data={data} update={update} /></SettingsCard>
      <button hidden type="submit" disabled={busy}>Save</button>
    </form>
  )
}

function ResumeSettings() {
  const queryClient = useQueryClient()
  const text = useQuery({ queryKey: ['resume', 'txt'], queryFn: () => api.resume('txt') })
  const latex = useQuery({ queryKey: ['resume', 'tex'], queryFn: () => api.resume('tex') })
  const [removeComments, setRemoveComments] = useState(true)
  const [notice, setNotice] = useState('')
  const upload = useMutation({
    mutationFn: async ({ file, remove }: { file: File; remove: boolean }) => {
      if (file.size > 1_000_000) throw new Error('Resume must be 1 MB or smaller.')
      const content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
      return api.saveResume(file.name, content, remove)
    },
    onSuccess: async (result) => { setNotice(`${result.filename ?? 'Resume'} saved.`); await queryClient.invalidateQueries({ queryKey: ['resume'] }) },
  })
  const submit = (event: React.FormEvent<HTMLFormElement>, remove: boolean) => {
    event.preventDefault()
    const input = event.currentTarget.elements.namedItem('resume') as HTMLInputElement
    const file = input.files?.[0]
    if (file) upload.mutate({ file, remove })
  }
  return (
    <div className="settings-grid">
      <SettingsCard title="Plain-text resume" wide>{text.isPending ? <p>Loading…</p> : <pre className="source-preview">{text.data?.content || 'No text resume found.'}</pre>}</SettingsCard>
      <SettingsCard title="Upload a new text resume" wide><form className="upload-row" onSubmit={(event) => submit(event, false)}><input name="resume" type="file" accept=".txt,text/plain" required /><button className="button primary" disabled={upload.isPending}>Upload text resume</button></form></SettingsCard>
      <SettingsCard title="LaTeX resume" wide>{latex.isPending ? <p>Loading…</p> : latex.data?.pdf_available ? <PdfViewer url="/api/resume/pdf" label="Compiled LaTeX resume" /> : <div className="empty-state">No compiled LaTeX resume is available.</div>}<details><summary>View LaTeX source</summary><pre className="source-preview">{latex.data?.content || 'No LaTeX source found.'}</pre></details></SettingsCard>
      <SettingsCard title="Upload a new LaTeX resume" wide><p className="muted">ApplyPilot compiles this file with Tectonic before replacing the current resume.</p><form className="upload-row" onSubmit={(event) => submit(event, removeComments)}><input name="resume" type="file" accept=".tex,text/x-tex,application/x-tex" required /><label className="check"><input type="checkbox" checked={removeComments} onChange={(event) => setRemoveComments(event.target.checked)} /> Remove LaTeX comments</label><button className="button primary" disabled={upload.isPending}>Upload LaTeX resume</button></form></SettingsCard>
      {(notice || upload.isError) && <p className={`status-line ${upload.isError ? 'error' : ''}`} role="status">{upload.isError ? message(upload.error) : notice}</p>}
    </div>
  )
}

function SettingsCard({ title, wide = false, children }: { title: string; wide?: boolean; children: React.ReactNode }) { return <section className={`settings-card ${wide ? 'wide' : ''}`}><h2>{title}</h2>{children}</section> }

function BoundField({ label, path, data, update, type = 'text', textarea = false, min, max }: { label: string; path: string; data: Data; update: (path: string, value: unknown) => void; type?: string; textarea?: boolean; min?: number; max?: number }) {
  const raw = getAtPath(data, path)
  const value = raw == null ? '' : String(raw)
  const onChange = (text: string) => update(path, type === 'number' ? (text === '' ? '' : Number(text)) : text)
  return <label className="field">{label}{textarea ? <textarea rows={3} value={value} onChange={(event) => onChange(event.target.value)} /> : <input type={type} min={min} max={max} step={type === 'number' ? 'any' : undefined} value={value} onChange={(event) => onChange(event.target.value)} />}</label>
}

function SelectField({ label, path, options, data, update }: { label: string; path: string; options: string[]; data: Data; update: (path: string, value: unknown) => void }) { return <label className="field">{label}<select value={String(getAtPath(data, path) ?? '')} onChange={(event) => update(path, event.target.value)}>{options.map((option) => <option key={option} value={option}>{option || 'Not specified'}</option>)}</select></label> }
function Check({ label, path, data, update }: { label: string; path: string; data: Data; update: (path: string, value: unknown) => void }) { return <label className="check"><input type="checkbox" checked={Boolean(getAtPath(data, path))} onChange={(event) => update(path, event.target.checked)} /> {label}</label> }

function TagEditor({ label, values, onChange }: { label: string; values: string[]; onChange: (values: string[]) => void }) {
  const [input, setInput] = useState('')
  const add = () => { const value = input.trim(); if (value && !values.includes(value)) onChange([...values, value]); setInput('') }
  return <div className="field tag-field"><label>{label}</label><div className="tag-list" aria-live="polite">{values.map((value) => <span className="tag" key={value}>{value}<button type="button" aria-label={`Remove ${value}`} onClick={() => onChange(values.filter((item) => item !== value))}>×</button></span>)}</div><div className="tag-controls"><input value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); add() } }} /><button className="button compact" type="button" onClick={add}>Add</button></div></div>
}

function Weekdays({ values, onChange }: { values: number[]; onChange: (days: number[]) => void }) { return <fieldset className="weekday-field"><legend>Sending days</legend>{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day, index) => <label key={day}><input type="checkbox" checked={values.includes(index)} onChange={(event) => onChange(event.target.checked ? [...values, index].sort() : values.filter((value) => value !== index))} /> {day}</label>)}</fieldset> }

interface EditableField { key: string; placeholder?: string; kind?: 'select' | 'checkbox'; options?: string[]; label?: string }
function EditableRows({ rows, onChange, fields, add }: { rows: Array<Record<string, unknown>>; onChange: (rows: Array<Record<string, unknown>>) => void; fields: EditableField[]; add: Record<string, unknown> }) {
  const update = (index: number, key: string, value: unknown) => onChange(rows.map((row, rowIndex) => rowIndex === index ? { ...row, [key]: value } : row))
  return <div className="editable-list">{rows.map((row, index) => <div className="editable-row" key={index}>{fields.map((field) => field.kind === 'select' ? <select key={field.key} aria-label={field.key} value={String(row[field.key] ?? '')} onChange={(event) => update(index, field.key, Number(event.target.value))}>{field.options?.map((value) => <option key={value} value={value}>Tier {value}</option>)}</select> : field.kind === 'checkbox' ? <label className="check" key={field.key}><input type="checkbox" checked={Boolean(row[field.key])} onChange={(event) => update(index, field.key, event.target.checked)} /> {field.label}</label> : <input key={field.key} value={String(row[field.key] ?? '')} placeholder={field.placeholder} onChange={(event) => update(index, field.key, event.target.value)} />)}<button className="button danger compact" type="button" onClick={() => onChange(rows.filter((_, rowIndex) => rowIndex !== index))}>Remove</button></div>)}<button className="button" type="button" onClick={() => onChange([...rows, structuredClone(add)])}>Add row</button></div>
}

function getAtPath(data: Data, path: string): unknown { return path.split('.').reduce<unknown>((current, key) => current && typeof current === 'object' ? (current as Data)[key] : undefined, data) }
function setAtPath(data: Data, path: string, value: unknown): void { const keys = path.split('.'); let current = data; keys.slice(0, -1).forEach((key) => { if (!current[key] || typeof current[key] !== 'object') current[key] = {}; current = current[key] as Data }); current[keys.at(-1)!] = value }
function withPath(data: Data, path: string, value: unknown): Data { const next = structuredClone(data); setAtPath(next, path, value); return next }
function asStrings(value: unknown): string[] { return Array.isArray(value) ? value.map(String) : [] }
function message(error: unknown): string { return error instanceof Error ? error.message : 'Something went wrong.' }
function normalizeSearchNumbers(searches: Data): Data { const result = structuredClone(searches); for (const path of ['defaults.distance', 'defaults.hours_old', 'defaults.results_per_site']) if (getAtPath(result, path) === '') setAtPath(result, path, { __applypilot_delete__: true }); return result }
