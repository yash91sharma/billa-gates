import { Eye, EyeOff } from 'lucide-react'
import { useState } from 'react'
import type { BackupJob } from '../lib/types'
import FieldLabel, { helpId, type FieldHelp } from './FieldLabel'
import ScheduleInput, { type ScheduleValue } from './ScheduleInput'
import { TooltipProvider } from './ui/tooltip'

// Split a textarea of "one item per line" into a string array, or null when empty.
// Blank lines are dropped so a stray newline doesn't become a "" pattern.
function parseLines(text: string): string[] | null {
  const items = text
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  return items.length === 0 ? null : items
}

// Mirrors the backend whitelist (app/api/schemas/jobs.py::_validate_label).
// A subpath is a single path component under the source mount: '/' would
// nest deeper, and '.' / '..' would resolve to the mount itself or escape to
// the sources root — the backend rejects all of these with a 422.
//
// The job name is validated against the same pattern: it names the repository
// directory at /destinations/<destination>/<name>, so it is a path component
// too, not free text.
const SUBPATH_RE = /^[\p{L}\p{N}_][\p{L}\p{N}_ .-]*$/u

// Limits restic itself imposes (verified against restic 0.19.1). Offering a
// value it rejects turns every future run into a failure — and a rejected
// --keep-within fails `restic forget` while the run still reports success, so
// retention silently stops applying. Mirrors app/api/schemas/jobs.py.
const PACK_SIZE_MIN_MIB = 4
const PACK_SIZE_MAX_MIB = 128
// `--keep-within*`: one or more <integer><unit> pairs, units y/m/d/h,
// lowercase. Weeks are not a unit and a bare number is rejected outright.
const KEEP_WITHIN_RE = /^(?:\d+[ymdh])+$/
// Not a restic limit: the run timeout drives the runner's deadline, and the
// ceiling matches the global default in Settings.
const TIMEOUT_HOURS_MIN = 1
const TIMEOUT_HOURS_MAX = 168

// Split a comma-separated input into a string array, or null when empty.
function parseCsv(text: string): string[] | null {
  const items = text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  return items.length === 0 ? null : items
}

