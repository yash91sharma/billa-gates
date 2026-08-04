import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  History,
  KeyRound,
  Pencil,
  Play,
  ShieldCheck,
  Square,
  Trash2,
  X,
} from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import ActionTooltip from '../components/ActionTooltip'
import EmptyState from '../components/EmptyState'
import JobForm from '../components/JobForm'
import PageHeader from '../components/PageHeader'
import RefreshCountdown from '../components/RefreshCountdown'
import RunStatusBadge from '../components/RunStatusBadge'
import TriggeredByIcon from '../components/TriggeredByIcon'
import { StopRunDialog } from './Dashboard'
import * as api from '../lib/api'
import type { BackupRun, JobCommand, LockInfo, UnlockResult } from '../lib/types'
import { formatBytes, parseApiError, type ConflictingJob } from '../lib/utils'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '../components/ui/dialog'
import { Skeleton } from '../components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '../components/ui/table'
import { TooltipProvider } from '../components/ui/tooltip'

type Tab = 'runs' | 'snapshots' | 'commands' | 'settings'

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
    'Removes every restic lock from this job’s repository — including the ones restic refuses to clear on its own, which is what a run killed mid-write leaves behind when every later run then fails with "repository is already locked". Only this job’s repository is touched, and snapshots and their data are not. The result lists the locks that were actually removed.',
  unlockDisabled:
    'Unavailable while this job has a run in progress or an integrity check still finishing — that run holds the lock, and removing a live lock risks corrupting the repository.',
} as const

// At most once a minute — see Dashboard.tsx for the rationale. Named here
// because RefreshCountdown counts down against the same number: a bar that
// disagreed with the interval it describes would be worse than no bar.
const POLL_INTERVAL_MS = 60_000

function shouldPoll(runs: BackupRun[]): boolean {
  return runs.some((r) => r.status === 'running' || r.check_status === null)
}

function formatLockAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

/** One lock, in the terms that decide what the operator does next: whether it
 *  blocked everything or only writers, who held it, and how old it was. */
function LockLine({ lock }: { lock: LockInfo }) {
  const held = [
    lock.pid != null ? `PID ${lock.pid}` : null,
    lock.hostname ? `on ${lock.hostname}` : null,
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <li className="font-mono text-xs">
      <span className="font-semibold">{lock.short_id}</span>
      {lock.exclusive != null && <> · {lock.exclusive ? 'exclusive' : 'shared'}</>}
      {held && <> · held by {held}</>}
      {lock.age_seconds != null && <> · {formatLockAge(lock.age_seconds)} old</>}
    </li>
  )
}

/** What Unlock did, taken from the lock listings the server made on both sides
 *  of the removal.
 *
 * Never phrased from the fact that the request succeeded: `restic unlock`
 * exits 0 whether it removed every lock or none of them, so a page that
 * reports "unlocked" on a 200 tells an operator their repository is usable at
 * the exact moment it is not. The three outcomes are visually distinct on
 * purpose — locks removed, nothing there to remove, and locks that survived —
 * because only the last one means the job is still stuck.
 */
