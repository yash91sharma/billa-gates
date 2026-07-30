/**
 * Screenshot tests for the app shell (sidebar + layout).
 *
 * Renders the Sidebar in isolation with controlled `expanded` state to capture
 * the expanded and collapsed visual states. The mobile overlay variant is
 * captured by shrinking the viewport on the Layout itself.
 */
import { render } from '@testing-library/react'
import { page } from '@vitest/browser/context'
import { capture } from './capture'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, test } from 'vitest'

import Layout from '../components/Layout'
import Sidebar from '../components/Sidebar'

const OUT = '../../screenshots/components'

let cleanup: (() => void) | undefined

// Force a desktop viewport so Layout's md: breakpoint kicks in and the
// sidebar is visible (under 768px it collapses behind a hamburger).
beforeEach(async () => {
  await page.viewport(1280, 900)
})

afterEach(() => {
  cleanup?.()
  cleanup = undefined
})

function renderSidebarOnly(expanded: boolean) {
  // Sidebar lives inside the same `aside` shell Layout would normally provide,
  // so the screenshot matches what users see in the real app.
  return render(
    <MemoryRouter initialEntries={['/jobs']}>
      <div
        style={{
          width: expanded ? 240 : 64,
          height: 480,
          borderRight: '1px solid hsl(var(--border))',
          background: 'hsl(var(--card))',
        }}
      >
        <Sidebar expanded={expanded} onToggle={() => {}} />
      </div>
    </MemoryRouter>
  )
}

test('Sidebar - expanded', async () => {
  const result = renderSidebarOnly(true)
  cleanup = result.unmount
  await capture(`${OUT}/Sidebar--expanded.png`)
})

test('Sidebar - collapsed', async () => {
  const result = renderSidebarOnly(false)
  cleanup = result.unmount
  await capture(`${OUT}/Sidebar--collapsed.png`)
})

test('Layout - desktop default', async () => {
  const result = render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route element={<Layout />}>
          <Route
            path="/"
            element={
              <div style={{ padding: 24 }}>
                <h1 style={{ fontSize: 24, fontWeight: 600 }}>Page content</h1>
                <p>The sidebar lives on the left.</p>
              </div>
            }
          />
        </Route>
      </Routes>
    </MemoryRouter>
  )
  cleanup = result.unmount
  await capture(`${OUT}/Layout--desktop.png`)
})
