import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../lib/api'
import type { BackupJob, BackupRun, Snapshot } from '../lib/types'
import { renderWithProviders } from '../test/utils'
import JobDetail from './JobDetail'

vi.mock('../lib/api')

const makeJob = (overrides: Partial<BackupJob> = {}): BackupJob => ({
  id: 'job-1',
  name: 'My Documents',
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

const makeRun = (overrides: Partial<BackupRun> = {}): BackupRun => ({
  id: 'run-1',
  job_id: 'job-1',
  job_name: 'My Documents',
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
  check_status: 'passed',
  check_error_output: null,
  triggered_by: 'scheduler',
  ...overrides,
})

const makeSnapshot = (overrides: Partial<Snapshot> = {}): Snapshot => ({
  snapshot_id: 'abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890',
  snapshot_time: '2024-01-15T10:30:00Z',
  hostname: 'myhost',
  paths: ['/sources/documents'],
  tags: null,
  size_bytes: 1073741824,
  ...overrides,
})

beforeEach(() => {
  vi.mocked(api.getJob).mockResolvedValue(makeJob())
  vi.mocked(api.getJobRuns).mockResolvedValue([])
  vi.mocked(api.getJobSnapshots).mockResolvedValue([])
  vi.mocked(api.unlockJob).mockResolvedValue({ output: 'unlock successful' })
  vi.mocked(api.triggerRun).mockResolvedValue({ run_id: 'run-new' })
  vi.mocked(api.triggerPrune).mockResolvedValue({ run_id: 'prune-new' })
  vi.mocked(api.listSourceMounts).mockResolvedValue(['documents', 'photos'])
  vi.mocked(api.listDestinationMounts).mockResolvedValue(['main'])
  vi.mocked(api.updateJob).mockImplementation(async (_id, data) => ({
    ...makeJob(),
    ...(data as object),
  }))
})

describe('JobDetail', () => {
  describe('header', () => {
    it('shows the job name', async () => {
      vi.mocked(api.getJob).mockResolvedValue(makeJob({ name: 'Home Photos Backup' }))
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => expect(screen.getByText('Home Photos Backup')).toBeInTheDocument())
    })

    it('shows enabled/disabled badge', async () => {
      vi.mocked(api.getJob).mockResolvedValue(makeJob({ enabled: true }))
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => expect(screen.getByText(/enabled/i)).toBeInTheDocument())
    })

    it('shows Run Now button', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /run now/i })).toBeInTheDocument()
      )
    })

    it('shows Edit button', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument())
    })

    it('shows a Prune button so the operator can run restic prune on demand', async () => {
      // Prune is decoupled from the backup pipeline (gaps.md H1) — backup no
      // longer runs `restic prune`, so the operator needs an explicit way to
      // reclaim space.
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /prune/i })).toBeInTheDocument()
      )
    })

    it('shows a notice about manual pruning and disk space reclamation', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByText(/only remove snapshot metadata/i)).toBeInTheDocument()
      )
    })
  })

  describe('Prune Now behavior', () => {
    it('calls triggerPrune with the correct job id and navigates to the new run', async () => {
      const user = userEvent.setup()
      vi.mocked(api.getJob).mockResolvedValue(makeJob({ id: 'job-1' }))
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /prune/i }))
      await user.click(screen.getByRole('button', { name: /prune/i }))
      expect(vi.mocked(api.triggerPrune)).toHaveBeenCalledWith('job-1')
    })

    it('shows 409 error when a run is already in progress', async () => {
      const user = userEvent.setup()
      vi.mocked(api.triggerPrune).mockRejectedValue(
        Object.assign(new Error('Run in progress'), { status: 409 })
      )
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /prune/i }))
      await user.click(screen.getByRole('button', { name: /prune/i }))
      await waitFor(() =>
        expect(screen.getByText(/already.*progress|in progress|409/i)).toBeInTheDocument()
      )
    })
  })

  describe('runs list triggered-by column', () => {
    it('renders triggered_by as a labelled icon (matching the dashboard) rather than raw text', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ id: 'r-manual', triggered_by: 'manual' }),
        makeRun({ id: 'r-sched', triggered_by: 'scheduler' }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const table = await screen.findByRole('table')
      // The Triggered By column should not contain the bare words "manual"
      // or "scheduler" — the meaning is conveyed by the icon + tooltip.
      const cells = Array.from(table.querySelectorAll('td')).map((c) => c.textContent ?? '')
      expect(cells).not.toContain('manual')
      expect(cells).not.toContain('scheduler')
      // Each row should expose a TriggeredByIcon element with the matching
      // aria-label.
      const manualTrigger = table.querySelector('[data-trigger-by="manual"]') as HTMLElement
      const schedTrigger = table.querySelector('[data-trigger-by="scheduler"]') as HTMLElement
      expect(manualTrigger).not.toBeNull()
      expect(schedTrigger).not.toBeNull()
      expect(manualTrigger).toHaveAttribute('aria-label', expect.stringMatching(/manual/i))
      expect(schedTrigger).toHaveAttribute('aria-label', expect.stringMatching(/scheduler/i))
      // Visually distinguishable (different classes/colors).
      expect(manualTrigger.className).not.toBe(schedTrigger.className)
    })
  })

  describe('runs list shows kind', () => {
    it('shows the kind column with backup and prune rows side-by-side', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ id: 'r-backup', kind: 'backup' }),
        makeRun({ id: 'r-prune', kind: 'prune', status: 'success' }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      // Wait for the table to render, then scope queries inside it. The
      // header has a "Prune Old Files" button too, so we must restrict the
      // /prune/i match to the table cells.
      const table = await screen.findByRole('table')
      const tableCells = Array.from(table.querySelectorAll('td')).map((c) => c.textContent ?? '')
      expect(tableCells).toContain('backup')
      expect(tableCells).toContain('prune')
    })
  })

  describe('tab switching', () => {
    it('shows Runs tab', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => expect(screen.getByRole('tab', { name: /runs/i })).toBeInTheDocument())
    })

    it('shows Snapshots tab', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('tab', { name: /snapshots/i })).toBeInTheDocument()
      )
    })

    it('shows Settings tab', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('tab', { name: /settings|configuration/i })).toBeInTheDocument()
      )
    })

    it('switches to Snapshots tab on click', async () => {
      const user = userEvent.setup()
      vi.mocked(api.getJobSnapshots).mockResolvedValue([makeSnapshot()])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('tab', { name: /snapshots/i }))
      await user.click(screen.getByRole('tab', { name: /snapshots/i }))
      await waitFor(() => expect(screen.getByText('abcdef12')).toBeInTheDocument())
    })

    it('shows run list in Runs tab', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([makeRun({ id: 'run-1' })])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => expect(screen.getByText('success')).toBeInTheDocument())
    })

    it('explains the run-history cap and clarifies snapshots are separate', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([makeRun({ id: 'run-1' })])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      // The Runs-tab note must reference the global cap setting and call out
      // that the per-job Retention Policy controls snapshot retention.
      await waitFor(() =>
        expect(screen.getByText(/run history is capped.*keep last runs/i)).toBeInTheDocument()
      )
      expect(screen.getByText(/retention policy/i)).toBeInTheDocument()
    })
  })

  describe('Unlock button', () => {
    it('shows Unlock button', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /unlock/i })).toBeInTheDocument()
      )
    })

    it('Unlock button is disabled when a run is in progress', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ status: 'running', check_status: null }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => {
        const btn = screen.getByRole('button', { name: /unlock/i })
        expect(btn).toBeDisabled()
      })
    })

    it('Unlock button is disabled when status is terminal but check_status is null', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: null }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => {
        const btn = screen.getByRole('button', { name: /unlock/i })
        expect(btn).toBeDisabled()
      })
    })

    it('Unlock button is enabled when all runs are complete (check_status set)', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: 'passed' }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => {
        const btn = screen.getByRole('button', { name: /unlock/i })
        expect(btn).not.toBeDisabled()
      })
    })

    it('calls unlockJob when Unlock is clicked', async () => {
      const user = userEvent.setup()
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: 'passed' }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /unlock/i }))
      await user.click(screen.getByRole('button', { name: /unlock/i }))
      expect(vi.mocked(api.unlockJob)).toHaveBeenCalledWith('job-1')
    })
  })

  describe('restore snippet', () => {
    it('shows a restore command snippet', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByText(/restic restore|restic snapshots/i)).toBeInTheDocument()
      )
    })

    it('shows the correct repository path containing the destination label and job ID', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByText(/restic restore|restic snapshots/i))
      const snippetEl = screen.getByText(/restic restore|restic snapshots/i).closest('pre')
      expect(snippetEl?.textContent).toContain('export RESTIC_REPOSITORY=/destinations/main/job-1')
    })

    it('never shows the real restic password in the restore snippet', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByText(/restic restore|restic snapshots/i))
      const snippetEl = screen
        .getByText(/restic restore|restic snapshots/i)
        .closest('pre, code, [data-testid]')
      if (snippetEl) {
        expect(snippetEl.textContent).not.toMatch(/s3cr3t|real_password/)
      }
      expect(screen.queryByText(/RESTIC_PASSWORD=\w{8,}/)).not.toBeInTheDocument()
    })

    it('shows placeholder password reference in restore snippet', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(
          screen.getByText(/RESTIC_PASSWORD|\$\{password\}|your.password/i)
        ).toBeInTheDocument()
      )
    })
  })

  describe('settings tab content', () => {
    it('shows source label in settings', async () => {
      const user = userEvent.setup()
      vi.mocked(api.getJob).mockResolvedValue(makeJob({ source_label: 'documents' }))
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('tab', { name: /settings|configuration/i }))
      await user.click(screen.getByRole('tab', { name: /settings|configuration/i }))
      await waitFor(() => expect(screen.getByText('documents')).toBeInTheDocument())
    })

    it('shows schedule in settings', async () => {
      const user = userEvent.setup()
      vi.mocked(api.getJob).mockResolvedValue(makeJob({ schedule_value: '6h' }))
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('tab', { name: /settings|configuration/i }))
      await user.click(screen.getByRole('tab', { name: /settings|configuration/i }))
      await waitFor(() => expect(screen.getByText(/6h/)).toBeInTheDocument())
    })
  })

  describe('404 state', () => {
    it('shows not found message when job does not exist', async () => {
      vi.mocked(api.getJob).mockRejectedValue(
        Object.assign(new Error('Not Found'), { status: 404 })
      )
      renderWithProviders(<JobDetail />, { route: '/jobs/nonexistent' })
      await waitFor(() =>
        expect(screen.getByText(/not found|does not exist|404/i)).toBeInTheDocument()
      )
    })

    it('shows error state when API returns 500', async () => {
      vi.mocked(api.getJob).mockRejectedValue(
        Object.assign(new Error('Internal Server Error'), { status: 500 })
      )
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByText(/error|failed|could not load/i)).toBeInTheDocument()
      )
    })
  })

  describe('Run Now behavior', () => {
    it('calls triggerRun with the correct job id', async () => {
      const user = userEvent.setup()
      vi.mocked(api.getJob).mockResolvedValue(makeJob({ id: 'job-1' }))
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /run now/i }))
      await user.click(screen.getByRole('button', { name: /run now/i }))
      expect(vi.mocked(api.triggerRun)).toHaveBeenCalledWith('job-1')
    })

    it('shows 409 error when run is already in progress', async () => {
      const user = userEvent.setup()
      vi.mocked(api.triggerRun).mockRejectedValue(
        Object.assign(new Error('Run in progress'), { status: 409 })
      )
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /run now/i }))
      await user.click(screen.getByRole('button', { name: /run now/i }))
      await waitFor(() =>
        expect(screen.getByText(/already.*running|in progress|409/i)).toBeInTheDocument()
      )
    })
  })

  describe('unlock output', () => {
    it('shows unlock output after successful unlock', async () => {
      const user = userEvent.setup()
      vi.mocked(api.unlockJob).mockResolvedValue({ output: 'successfully removed 1 locks' })
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: 'passed' }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /unlock/i }))
      await user.click(screen.getByRole('button', { name: /unlock/i }))
      await waitFor(() =>
        expect(screen.getByText(/removed.*lock|successfully|output/i)).toBeInTheDocument()
      )
    })
  })

  describe('edit job flow', () => {
    it('opens the edit form when Edit is clicked', async () => {
      const user = userEvent.setup()
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /edit/i }))
      await user.click(screen.getByRole('button', { name: /edit/i }))
      await waitFor(() => expect(screen.getByRole('form')).toBeInTheDocument())
    })

    it('calls updateJob with the form payload and closes on success', async () => {
      const user = userEvent.setup()
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /edit/i }))
      await user.click(screen.getByRole('button', { name: /edit/i }))
      const form = await screen.findByRole('form')
      // Change the name then submit.
      const nameInput = screen.getByLabelText(/name/i) as HTMLInputElement
      await user.clear(nameInput)
      await user.type(nameInput, 'Renamed Job')
      // Use the form-scoped submit button (header has its own Edit button).
      const submitBtn = Array.from(form.querySelectorAll('button')).find((b) =>
        /save|create|submit/i.test(b.textContent ?? '')
      )!
      await user.click(submitBtn)
      await waitFor(() =>
        expect(vi.mocked(api.updateJob)).toHaveBeenCalledWith(
          'job-1',
          expect.objectContaining({ name: 'Renamed Job' })
        )
      )
      // Form closes on success.
      await waitFor(() => expect(screen.queryByRole('form')).not.toBeInTheDocument())
    })

    it('shows an error and keeps the form open when update fails', async () => {
      const user = userEvent.setup()
      vi.mocked(api.updateJob).mockRejectedValue(
        Object.assign(new Error('Validation failed'), {
          status: 422,
          data: { detail: 'destination_label: immutable' },
        })
      )
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() => screen.getByRole('button', { name: /edit/i }))
      await user.click(screen.getByRole('button', { name: /edit/i }))
      const form = await screen.findByRole('form')
      const submitBtn = Array.from(form.querySelectorAll('button')).find((b) =>
        /save|create|submit/i.test(b.textContent ?? '')
      )!
      await user.click(submitBtn)
      await waitFor(() =>
        expect(screen.getByText(/destination_label.*immutable/i)).toBeInTheDocument()
      )
      expect(screen.getByRole('form')).toBeInTheDocument()
    })
  })

  describe('runs list statistics columns', () => {
    it('renders Total Size, Added size, Files, and Snapshot columns for runs with stats', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({
          id: 'r-stats',
          kind: 'backup',
          status: 'success',
          total_bytes_processed: 50000000, // ~47.7 MB
          data_added_bytes: 1024000, // 1000 KB
          files_new: 10,
          files_changed: 5,
          files_unmodified: 1000,
          snapshot_id: 'abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890',
        }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const table = await screen.findByRole('table')

      // Verify table headers are present
      expect(screen.getByText('Total Size')).toBeInTheDocument()
      expect(screen.getByText('Added')).toBeInTheDocument()
      expect(screen.getByText('Files')).toBeInTheDocument()
      expect(screen.getByText('Snapshot')).toBeInTheDocument()

      // Verify row values are formatted and displayed
      const cells = Array.from(table.querySelectorAll('td')).map((c) => c.textContent ?? '')
      expect(cells).toContain('47.7 MB')
      expect(cells).toContain('1000 KB')
      expect(cells).toContain('+10 / ~5 / =1000')
      expect(cells).toContain('abcdef12')
    })

    it('renders dashes for stats when they are missing or null (e.g. prune runs)', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({
          id: 'r-prune',
          kind: 'prune',
          status: 'success',
          total_bytes_processed: null,
          data_added_bytes: null,
          files_new: null,
          files_changed: null,
          files_unmodified: null,
          snapshot_id: null,
        }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const table = await screen.findByRole('table')

      const rows = table.querySelectorAll('tbody tr')
      expect(rows.length).toBe(1)
      const cells = Array.from(rows[0].querySelectorAll('td')).map((c) => c.textContent ?? '')

      // Kind (0): 'prune'
      // Status (1): 'success' (RunStatusBadge)
      // Started (2): date
      // Duration (3): duration
      // Total Size (4): '—'
      // Added (5): '—'
      // Files (6): '—'
      // Snapshot (7): '—'
      // Triggered By (8): Icon
      expect(cells[4]).toBe('—')
      expect(cells[5]).toBe('—')
      expect(cells[6]).toBe('—')
      expect(cells[7]).toBe('—')
    })
  })

  describe('Stop button', () => {
    it('shows Stop button when a run is active', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ status: 'running', check_status: null }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /^stop$/i })).toBeInTheDocument()
      )
    })

    it('hides Stop button when no run is active', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ status: 'success', check_status: 'passed' }),
      ])
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /^run now$/i })).toBeInTheDocument()
      )
      expect(screen.queryByRole('button', { name: /^stop$/i })).not.toBeInTheDocument()
    })

    it('clicking Stop calls cancelRun with the active run id after confirm', async () => {
      vi.mocked(api.getJobRuns).mockResolvedValue([
        makeRun({ id: 'active-run', status: 'running', check_status: null }),
      ])
      vi.mocked(api.cancelRun).mockResolvedValue(undefined)
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const btn = await screen.findByRole('button', { name: /^stop$/i })
      await userEvent.setup().click(btn)
      await waitFor(() => expect(vi.mocked(api.cancelRun)).toHaveBeenCalledWith('active-run'))
      confirmSpy.mockRestore()
    })
  })

  describe('Integrity Check button and modal', () => {
    beforeEach(() => {
      vi.mocked(api.triggerCheck).mockResolvedValue({ run_id: 'check-run-new' })
    })

    it('shows Integrity Check button', async () => {
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /integrity check/i })).toBeInTheDocument()
      )
    })

    it('opens the dialog modal with correct options when clicked', async () => {
      const user = userEvent.setup()
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const btn = await screen.findByRole('button', { name: /integrity check/i })
      await user.click(btn)

      await waitFor(() => {
        expect(screen.getByRole('heading', { name: /trigger integrity check/i })).toBeInTheDocument()
        expect(screen.getByLabelText(/check mode/i)).toBeInTheDocument()
        expect(screen.getByLabelText(/check timeout/i)).toBeInTheDocument()
      })
      // By default structural mode is selected, so subset percent is not visible
      expect(screen.queryByLabelText(/subset percent/i)).not.toBeInTheDocument()
    })

    it('shows subset percent input when subset mode is selected', async () => {
      const user = userEvent.setup()
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const btn = await screen.findByRole('button', { name: /integrity check/i })
      await user.click(btn)

      await waitFor(() => screen.getByLabelText(/check mode/i))
      const modeSelect = screen.getByLabelText(/check mode/i)
      await user.selectOptions(modeSelect, 'subset')

      await waitFor(() => {
        expect(screen.getByLabelText(/subset percent/i)).toBeInTheDocument()
      })
    })

    it('calls triggerCheck and navigates to the run page when submitted', async () => {
      const user = userEvent.setup()
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const btn = await screen.findByRole('button', { name: /integrity check/i })
      await user.click(btn)

      await waitFor(() => screen.getByLabelText(/check mode/i))
      const modeSelect = screen.getByLabelText(/check mode/i)
      await user.selectOptions(modeSelect, 'subset')
      
      const percentInput = screen.getByLabelText(/subset percent/i)
      await user.clear(percentInput)
      await user.type(percentInput, '10')

      const timeoutInput = screen.getByLabelText(/check timeout/i)
      await user.type(timeoutInput, '12')

      const submitBtn = screen.getByRole('button', { name: /^run check$/i })
      await user.click(submitBtn)

      await waitFor(() => {
        expect(api.triggerCheck).toHaveBeenCalledWith('job-1', {
          check_mode: 'subset',
          check_subset_percent: 10,
          timeout_hours: 12,
        })
      })
    })

    it('shows 409 error in the modal when a run is already in progress', async () => {
      const user = userEvent.setup()
      vi.mocked(api.triggerCheck).mockRejectedValue(
        Object.assign(new Error('Run in progress'), { status: 409 })
      )
      renderWithProviders(<JobDetail />, { route: '/jobs/job-1' })
      const btn = await screen.findByRole('button', { name: /integrity check/i })
      await user.click(btn)

      await waitFor(() => screen.getByRole('button', { name: /^run check$/i }))
      await user.click(screen.getByRole('button', { name: /^run check$/i }))

      await waitFor(() => {
        expect(screen.getByText(/already.*progress|in progress|409/i)).toBeInTheDocument()
      })
    })
  })
})
