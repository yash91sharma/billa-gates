import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'sonner'
import App from './App'
import './index.css'

const queryClient = new QueryClient()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>
        <App />
        {/*
          Transient confirmations only — "it worked, carry on". Anything the
          user has to act on (a validation message, a failed create, a rename
          that was refused) stays inline next to the control that caused it,
          where they are already looking and where it survives longer than a
          toast's few seconds.
        */}
        <Toaster
          position="bottom-right"
          toastOptions={{
            classNames: {
              toast:
                'rounded-lg border border-border bg-card text-card-foreground shadow-sm text-sm',
              description: 'text-muted-foreground',
            },
          }}
        />
      </QueryClientProvider>
    </BrowserRouter>
  </React.StrictMode>
)

// Register the PWA service worker so the app is installable on mobile/tablet.
// Only in production builds — the dev server does not serve /sw.js, and we do
// not want asset caching during development.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
