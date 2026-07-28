/**
 * Screenshot tests for top-level pages in their default populated state.
 *
 * Each test renders a page through the real router + query provider, mocks
 * the API responses (same pattern as the unit tests), waits for the data
 * to land, then takes a screenshot to ../../screenshots/pages/.
 */
import { waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { page } from '@vitest/browser/context'
import { Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, test, vi } from 'vitest'

import Layout from '../components/Layout'
import * as api from '../lib/api'
import type {
  AppSettings,
  BackupJob,
  BackupRun,
  HealthStatus,
  JobCommand,
  Snapshot,
} from '../lib/types'
import Dashboard from '../pages/Dashboard'
import JobDetail from '../pages/JobDetail'
import Jobs from '../pages/Jobs'
import RunDetail from '../pages/RunDetail'
import Settings from '../pages/Settings'
import { renderWithProviders } from '../test/utils'

vi.mock('../lib/api')

const OUT = '../../screenshots/pages'

// ── Fixtures ─────────────────────────────────────────────────────────────────

const FIXED_NOW = new Date('2026-06-01T12:00:00Z')

const job: BackupJob = {
  id: 'job-1',
  name: 'Documents Backup',
  source_label: 'documents',
  destination_label: 'main',
  restic_password: null,
  schedule_type: 'interval',
  schedule_value: '6h',
  enabled: true,
  retain_keep_last: 7,
  retain_keep_hourly: null,
  retain_keep_daily: 30,
  retain_keep_weekly: 12,
  retain_keep_monthly: 12,
  retain_keep_yearly: null,
  retain_keep_within: null,
  retain_keep_within_hourly: null,
  retain_keep_within_daily: null,
  retain_keep_within_weekly: null,
  retain_keep_within_monthly: null,
  retain_keep_within_yearly: null,
  exclude_patterns: ['*.tmp', 'node_modules/'],
  exclude_caches: true,
  exclude_if_present: null,
  one_file_system: false,
  no_scan: false,
  tags: null,
  compression: 'auto',
  pack_size: null,
  read_concurrency: null,
  timeout_hours: null,
  check_enabled: true,
  check_mode: 'structural',
  check_subset_percent: null,
  check_timeout_hours: null,
  created_at: '2026-05-01T10:00:00Z',
  updated_at: '2026-05-15T10:00:00Z',
  next_run_time: '2026-06-02T14:02:00Z',
  last_run: {
    id: 'run-1',
    kind: 'backup',
    status: 'success',
    check_status: 'passed',
    started_at: '2026-05-19T12:00:00Z',
    finished_at: '2026-05-19T12:02:00Z',
    duration_seconds: 120,
    triggered_by: 'scheduler',
  },
}

const run: BackupRun = {
  id: 'run-1',
  job_id: 'job-1',
  job_name: 'Documents Backup',
  kind: 'backup',
  status: 'success',
  reason: null,
  started_at: '2026-05-19T12:00:00Z',
  finished_at: '2026-05-19T12:02:00Z',
  duration_seconds: 120,
  snapshot_id: 'a'.repeat(64),
  files_new: 10,
  files_changed: 5,
  files_unmodified: 1000,
  dirs_new: 2,
  dirs_changed: 1,
  dirs_unmodified: 50,
  data_added_bytes: 50 * 1024 * 1024,
  data_added_packed_bytes: 45 * 1024 * 1024,
  total_bytes_processed: 500 * 1024 * 1024,
  backup_output: 'backup complete: 1 snapshot saved',
  error_output: null,
  prune_status: 'passed',
  prune_error_output: null,
  check_status: 'passed',
  check_error_output: null,
  triggered_by: 'scheduler',
}

const snapshot: Snapshot = {
  snapshot_id: 'a'.repeat(64),
  snapshot_time: '2026-05-19T12:01:30Z',
  hostname: 'home-server',
  paths: ['/sources/documents'],
  tags: ['scheduled'],
  size_bytes: 1_073_741_824,
}

// The restic command preview, exactly as GET /api/jobs/{id}/commands returns
// it — the page renders the server's strings verbatim.
const resticEnv = {
  RESTIC_REPOSITORY: '/destinations/main/Documents Backup',
  RESTIC_PASSWORD: "<this job's repository password>",
  RESTIC_CACHE_DIR: '/app/data/restic-cache',
}

const jobCommands: JobCommand[] = [
  {
    step: 'verify_repository',
    title: 'Verify the repository',
    description:
      'Reads the repository config to prove the destination is reachable and the stored password opens it.',
    group: 'backup_run',
    runs: true,
    condition: null,
    env: resticEnv,
    argv: ['restic', 'cat', 'config'],
    command: 'restic cat config',
  },
  {
    step: 'unlock',
    title: 'Clear stale locks',
    description: 'Removes lock files left behind by a run that was killed mid-write.',
    group: 'backup_run',
    runs: true,
    condition: 'Runs on every backup because “Auto unlock” is on in Settings.',
    env: resticEnv,
    argv: ['restic', 'unlock'],
    command: 'restic unlock',
  },
  {
    step: 'backup',
    title: 'Back up the source',
    description:
      'The backup itself: reads /sources/documents and writes a new snapshot to the repository.',
    group: 'backup_run',
    runs: true,
    condition:
      '<id-of-latest-snapshot> is resolved by the previous command; on the first backup --parent is left off.',
    env: resticEnv,
    argv: [],
    command:
      "restic backup --host billa-gates --parent '<id-of-latest-snapshot>' --exclude '*.tmp' --exclude-caches --compression auto --json /sources/documents",
  },
  {
    step: 'retention',
    title: 'Apply the retention policy',
    description: 'Applies the retention policy by dropping snapshot references.',
    group: 'backup_run',
    runs: true,
    condition: 'Runs after a successful backup only.',
    env: resticEnv,
    argv: [],
    command: "restic forget --group-by '' --keep-last 10 --keep-daily 7 --keep-monthly 12",
  },
  {
    step: 'prune',
    title: 'Prune Old Files',
    description:
      'Deletes the data behind forgotten snapshots. This is the only thing that frees space on the destination drive.',
    group: 'on_demand',
    runs: true,
    condition: 'Runs when you click “Prune Old Files”. Never scheduled.',
    env: resticEnv,
    argv: [],
    command: 'restic prune',
  },
  {
    step: 'check_structural',
    title: 'Integrity Check — structural',
    description: "Verifies that the repository's metadata is complete and consistent.",
    group: 'on_demand',
    runs: true,
    condition:
      "Runs when you click “Integrity Check” and leave the mode on Structural (the dialog's default).",
    env: resticEnv,
    argv: [],
    command: 'restic check',
  },
  {
    step: 'check_subset',
    title: 'Integrity Check — subset',
    description: 'Structural verification plus a re-read of a percentage of the pack data.',
    group: 'on_demand',
    runs: true,
    condition:
      'Runs when you pick Subset in the Integrity Check dialog; <percent> is the percentage you enter there.',
    env: resticEnv,
    argv: [],
    command: 'restic check --read-data-subset=<percent>%',
  },
  {
    step: 'check_full',
    title: 'Integrity Check — full',
    description: 'Structural verification plus a re-read of every pack file in the repository.',
    group: 'on_demand',
    runs: true,
    condition: 'Runs when you pick Full in the Integrity Check dialog.',
    env: resticEnv,
    argv: [],
    command: 'restic check --read-data',
  },
  {
    step: 'unlock_manual',
    title: 'Unlock',
    description: "Removes stale restic locks from this job's repository.",
    group: 'on_demand',
    runs: true,
    condition: 'Runs when you click “Unlock”.',
    env: resticEnv,
    argv: [],
    command: 'restic unlock',
  },
]

const settings: AppSettings = {
  id: 1,
  ntfy_server_url: 'https://ntfy.sh',
  ntfy_topic: 'home-backups',
  ntfy_token: null,
  notify_on_start: false,
  notify_on_success: true,
  notify_on_failure: true,
  notify_on_warning: true,
  keep_last_runs: 100,
  auto_unlock: true,
  notify_on_verification: false,
  restic_version: '0.17.3',
  default_job_timeout_hours: 24,
  metadata_timeout_seconds: 600,
}

const health: HealthStatus = {
  scheduler_running: true,
  restic_version: '0.17.3',
  db_ok: true,
}

const job2: BackupJob = {
  ...job,
  id: 'job-2',
  name: 'Photos & Media Backup',
  source_label: 'photos',
  destination_label: 'archive',
  next_run_time: '2026-06-01T14:30:00Z',
}

const job3: BackupJob = {
  ...job,
  id: 'job-3',
  name: 'System Configuration',
  source_label: 'etc',
  destination_label: 'main',
  next_run_time: '2026-06-03T14:10:00Z',
}

// ── Mock setup ───────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.useFakeTimers({ now: FIXED_NOW, shouldAdvanceTime: true })
  vi.mocked(api.listJobs).mockResolvedValue([job, job2, job3])
  vi.mocked(api.getJob).mockResolvedValue(job)
  vi.mocked(api.getJobRuns).mockResolvedValue([run])
  vi.mocked(api.getJobSnapshots).mockResolvedValue([snapshot])
  vi.mocked(api.getJobCommands).mockResolvedValue(jobCommands)
  vi.mocked(api.getRecentRuns).mockResolvedValue([run])
  vi.mocked(api.getRun).mockResolvedValue(run)
  vi.mocked(api.getHealth).mockResolvedValue(health)
  vi.mocked(api.getSettings).mockResolvedValue(settings)
  vi.mocked(api.checkResticUpdate).mockResolvedValue({
    current: '0.17.3',
    latest: '0.17.3',
    update_available: false,
  })
  vi.mocked(api.listSourceMounts).mockResolvedValue(['documents', 'photos'])
  vi.mocked(api.listDestinationMounts).mockResolvedValue(['main', 'archive'])
})

