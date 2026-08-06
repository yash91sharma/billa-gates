import { Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Layout from './Layout'
import { act, renderWithProviders, screen, userEvent } from '../test/utils'

/**
 * Layout owns the sidebar + content frame. These tests exercise the full
 * nested-route shape (`<Route element={<Layout/>}>...child routes...</Route>`)
 * so we catch regressions in how the Outlet is wired.
 */
function renderRoutes(initialRoute: string) {
  return renderWithProviders(
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<div>HOME PAGE</div>} />
        <Route path="/jobs" element={<div>JOBS PAGE</div>} />
        <Route path="/settings" element={<div>SETTINGS PAGE</div>} />
      </Route>
    </Routes>,
    { route: initialRoute }
  )
}

describe('Layout', () => {
  it('renders the sidebar nav alongside the routed page', () => {
    renderRoutes('/')
    expect(screen.getByRole('navigation', { name: /primary/i })).toBeInTheDocument()
    expect(screen.getByText('HOME PAGE')).toBeInTheDocument()
  })

  it('clicking a nav link routes to the matching page', async () => {
    renderRoutes('/')
    await userEvent.click(screen.getByRole('link', { name: /jobs/i }))
    expect(await screen.findByText('JOBS PAGE')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('link', { name: /settings/i }))
    expect(await screen.findByText('SETTINGS PAGE')).toBeInTheDocument()
  })

  it('toggle button collapses and re-expands the sidebar', async () => {
    renderRoutes('/')
    const sidebar = screen.getByRole('complementary', { name: /sidebar/i })
    const toggle = screen.getByRole('button', { name: /toggle navigation|collapse|expand/i })

    // Start expanded — sidebar carries data-expanded="true"
    expect(sidebar).toHaveAttribute('data-expanded', 'true')

    await userEvent.click(toggle)
    expect(sidebar).toHaveAttribute('data-expanded', 'false')

    await userEvent.click(toggle)
    expect(sidebar).toHaveAttribute('data-expanded', 'true')
  })
})

/**
 * The sidebar preference, the mobile drawer and the resize handler were all
 * unexercised (57% of Layout's functions). Each is a behaviour someone
 * deliberately added:
 *
 *  - the expanded/collapsed rail is persisted because it used to be plain
 *    component state, so every reload re-expanded it;
 *  - the drawer and its backdrop are the only navigation that exists below
 *    768px — the rail is translated off-screen there;
 *  - the resize listener exists so growing past the breakpoint doesn't leave a
 *    stale overlay covering the page.
 */

const STORAGE_KEY = 'billa-gates:sidebar-expanded'

describe('Layout: the sidebar preference', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('starts expanded when nothing has been stored', () => {
    renderRoutes('/')

    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-expanded',
      'true'
    )
  })

  it('restores a collapsed rail from a previous visit', () => {
    window.localStorage.setItem(STORAGE_KEY, 'false')

    renderRoutes('/')

    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-expanded',
      'false'
    )
  })

  it('treats any other stored value as expanded', () => {
    // Only the exact string 'false' collapses it, so a corrupted or
    // half-written value fails open rather than hiding the navigation.
    window.localStorage.setItem(STORAGE_KEY, 'garbage')

    renderRoutes('/')

    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-expanded',
      'true'
    )
  })

  it('persists the collapse so the next visit keeps it', async () => {
    renderRoutes('/')
    const toggle = screen.getByRole('button', { name: /toggle navigation|collapse|expand/i })

    await userEvent.click(toggle)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('false')

    await userEvent.click(toggle)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('true')
  })

  it('still renders when localStorage reads throw', () => {
    // Safari private mode throws on access. The preference is not worth
    // failing the whole app shell over.
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })

    try {
      renderRoutes('/')

      expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
        'data-expanded',
        'true'
      )
      expect(screen.getByText('HOME PAGE')).toBeInTheDocument()
    } finally {
      spy.mockRestore()
    }
  })

  it('still toggles when localStorage writes throw', async () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })

    try {
      renderRoutes('/')
      const sidebar = screen.getByRole('complementary', { name: /sidebar/i })

      await userEvent.click(
        screen.getByRole('button', { name: /toggle navigation|collapse|expand/i })
      )

      // The preference is lost for the next visit, but the control works now.
      expect(sidebar).toHaveAttribute('data-expanded', 'false')
    } finally {
      spy.mockRestore()
    }
  })
})

describe('Layout: the mobile drawer', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('is closed on first render', () => {
    renderRoutes('/')

    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-mobile-open',
      'false'
    )
  })

  it('opens from the header hamburger', async () => {
    renderRoutes('/')

    await userEvent.click(screen.getByRole('button', { name: /open navigation/i }))

    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-mobile-open',
      'true'
    )
  })

  it('closes when the backdrop is tapped', async () => {
    const { container } = renderRoutes('/')
    await userEvent.click(screen.getByRole('button', { name: /open navigation/i }))

    const backdrop = container.querySelector('[aria-hidden="true"].fixed')
    expect(backdrop).not.toBeNull()
    await userEvent.click(backdrop as Element)

    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-mobile-open',
      'false'
    )
  })

  it('has no backdrop while closed, so it cannot swallow clicks', () => {
    const { container } = renderRoutes('/')

    expect(container.querySelector('[aria-hidden="true"].fixed')).toBeNull()
  })

  it('closes after navigating, so the drawer does not cover the new page', async () => {
    renderRoutes('/')
    await userEvent.click(screen.getByRole('button', { name: /open navigation/i }))

    await userEvent.click(screen.getByRole('link', { name: /jobs/i }))

    expect(await screen.findByText('JOBS PAGE')).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-mobile-open',
      'false'
    )
  })

  it('closes when the viewport grows past the breakpoint', async () => {
    renderRoutes('/')
    await userEvent.click(screen.getByRole('button', { name: /open navigation/i }))

    await act(async () => {
      window.innerWidth = 1280
      window.dispatchEvent(new Event('resize'))
    })

    // Otherwise rotating a tablet leaves a full-screen overlay over the app
    // with its dismiss target (the backdrop) hidden by `md:hidden`.
    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-mobile-open',
      'false'
    )
  })

  it('stays open when the viewport is resized but stays below the breakpoint', async () => {
    renderRoutes('/')
    await userEvent.click(screen.getByRole('button', { name: /open navigation/i }))

    await act(async () => {
      window.innerWidth = 500
      window.dispatchEvent(new Event('resize'))
    })

    expect(screen.getByRole('complementary', { name: /sidebar/i })).toHaveAttribute(
      'data-mobile-open',
      'true'
    )
  })

  it('removes its resize listener on unmount', () => {
    const remove = vi.spyOn(window, 'removeEventListener')

    const { unmount } = renderRoutes('/')
    unmount()

    // A listener left behind calls setState on an unmounted tree.
    expect(remove).toHaveBeenCalledWith('resize', expect.any(Function))
    remove.mockRestore()
  })
})
