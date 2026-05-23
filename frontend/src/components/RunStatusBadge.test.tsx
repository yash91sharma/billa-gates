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

  it('renders "pending" text for null status (check not yet complete)', () => {
    render(<RunStatusBadge status={null} />)
    expect(screen.getByText('pending')).toBeInTheDocument()
  })

  it('renders "passed" text for check passed status', () => {
    render(<RunStatusBadge status="passed" />)
    expect(screen.getByText('passed')).toBeInTheDocument()
  })

  // Soft pastel pairs from the Arctic theme: bg-<tone>-100 + text-<tone>-800.
  it('applies pastel green styling for success', () => {
    render(<RunStatusBadge status="success" />)
    const badge = screen.getByText('success')
    expect(badge.className).toMatch(/\bbg-green-100\b/)
    expect(badge.className).toMatch(/\btext-green-800\b/)
  })

  it('applies pastel red styling for failed', () => {
    render(<RunStatusBadge status="failed" />)
    const badge = screen.getByText('failed')
    expect(badge.className).toMatch(/\bbg-red-100\b/)
    expect(badge.className).toMatch(/\btext-red-800\b/)
  })

  it('applies pastel blue styling for running (Arctic-theme accent family)', () => {
    render(<RunStatusBadge status="running" />)
    const badge = screen.getByText('running')
    expect(badge.className).toMatch(/\bbg-blue-100\b/)
    expect(badge.className).toMatch(/\btext-blue-800\b/)
  })

  it('applies muted slate styling for skipped', () => {
    render(<RunStatusBadge status="skipped" />)
    const badge = screen.getByText('skipped')
    expect(badge.className).toMatch(/\bbg-slate-100\b/)
    expect(badge.className).toMatch(/\btext-slate-(600|700)\b/)
  })

  it('applies muted slate styling for pending (null)', () => {
    render(<RunStatusBadge status={null} />)
    const badge = screen.getByText('pending')
    expect(badge.className).toMatch(/\bbg-slate-100\b/)
    expect(badge.className).toMatch(/\btext-slate-(500|600)\b/)
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
})
