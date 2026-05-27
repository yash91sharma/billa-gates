import type { Snapshot } from '../lib/types'
import { formatBytes } from '../lib/utils'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function formatDate(iso: string): string {
  const d = new Date(iso)
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`
}

export interface SnapshotListProps {
  snapshots: Snapshot[]
}

export default function SnapshotList({ snapshots }: SnapshotListProps) {
  if (snapshots.length === 0) {
    return <p className="text-muted-foreground text-sm">No snapshots yet</p>
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b text-left text-muted-foreground">
          <th className="py-2 pr-4">Snapshot ID</th>
          <th className="py-2 pr-4">Time</th>
          <th className="py-2 pr-4">Size</th>
          <th className="py-2 pr-4">Hostname</th>
          <th className="py-2 pr-4">Paths</th>
          <th className="py-2">Tags</th>
        </tr>
      </thead>
      <tbody className="[&>tr:nth-child(even)]:bg-muted/40">
        {snapshots.map((snap) => (
          <tr key={snap.snapshot_id} className="border-b last:border-0 hover:bg-muted/60">
            <td className="py-2 pr-4 font-mono">{snap.snapshot_id.slice(0, 8)}</td>
            <td className="py-2 pr-4">{formatDate(snap.snapshot_time)}</td>
            <td className="py-2 pr-4">{formatBytes(snap.size_bytes)}</td>
            <td className="py-2 pr-4">{snap.hostname}</td>
            <td className="py-2 pr-4">{snap.paths.join(', ')}</td>
            <td className="py-2">
              {snap.tags?.map((tag, i) => (
                <span key={i} className="mr-1">
                  {tag}
                </span>
              )) ?? ''}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
