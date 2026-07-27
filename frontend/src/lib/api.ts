import type {
  AppSettings,
  BackupJob,
  BackupRun,
  HealthStatus,
  JobCommand,
  RenameDestinationResult,
  ResticUpdateCheck,
  Snapshot,
} from './types'

const BASE = '/api'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw Object.assign(new Error(err.detail ?? resp.statusText), {
      status: resp.status,
      data: err,
    })
  }
  return resp.json()
}

// ── Jobs ──────────────────────────────────────────────────────────────────────
export const listJobs = () => request<BackupJob[]>('/jobs')
export const getJob = (id: string) => request<BackupJob>(`/jobs/${id}`)
export const createJob = (data: unknown) =>
  request<BackupJob>('/jobs', { method: 'POST', body: JSON.stringify(data) })
export const updateJob = (id: string, data: unknown) =>
  request<BackupJob>(`/jobs/${id}`, { method: 'PUT', body: JSON.stringify(data) })
// DELETE returns 204 with no body, so it can't go through request<T>()'s
// json parse — but non-ok responses (409 = run in progress) must still reject
// with the same { status, data } shape so callers can surface them.
export const deleteJob = async (id: string, deleteRepository = false): Promise<void> => {
  const query = deleteRepository ? '?delete_repository=true' : ''
  const resp = await fetch(`${BASE}/jobs/${id}${query}`, { method: 'DELETE' })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw Object.assign(new Error(err.detail ?? resp.statusText), {
      status: resp.status,
      data: err,
    })
  }
}
export const triggerRun = (id: string) =>
  request<{ run_id: string }>(`/jobs/${id}/run`, { method: 'POST' })
export const triggerPrune = (id: string) =>
  request<{ run_id: string }>(`/jobs/${id}/prune`, { method: 'POST' })
export const triggerCheck = (
  id: string,
  data: {
    check_mode: string
    check_subset_percent?: number | null
    timeout_hours?: number | null
  }
) =>
  request<{ run_id: string }>(`/jobs/${id}/check`, {
    method: 'POST',
    body: JSON.stringify(data),
  })
export const enableJob = (id: string) =>
  request<{ id: string; enabled: boolean }>(`/jobs/${id}/enable`, { method: 'POST' })
export const disableJob = (id: string) =>
  request<{ id: string; enabled: boolean }>(`/jobs/${id}/disable`, { method: 'POST' })
export const unlockJob = (id: string) =>
  request<{ output: string }>(`/jobs/${id}/unlock`, { method: 'POST' })
export const getJobRuns = (id: string) => request<BackupRun[]>(`/jobs/${id}/runs`)
export const getJobSnapshots = (id: string) => request<Snapshot[]>(`/jobs/${id}/snapshots`)
export const getJobCommands = (id: string) => request<JobCommand[]>(`/jobs/${id}/commands`)

// ── Runs ──────────────────────────────────────────────────────────────────────
export const getRecentRuns = (limit = 10) => request<BackupRun[]>(`/runs/recent?limit=${limit}`)
export const getRun = (id: string) => request<BackupRun>(`/runs/${id}`)
export const cancelRun = async (id: string): Promise<void> => {
  const resp = await fetch(`${BASE}/runs/${id}/cancel`, { method: 'POST' })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw Object.assign(new Error(err.detail ?? resp.statusText), {
      status: resp.status,
      data: err,
    })
  }
}

// ── Mounts ────────────────────────────────────────────────────────────────────
export const listSourceMounts = () => request<string[]>('/mounts/sources')
export const listDestinationMounts = () => request<string[]>('/mounts/destinations')
export const renameDestination = (old_label: string, new_label: string) =>
  request<RenameDestinationResult>('/mounts/destinations/rename', {
    method: 'POST',
    body: JSON.stringify({ old_label, new_label }),
  })

// ── Settings ──────────────────────────────────────────────────────────────────
export const getSettings = () => request<AppSettings>('/settings')
export const updateSettings = (data: unknown) =>
  request<AppSettings>('/settings', { method: 'PUT', body: JSON.stringify(data) })
export const testNtfy = () =>
  request<{ ok: boolean; error?: string }>('/settings/test-ntfy', { method: 'POST' })
export const checkResticUpdate = () => request<ResticUpdateCheck>('/settings/restic-update-check')
export const getHealth = () => request<HealthStatus>('/health')
