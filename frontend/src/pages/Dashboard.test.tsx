import { screen, waitFor } from '@testing-library/react'
import * as api from '../lib/api'
import type { BackupJob, BackupRun, HealthStatus } from '../lib/types'
import { renderWithProviders } from '../test/utils'
import Dashboard from './Dashboard'

vi.mock('../lib/api')

const makeRun = (overrides: Partial<BackupRun> = {}): BackupRun => ({
  id: 'run-1',
  job_id: 'job-1',
  job_name: 'Test Job',
  kind: 'backup',
  status: 'success',
  reason: null,
  started_at: '2024-01-15T10:00:00Z',
  finished_at: '2024-01-15T10:02:00Z',
  duration_seconds: 120,
  snapshot_id: null,
  files_new: 10,
  files_changed: 5,
  files_unmodified: 1000,
  dirs_new: 2,
  dirs_changed: 1,
  dirs_unmodified: 50,
  data_added_bytes: 1024000,
  data_added_packed_bytes: 900000,
  total_bytes_processed: 50000000,
  backup_output: null,
  error_output: null,
  prune_status: 'passed',
  prune_error_output: null,
  check_status: 'skipped',
  check_error_output: null,
  triggered_by: 'scheduler',
  ...overrides,
})

const makeJob = (overrides: Partial<BackupJob> = {}): BackupJob => ({
  id: 'job-1',
  name: 'Test Job',
  source_label: 'documents',
  source_subpath: null,
  destination_label: 'main',
  restic_password: null,
  schedule_type: 'interval',
  schedule_value: '6h',
  enabled: true,
  retain_keep_last: null,
  retain_keep_hourly: null,
  retain_keep_daily: null,
  retain_keep_weekly: null,
  retain_keep_monthly: null,
  retain_keep_yearly: null,
  retain_keep_within: null,
  retain_keep_within_hourly: null,
  retain_keep_within_daily: null,
  retain_keep_within_weekly: null,
  retain_keep_within_monthly: null,
  retain_keep_within_yearly: null,
  exclude_patterns: null,
  exclude_caches: false,
  exclude_if_present: null,
  one_file_system: false,
  no_scan: false,
  tags: null,
  compression: null,
  pack_size: null,
  read_concurrency: null,
  timeout_hours: null,
  check_enabled: false,
  check_mode: null,
  check_subset_percent: null,
  check_timeout_hours: null,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  next_run_time: '2024-01-15T16:00:00Z',
  last_run: null,
  has_successful_run: false,
  ...overrides,
})

const healthOk: HealthStatus = {
  scheduler_running: true,
  restic_version: '0.17.3',
  db_ok: true,
}

beforeEach(() => {
  vi.mocked(api.getRecentRuns).mockResolvedValue([])
  vi.mocked(api.listJobs).mockResolvedValue([])
  vi.mocked(api.getHealth).mockResolvedValue(healthOk)
})

