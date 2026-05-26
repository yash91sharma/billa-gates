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
import type { AppSettings, BackupJob, BackupRun, HealthStatus, Snapshot } from '../lib/types'
import Dashboard from '../pages/Dashboard'
import JobDetail from '../pages/JobDetail'
import Jobs from '../pages/Jobs'
import RunDetail from '../pages/RunDetail'
import Settings from '../pages/Settings'
import { renderWithProviders } from '../test/utils'

vi.mock('../lib/api')

const OUT = '../../screenshots/pages'

// ── Fixtures ─────────────────────────────────────────────────────────────────

const job: BackupJob = {
  id: 'job-1',
  name: 'Documents Backup',
  source_label: 'documents',
  source_subpath: null,
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
  next_run_time: '2026-05-19T18:00:00Z',
  last_run: {
    id: 'run-1',
    status: 'success',
    check_status: 'passed',
    started_at: '2026-05-19T12:00:00Z',
    finished_at: '2026-05-19T12:02:00Z',
    duration_seconds: 120,
    triggered_by: 'scheduler',
  },
  has_successful_run: true,
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
}

const health: HealthStatus = {
  scheduler_running: true,
  restic_version: '0.17.3',
  db_ok: true,
}

// ── Mock setup ───────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.mocked(api.listJobs).mockResolvedValue([job])
  vi.mocked(api.getJob).mockResolvedValue(job)
  vi.mocked(api.getJobRuns).mockResolvedValue([run])
  vi.mocked(api.getJobSnapshots).mockResolvedValue([snapshot])
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
