import type { ReactNode } from 'react'

function labelFor(key: string): string {
  return key.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

export function JsonValue({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  if (value === null || value === undefined || value === '') return <span className="muted">Not provided</span>
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value !== 'object') return String(value)
  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted">None</span>
    if (value.every((item) => typeof item !== 'object' || item === null)) {
      return <ul>{value.map((item, index) => <li key={index}>{String(item)}</li>)}</ul>
    }
    return <div className="report-stack">{value.map((item, index) => <section className="report-group" key={index}><strong>Item {index + 1}</strong><JsonValue value={item} depth={depth + 1} /></section>)}</div>
  }
  return (
    <dl className={depth ? 'report-fields nested' : 'report-fields'}>
      {Object.entries(value as Record<string, unknown>).map(([key, nested]) => (
        <div className="report-field" key={key}>
          <dt>{labelFor(key)}</dt>
          <dd><JsonValue value={nested} depth={depth + 1} /></dd>
        </div>
      ))}
    </dl>
  )
}

