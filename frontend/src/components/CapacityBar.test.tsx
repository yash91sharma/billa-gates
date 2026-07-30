import { render, screen } from '@testing-library/react'
import CapacityBar, { CAPACITY_TONE_THRESHOLDS } from './CapacityBar'

describe('CapacityBar', () => {
  it('exposes the value to assistive tech as a progressbar', () => {
    render(<CapacityBar percent={42} label="main capacity" />)
    const bar = screen.getByRole('progressbar', { name: /main capacity/i })
    expect(bar).toHaveAttribute('aria-valuenow', '42')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
  })

  it('renders the percentage as text, so colour is never the only signal', () => {
    render(<CapacityBar percent={42} label="main capacity" />)
    expect(screen.getByText('42.0%')).toBeInTheDocument()
  })

  it('sets the fill width from the value', () => {
    render(<CapacityBar percent={42} label="main capacity" />)
    // A dynamic width has to be an inline style: `w-[42%]` is invisible to
    // Tailwind v4's content scanner and would compile to nothing.
    expect(screen.getByTestId('capacity-bar-fill')).toHaveStyle({ width: '42%' })
  })

  it('clamps a value above 100 rather than overflowing its track', () => {
    render(<CapacityBar percent={140} label="main capacity" />)
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
    expect(screen.getByTestId('capacity-bar-fill')).toHaveStyle({ width: '100%' })
  })

  it('clamps a negative value to zero', () => {
    render(<CapacityBar percent={-5} label="main capacity" />)
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0')
    expect(screen.getByTestId('capacity-bar-fill')).toHaveStyle({ width: '0%' })
  })

  it('renders an unknown state with no value and no fill', () => {
    // A destination that could not be measured must not render as 0% full,
    // which is a claim about the drive rather than about the reading.
    render(<CapacityBar percent={null} label="nas capacity" />)
    const bar = screen.getByRole('progressbar', { name: /nas capacity/i })
    expect(bar).not.toHaveAttribute('aria-valuenow')
    expect(bar).toHaveAttribute('aria-valuetext', 'unknown')
    expect(screen.queryByTestId('capacity-bar-fill')).not.toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  describe('tone thresholds', () => {
    it('is calm below the warning threshold', () => {
      render(<CapacityBar percent={CAPACITY_TONE_THRESHOLDS.warning - 0.1} label="c" />)
      expect(screen.getByTestId('capacity-bar-fill').className).toContain('bg-success')
    })

    it('warns from the warning threshold up', () => {
      render(<CapacityBar percent={CAPACITY_TONE_THRESHOLDS.warning} label="c" />)
      expect(screen.getByTestId('capacity-bar-fill').className).toContain('bg-warning')
    })

    it('alarms from the danger threshold up', () => {
      render(<CapacityBar percent={CAPACITY_TONE_THRESHOLDS.danger} label="c" />)
      expect(screen.getByTestId('capacity-bar-fill').className).toContain('bg-destructive')
    })

    it('alarms at a full drive', () => {
      render(<CapacityBar percent={100} label="c" />)
      expect(screen.getByTestId('capacity-bar-fill').className).toContain('bg-destructive')
    })
  })
})
