import { cn } from '@/lib/utils'
import { formatPercent } from '@/lib/utils'

/**
 * Where the fill changes tone. Named rather than inlined so the component, its
 * tests and the screenshot gallery all agree on the boundaries.
 *
 * A tone is a hint, never the message: the percentage is printed beside the bar
 * (WCAG 1.4.1), and "this drive is nearly full" is decided by the page from
 * `free_bytes`, not from this number — root-reserved blocks mean a drive can sit
 * at 95% with zero writable space left.
 */
export const CAPACITY_TONE_THRESHOLDS = { warning: 75, danger: 90 } as const

function toneFor(percent: number): string {
  if (percent >= CAPACITY_TONE_THRESHOLDS.danger) return 'bg-destructive'
  if (percent >= CAPACITY_TONE_THRESHOLDS.warning) return 'bg-warning'
  return 'bg-success'
}

export interface CapacityBarProps {
  /** Percent used, 0–100. `null` when the destination could not be measured. */
  percent: number | null
  /** Accessible name, e.g. "main capacity" — the bar has no visible label. */
  label: string
  className?: string
}

/**
 * A used-space meter for one destination.
 *
 * Hand-written rather than scaffolded with `npx shadcn add progress`: the
 * radix-nova registry emits `data-checked:`-family variants that the installed
 * radix-ui never produces, and since the jsdom suite runs with `css: false`
 * nothing here could catch the resulting dead classes. It lives in
 * `components/` beside PageHeader and EmptyState for the same reason — `ui/` is
 * where the shadcn primitives (and that remapping rule) live.
 *
 * `role="progressbar"` rather than `role="meter"`: meter is ARIA 1.2 only and
 * testing-library's support for it is uneven. An unmeasured destination renders
 * with no `aria-valuenow` and an empty track, because a 0% fill would state
 * that the drive is empty when the truth is that nobody could read it.
 */
export default function CapacityBar({ percent, label, className }: CapacityBarProps) {
  const known = percent !== null && Number.isFinite(percent)
  const clamped = known ? Math.min(100, Math.max(0, percent as number)) : null

  return (
    <div className={cn('flex items-center justify-end gap-2', className)}>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        {...(clamped === null ? { 'aria-valuetext': 'unknown' } : { 'aria-valuenow': clamped })}
        className="h-2 w-20 shrink-0 overflow-hidden rounded-sm bg-muted"
      >
        {clamped !== null && (
          <div
            data-testid="capacity-bar-fill"
            // Inline width because the value is dynamic: `w-[${pct}%]` is
            // invisible to Tailwind v4's content scanner and compiles to
            // nothing at all.
            style={{ width: `${clamped}%` }}
            className={cn('h-full rounded-sm transition-[width]', toneFor(clamped))}
          />
        )}
      </div>
      <span className="w-14 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
        {formatPercent(percent)}
      </span>
    </div>
  )
}
