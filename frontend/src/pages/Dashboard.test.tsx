import { screen, waitFor, within } from '@testing-library/react'
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
      // Reported against the total, so "1" is legible on its own: one enabled
      // job means something different in a fleet of two than in a fleet of ten.
      await waitFor(() => expect(screen.getByText('1 of 2')).toBeInTheDocument())
      expect(screen.getByText('Enabled')).toBeInTheDocument()
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
      // Needs a run: with none, the card shows an empty state rather than a
      // header row standing over nothing.
      vi.mocked(api.getRecentRuns).mockResolvedValue([makeRun()])
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
        // A theme token, not a palette class. `backup` was sky-100 here while
        // `running` was blue-100 in RunStatusBadge — one idea, two hardcoded
        // colours, free to drift because nothing tied them together.
        expect(badge.className).toMatch(/\bbg-info-subtle\b/)
      })
    })

    it('uses an amber/orange-family pastel for prune kind', async () => {
      vi.mocked(api.getRecentRuns).mockResolvedValue([makeRun({ id: 'run-prune', kind: 'prune' })])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const badge = screen.getByTestId('kind-badge-run-prune')
        expect(badge.className).toMatch(/\bbg-warning-subtle\b/)
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
      // Polling runs at a 60s interval — advance a fake clock instead of
      // waiting real time. shouldAdvanceTime keeps waitFor's own timers alive.
      vi.useFakeTimers({ shouldAdvanceTime: true })
      try {
        vi.mocked(api.getRecentRuns).mockResolvedValue([
          makeRun({ status: 'success', check_status: null }),
        ])
        renderWithProviders(<Dashboard />)
        await waitFor(() => expect(vi.mocked(api.getRecentRuns)).toHaveBeenCalled())
        await vi.advanceTimersByTimeAsync(60_000)
        await waitFor(() => expect(vi.mocked(api.getRecentRuns)).toHaveBeenCalledTimes(2))
      } finally {
        vi.useRealTimers()
      }
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

    it('sorts upcoming runs chronologically by next_run_time, placing unscheduled jobs last', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'Docs', next_run_time: '2026-08-04T03:00:00Z' }),
        makeJob({ id: 'job-2', name: 'FamilyMedia', next_run_time: '2026-07-29T03:00:00Z' }),
        makeJob({ id: 'job-3', name: 'UnscheduledJob', next_run_time: null }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const upcomingSection = screen.getByRole('heading', {
          name: /upcoming runs/i,
        }).parentElement
        expect(upcomingSection).toBeInTheDocument()
        const card = upcomingSection!.closest('[data-slot="card"]')!
        const jobNames = Array.from(card.querySelectorAll('[data-slot="upcoming-job-name"]')).map(
          (el) => el.textContent
        )
        expect(jobNames).toEqual(['FamilyMedia', 'Docs', 'UnscheduledJob'])
      })
    })

    it('separates upcoming runs rows for improved visual clarity', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'Docs', next_run_time: '2026-08-04T03:00:00Z' }),
        makeJob({ id: 'job-2', name: 'FamilyMedia', next_run_time: '2026-07-29T03:00:00Z' }),
      ])
      renderWithProviders(<Dashboard />)
      await waitFor(() => {
        const upcomingSection = screen.getByRole('heading', {
          name: /upcoming runs/i,
        }).parentElement
        expect(upcomingSection).toBeInTheDocument()
        // Hairline dividers rather than zebra striping: with a job name on the
        // left and a timestamp on the right, the eye needs the row boundary,
        // not a tinted band behind every other one.
        const list = upcomingSection!.closest('[data-slot="card"]')!.querySelector('ul')
        expect(list?.className).toContain('divide-y')
        expect(list?.querySelectorAll('li')).toHaveLength(2)
      })
    })

    it('displays relative time detail in (In X days, Y hours) format', async () => {
      const mockNow = new Date('2026-07-28T03:00:00Z').getTime()
      const dateSpy = vi.spyOn(Date, 'now').mockReturnValue(mockNow)
      try {
        // 1 day, 2 hours away -> 2026-07-29T05:00:00Z
        // 7 days, 0 hours away -> 2026-08-04T03:00:00Z
        vi.mocked(api.listJobs).mockResolvedValue([
          makeJob({ id: 'job-1', name: 'Docs', next_run_time: '2026-08-04T03:00:00Z' }),
          makeJob({ id: 'job-2', name: 'FamilyMedia', next_run_time: '2026-07-29T05:00:00Z' }),
        ])
        renderWithProviders(<Dashboard />)
        await waitFor(() => {
          expect(screen.getByText(/\(In 1 day, 2 hours\)/)).toBeInTheDocument()
          expect(screen.getByText(/\(In 7 days, 0 hours\)/)).toBeInTheDocument()
        })
      } finally {
        dateSpy.mockRestore()
      }
    })

    it('handles singular and plural units correctly in relative time detail', async () => {
      const mockNow = new Date('2026-07-28T03:00:00Z').getTime()
      const dateSpy = vi.spyOn(Date, 'now').mockReturnValue(mockNow)
      try {
        // 1 day, 1 hour away -> 2026-07-29T04:00:00Z
        vi.mocked(api.listJobs).mockResolvedValue([
          makeJob({ id: 'job-1', name: 'SingularJob', next_run_time: '2026-07-29T04:00:00Z' }),
        ])
        renderWithProviders(<Dashboard />)
        await waitFor(() => {
          expect(screen.getByText(/\(In 1 day, 1 hour\)/)).toBeInTheDocument()
        })
      } finally {
        dateSpy.mockRestore()
      }
    })
  })

  describe('inline Stop action', () => {
    it('shows a Stop button on running rows', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([])
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ id: 'run-running', status: 'running', check_status: null }),
      ])
      vi.mocked(api.getHealth).mockResolvedValue({
        scheduler_running: true,
        restic_version: '0.17.3',
        db_ok: true,
      } as HealthStatus)

      renderWithProviders(<Dashboard />)
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /^stop$/i })).toBeInTheDocument()
      )
    })

    it('omits Stop button on terminal rows', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([])
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ id: 'run-done', status: 'success', check_status: 'passed' }),
      ])
      vi.mocked(api.getHealth).mockResolvedValue({
        scheduler_running: true,
        restic_version: '0.17.3',
        db_ok: true,
      } as HealthStatus)

      renderWithProviders(<Dashboard />)
      await waitFor(() => expect(screen.getByText('Test Job')).toBeInTheDocument())
      expect(screen.queryByRole('button', { name: /^stop$/i })).not.toBeInTheDocument()
    })

    it('clicking Stop calls cancelRun after confirming in the dialog', async () => {
      // Cancelling a running backup is confirmed in an in-app dialog rather
      // than window.confirm — the browser's own box cannot explain that
      // already-uploaded data is kept, which is the whole question the user
      // is weighing at that moment.
      const { default: userEvent } = await import('@testing-library/user-event')
      vi.mocked(api.listJobs).mockResolvedValue([])
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ id: 'run-running', status: 'running', check_status: null }),
      ])
      vi.mocked(api.cancelRun).mockResolvedValue(undefined)
      vi.mocked(api.getHealth).mockResolvedValue({
        scheduler_running: true,
        restic_version: '0.17.3',
        db_ok: true,
      } as HealthStatus)

      renderWithProviders(<Dashboard />)
      const user = userEvent.setup()
      await user.click(await screen.findByRole('button', { name: /^stop$/i }))

      const dialog = await screen.findByRole('dialog')
      await user.click(within(dialog).getByRole('button', { name: /stop backup/i }))
      await waitFor(() => expect(vi.mocked(api.cancelRun)).toHaveBeenCalledWith('run-running'))
    })

    it('dismissing the stop dialog leaves the run alone', async () => {
      const { default: userEvent } = await import('@testing-library/user-event')
      vi.mocked(api.getRecentRuns).mockResolvedValue([
        makeRun({ id: 'run-running', status: 'running', check_status: null }),
      ])
      renderWithProviders(<Dashboard />)
      const user = userEvent.setup()
      await user.click(await screen.findByRole('button', { name: /^stop$/i }))

      const dialog = await screen.findByRole('dialog')
      await user.click(within(dialog).getByRole('button', { name: /keep running/i }))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(vi.mocked(api.cancelRun)).not.toHaveBeenCalled()
    })
  })
})
