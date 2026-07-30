import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, HardDrive, RefreshCw } from 'lucide-react'
import CapacityBar from '../components/CapacityBar'
import EmptyState from '../components/EmptyState'
import PageHeader from '../components/PageHeader'
import { Button } from '../components/ui/button'
import { Card, CardContent } from '../components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '../components/ui/collapsible'
import { Skeleton } from '../components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '../components/ui/table'
import * as api from '../lib/api'
import type { DestinationUsage } from '../lib/types'
import { formatCapacity } from '../lib/utils'

const COLUMN_COUNT = 6
const QUERY_KEY = ['destinationUsage']

const NOTE_CLASS = 'text-xs text-muted-foreground'
const FLAG_CLASS =
  'inline-flex items-center rounded-sm bg-warning-subtle px-2 py-0.5 text-xs font-medium text-warning-subtle-foreground'
const ALARM_CLASS =
  'inline-flex items-center rounded-sm bg-danger-subtle px-2 py-0.5 text-xs font-medium text-danger-subtle-foreground'

/**
 * What a row has to say beyond its numbers.
 *
 * Each of these exists because the figures can be read wrongly, and a wrong
 * reading here is expensive: an operator sizes their next backup against it, or
 * adds two rows together that describe one drive.
 */
function rowNotes(destination: DestinationUsage): { tone: 'flag' | 'alarm'; text: string }[] {
  const notes: { tone: 'flag' | 'alarm'; text: string }[] = []

  // Reserved blocks mean a full drive reads ~95%, so this keys on free bytes.
  // Anything else lets a drive with nothing writable left look merely busy.
  if (destination.available && destination.free_bytes === 0) {
    notes.push({ tone: 'alarm', text: 'no space left' })
  }
  if (destination.is_separate_mount === false) {
    notes.push({ tone: 'flag', text: 'not a separate mount' })
  }
  if (destination.shares_filesystem_with.length > 0) {
    notes.push({
      tone: 'flag',
      text: `shares a filesystem with ${destination.shares_filesystem_with.join(', ')}`,
    })
  }
  if (destination.sentinel_present === false) {
    notes.push({ tone: 'flag', text: 'no marker file — not set up for backups' })
  }
  return notes
}

function measuredAtLabel(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString()
}

/**
 * Backup Destinations — how much room is left on each drive backups land on.
 *
 * Read-only and deliberately un-polled: the figures only move when a run
 * writes, and the backend drops its cached measurement in every pipeline's
 * `finally`, so the next visit (or Refresh) after a job finishes is fresh.
 * Polling here would spin up sleeping USB drives to learn nothing.
 *
 * The page carries its own caveats rather than leaving them implicit in a
 * badge — see the disclosure below. The one line that corrects the most costly
 * misreading (that the three figures should add up, and that Free is the one to
 * trust) is unconditional and cannot be collapsed away.
 */
