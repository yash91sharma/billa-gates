import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../lib/api'
import type { DestinationUsage, DestinationUsageResponse } from '../lib/types'
import { renderWithProviders } from '../test/utils'
import Destinations from './Destinations'

vi.mock('../lib/api')

const GB = 1024 * 1024 * 1024
const TB = 1024 * GB

const makeDestination = (overrides: Partial<DestinationUsage> = {}): DestinationUsage => ({
  label: 'main',
  path: '/destinations/main',
  available: true,
  unavailable_reason: null,
  total_bytes: 2 * TB,
  used_bytes: 1.2 * TB,
  free_bytes: 700 * GB,
  reserved_bytes: 100 * GB,
  percent_used: 60,
  filesystem_id: '8:1',
  is_separate_mount: true,
  shares_filesystem_with: [],
  sentinel_present: true,
  job_count: 2,
  job_names: ['Documents Backup', 'Photos Backup'],
  measured_at: '2026-07-29T12:00:00Z',
  ...overrides,
})

const makeResponse = (
  destinations: DestinationUsage[],
  measured_at = '2026-07-29T12:00:00Z'
): DestinationUsageResponse => ({ measured_at, destinations })

const rowFor = (label: string) => screen.getByText(label).closest('tr') as HTMLElement

beforeEach(() => {
  vi.mocked(api.getDestinationUsage).mockResolvedValue(makeResponse([makeDestination()]))
})

