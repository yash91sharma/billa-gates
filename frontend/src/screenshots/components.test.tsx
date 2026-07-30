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

import { HardDriveDownload } from 'lucide-react'
import { MemoryRouter } from 'react-router-dom'
import CapacityBar from '../components/CapacityBar'
import EmptyState from '../components/EmptyState'
import FieldLabel from '../components/FieldLabel'
import PageHeader from '../components/PageHeader'
import RunStatusBadge from '../components/RunStatusBadge'
import ScheduleInput from '../components/ScheduleInput'
import SnapshotList from '../components/SnapshotList'
import { Button } from '../components/ui/button'
import { Skeleton } from '../components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '../components/ui/table'
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

const STATUSES: Array<'running' | 'success' | 'warning' | 'failed' | 'skipped' | 'canceled'> = [
  'running',
  'success',
  'warning',
  'failed',
  'skipped',
  'canceled',
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

// ── The shared shell + state components ──────────────────────────────────────
//
// These are wide by nature (a page header, a table, a full-width empty state),
// so they need a viewport that fits them — the default crops the copy. Set at
// the end of the file so the badge and field captures above keep the tight
// framing they were tuned for.
const WIDE = async () => {
  await page.viewport(1024, 768)
}

test('PageHeader - with breadcrumb, status and actions', async () => {
  await WIDE()
  const result = render(
    <MemoryRouter>
      <div style={{ width: 900, padding: 24 }}>
        <PageHeader
          breadcrumb={[{ label: 'Jobs', to: '/jobs' }, { label: 'Documents Backup' }]}
          title="Documents Backup"
          description="Every configured backup, its schedule, and how its last run went."
          status={<RunStatusBadge status="success" />}
          actions={<Button size="lg">Run Now</Button>}
        />
      </div>
    </MemoryRouter>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/PageHeader.png` })
})

test('EmptyState - with icon and action', async () => {
  await WIDE()
  const result = render(
    <div style={{ width: 640, padding: 24 }}>
      <EmptyState
        icon={HardDriveDownload}
        title="No backup jobs configured yet"
        description="A job pairs a source mount with a destination repository and a schedule. Create one to start backing up."
        action={<Button>Create Job</Button>}
      />
    </div>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/EmptyState.png` })
})

test('Table - numeric alignment and tabular figures', async () => {
  await WIDE()
  const result = render(
    <div style={{ width: 640, padding: 24 }}>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Job</TableHead>
            <TableHead numeric>Duration</TableHead>
            <TableHead numeric>Added</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>Documents Backup</TableCell>
            <TableCell numeric>120s</TableCell>
            <TableCell numeric>50 MB</TableCell>
          </TableRow>
          <TableRow>
            <TableCell>Photos &amp; Media</TableCell>
            <TableCell numeric>4,180s</TableCell>
            <TableCell numeric>1.4 GB</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/Table.png` })
})

test('Table - active row (a live run)', async () => {
  // `<TableRow active>` tints the row and hangs a pulsing accent bar off its
  // first cell (a ::before declared in index.css). Neither is visible to the
  // jsdom suite, so this PNG is the only review of it.
  await WIDE()
  const result = render(
    <div style={{ width: 640, padding: 24 }}>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Job</TableHead>
            <TableHead>Status</TableHead>
            <TableHead numeric>Duration</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow active>
            <TableCell>Documents Backup</TableCell>
            <TableCell>
              <RunStatusBadge status="running" />
            </TableCell>
            <TableCell numeric>—</TableCell>
          </TableRow>
          <TableRow>
            <TableCell>Photos &amp; Media</TableCell>
            <TableCell>
              <RunStatusBadge status="success" />
            </TableCell>
            <TableCell numeric>4,180s</TableCell>
          </TableRow>
          <TableRow>
            <TableCell>System Configuration</TableCell>
            <TableCell>
              <RunStatusBadge status="failed" />
            </TableCell>
            <TableCell numeric>12s</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/Table--active-row.png` })
})

test('Skeleton - loading row', async () => {
  await WIDE()
  const result = render(
    <div style={{ width: 420, padding: 24, display: 'grid', gap: 12 }}>
      <Skeleton style={{ height: 12, width: '35%' }} />
      <Skeleton style={{ height: 16, width: '100%' }} />
      <Skeleton style={{ height: 16, width: '80%' }} />
    </div>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/Skeleton.png` })
})

test('CapacityBar - tones and unknown', async () => {
  await WIDE()
  const result = render(
    <div style={{ width: 420, padding: 24, display: 'grid', gap: 12 }}>
      <CapacityBar percent={12} label="calm" />
      <CapacityBar percent={45} label="calm" />
      <CapacityBar percent={82} label="warning" />
      <CapacityBar percent={96} label="danger" />
      <CapacityBar percent={null} label="unknown" />
    </div>
  )
  cleanup = result.unmount
  await page.screenshot({ path: `${OUT}/CapacityBar.png` })
})
