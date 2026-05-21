import { useState } from 'react'
import type { BackupJob } from '../lib/types'
import ScheduleInput, { type ScheduleValue } from './ScheduleInput'

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

  // Backup option fields. Lists are stored as text in local state and serialised
  // back to arrays on submit so the input controls stay simple.
  const [excludePatterns, setExcludePatterns] = useState((job?.exclude_patterns ?? []).join('\n'))
  const [excludeCaches, setExcludeCaches] = useState(job?.exclude_caches ?? false)
  const [tagsText, setTagsText] = useState((job?.tags ?? []).join(', '))
  const [compression, setCompression] = useState<string>(job?.compression ?? '')
  const [timeoutHoursText, setTimeoutHoursText] = useState(job?.timeout_hours?.toString() ?? '')

  // Verification fields
  const [checkEnabled, setCheckEnabled] = useState(job?.check_enabled ?? false)
  const [checkMode, setCheckMode] = useState(job?.check_mode ?? '')
  const [checkSubsetPercent, setCheckSubsetPercent] = useState(
    job?.check_subset_percent?.toString() ?? ''
  )
  const [checkTimeoutHours, setCheckTimeoutHours] = useState(
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
    if (checkEnabled && !checkMode) {
      setSubmitError(
        'verification mode required: check_mode is required when verification is enabled'
      )
      return
    }
    if (checkEnabled && checkMode === 'subset' && !checkSubsetPercent) {
      setSubmitError('subset_percent is required when check_mode is subset')
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
      exclude_caches: excludeCaches,
      tags: parseCsv(tagsText),
      compression: compression || null,
      timeout_hours: timeoutHoursText ? parseInt(timeoutHoursText) : null,
      check_enabled: checkEnabled,
      check_mode: checkMode || null,
      check_subset_percent: checkSubsetPercent ? parseInt(checkSubsetPercent) : null,
      check_timeout_hours: checkTimeoutHours ? parseInt(checkTimeoutHours) : null,
    })
  }

  return (
    <form role="form" aria-label="Backup job form" onSubmit={handleSubmit} className="space-y-6">
      {/* Conflict banner */}
      {conflictingJob && (
        <div className="bg-yellow-50 border border-yellow-200 rounded p-3 text-sm">
          <p>There is already a job using this source and destination.</p>
          <a href={`/jobs/${conflictingJob.id}`} className="text-blue-600 underline">
            {conflictingJob.name}
          </a>
        </div>
      )}

      {/* Source change warning */}
      {sourceChanged && (
        <div className="bg-amber-50 border border-amber-200 rounded p-3 text-sm">
          Changing the source label will redirect future backups to the new source path.
        </div>
      )}

      {/* Submit error */}
      {submitError && (
        <div className="bg-red-50 border border-red-200 rounded p-3 text-sm text-red-700">
          {submitError}
        </div>
      )}

      {/* Basic section */}
      <section>
        <h2 className="text-base font-semibold mb-3">Basic</h2>
        <div className="space-y-3">
          <div>
            <label htmlFor="job-name" className="block text-sm font-medium mb-1">
              Name
            </label>
            <input
              id="job-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="border rounded px-2 py-1 text-sm w-full"
            />
          </div>

          <div>
            <label htmlFor="source-label" className="block text-sm font-medium mb-1">
              Source
            </label>
            <select
              id="source-label"
              value={sourceLabel}
              onChange={(e) => setSourceLabel(e.target.value)}
              className="border rounded px-2 py-1 text-sm w-full bg-background"
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
              <p className="text-gray-500 text-xs mt-1">
                No source mounts configured. Add a volume under <code>/sources/&lt;label&gt;</code>{' '}
                in your docker compose.
              </p>
            )}
          </div>

          <div>
            <label htmlFor="source-subpath" className="block text-sm font-medium mb-1">
              Subfolder <span className="text-gray-500 text-xs">(optional)</span>
            </label>
            <input
              id="source-subpath"
              type="text"
              value={sourceSubpath}
              onChange={(e) => setSourceSubpath(e.target.value)}
              placeholder="e.g. photos"
              className="border rounded px-2 py-1 text-sm w-full"
            />
            <p className="text-gray-500 text-xs mt-1">
              Back up a single folder inside the source mount. No slashes — just the folder name.
            </p>
          </div>

          <div>
            <label htmlFor="destination-label" className="block text-sm font-medium mb-1">
              Destination
            </label>
            <select
              id="destination-label"
              value={destinationLabel}
              onChange={(e) => setDestinationLabel(e.target.value)}
              disabled={isEdit}
              className="border rounded px-2 py-1 text-sm w-full bg-background disabled:opacity-60"
            >
              <option value="">Select a destination…</option>
              {/* Same fallback as Source: preserve the saved label if it disappeared from mounts. */}
              {destinationLabel && !destinationMounts.includes(destinationLabel) && (
                <option value={destinationLabel}>{destinationLabel} (not currently mounted)</option>
              )}
              {destinationMounts.map((label) => (
                <option key={label} value={label}>
                  {label}
                </option>
              ))}
            </select>
            {!isEdit && destinationMounts.length === 0 && (
              <p className="text-gray-500 text-xs mt-1">
                No destination mounts configured. Add a volume under{' '}
                <code>/destinations/&lt;label&gt;</code> in your docker compose.
              </p>
            )}
            {isEdit && (
              <>
                <p className="text-gray-500 text-xs mt-1">This cannot be changed after creation.</p>
                <p className="text-gray-500 text-xs">
                  <a href="/settings" className="text-blue-600 underline">
                    Rename destination tool
                  </a>
                  {' — use this if remounted with a new label'}
                </p>
              </>
            )}
          </div>

          <div>
            <label htmlFor="job-password" className="block text-sm font-medium mb-1">
              Password
            </label>
            <input
              id="job-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={passwordLocked}
              className="border rounded px-2 py-1 text-sm w-full"
            />
            {passwordLocked ? (
              <p className="text-gray-500 text-xs mt-1">
                🔒 Password cannot change after the first successful backup. To rotate, use{' '}
                <code>restic key</code>.
              </p>
            ) : (
              isEdit && (
                <p className="text-gray-500 text-xs mt-1">
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
            />
            <label htmlFor="job-enabled" className="text-sm font-medium">
              Enabled
            </label>
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
            <div>
              <label htmlFor="retain-keep-last" className="block text-sm font-medium mb-1">
                Keep Last
              </label>
              <input
                id="retain-keep-last"
                type="number"
                value={keepLast}
                onChange={(e) => setKeepLast(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                min={1}
              />
            </div>
            <div>
              <label htmlFor="retain-keep-hourly" className="block text-sm font-medium mb-1">
                Keep Hourly
              </label>
              <input
                id="retain-keep-hourly"
                type="number"
                value={keepHourly}
                onChange={(e) => setKeepHourly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                min={1}
              />
            </div>
            <div>
              <label htmlFor="retain-keep-daily" className="block text-sm font-medium mb-1">
                Keep Daily
              </label>
              <input
                id="retain-keep-daily"
                type="number"
                value={keepDaily}
                onChange={(e) => setKeepDaily(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                min={1}
              />
            </div>
            <div>
              <label htmlFor="retain-keep-weekly" className="block text-sm font-medium mb-1">
                Keep Weekly
              </label>
              <input
                id="retain-keep-weekly"
                type="number"
                value={keepWeekly}
                onChange={(e) => setKeepWeekly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                min={1}
              />
            </div>
            <div>
              <label htmlFor="retain-keep-monthly" className="block text-sm font-medium mb-1">
                Keep Monthly
              </label>
              <input
                id="retain-keep-monthly"
                type="number"
                value={keepMonthly}
                onChange={(e) => setKeepMonthly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                min={1}
              />
            </div>
            <div>
              <label htmlFor="retain-keep-yearly" className="block text-sm font-medium mb-1">
                Keep Yearly
              </label>
              <input
                id="retain-keep-yearly"
                type="number"
                value={keepYearly}
                onChange={(e) => setKeepYearly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                min={1}
              />
            </div>
            <div>
              <label htmlFor="retain-keep-within" className="block text-sm font-medium mb-1">
                Keep Within
              </label>
              <input
                id="retain-keep-within"
                type="text"
                value={keepWithin}
                onChange={(e) => setKeepWithin(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                placeholder="e.g. 1y"
              />
            </div>
            <div>
              <label htmlFor="retain-keep-within-hourly" className="block text-sm font-medium mb-1">
                Keep Within Hourly
              </label>
              <input
                id="retain-keep-within-hourly"
                type="text"
                value={keepWithinHourly}
                onChange={(e) => setKeepWithinHourly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
              />
            </div>
            <div>
              <label htmlFor="retain-keep-within-daily" className="block text-sm font-medium mb-1">
                Keep Within Daily
              </label>
              <input
                id="retain-keep-within-daily"
                type="text"
                value={keepWithinDaily}
                onChange={(e) => setKeepWithinDaily(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
              />
            </div>
            <div>
              <label htmlFor="retain-keep-within-weekly" className="block text-sm font-medium mb-1">
                Keep Within Weekly
              </label>
              <input
                id="retain-keep-within-weekly"
                type="text"
                value={keepWithinWeekly}
                onChange={(e) => setKeepWithinWeekly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
              />
            </div>
            <div>
              <label
                htmlFor="retain-keep-within-monthly"
                className="block text-sm font-medium mb-1"
              >
                Keep Within Monthly
              </label>
              <input
                id="retain-keep-within-monthly"
                type="text"
                value={keepWithinMonthly}
                onChange={(e) => setKeepWithinMonthly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
              />
            </div>
            <div>
              <label htmlFor="retain-keep-within-yearly" className="block text-sm font-medium mb-1">
                Keep Within Yearly
              </label>
              <input
                id="retain-keep-within-yearly"
                type="text"
                value={keepWithinYearly}
                onChange={(e) => setKeepWithinYearly(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
              />
            </div>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-base font-semibold mb-3">Backup Options</h2>
        <div className="space-y-3">
          <div>
            <label htmlFor="exclude-patterns" className="block text-sm font-medium mb-1">
              Exclude patterns
            </label>
            <textarea
              id="exclude-patterns"
              value={excludePatterns}
              onChange={(e) => setExcludePatterns(e.target.value)}
              rows={3}
              placeholder={'*.tmp\nnode_modules/'}
              className="border rounded px-2 py-1 text-sm w-full font-mono"
            />
            <p className="text-gray-500 text-xs mt-1">
              One pattern per line. Restic glob syntax (e.g. <code>*.log</code>, <code>cache/</code>
              ).
            </p>
          </div>

          <div className="flex items-center gap-2">
            <input
              id="exclude-caches"
              type="checkbox"
              checked={excludeCaches}
              onChange={(e) => setExcludeCaches(e.target.checked)}
            />
            <label htmlFor="exclude-caches" className="text-sm font-medium">
              Exclude caches (folders containing a <code>CACHEDIR.TAG</code>)
            </label>
          </div>

          <div>
            <label htmlFor="job-tags" className="block text-sm font-medium mb-1">
              Tags
            </label>
            <input
              id="job-tags"
              type="text"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
              placeholder="daily, important"
              className="border rounded px-2 py-1 text-sm w-full"
            />
            <p className="text-gray-500 text-xs mt-1">
              Comma-separated. Applied to each snapshot, useful for filtering with{' '}
              <code>restic snapshots --tag</code>.
            </p>
          </div>

          <div>
            <label htmlFor="job-compression" className="block text-sm font-medium mb-1">
              Compression
            </label>
            <select
              id="job-compression"
              value={compression}
              onChange={(e) => setCompression(e.target.value)}
              className="border rounded px-2 py-1 text-sm w-full bg-background"
            >
              <option value="">Default (auto)</option>
              <option value="auto">auto</option>
              <option value="off">off</option>
              <option value="max">max</option>
            </select>
          </div>

          <div>
            <label htmlFor="job-timeout-hours" className="block text-sm font-medium mb-1">
              Timeout (hours)
            </label>
            <input
              id="job-timeout-hours"
              type="number"
              min={1}
              value={timeoutHoursText}
              onChange={(e) => setTimeoutHoursText(e.target.value)}
              placeholder="24"
              className="border rounded px-2 py-1 text-sm w-full"
            />
            <p className="text-gray-500 text-xs mt-1">
              Kill the backup if it runs longer than this. Leave blank to use the default from
              Settings.
            </p>
          </div>
        </div>
      </section>

      {/* Verification section (content always in DOM so tests can interact) */}
      <section>
        <h2 className="text-base font-semibold mb-3">Verification</h2>
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <input
              id="check-enabled"
              type="checkbox"
              checked={checkEnabled}
              onChange={(e) => setCheckEnabled(e.target.checked)}
            />
            <label htmlFor="check-enabled" className="text-sm font-medium">
              Enable Integrity Check
            </label>
          </div>

          <div>
            <label htmlFor="check-mode" className="block text-sm font-medium mb-1">
              Check Mode
            </label>
            <select
              id="check-mode"
              value={checkMode}
              onChange={(e) => setCheckMode(e.target.value)}
              className="border rounded px-2 py-1 text-sm w-full"
            >
              <option value="">Select mode...</option>
              <option value="structural">Structural</option>
              <option value="subset">Subset</option>
              <option value="full">Full</option>
            </select>
          </div>

          {checkMode === 'subset' && (
            <div>
              <label htmlFor="check-subset-percent" className="block text-sm font-medium mb-1">
                Subset Percent
              </label>
              <input
                id="check-subset-percent"
                type="number"
                value={checkSubsetPercent}
                onChange={(e) => setCheckSubsetPercent(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
                min={1}
                max={100}
              />
            </div>
          )}

          <div>
            <label htmlFor="check-timeout-hours" className="block text-sm font-medium mb-1">
              Check Timeout (hours)
            </label>
            <input
              id="check-timeout-hours"
              type="number"
              value={checkTimeoutHours}
              onChange={(e) => setCheckTimeoutHours(e.target.value)}
              className="border rounded px-2 py-1 text-sm w-full"
              min={1}
            />
          </div>
        </div>
      </section>

      <button
        type="submit"
        className="px-4 py-2 bg-blue-600 text-white rounded text-sm font-medium"
      >
        {isEdit ? 'Save' : 'Create'}
      </button>
    </form>
  )
}
