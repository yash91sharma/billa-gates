import type { CheckStatus, RunStatus } from '../lib/types'

// Soft tonal badge: a pale fill with matching darker text, plus a dot in the
// same family. The dot is what makes the set scannable down a column — at 12px
// the difference between "passed" and "failed" is two words of similar length,
// and the eye finds a colour before it reads a word.
//
// The colours come from the theme's `*-subtle` tokens rather than raw palette
// classes (`bg-green-100 text-green-800`, as this used to be). Those had
// already drifted: `running` here was blue-100 while the Dashboard's `backup`
// kind badge was sky-100, two names for one idea with nothing tying them
// together. The `badge-*` marker classes are kept — tests and the screenshot
// gallery identify badges by them.
const BADGE_BASE =
  'inline-flex items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs font-medium whitespace-nowrap'

const STATUS_CONFIG: Record<string, { label: string; className: string }> = {
  running: {
    label: 'running',
    className: `badge-running bg-info-subtle text-info-subtle-foreground ${BADGE_BASE}`,
  },
  success: {
    label: 'success',
    className: `badge-success bg-success-subtle text-success-subtle-foreground ${BADGE_BASE}`,
  },
  warning: {
    label: 'warning',
    className: `badge-warning bg-warning-subtle text-warning-subtle-foreground ${BADGE_BASE}`,
  },
  failed: {
    label: 'failed',
    className: `badge-failed bg-danger-subtle text-danger-subtle-foreground ${BADGE_BASE}`,
  },
  skipped: {
    label: 'skipped',
    className: `badge-skipped bg-neutral-subtle text-neutral-subtle-foreground ${BADGE_BASE}`,
  },
  canceled: {
    label: 'canceled',
    className: `badge-canceled bg-neutral-subtle text-neutral-subtle-foreground ${BADGE_BASE}`,
  },
  passed: {
    label: 'passed',
    className: `badge-success bg-success-subtle text-success-subtle-foreground ${BADGE_BASE}`,
  },
  pending: {
    label: 'pending',
    className: `badge-pending bg-neutral-subtle text-neutral-subtle-foreground ${BADGE_BASE}`,
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
    <span className={`${config.className}${className ? ' ' + className : ''}`}>
      {/* Decorative: the label beside it already says the status, and a screen
          reader announcing "circle" adds nothing. */}
      <span
        aria-hidden="true"
        className={`size-1.5 shrink-0 rounded-full bg-current ${
          key === 'running' ? 'animate-pulse' : ''
        }`}
      />
      {config.label}
    </span>
  )
}