// Help text for every form field — kept in one place so labels, optional
// flags, descriptions, and examples are easy to read and update.
const HELP: Record<string, FieldHelp> = {
  name: {
    label: 'Name',
    description:
      'Names this job everywhere, and names its repository folder on the destination drive (/destinations/<destination>/<name>). Letters, numbers, spaces, dots and hyphens only. Cannot be changed later — pick a name you can remember, because recreating a job with the same name and destination is how you reconnect to an existing backup history.',
    example: 'Documents Daily',
  },
  source: {
    label: 'Source',
    description:
      'Mounted folder to back up. Sources come from /sources/<label> in docker-compose and are mounted read-only. IMPORTANT: The folder this job actually backs up must contain a .billa_gates_check file at its root — the mount root, or the subfolder below if you set one — or else the backup will fail.',
    example: 'documents',
  },
  subfolder: {
    label: 'Subfolder',
    optional: true,
    description:
      'Back up a single direct subfolder of the source mount instead of the whole mount. No slashes — one level only. The subfolder needs its own .billa_gates_check file: a sentinel at the mount root proves the mount is up but says nothing about the subfolder, which could be empty or gone.',
    example: 'photos',
  },
  destination: {
    label: 'Destination',
    description:
      'Where to store the restic repository. Permanent — the destination becomes part of the repo path on disk and cannot be changed later.',
    example: 'main',
  },
  password: {
    label: 'Password',
    description:
      'Encryption password for this job’s restic repository. Cannot be changed after a backup has written to the repository — use `restic key add/remove` to rotate.',
    example: 'a long, unique passphrase',
  },
  enabled: {
    label: 'Enabled',
    description:
      'When on, the scheduler runs this job on its schedule. Disabled jobs can still be run manually. Default: on.',
  },
  keepLast: {
    label: 'Keep Last',
    optional: true,
    description: 'Keep the N most recent snapshots regardless of age. Restic flag: --keep-last.',
    example: '5',
  },
  keepHourly: {
    label: 'Keep Hourly',
    optional: true,
    description:
      'Keep the most recent snapshot for each of the last N hours that contain a snapshot.',
    example: '24',
  },
  keepDaily: {
    label: 'Keep Daily',
    optional: true,
    description:
      'Keep the most recent snapshot for each of the last N days that contain a snapshot.',
    example: '7',
  },
  keepWeekly: {
    label: 'Keep Weekly',
    optional: true,
    description:
      'Keep the most recent snapshot for each of the last N weeks that contain a snapshot.',
    example: '4',
  },
  keepMonthly: {
    label: 'Keep Monthly',
    optional: true,
    description:
      'Keep the most recent snapshot for each of the last N months that contain a snapshot.',
    example: '12',
  },
  keepYearly: {
    label: 'Keep Yearly',
    optional: true,
    description:
      'Keep the most recent snapshot for each of the last N years that contain a snapshot.',
    example: '5',
  },
  keepWithin: {
    label: 'Keep Within',
    optional: true,
    description:
      'Keep every snapshot taken within this window. restic duration: one or more <number><unit> pairs using y, m, d, or h — weeks are not a unit, and a bare number is rejected.',
    example: '30d',
  },
  keepWithinHourly: {
    label: 'Keep Within Hourly',
    optional: true,
    description:
      'Keep one snapshot per hour for the snapshots within this window. Units: y, m, d, h.',
    example: '48h',
  },
  keepWithinDaily: {
    label: 'Keep Within Daily',
    optional: true,
    description:
      'Keep one snapshot per day for the snapshots within this window. Units: y, m, d, h.',
    example: '14d',
  },
  keepWithinWeekly: {
    label: 'Keep Within Weekly',
    optional: true,
    description:
      'Keep one snapshot per week for the snapshots within this window. Units: y, m, d, h — restic has no week unit, so express weeks in days (8 weeks = 56d).',
    example: '56d',
  },
  keepWithinMonthly: {
    label: 'Keep Within Monthly',
    optional: true,
    description:
      'Keep one snapshot per month for the snapshots within this window. Units: y, m, d, h.',
    example: '6m',
  },
  keepWithinYearly: {
    label: 'Keep Within Yearly',
    optional: true,
    description:
      'Keep one snapshot per year for the snapshots within this window. Units: y, m, d, h.',
    example: '2y',
  },
  excludePatterns: {
    label: 'Exclude patterns',
    optional: true,
    description: 'Glob patterns to skip. One per line.',
    example: 'node_modules/, *.tmp, .DS_Store',
  },
  excludeIfPresent: {
    label: 'Exclude if present',
    optional: true,
    description: 'Skip a directory when it contains a file with this name. One filename per line.',
    example: '.nobackup',
  },
  excludeCaches: {
    label: 'Exclude caches',
    optional: true,
    description:
      'Skip directories that contain a CACHEDIR.TAG file — the standard marker used by browsers, package managers, and build tools. Default: off.',
  },
  oneFileSystem: {
    label: 'One file system',
    optional: true,
    description:
      'Do not cross filesystem mount boundaries during backup. Useful when the source contains nested mounts you want to skip. Default: off.',
  },
  noScan: {
    label: 'No scan',
    optional: true,
    description:
      'Skip the pre-scan step that estimates the total size. The backup starts faster but no progress percentage is shown. Default: off.',
  },
  tags: {
    label: 'Tags',
    optional: true,
    description:
      'Labels attached to each snapshot — useful for filtering with `restic snapshots --tag`. Comma-separated.',
    example: 'daily, important',
  },
  compression: {
    label: 'Compression',
    optional: true,
    description:
      'Options: off | fastest | auto | better | max — zstd levels, weakest to strongest. `auto` compresses compressible data and is what you want unless you have a reason. `fastest` trades ratio for speed on a slow CPU; `better` and `max` spend more CPU for a smaller repo. `off` disables compression entirely. Default: auto.',
    example: 'auto',
  },
  packSize: {
    label: 'Pack size (MiB)',
    optional: true,
    description:
      'Internal pack file size in MiB. Increase for large repos to reduce the destination file count. restic accepts 4–128 MiB and fails the backup outside that range. Default: 16 MiB (leave blank).',
    example: '64',
  },
  readConcurrency: {
    label: 'Read concurrency',
    optional: true,
    description:
      'Number of source files read in parallel. Must be 1 or more. Default: restic’s automatic value (leave blank).',
    example: '2',
  },
  timeoutHours: {
    label: 'Timeout (hours)',
    optional: true,
    description:
      'Maximum hours the backup may run before it is killed and marked failed. Between 1 and 168 (one week). Default: the global value from Settings (leave blank).',
    example: '12',
  },
}

