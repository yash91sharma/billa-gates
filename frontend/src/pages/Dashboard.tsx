import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Briefcase, CalendarClock, History, Power, Terminal } from 'lucide-react'
import type { ComponentType, SVGProps } from 'react'
import { Link } from 'react-router-dom'
import EmptyState from '../components/EmptyState'
import PageHeader from '../components/PageHeader'
import RunStatusBadge from '../components/RunStatusBadge'
import TriggeredByIcon from '../components/TriggeredByIcon'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog'
import { Skeleton } from '../components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '../components/ui/table'
import * as api from '../lib/api'
import type { BackupRun, RunKind } from '../lib/types'

const KIND_BADGE_BASE =
  'inline-flex items-center rounded-sm px-2 py-0.5 text-xs font-medium capitalize'
// Semantic tokens rather than raw palette classes: `backup` and `running` used
// to be sky-100 and blue-100 respectively — two names for one idea that drifted
// apart because nothing tied them together.
const KIND_CLASS: Record<RunKind, string> = {
  backup: `bg-info-subtle text-info-subtle-foreground ${KIND_BADGE_BASE}`,
  prune: `bg-warning-subtle text-warning-subtle-foreground ${KIND_BADGE_BASE}`,
  check: `bg-verify-subtle text-verify-subtle-foreground ${KIND_BADGE_BASE}`,
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
  const [runToStop, setRunToStop] = useState<BackupRun | null>(null)

  const {
    data: jobs,
    error: jobsError,
    isPending: jobsPending,
  } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.listJobs,
  })

  async function handleConfirmStop() {
    const run = runToStop
    setRunToStop(null)
    if (!run) return
    try {
      await api.cancelRun(run.id)
      await queryClient.invalidateQueries({ queryKey: ['recentRuns'] })
    } catch {
      // The polling refetch will reconcile; ignore (409 if already finished).
    }
  }

  const {
    data: runs,
    error: runsError,
    isPending: runsPending,
  } = useQuery({
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
      <>
        <PageHeader title="Dashboard" />
        <Card className="border border-destructive/30 bg-destructive/5">
          <CardContent className="text-destructive">
            Error: could not load dashboard data.
          </CardContent>
        </Card>
      </>
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
    <>
      <PageHeader
        title="Dashboard"
        description="What is scheduled, what has run, and whether the scheduler is alive."
      />

      {health && !health.scheduler_running && (
        <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>Scheduler is not running. Check the container logs for details.</span>
        </div>
      )}

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <MetricCard
          icon={Briefcase}
          label="Total jobs"
          value={jobsPending ? null : String(totalJobs)}
        />
        <MetricCard
          icon={Power}
          label="Enabled"
          value={jobsPending ? null : `${enabledCount} of ${totalJobs}`}
        />
        <MetricCard
          icon={Terminal}
          label="restic"
          value={health ? (health.restic_version ?? 'not detected') : null}
        />
      </div>

      <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
        <span>Note: disk space is not monitored by this application.</span>
      </div>

      {sortedJobs.length > 0 && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle as="h2" className="flex items-center gap-2">
              <CalendarClock className="size-4 text-muted-foreground" aria-hidden="true" />
              Upcoming Runs
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="divide-y divide-border">
              {sortedJobs.map((job) => (
                <li
                  key={job.id}
                  data-testid={`upcoming-${job.id}`}
                  className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-2.5 text-sm"
                >
                  <span data-slot="upcoming-job-name" className="font-medium">
                    {job.name}
                  </span>
                  <span className="tabular-nums text-muted-foreground">
                    {formatNextRun(job.next_run_time)}
                  </span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle as="h2" className="flex items-center gap-2">
            <History className="size-4 text-muted-foreground" aria-hidden="true" />
            Recent Runs
          </CardTitle>
        </CardHeader>
        <CardContent>
          {runsPending ? (
            <RecentRunsSkeleton />
          ) : (runs ?? []).length === 0 ? (
            <EmptyState
              icon={History}
              title="No runs yet"
              description="Runs appear here as soon as a job fires — on its schedule, or when you trigger one by hand."
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Job</TableHead>
                  <TableHead>Backup</TableHead>
                  <TableHead>Verification</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead numeric>Duration</TableHead>
                  <TableHead>Started</TableHead>
                  <TableHead>Triggered By</TableHead>
                  <TableHead></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(runs ?? []).map((run) => (
                  // A live run is the one row here that is time-sensitive: it
                  // is the only one carrying a Stop button, and the only one
                  // whose numbers are still moving.
                  <TableRow key={run.id} active={run.status === 'running'}>
                    <TableCell className="font-medium">
                      <Link to={`/runs/${run.id}`} className="text-primary hover:underline">
                        {run.job_name ?? run.job_id}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <RunStatusBadge status={run.status} />
                    </TableCell>
                    <TableCell>
                      {run.kind === 'prune' || !run.check_status ? (
                        '—'
                      ) : (
                        <RunStatusBadge status={run.check_status} />
                      )}
                    </TableCell>
                    <TableCell>
                      <span data-testid={`kind-badge-${run.id}`} className={KIND_CLASS[run.kind]}>
                        {run.kind}
                      </span>
                    </TableCell>
                    <TableCell numeric>
                      {run.duration_seconds != null ? `${run.duration_seconds}s` : '—'}
                    </TableCell>
                    <TableCell className="tabular-nums whitespace-nowrap">
                      {new Date(run.started_at).toLocaleString()}
                    </TableCell>
                    <TableCell>
                      <TriggeredByIcon value={run.triggered_by} />
                    </TableCell>
                    <TableCell className="text-right">
                      {run.status === 'running' && (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                          onClick={() => setRunToStop(run)}
                        >
                          Stop
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <StopRunDialog
        open={runToStop !== null}
        onOpenChange={(open) => !open && setRunToStop(null)}
        onConfirm={handleConfirmStop}
      />
    </>
  )
}

interface MetricCardProps {
  icon: ComponentType<SVGProps<SVGSVGElement>>
  label: string
  /** null while the value is still loading. */
  value: string | null
}

/**
 * One number, one label, one icon — the same shape three times.
 *
 * The three tiles used to disagree about what they were: the first rendered a
 * large number over a caption, the other two a bare sentence ("3 enabled",
 * "restic 0.17.3") at body size. Three tiles side by side read as a comparable
 * set, so anything they don't share is noise.
 */
function MetricCard({ icon: Icon, label, value }: MetricCardProps) {
  return (
    <Card size="sm" className="shadow-xs">
      <CardContent className="flex items-center gap-3">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          <Icon className="size-4.5" aria-hidden="true" />
        </div>
        <div className="min-w-0">
          {value === null ? (
            <Skeleton className="mb-1 h-6 w-16" />
          ) : (
            <div className="truncate text-xl font-semibold tabular-nums">{value}</div>
          )}
          <div className="text-xs text-muted-foreground">{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}

export interface StopRunDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: () => void
}

/**
 * Confirmation for cancelling a live run.
 *
 * This was a `window.confirm()`, which renders in the browser's own chrome,
 * cannot say anything in more than one flat line, and looks like the page has
 * been taken over by something else at the moment the user is deciding whether
 * to interrupt a backup.
 */
export function StopRunDialog({ open, onOpenChange, onConfirm }: StopRunDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Cancel this running backup?</DialogTitle>
          <DialogDescription>
            Data already uploaded is kept in the repository and reused by the next run, so nothing
            transferred so far is wasted. No snapshot is written for the cancelled run, and the
            schedule is unaffected.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" size="lg" onClick={() => onOpenChange(false)}>
            Keep running
          </Button>
          <Button
            size="lg"
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            onClick={onConfirm}
          >
            Stop backup
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RecentRunsSkeleton() {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {Array.from({ length: 8 }).map((_, i) => (
            <TableHead key={i}>
              <Skeleton className="h-3 w-14" />
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {Array.from({ length: 3 }).map((_, row) => (
          <TableRow key={row}>
            {Array.from({ length: 8 }).map((_, col) => (
              <TableCell key={col}>
                <Skeleton className="h-4 w-full max-w-24" />
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
