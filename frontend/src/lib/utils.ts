import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export interface ConflictingJob {
  id: string
  name: string
}

export interface ApiErrorDetail {
  message: string | null
  conflictingJob: ConflictingJob | null
}

// FastAPI error payloads usually carry a string detail, but the duplicate-job
// 409 nests an object: { message, conflicting_job_id, conflicting_job_name }.
// Rendering that object directly as a React child crashes the whole app, so
// error handlers funnel through here to get a plain string plus the optional
// conflicting-job reference for JobForm's conflict banner.
export function parseApiError(err: unknown): ApiErrorDetail {
  const detail = (err as { data?: { detail?: unknown } } | null)?.data?.detail
  if (typeof detail === 'string') {
    return { message: detail, conflictingJob: null }
  }
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>
    return {
      message: typeof d.message === 'string' ? d.message : null,
      conflictingJob:
        typeof d.conflicting_job_id === 'string' && typeof d.conflicting_job_name === 'string'
          ? { id: d.conflicting_job_id, name: d.conflicting_job_name }
          : null,
    }
  }
  return { message: null, conflictingJob: null }
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  const GB = 1073741824
  const MB = 1048576
  const KB = 1024
  if (bytes >= GB) {
    const val = bytes / GB
    return `${val % 1 === 0 ? val.toFixed(0) : val.toFixed(1)} GB`
  }
  if (bytes >= MB) {
    const val = bytes / MB
    return `${val % 1 === 0 ? val.toFixed(0) : val.toFixed(1)} MB`
  }
  if (bytes >= KB) {
    const val = bytes / KB
    return `${val % 1 === 0 ? val.toFixed(0) : val.toFixed(1)} KB`
  }
  return `${bytes} B`
}
