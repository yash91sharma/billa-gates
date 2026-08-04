import { describe, expect, it, vi } from 'vitest'
import Sidebar from './Sidebar'
import { renderWithProviders, screen, userEvent, within } from '../test/utils'

describe('Sidebar', () => {
  it('renders all top-level nav links', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    const nav = screen.getByRole('navigation', { name: /primary/i })
    expect(within(nav).getByRole('link', { name: /dashboard/i })).toBeInTheDocument()
    expect(within(nav).getByRole('link', { name: /jobs/i })).toBeInTheDocument()
    expect(within(nav).getByRole('link', { name: /settings/i })).toBeInTheDocument()
  })

  it('each link points to its route', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    expect(screen.getByRole('link', { name: /dashboard/i })).toHaveAttribute('href', '/')
    expect(screen.getByRole('link', { name: /jobs/i })).toHaveAttribute('href', '/jobs')
    expect(screen.getByRole('link', { name: /settings/i })).toHaveAttribute('href', '/settings')
  })

  it('marks the link matching the current route as active', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />, { route: '/jobs' })
    const jobsLink = screen.getByRole('link', { name: /jobs/i })
    expect(jobsLink).toHaveAttribute('aria-current', 'page')

    const dashLink = screen.getByRole('link', { name: /dashboard/i })
    expect(dashLink).not.toHaveAttribute('aria-current', 'page')
  })

  it('treats nested routes as active for their parent (e.g. /jobs/:id activates Jobs)', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />, {
      route: '/jobs/job-1',
    })
    expect(screen.getByRole('link', { name: /jobs/i })).toHaveAttribute('aria-current', 'page')
  })

  it('renders a toggle button that calls onToggle when clicked', async () => {
    const onToggle = vi.fn()
    renderWithProviders(<Sidebar expanded={true} onToggle={onToggle} />)
    const toggle = screen.getByRole('button', { name: /toggle navigation|collapse|expand/i })
    await userEvent.click(toggle)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('shows text labels when expanded', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    // Labels are real text nodes (not just sr-only) when expanded
    const dashLink = screen.getByRole('link', { name: /dashboard/i })
    expect(dashLink.textContent).toMatch(/dashboard/i)
  })

  it('still exposes accessible labels when collapsed (icons only)', () => {
    renderWithProviders(<Sidebar expanded={false} onToggle={() => {}} />)
    // Links must remain reachable by name even when their visible label is hidden,
    // either via aria-label or a visually-hidden text node.
    expect(screen.getByRole('link', { name: /dashboard/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /jobs/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /settings/i })).toBeInTheDocument()
  })

  it('links to the backup destinations page', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    const nav = screen.getByRole('navigation', { name: /primary/i })
    expect(within(nav).getByRole('link', { name: /destinations/i })).toHaveAttribute(
      'href',
      '/destinations'
    )
  })

  it('marks the destinations link active on its own route only', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />, {
      route: '/destinations',
    })
    expect(screen.getByRole('link', { name: /destinations/i })).toHaveAttribute(
      'aria-current',
      'page'
    )
    expect(screen.getByRole('link', { name: /jobs/i })).not.toHaveAttribute('aria-current', 'page')
  })

  it('logo links to home (dashboard) view and calls onNavigate', async () => {
    const onNavigate = vi.fn()
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} onNavigate={onNavigate} />)
    const logoLink = screen.getByRole('link', { name: /billa-gates/i })
    expect(logoLink).toHaveAttribute('href', '/')
    await userEvent.click(logoLink)
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })
})

describe('Sidebar footer', () => {
  it('shows the running version', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    expect(screen.getByText(`v${__APP_VERSION__}`)).toBeInTheDocument()
  })

  it('links to the GitHub issue form', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    const link = screen.getByRole('link', { name: /report an issue/i })
    const href = link.getAttribute('href') ?? ''
    expect(href).toContain('https://github.com/')
    expect(href).toContain('/issues/new')
  })

  it('opens the issue form in a new tab, without handing it the opener', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    const link = screen.getByRole('link', { name: /report an issue/i })
    expect(link).toHaveAttribute('target', '_blank')
    // noopener is the security half; noreferrer is what stops the new tab
    // learning which page it came from.
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(link.getAttribute('rel')).toContain('noreferrer')
  })

  it('prefills the report with the version that is running', () => {
    // A bug report without a build number costs a round trip before anyone can
    // reproduce it, and the number is right there under the link.
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    const href = screen.getByRole('link', { name: /report an issue/i }).getAttribute('href') ?? ''
    const body = new URL(href).searchParams.get('body') ?? ''
    expect(body).toContain(__APP_VERSION__)
  })

  it('warns assistive tech that the link leaves the app', () => {
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} />)
    expect(screen.getByRole('link', { name: /report an issue.*new tab/i })).toBeInTheDocument()
  })

  it('keeps the issue link reachable by name when collapsed', () => {
    renderWithProviders(<Sidebar expanded={false} onToggle={() => {}} />)
    const link = screen.getByRole('link', { name: /report an issue/i })
    expect(link.getAttribute('href')).toContain('/issues/new')
    // The version stays visible in the rail — it is the reason the footer exists.
    expect(screen.getByText(__APP_VERSION__)).toBeInTheDocument()
  })

  it('does not close the mobile drawer — the issue form opens elsewhere', async () => {
    const onNavigate = vi.fn()
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} onNavigate={onNavigate} />)
    await userEvent.click(screen.getByRole('link', { name: /report an issue/i }))
    expect(onNavigate).not.toHaveBeenCalled()
  })
})
