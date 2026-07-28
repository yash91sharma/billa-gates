import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// The Arctic theme is encoded as CSS custom properties on :root in index.css,
// mapped onto Tailwind's colour namespace by the `@theme inline` block. Those
// are the single source of truth that utilities like `bg-background`,
// `text-foreground`, `border-border` and `ring-ring` resolve to.
//
// We verify the declared values here so a careless edit doesn't silently shift
// the entire UI palette. The values are asserted by hue family rather than
// exactly, so a deliberate nudge is cheap while a wholesale repaint fails.
const cssPath = resolve(__dirname, '../../src/index.css')
const css = readFileSync(cssPath, 'utf-8')

/** The raw value of a token declared on :root — e.g. `hsl(213 92% 67%)`. */
function tokenValue(name: string): string | undefined {
  // Anchored to the start of a line so `--background` cannot be matched inside
  // the `--color-background: var(--background)` mapping in @theme inline.
  const re = new RegExp(`^\\s*--${name}:\\s*([^;]+);`, 'm')
  return css.match(re)?.[1]?.trim()
}

/** The hue, saturation and lightness of an `hsl(h s% l%)` token. */
function hsl(name: string): { h: number; s: number; l: number } | undefined {
  const parts = tokenValue(name)?.match(/hsl\(\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%/)
  if (!parts) return undefined
  return { h: parseFloat(parts[1]), s: parseFloat(parts[2]), l: parseFloat(parts[3]) }
}

describe('Arctic theme tokens (index.css)', () => {
  it('uses the near-white page background', () => {
    expect(hsl('background')).toMatchObject({ h: 210 })
    expect(hsl('background')!.l).toBeGreaterThan(97)
  })

  it('uses pure white card / popover surfaces', () => {
    expect(hsl('card')).toMatchObject({ s: 0, l: 100 })
    expect(hsl('popover')).toMatchObject({ s: 0, l: 100 })
  })

  it('uses soft sky blue as primary accent (#5B9DF9 family)', () => {
    expect(hsl('primary')).toMatchObject({ h: 213 })
    expect(hsl('ring')).toMatchObject({ h: 213 })
  })

  it('uses a cool light grey for borders', () => {
    const border = hsl('border')!
    expect(border.h).toBeGreaterThanOrEqual(210)
    expect(border.h).toBeLessThanOrEqual(216)
    expect(border.l).toBeGreaterThanOrEqual(80)
    expect(border.l).toBeLessThanOrEqual(95)
  })

  it('uses sharp corners (radius <= 0.25rem)', () => {
    const r = tokenValue('radius')
    expect(r).toBeDefined()
    expect(parseFloat((r ?? '0').replace('rem', ''))).toBeLessThanOrEqual(0.25)
  })

  it('derives the whole radius scale from --radius so nothing rounds past it', () => {
    // Tailwind v4 ships a default radius scale (rounded-lg = 0.5rem,
    // rounded-xl = 0.75rem, rounded-4xl = 2rem). The primitives in
    // components/ui use those names freely, so leaving the defaults in place
    // would round the whole app well past the 4px the theme is built on.
    for (const step of ['sm', 'md', 'lg', 'xl']) {
      expect(tokenValue(`radius-${step}`)).toContain('var(--radius)')
    }
  })

  it('declares a slightly tinted sidebar background distinct from the page', () => {
    expect(tokenValue('sidebar')).toBeDefined()
    expect(tokenValue('sidebar')).not.toEqual(tokenValue('background'))
  })

  it('stays a light-only theme', () => {
    // Two halves of the same guarantee. Without the custom variant, Tailwind v4
    // binds `dark:` to prefers-color-scheme — and every primitive in
    // components/ui carries `dark:` classes, so a visitor on a dark-mode OS
    // would get those styles applied over the light palette. Binding it to a
    // `.dark` ancestor that is never mounted keeps them inert.
    expect(css).toMatch(/@custom-variant\s+dark\s*\(&:is\(\.dark \*\)\)/)
    expect(css).not.toMatch(/^\s*\.dark\s*{/m)
  })

  it('sets Geist as the primary body font', () => {
    expect(tokenValue('font-sans')).toMatch(/['"]Geist Variable['"]/)
    expect(css).toMatch(/@import\s+['"]@fontsource-variable\/geist['"]/)
  })

  it('declares a mono stack for snapshot ids and log output', () => {
    expect(tokenValue('font-mono')).toMatch(/ui-monospace/)
  })

  it('declares a heading font, which the card primitive resolves', () => {
    // components/ui/card.tsx styles CardTitle with `font-heading`; without the
    // token that class compiles to nothing.
    expect(tokenValue('font-heading')).toBeDefined()
  })

  it('declares elevation tokens so surfaces are not flat outlines', () => {
    expect(tokenValue('shadow-xs')).toBeDefined()
    expect(tokenValue('shadow-sm')).toBeDefined()
  })

  it('declares subtle status colours so badges stop hardcoding palette classes', () => {
    for (const role of ['success', 'warning', 'danger', 'info', 'neutral', 'verify']) {
      expect(hsl(`${role}-subtle`)).toBeDefined()
      expect(hsl(`${role}-subtle-foreground`)).toBeDefined()
    }
  })
})