let cleanup: (() => void) | undefined

// Pages render inside the Layout shell, which hides the sidebar below the md
// breakpoint (768px). Force a desktop-sized viewport so the screenshots
// include the sidebar — that's the canonical view users will see.
beforeEach(async () => {
  await page.viewport(1280, 900)
})

afterEach(() => {
  vi.useRealTimers()
  cleanup?.()
  cleanup = undefined
})

// ── Page screenshots ─────────────────────────────────────────────────────────

/**
 * Render a page through the same Layout shell users see in the real app,
 * so screenshots include the sidebar instead of the bare page tree.
 */
function renderPage(path: string, element: React.ReactNode) {
  return renderWithProviders(
    <Routes>
      <Route element={<Layout />}>
        <Route path={path} element={element} />
      </Route>
    </Routes>,
    { route: path === '/jobs/:id' ? '/jobs/job-1' : path === '/runs/:id' ? '/runs/run-1' : path }
  )
}

test('Dashboard - populated', async () => {
  const result = renderPage('/', <Dashboard />)
  cleanup = result.unmount
  await waitFor(() => {
    if (!result.container.textContent?.includes('Documents Backup')) {
      throw new Error('dashboard not ready')
    }
  })
  await page.screenshot({ path: `${OUT}/Dashboard.png` })
})

