import { screen, waitFor } from '@testing-library/react'
import * as api from '../lib/api'
import type { BackupRun } from '../lib/types'
import { renderWithProviders } from '../test/utils'
import RunDetail from './RunDetail'

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
  snapshot_id: 'snap-abc',
  files_new: 10,
  files_changed: 5,
  files_unmodified: 1000,
  dirs_new: 2,
  dirs_changed: 1,
  dirs_unmodified: 50,
  data_added_bytes: 1024000,
  data_added_packed_bytes: 900000,
  total_bytes_processed: 50000000,
  backup_output: 'Files: 1000 new, 5 changed, 1000 unmodified',
  error_output: null,
  prune_status: 'passed',
  prune_error_output: null,
  check_status: 'passed',
  check_error_output: null,
  triggered_by: 'scheduler',
  ...overrides,
})

beforeEach(() => {
  vi.mocked(api.getRun).mockResolvedValue(makeRun())
})

describe('RunDetail', () => {
  describe('header info', () => {
    it('shows the job name', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ job_name: 'Home Photos' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('Home Photos')).toBeInTheDocument())
    })

    it('shows the run status badge', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ status: 'success' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('success')).toBeInTheDocument())
    })

    it('shows start time', async () => {
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() =>
        expect(screen.getByText(/jan.*2024|2024.*jan|10:00/i)).toBeInTheDocument()
      )
    })

    it('shows duration', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ duration_seconds: 120 }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/2.*min|120.*sec/i)).toBeInTheDocument())
    })

    it('shows triggered_by label', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ triggered_by: 'manual' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/manual/i)).toBeInTheDocument())
    })
  })

  describe('stats section', () => {
    it('shows files_new count', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ files_new: 42 }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('42')).toBeInTheDocument())
    })

    it('shows files_changed count', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ files_changed: 7 }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('7')).toBeInTheDocument())
    })

    it('shows data_added_bytes formatted', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ data_added_bytes: 1073741824 }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/1.*GB|1.*GiB/i)).toBeInTheDocument())
    })

    it('shows prune status', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ prune_status: 'passed' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('passed')).toBeInTheDocument())
    })

    it('shows check_status badge', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ check_status: 'passed' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getAllByText('passed').length).toBeGreaterThanOrEqual(1))
    })
  })

  describe('backup output log', () => {
    it('shows backup_output text', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ backup_output: 'Files: 10 new, 5 changed' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/Files: 10 new, 5 changed/)).toBeInTheDocument())
    })

    it('shows error_output when present', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ status: 'failed', error_output: 'connection refused' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/connection refused/i)).toBeInTheDocument())
    })

    it('shows check_error_output when check failed', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ check_status: 'failed', check_error_output: 'pack mismatch' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/pack mismatch/i)).toBeInTheDocument())
    })
  })

  describe('skipped run reason', () => {
    it('shows container_restart info card when reason is container_restart', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ status: 'skipped', reason: 'container_restart' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() =>
        expect(screen.getByText(/container.*restart|was skipped.*restart/i)).toBeInTheDocument()
      )
    })

    it('shows overlapping_run info when reason is overlapping_run', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ status: 'skipped', reason: 'overlapping_run' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() =>
        expect(
          screen.getByText(/overlapping|already.*running|skipped.*previous/i)
        ).toBeInTheDocument()
      )
    })
  })

  describe('locked repository callout', () => {
    it('shows locked-repo callout when error_output mentions locked', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({
          status: 'failed',
          error_output: 'unable to create lock in backend: repository is already locked',
        })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() =>
        expect(screen.getByText(/locked|repository.*lock|unlock/i)).toBeInTheDocument()
      )
    })
  })

  describe('polling behavior', () => {
    it('polls while run status is "running"', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ status: 'running', check_status: null }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(vi.mocked(api.getRun)).toHaveBeenCalled())
      expect(vi.mocked(api.getRun).mock.calls.length).toBeGreaterThanOrEqual(1)
    })

    it('stops polling when status is terminal and check_status is non-null', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ status: 'success', check_status: 'passed' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(vi.mocked(api.getRun)).toHaveBeenCalled())
      const callCount = vi.mocked(api.getRun).mock.calls.length
      await new Promise((r) => setTimeout(r, 100))
      expect(vi.mocked(api.getRun).mock.calls.length).toBe(callCount)
    })

    it('continues polling when status is success but check_status is null', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ status: 'success', check_status: null }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(vi.mocked(api.getRun)).toHaveBeenCalledTimes(2))
    })

    it('continues polling when status is failed but check_status is null', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ status: 'failed', check_status: null }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(vi.mocked(api.getRun)).toHaveBeenCalledTimes(2))
    })
  })

  describe('layout by status', () => {
    it('shows success layout for successful run', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ status: 'success' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('success')).toBeInTheDocument())
    })

    it('shows failed layout for failed run', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ status: 'failed', error_output: 'something went wrong' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => {
        expect(screen.getByText('failed')).toBeInTheDocument()
        expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()
      })
    })

    it('shows skipped layout for skipped run', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({
          status: 'skipped',
          reason: 'overlapping_run',
          finished_at: null,
          duration_seconds: null,
        })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('skipped')).toBeInTheDocument())
    })

    it('shows running layout with no finished time for in-progress runs', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({
          status: 'running',
          finished_at: null,
          duration_seconds: null,
          check_status: null,
        })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('running')).toBeInTheDocument())
    })
  })

  describe('404 state', () => {
    it('shows not found message when run does not exist', async () => {
      vi.mocked(api.getRun).mockRejectedValue(
        Object.assign(new Error('Not Found'), { status: 404 })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/nonexistent' })
      await waitFor(() =>
        expect(screen.getByText(/not found|does not exist|404/i)).toBeInTheDocument()
      )
    })

    it('shows error state when API returns 500', async () => {
      vi.mocked(api.getRun).mockRejectedValue(
        Object.assign(new Error('Internal Server Error'), { status: 500 })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() =>
        expect(screen.getByText(/error|failed|could not load/i)).toBeInTheDocument()
      )
    })
  })

  describe('prune status', () => {
    it('shows prune_status badge', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ prune_status: 'failed' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('failed')).toBeInTheDocument())
    })

    it('shows prune_error_output when prune failed', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ prune_status: 'failed', prune_error_output: 'disk full during prune' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/disk full during prune/i)).toBeInTheDocument())
    })

    it('shows prune_status skipped when not configured', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ prune_status: 'skipped' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('skipped')).toBeInTheDocument())
    })
  })

  describe('kind discriminator (gaps.md H1)', () => {
    it('renders a "prune" kind label on prune runs', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ kind: 'prune' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      // The kind badge appears next to the run-status badge in the header.
      // Use exact text match to avoid conflict with the "Prune:" status row
      // label that also matches /prune/i.
      await waitFor(() => expect(screen.getByText('prune')).toBeInTheDocument())
    })

    it('hides the Verification row entirely for prune runs', async () => {
      // Prune runs always have check_status=skipped so the verification line
      // would just say "Verification: skipped" — pure noise. Hide it.
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ kind: 'prune', check_status: 'skipped' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText('prune')).toBeInTheDocument())
      expect(screen.queryByText(/^Verification:/)).not.toBeInTheDocument()
    })
  })

  describe('triggered_by display', () => {
    it('shows "scheduler" triggered_by', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ triggered_by: 'scheduler' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/scheduler/i)).toBeInTheDocument())
    })

    it('shows "manual" triggered_by', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ triggered_by: 'manual' }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/manual/i)).toBeInTheDocument())
    })
  })

  describe('snapshot_id display', () => {
    it('shows snapshot id when present', async () => {
      vi.mocked(api.getRun).mockResolvedValue(
        makeRun({ snapshot_id: 'abcdef12abcdef12abcdef12abcdef12abcdef12abcdef12abcdef12abcdef12' })
      )
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/abcdef12/i)).toBeInTheDocument())
    })

    it('shows dash or N/A when no snapshot', async () => {
      vi.mocked(api.getRun).mockResolvedValue(makeRun({ snapshot_id: null }))
      renderWithProviders(<RunDetail />, { route: '/runs/run-1' })
      await waitFor(() => expect(screen.getByText(/^—$|^N\/A$|no snapshot/i)).toBeInTheDocument())
    })
  })
})
