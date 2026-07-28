import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * The app's tables carry operational data — sizes, durations, file counts,
 * timestamps — so two things are baked in here rather than left to call sites:
 * the wrapper scrolls horizontally on its own (a 10-column run history must
 * not push the whole page sideways), and numeric cells opt into `tabular-nums`
 * via `<TableCell numeric>` so digits line up column-wise instead of drifting
 * with proportional glyph widths.
 */
function Table({ className, ...props }: React.ComponentProps<'table'>) {
  return (
    <div data-slot="table-container" className="w-full overflow-x-auto">
      <table
        data-slot="table"
        className={cn('w-full caption-bottom text-sm', className)}
        {...props}
      />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<'thead'>) {
  return <thead data-slot="table-header" className={cn('[&_tr]:border-b', className)} {...props} />
}

function TableBody({ className, ...props }: React.ComponentProps<'tbody'>) {
  return (
    <tbody
      data-slot="table-body"
      className={cn('[&_tr:last-child]:border-0', className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<'tfoot'>) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn('border-t bg-muted/40 font-medium [&>tr]:last:border-b-0', className)}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<'tr'>) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        'border-b border-border transition-colors hover:bg-muted/50 data-[state=selected]:bg-muted',
        className
      )}
      {...props}
    />
  )
}

function TableHead({
  className,
  numeric = false,
  ...props
}: React.ComponentProps<'th'> & { numeric?: boolean }) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        'h-9 px-3 text-left align-middle text-xs font-medium tracking-wide text-muted-foreground uppercase whitespace-nowrap first:pl-0 last:pr-0',
        numeric && 'text-right',
        className
      )}
      {...props}
    />
  )
}

function TableCell({
  className,
  numeric = false,
  ...props
}: React.ComponentProps<'td'> & { numeric?: boolean }) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        'px-3 py-2.5 align-middle first:pl-0 last:pr-0',
        // Numbers never wrap: "50 MB" broken across two lines stops being a
        // quantity and starts being two words.
        numeric && 'text-right tabular-nums whitespace-nowrap',
        className
      )}
      {...props}
    />
  )
}

function TableCaption({ className, ...props }: React.ComponentProps<'caption'>) {
  return (
    <caption
      data-slot="table-caption"
      className={cn('mt-4 text-sm text-muted-foreground', className)}
      {...props}
    />
  )
}

export { Table, TableHeader, TableBody, TableFooter, TableHead, TableRow, TableCell, TableCaption }
