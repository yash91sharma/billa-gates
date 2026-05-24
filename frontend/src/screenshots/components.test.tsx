/**
 * Screenshot tests for individual components in isolation.
 *
 * These tests render each component into a real Chromium (via Vitest browser
 * mode + Playwright) and write a PNG to ../../screenshots/components/.
 * Their purpose is artifact generation for visual review — they don't make
 * behavioural assertions (that's what the *.test.tsx files in pages/components
 * are for).
 *
 * Add or remove screenshots by editing the SCENARIOS list below.
 */
import { render } from '@testing-library/react'
import { page } from '@vitest/browser/context'
import { afterEach, test } from 'vitest'

import FieldLabel from '../components/FieldLabel'
import RunStatusBadge from '../components/RunStatusBadge'
import ScheduleInput from '../components/ScheduleInput'
import SnapshotList from '../components/SnapshotList'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../components/ui/tooltip'
import type { Snapshot } from '../lib/types'

// All paths are relative to this test file; ../../screenshots resolves to
// frontend/screenshots.
const OUT = '../../screenshots/components'

let cleanup: (() => void) | undefined

afterEach(() => {
  cleanup?.()
  cleanup = undefined
})

// ── RunStatusBadge — one PNG per status value ────────────────────────────────

const STATUSES: Array<'running' | 'success' | 'failed' | 'skipped'> = [
  'running',
  'success',
  'failed',
  'skipped',
]

for (const status of STATUSES) {
  test(`RunStatusBadge - ${status}`, async () => {
    const result = render(<RunStatusBadge status={status} />)
    cleanup = result.unmount
    await page.screenshot({ path: `${OUT}/RunStatusBadge--${status}.png` })
  })
}

test('RunStatusBadge - null (pending)', async () => {
  const result = render(<RunStatusBadge status={null} />)
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/RunStatusBadge--pending.png` })
})

// ── ScheduleInput — interval and cron modes ──────────────────────────────────

test('ScheduleInput - interval empty', async () => {
  const result = render(
    <ScheduleInput value={{ type: 'interval', value: '' }} onChange={() => {}} />
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/ScheduleInput--interval-empty.png` })
})

test('ScheduleInput - interval valid 6h', async () => {
  const result = render(
    <ScheduleInput value={{ type: 'interval', value: '6h' }} onChange={() => {}} />
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/ScheduleInput--interval-6h.png` })
})

test('ScheduleInput - cron mode', async () => {
  const result = render(
    <ScheduleInput value={{ type: 'cron', value: '0 3 * * *' }} onChange={() => {}} />
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/ScheduleInput--cron.png` })
})

// ── SnapshotList — empty vs populated ────────────────────────────────────────

test('SnapshotList - empty', async () => {
  const result = render(<SnapshotList snapshots={[]} />)
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/SnapshotList--empty.png` })
})

// ── Tooltip — opened, with FieldLabel for context ────────────────────────────

test('FieldLabel - tooltip open', async () => {
  // Render the field label next to a real input, with the tooltip forced open
  // so the new styling (opaque popover, border, shadow) is captured along with
  // the full help structure: description, Options, Default, Example.
  const result = render(
    <TooltipProvider>
      <div style={{ padding: 80 }}>
        <Tooltip defaultOpen>
          <TooltipTrigger asChild>
            <button type="button" aria-label="More info">
              ⓘ
            </button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              Options: auto | max | off. <code>auto</code> compresses compressible data.{' '}
              <code>max</code> tries harder but is slower. <code>off</code> disables compression.
              Default: auto.
            </p>
            <p>
              <span style={{ fontWeight: 600 }}>Example:</span> auto
            </p>
          </TooltipContent>
        </Tooltip>
      </div>
    </TooltipProvider>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/Tooltip--open.png` })
})

test('FieldLabel - row preview', async () => {
  // Just the label row composition: label + (optional) chip + info icon.
  const result = render(
    <TooltipProvider>
      <div style={{ padding: 24, width: 360 }}>
        <FieldLabel
          htmlFor="example-field"
          help={{
            label: 'Keep Last',
            optional: true,
            description: 'Keep the N most recent snapshots regardless of age.',
            example: '5',
          }}
        />
        <input
          id="example-field"
          type="number"
          aria-describedby="example-field-help"
          className="border rounded px-2 py-1 text-sm w-full"
        />
      </div>
    </TooltipProvider>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/FieldLabel--row.png` })
})

test('SnapshotList - populated', async () => {
  const snaps: Snapshot[] = [
    {
      snapshot_id: 'a'.repeat(64),
      snapshot_time: '2026-05-19T10:30:00Z',
      hostname: 'home-server',
      paths: ['/sources/documents'],
      tags: ['weekly'],
      size_bytes: 1_073_741_824,
    },
    {
      snapshot_id: 'b'.repeat(64),
      snapshot_time: '2026-05-18T10:30:00Z',
      hostname: 'home-server',
      paths: ['/sources/documents'],
      tags: null,
      size_bytes: 524_288_000,
    },
  ]
  const result = render(<SnapshotList snapshots={snaps} />)
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/SnapshotList--populated.png` })
})
