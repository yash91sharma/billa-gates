import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Ban, Info, RotateCcw } from 'lucide-react'
import type { ComponentType, ReactNode, SVGProps } from 'react'
import { useParams } from 'react-router-dom'
import PageHeader from '../components/PageHeader'
import RunStatusBadge from '../components/RunStatusBadge'
import { StopRunDialog } from './Dashboard'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card'
import { Skeleton } from '../components/ui/skeleton'
import * as api from '../lib/api'
import type { BackupRun, RunKind } from '../lib/types'
import { formatBytes } from '../lib/utils'

// Restic's own lock failures, plus the sentence the runner writes on restic
// exit code 11 ("Repository is locked and could not be unlocked: …").
//
// Deliberately narrow. This used to be a bare `.includes('locked')`, which
// matches "connection blocked", a path under a folder named `locked/`, and
// anything else containing those seven letters — and because the callout
// *replaced* the error output, those runs showed advice about a lock nobody
// took and hid the actual failure completely. The callout is now additive, so
// the worst a miss can do is drop a hint; the error itself is always rendered.
//
// No `g` flag: a global regex keeps `lastIndex` between `.test()` calls and
// would match every other render.
const LOCKED_REPO_RE = /repository is (?:already )?locked|unable to create lock/i

const KIND_TITLE: Record<RunKind, string> = {
  backup: 'Backup run',
  prune: 'Prune run',
  check: 'Integrity check run',
}

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
  const queryClient = useQueryClient()
  const [stopOpen, setStopOpen] = useState(false)

  const {
    data: run,
    error,
    isPending,
  } = useQuery({
    queryKey: ['run', id],
    queryFn: () => api.getRun(id ?? ''),
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return false
      // At most once a minute — see Dashboard.tsx for rationale.
      return shouldPoll(data) ? 60_000 : false
    },
  })

  async function handleConfirmStop() {
    setStopOpen(false)
    if (!run) return
    try {
      await api.cancelRun(run.id)
      await queryClient.invalidateQueries({ queryKey: ['run', run.id] })
    } catch {
      // Polling will pick up any successful cancellation; silent failure is
      // acceptable here (e.g. 409 if the run just finished naturally).
    }
  }

  if (error) {
    const status = (error as { status?: number }).status
    return (
      <>
        <PageHeader title="Run" breadcrumb={[{ label: 'Jobs', to: '/jobs' }]} />
        <Card className="border border-destructive/30 bg-destructive/5">
          <CardContent className="text-destructive">
            {status === 404 ? 'Run not found (404).' : 'Error: could not load run.'}
          </CardContent>
        </Card>
      </>
    )
  }

  if (isPending || !run) {
    return <RunDetailSkeleton />
  }

  return (
    <>
      <PageHeader
        breadcrumb={[
          { label: 'Jobs', to: '/jobs' },
          { label: run.job_name ?? run.job_id, to: `/jobs/${run.job_id}` },
        ]}
        title={KIND_TITLE[run.kind]}
        status={<RunStatusBadge status={run.status} />}
        actions={
          run.status === 'running' && (
            <Button
              size="lg"
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => setStopOpen(true)}
            >
              Stop
            </Button>
          )
        }
      />

      <div className="space-y-4">
        {run.reason === 'user_canceled' && (
          <Callout icon={Ban}>
            This run was canceled by the user. Already-uploaded data is preserved in the repo — the
            next run will reuse it via deduplication.
          </Callout>
        )}

        {run.reason === 'container_restart' && (
          <Callout icon={RotateCcw}>
            This run was interrupted and marked failed due to a container restart.
          </Callout>
        )}

        {run.reason === 'overlapping_run' && (
          <Callout icon={Info}>
            This run was skipped — a previous run was already running (overlapping).
          </Callout>
        )}

        {run.error_output && LOCKED_REPO_RE.test(run.error_output) && (
          <Callout icon={AlertTriangle} tone="warning">
            The repository is locked. Use the unlock button to remove the lock.
          </Callout>
        )}

        <Card>
          <CardHeader>
            <CardTitle as="h2">Summary</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Started" value={new Date(run.started_at).toLocaleString()} />
              <Field label="Triggered by" value={run.triggered_by} />
              <Field
                label="Duration"
                value={
                  run.duration_seconds != null
                    ? formatDuration(run.duration_seconds)
                    : 'in progress'
                }
              />
              <Field
                label="Snapshot"
                value={run.snapshot_id ? run.snapshot_id.substring(0, 8) : '—'}
                mono
              />
            </dl>
          </CardContent>
        </Card>

        {run.files_new != null && (
          <Card>
            <CardHeader>
              <CardTitle as="h2">Contents</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-3">
                <Field label="New files" value={String(run.files_new)} />
                <Field label="Changed" value={String(run.files_changed)} />
                {run.data_added_bytes != null && (
                  <Field label="Added" value={formatBytes(run.data_added_bytes)} />
                )}
              </dl>
            </CardContent>
          </Card>
        )}

        {run.backup_output && (
          <Card>
            <CardHeader>
              <CardTitle as="h2">Output</CardTitle>
            </CardHeader>
            <CardContent>
              <LogBlock>{run.backup_output}</LogBlock>
            </CardContent>
          </Card>
        )}

        {/* Always rendered. The callout above is a hint, not a replacement: it
            says what to do, this says what actually happened, and for a
            mount-check or init-check failure there is no other place the text
            appears (backup_output is empty on those runs). */}
        {run.error_output && (
          <Card className="border border-destructive/30">
            <CardHeader>
              <CardTitle as="h2" className="text-destructive">
                Error
              </CardTitle>
            </CardHeader>
            <CardContent>
              <LogBlock tone="destructive">{run.error_output}</LogBlock>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle as="h2">Steps</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-sm">
                <span className="w-28 shrink-0 text-muted-foreground">Retention</span>
                <RunStatusBadge status={run.prune_status} />
              </div>
              {/* Rendered for any status, not just `failed`: a partial backup
                  withholds `restic forget` and lands as prune_status=skipped —
                  the same value a job with no retention policy gets — so the
                  runner's explanation is the only thing separating "held back,
                  repo is growing" from "nothing configured". Styled as a note
                  unless the step actually failed; a red block for a deliberate
                  decision reads as a fault. */}
              {run.prune_error_output && (
                <LogBlock tone={run.prune_status === 'failed' ? 'destructive' : 'muted'} wrap>
                  {run.prune_error_output}
                </LogBlock>
              )}
            </div>

            {run.kind !== 'prune' && (
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm">
                  <span className="w-28 shrink-0 text-muted-foreground">Verification</span>
                  <RunStatusBadge status={run.check_status} />
                </div>
                {run.check_status === 'failed' && run.check_error_output && (
                  <LogBlock tone="destructive" wrap>
                    {run.check_error_output}
                  </LogBlock>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <StopRunDialog open={stopOpen} onOpenChange={setStopOpen} onConfirm={handleConfirmStop} />
    </>
  )
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={`truncate text-sm tabular-nums ${mono ? 'font-mono' : ''}`} title={value}>
        {value}
      </dd>
    </div>
  )
}

function Callout({
  icon: Icon,
  tone = 'info',
  children,
}: {
  icon: ComponentType<SVGProps<SVGSVGElement>>
  tone?: 'info' | 'warning'
  children: ReactNode
}) {
  const toneClass =
    tone === 'warning' ? 'border-warning/40 bg-warning/10' : 'border-border bg-muted/60'
  return (
    <div className={`flex items-start gap-2.5 rounded-lg border p-3 text-sm ${toneClass}`}>
      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span>{children}</span>
    </div>
  )
}

function LogBlock({
  tone = 'muted',
  wrap = false,
  children,
}: {
  tone?: 'muted' | 'destructive'
  wrap?: boolean
  children: ReactNode
}) {
  const toneClass =
    tone === 'destructive' ? 'bg-destructive/10 text-destructive' : 'bg-muted text-foreground'
  return (
    <pre
      className={`max-h-64 overflow-auto rounded-md p-3 font-mono text-xs ${toneClass} ${
        wrap ? 'whitespace-pre-wrap' : ''
      }`}
    >
      {children}
    </pre>
  )
}

/**
 * The page used to `return null` until the fetch landed, so opening a run from
 * the dashboard showed a blank white screen — worst on exactly the runs worth
 * opening, since a slow response usually means the app is busy.
 */
function RunDetailSkeleton() {
  return (
    <>
      <PageHeader title="Run" breadcrumb={[{ label: 'Jobs', to: '/jobs' }]} />
      <div className="space-y-4">
        <Card>
          <CardContent className="space-y-4">
            <Skeleton className="h-4 w-32" />
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="space-y-1.5">
                  <Skeleton className="h-3 w-16" />
                  <Skeleton className="h-4 w-24" />
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <Skeleton className="h-24 w-full" />
          </CardContent>
        </Card>
      </div>
    </>
  )
}
