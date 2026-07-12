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
    description: 'Friendly name shown in the dashboard, notifications, and logs.',
    example: 'Documents — Daily',
  },
  source: {
    label: 'Source',
    description:
      'Mounted folder to back up. Sources come from /sources/<label> in docker-compose and are mounted read-only. IMPORTANT: The root folder of the mount must contain a .billa_gates_check file, or else the backup will fail.',
    example: 'documents',
  },
  subfolder: {
    label: 'Subfolder',
    optional: true,
    description:
      'Back up a single direct subfolder of the source mount instead of the whole mount. No slashes — one level only.',
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
      'Encryption password for this job’s restic repository. Cannot be changed after the first successful backup — use `restic key add/remove` to rotate.',
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
    description: 'Keep every snapshot taken within this window. Format: Nh, Nd, Nm, or Ny.',
    example: '30d',
  },
  keepWithinHourly: {
    label: 'Keep Within Hourly',
    optional: true,
    description: 'Keep one snapshot per hour for the snapshots within this window.',
    example: '48h',
  },
  keepWithinDaily: {
    label: 'Keep Within Daily',
    optional: true,
    description: 'Keep one snapshot per day for the snapshots within this window.',
    example: '14d',
  },
  keepWithinWeekly: {
    label: 'Keep Within Weekly',
    optional: true,
    description: 'Keep one snapshot per week for the snapshots within this window.',
    example: '8w',
  },
  keepWithinMonthly: {
    label: 'Keep Within Monthly',
    optional: true,
    description: 'Keep one snapshot per month for the snapshots within this window.',
    example: '6m',
  },
  keepWithinYearly: {
    label: 'Keep Within Yearly',
    optional: true,
    description: 'Keep one snapshot per year for the snapshots within this window.',
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
      'Options: auto | max | off. `auto` compresses compressible data (recommended). `max` tries harder but is slower. `off` disables compression entirely. Default: auto.',
    example: 'auto',
  },
  packSize: {
    label: 'Pack size (MiB)',
    optional: true,
    description:
      'Internal pack file size in MiB. Increase for large repos to reduce the destination file count. Default: 128 MiB (leave blank).',
    example: '512',
  },
  readConcurrency: {
    label: 'Read concurrency',
    optional: true,
    description:
      'Number of source files read in parallel. Default: restic’s automatic value (leave blank).',
    example: '2',
  },
  timeoutHours: {
    label: 'Timeout (hours)',
    optional: true,
    description:
      'Maximum hours the backup may run before it is killed and marked failed. Default: the global value from Settings (leave blank).',
    example: '12',
  },
  checkEnabled: {
    label: 'Scheduled integrity check',
    optional: true,
    description:
      'Store an integrity-check configuration with this job (restic check). Checks are run from the Job Details page. Default: off.',
  },
  checkMode: {
    label: 'Check Mode',
    optional: true,
    description:
      'Options: structural | subset | full. `structural` verifies repository metadata only. `subset` reads a percentage of the pack data. `full` reads all data (slowest, most thorough).',
    example: 'structural',
  },
  checkSubsetPercent: {
    label: 'Subset Percent',
    optional: true,
    description: 'Percentage of pack data to read when Check Mode is `subset` (1–100).',
    example: '5',
  },
  checkTimeoutHours: {
    label: 'Check Timeout (hours)',
    optional: true,
    description:
      'Maximum hours an integrity check may run before it is killed and marked failed. Default: the global value from Settings (leave blank).',
    example: '6',
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
  const isEdit = !!job
  const passwordLocked = isEdit && !!job.has_successful_run

  // Basic fields
  const [name, setName] = useState(job?.name ?? '')
  const [sourceLabel, setSourceLabel] = useState(job?.source_label ?? '')
  const [sourceSubpath, setSourceSubpath] = useState(job?.source_subpath ?? '')
  const [destinationLabel, setDestinationLabel] = useState(job?.destination_label ?? '')
  const [password, setPassword] = useState('')
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

  // Integrity verification fields — shown so an edit round-trips the full
  // job configuration (omitting them used to silently reset check settings).
  const [checkEnabled, setCheckEnabled] = useState(job?.check_enabled ?? false)
  const [checkMode, setCheckMode] = useState<string>(job?.check_mode ?? '')
  const [checkSubsetText, setCheckSubsetText] = useState(
    job?.check_subset_percent?.toString() ?? ''
  )
  const [checkTimeoutText, setCheckTimeoutText] = useState(
    job?.check_timeout_hours?.toString() ?? ''
  )

  // Error state
  const [submitError, setSubmitError] = useState<string | null>(null)

  // Source change warning
  const originalSourceLabel = job?.source_label ?? null
  const sourceChanged = !!originalSourceLabel && sourceLabel !== originalSourceLabel

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitError(null)

    if (sourceSubpath && sourceSubpath.includes('/')) {
      setSubmitError('source_subpath must not contain "/"')
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
      pack_size: packSizeText ? parseInt(packSizeText) : null,
      read_concurrency: readConcurrencyText ? parseInt(readConcurrencyText) : null,
      timeout_hours: timeoutHoursText ? parseInt(timeoutHoursText) : null,
      check_enabled: checkEnabled,
      check_mode: checkMode || null,
      check_subset_percent:
        checkMode === 'subset' && checkSubsetText ? parseInt(checkSubsetText) : null,
      check_timeout_hours: checkTimeoutText ? parseInt(checkTimeoutText) : null,
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
                aria-describedby={helpId('job-name')}
                className={inputCls}
              />
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
                  ⚠️ The root directory of source <code>{sourceLabel}</code> must contain a{' '}
                  <code>.billa_gates_check</code> file.
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
              <input
                id="job-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={passwordLocked}
                aria-describedby={helpId('job-password')}
                className={inputCls}
              />
              {passwordLocked ? (
                <p className="text-muted-foreground text-xs mt-1">
                  🔒 Password cannot change after the first successful backup. To rotate, use{' '}
                  <code>restic key</code>.
                </p>
              ) : (
                isEdit && (
                  <p className="text-muted-foreground text-xs mt-1">
                    No backups run yet — you can still change this password.
                  </p>
                )
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
                <option value="max">max</option>
              </select>
            </div>

            <div>
              <FieldLabel htmlFor="job-pack-size" help={HELP.packSize} />
              <input
                id="job-pack-size"
                type="number"
                min={1}
                value={packSizeText}
                onChange={(e) => setPackSizeText(e.target.value)}
                placeholder="128"
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
                min={1}
                value={timeoutHoursText}
                onChange={(e) => setTimeoutHoursText(e.target.value)}
                placeholder="24"
                aria-describedby={helpId('job-timeout-hours')}
                className={inputCls}
              />
            </div>
          </div>
        </section>

        <section>
          <h2 className="text-base font-semibold mb-3">Integrity Verification</h2>
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <input
                id="check-enabled"
                type="checkbox"
                checked={checkEnabled}
                onChange={(e) => setCheckEnabled(e.target.checked)}
                aria-describedby={helpId('check-enabled')}
              />
              <FieldLabel htmlFor="check-enabled" help={HELP.checkEnabled} variant="inline" />
            </div>

            <div>
              <FieldLabel htmlFor="check-mode" help={HELP.checkMode} />
              <select
                id="check-mode"
                value={checkMode}
                onChange={(e) => setCheckMode(e.target.value)}
                aria-describedby={helpId('check-mode')}
                className={`${inputCls} bg-background`}
              >
                <option value="">Not configured</option>
                <option value="structural">structural</option>
                <option value="subset">subset</option>
                <option value="full">full</option>
              </select>
            </div>

            {checkMode === 'subset' && (
              <div>
                <FieldLabel htmlFor="check-subset-percent" help={HELP.checkSubsetPercent} />
                <input
                  id="check-subset-percent"
                  type="number"
                  min={1}
                  max={100}
                  value={checkSubsetText}
                  onChange={(e) => setCheckSubsetText(e.target.value)}
                  placeholder="5"
                  aria-describedby={helpId('check-subset-percent')}
                  className={inputCls}
                />
              </div>
            )}

            <div>
              <FieldLabel htmlFor="check-timeout-hours" help={HELP.checkTimeoutHours} />
              <input
                id="check-timeout-hours"
                type="number"
                min={1}
                value={checkTimeoutText}
                onChange={(e) => setCheckTimeoutText(e.target.value)}
                placeholder="24"
                aria-describedby={helpId('check-timeout-hours')}
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
