import { render, screen } from '@testing-library/react'
import type { Snapshot } from '../lib/types'
import SnapshotList from './SnapshotList'

const makeSnapshot = (overrides: Partial<Snapshot> = {}): Snapshot => ({
  snapshot_id: 'abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890',
  snapshot_time: '2024-01-15T10:30:00Z',
  hostname: 'myhost',
  paths: ['/sources/documents'],
  tags: ['weekly'],
  size_bytes: 524288000, // 500 MB
  ...overrides,
})

describe('SnapshotList', () => {
  describe('empty state', () => {
    it('shows empty state message when no snapshots', () => {
      render(<SnapshotList snapshots={[]} />)
      expect(screen.getByText(/no snapshots/i)).toBeInTheDocument()
    })

    it('does not render a table when empty', () => {
      render(<SnapshotList snapshots={[]} />)
      expect(screen.queryByRole('table')).not.toBeInTheDocument()
    })
  })

  describe('snapshot rows', () => {
    it('renders one row per snapshot', () => {
      render(
        <SnapshotList snapshots={[makeSnapshot(), makeSnapshot({ snapshot_id: 'b'.repeat(64) })]} />
      )
      const rows = screen.getAllByRole('row')
      expect(rows.length).toBeGreaterThanOrEqual(2)
    })

    it('shows the short snapshot ID (first 8 chars)', () => {
      render(<SnapshotList snapshots={[makeSnapshot()]} />)
      expect(screen.getByText('abcdef12')).toBeInTheDocument()
    })

    it('shows snapshot time in human-readable format', () => {
      render(<SnapshotList snapshots={[makeSnapshot()]} />)
      expect(screen.getByText(/jan.*2024|2024.*jan/i)).toBeInTheDocument()
    })

    it('shows hostname', () => {
      render(<SnapshotList snapshots={[makeSnapshot()]} />)
      expect(screen.getByText('myhost')).toBeInTheDocument()
    })

    it('shows paths', () => {
      render(<SnapshotList snapshots={[makeSnapshot()]} />)
      expect(screen.getByText('/sources/documents')).toBeInTheDocument()
    })

    it('shows tags when present', () => {
      render(<SnapshotList snapshots={[makeSnapshot({ tags: ['weekly', 'manual'] })]} />)
      expect(screen.getByText('weekly')).toBeInTheDocument()
      expect(screen.getByText('manual')).toBeInTheDocument()
    })
  })

  describe('size formatting', () => {
    it('formats 500 MB correctly', () => {
      render(<SnapshotList snapshots={[makeSnapshot({ size_bytes: 524288000 })]} />)
      expect(screen.getByText(/500.*MB|500.*MiB/i)).toBeInTheDocument()
    })

    it('formats 1 GB correctly', () => {
      render(<SnapshotList snapshots={[makeSnapshot({ size_bytes: 1073741824 })]} />)
      expect(screen.getByText(/1.*GB|1.*GiB/i)).toBeInTheDocument()
    })

    it('formats bytes for small sizes', () => {
      render(<SnapshotList snapshots={[makeSnapshot({ size_bytes: 1024 })]} />)
      expect(screen.getByText(/1.*KB|1024.*B/i)).toBeInTheDocument()
    })

    it('shows dash or N/A for null size', () => {
      render(<SnapshotList snapshots={[makeSnapshot({ size_bytes: null })]} />)
      expect(screen.getByText(/^—$|^N\/A$|^-$/)).toBeInTheDocument()
    })
  })

  describe('column headers', () => {
    it('renders expected column headers', () => {
      render(<SnapshotList snapshots={[makeSnapshot()]} />)
      expect(screen.getByText(/snapshot id/i)).toBeInTheDocument()
      expect(screen.getByText(/time/i)).toBeInTheDocument()
      expect(screen.getByText(/size/i)).toBeInTheDocument()
    })
  })
})
