import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import JobForm from '../components/JobForm'
import RunStatusBadge from '../components/RunStatusBadge'
import * as api from '../lib/api'
import type { BackupJob } from '../lib/types'
import { parseApiError, type ConflictingJob } from '../lib/utils'

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
    refetch,
  } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.listJobs,
    refetchOnWindowFocus: true,
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
      <div className="p-6">
        <p className="text-destructive">Error: could not load jobs.</p>
      </div>
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
    <div className="p-6">
      <div className="flex justify-between items-center mb-4">
        <h1 className="text-2xl font-bold">Jobs</h1>
        <button
          className="bg-primary text-primary-foreground hover:bg-primary/90 px-4 py-2 rounded-sm"
          onClick={() => setShowCreateForm(true)}
        >
          Create Job
        </button>
      </div>

      {runError && <p className="text-destructive mb-2">{runError}</p>}
      {deleteError && <p className="text-destructive mb-2">{deleteError}</p>}

      {!jobToDelete && jobs?.length === 0 && (
        <p className="text-muted-foreground">No backup jobs configured yet.</p>
      )}

      {!jobToDelete && jobs && jobs.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left border-b">
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">Source → Dest</th>
              <th className="py-2 pr-4">Schedule</th>
              <th className="py-2 pr-4">Last Run</th>
              <th className="py-2 pr-4">Next Run</th>
              <th className="py-2 pr-4">Status</th>
              <th className="py-2">Actions</th>
            </tr>
          </thead>
          <tbody className="[&>tr:nth-child(even)]:bg-muted/40">
            {jobs.map((job) => (
              <tr key={job.id} className="border-b hover:bg-muted/60">
                <td className="py-2 pr-4">
                  <Link to={`/jobs/${job.id}`} className="text-primary hover:underline">
                    {job.name}
                  </Link>
                </td>
                <td className="py-2 pr-4">
                  <span>{job.source_label}</span>
                  {' → '}
                  <span>{job.destination_label}</span>
                </td>
                <td className="py-2 pr-4">{job.schedule_value}</td>
                <td className="py-2 pr-4">
                  {job.last_run ? <RunStatusBadge status={job.last_run.status} /> : '—'}
                </td>
                <td className="py-2 pr-4">
                  {job.next_run_time ? new Date(job.next_run_time).toLocaleString() : '—'}
                </td>
                <td className="py-2 pr-4">
                  <input
                    type="checkbox"
                    checked={job.enabled}
                    onChange={() => handleToggleEnabled(job)}
                    aria-label="enabled"
                    className="mr-1"
                  />
                  {job.enabled ? 'Enabled' : 'Disabled'}
                </td>
                <td className="py-2 flex gap-2">
                  <button
                    className="text-sm text-primary hover:underline"
                    onClick={() => handleRunNow(job)}
                  >
                    Run Now
                  </button>
                  <button
                    className="text-sm text-destructive hover:underline"
                    onClick={() => openDeleteDialog(job)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {jobToDelete && (
        <div
          role="dialog"
          className="fixed inset-0 bg-black/40 flex items-center justify-center z-50"
        >
          <div className="bg-white rounded p-6 max-w-md w-full">
            <p className="mb-4">
              Are you sure you want to delete &ldquo;{jobToDelete.name}&rdquo;? This cannot be
              undone.
            </p>
            <p className="text-muted-foreground text-sm mb-4">
              The backup repository is kept by default, so a new job named &ldquo;
              {jobToDelete.name}&rdquo; on <code>{jobToDelete.destination_label}</code> will
              continue its history.
            </p>
            <label className="flex items-start gap-2 mb-4 text-sm">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={deleteRepository}
                onChange={(e) => setDeleteRepository(e.target.checked)}
              />
              <span>
                Also permanently delete the repository and all its snapshots at{' '}
                <code>
                  /destinations/{jobToDelete.destination_label}/{jobToDelete.name}
                </code>
                . <span className="text-destructive">This cannot be undone.</span>
              </span>
            </label>
            <div className="flex gap-2 justify-end">
              <button
                className="px-4 py-2 bg-destructive text-destructive-foreground hover:bg-destructive/90 rounded-sm"
                onClick={handleConfirmDelete}
              >
                {deleteRepository ? 'Yes, Delete Job and Repository' : 'Yes, Delete'}
              </button>
              <button className="px-4 py-2 border rounded" onClick={() => setJobToDelete(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {showCreateForm && (
        <div className="mt-6 space-y-3">
          {createError && (
            <div className="bg-destructive/10 border border-destructive/30 rounded-sm p-3 text-sm text-destructive">
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
        </div>
      )}
    </div>
  )
}
