import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { HardDriveDownload, Play, Plus, Trash2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import EmptyState from '../components/EmptyState'
import JobForm from '../components/JobForm'
import PageHeader from '../components/PageHeader'
import RunStatusBadge from '../components/RunStatusBadge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card'
import { Checkbox } from '../components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog'
import { Skeleton } from '../components/ui/skeleton'
import { Switch } from '../components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '../components/ui/table'
import * as api from '../lib/api'
import type { BackupJob } from '../lib/types'
import { parseApiError, type ConflictingJob } from '../lib/utils'

const COLUMN_COUNT = 7

/**
 * A job whose most recent run is still going gets its row highlighted, so the
 * list has to keep up with the run ending — a tab left open would otherwise
 * keep shouting "running" at a job that finished hours ago, and a highlight
 * that lies is worse than no highlight. Same 60s cadence as the Dashboard and
 * JobDetail: anything faster hammers the backend for the whole duration of a
 * multi-hour backup without saying anything new.
 */
function shouldPoll(jobs: BackupJob[]): boolean {
  return jobs.some((job) => job.last_run?.status === 'running')
}

export default function Jobs() {
  const navigate = useNavigate()
  const [jobToDelete, setJobToDelete] = useState<BackupJob | null>(null)
  const [deleteRepository, setDeleteRepository] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [createConflict, setCreateConflict] = useState<ConflictingJob | null>(null)

  const {
    data: jobs,
    error: jobsError,
    isPending,
    refetch,
  } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.listJobs,
    refetchOnWindowFocus: true,
    refetchInterval: (query) => (shouldPoll(query.state.data ?? []) ? 60_000 : false),
  })

  // Mounts populate the Source/Destination dropdowns in the create form.
  // Fetched here (rather than inside JobForm) so the form stays a pure
  // presentation component and stays easy to test in isolation.
  const { data: sourceMounts } = useQuery({
    queryKey: ['mounts', 'sources'],
    queryFn: api.listSourceMounts,
  })
  const { data: destinationMounts } = useQuery({
    queryKey: ['mounts', 'destinations'],
    queryFn: api.listDestinationMounts,
  })

  if (jobsError) {
    return (
      <>
        <PageHeader title="Jobs" />
        <Card className="border border-destructive/30 bg-destructive/5">
          <CardContent className="text-destructive">Error: could not load jobs.</CardContent>
        </Card>
      </>
    )
  }

  async function handleRunNow(job: BackupJob) {
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

  async function handleToggleEnabled(job: BackupJob) {
    try {
      if (job.enabled) {
        await api.disableJob(job.id)
      } else {
        await api.enableJob(job.id)
      }
      refetch()
    } catch {
      // ignore toggle errors
    }
  }

  // Always reset the destructive checkbox when opening the dialog — a value
  // carried over from a previous job would delete a repository unasked.
  function openDeleteDialog(job: BackupJob) {
    setDeleteRepository(false)
    setJobToDelete(job)
  }

  async function handleConfirmDelete() {
    if (!jobToDelete) return
    setDeleteError(null)
    try {
      await api.deleteJob(jobToDelete.id, deleteRepository)
      setJobToDelete(null)
      setDeleteRepository(false)
      refetch()
    } catch (err: unknown) {
      const status = (err as { status?: number }).status
      const detail = (err as { data?: { detail?: string } }).data?.detail
      setDeleteError(
        status === 409
          ? 'Cannot delete: a run is in progress for this job.'
          : status === 422 && detail
            ? `Error: ${detail}`
            : 'Error: failed to delete job.'
      )
      setJobToDelete(null)
      setDeleteRepository(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Every configured backup, its schedule, and how its last run went."
        actions={
          !showCreateForm && (
            <Button size="lg" onClick={() => setShowCreateForm(true)}>
              <Plus aria-hidden="true" />
              Create Job
            </Button>
          )
        }
      />

      {(runError || deleteError) && (
        <div className="mb-4 space-y-2">
          {runError && (
            <p className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {runError}
            </p>
          )}
          {deleteError && (
            <p className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {deleteError}
            </p>
          )}
        </div>
      )}

      {showCreateForm ? (
        <Card>
          <CardHeader>
            <CardTitle>New backup job</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {createError && (
              <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                {createError}
              </div>
            )}
            <JobForm
              sourceMounts={sourceMounts ?? []}
              destinationMounts={destinationMounts ?? []}
              conflictingJob={createConflict ?? undefined}
              onSubmit={async (data) => {
                setCreateError(null)
                setCreateConflict(null)
                try {
                  await api.createJob(data)
                  setShowCreateForm(false)
                  refetch()
                } catch (err: unknown) {
                  // Surface backend detail strings; the duplicate-job 409 nests
                  // an object, which parseApiError flattens (rendering it raw
                  // would crash React).
                  const { message, conflictingJob } = parseApiError(err)
                  setCreateError(message || 'Failed to create job.')
                  setCreateConflict(conflictingJob)
                }
              }}
              onCancel={() => {
                setCreateError(null)
                setCreateConflict(null)
                setShowCreateForm(false)
              }}
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent>
            {isPending ? (
              <JobsTableSkeleton />
            ) : jobs && jobs.length > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Source → Dest</TableHead>
                    <TableHead>Schedule</TableHead>
                    <TableHead>Last Run</TableHead>
                    <TableHead>Next Run</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {jobs.map((job) => (
                    // `last_run` is the most recent run, so `running` here means
                    // this job is backing up right now.
                    <TableRow key={job.id} active={job.last_run?.status === 'running'}>
                      <TableCell className="font-medium">
                        <Link to={`/jobs/${job.id}`} className="text-primary hover:underline">
                          {job.name}
                        </Link>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        <span className="text-foreground">{job.source_label}</span>
                        {' → '}
                        <span className="text-foreground">{job.destination_label}</span>
                      </TableCell>
                      <TableCell className="tabular-nums">{job.schedule_value}</TableCell>
                      <TableCell>
                        {job.last_run ? <RunStatusBadge status={job.last_run.status} /> : '—'}
                      </TableCell>
                      <TableCell className="tabular-nums whitespace-nowrap">
                        {job.next_run_time ? new Date(job.next_run_time).toLocaleString() : '—'}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Switch
                            checked={job.enabled}
                            onCheckedChange={() => handleToggleEnabled(job)}
                            aria-label="enabled"
                          />
                          <span className="text-muted-foreground">
                            {job.enabled ? 'Enabled' : 'Disabled'}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center justify-end gap-1">
                          <Button variant="ghost" size="sm" onClick={() => handleRunNow(job)}>
                            <Play aria-hidden="true" />
                            Run Now
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                            onClick={() => openDeleteDialog(job)}
                          >
                            <Trash2 aria-hidden="true" />
                            Delete
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <EmptyState
                icon={HardDriveDownload}
                title="No backup jobs configured yet"
                description="A job pairs a source mount with a destination repository and a schedule. Create one to start backing up."
                action={
                  <Button onClick={() => setShowCreateForm(true)}>
                    <Plus aria-hidden="true" />
                    Create Job
                  </Button>
                }
              />
            )}
          </CardContent>
        </Card>
      )}

      <Dialog
        open={jobToDelete !== null}
        onOpenChange={(open) => {
          if (!open) {
            setJobToDelete(null)
            setDeleteRepository(false)
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{jobToDelete?.name}”?</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this job? This cannot be undone.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 text-sm">
            <p className="text-muted-foreground">
              The backup repository is kept by default, so a new job named “{jobToDelete?.name}” on{' '}
              <code className="font-mono">{jobToDelete?.destination_label}</code> will continue its
              history.
            </p>
            <label className="flex items-start gap-2.5">
              <Checkbox
                className="mt-0.5"
                checked={deleteRepository}
                onCheckedChange={(checked) => setDeleteRepository(checked === true)}
              />
              <span>
                Also permanently delete the repository and all its snapshots at{' '}
                <code className="font-mono">
                  /destinations/{jobToDelete?.destination_label}/{jobToDelete?.name}
                </code>
                . <span className="text-destructive">This cannot be undone.</span>
              </span>
            </label>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              size="lg"
              onClick={() => {
                setJobToDelete(null)
                setDeleteRepository(false)
              }}
            >
              Cancel
            </Button>
            <Button
              size="lg"
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={handleConfirmDelete}
            >
              {deleteRepository ? 'Yes, Delete Job and Repository' : 'Yes, Delete'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

/**
 * Shown while the job list is in flight. The page used to render nothing at
 * all until the fetch landed, so opening it flashed an empty white frame —
 * indistinguishable from having no jobs configured.
 */
function JobsTableSkeleton() {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {Array.from({ length: COLUMN_COUNT }).map((_, i) => (
            <TableHead key={i}>
              <Skeleton className="h-3 w-16" />
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {Array.from({ length: 3 }).map((_, row) => (
          <TableRow key={row}>
            {Array.from({ length: COLUMN_COUNT }).map((_, col) => (
              <TableCell key={col}>
                <Skeleton className="h-4 w-full max-w-28" />
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
