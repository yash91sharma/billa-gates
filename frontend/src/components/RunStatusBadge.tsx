import type { CheckStatus, RunStatus } from '../lib/types'

// Soft pastel badge palette — pale tonal background + matching darker text.
// Aligned with the Arctic theme; running uses the accent blue family.
const BADGE_BASE = 'rounded-sm px-2 py-0.5 text-xs font-medium'

const STATUS_CONFIG: Record<string, { label: string; className: string }> = {
  running: {
    label: 'running',
    className: `badge-running bg-blue-100 text-blue-800 ${BADGE_BASE}`,
  },
  success: {
    label: 'success',
    className: `badge-success bg-green-100 text-green-800 ${BADGE_BASE}`,
  },
  warning: {
    label: 'warning',
    className: `badge-warning bg-amber-100 text-amber-800 ${BADGE_BASE}`,
  },
  failed: {
    label: 'failed',
    className: `badge-failed bg-red-100 text-red-800 ${BADGE_BASE}`,
  },
  skipped: {
    label: 'skipped',
    className: `badge-skipped bg-slate-100 text-slate-600 ${BADGE_BASE}`,
  },
  passed: {
    label: 'passed',
    className: `badge-success bg-green-100 text-green-800 ${BADGE_BASE}`,
  },
  pending: {
    label: 'pending',
    className: `badge-pending bg-slate-100 text-slate-500 ${BADGE_BASE}`,
  },
}

export interface RunStatusBadgeProps {
  status: RunStatus | CheckStatus | null
  className?: string
}

export default function RunStatusBadge({ status, className = '' }: RunStatusBadgeProps) {
  const key = status ?? 'pending'
  const config = STATUS_CONFIG[key] ?? STATUS_CONFIG.pending
  return (
    <span className={`${config.className}${className ? ' ' + className : ''}`}>{config.label}</span>
  )
}
