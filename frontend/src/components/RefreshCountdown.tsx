import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { Button } from './ui/button'
import { cn } from '@/lib/utils'

export interface RefreshCountdownProps {
  /**
   * When the polled query last landed, as a ms epoch — TanStack Query's
   * `dataUpdatedAt`, which bumps on every *successful* fetch (including one
   * that returned identical data). `0` means it has never succeeded.
   */
  updatedAt: number
  /** The live poll interval in ms, or `null` when auto-refresh is off. */
  intervalMs: number | null
  /** True while the polled query has a fetch in flight. */
  isFetching?: boolean
  onRefresh: () => void
  /** True while a manual refresh is in flight. */
  isRefreshing?: boolean
}

const TICK_MS = 1000

/** `M:SS`. Rounded up, so the final second reads `0:01` instead of parking on
 *  `0:00` for a whole second before the refetch fires. */
function formatCountdown(ms: number): string {
  const total = Math.ceil(ms / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

function formatAgo(ms: number): string {
  const seconds = Math.floor(ms / 1000)
  if (seconds < 10) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

/**
 * How fresh the data on the page is, and a way to ask for more.
 *
 * A backup page polls only while something is actually running, so "is this
 * number current?" has two different answers and neither was visible: an
 * operator watching a multi-hour run could not tell a two-second-old figure
 * from a ten-minute-old one, and had no way to ask short of reloading.
 *
 * The countdown is anchored to `updatedAt` rather than to a timer of its own,
 * which is what keeps it honest: a manual refresh, a poll, or a refetch
 * triggered anywhere else all bump `dataUpdatedAt`, and TanStack restarts its
 * interval from the same settled fetch — so the bar resets itself with no
 * coordination between the two.
 *
 * The 1s ticker lives *here* rather than in the page for one reason: this
 * component re-renders every second for the whole duration of a backup, and a
 * `setNow` in JobDetail would drag six queries, a run-history table and four
 * tabs along with it.
 */
export default function RefreshCountdown({
  updatedAt,
  intervalMs,
  isFetching = false,
  onRefresh,
  isRefreshing = false,
}: RefreshCountdownProps) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(timer)
  }, [])

  const live = intervalMs !== null
  // Clamped at both ends: a clock that jumps backwards must not print a
  // countdown longer than the interval it is counting down.
  const remaining = live ? Math.min(intervalMs, Math.max(0, updatedAt + intervalMs - now)) : 0
  // The refetch fires when the countdown runs out but takes a round trip to
  // land, and a bare `0:00` sitting there for that whole time reads as stuck.
  const updating = live && (isFetching || remaining <= 0)

  let detail: string | null = null
  if (live) {
    detail = updating ? 'Updating now…' : `Next update in ${formatCountdown(remaining)}`
  } else if (updatedAt > 0) {
    detail = `Updated ${formatAgo(Math.max(0, now - updatedAt))}`
  }

  return (
    <div
      data-slot="refresh-countdown"
      className={cn(
        'flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-lg px-3 py-2 text-xs',
        live
          ? 'bg-info-subtle text-info-subtle-foreground'
          : 'bg-neutral-subtle text-neutral-subtle-foreground'
      )}
    >
      <span className="flex flex-wrap items-center gap-2">
        {/* Decorative: the wording beside it already names the mode, and
            colour is never the only signal (WCAG 1.4.1). */}
        <span
          data-slot="refresh-countdown-dot"
          aria-hidden="true"
          className={cn(
            'size-1.5 shrink-0 rounded-full bg-current',
            // A backup page sits open on a screen for hours — the same reason
            // the live-run row accent gates its pulse.
            live && 'motion-safe:animate-pulse'
          )}
        />
        <span className="font-semibold">{live ? 'Auto-updating' : 'Auto-update off'}</span>
        {detail && (
          <>
            <span aria-hidden="true">·</span>
            {/* No aria-live: announcing a ticking countdown once a second is
                relentless, and the value is available on demand anyway. */}
            <span className="tabular-nums">{detail}</span>
          </>
        )}
      </span>
      <Button variant="outline" size="sm" onClick={onRefresh} disabled={isRefreshing}>
        <RefreshCw
          className={cn('size-4', isRefreshing && 'motion-safe:animate-spin')}
          aria-hidden="true"
        />
        {isRefreshing ? 'Refreshing…' : 'Refresh'}
      </Button>
    </div>
  )
}
