import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import RunStatusBadge from '../components/RunStatusBadge'
import * as api from '../lib/api'
import type { BackupRun } from '../lib/types'

function shouldPoll(runs: BackupRun[]): boolean {
  return runs.some((r) => r.status === 'running' || r.check_status === null)
}

export default function Dashboard() {
  const { data: jobs, error: jobsError } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.listJobs,
  })

  const { data: runs, error: runsError } = useQuery({
    queryKey: ['recentRuns'],
    queryFn: () => api.getRecentRuns(10),
    refetchInterval: (query) => (shouldPoll(query.state.data ?? []) ? 100 : false),
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

      {jobs && jobs.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-2">Upcoming Runs</h2>
          <div className="space-y-1">
            {jobs.map((job) => (
              <div key={job.id} className="flex justify-between text-sm">
                <span>{job.name}</span>
                <span>
                  {job.next_run_time
                    ? `Next run: ${new Date(job.next_run_time).toLocaleString()}`
                    : '—'}
                </span>
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
              <th className="py-2 pr-4">Status</th>
              <th className="py-2 pr-4">Duration</th>
              <th className="py-2 pr-4">Started</th>
              <th className="py-2">Triggered By</th>
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
                <td className="py-2 pr-4 flex gap-1">
                  <RunStatusBadge status={run.status} />
                  {run.check_status && <RunStatusBadge status={run.check_status} />}
                </td>
                <td className="py-2 pr-4">
                  {run.duration_seconds != null ? `${run.duration_seconds}s` : '—'}
                </td>
                <td className="py-2 pr-4">{new Date(run.started_at).toLocaleString()}</td>
                <td className="py-2">{run.triggered_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
