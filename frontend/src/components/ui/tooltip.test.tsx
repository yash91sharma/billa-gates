import { render, screen } from '@testing-library/react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './tooltip'

// Radix's tooltip primitive uses ResizeObserver internally; jsdom doesn't ship
// one, so we install a no-op stub before the component mounts.
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

// Open a tooltip immediately so we can inspect its rendered content classes.
// Radix only mounts TooltipContent when the tooltip is open, so we rely on
// `defaultOpen` to force it without driving hover events.
function renderOpenTooltip(content: string = 'help text') {
  return render(
    <TooltipProvider>
      <Tooltip defaultOpen>
        <TooltipTrigger>Trigger</TooltipTrigger>
        <TooltipContent>{content}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

describe('TooltipContent', () => {
  it('renders the content text', async () => {
    renderOpenTooltip('a helpful tooltip')
    // There may be multiple matching nodes (one in portal, one as aria-describedby reference);
    // we only need to know the text is in the DOM somewhere.
    expect(screen.getAllByText('a helpful tooltip').length).toBeGreaterThan(0)
  })

  it('uses a solid (opaque) popover background — not transparent', () => {
    renderOpenTooltip()
    const el = document.querySelector('[data-slot="tooltip-content"]') as HTMLElement
    expect(el).not.toBeNull()
    // bg-popover is the shadcn token that resolves to a fully opaque card-like surface.
    expect(el.className).toMatch(/\bbg-popover\b/)
    // Belt-and-suspenders: explicit text colour so contrast is locked in.
    expect(el.className).toMatch(/\btext-popover-foreground\b/)
  })

  it('has a visible border', () => {
    renderOpenTooltip()
    const el = document.querySelector('[data-slot="tooltip-content"]') as HTMLElement
    // Tailwind's `border` utility (1px) plus a token-driven colour.
    expect(el.className).toMatch(/(^|\s)border(\s|$)/)
    expect(el.className).toMatch(/\bborder-border\b/)
  })

  it('has a soft drop shadow for depth', () => {
    renderOpenTooltip()
    const el = document.querySelector('[data-slot="tooltip-content"]') as HTMLElement
    expect(el.className).toMatch(/\bshadow-(sm|md|lg)\b/)
  })

  it('uses readable text sizing and padding so help text is easy to scan', () => {
    renderOpenTooltip()
    const el = document.querySelector('[data-slot="tooltip-content"]') as HTMLElement
    expect(el.className).toMatch(/\btext-(xs|sm)\b/)
    expect(el.className).toMatch(/\bpx-/)
    expect(el.className).toMatch(/\bpy-/)
  })
})