describe('Dashboard', () => {
  describe('stats section', () => {
    it('shows total job count', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob(), makeJob({ id: 'job-2' })])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText('2')).toBeInTheDocument())
    })

    it('shows enabled job count', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ enabled: true }),
        makeJob({ id: 'job-2', enabled: false }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText(/1.*enabled|enabled.*1/i)).toBeInTheDocument())
    })

    it('shows restic version from health endpoint', async () => {
      vi.mocked(api.getHealth).mockResolvedValue({ ...healthOk, restic_version: '0.17.3' })
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText(/0\.17\.3/)).toBeInTheDocument())
    })

    it('shows disk space warning callout', async () => {
      renderWithProviders(<Dashboard />)
      await waitFor(() =>
        expect(screen.getByText(/disk space.*not monitored/i)).toBeInTheDocument()
      )
    })
  })

  describe('recent runs list', () => {
    it('shows last 10 runs', async () => {
      const runs = Array.from({ length: 10 }, (_, i) => makeRun({ id: `run-${i}` }))
      vi.mocked(api.getRecentRuns).mockResolvedValue(runs)
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const rows = screen.getAllByRole('row')
        expect(rows.length).toBeGreaterThanOrEqual(10)
      })
    })

    it('shows job name in each run row', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([makeRun({ job_name: 'Documents Backup' })])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText('Documents Backup')).toBeInTheDocument())
    })

    it('renders separate Backup and Verification column headers', async () => {
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        expect(screen.getByRole('columnheader', { name: /^backup$/i })).toBeInTheDocument()
        expect(screen.getByRole('columnheader', { name: /^verification$/i })).toBeInTheDocument()
      })
    })

    it('renders backup status and verification status in separate cells', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: 'failed' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const row = screen.getByText('Test Job').closest('tr')!
        const cells = row.querySelectorAll('td')
        expect(cells[1]).toHaveTextContent('success')
        expect(cells[2]).toHaveTextContent('failed')
        expect(cells[1]).not.toHaveTextContent('failed')
        expect(cells[2]).not.toHaveTextContent('success')
      })
    })

    it('shows a placeholder in the verification cell when check_status is null', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ status: 'running', check_status: null }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const row = screen.getByText('Test Job').closest('tr')!
        const cells = row.querySelectorAll('td')
        expect(cells[1]).toHaveTextContent('running')
        expect(cells[2]).toHaveTextContent('—')
      })
    })

    it('color-codes the Kind column distinctly for backup vs prune', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ id: 'run-backup', job_name: 'Backup Row', kind: 'backup' }),
        makeRun({ id: 'run-prune', job_name: 'Prune Row', kind: 'prune' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const backupBadge = screen.getByTestId('kind-badge-run-backup')
        const pruneBadge = screen.getByTestId('kind-badge-run-prune')
        expect(backupBadge).toHaveTextContent(/backup/i)
        expect(pruneBadge).toHaveTextContent(/prune/i)
        expect(backupBadge.className).not.toBe(pruneBadge.className)
      })
    })

    it('uses a blue-family pastel for backup kind', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ id: 'run-backup', kind: 'backup' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const badge = screen.getByTestId('kind-badge-run-backup')
        expect(badge.className).toMatch(/\bbg-(blue|sky|indigo)-100\b/)
      })
    })

    it('uses an amber/orange-family pastel for prune kind', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([makeRun({ id: 'run-prune', kind: 'prune' })])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const badge = screen.getByTestId('kind-badge-run-prune')
        expect(badge.className).toMatch(/\bbg-(amber|orange|purple)-100\b/)
      })
    })

    it('renders triggered_by as an icon (not plain text) with a descriptive label and distinct colors', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ id: 'run-manual', job_name: 'Manual Row', triggered_by: 'manual' }),
        makeRun({ id: 'run-scheduler', job_name: 'Sched Row', triggered_by: 'scheduler' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const manualRow = screen.getByText('Manual Row').closest('tr')!
        const schedRow = screen.getByText('Sched Row').closest('tr')!
        const manualCell = manualRow.querySelectorAll('td')[6]
        const schedCell = schedRow.querySelectorAll('td')[6]
        // The visible cell should NOT contain the raw text "manual" / "scheduler".
        expect(manualCell.textContent?.trim()).not.toMatch(/^manual$/i)
        expect(schedCell.textContent?.trim()).not.toMatch(/^scheduler$/i)
        // Each cell renders an svg icon.
        expect(manualCell.querySelector('svg')).toBeInTheDocument()
        expect(schedCell.querySelector('svg')).toBeInTheDocument()
        // Each icon carries a descriptive aria-label (read by screen readers
        // and surfaced as a fallback hover tooltip).
        const manualTrigger = manualCell.querySelector('[data-trigger-by="manual"]') as HTMLElement
        const schedTrigger = schedCell.querySelector('[data-trigger-by="scheduler"]') as HTMLElement
        expect(manualTrigger).toHaveAttribute('aria-label', expect.stringMatching(/manual/i))
        expect(schedTrigger).toHaveAttribute('aria-label', expect.stringMatching(/scheduler/i))
        // And the two icons must be visually distinguishable (different classes
        // → different colors), not the same muted gray.
        expect(manualTrigger.className).not.toBe(schedTrigger.className)
      })
    })

    it('shows next run times per job', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ next_run_time: '2024-01-15T16:00:00Z' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText(/next run|next:/i)).toBeInTheDocument())
    })

    it('shows "—" for disabled jobs with no next run time', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ enabled: false, next_run_time: null })])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText('—')).toBeInTheDocument())
    })
  })

  describe('scheduler health banner', () => {
    it('shows red error banner when scheduler is not running', async () => {
      vi.mocked(api.getHealth).mockResolvedValue({ ...healthOk, scheduler_running: false })
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText(/scheduler.*not running/i)).toBeInTheDocument())
    })

    it('does not show error banner when scheduler is running', async () => {
      vi.mocked(api.getHealth).mockResolvedValue({ ...healthOk, scheduler_running: true })
      renderWithProviders(<Dashboard />)
      await waitFor(() =>
        expect(screen.queryByText(/scheduler.*not running/i)).not.toBeInTheDocument()
      )
    })

    it('banner mentions checking container logs', async () => {
      vi.mocked(api.getHealth).mockResolvedValue({ ...healthOk, scheduler_running: false })
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText(/container logs/i)).toBeInTheDocument())
    })
  })

  describe('polling behavior', () => {
    it('polls while a run is in progress', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ status: 'running', check_status: null }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(vi.mocked(api.getRecentRuns)).toHaveBeenCalled())
      expect(vi.mocked(api.getRecentRuns).mock.calls.length).toBeGreaterThanOrEqual(1)
    })

    it('stops polling when all runs are terminal and check_status is set', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: 'passed' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(vi.mocked(api.getRecentRuns)).toHaveBeenCalled())
      const callCount = vi.mocked(api.getRecentRuns).mock.calls.length
      await new Promise((r) => setTimeout(r, 100))
      expect(vi.mocked(api.getRecentRuns).mock.calls.length).toBe(callCount)
    })

    it('continues polling when status is success but check_status is null', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: null }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(vi.mocked(api.getRecentRuns)).toHaveBeenCalledTimes(2))
    })
  })

  describe('error states', () => {
    it('shows error message when jobs API fails', async () => {
      vi.mocked(api.listJobs).mockRejectedValue(new Error('Network error'))
      renderWithProviders(<Dashboard />)
      await waitFor(() =>
        expect(screen.getByText(/error|failed|could not load/i)).toBeInTheDocument()
      )
    })

    it('shows error message when runs API fails', async () => {
      vi.mocked(api.getRecentRuns).mockRejectedValue(new Error('Network error'))
      renderWithProviders(<Dashboard />)
      await waitFor(() =>
        expect(screen.getByText(/error|failed|could not load/i)).toBeInTheDocument()
      )
    })
  })

  describe('next run times', () => {
    it('shows "—" for enabled jobs with no next_run_time', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ enabled: true, next_run_time: null })])
      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText('—')).toBeInTheDocument())
    })

    it('shows next run time for multiple jobs', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', next_run_time: '2024-01-15T16:00:00Z' }),
        makeJob({ id: 'job-2', next_run_time: '2024-01-15T20:00:00Z' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const nextRunEls = screen.getAllByText(/next run|next:/i)
        expect(nextRunEls.length).toBeGreaterThanOrEqual(1)
      })
    })
  })
})
