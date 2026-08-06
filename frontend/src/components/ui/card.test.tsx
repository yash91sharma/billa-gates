import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from './card'

/**
 * `CardTitle`'s `as` prop is a local addition to the upstream shadcn
 * component, and it is the only part of this file that carries a contract
 * rather than styling.
 *
 * Upstream renders every card title as a `<div>`. A screen-reader user skims
 * a page by its heading outline, so on pages built out of cards — Dashboard,
 * Destinations — a `div` title means the whole page has no structure to skim:
 * the sections are visually obvious and completely invisible to the outline.
 * `as` opts a card into contributing a real heading, and defaults to `div` so
 * decorative cards don't inject stray ones (a heading that titles nothing is
 * its own navigation hazard).
 *
 * None of this can fail a snapshot or a style test — the jsdom suite runs with
 * `css: false` — so it is asserted through the accessibility tree, which is
 * where the behaviour actually lives.
 */
describe('CardTitle', () => {
  it('renders a plain div by default, contributing no heading', () => {
    render(<CardTitle>Recent Runs</CardTitle>)

    expect(screen.queryByRole('heading')).not.toBeInTheDocument()
    expect(screen.getByText('Recent Runs').tagName).toBe('DIV')
  })

  it('renders a level-2 heading when asked', () => {
    render(<CardTitle as="h2">Recent Runs</CardTitle>)

    const heading = screen.getByRole('heading', { level: 2, name: 'Recent Runs' })
    expect(heading.tagName).toBe('H2')
  })

  it('renders a level-3 heading when asked', () => {
    render(<CardTitle as="h3">Backup Destinations</CardTitle>)

    expect(
      screen.getByRole('heading', { level: 3, name: 'Backup Destinations' })
    ).toBeInTheDocument()
  })

  it('keeps its slot and styling whichever element it renders', () => {
    // The `as` switch must not cost the card its appearance — otherwise the
    // accessible version and the styled version are different components and
    // nobody uses the accessible one.
    const { rerender } = render(<CardTitle>Title</CardTitle>)
    const asDiv = screen.getByText('Title')
    const divClass = asDiv.className

    rerender(<CardTitle as="h2">Title</CardTitle>)
    const asHeading = screen.getByText('Title')

    expect(asHeading).toHaveAttribute('data-slot', 'card-title')
    expect(asHeading.className).toBe(divClass)
  })

  it('passes arbitrary props through to the rendered element', () => {
    render(
      <CardTitle as="h2" id="runs-heading" className="custom">
        Runs
      </CardTitle>
    )

    const heading = screen.getByRole('heading', { name: 'Runs' })
    expect(heading).toHaveAttribute('id', 'runs-heading')
    // `cn` merges rather than replaces, so the component's own classes survive.
    expect(heading.className).toContain('custom')
    expect(heading.className).toContain('font-heading')
  })
})

describe('Card composition', () => {
  it('renders a full card with every slot marked', () => {
    // The slots are what the container-query styling keys on
    // (`has-data-[slot=card-footer]`, `has-data-[slot=card-action]`), so a
    // renamed slot silently drops the layout rules that depend on it.
    const { container } = render(
      <Card>
        <CardHeader>
          <CardTitle as="h2">Photos</CardTitle>
          <CardDescription>Last run 2 hours ago</CardDescription>
          <CardAction>
            <button>Run now</button>
          </CardAction>
        </CardHeader>
        <CardContent>content</CardContent>
        <CardFooter>footer</CardFooter>
      </Card>
    )

    for (const slot of [
      'card',
      'card-header',
      'card-title',
      'card-description',
      'card-action',
      'card-content',
      'card-footer',
    ]) {
      expect(container.querySelector(`[data-slot="${slot}"]`), slot).not.toBeNull()
    }
  })

  it('defaults to the regular size and accepts the compact one', () => {
    // `size` drives the padding via `data-[size=sm]` descendant selectors; a
    // missing attribute leaves a compact card rendering at full padding.
    const { container, rerender } = render(<Card>body</Card>)
    expect(container.querySelector('[data-slot="card"]')).toHaveAttribute('data-size', 'default')

    rerender(<Card size="sm">body</Card>)
    expect(container.querySelector('[data-slot="card"]')).toHaveAttribute('data-size', 'sm')
  })

  it('keeps the heading inside the card it titles', () => {
    render(
      <Card>
        <CardHeader>
          <CardTitle as="h2">Destinations</CardTitle>
        </CardHeader>
      </Card>
    )

    const heading = screen.getByRole('heading', { name: 'Destinations' })
    expect(heading.closest('[data-slot="card"]')).not.toBeNull()
  })
})
