import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderOptions } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '../components/ui/tooltip'

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
}

interface WrapperOptions extends RenderOptions {
  route?: string
}

export function renderWithProviders(
  ui: React.ReactElement,
  { route = '/', ...options }: WrapperOptions = {}
) {
  const queryClient = makeQueryClient()
  return render(
    <MemoryRouter initialEntries={[route]}>
      <QueryClientProvider client={queryClient}>
        {/* Mirrors App.tsx: the tooltip provider is app-level, so components
            under test see the same context they see in the real app. */}
        <TooltipProvider delayDuration={200}>{ui}</TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
    options
  )
}

export { screen, waitFor, within, act } from '@testing-library/react'
export { userEvent } from '@testing-library/user-event'
