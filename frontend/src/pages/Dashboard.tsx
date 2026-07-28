import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import RunStatusBadge from '../components/RunStatusBadge'
import TriggeredByIcon from '../components/TriggeredByIcon'
import * as api from '../lib/api'
import type { BackupRun, RunKind } from '../lib/types'

const KIND_BADGE_BASE = 'rounded-sm px-2 py-0.5 text-xs font-medium capitalize'
const KIND_CLASS: Record<RunKind, string> = {
  backup: `bg-sky-100 text-sky-800 ${KIND_BADGE_BASE}`,
  prune: `bg-amber-100 text-amber-800 ${KIND_BADGE_BASE}`,
  check: `bg-purple-100 text-purple-800 ${KIND_BADGE_BASE}`,
}

function shouldPoll(runs: BackupRun[]): boolean {
  return runs.some((r) => r.status === 'running' || r.check_status === null)
}

function formatNextRun(nextRunTimeIso: string | null): string {
  if (!nextRunTimeIso) {
    return '—'
  }
  const nextDate = new Date(nextRunTimeIso)
  const formattedDate = nextDate.toLocaleString()
  const diffMs = Math.max(0, nextDate.getTime() - Date.now())
  const totalHours = Math.floor(diffMs / (1000 * 60 * 60))
  const days = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  const dayUnit = days === 1 ? 'day' : 'days'
  const hourUnit = hours === 1 ? 'hour' : 'hours'

  return `Next run: ${formattedDate} (In ${days} ${dayUnit}, ${hours} ${hourUnit})`
}

export default function Dashboard() {
  const queryClient = useQueryClient()
  const { data: jobs, error: jobsError } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.listJobs,
  })

  async function handleStop(runId: string) {
    if (!window.confirm('Cancel this running backup? Already-uploaded data is kept.')) {
      return
    }
    try {
      await api.cancelRun(runId)
      await queryClient.invalidateQueries({ queryKey: ['recentRuns'] })
    } catch {
      // The polling refetch will reconcile; ignore (409 if already finished).
    }
  }

  const { data: runs, error: runsError } = useQuery({
    queryKey: ['recentRuns'],
    queryFn: () => api.getRecentRuns(10),
    // Poll at most once a minute while a run is live — anything faster
    // hammers the backend (and its request log) for the whole duration of
    // a multi-hour backup without telling the user anything new.
    refetchInterval: (query) => (shouldPoll(query.state.data ?? []) ? 60_000 : false),
  })

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: api.getHealth,
  })

  if (jobsError || runsError) {
    return (
      <div className="p-6">
        <p className="text-destructive">Error: could not load dashboard data.</p>
      </div>
    )
  }

  const totalJobs = jobs?.length ?? 0
  const enabledCount = jobs?.filter((j) => j.enabled).length ?? 0

  const sortedJobs = jobs
    ? [...jobs].sort((a, b) => {
        if (a.next_run_time && b.next_run_time) {
          return new Date(a.next_run_time).getTime() - new Date(b.next_run_time).getTime()
        }
        if (a.next_run_time && !b.next_run_time) {
          return -1
        }
        if (!a.next_run_time && b.next_run_time) {
          return 1
        }
        return a.name.localeCompare(b.name)
      })
    : []

  return (
    <div className="p-6 space-y-6">
      {health && !health.scheduler_running && (
        <div className="bg-destructive/10 border border-destructive/30 text-destructive rounded-sm p-4">
          Scheduler is not running. Check the container logs for details.
        </div>
      )}

      <div className="grid grid-cols-3 gap-4">
        <div className="border rounded p-4">
          <div className="text-2xl font-bold">{totalJobs}</div>
          <div className="text-sm text-muted-foreground">Total Jobs</div>
        </div>
        <div className="border rounded p-4">
          <div>{enabledCount} enabled</div>
        </div>
        <div className="border rounded p-4">
          <div>restic {health?.restic_version ?? 'not detected'}</div>
        </div>
      </div>

      <div className="bg-warning/15 border border-warning/40 rounded-sm p-3 text-sm text-foreground">
        Note: disk space is not monitored by this application.
      </div>

      {sortedJobs.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-2">Upcoming Runs</h2>
          <div className="[&>div:nth-child(even)]:bg-muted/40">
            {sortedJobs.map((job) => (
              <div
                key={job.id}
                className="flex justify-between text-sm py-2 px-3 border-b hover:bg-muted/60"
              >
                <span>{job.name}</span>
                <span>{formatNextRun(job.next_run_time)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <h2 className="text-lg font-semibold mb-2">Recent Runs</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left border-b">
              <th className="py-2 pr-4">Job</th>
              <th className="py-2 pr-4">Backup</th>
              <th className="py-2 pr-4">Verification</th>
              <th className="py-2 pr-4">Kind</th>
              <th className="py-2 pr-4">Duration</th>
              <th className="py-2 pr-4">Started</th>
              <th className="py-2 pr-4">Triggered By</th>
              <th className="py-2"></th>
            </tr>
          </thead>
          <tbody className="[&>tr:nth-child(even)]:bg-muted/40">
            {(runs ?? []).map((run) => (
              <tr key={run.id} className="border-b hover:bg-muted/60">
                <td className="py-2 pr-4">
                  <Link to={`/runs/${run.id}`} className="text-primary hover:underline">
                    {run.job_name ?? run.job_id}
                  </Link>
                </td>
                <td className="py-2 pr-4">
                  <RunStatusBadge status={run.status} />
                </td>
                <td className="py-2 pr-4">
                  {run.kind === 'prune' || !run.check_status ? (
                    '—'
                  ) : (
                    <RunStatusBadge status={run.check_status} />
                  )}
                </td>
                <td className="py-2 pr-4">
                  <span data-testid={`kind-badge-${run.id}`} className={KIND_CLASS[run.kind]}>
                    {run.kind}
                  </span>
                </td>
                <td className="py-2 pr-4">
                  {run.duration_seconds != null ? `${run.duration_seconds}s` : '—'}
                </td>
                <td className="py-2 pr-4">{new Date(run.started_at).toLocaleString()}</td>
                <td className="py-2 pr-4">
                  <TriggeredByIcon value={run.triggered_by} />
                </td>
                <td className="py-2">
                  {run.status === 'running' && (
                    <button
                      onClick={() => handleStop(run.id)}
                      className="border border-destructive/40 text-destructive hover:bg-destructive/10 px-2 py-0.5 rounded-sm text-xs"
                    >
                      Stop
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
