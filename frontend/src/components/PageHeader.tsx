import { ChevronRight } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'

export interface BreadcrumbItem {
  label: string
  /** Omitted on the last item — the current page is not a link to itself. */
  to?: string
}

export interface PageHeaderProps {
  title: string
  /** One line on what the page is for. Optional; not every page needs one. */
  description?: string
  /** Badge-ish content shown next to the title (e.g. a job's Enabled state). */
  status?: ReactNode
  /** Page-level actions, right-aligned on desktop and wrapped below on mobile. */
  actions?: ReactNode
  breadcrumb?: BreadcrumbItem[]
  className?: string
}

/**
 * The band at the top of every page: breadcrumb, title, description, actions.
 *
 * Shared rather than per-page because the pages had drifted — Jobs opened with
 * an `<h1>`, the Dashboard opened with a grid of cards and no heading at all,
 * and the detail pages opened with a bare link. A route with no title is
 * disorienting in a tab strip and invisible to a screen reader's heading list,
 * and detail pages had no way back to their parent except the browser button.
 */
export default function PageHeader({
  title,
  description,
  status,
  actions,
  breadcrumb,
  className,
}: PageHeaderProps) {
  return (
    <div data-slot="page-header" className={cn('mb-6 space-y-2', className)}>
      {breadcrumb && breadcrumb.length > 0 && (
        <nav aria-label="Breadcrumb">
          <ol className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
            {breadcrumb.map((item, i) => (
              <li key={`${item.label}-${i}`} className="flex items-center gap-1">
                {i > 0 && <ChevronRight className="size-3.5 shrink-0" aria-hidden="true" />}
                {item.to ? (
                  <Link to={item.to} className="hover:text-foreground hover:underline">
                    {item.label}
                  </Link>
                ) : (
                  <span aria-current="page">{item.label}</span>
                )}
              </li>
            ))}
          </ol>
        </nav>
      )}

      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="font-heading text-2xl font-semibold tracking-tight text-balance">
              {title}
            </h1>
            {status}
          </div>
          {description && <p className="max-w-2xl text-sm text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  )
}
