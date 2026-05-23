import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// The Arctic theme is encoded as CSS custom properties on :root in index.css.
// These are the single source of truth that downstream Tailwind utilities like
// `bg-background`, `text-foreground`, `border-border`, `ring-ring` resolve to.
// We verify the declared HSL values here so a careless edit doesn't silently
// shift the entire UI palette.
const cssPath = resolve(__dirname, '../../src/index.css')
const css = readFileSync(cssPath, 'utf-8')

function tokenValue(name: string): string | undefined {
  // Match `  --name: <hsl values>;` inside :root.
  const re = new RegExp(`--${name}:\\s*([^;]+);`)
  return css.match(re)?.[1]?.trim()
}

describe('Arctic theme tokens (index.css)', () => {
  it('uses the near-white page background', () => {
    // #FAFBFC in HSL ≈ 210 14% 99%. Allow small variance via regex on the leading hue.
    expect(tokenValue('background')).toMatch(/^210\s+/)
  })

  it('uses pure white card / popover surfaces', () => {
    expect(tokenValue('card')).toMatch(/^0 0% 100%$/)
    expect(tokenValue('popover')).toMatch(/^0 0% 100%$/)
  })

  it('uses soft sky blue as primary accent (#5B9DF9 family)', () => {
    // 213 deg hue covers #5B9DF9.
    expect(tokenValue('primary')).toMatch(/^213\s/)
    expect(tokenValue('ring')).toMatch(/^213\s/)
  })

  it('uses a cool light grey for borders', () => {
    expect(tokenValue('border')).toMatch(/^(210|214|216)\s+(\d+%)\s+(8[0-9]|9[0-5])%$/)
  })

  it('uses sharp corners (radius ≤ 0.25rem)', () => {
    const r = tokenValue('radius')
    expect(r).toBeDefined()
    const rem = parseFloat((r ?? '0').replace('rem', ''))
    expect(rem).toBeLessThanOrEqual(0.25)
  })

  it('declares a slightly tinted sidebar background distinct from the page', () => {
    const sidebar = tokenValue('sidebar')
    const bg = tokenValue('background')
    expect(sidebar).toBeDefined()
    expect(sidebar).not.toEqual(bg)
  })

  it('drops dark-mode overrides (light-only theme)', () => {
    expect(css).not.toMatch(/^\s*\.dark\s*{/m)
  })

  it('sets Carlito as the primary body font', () => {
    expect(css).toMatch(/font-family:\s*['"]Carlito['"]/i)
  })
})