test('Jobs - populated', async () => {
  const result = renderPage('/jobs', <Jobs />)
  cleanup = result.unmount
  await waitFor(() => {
    if (!result.container.textContent?.includes('Documents Backup')) {
      throw new Error('jobs not ready')
    }
  })
  await page.screenshot({ path: `${OUT}/Jobs.png` })
})

test('Jobs - create form open', async () => {
  // Open the create form, then screenshot so the populated source/destination
  // dropdowns are visible.
  const result = renderPage('/jobs', <Jobs />)
  cleanup = result.unmount
  const user = userEvent.setup()
  // The button label varies ("Create Job" / "New Job"); find it by visible text.
  await waitFor(() => {
    if (
      !Array.from(result.container.querySelectorAll('button')).some((b) =>
        /create.*job|new.*job/i.test(b.textContent ?? '')
      )
    ) {
      throw new Error('create-job button not found')
    }
  })
  const target = Array.from(result.container.querySelectorAll('button')).find((b) =>
    /create.*job|new.*job/i.test(b.textContent ?? '')
  )!
  await user.click(target)
  await waitFor(() => {
    const src = result.container.querySelector('#source-label') as HTMLSelectElement | null
    if (!src || src.options.length < 2) {
      throw new Error('source dropdown not populated yet')
    }
  })
  await page.screenshot({ path: `${OUT}/Jobs--create-form.png` })
})

