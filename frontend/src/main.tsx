import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import './styles.css'

function migrateLegacyHash() {
  const hash = window.location.hash
  if (!hash) return
  if (hash === '#profile') history.replaceState(null, '', '/profile')
  else if (hash === '#needs_drafts' || hash === '#applied') history.replaceState(null, '', '/?bucket=needs_drafts')
  else if (hash === '#drafts_done') history.replaceState(null, '', '/?bucket=drafts_done')
}

migrateLegacyHash()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 2_000, retry: 1, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter><App /></BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)