export default function Destinations() {
  const queryClient = useQueryClient()

  const { data, error, isPending } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => api.getDestinationUsage(),
    refetchOnWindowFocus: true,
  })

  // A plain refetch would be served from the backend's TTL cache, so Refresh
  // asks for a re-read and writes the result into the same cache entry the
  // page renders from.
  const refresh = useMutation({
    mutationFn: () => api.getDestinationUsage(true),
    onSuccess: (fresh) => queryClient.setQueryData(QUERY_KEY, fresh),
  })

  const destinations = data?.destinations ?? []

  if (error) {
    return (
      <>
        <PageHeader title="Backup Destinations" />
        <Card className="border border-destructive/30 bg-destructive/5">
          <CardContent className="text-destructive">
            Error: could not load destination capacity.
          </CardContent>
        </Card>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Backup Destinations"
        description="How much room is left on each drive your backups are written to."
        actions={
          <Button variant="outline" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            <RefreshCw className="size-4" aria-hidden="true" />
            {refresh.isPending ? 'Refreshing…' : 'Refresh'}
          </Button>
        }
      />

      <Card>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <p className={NOTE_CLASS}>
              Used + free does not add up to total — filesystems hold some space back.{' '}
              <strong className="font-medium text-foreground">Free</strong> is what a backup can
              actually write.
              {data?.measured_at && <> Measured {measuredAtLabel(data.measured_at)}.</>} Refreshed
              after each job run.
            </p>

            <Collapsible>
              <CollapsibleTrigger className="group inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
                How to read these numbers
                <ChevronDown
                  className="size-3 transition-transform group-data-[state=open]:rotate-180"
                  aria-hidden="true"
                />
              </CollapsibleTrigger>
              <CollapsibleContent>
                <ul className={`mt-2 list-disc space-y-1 pl-5 ${NOTE_CLASS}`}>
                  <li>
                    The three figures do not sum: filesystems hold blocks back (about 5% on ext4).
                    Percent used is measured against total, so a drive can read 95% with{' '}
                    <strong className="font-medium text-foreground">zero bytes free</strong> — trust
                    Free.
                  </li>
                  <li>
                    <code>df</code> computes its Use% against used + free rather than total, so it
                    reads a few points higher than the percentage here. The byte figures do match
                    it: Free is the same number as <code>df</code>'s Avail column.
                  </li>
                  <li>
                    Figures are read from the filesystem when this page loads and again after any
                    job run finishes. Nothing is stored and no history is kept; Refresh re-reads
                    every drive.
                  </li>
                  <li>
                    Two labels can be folders on one device. Rows that share one are marked, and
                    their capacities must not be added together — which is why this page shows no
                    total.
                  </li>
                  <li>
                    A label that was never mounted, or whose drive detached, reports the filesystem
                    behind <code>/destinations</code> — this app's own container — rather than a
                    backup drive. Those rows are marked "not a separate mount".
                  </li>
                  <li>
                    A destination without its <code>.billa_gates_check</code> marker file is not set
                    up for backups, and a job pointing at it refuses to run.
                  </li>
                  <li>
                    A drive that is detached, unreadable, or too slow to answer shows dashes and a
                    reason for that row alone — the rest of the page still loads.
                  </li>
                  <li>
                    A destination whose folder is gone but which jobs still point at is listed as
                    unavailable rather than quietly dropped.
                  </li>
                  <li>
                    restic deduplicates and compresses, so a source larger than Free may still fit.
                    This page sizes the drive, not the next snapshot.
                  </li>
                </ul>
              </CollapsibleContent>
            </Collapsible>
          </div>

          {isPending ? (
            <DestinationsTableSkeleton />
          ) : destinations.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Destination</TableHead>
                  <TableHead numeric>Capacity</TableHead>
                  <TableHead numeric>Used</TableHead>
                  <TableHead numeric>Free</TableHead>
                  <TableHead numeric>Total</TableHead>
                  <TableHead numeric>Jobs</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {destinations.map((destination) => {
                  const notes = rowNotes(destination)
                  return (
                    <TableRow key={destination.label}>
                      <TableCell>
                        <div className="space-y-1">
                          <span className="font-medium text-foreground">{destination.label}</span>
                          <p className={`font-mono ${NOTE_CLASS}`}>{destination.path}</p>
                          {!destination.available && destination.unavailable_reason && (
                            <p className="text-xs text-danger-subtle-foreground">
                              {destination.unavailable_reason}
                            </p>
                          )}
                          {notes.length > 0 && (
                            <div className="flex flex-wrap gap-1 pt-0.5">
                              {notes.map((note) => (
                                <span
                                  key={note.text}
                                  className={note.tone === 'alarm' ? ALARM_CLASS : FLAG_CLASS}
                                >
                                  {note.text}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </TableCell>
                      <TableCell numeric>
                        <CapacityBar
                          percent={destination.percent_used}
                          label={`${destination.label} capacity used`}
                        />
                      </TableCell>
                      <TableCell numeric>{formatCapacity(destination.used_bytes)}</TableCell>
                      <TableCell numeric>{formatCapacity(destination.free_bytes)}</TableCell>
                      <TableCell numeric>{formatCapacity(destination.total_bytes)}</TableCell>
                      <TableCell numeric>
                        {destination.job_count === 0 ? (
                          <span className={NOTE_CLASS}>no jobs</span>
                        ) : (
                          <div className="space-y-0.5">
                            <span>{destination.job_count}</span>
                            <p className={NOTE_CLASS}>{destination.job_names.join(', ')}</p>
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          ) : (
            <EmptyState
              icon={HardDrive}
              title="No destinations mounted"
              description="Nothing is mounted under /destinations. Mount a backup drive there in docker-compose, put the marker file at its root, and it will show up here."
            />
          )}
        </CardContent>
      </Card>
    </>
  )
}

/**
 * Shown while the first read is in flight. Shaped like the real table, because
 * this page has to reach four drives — one of them possibly a sleeping USB disk
 * or a NAS — and a blank frame for that long reads as "no destinations", which
 * in a backup tool is the more alarming reading.
 */
function DestinationsTableSkeleton() {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {Array.from({ length: COLUMN_COUNT }).map((_, i) => (
            <TableHead key={i}>
              <Skeleton className="h-3 w-16" />
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {Array.from({ length: 3 }).map((_, row) => (
          <TableRow key={row}>
            {Array.from({ length: COLUMN_COUNT }).map((_, col) => (
              <TableCell key={col}>
                <Skeleton className="h-4 w-full max-w-28" />
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
