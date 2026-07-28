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

  it('logo links to home (dashboard) view and calls onNavigate', async () => {
    const onNavigate = vi.fn()
    renderWithProviders(<Sidebar expanded={true} onToggle={() => {}} onNavigate={onNavigate} />)
    const logoLink = screen.getByRole('link', { name: /billa-gates/i })
    expect(logoLink).toHaveAttribute('href', '/')
    await userEvent.click(logoLink)
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })
})
