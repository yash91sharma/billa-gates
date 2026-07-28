import type { ComponentType, ReactNode, SVGProps } from 'react'
import { cn } from '@/lib/utils'

export interface EmptyStateProps {
  /** Optional lucide icon. Decorative — the title carries the meaning. */
  icon?: ComponentType<SVGProps<SVGSVGElement>>
  title: string
  /** One line on why it's empty or what fills it. Kept short on purpose. */
  description?: string
  /** Usually the button that resolves the emptiness (e.g. "Create Job"). */
  action?: ReactNode
  className?: string
}

/**
 * The "there is nothing here" state, used wherever a list or table comes back
 * empty.
 *
 * It replaces the bare sentences the pages used to render ("No runs yet.",
 * "No snapshots yet") — and, on the Dashboard's Recent Runs, the absence of
 * any state at all, which left column headers standing over nothing. An empty
 * table is ambiguous in a backup tool: it reads the same whether nothing has
 * run yet or the fetch quietly returned nothing, and the difference matters
 * when you are checking that your backups are happening.
 */
export default function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        'flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-10 text-center',
        className
      )}
    >
      {Icon && (
        <div className="mb-1 flex size-9 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <Icon className="size-4.5" aria-hidden="true" />
        </div>
      )}
      <p data-slot="empty-state-title" className="text-sm font-medium text-foreground">
        {title}
      </p>
      {description && (
        <p
          data-slot="empty-state-description"
          className="max-w-sm text-sm text-balance text-muted-foreground"
        >
          {description}
        </p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}