const inputCls = 'border rounded px-2 py-1 text-sm w-full'

export interface JobFormProps {
  job?: BackupJob
  onSubmit: (data: unknown) => void
  onCancel?: () => void
  conflictingJob?: { id: string; name: string }
  /** Source mount labels (from /api/mounts/sources). Populates the Source dropdown. */
  sourceMounts?: string[]
  /** Destination mount labels (from /api/mounts/destinations). Populates the Destination dropdown. */
  destinationMounts?: string[]
}

export default function JobForm({
  job,
  onSubmit,
  conflictingJob,
  sourceMounts = [],
  destinationMounts = [],
}: JobFormProps) {
  // The repository is created (and encrypted) when the job is created, so
  // name, destination and password address it and are all locked once the job
  // exists — there is no "before the first run" window any more.
  const isEdit = !!job

  // Basic fields
  const [name, setName] = useState(job?.name ?? '')
  const [sourceLabel, setSourceLabel] = useState(job?.source_label ?? '')
  const [sourceSubpath, setSourceSubpath] = useState(job?.source_subpath ?? '')
  const [destinationLabel, setDestinationLabel] = useState(job?.destination_label ?? '')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [enabled, setEnabled] = useState(job?.enabled ?? true)
  const [schedule, setSchedule] = useState<ScheduleValue>({
    type: job?.schedule_type ?? 'interval',
    value: job?.schedule_value ?? '',
  })

  // Retention fields
  const [retentionExpanded, setRetentionExpanded] = useState(false)
  const [keepLast, setKeepLast] = useState(job?.retain_keep_last?.toString() ?? '')
  const [keepHourly, setKeepHourly] = useState(job?.retain_keep_hourly?.toString() ?? '')
  const [keepDaily, setKeepDaily] = useState(job?.retain_keep_daily?.toString() ?? '')
  const [keepWeekly, setKeepWeekly] = useState(job?.retain_keep_weekly?.toString() ?? '')
  const [keepMonthly, setKeepMonthly] = useState(job?.retain_keep_monthly?.toString() ?? '')
  const [keepYearly, setKeepYearly] = useState(job?.retain_keep_yearly?.toString() ?? '')
  const [keepWithin, setKeepWithin] = useState(job?.retain_keep_within ?? '')
  const [keepWithinHourly, setKeepWithinHourly] = useState(job?.retain_keep_within_hourly ?? '')
  const [keepWithinDaily, setKeepWithinDaily] = useState(job?.retain_keep_within_daily ?? '')
  const [keepWithinWeekly, setKeepWithinWeekly] = useState(job?.retain_keep_within_weekly ?? '')
  const [keepWithinMonthly, setKeepWithinMonthly] = useState(job?.retain_keep_within_monthly ?? '')
  const [keepWithinYearly, setKeepWithinYearly] = useState(job?.retain_keep_within_yearly ?? '')

  // Backup option fields
  const [excludePatterns, setExcludePatterns] = useState((job?.exclude_patterns ?? []).join('\n'))
  const [excludeIfPresent, setExcludeIfPresent] = useState(
    (job?.exclude_if_present ?? []).join('\n')
  )
  const [excludeCaches, setExcludeCaches] = useState(job?.exclude_caches ?? false)
  const [oneFileSystem, setOneFileSystem] = useState(job?.one_file_system ?? false)
  const [noScan, setNoScan] = useState(job?.no_scan ?? false)
  const [tagsText, setTagsText] = useState((job?.tags ?? []).join(', '))
  const [compression, setCompression] = useState<string>(job?.compression ?? '')
  const [packSizeText, setPackSizeText] = useState(job?.pack_size?.toString() ?? '')
  const [readConcurrencyText, setReadConcurrencyText] = useState(
    job?.read_concurrency?.toString() ?? ''
  )
  const [timeoutHoursText, setTimeoutHoursText] = useState(job?.timeout_hours?.toString() ?? '')

  // Error state
  const [submitError, setSubmitError] = useState<string | null>(null)

  // Source change warning
  const originalSourceLabel = job?.source_label ?? null
  const sourceChanged = !!originalSourceLabel && sourceLabel !== originalSourceLabel

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitError(null)

    // Presence is enforced natively via `required`; this guards the separate
    // property that the name is a usable single path component.
    if (name && !SUBPATH_RE.test(name)) {
      setSubmitError(
        'name must not contain "/", "." or ".." — it names the repository folder on the ' +
          'destination drive (letters, digits, underscores, spaces, dots, and hyphens)'
      )
      return
    }

    if (sourceSubpath && !SUBPATH_RE.test(sourceSubpath)) {
      setSubmitError(
        'source_subpath must not contain "/", "." or ".." — use a single folder name ' +
          '(letters, digits, underscores, spaces, dots, and hyphens)'
      )
      return
    }

    // restic parses every --keep-within* value itself and rejects anything
    // else. A bad value here does not fail the backup — it fails `restic
    // forget` afterwards, so the run still says "success" while retention
    // silently stops applying. Catch it before the job is saved.
    const badDuration = (
      [
        ['Keep Within', keepWithin],
        ['Keep Within Hourly', keepWithinHourly],
        ['Keep Within Daily', keepWithinDaily],
        ['Keep Within Weekly', keepWithinWeekly],
        ['Keep Within Monthly', keepWithinMonthly],
        ['Keep Within Yearly', keepWithinYearly],
      ] as const
    ).find(([, value]) => value && !KEEP_WITHIN_RE.test(value))
    if (badDuration) {
      setSubmitError(
        `${badDuration[0]}: "${badDuration[1]}" is not a duration restic accepts. ` +
          'Use one or more <number><unit> pairs with the units y, m, d, or h — ' +
          'for example 30d, 48h, or 2y5m7d3h. There is no unit for weeks (use 56d ' +
          'for 8 weeks), and bare numbers, spaces and decimals are rejected.'
      )
      return
    }

    const packSizeValue = packSizeText ? parseInt(packSizeText) : null
    if (
      packSizeValue !== null &&
      (packSizeValue < PACK_SIZE_MIN_MIB || packSizeValue > PACK_SIZE_MAX_MIB)
    ) {
      setSubmitError(
        `Pack size must be between ${PACK_SIZE_MIN_MIB} and ${PACK_SIZE_MAX_MIB} MiB — ` +
          'restic refuses anything outside that range and the backup would fail.'
      )
      return
    }

    const timeoutValue = timeoutHoursText ? parseInt(timeoutHoursText) : null
    if (
      timeoutValue !== null &&
      (timeoutValue < TIMEOUT_HOURS_MIN || timeoutValue > TIMEOUT_HOURS_MAX)
    ) {
      setSubmitError(`Timeout must be between ${TIMEOUT_HOURS_MIN} and ${TIMEOUT_HOURS_MAX} hours.`)
      return
    }

    const readConcurrencyValue = readConcurrencyText ? parseInt(readConcurrencyText) : null
    if (readConcurrencyValue !== null && readConcurrencyValue < 1) {
      setSubmitError('Read concurrency must be 1 or more (leave blank for restic’s default).')
      return
    }

    onSubmit({
      name,
      source_label: sourceLabel,
      source_subpath: sourceSubpath || null,
      destination_label: destinationLabel,
      restic_password: password || undefined,
      enabled,
      schedule_type: schedule.type,
      schedule_value: schedule.value,
      retain_keep_last: keepLast ? parseInt(keepLast) : null,
      retain_keep_hourly: keepHourly ? parseInt(keepHourly) : null,
      retain_keep_daily: keepDaily ? parseInt(keepDaily) : null,
      retain_keep_weekly: keepWeekly ? parseInt(keepWeekly) : null,
      retain_keep_monthly: keepMonthly ? parseInt(keepMonthly) : null,
      retain_keep_yearly: keepYearly ? parseInt(keepYearly) : null,
      retain_keep_within: keepWithin || null,
      retain_keep_within_hourly: keepWithinHourly || null,
      retain_keep_within_daily: keepWithinDaily || null,
      retain_keep_within_weekly: keepWithinWeekly || null,
      retain_keep_within_monthly: keepWithinMonthly || null,
      retain_keep_within_yearly: keepWithinYearly || null,
      exclude_patterns: parseLines(excludePatterns),
      exclude_if_present: parseLines(excludeIfPresent),
      exclude_caches: excludeCaches,
      one_file_system: oneFileSystem,
      no_scan: noScan,
      tags: parseCsv(tagsText),
      compression: compression || null,
      pack_size: packSizeValue,
      read_concurrency: readConcurrencyValue,
      timeout_hours: timeoutValue,
    })
  }

  return (
    <TooltipProvider>
      <form role="form" aria-label="Backup job form" onSubmit={handleSubmit} className="space-y-6">
        {/* Conflict banner */}
        {conflictingJob && (
          <div className="bg-warning/15 border border-warning/40 rounded-sm p-3 text-sm">
            <p>There is already a job using this source and destination.</p>
            <a href={`/jobs/${conflictingJob.id}`} className="text-primary underline">
              {conflictingJob.name}
            </a>
          </div>
        )}

        {/* Source change warning */}
        {sourceChanged && (
          <div className="bg-warning/15 border border-warning/40 rounded-sm p-3 text-sm">
            Changing the source label will redirect future backups to the new source path.
          </div>
        )}

        {/* Submit error */}
        {submitError && (
          <div className="bg-destructive/10 border border-destructive/30 rounded-sm p-3 text-sm text-destructive">
            {submitError}
          </div>
        )}

        {/* Basic section */}
        <section>
          <h2 className="text-base font-semibold mb-3">Basic</h2>
          <div className="space-y-3">
            <div>
              <FieldLabel htmlFor="job-name" help={HELP.name} />
              <input
                id="job-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={isEdit}
                required
                aria-describedby={helpId('job-name')}
                className={`${inputCls} disabled:opacity-60`}
              />
              {isEdit ? (
                <p className="text-muted-foreground text-xs mt-1">
                  🔒 The name cannot be changed — it names this job's repository folder at{' '}
                  <code>
                    /destinations/{destinationLabel}/{name}
                  </code>
                  . Create a new job to use a different name.
                </p>
              ) : (
                destinationLabel &&
                name && (
                  <p className="text-muted-foreground text-xs mt-1">
                    The repository will be created at{' '}
                    <code>
                      /destinations/{destinationLabel}/{name}
                    </code>
                    .
                  </p>
                )
              )}
            </div>

            <div>
              <FieldLabel htmlFor="source-label" help={HELP.source} />
              <select
                id="source-label"
                value={sourceLabel}
                onChange={(e) => setSourceLabel(e.target.value)}
                aria-describedby={helpId('source-label')}
                className={`${inputCls} bg-background`}
              >
                <option value="">Select a source…</option>
                {/* Preserve the saved label even if it's no longer in the mounts list
                    (e.g. the volume was unmounted) so the user can see what it was. */}
                {sourceLabel && !sourceMounts.includes(sourceLabel) && (
                  <option value={sourceLabel}>{sourceLabel} (not currently mounted)</option>
                )}
                {sourceMounts.map((label) => (
                  <option key={label} value={label}>
                    {label}
                  </option>
                ))}
              </select>
              {sourceMounts.length === 0 && (
                <p className="text-muted-foreground text-xs mt-1">
                  No source mounts configured. Add a volume under{' '}
                  <code>/sources/&lt;label&gt;</code> in your docker compose.
                </p>
              )}
              {sourceLabel && (
                <p className="text-muted-foreground text-xs mt-1">
                  ⚠️ The folder this job backs up must contain a <code>.billa_gates_check</code>{' '}
                  file at its root:{' '}
                  <code>
                    {sourceSubpath
                      ? `/sources/${sourceLabel}/${sourceSubpath}`
                      : `/sources/${sourceLabel}`}
                  </code>
                </p>
              )}
            </div>

            <div>
              <FieldLabel htmlFor="source-subpath" help={HELP.subfolder} />
              <input
                id="source-subpath"
                type="text"
                value={sourceSubpath}
                onChange={(e) => setSourceSubpath(e.target.value)}
                placeholder="e.g. photos"
                aria-describedby={helpId('source-subpath')}
                className={inputCls}
              />
            </div>

            <div>
              <FieldLabel htmlFor="destination-label" help={HELP.destination} />
              <select
                id="destination-label"
                value={destinationLabel}
                onChange={(e) => setDestinationLabel(e.target.value)}
                disabled={isEdit}
                aria-describedby={helpId('destination-label')}
                className={`${inputCls} bg-background disabled:opacity-60`}
              >
                <option value="">Select a destination…</option>
                {destinationLabel && !destinationMounts.includes(destinationLabel) && (
                  <option value={destinationLabel}>
                    {destinationLabel} (not currently mounted)
                  </option>
                )}
                {destinationMounts.map((label) => (
                  <option key={label} value={label}>
                    {label}
                  </option>
                ))}
              </select>
              {!isEdit && destinationMounts.length === 0 && (
                <p className="text-muted-foreground text-xs mt-1">
                  No destination mounts configured. Add a volume under{' '}
                  <code>/destinations/&lt;label&gt;</code> in your docker compose.
                </p>
              )}
              {destinationLabel && (
                <p className="text-muted-foreground text-xs mt-1">
                  ⚠️ The root directory of destination <code>{destinationLabel}</code> must contain
                  a <code>.billa_gates_check</code> file.
                </p>
              )}
              {isEdit && (
                <>
                  <p className="text-muted-foreground text-xs mt-1">
                    This cannot be changed after creation.
                  </p>
                  <p className="text-muted-foreground text-xs">
                    <a href="/settings" className="text-primary underline">
                      Rename destination tool
                    </a>
                    {' — use this if remounted with a new label'}
                  </p>
                </>
              )}
            </div>

            <div>
              <FieldLabel htmlFor="job-password" help={HELP.password} />
              {/* The password is typed once and never echoed back by the API,
                  and it addresses the repository, so it cannot be corrected
                  later — the reveal toggle is the only way to confirm what was
                  typed. It is pointless in edit mode, where the field is empty
                  and disabled, so it is only rendered on create. */}
              <div className="relative">
                <input
                  id="job-password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={isEdit}
                  aria-describedby={helpId('job-password')}
                  className={`${inputCls}${isEdit ? '' : ' pr-9'}`}
                />
                {!isEdit && (
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    aria-pressed={showPassword}
                    aria-controls="job-password"
                    title={showPassword ? 'Hide password' : 'Show password'}
                    className="absolute inset-y-0 right-0 flex items-center px-2 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-r"
                  >
                    {showPassword ? (
                      <EyeOff className="h-4 w-4" aria-hidden="true" />
                    ) : (
                      <Eye className="h-4 w-4" aria-hidden="true" />
                    )}
                  </button>
                )}
              </div>
              {isEdit && (
                <p className="text-muted-foreground text-xs mt-1">
                  🔒 The repository is encrypted with this password, so it cannot be changed. To
                  rotate it, use <code>restic key</code>.
                </p>
              )}
            </div>

            <div className="flex items-center gap-2">
              <input
                id="job-enabled"
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                aria-describedby={helpId('job-enabled')}
              />
              <FieldLabel htmlFor="job-enabled" help={HELP.enabled} variant="inline" />
            </div>
          </div>
        </section>

        {/* Schedule section */}
        <section>
          <ScheduleInput value={schedule} onChange={setSchedule} />
        </section>

        {/* Retention Policy section (collapsible, default closed) */}
        <section>
          <button
            type="button"
            onClick={() => setRetentionExpanded(!retentionExpanded)}
            className="text-base font-semibold w-full text-left py-1"
          >
            Retention Policy
          </button>
          {retentionExpanded && (
            <div className="space-y-3 mt-3">
              <p className="text-xs text-muted-foreground">
                Controls how many backup snapshots this job keeps in the repository (via{' '}
                <code>restic forget</code>). This is separate from run history, which is capped
                globally on the Settings page under "Keep last runs".
              </p>
              <div className="bg-warning/15 border border-warning/40 rounded-sm p-3 text-xs mt-2">
                ⚠️ <strong>Important:</strong> Restic retention policies (forgetting snapshots) only
                remove snapshot metadata reference points. They <strong>do not</strong>{' '}
                automatically free physical disk space. To reclaim physical space and prevent silent
                disk accumulation, you must manually run a Prune operation from the Job Details
                page.
              </div>
              {(
                [
                  ['retain-keep-last', keepLast, setKeepLast, HELP.keepLast],
                  ['retain-keep-hourly', keepHourly, setKeepHourly, HELP.keepHourly],
                  ['retain-keep-daily', keepDaily, setKeepDaily, HELP.keepDaily],
                  ['retain-keep-weekly', keepWeekly, setKeepWeekly, HELP.keepWeekly],
                  ['retain-keep-monthly', keepMonthly, setKeepMonthly, HELP.keepMonthly],
                  ['retain-keep-yearly', keepYearly, setKeepYearly, HELP.keepYearly],
                ] as const
              ).map(([id, val, setter, help]) => (
                <div key={id}>
                  <FieldLabel htmlFor={id} help={help} />
                  <input
                    id={id}
                    type="number"
                    value={val}
                    onChange={(e) => setter(e.target.value)}
                    aria-describedby={helpId(id)}
                    className={inputCls}
                    min={1}
                    max={9999}
                  />
                </div>
              ))}
              {(
                [
                  ['retain-keep-within', keepWithin, setKeepWithin, HELP.keepWithin],
                  [
                    'retain-keep-within-hourly',
                    keepWithinHourly,
                    setKeepWithinHourly,
                    HELP.keepWithinHourly,
                  ],
                  [
                    'retain-keep-within-daily',
                    keepWithinDaily,
                    setKeepWithinDaily,
                    HELP.keepWithinDaily,
                  ],
                  [
                    'retain-keep-within-weekly',
                    keepWithinWeekly,
                    setKeepWithinWeekly,
                    HELP.keepWithinWeekly,
                  ],
                  [
                    'retain-keep-within-monthly',
                    keepWithinMonthly,
                    setKeepWithinMonthly,
                    HELP.keepWithinMonthly,
                  ],
                  [
                    'retain-keep-within-yearly',
                    keepWithinYearly,
                    setKeepWithinYearly,
                    HELP.keepWithinYearly,
                  ],
                ] as const
              ).map(([id, val, setter, help]) => (
                <div key={id}>
                  <FieldLabel htmlFor={id} help={help} />
                  <input
                    id={id}
                    type="text"
                    value={val}
                    onChange={(e) => setter(e.target.value)}
                    aria-describedby={helpId(id)}
                    className={inputCls}
                    placeholder={help.example}
                  />
                </div>
              ))}
            </div>
          )}
        </section>

        <section>
          <h2 className="text-base font-semibold mb-3">Backup Options</h2>
          <div className="space-y-3">
            <div>
              <FieldLabel htmlFor="exclude-patterns" help={HELP.excludePatterns} />
              <textarea
                id="exclude-patterns"
                value={excludePatterns}
                onChange={(e) => setExcludePatterns(e.target.value)}
                rows={3}
                placeholder={'*.tmp\nnode_modules/'}
                aria-describedby={helpId('exclude-patterns')}
                className={`${inputCls} font-mono`}
              />
            </div>

            <div>
              <FieldLabel htmlFor="exclude-if-present" help={HELP.excludeIfPresent} />
              <textarea
                id="exclude-if-present"
                value={excludeIfPresent}
                onChange={(e) => setExcludeIfPresent(e.target.value)}
                rows={2}
                placeholder={'.nobackup'}
                aria-describedby={helpId('exclude-if-present')}
                className={`${inputCls} font-mono`}
              />
            </div>

            <div className="flex items-center gap-2">
              <input
                id="exclude-caches"
                type="checkbox"
                checked={excludeCaches}
                onChange={(e) => setExcludeCaches(e.target.checked)}
                aria-describedby={helpId('exclude-caches')}
              />
              <FieldLabel htmlFor="exclude-caches" help={HELP.excludeCaches} variant="inline" />
            </div>

            <div className="flex items-center gap-2">
              <input
                id="one-file-system"
                type="checkbox"
                checked={oneFileSystem}
                onChange={(e) => setOneFileSystem(e.target.checked)}
                aria-describedby={helpId('one-file-system')}
              />
              <FieldLabel htmlFor="one-file-system" help={HELP.oneFileSystem} variant="inline" />
            </div>

            <div className="flex items-center gap-2">
              <input
                id="no-scan"
                type="checkbox"
                checked={noScan}
                onChange={(e) => setNoScan(e.target.checked)}
                aria-describedby={helpId('no-scan')}
              />
              <FieldLabel htmlFor="no-scan" help={HELP.noScan} variant="inline" />
            </div>

            <div>
              <FieldLabel htmlFor="job-tags" help={HELP.tags} />
              <input
                id="job-tags"
                type="text"
                value={tagsText}
                onChange={(e) => setTagsText(e.target.value)}
                placeholder="daily, important"
                aria-describedby={helpId('job-tags')}
                className={inputCls}
              />
            </div>

            <div>
              <FieldLabel htmlFor="job-compression" help={HELP.compression} />
              <select
                id="job-compression"
                value={compression}
                onChange={(e) => setCompression(e.target.value)}
                aria-describedby={helpId('job-compression')}
                className={`${inputCls} bg-background`}
              >
                <option value="">Default (auto)</option>
                <option value="auto">auto</option>
                <option value="off">off</option>
                <option value="fastest">fastest</option>
                <option value="better">better</option>
                <option value="max">max</option>
              </select>
            </div>

            <div>
              <FieldLabel htmlFor="job-pack-size" help={HELP.packSize} />
              <input
                id="job-pack-size"
                type="number"
                min={PACK_SIZE_MIN_MIB}
                max={PACK_SIZE_MAX_MIB}
                value={packSizeText}
                onChange={(e) => setPackSizeText(e.target.value)}
                placeholder="16"
                aria-describedby={helpId('job-pack-size')}
                className={inputCls}
              />
            </div>

            <div>
              <FieldLabel htmlFor="job-read-concurrency" help={HELP.readConcurrency} />
              <input
                id="job-read-concurrency"
                type="number"
                min={1}
                value={readConcurrencyText}
                onChange={(e) => setReadConcurrencyText(e.target.value)}
                aria-describedby={helpId('job-read-concurrency')}
                className={inputCls}
              />
            </div>

            <div>
              <FieldLabel htmlFor="job-timeout-hours" help={HELP.timeoutHours} />
              <input
                id="job-timeout-hours"
                type="number"
                min={TIMEOUT_HOURS_MIN}
                max={TIMEOUT_HOURS_MAX}
                value={timeoutHoursText}
                onChange={(e) => setTimeoutHoursText(e.target.value)}
                placeholder="24"
                aria-describedby={helpId('job-timeout-hours')}
                className={inputCls}
              />
            </div>
          </div>
        </section>

        <button
          type="submit"
          className="px-4 py-2 bg-primary text-primary-foreground hover:bg-primary/90 rounded-sm text-sm font-medium"
        >
          {isEdit ? 'Save' : 'Create'}
        </button>
      </form>
    </TooltipProvider>
  )
}
