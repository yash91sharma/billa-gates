import { render, screen } from '@testing-library/react'
import RunStatusBadge from './RunStatusBadge'

describe('RunStatusBadge', () => {
  it('renders "running" text for running status', () => {
    render(<RunStatusBadge status="running" />)
    expect(screen.getByText('running')).toBeInTheDocument()
  })

  it('renders "success" text for success status', () => {
    render(<RunStatusBadge status="success" />)
    expect(screen.getByText('success')).toBeInTheDocument()
  })

  it('renders "failed" text for failed status', () => {
    render(<RunStatusBadge status="failed" />)
    expect(screen.getByText('failed')).toBeInTheDocument()
  })

  it('renders "skipped" text for skipped status', () => {
    render(<RunStatusBadge status="skipped" />)
    expect(screen.getByText('skipped')).toBeInTheDocument()
  })

  it('renders "warning" text for warning status (rc=3, partial backup)', () => {
    render(<RunStatusBadge status="warning" />)
    expect(screen.getByText('warning')).toBeInTheDocument()
  })

  it('applies pastel amber styling for warning', () => {
    render(<RunStatusBadge status="warning" />)
    const badge = screen.getByText('warning')
    expect(badge.className).toMatch(/\bbg-warning-subtle\b/)
    expect(badge.className).toMatch(/\btext-warning-subtle-foreground\b/)
  })

  it('renders "pending" text for null status (check not yet complete)', () => {
    render(<RunStatusBadge status={null} />)
    expect(screen.getByText('pending')).toBeInTheDocument()
  })

  it('renders "passed" text for check passed status', () => {
    render(<RunStatusBadge status="passed" />)
    expect(screen.getByText('passed')).toBeInTheDocument()
  })

  // Theme tokens, not palette classes. Pinning `bg-green-100` here is what let
  // the badge set drift from the Dashboard's kind badges — one idea, two
  // hardcoded colours, nothing tying them together.
  it('applies pastel green styling for success', () => {
    render(<RunStatusBadge status="success" />)
    const badge = screen.getByText('success')
    expect(badge.className).toMatch(/\bbg-success-subtle\b/)
    expect(badge.className).toMatch(/\btext-success-subtle-foreground\b/)
  })

  it('applies pastel red styling for failed', () => {
    render(<RunStatusBadge status="failed" />)
    const badge = screen.getByText('failed')
    expect(badge.className).toMatch(/\bbg-danger-subtle\b/)
    expect(badge.className).toMatch(/\btext-danger-subtle-foreground\b/)
  })

  it('applies pastel blue styling for running (Arctic-theme accent family)', () => {
    render(<RunStatusBadge status="running" />)
    const badge = screen.getByText('running')
    expect(badge.className).toMatch(/\bbg-info-subtle\b/)
    expect(badge.className).toMatch(/\btext-info-subtle-foreground\b/)
  })

  it('applies muted slate styling for skipped', () => {
    render(<RunStatusBadge status="skipped" />)
    const badge = screen.getByText('skipped')
    expect(badge.className).toMatch(/\bbg-neutral-subtle\b/)
    expect(badge.className).toMatch(/\btext-neutral-subtle-foreground\b/)
  })

  it('applies muted slate styling for pending (null)', () => {
    render(<RunStatusBadge status={null} />)
    const badge = screen.getByText('pending')
    expect(badge.className).toMatch(/\bbg-neutral-subtle\b/)
    expect(badge.className).toMatch(/\btext-neutral-subtle-foreground\b/)
  })

  it('accepts an additional className prop', () => {
    render(<RunStatusBadge status="success" className="extra-class" />)
    const badge = screen.getByText('success')
    expect(badge.className).toContain('extra-class')
  })

  it('renders as an inline element (span or similar)', () => {
    render(<RunStatusBadge status="success" />)
    const badge = screen.getByText('success')
    expect(['SPAN', 'DIV', 'BADGE']).toContain(badge.tagName)
  })

  it('renders "canceled" text for canceled status', () => {
    render(<RunStatusBadge status="canceled" />)
    expect(screen.getByText('canceled')).toBeInTheDocument()
  })

  it('applies muted slate styling for canceled', () => {
    render(<RunStatusBadge status="canceled" />)
    const badge = screen.getByText('canceled')
    expect(badge.className).toMatch(/\bbg-neutral-subtle\b/)
    expect(badge.className).toMatch(/\btext-neutral-subtle-foreground\b/)
  })
})
