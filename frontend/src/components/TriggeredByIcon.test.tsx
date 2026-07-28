import TriggeredByIcon from './TriggeredByIcon'
// Rendered through the shared harness because the tooltip provider is
// app-level (App.tsx) rather than per-icon — one provider for a whole table,
// not one per row.
import { renderWithProviders as render, screen } from '../test/utils'

// Radix's tooltip uses ResizeObserver — install a no-op stub for jsdom.
beforeAll(() => {
  if (typeof globalThis.ResizeObserver === 'undefined') {
    class StubResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    ;(globalThis as unknown as { ResizeObserver: typeof StubResizeObserver }).ResizeObserver =
      StubResizeObserver
  }
})

describe('TriggeredByIcon', () => {
  it('renders an svg for manual triggers', () => {
    const { container } = render(<TriggeredByIcon value="manual" />)
    expect(container.querySelector('svg')).toBeInTheDocument()
  })

  it('renders an svg for scheduler triggers', () => {
    const { container } = render(<TriggeredByIcon value="scheduler" />)
    expect(container.querySelector('svg')).toBeInTheDocument()
  })

  it('does not show the raw enum value (e.g. "manual") as visible text', () => {
    const { container } = render(<TriggeredByIcon value="manual" />)
    // The trigger element should not have "manual" as visible text — the
    // meaning comes from the icon + tooltip, not a label next to it.
    const trigger = container.querySelector('[data-trigger-by]') as HTMLElement
    expect(trigger).not.toBeNull()
    expect(trigger.textContent?.trim()).toBe('')
  })

  it('exposes a descriptive aria-label so screen readers can announce the meaning', () => {
    render(<TriggeredByIcon value="manual" />)
    expect(screen.getByLabelText(/triggered manually/i)).toBeInTheDocument()
  })

  it('exposes a descriptive aria-label for scheduler triggers', () => {
    render(<TriggeredByIcon value="scheduler" />)
    expect(screen.getByLabelText(/triggered by scheduler/i)).toBeInTheDocument()
  })

  it('uses visually distinct colors for manual vs scheduler so the icons can be told apart at a glance', () => {
    const { container: manualContainer } = render(<TriggeredByIcon value="manual" />)
    const { container: schedContainer } = render(<TriggeredByIcon value="scheduler" />)
    const manualTrigger = manualContainer.querySelector('[data-trigger-by]') as HTMLElement
    const schedTrigger = schedContainer.querySelector('[data-trigger-by]') as HTMLElement
    // Sanity-check both rendered.
    expect(manualTrigger).not.toBeNull()
    expect(schedTrigger).not.toBeNull()
    // The class list must differ — same gray icon for both is the exact bug
    // this component is meant to fix.
    expect(manualTrigger.className).not.toBe(schedTrigger.className)
    // Each must carry a tailwind text-* color utility (not just text-muted-foreground).
    expect(manualTrigger.className).toMatch(/\btext-[a-z]+-\d{3}\b/)
    expect(schedTrigger.className).toMatch(/\btext-[a-z]+-\d{3}\b/)
  })

  it('tags the trigger element with the value so callers can scope queries', () => {
    const { container } = render(<TriggeredByIcon value="manual" />)
    expect(container.querySelector('[data-trigger-by="manual"]')).toBeInTheDocument()
  })
})