function UnlockReport({ result }: { result: UnlockResult }) {
  const { removed, remaining } = result
  return (
    <div className="space-y-2">
      {removed.length > 0 ? (
        <div className="rounded-lg border border-success/30 bg-success-subtle px-3 py-2 text-sm text-success-subtle-foreground">
          <p>
            Removed {removed.length} lock{removed.length === 1 ? '' : 's'}:
          </p>
          <ul className="mt-1 space-y-0.5">
            {removed.map((lock) => (
              <LockLine key={lock.id} lock={lock} />
            ))}
          </ul>
        </div>
      ) : (
        remaining.length === 0 && (
          <p className="rounded-lg border border-border bg-muted px-3 py-2 text-sm text-muted-foreground">
            No locks were present on this repository — nothing to remove.
          </p>
        )
      )}
      {remaining.length > 0 && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
          <p>
            {remaining.length} lock{remaining.length === 1 ? '' : 's'} could not be removed. Another
            process may be using this repository — check that no restic command is running against
            it, then try again.
          </p>
          <ul className="mt-1 space-y-0.5">
            {remaining.map((lock) => (
              <LockLine key={lock.id} lock={lock} />
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** Renders the server's command strings verbatim.
 *
 * Nothing here re-assembles a command line from job fields: the whole value of
 * this section is that it cannot disagree with the runner, and a second
 * assembly site in the browser would break that on the first flag added.
 */
function CommandList({ commands }: { commands: JobCommand[] }) {
  return (
    <ol className="space-y-4">
      {commands.map((c) => (
        <li key={c.step} className="space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold">{c.title}</h3>
            {!c.runs && (
              // A step the job's own configuration turns off. Saying so beats
              // printing a command line that never executes.
              <span className="bg-muted text-muted-foreground rounded-sm px-2 py-0.5 text-xs">
                does not run
              </span>
            )}
          </div>
          <p className="text-xs text-muted-foreground">{c.description}</p>
          {c.command === null ? (
            <p className="text-xs text-muted-foreground italic">{c.condition}</p>
          ) : (
            <>
              <pre className="bg-muted rounded-sm p-3 text-xs overflow-x-auto">
                {/* The environment the subprocess is given. The password is a
                  label, not the value — the API never returns it. */}
                {Object.entries(c.env).map(([k, v]) => (
                  <span key={k} className="block text-muted-foreground">{`${k}=${v}`}</span>
                ))}
                <code className="block">{c.command}</code>
              </pre>
              {c.condition && <p className="text-xs text-muted-foreground italic">{c.condition}</p>}
            </>
          )}
        </li>
      ))}
    </ol>
  )
}

export default function JobDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<Tab>('runs')
  const [runError, setRunError] = useState<string | null>(null)
  const [pruneError, setPruneError] = useState<string | null>(null)
  const [unlockResult, setUnlockResult] = useState<UnlockResult | null>(null)
  const [unlockError, setUnlockError] = useState<string | null>(null)
  const [isEditing, setIsEditing] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)
  const [editConflict, setEditConflict] = useState<ConflictingJob | null>(null)
  const [isCheckModalOpen, setIsCheckModalOpen] = useState(false)
  const [stopOpen, setStopOpen] = useState(false)
  const [checkMode, setCheckMode] = useState<'structural' | 'subset' | 'full'>('structural')
  const [checkSubsetPercent, setCheckSubsetPercent] = useState('5')
  const [checkTimeoutHours, setCheckTimeoutHours] = useState('')
  const [checkError, setCheckError] = useState<string | null>(null)

  const { data: job, error: jobError } = useQuery({
    queryKey: ['job', id],
    queryFn: () => api.getJob(id ?? ''),
  })

  const {
    data: runs,
    dataUpdatedAt: runsUpdatedAt,
    isFetching: runsFetching,
  } = useQuery({
    queryKey: ['jobRuns', id],
    queryFn: () => api.getJobRuns(id ?? ''),
    refetchInterval: (q) => (shouldPoll(q.state.data ?? []) ? POLL_INTERVAL_MS : false),
  })

  const { data: snapshots, error: snapshotsError } = useQuery({
    queryKey: ['jobSnapshots', id],
    queryFn: () => api.getJobSnapshots(id ?? ''),
  })

  // The exact restic commands a run of this job issues, rendered by the
  // backend from the same builders the runner execs. Re-fetched (never
  // re-derived here) so an edit is reflected the moment it is saved.
  const { data: commands, error: commandsError } = useQuery({
    queryKey: ['jobCommands', id],
    queryFn: () => api.getJobCommands(id ?? ''),
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

  // "Refresh" means the page, not just the query that polls: the snapshot list
  // and the command preview are fetched once on mount and never again, so a
  // button that only reloaded the run list would leave two tabs stale while
  // claiming the page was current. A mutation rather than four bare refetches
  // because `isPending` then covers the whole round trip — invalidateQueries
  // awaits the refetch it triggers. The mount lists are deliberately left out:
  // they feed the edit form's dropdowns and cannot change while a job runs.
  const refresh = useMutation({
    mutationFn: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['job', id] }),
        queryClient.invalidateQueries({ queryKey: ['jobRuns', id] }),
        queryClient.invalidateQueries({ queryKey: ['jobSnapshots', id] }),
        queryClient.invalidateQueries({ queryKey: ['jobCommands', id] }),
      ]),
  })

  if (jobError) {
    const status = (jobError as { status?: number }).status
    return (
      <>
        <PageHeader title="Job" breadcrumb={[{ label: 'Jobs', to: '/jobs' }]} />
        <Card className="border border-destructive/30 bg-destructive/5">
          <CardContent className="text-destructive">
            {status === 404 ? 'Job not found (404).' : 'Error: could not load job.'}
          </CardContent>
        </Card>
      </>
    )
  }

  // The page used to render nothing until the job landed, which showed a blank
  // white frame on every navigation into a job.
  if (!job) return <JobDetailSkeleton />

  const unlockDisabled =
    runs === undefined || runs.some((r) => r.status === 'running' || r.check_status === null)

  // First in-flight run (sequential per job — there can be at most one).
  const activeRun = runs?.find((r) => r.status === 'running')

  // Kept clickable while already editing so the form can still be closed.
  const editDisabled = !!activeRun && !isEditing

  // The same predicate the query above polls on, so the countdown cannot claim
  // an update that is not coming.
  const pollIntervalMs = shouldPoll(runs ?? []) ? POLL_INTERVAL_MS : null

  const backupRunCommands = (commands ?? []).filter((c) => c.group === 'backup_run')
  const onDemandCommands = (commands ?? []).filter((c) => c.group === 'on_demand')

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

  async function handleConfirmStop() {
    setStopOpen(false)
    if (!activeRun) return
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
    setUnlockResult(null)
    try {
      setUnlockResult(await api.unlockJob(job.id))
    } catch (err) {
      // The server's reason is the useful half here — an unreachable
      // destination and a refused removal need different actions from the
      // operator, and "failed to unlock" tells them apart from neither.
      setUnlockError(parseApiError(err).message ?? 'Failed to unlock repository.')
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
      <div className="space-y-4">
        <PageHeader
          breadcrumb={[{ label: 'Jobs', to: '/jobs' }]}
          title={job.name}
          status={
            <span
              className={`inline-flex items-center rounded-sm px-2 py-0.5 text-xs font-medium ${
                job.enabled
                  ? 'bg-success-subtle text-success-subtle-foreground'
                  : 'bg-neutral-subtle text-neutral-subtle-foreground'
              }`}
            >
              {job.enabled ? 'Enabled' : 'Disabled'}
            </span>
          }
          actions={
            <>
              <ActionTooltip content={ACTION_HELP.runNow}>
                <Button size="lg" onClick={handleRunNow}>
                  <Play aria-hidden="true" />
                  Run Now
                </Button>
              </ActionTooltip>
              {activeRun && (
                <ActionTooltip content={ACTION_HELP.stop}>
                  <Button
                    variant="outline"
                    size="lg"
                    className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => setStopOpen(true)}
                  >
                    <Square aria-hidden="true" />
                    Stop
                  </Button>
                </ActionTooltip>
              )}
              <ActionTooltip content={ACTION_HELP.prune}>
                <Button variant="outline" size="lg" onClick={handlePruneNow}>
                  <Trash2 aria-hidden="true" />
                  Prune Old Files
                </Button>
              </ActionTooltip>
              <ActionTooltip content={ACTION_HELP.check}>
                <Button
                  variant="outline"
                  size="lg"
                  onClick={() => {
                    setCheckError(null)
                    setIsCheckModalOpen(true)
                  }}
                >
                  <ShieldCheck aria-hidden="true" />
                  Integrity Check
                </Button>
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
                <Button
                  variant="outline"
                  size="lg"
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
                >
                  {isEditing ? <X aria-hidden="true" /> : <Pencil aria-hidden="true" />}
                  {isEditing ? 'Cancel Edit' : 'Edit'}
                </Button>
              </ActionTooltip>
              <ActionTooltip
                content={unlockDisabled ? ACTION_HELP.unlockDisabled : ACTION_HELP.unlock}
                disabled={unlockDisabled}
              >
                <Button
                  variant="outline"
                  size="lg"
                  onClick={handleUnlock}
                  disabled={unlockDisabled}
                >
                  <KeyRound aria-hidden="true" />
                  Unlock
                </Button>
              </ActionTooltip>
            </>
          }
        />

        <RefreshCountdown
          updatedAt={runsUpdatedAt}
          intervalMs={pollIntervalMs}
          isFetching={runsFetching}
          isRefreshing={refresh.isPending}
          onRefresh={() => refresh.mutate()}
        />

        {runError && <p className="text-sm text-destructive">{runError}</p>}
        {pruneError && <p className="text-sm text-destructive">{pruneError}</p>}
        {unlockResult && <UnlockReport result={unlockResult} />}
        {unlockError && <p className="text-sm text-destructive">{unlockError}</p>}

        <div className="flex items-start gap-2.5 rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
          <span>
            <strong>Notice on Disk Space:</strong> Restic retention policies (forgetting snapshots)
            only remove snapshot metadata reference points. They <strong>do not</strong>{' '}
            automatically free physical disk space. To reclaim physical space and avoid silent disk
            space accumulation, you must click the <strong>Prune Old Files</strong> button above.
            Pruning is not scheduled or automated to avoid performance impact.
          </span>
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
                  // The command preview is built from the job's own fields, so
                  // a stale cache here would keep showing the old excludes and
                  // retention — authoritative-looking and wrong.
                  await queryClient.invalidateQueries({ queryKey: ['jobCommands', id] })
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

        <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
          {/* The rule runs the full width; the tabs sit at its left end rather
              than stretching to fill it, so four short labels don't end up a
              screen apart. */}
          <div className="border-b border-border">
            <TabsList variant="line" className="w-fit justify-start">
              <TabsTrigger value="runs" className="flex-none px-3">
                Runs
              </TabsTrigger>
              <TabsTrigger value="snapshots" className="flex-none px-3">
                Snapshots
              </TabsTrigger>
              <TabsTrigger value="commands" className="flex-none px-3">
                Commands
              </TabsTrigger>
              <TabsTrigger value="settings" className="flex-none px-3">
                Settings
              </TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="runs">
            <Card>
              <CardContent className="space-y-3">
                <p className="text-xs text-muted-foreground">
                  Run history is capped by the global "Keep last runs" setting (default 100). Older
                  run records are deleted automatically after each run. Backup snapshots in the repo
                  are not affected — those are governed by this job's Retention Policy.
                </p>
                {(runs ?? []).length === 0 ? (
                  <EmptyState
                    icon={History}
                    title="No runs yet"
                    description="This job has not run. It will appear here on its next scheduled run, or as soon as you use Run Now."
                  />
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Kind</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Retention</TableHead>
                        <TableHead>Started</TableHead>
                        <TableHead numeric>Duration</TableHead>
                        <TableHead numeric>Total Size</TableHead>
                        <TableHead numeric>Added</TableHead>
                        <TableHead numeric>Files</TableHead>
                        <TableHead>Snapshot</TableHead>
                        <TableHead>Triggered By</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(runs ?? []).map((r) => (
                        <TableRow key={r.id} active={r.status === 'running'}>
                          <TableCell className="capitalize">{r.kind}</TableCell>
                          <TableCell>
                            <Link to={`/runs/${r.id}`} className="hover:underline">
                              <RunStatusBadge status={r.status} />
                            </Link>
                          </TableCell>
                          {/* `restic forget` is the retention policy; when it fails
                            it keeps failing, so the pattern has to be visible
                            across the history rather than one run at a time. */}
                          <TableCell>
                            <RunStatusBadge status={r.prune_status} />
                          </TableCell>
                          <TableCell className="tabular-nums whitespace-nowrap">
                            {new Date(r.started_at).toLocaleString()}
                          </TableCell>
                          <TableCell numeric>
                            {r.duration_seconds != null ? `${r.duration_seconds}s` : '—'}
                          </TableCell>
                          <TableCell numeric>{formatBytes(r.total_bytes_processed)}</TableCell>
                          <TableCell numeric>{formatBytes(r.data_added_bytes)}</TableCell>
                          <TableCell numeric className="whitespace-nowrap">
                            {r.files_new != null ||
                            r.files_changed != null ||
                            r.files_unmodified != null
                              ? `+${r.files_new ?? 0} / ~${r.files_changed ?? 0} / =${r.files_unmodified ?? 0}`
                              : '—'}
                          </TableCell>
                          <TableCell className="font-mono">
                            {r.snapshot_id ? r.snapshot_id.substring(0, 8) : '—'}
                          </TableCell>
                          <TableCell>
                            <TriggeredByIcon value={r.triggered_by} />
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="snapshots">
            <Card>
              <CardContent>
                {snapshotsError ? (
                  // Never render a failed listing as "no snapshots" — the repository
                  // is created with the job, so a failure here means the drive is
                  // detached, not that the backups are gone. Saying "none" would
                  // invite the user to delete and recreate the job.
                  <div className="flex items-start gap-2.5 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
                    <AlertTriangle
                      className="mt-0.5 size-4 shrink-0 text-warning"
                      aria-hidden="true"
                    />
                    <div className="space-y-1">
                      <p>
                        <strong>Could not list snapshots.</strong> The repository at{' '}
                        <code className="font-mono">
                          /destinations/{job.destination_label}/{job.name}
                        </code>{' '}
                        is not reachable — check that the destination drive is mounted. Your
                        snapshots are not affected by this.
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {parseApiError(snapshotsError).message ?? 'The listing request failed.'}
                      </p>
                    </div>
                  </div>
                ) : (snapshots ?? []).length === 0 ? (
                  <EmptyState
                    icon={History}
                    title="No snapshots yet."
                    description="The repository is reachable but holds nothing yet — the first successful backup writes a snapshot here."
                  />
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Snapshot</TableHead>
                        <TableHead>Taken</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(snapshots ?? []).map((s) => (
                        <TableRow key={s.snapshot_id}>
                          <TableCell className="font-mono">
                            {s.snapshot_id.substring(0, 8)}
                          </TableCell>
                          <TableCell className="tabular-nums">
                            {new Date(s.snapshot_time).toLocaleString()}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="commands">
            <Card>
              <CardContent className="space-y-6">
                <p className="text-xs text-muted-foreground">
                  The exact restic commands this job causes. They are generated by the same code
                  that runs them and are rebuilt from this job's current settings, so editing the
                  job changes what you see here.
                </p>
                {commandsError ? (
                  <div className="flex items-start gap-2.5 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
                    <AlertTriangle
                      className="mt-0.5 size-4 shrink-0 text-warning"
                      aria-hidden="true"
                    />
                    <div className="space-y-1">
                      <p>
                        <strong>Could not load the commands for this job.</strong>
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {parseApiError(commandsError).message ?? 'The request failed.'}
                      </p>
                    </div>
                  </div>
                ) : (
                  <>
                    <section aria-labelledby="commands-backup-run" className="space-y-3">
                      <h2 id="commands-backup-run" className="text-base font-semibold">
                        Backup run
                      </h2>
                      <p className="text-xs text-muted-foreground">
                        What one backup run issues, in the order the runner issues them — this is
                        what the schedule does, unattended.
                      </p>
                      <CommandList commands={backupRunCommands} />
                    </section>

                    {/* Kept visibly apart from the pipeline above: a backup and a
                  button click are different promises, and listing `restic
                  prune` among the backup steps would tell an operator their
                  schedule reclaims disk space, which it never does. */}
                    <section aria-labelledby="commands-on-demand" className="space-y-3">
                      <h2 id="commands-on-demand" className="text-base font-semibold">
                        Only when you click a button
                      </h2>
                      <p className="text-xs text-muted-foreground">
                        These are <strong>not part of a backup</strong> and they{' '}
                        <strong>never run on a schedule</strong>. Each one happens only when you use
                        the matching action at the top of this page.
                      </p>
                      <CommandList commands={onDemandCommands} />
                    </section>
                  </>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="settings">
            <Card>
              <CardContent>
                <dl className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-3">
                  <div>
                    <dt className="text-xs text-muted-foreground">Source</dt>
                    <dd className="text-sm">{job.source_label}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">Destination</dt>
                    <dd className="text-sm">{job.destination_label}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">Schedule</dt>
                    <dd className="text-sm tabular-nums">{job.schedule_value}</dd>
                  </div>
                </dl>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>

        <Card>
          <CardHeader>
            <CardTitle as="h2">Restore</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="overflow-auto rounded-md bg-muted p-3 font-mono text-xs">
              {`# Restore with restic
export RESTIC_REPOSITORY=/destinations/${job.destination_label}/${job.name}
export RESTIC_PASSWORD=your-password-here
restic snapshots
restic restore latest --target ./restored`}
            </pre>
          </CardContent>
        </Card>

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
                  className="h-8 w-full rounded-md border border-input bg-background px-2.5 text-sm focus-visible:border-ring"
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
                    className="h-8 w-full rounded-md border border-input bg-background px-2.5 text-sm focus-visible:border-ring"
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
                  className="h-8 w-full rounded-md border border-input bg-background px-2.5 text-sm focus-visible:border-ring"
                />
              </div>

              <DialogFooter className="mt-4 border-t pt-4">
                <Button
                  type="button"
                  variant="outline"
                  size="lg"
                  onClick={() => setIsCheckModalOpen(false)}
                >
                  Cancel
                </Button>
                <Button type="submit" size="lg">
                  Run Check
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>

        <StopRunDialog open={stopOpen} onOpenChange={setStopOpen} onConfirm={handleConfirmStop} />
      </div>
    </TooltipProvider>
  )
}

/**
 * Shown while the job itself is loading. Mirrors the real page's shape — a
 * header band, an action row, and a table — so the layout doesn't jump when
 * the data arrives.
 */
function JobDetailSkeleton() {
  return (
    <div className="space-y-4">
      <PageHeader title="Job" breadcrumb={[{ label: 'Jobs', to: '/jobs' }]} />
      <Card>
        <CardContent className="space-y-3">
          <Skeleton className="h-4 w-48" />
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
