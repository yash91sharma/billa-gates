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

// Always one decimal, so a column of percentages lines up under `tabular-nums`
// and 60% never renders as "60" beside "82.4". Null is a reading that could not
// be taken, which is different from a drive that is 0% full — hence the dash
// rather than "0.0%", matching formatBytes' contract.
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value.toFixed(1)}%`
}

// Drive capacities are the one place in the app that reaches terabytes, and a
// 4 TB disk rendered as "4096 GB" is not how anyone reads a disk. Everything
// below 1 TB is handed to formatBytes so there is still only one definition of
// the KB/MB/GB tiers. formatBytes itself is deliberately left alone: it renders
// per-run snapshot sizes, and changing what those show is a change to a
// different feature.
export function formatCapacity(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  const TB = 1099511627776
  if (bytes >= TB) {
    const val = bytes / TB
    return `${val % 1 === 0 ? val.toFixed(0) : val.toFixed(1)} TB`
  }
  return formatBytes(bytes)
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
