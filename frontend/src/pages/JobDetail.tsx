import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import ActionTooltip from '../components/ActionTooltip'
import JobForm from '../components/JobForm'
import RunStatusBadge from '../components/RunStatusBadge'
import TriggeredByIcon from '../components/TriggeredByIcon'
import * as api from '../lib/api'
import type { BackupRun } from '../lib/types'
import { formatBytes, parseApiError, type ConflictingJob } from '../lib/utils'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '../components/ui/dialog'
import { TooltipProvider } from '../components/ui/tooltip'

type Tab = 'runs' | 'snapshots' | 'settings'

// Hover/focus help for the header actions. Each one says what the action does
// *and* what it changes for this job's repository — the labels alone don't
// distinguish "forget" from "prune", or a read-only check from a write.
const ACTION_HELP = {
  runNow:
    'Starts a backup of this job immediately, on top of its schedule. Writes a new snapshot to this job’s repository, then applies the retention policy. The schedule and next run time are unchanged.',
  stop: 'Cancels the run in progress. Data already uploaded stays in the repository and is reused by the next run, but no snapshot is written for the cancelled run. The schedule is unaffected.',
  prune:
    'Runs restic prune: deletes the data behind forgotten snapshots and is the only thing that frees space on the destination drive — retention alone just drops snapshot references. Heavy on disk and I/O, can take a long time, and no other run for this job can start until it finishes.',
  check:
    'Runs restic check to verify this job’s repository is complete and consistent. Read-only — nothing is backed up, changed or deleted. Subset and full modes also re-read pack data and can take a long time.',
  edit: 'Opens this job’s settings — schedule, retention, excludes and restic options. Name, destination and repository password are permanent: together they address the repository that already holds your snapshots.',
  editDisabled:
    'A run is in progress. Stop it before editing — the backup reads this job’s settings mid-run, so the server rejects changes until it finishes.',
  cancelEdit:
    'Closes the edit form and discards any unsaved changes. The job keeps its current settings.',
  unlock:
    'Removes stale restic locks from this job’s repository — the ones left behind when a previous run was killed mid-write and every later run fails with "repository is already locked". Snapshots and their data are not touched.',
  unlockDisabled:
    'Unavailable while this job has a run in progress or an integrity check still finishing — that run holds the lock, and removing a live lock risks corrupting the repository.',
} as const

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
  const [editConflict, setEditConflict] = useState<ConflictingJob | null>(null)
  const [isCheckModalOpen, setIsCheckModalOpen] = useState(false)
  const [checkMode, setCheckMode] = useState<'structural' | 'subset' | 'full'>('structural')
  const [checkSubsetPercent, setCheckSubsetPercent] = useState('5')
  const [checkTimeoutHours, setCheckTimeoutHours] = useState('')
  const [checkError, setCheckError] = useState<string | null>(null)

  const { data: job, error: jobError } = useQuery({
    queryKey: ['job', id],
    queryFn: () => api.getJob(id ?? ''),
  })

  const { data: runs } = useQuery({
    queryKey: ['jobRuns', id],
    queryFn: () => api.getJobRuns(id ?? ''),
    // At most once a minute — see Dashboard.tsx for rationale.
    refetchInterval: (q) => (shouldPoll(q.state.data ?? []) ? 60_000 : false),
  })

  const { data: snapshots, error: snapshotsError } = useQuery({
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

  // First in-flight run (sequential per job — there can be at most one).
  const activeRun = runs?.find((r) => r.status === 'running')

  // Kept clickable while already editing so the form can still be closed.
  const editDisabled = !!activeRun && !isEditing

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

  async function handleStop() {
    if (!activeRun) return
    if (!window.confirm('Cancel this running backup? Already-uploaded data is kept.')) {
      return
    }
    try {
      await api.cancelRun(activeRun.id)
      await queryClient.invalidateQueries({ queryKey: ['jobRuns', id] })
    } catch {
      // Polling will reconcile; ignore (409 if run just finished, etc).
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

  async function handleCheckSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!job) return
    setCheckError(null)
    try {
      const result = await api.triggerCheck(job.id, {
        check_mode: checkMode,
        check_subset_percent: checkMode === 'subset' ? parseInt(checkSubsetPercent) : null,
        timeout_hours: checkTimeoutHours ? parseInt(checkTimeoutHours) : null,
      })
      setIsCheckModalOpen(false)
      navigate(`/runs/${result.run_id}`)
    } catch (err: unknown) {
      const status = (err as { status?: number }).status
      if (status === 409) {
        setCheckError('A run is already in progress for this job.')
      } else {
        const { message } = parseApiError(err)
        setCheckError(message || 'Failed to trigger integrity check.')
      }
    }
  }

  return (
    <TooltipProvider>
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
          <ActionTooltip content={ACTION_HELP.runNow}>
            <button
              onClick={handleRunNow}
              className="bg-primary text-primary-foreground hover:bg-primary/90 px-3 py-1 rounded-sm text-sm"
            >
              Run Now
            </button>
          </ActionTooltip>
          {activeRun && (
            <ActionTooltip content={ACTION_HELP.stop}>
              <button
                onClick={handleStop}
                className="border border-destructive/40 text-destructive hover:bg-destructive/10 px-3 py-1 rounded-sm text-sm"
              >
                Stop
              </button>
            </ActionTooltip>
          )}
          <ActionTooltip content={ACTION_HELP.prune}>
            <button onClick={handlePruneNow} className="border px-3 py-1 rounded text-sm">
              Prune Old Files
            </button>
          </ActionTooltip>
          <ActionTooltip content={ACTION_HELP.check}>
            <button
              onClick={() => {
                setCheckError(null)
                setIsCheckModalOpen(true)
              }}
              className="border px-3 py-1 rounded text-sm"
            >
              Integrity Check
            </button>
          </ActionTooltip>
          <ActionTooltip
            content={
              editDisabled
                ? ACTION_HELP.editDisabled
                : isEditing
                  ? ACTION_HELP.cancelEdit
                  : ACTION_HELP.edit
            }
            disabled={editDisabled}
          >
            <button
              onClick={() => {
                setEditError(null)
                setEditConflict(null)
                setIsEditing((v) => !v)
              }}
              aria-pressed={isEditing}
              // A live run reads job fields mid-pipeline and the backend rejects
              // PUT with 409 — don't offer an edit that can only fail. The run
              // must be stopped first. (Kept clickable while already editing so
              // the form can still be closed.)
              disabled={editDisabled}
              className="border px-3 py-1 rounded text-sm disabled:opacity-50"
            >
              {isEditing ? 'Cancel Edit' : 'Edit'}
            </button>
          </ActionTooltip>
          <ActionTooltip
            content={unlockDisabled ? ACTION_HELP.unlockDisabled : ACTION_HELP.unlock}
            disabled={unlockDisabled}
          >
            <button
              onClick={handleUnlock}
              disabled={unlockDisabled}
              className="border px-3 py-1 rounded text-sm disabled:opacity-50"
            >
              Unlock
            </button>
          </ActionTooltip>
        </div>

        {runError && <p className="text-destructive text-sm">{runError}</p>}
        {pruneError && <p className="text-destructive text-sm">{pruneError}</p>}
        {unlockOutput && <p className="text-sm text-green-700">Output: {unlockOutput}</p>}
        {unlockError && <p className="text-sm text-destructive">{unlockError}</p>}

        <div className="bg-warning/15 border border-warning/40 rounded-sm p-3 text-xs text-foreground">
          ⚠️ <strong>Notice on Disk Space:</strong> Restic retention policies (forgetting snapshots)
          only remove snapshot metadata reference points. They <strong>do not</strong> automatically
          free physical disk space. To reclaim physical space and avoid silent disk space
          accumulation, you must click the <strong>Prune Old Files</strong> button above. Pruning is
          not scheduled or automated to avoid performance impact.
        </div>

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
              conflictingJob={editConflict ?? undefined}
              onSubmit={async (data) => {
                setEditError(null)
                setEditConflict(null)
                try {
                  await api.updateJob(job.id, data)
                  // Pull a fresh copy so the header + tabs reflect the change.
                  await queryClient.invalidateQueries({ queryKey: ['job', id] })
                  setIsEditing(false)
                } catch (err: unknown) {
                  // The duplicate-job 409 nests an object in detail; parseApiError
                  // flattens it (rendering it raw would crash React).
                  const { message, conflictingJob } = parseApiError(err)
                  setEditError(message || 'Failed to save changes.')
                  setEditConflict(conflictingJob)
                }
              }}
              onCancel={() => {
                setEditError(null)
                setEditConflict(null)
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
                    <th className="py-2 pr-4">Retention</th>
                    <th className="py-2 pr-4">Started</th>
                    <th className="py-2 pr-4">Duration</th>
                    <th className="py-2 pr-4">Total Size</th>
                    <th className="py-2 pr-4">Added</th>
                    <th className="py-2 pr-4">Files</th>
                    <th className="py-2 pr-4">Snapshot</th>
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
                      {/* `restic forget` is the retention policy; when it fails
                        it keeps failing, so the pattern has to be visible
                        across the history rather than one run at a time. */}
                      <td className="py-2 pr-4">
                        <RunStatusBadge status={r.prune_status} />
                      </td>
                      <td className="py-2 pr-4">{new Date(r.started_at).toLocaleString()}</td>
                      <td className="py-2 pr-4">
                        {r.duration_seconds != null ? `${r.duration_seconds}s` : '—'}
                      </td>
                      <td className="py-2 pr-4">{formatBytes(r.total_bytes_processed)}</td>
                      <td className="py-2 pr-4">{formatBytes(r.data_added_bytes)}</td>
                      <td className="py-2 pr-4">
                        {r.files_new != null ||
                        r.files_changed != null ||
                        r.files_unmodified != null
                          ? `+${r.files_new ?? 0} / ~${r.files_changed ?? 0} / =${r.files_unmodified ?? 0}`
                          : '—'}
                      </td>
                      <td className="py-2 pr-4 font-mono">
                        {r.snapshot_id ? r.snapshot_id.substring(0, 8) : '—'}
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
            {snapshotsError ? (
              // Never render a failed listing as "no snapshots" — the repository
              // is created with the job, so a failure here means the drive is
              // detached, not that the backups are gone. Saying "none" would
              // invite the user to delete and recreate the job.
              <div className="bg-warning/15 border border-warning/40 rounded-sm p-3 text-sm space-y-1">
                <p>
                  <strong>Could not list snapshots.</strong> The repository at{' '}
                  <code>
                    /destinations/{job.destination_label}/{job.name}
                  </code>{' '}
                  is not reachable — check that the destination drive is mounted. Your snapshots are
                  not affected by this.
                </p>
                <p className="text-xs text-muted-foreground">
                  {parseApiError(snapshotsError).message ?? 'The listing request failed.'}
                </p>
              </div>
            ) : (snapshots ?? []).length === 0 ? (
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
export RESTIC_REPOSITORY=/destinations/${job.destination_label}/${job.name}
export RESTIC_PASSWORD=your-password-here
restic snapshots
restic restore latest --target ./restored`}
          </pre>
        </div>

        <Dialog open={isCheckModalOpen} onOpenChange={setIsCheckModalOpen}>
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle>Trigger Integrity Check</DialogTitle>
              <DialogDescription>
                Verify the structural consistency and completeness of your backup repository.
              </DialogDescription>
            </DialogHeader>

            {checkError && (
              <div className="bg-destructive/10 border border-destructive/30 rounded-sm p-3 text-sm text-destructive">
                {checkError}
              </div>
            )}

            <form onSubmit={handleCheckSubmit} className="space-y-4">
              <div className="space-y-1">
                <label
                  htmlFor="modal-check-mode"
                  className="text-xs font-semibold text-muted-foreground block"
                >
                  Check Mode
                </label>
                <select
                  id="modal-check-mode"
                  value={checkMode}
                  onChange={(e) => setCheckMode(e.target.value as any)}
                  className="border rounded px-2 py-1 text-sm w-full bg-background"
                >
                  <option value="structural">Structural</option>
                  <option value="subset">Subset</option>
                  <option value="full">Full</option>
                </select>
              </div>

              {checkMode === 'subset' && (
                <div className="space-y-1">
                  <label
                    htmlFor="modal-check-subset-percent"
                    className="text-xs font-semibold text-muted-foreground block"
                  >
                    Subset Percent
                  </label>
                  <input
                    id="modal-check-subset-percent"
                    type="number"
                    min={1}
                    max={100}
                    value={checkSubsetPercent}
                    onChange={(e) => setCheckSubsetPercent(e.target.value)}
                    className="border rounded px-2 py-1 text-sm w-full bg-background"
                    required
                  />
                </div>
              )}

              <div className="space-y-1">
                <label
                  htmlFor="modal-check-timeout-hours"
                  className="text-xs font-semibold text-muted-foreground block"
                >
                  Check Timeout (hours)
                </label>
                <input
                  id="modal-check-timeout-hours"
                  type="number"
                  min={1}
                  max={168}
                  value={checkTimeoutHours}
                  onChange={(e) => setCheckTimeoutHours(e.target.value)}
                  placeholder="24"
                  className="border rounded px-2 py-1 text-sm w-full bg-background"
                />
              </div>

              <div className="flex justify-end gap-2 pt-2 border-t mt-4">
                <button
                  type="button"
                  onClick={() => setIsCheckModalOpen(false)}
                  className="px-4 py-2 border rounded text-sm hover:bg-muted font-medium"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-2 bg-primary text-primary-foreground hover:bg-primary/90 rounded text-sm font-medium"
                >
                  Run Check
                </button>
              </div>
            </form>
          </DialogContent>
        </Dialog>
      </div>
    </TooltipProvider>
  )
}
