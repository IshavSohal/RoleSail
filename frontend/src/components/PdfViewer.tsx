import { useEffect, useRef, useState } from 'react'

interface PdfViewerProps {
  url: string
  label: string
}

export function PdfViewer({ url, label }: PdfViewerProps) {
  const container = useRef<HTMLDivElement>(null)
  const [scale, setScale] = useState(1.2)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const current = container.current
    if (!current) return
    current.replaceChildren()
    setError('')
    setLoading(true)
    let destroy: (() => Promise<void>) | undefined
    void Promise.all([
      import('pdfjs-dist'),
      import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
    ]).then(([pdfjs, worker]) => {
      pdfjs.GlobalWorkerOptions.workerSrc = worker.default
      const task = pdfjs.getDocument({ url, withCredentials: false })
      destroy = () => task.destroy()
      return task.promise
    }).then(async (pdf) => {
      for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
        if (cancelled) return
        const page = await pdf.getPage(pageNumber)
        const viewport = page.getViewport({ scale })
        const canvas = document.createElement('canvas')
        canvas.className = 'pdf-page'
        canvas.width = viewport.width
        canvas.height = viewport.height
        canvas.setAttribute('aria-label', `${label}, page ${pageNumber}`)
        current.append(canvas)
        const context = canvas.getContext('2d')
        if (context) await page.render({ canvas, canvasContext: context, viewport }).promise
      }
      if (!cancelled) setLoading(false)
    }).catch((reason: unknown) => {
      if (!cancelled) {
        setError(reason instanceof Error ? reason.message : 'Could not render this PDF.')
        setLoading(false)
      }
    })
    return () => {
      cancelled = true
      if (destroy) void destroy()
    }
  }, [label, scale, url])

  return (
    <div className="pdf-shell">
      <div className="pdf-toolbar">
        <span>{label}</span>
        <span className="button-row">
          <button className="button subtle" onClick={() => setScale((value) => Math.max(.7, value - .15))} aria-label="Zoom out">−</button>
          <span>{Math.round(scale * 100)}%</span>
          <button className="button subtle" onClick={() => setScale((value) => Math.min(2, value + .15))} aria-label="Zoom in">+</button>
          <a className="button subtle" href={url} download>Download</a>
        </span>
      </div>
      {loading && <div className="state-message">Rendering PDF…</div>}
      {error && <div className="callout error">{error} <a href={url} download>Download instead</a></div>}
      <div className="pdf-pages" ref={container} />
    </div>
  )
}