test('Jobs - create in flight', async () => {
  // POST /api/jobs runs `restic init` on the destination drive before it
  // answers, so this state is on screen for seconds on every create. Held
  // pending here to capture what the user actually sees while waiting:
  // the modal, the dimmed/blurred page behind it, and the message.
  vi.mocked(api.createJob).mockReturnValue(new Promise<BackupJob>(() => {}))
  const result = renderPage('/jobs', <Jobs />)
  cleanup = result.unmount
  const user = userEvent.setup()
  await waitFor(() => {
    if (!result.container.querySelector('#job-name')) {
      const open = Array.from(result.container.querySelectorAll('button')).find((b) =>
        /create.*job|new.*job/i.test(b.textContent ?? '')
      )
      if (!open) throw new Error('create-job button not found')
      open.click()
      throw new Error('form not open yet')
    }
  })
  await user.type(result.container.querySelector('#job-name') as HTMLInputElement, 'Photos Daily')

  const form = result.container.querySelector('form') as HTMLFormElement
  const submit = form.querySelector('button[type="submit"]') as HTMLButtonElement
  await user.click(submit)

  await waitFor(() => {
    if (!document.querySelector('[role="dialog"]')) throw new Error('working dialog not shown yet')
  })
  // Let the overlay fade and the modal zoom in finish, or the shot catches a
  // half-transparent dialog over an unblurred page.
  await new Promise((r) => setTimeout(r, 300))
  await page.screenshot({ path: `${OUT}/Jobs--create-in-flight.png` })
})

test('JobDetail - populated', async () => {
  const result = renderPage('/jobs/:id', <JobDetail />)
  cleanup = result.unmount
  await waitFor(() => {
    if (!result.container.textContent?.includes('Documents Backup')) {
      throw new Error('job detail not ready')
    }
  })
  await page.screenshot({ path: `${OUT}/JobDetail.png` })
})

test('JobDetail - commands tab', async () => {
  // Taller than the shared 900px viewport: the tab lists the backup pipeline
  // *and* the button-triggered commands, and a clipped shot would hide the
  // separation that is the point of the layout.
  await page.viewport(1280, 1900)
  const result = renderPage('/jobs/:id', <JobDetail />)
  cleanup = result.unmount
  await waitFor(() => {
    if (!result.container.textContent?.includes('Documents Backup')) {
      throw new Error('job detail not ready')
    }
  })
  const commandsTab = Array.from(result.container.querySelectorAll('[role="tab"]')).find(
    (t) => t.textContent?.trim() === 'Commands'
  ) as HTMLElement
  await userEvent.click(commandsTab)
  await waitFor(() => {
    if (!result.container.textContent?.includes('restic cat config')) {
      throw new Error('commands not ready')
    }
  })
  await page.screenshot({ path: `${OUT}/JobDetail-commands.png` })
})

test('RunDetail - success', async () => {
  const result = renderPage('/runs/:id', <RunDetail />)
  cleanup = result.unmount
  await waitFor(() => {
    if (!result.container.textContent?.includes('Documents Backup')) {
      throw new Error('run detail not ready')
    }
  })
  await page.screenshot({ path: `${OUT}/RunDetail.png` })
})

// ── Cancel-feature scenarios ─────────────────────────────────────────────────
//
// The default-state fixtures above all use a successful/terminal run, so the
// Stop button (visible only while a run is `running`) never appears. These
// scenarios re-mock the API with an in-flight run to capture the cancel UI on
// the three pages where it lives, plus the canceled terminal state.

const runningRun: BackupRun = {
  ...run,
  id: 'run-running',
  status: 'running',
  finished_at: null,
  duration_seconds: null,
  snapshot_id: null,
  files_new: null,
  files_changed: null,
  files_unmodified: null,
  dirs_new: null,
  dirs_changed: null,
  dirs_unmodified: null,
  data_added_bytes: null,
  data_added_packed_bytes: null,
  total_bytes_processed: null,
  backup_output: null,
  prune_status: null,
  check_status: null,
  triggered_by: 'manual',
}

const canceledRun: BackupRun = {
  ...run,
  id: 'run-canceled',
  status: 'canceled',
  reason: 'user_canceled',
  snapshot_id: null,
  files_new: null,
  files_changed: null,
  files_unmodified: null,
  data_added_bytes: null,
  backup_output: null,
  error_output: 'Canceled by user.',
  prune_status: 'skipped',
  check_status: 'skipped',
  triggered_by: 'manual',
  duration_seconds: 42,
}

