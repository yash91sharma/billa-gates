import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import RunStatusBadge from '../components/RunStatusBadge'
import * as api from '../lib/api'
import type { BackupRun } from '../lib/types'
import { formatBytes } from '../lib/utils'

function shouldPoll(run: BackupRun): boolean {
  return run.status === 'running' || run.check_status === null
}

function formatDuration(seconds: number): string {
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} hr`
  if (seconds >= 60) return `${Math.round(seconds / 60)} min`
  return `${seconds} sec`
}

export default function RunDetail() {
  const { id } = useParams<{ id: string }>()

  const { data: run, error } = useQuery({
    queryKey: ['run', id],
    queryFn: () => api.getRun(id ?? ''),
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return false
      return shouldPoll(data) ? 100 : false
    },
  })

  if (error) {
    const status = (error as { status?: number }).status
    if (status === 404) {
      return (
        <div className="p-6">
          <p>Run not found (404).</p>
        </div>
      )
    }
    return (
      <div className="p-6">
        <p>Error: could not load run.</p>
      </div>
    )
  }

  if (!run) return null

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <Link to={`/jobs/${run.job_id}`} className="text-primary hover:underline font-medium">
          {run.job_name ?? run.job_id}
        </Link>
        <span className="border rounded-sm px-2 py-0.5 text-xs capitalize">{run.kind}</span>
        <RunStatusBadge status={run.status} />
      </div>

      <div className="text-sm text-muted-foreground space-y-1">
        <div>Started: {new Date(run.started_at).toLocaleString()}</div>
        <div>Triggered by: {run.triggered_by}</div>
        {run.duration_seconds != null && (
          <div>Duration: {formatDuration(run.duration_seconds)}</div>
        )}
        <div>
          Snapshot: <span>{run.snapshot_id ? run.snapshot_id.substring(0, 8) : '—'}</span>
        </div>
      </div>

      {run.reason === 'container_restart' && (
        <div className="bg-primary/10 border border-primary/30 text-foreground rounded-sm p-3 text-sm">
          This run was interrupted and marked failed due to a container restart.
        </div>
      )}

      {run.reason === 'overlapping_run' && (
        <div className="bg-primary/10 border border-primary/30 text-foreground rounded-sm p-3 text-sm">
          This run was skipped — a previous run was already running (overlapping).
        </div>
      )}

      {run.error_output?.includes('locked') && (
        <div className="bg-warning/15 border border-warning/40 rounded-sm p-3 text-sm">
          The repository is locked. Use the unlock button to remove the lock.
        </div>
      )}

      {run.files_new != null && (
        <div className="grid grid-cols-3 gap-3 text-sm">
          <div>
            New files: <span>{run.files_new}</span>
          </div>
          <div>
            Changed: <span>{run.files_changed}</span>
          </div>
          {run.data_added_bytes != null && <div>Added: {formatBytes(run.data_added_bytes)}</div>}
        </div>
      )}

      {run.backup_output && (
        <pre className="bg-muted rounded-sm p-3 text-xs overflow-auto max-h-64">
          {run.backup_output}
        </pre>
      )}

      {run.error_output && !run.error_output.includes('locked') && (
        <pre className="bg-destructive/10 text-destructive rounded-sm p-3 text-xs overflow-auto max-h-64">
          {run.error_output}
        </pre>
      )}

      <div className="text-sm">
        <span className="text-muted-foreground mr-2">Prune:</span>
        <RunStatusBadge status={run.prune_status} />
        {run.prune_status === 'failed' && run.prune_error_output && (
          <pre className="mt-2 bg-destructive/10 text-destructive rounded-sm p-2 text-xs">
            {run.prune_error_output}
          </pre>
        )}
      </div>

      {run.kind !== 'prune' && (
        <div className="text-sm">
          <span className="text-muted-foreground mr-2">Verification:</span>
          <RunStatusBadge status={run.check_status} />
          {run.check_status === 'failed' && run.check_error_output && (
            <pre className="mt-2 bg-destructive/10 text-destructive rounded-sm p-2 text-xs">
              {run.check_error_output}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