describe('Destinations', () => {
  describe('table rendering', () => {
    it('renders a row per destination', async () => {
      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([
          makeDestination({ label: 'main' }),
          makeDestination({ label: 'offsite', filesystem_id: '8:2' }),
        ])
      )
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      expect(screen.getByText('offsite')).toBeInTheDocument()
    })

    it('shows used, free and total space', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      const row = rowFor('main')
      // A 2 TB drive must read as terabytes, not as "2048 GB".
      expect(within(row).getByText('1.2 TB')).toBeInTheDocument()
      expect(within(row).getByText('700 GB')).toBeInTheDocument()
      expect(within(row).getByText('2 TB')).toBeInTheDocument()
    })

    it('shows the percentage used as text beside the bar', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      const row = rowFor('main')
      expect(within(row).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '60')
      expect(within(row).getByText('60.0%')).toBeInTheDocument()
    })

    it('shows how many jobs write to each destination, and their names', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      const row = rowFor('main')
      expect(within(row).getByText('2')).toBeInTheDocument()
      expect(within(row).getByText(/Documents Backup/)).toBeInTheDocument()
      expect(within(row).getByText(/Photos Backup/)).toBeInTheDocument()
    })

    it('says when a destination has no jobs yet', async () => {
      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([makeDestination({ label: 'spare', job_count: 0, job_names: [] })])
      )
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('spare')).toBeInTheDocument())
      expect(within(rowFor('spare')).getByText(/no jobs/i)).toBeInTheDocument()
    })
  })

  describe('edge cases the operator has to know about', () => {
    it('shows the reason a destination could not be measured, and no invented zeros', async () => {
      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([
          makeDestination({
            label: 'nas',
            available: false,
            unavailable_reason: '/destinations/nas did not respond within 10.0s',
            total_bytes: null,
            used_bytes: null,
            free_bytes: null,
            reserved_bytes: null,
            percent_used: null,
            filesystem_id: null,
            is_separate_mount: null,
            sentinel_present: null,
          }),
        ])
      )
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('nas')).toBeInTheDocument())
      const row = rowFor('nas')
      expect(within(row).getByText(/did not respond/i)).toBeInTheDocument()
      expect(within(row).queryByText('0 B')).not.toBeInTheDocument()
      expect(within(row).getAllByText('—').length).toBeGreaterThan(0)
    })

    it('flags a destination that is not its own mount', async () => {
      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([makeDestination({ label: 'scratch', is_separate_mount: false })])
      )
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('scratch')).toBeInTheDocument())
      expect(within(rowFor('scratch')).getByText(/not a separate mount/i)).toBeInTheDocument()
    })

    it('names the sibling when two destinations share one filesystem', async () => {
      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([
          makeDestination({ label: 'main', shares_filesystem_with: ['spare'] }),
          makeDestination({ label: 'spare', shares_filesystem_with: ['main'] }),
        ])
      )
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      expect(within(rowFor('main')).getByText(/shares.*filesystem.*spare/i)).toBeInTheDocument()
    })

    it('flags a destination with no marker file', async () => {
      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([makeDestination({ label: 'unset', sentinel_present: false })])
      )
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('unset')).toBeInTheDocument())
      expect(within(rowFor('unset')).getByText(/marker file/i)).toBeInTheDocument()
    })

    it('flags a drive with no writable space left, even below 100%', async () => {
      // Reserved blocks mean a full drive reads 95%, so the warning keys on
      // free bytes rather than on the percentage.
      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([
          makeDestination({
            label: 'full',
            used_bytes: 1.9 * TB,
            free_bytes: 0,
            percent_used: 95,
          }),
        ])
      )
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('full')).toBeInTheDocument())
      expect(within(rowFor('full')).getByText(/no space left/i)).toBeInTheDocument()
    })

    it('states unconditionally that the figures do not add up', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      expect(screen.getByText(/does not add up to total/i)).toBeInTheDocument()
    })

    it('says when the figures were measured', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText(/measured/i)).toBeInTheDocument())
    })

    it('keeps the long explanation collapsed until asked for', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      expect(screen.queryByText(/must not be added/i)).not.toBeInTheDocument()

      await userEvent.click(screen.getByRole('button', { name: /how to read these numbers/i }))
      expect(screen.getByText(/must not be added/i)).toBeInTheDocument()
    })

    it('explains why df prints a different percentage', async () => {
      // Measured live: df says 59% where this page says 55.4% on the same
      // filesystem, because df divides by used + free and ignores the reserve.
      // An operator who cross-checks and finds two percentages stops trusting
      // both, so the page owns the difference.
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
      await userEvent.click(screen.getByRole('button', { name: /how to read these numbers/i }))
      expect(screen.getByText(/reads a few points higher/i)).toBeInTheDocument()
    })
  })

  describe('loading, empty and error states', () => {
    it('renders a skeleton while the request is in flight', () => {
      vi.mocked(api.getDestinationUsage).mockReturnValue(new Promise(() => {}))
      const { container } = renderWithProviders(<Destinations />, { route: '/destinations' })

      expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument()
    })

    it('renders an empty state when nothing is mounted', async () => {
      vi.mocked(api.getDestinationUsage).mockResolvedValue(makeResponse([]))
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText(/no destinations mounted/i)).toBeInTheDocument())
    })

    it('renders an error card when the request fails', async () => {
      vi.mocked(api.getDestinationUsage).mockRejectedValue(new Error('boom'))
      renderWithProviders(<Destinations />, { route: '/destinations' })

      await waitFor(() => expect(screen.getByText(/could not load/i)).toBeInTheDocument())
    })
  })

  describe('refresh', () => {
    it('re-reads the drives, bypassing the cached measurement', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })
      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())

      vi.mocked(api.getDestinationUsage).mockResolvedValue(
        makeResponse([makeDestination({ used_bytes: 1.6 * TB, percent_used: 80 })])
      )
      await userEvent.click(screen.getByRole('button', { name: /refresh/i }))

      await waitFor(() => expect(screen.getByText('1.6 TB')).toBeInTheDocument())
      expect(api.getDestinationUsage).toHaveBeenCalledWith(true)
    })

    it('does not poll on a timer', async () => {
      renderWithProviders(<Destinations />, { route: '/destinations' })
      await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())

      // The figures only move when a run writes, and the backend drops its
      // cache when one finishes — polling would spin up sleeping drives for
      // nothing.
      expect(vi.mocked(api.getDestinationUsage)).toHaveBeenCalledTimes(1)
    })
  })
})
