import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import JobForm from '../components/JobForm'
import RunStatusBadge from '../components/RunStatusBadge'
import TriggeredByIcon from '../components/TriggeredByIcon'
import * as api from '../lib/api'
import type { BackupRun } from '../lib/types'

type Tab = 'runs' | 'snapshots' | 'settings'

function shouldPoll(runs: BackupRun[]): boolean {
  return runs.some((r) => r.status === 'running' || r.check_status === null)
}

export default function JobDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<Tab>('runs')
  const [runError, setRunError] = useState<string | null>(null)
  const [pruneError, setPruneError] = useState<string | null>(null)
  const [unlockOutput, setUnlockOutput] = useState<string | null>(null)
  const [unlockError, setUnlockError] = useState<string | null>(null)
  const [isEditing, setIsEditing] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)

  const { data: job, error: jobError } = useQuery({
    queryKey: ['job', id],
    queryFn: () => api.getJob(id ?? ''),
  })

  const { data: runs } = useQuery({
    queryKey: ['jobRuns', id],
    queryFn: () => api.getJobRuns(id ?? ''),
    refetchInterval: (q) => (shouldPoll(q.state.data ?? []) ? 100 : false),
  })

  const { data: snapshots } = useQuery({
    queryKey: ['jobSnapshots', id],
    queryFn: () => api.getJobSnapshots(id ?? ''),
  })

  // Mounts feed the source/destination dropdowns in the edit form. Fetched
  // unconditionally so the data is ready the moment the user clicks Edit.
  const { data: sourceMounts } = useQuery({
    queryKey: ['mounts', 'sources'],
    queryFn: api.listSourceMounts,
  })
  const { data: destinationMounts } = useQuery({
    queryKey: ['mounts', 'destinations'],
    queryFn: api.listDestinationMounts,
  })

  if (jobError) {
    const status = (jobError as { status?: number }).status
    if (status === 404) {
      return (
        <div className="p-6">
          <p>Job not found (404).</p>
        </div>
      )
    }
    return (
      <div className="p-6">
        <p>Error: could not load job.</p>
      </div>
    )
  }

  if (!job) return null

  const unlockDisabled =
    runs === undefined || runs.some((r) => r.status === 'running' || r.check_status === null)

  async function handleRunNow() {
    if (!job) return
    setRunError(null)
    try {
      const result = await api.triggerRun(job.id)
      navigate(`/runs/${result.run_id}`)
    } catch (err: unknown) {
      const status = (err as { status?: number }).status
      if (status === 409) {
        setRunError('A run is already in progress for this job.')
      } else {
        setRunError('Failed to trigger run.')
      }
    }
  }

  async function handlePruneNow() {
    if (!job) return
    setPruneError(null)
    try {
      const result = await api.triggerPrune(job.id)
      navigate(`/runs/${result.run_id}`)
    } catch (err: unknown) {
      const status = (err as { status?: number }).status
      if (status === 409) {
        setPruneError('A run is already in progress for this job.')
      } else {
        setPruneError('Failed to trigger prune.')
      }
    }
  }

  async function handleUnlock() {
    if (!job) return
    setUnlockError(null)
    setUnlockOutput(null)
    try {
      const result = await api.unlockJob(job.id)
      setUnlockOutput(result.output)
    } catch {
      setUnlockError('Failed to unlock repository.')
    }
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-2xl font-bold">{job.name}</h1>
        <span
          className={
            job.enabled
              ? 'bg-green-100 text-green-800 rounded-sm px-2 py-0.5 text-sm'
              : 'bg-slate-100 text-slate-600 rounded-sm px-2 py-0.5 text-sm'
          }
        >
          {job.enabled ? 'Enabled' : 'Disabled'}
        </span>
        <button
          onClick={handleRunNow}
          className="bg-primary text-primary-foreground hover:bg-primary/90 px-3 py-1 rounded-sm text-sm"
        >
          Run Now
        </button>
        <button
          onClick={handlePruneNow}
          className="border px-3 py-1 rounded text-sm"
          title="Reclaim space by running restic prune. This is heavy — run when you have time."
        >
          Prune Old Files
        </button>
        <button
          onClick={() => {
            setEditError(null)
            setIsEditing((v) => !v)
          }}
          aria-pressed={isEditing}
          className="border px-3 py-1 rounded text-sm"
        >
          {isEditing ? 'Cancel Edit' : 'Edit'}
        </button>
        <button
          onClick={handleUnlock}
          disabled={unlockDisabled}
          className="border px-3 py-1 rounded text-sm disabled:opacity-50"
        >
          Unlock
        </button>
      </div>

      {runError && <p className="text-destructive text-sm">{runError}</p>}
      {pruneError && <p className="text-destructive text-sm">{pruneError}</p>}
      {unlockOutput && <p className="text-sm text-green-700">Output: {unlockOutput}</p>}
      {unlockError && <p className="text-sm text-destructive">{unlockError}</p>}

      {isEditing && (
        <div className="space-y-3 border-t border-b border-border py-4">
          {editError && (
            <div className="bg-destructive/10 border border-destructive/30 rounded-sm p-3 text-sm text-destructive">
              {editError}
            </div>
          )}
          <JobForm
            job={job}
            sourceMounts={sourceMounts ?? []}
            destinationMounts={destinationMounts ?? []}
            onSubmit={async (data) => {
              setEditError(null)
              try {
                await api.updateJob(job.id, data)
                // Pull a fresh copy so the header + tabs reflect the change.
                await queryClient.invalidateQueries({ queryKey: ['job', id] })
                setIsEditing(false)
              } catch (err: unknown) {
                const detail = (err as { data?: { detail?: string } }).data?.detail
                setEditError(detail || 'Failed to save changes.')
              }
            }}
            onCancel={() => {
              setEditError(null)
              setIsEditing(false)
            }}
          />
        </div>
      )}

      <div role="tablist" className="flex gap-2 border-b">
        <button
          role="tab"
          aria-selected={tab === 'runs'}
          onClick={() => setTab('runs')}
          className={`px-3 py-1 text-sm ${tab === 'runs' ? 'border-b-2 border-primary text-primary font-medium' : ''}`}
        >
          Runs
        </button>
        <button
          role="tab"
          aria-selected={tab === 'snapshots'}
          onClick={() => setTab('snapshots')}
          className={`px-3 py-1 text-sm ${tab === 'snapshots' ? 'border-b-2 border-primary text-primary font-medium' : ''}`}
        >
          Snapshots
        </button>
        <button
          role="tab"
          aria-selected={tab === 'settings'}
          onClick={() => setTab('settings')}
          className={`px-3 py-1 text-sm ${tab === 'settings' ? 'border-b-2 border-primary text-primary font-medium' : ''}`}
        >
          Settings
        </button>
      </div>

      {tab === 'runs' && (
        <div>
          <p className="text-xs text-muted-foreground mb-2">
            Run history is capped by the global "Keep last runs" setting (default 100). Older run
            records are deleted automatically after each run. Backup snapshots in the repo are not
            affected — those are governed by this job's Retention Policy.
          </p>
          {(runs ?? []).length === 0 ? (
            <p className="text-muted-foreground text-sm">No runs yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left border-b">
                  <th className="py-2 pr-4">Kind</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Started</th>
                  <th className="py-2 pr-4">Duration</th>
                  <th className="py-2">Triggered By</th>
                </tr>
              </thead>
              <tbody className="[&>tr:nth-child(even)]:bg-muted/40">
                {(runs ?? []).map((r) => (
                  <tr key={r.id} className="border-b hover:bg-muted/60">
                    <td className="py-2 pr-4 capitalize">{r.kind}</td>
                    <td className="py-2 pr-4">
                      <Link to={`/runs/${r.id}`} className="hover:underline">
                        <RunStatusBadge status={r.status} />
                      </Link>
                    </td>
                    <td className="py-2 pr-4">{new Date(r.started_at).toLocaleString()}</td>
                    <td className="py-2 pr-4">
                      {r.duration_seconds != null ? `${r.duration_seconds}s` : '—'}
                    </td>
                    <td className="py-2">
                      <TriggeredByIcon value={r.triggered_by} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'snapshots' && (
        <div>
          {(snapshots ?? []).length === 0 ? (
            <p className="text-muted-foreground text-sm">No snapshots yet.</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {(snapshots ?? []).map((s) => (
                <li key={s.snapshot_id}>
                  <span>{s.snapshot_id.substring(0, 8)}</span>
                  {' — '}
                  {new Date(s.snapshot_time).toLocaleString()}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {tab === 'settings' && (
        <div className="space-y-2 text-sm">
          <div>
            Source: <span>{job.source_label}</span>
          </div>
          <div>
            Destination: <span>{job.destination_label}</span>
          </div>
          <div>
            Schedule: <span>{job.schedule_value}</span>
          </div>
        </div>
      )}

      <div className="mt-6">
        <h2 className="text-lg font-semibold mb-2">Restore</h2>
        <pre className="bg-muted rounded-sm p-3 text-xs overflow-auto">
          {`# Restore with restic
export RESTIC_REPOSITORY=/destinations/${job.destination_label}
export RESTIC_PASSWORD=your-password-here
restic snapshots
restic restore latest --target ./restored`}
        </pre>
      </div>
    </div>
  )
}