test('Dashboard - running run with Stop button', async () => {
  vi.mocked(api.getRecentRuns).mockResolvedValue([runningRun, run])
  vi.mocked(api.getRun).mockResolvedValue(runningRun)

  const result = renderPage('/', <Dashboard />)
  cleanup = result.unmount
  await waitFor(() => {
    const hasStop = Array.from(result.container.querySelectorAll('button')).some(
      (b) => b.textContent?.trim() === 'Stop'
    )
    if (!hasStop) throw new Error('Stop button not rendered yet')
  })
  await page.screenshot({ path: `${OUT}/Dashboard--running.png` })
})

test('JobDetail - running run with Stop button', async () => {
  vi.mocked(api.getJobRuns).mockResolvedValue([runningRun])

  const result = renderPage('/jobs/:id', <JobDetail />)
  cleanup = result.unmount
  await waitFor(() => {
    const hasStop = Array.from(result.container.querySelectorAll('button')).some(
      (b) => b.textContent?.trim() === 'Stop'
    )
    if (!hasStop) throw new Error('Stop button not rendered yet')
  })
  await page.screenshot({ path: `${OUT}/JobDetail--running.png` })
})

// Real-browser coverage for the header action tooltips: jsdom has no layout,
// so placement, width and pointer-out dismissal can only be seen here.
test('JobDetail - action tooltip on hover', async () => {
  const user = userEvent.setup()
  const result = renderPage('/jobs/:id', <JobDetail />)
  cleanup = result.unmount
  await waitFor(() => {
    if (!result.container.textContent?.includes('Documents Backup')) {
      throw new Error('job detail not ready')
    }
  })

  const prune = Array.from(result.container.querySelectorAll('button')).find((b) =>
    /prune/i.test(b.textContent ?? '')
  )!
  await user.hover(prune)
  await waitFor(() => {
    if (!document.querySelector('[role="tooltip"]')) throw new Error('tooltip not open yet')
  })
  // Let the fade/zoom-in finish, or the shot catches a half-opaque bubble.
  await new Promise((resolve) => setTimeout(resolve, 300))
  await page.screenshot({ path: `${OUT}/JobDetail--action-tooltip.png` })

  // Moving away must close it — the behaviour the jsdom suite cannot assert.
  await user.unhover(prune)
  await user.hover(result.container.querySelector('h1')!)
  await waitFor(() => {
    if (document.querySelector('[role="tooltip"]')) throw new Error('tooltip still open')
  })
})

test('RunDetail - running with Stop button', async () => {
  vi.mocked(api.getRun).mockResolvedValue(runningRun)

  const result = renderPage('/runs/:id', <RunDetail />)
  cleanup = result.unmount
  await waitFor(() => {
    const hasStop = Array.from(result.container.querySelectorAll('button')).some(
      (b) => b.textContent?.trim() === 'Stop'
    )
    if (!hasStop) throw new Error('Stop button not rendered yet')
  })
  await page.screenshot({ path: `${OUT}/RunDetail--running.png` })
})

test('RunDetail - canceled run', async () => {
  vi.mocked(api.getRun).mockResolvedValue(canceledRun)

  const result = renderPage('/runs/:id', <RunDetail />)
  cleanup = result.unmount
  await waitFor(() => {
    if (!result.container.textContent?.includes('canceled')) {
      throw new Error('canceled badge not rendered yet')
    }
  })
  await page.screenshot({ path: `${OUT}/RunDetail--canceled.png` })
})

test('Settings - populated', async () => {
  const result = renderPage('/settings', <Settings />)
  cleanup = result.unmount
  // Settings delays the ntfy form by ~100ms after settings load (see
  // Settings.tsx for the rationale). Wait for one of its labels — the
  // "Ntfy Server URL" text — to appear before screenshotting so the PNG
  // captures the full page.
  await waitFor(
    () => {
      if (!result.container.textContent?.includes('Ntfy Server URL')) {
        throw new Error('settings not ready')
      }
    },
    { timeout: 1000 }
  )
  await page.screenshot({ path: `${OUT}/Settings.png` })
})
