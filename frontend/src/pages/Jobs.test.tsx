import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../lib/api'
import type { BackupJob } from '../lib/types'
import { renderWithProviders } from '../test/utils'
import Jobs from './Jobs'

vi.mock('../lib/api')

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

beforeEach(() => {
  vi.mocked(api.listJobs).mockResolvedValue([])
  vi.mocked(api.deleteJob).mockResolvedValue(undefined)
  vi.mocked(api.enableJob).mockResolvedValue({ id: 'job-1', enabled: true })
  vi.mocked(api.disableJob).mockResolvedValue({ id: 'job-1', enabled: false })
  vi.mocked(api.triggerRun).mockResolvedValue({ run_id: 'run-abc' })
  vi.mocked(api.listSourceMounts).mockResolvedValue(['meh1', 'meh2'])
  vi.mocked(api.listDestinationMounts).mockResolvedValue(['main'])
})

describe('Jobs', () => {
  describe('table rendering', () => {
    it('shows empty state when no jobs exist', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText(/no jobs|no backup jobs/i)).toBeInTheDocument())
    })

    it('renders a row for each job', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'Job A' }),
        makeJob({ id: 'job-2', name: 'Job B' }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => {
        expect(screen.getByText('Job A')).toBeInTheDocument()
        expect(screen.getByText('Job B')).toBeInTheDocument()
      })
    })

    it('shows job name as link to detail page', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'My Docs' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => {
        const link = screen.getByRole('link', { name: /My Docs/i })
        expect(link).toBeInTheDocument()
      })
    })

    it('shows source and destination labels', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ source_label: 'photos', destination_label: 'nas' }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => {
        expect(screen.getByText('photos')).toBeInTheDocument()
        expect(screen.getByText('nas')).toBeInTheDocument()
      })
    })

    it('shows schedule value', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ schedule_value: '12h' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText(/12h/)).toBeInTheDocument())
    })

    it('shows enabled status indicator', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ enabled: true })])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText(/enabled/i)).toBeInTheDocument())
    })

    it('shows disabled status indicator for disabled jobs', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ enabled: false })])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText(/disabled/i)).toBeInTheDocument())
    })

    it('shows last run status when available', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({
          last_run: {
            id: 'run-1',
            kind: 'backup',
            status: 'success',
            check_status: 'passed',
            started_at: '2024-01-15T10:00:00Z',
            finished_at: '2024-01-15T10:02:00Z',
            duration_seconds: 120,
            triggered_by: 'scheduler',
          },
        }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText('success')).toBeInTheDocument())
    })

    it('shows Create Job button', async () => {
      renderWithProviders(<Jobs />)
      await waitFor(() =>
        expect(
          screen.getByRole('button', { name: /create.*job|new.*job|add.*job/i })
        ).toBeInTheDocument()
      )
    })
  })

  describe('enable/disable toggle', () => {
    // Implementation may use role="switch" (shadcn Switch) or role="checkbox".
    // Helper finds whichever is present.
    function getToggle() {
      return (
        screen.queryByRole('switch', { name: /enabled|toggle/i }) ??
        screen.queryByRole('checkbox', { name: /enabled|toggle/i }) ??
        screen.queryByRole('switch') ??
        screen.queryByRole('checkbox')
      )
    }

    it('calls disableJob when toggling off an enabled job', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', enabled: true })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      const toggle = getToggle()
      expect(toggle).not.toBeNull()
      await user.click(toggle!)
      expect(vi.mocked(api.disableJob)).toHaveBeenCalledWith('job-1')
    })

    it('calls enableJob when toggling on a disabled job', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', enabled: false })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      const toggle = getToggle()
      expect(toggle).not.toBeNull()
      await user.click(toggle!)
      expect(vi.mocked(api.enableJob)).toHaveBeenCalledWith('job-1')
    })
  })

  describe('Run Now action', () => {
    it('calls triggerRun when Run Now is clicked', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /run now/i }))
      expect(vi.mocked(api.triggerRun)).toHaveBeenCalledWith('job-1')
    })

    it('shows 409 error toast when a run is already in progress', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1' })])
      vi.mocked(api.triggerRun).mockRejectedValue(
        Object.assign(new Error('Run already in progress'), { status: 409 })
      )
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /run now/i }))
      await waitFor(() =>
        expect(screen.getByText(/already.*running|in progress|409/i)).toBeInTheDocument()
      )
    })
  })

  describe('delete confirmation dialog', () => {
    it('shows confirmation dialog when delete is clicked', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'Test Job' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
      expect(screen.getByText(/are you sure/i)).toBeInTheDocument()
    })

    it('calls deleteJob keeping the repository by default', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'Test Job' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      await waitFor(() => screen.getByText(/are you sure/i))
      await user.click(screen.getByRole('button', { name: /yes.*delete/i }))
      expect(vi.mocked(api.deleteJob)).toHaveBeenCalledWith('job-1', false)
    })

    it('calls deleteJob with delete_repository when the checkbox is ticked', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'Test Job' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      await waitFor(() => screen.getByText(/are you sure/i))
      await user.click(screen.getByRole('checkbox', { name: /also permanently delete/i }))
      await user.click(screen.getByRole('button', { name: /yes.*delete/i }))
      expect(vi.mocked(api.deleteJob)).toHaveBeenCalledWith('job-1', true)
    })

    it('resets the delete-repository checkbox between jobs', async () => {
      // A value carried over from a previous dialog would destroy a
      // repository the user never agreed to delete.
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'First' }),
        makeJob({ id: 'job-2', name: 'Second' }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('First'))

      await user.click(screen.getAllByRole('button', { name: /^delete$/i })[0])
      await user.click(screen.getByRole('checkbox', { name: /also permanently delete/i }))
      await user.click(screen.getByRole('button', { name: /cancel/i }))

      await user.click(screen.getAllByRole('button', { name: /^delete$/i })[1])
      expect(screen.getByRole('checkbox', { name: /also permanently delete/i })).not.toBeChecked()
    })

    it('does not call deleteJob when cancel is clicked', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'Test Job' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      await waitFor(() => screen.getByText(/confirm|are you sure/i))
      await user.click(screen.getByRole('button', { name: /cancel/i }))
      expect(vi.mocked(api.deleteJob)).not.toHaveBeenCalled()
    })

    it('shows job name in the delete confirmation dialog', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'Important Backup' }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Important Backup'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      // The name appears both in the prose and in the repository path.
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getAllByText(/Important Backup/).length).toBeGreaterThan(0)
    })

    it('shows the repository path that will be kept or deleted', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'photos', destination_label: 'main' }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('photos'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText('/destinations/main/photos')).toBeInTheDocument()
    })
  })

  describe('create job flow', () => {
    it('shows job form when Create Job is clicked', async () => {
      const user = userEvent.setup()
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByRole('button', { name: /create.*job|new.*job/i }))
      await user.click(screen.getByRole('button', { name: /create.*job|new.*job/i }))
      await waitFor(() => expect(screen.getByRole('form')).toBeInTheDocument())
    })

    it('shows an error banner and keeps the form open when create fails', async () => {
      const user = userEvent.setup()
      vi.mocked(api.createJob).mockRejectedValue(
        Object.assign(new Error('Validation failed'), {
          status: 422,
          data: { detail: 'restic_password: field required' },
        })
      )
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByRole('button', { name: /create.*job|new.*job/i }))
      await user.click(screen.getByRole('button', { name: /create.*job|new.*job/i }))
      const form = await screen.findByRole('form')
      // Submit the form (the page also has a "Create Job" button — scope to the form).
      await user.type(within(form).getByLabelText(/name/i), 'Test Job')
      await user.click(within(form).getByRole('button', { name: /save|create|submit/i }))
      await waitFor(() => {
        expect(screen.getByText(/restic_password.*required/i)).toBeInTheDocument()
      })
      expect(screen.getByRole('form')).toBeInTheDocument()
    })

    it('shows the message and conflicting-job link when create fails with a duplicate 409', async () => {
      // The duplicate-job 409 nests an OBJECT in detail — rendering it
      // directly as a React child crashes the whole app (blank page).
      const user = userEvent.setup()
      vi.mocked(api.createJob).mockRejectedValue(
        Object.assign(new Error('Conflict'), {
          status: 409,
          data: {
            detail: {
              message: 'A job with the same source label and destination label already exists',
              conflicting_job_id: 'job-9',
              conflicting_job_name: 'Existing Job',
            },
          },
        })
      )
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByRole('button', { name: /create.*job|new.*job/i }))
      await user.click(screen.getByRole('button', { name: /create.*job|new.*job/i }))
      const form = await screen.findByRole('form')
      await user.type(within(form).getByLabelText(/name/i), 'Test Job')
      await user.click(within(form).getByRole('button', { name: /save|create|submit/i }))
      await waitFor(() => {
        expect(screen.getByText(/already exists/i)).toBeInTheDocument()
      })
      const conflictLink = screen.getByRole('link', { name: 'Existing Job' })
      expect(conflictLink).toHaveAttribute('href', '/jobs/job-9')
      expect(screen.getByRole('form')).toBeInTheDocument()
    })

    it('blocks the page with a working dialog while the create is in flight', async () => {
      // POST /api/jobs validates both mount sentinels and runs `restic init`
      // before it answers, so the click is followed by seconds of nothing.
      // The page must say it is working — and stop accepting input — until
      // the request settles.
      const user = userEvent.setup()
      let resolveCreate!: (job: BackupJob) => void
      vi.mocked(api.createJob).mockReturnValue(
        new Promise<BackupJob>((resolve) => {
          resolveCreate = resolve
        })
      )
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByRole('button', { name: /create.*job|new.*job/i }))
      await user.click(screen.getByRole('button', { name: /create.*job|new.*job/i }))
      const form = await screen.findByRole('form')
      await user.type(within(form).getByLabelText(/name/i), 'Test Job')
      const submit = within(form).getByRole('button', { name: /save|create|submit/i })
      await user.click(submit)

      const dialog = await screen.findByRole('dialog')
      expect(dialog).toHaveTextContent(/creating/i)
      expect(submit).toBeDisabled()
      expect(within(form).getByLabelText(/name/i)).toBeDisabled()

      resolveCreate(makeJob())
      // The form closes on success, taking the indicator with it.
      await waitFor(() => expect(screen.queryByRole('form')).not.toBeInTheDocument())
    })

    it('populates source and destination dropdowns from the mounts API', async () => {
      const user = userEvent.setup()
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByRole('button', { name: /create.*job|new.*job/i }))
      await user.click(screen.getByRole('button', { name: /create.*job|new.*job/i }))
      await waitFor(() => expect(screen.getByRole('form')).toBeInTheDocument())

      const source = screen.getByLabelText(/source/i) as HTMLSelectElement
      const dest = screen.getByLabelText(/destination/i) as HTMLSelectElement
      // beforeEach mocks: sources=['meh1','meh2'], destinations=['main']
      await waitFor(() => {
        expect(Array.from(source.options).map((o) => o.value)).toEqual(
          expect.arrayContaining(['meh1', 'meh2'])
        )
        expect(Array.from(dest.options).map((o) => o.value)).toEqual(
          expect.arrayContaining(['main'])
        )
      })
    })
  })

  describe('error states', () => {
    it('shows error message when jobs API fails', async () => {
      vi.mocked(api.listJobs).mockRejectedValue(new Error('Network error'))
      renderWithProviders(<Jobs />)
      await waitFor(() =>
        expect(screen.getByText(/error|failed|could not load/i)).toBeInTheDocument()
      )
    })

    it('shows a run-in-progress message when delete fails with 409', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'Test Job' })])
      vi.mocked(api.deleteJob).mockRejectedValue(
        Object.assign(new Error('A backup run is in progress for this job'), {
          status: 409,
          data: { detail: 'A backup run is in progress for this job' },
        })
      )
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      await waitFor(() => screen.getByText(/confirm|are you sure/i))
      await user.click(screen.getByRole('button', { name: /confirm|yes.*delete|delete.*job/i }))
      await waitFor(() => expect(screen.getByText(/run.*in progress/i)).toBeInTheDocument())
    })

    it('shows error when delete fails with non-409 error', async () => {
      const user = userEvent.setup()
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'Test Job' })])
      vi.mocked(api.deleteJob).mockRejectedValue(
        Object.assign(new Error('Server Error'), { status: 500 })
      )
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      await user.click(screen.getByRole('button', { name: /delete/i }))
      await waitFor(() => screen.getByText(/confirm|are you sure/i))
      await user.click(screen.getByRole('button', { name: /confirm|yes.*delete|delete.*job/i }))
      await waitFor(() => expect(screen.getByText(/error|failed/i)).toBeInTheDocument())
    })
  })

  describe('enable/disable toggle', () => {
    it('shows a toggle control per job', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', enabled: true })])
      renderWithProviders(<Jobs />)
      await waitFor(() => screen.getByText('Test Job'))
      const toggle = screen.queryByRole('switch') ?? screen.queryByRole('checkbox')
      expect(toggle).not.toBeNull()
    })
  })

  describe('live run row highlight', () => {
    // `last_run` is the most recent run, so `last_run.status === 'running'`
    // means this job is backing up right now. The Jobs table is the page an
    // operator lands on to check on things, and a badge in the Last Run column
    // is the only thing that said so.
    const runningRun = (overrides: Partial<NonNullable<BackupJob['last_run']>> = {}) => ({
      id: 'run-live',
      kind: 'backup' as const,
      status: 'running' as const,
      check_status: null,
      started_at: '2024-01-15T10:00:00Z',
      finished_at: null,
      duration_seconds: null,
      triggered_by: 'scheduler' as const,
      ...overrides,
    })

    const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

    it('marks the row of a job that is running right now', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'Alpha', last_run: runningRun() }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument())
      expect(rowOf('Alpha')).toHaveAttribute('data-active', 'true')
    })

    it('leaves idle jobs unmarked, whatever their last run did', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'Alpha', last_run: runningRun() }),
        makeJob({
          id: 'job-2',
          name: 'Beta',
          last_run: runningRun({ id: 'run-ok', status: 'failed', check_status: 'skipped' }),
        }),
        makeJob({ id: 'job-3', name: 'Gamma', last_run: null }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText('Gamma')).toBeInTheDocument())
      expect(rowOf('Beta')).not.toHaveAttribute('data-active')
      expect(rowOf('Gamma')).not.toHaveAttribute('data-active')
    })

    it('polls while a job is running so the highlight clears itself', async () => {
      // Without this the page only refetched on mount and on window focus, so a
      // tab left open would keep shouting "running" at a job that finished
      // hours ago — a highlight that lies is worse than none.
      vi.useFakeTimers({ shouldAdvanceTime: true })
      try {
        vi.mocked(api.listJobs).mockResolvedValue([
          makeJob({ id: 'job-1', name: 'Alpha', last_run: runningRun() }),
        ])
        renderWithProviders(<Jobs />)
        await waitFor(() => expect(vi.mocked(api.listJobs)).toHaveBeenCalled())
        await vi.advanceTimersByTimeAsync(60_000)
        await waitFor(() => expect(vi.mocked(api.listJobs)).toHaveBeenCalledTimes(2))
      } finally {
        vi.useRealTimers()
      }
    })

    it('does not poll when nothing is running', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([makeJob({ id: 'job-1', name: 'Alpha' })])
      renderWithProviders(<Jobs />)
      await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument())
      const calls = vi.mocked(api.listJobs).mock.calls.length
      await new Promise((r) => setTimeout(r, 100))
      expect(vi.mocked(api.listJobs).mock.calls.length).toBe(calls)
    })
  })

  describe('job list sorting', () => {
    it('shows all jobs when multiple exist', async () => {
      vi.mocked(api.listJobs).mockResolvedValue([
        makeJob({ id: 'job-1', name: 'Alpha' }),
        makeJob({ id: 'job-2', name: 'Beta' }),
        makeJob({ id: 'job-3', name: 'Gamma' }),
      ])
      renderWithProviders(<Jobs />)
      await waitFor(() => {
        expect(screen.getByText('Alpha')).toBeInTheDocument()
        expect(screen.getByText('Beta')).toBeInTheDocument()
        expect(screen.getByText('Gamma')).toBeInTheDocument()
      })
    })
  })
})
