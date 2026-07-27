export type ScheduleType = 'cron' | 'interval'
export type RunKind = 'backup' | 'prune' | 'check'
export type RunStatus = 'running' | 'success' | 'warning' | 'failed' | 'skipped' | 'canceled'
export type RunReason = 'overlapping_run' | 'container_restart' | 'user_canceled'
export type TriggeredBy = 'scheduler' | 'manual'
export type PruneStatus = 'passed' | 'failed' | 'skipped'
export type CheckStatus = 'passed' | 'failed' | 'skipped'
export type CheckMode = 'structural' | 'subset' | 'full'
// `fastest` and `better` require restic >= 0.19.0 (see Dockerfile RESTIC_VERSION).
export type CompressionMode = 'auto' | 'max' | 'off' | 'fastest' | 'better'

export interface RunSummary {
  id: string
  kind: RunKind
  status: RunStatus
  check_status: CheckStatus | null
  started_at: string
  finished_at: string | null
  duration_seconds: number | null
  triggered_by: TriggeredBy
}

export interface BackupJob {
  id: string
  name: string
  source_label: string
  source_subpath: string | null
  destination_label: string
  restic_password: null
  schedule_type: ScheduleType
  schedule_value: string
  enabled: boolean
  retain_keep_last: number | null
  retain_keep_hourly: number | null
  retain_keep_daily: number | null
  retain_keep_weekly: number | null
  retain_keep_monthly: number | null
  retain_keep_yearly: number | null
  retain_keep_within: string | null
  retain_keep_within_hourly: string | null
  retain_keep_within_daily: string | null
  retain_keep_within_weekly: string | null
  retain_keep_within_monthly: string | null
  retain_keep_within_yearly: string | null
  exclude_patterns: string[] | null
  exclude_caches: boolean
  exclude_if_present: string[] | null
  one_file_system: boolean
  no_scan: boolean
  tags: string[] | null
  compression: CompressionMode | null
  pack_size: number | null
  read_concurrency: number | null
  timeout_hours: number | null
  check_enabled: boolean
  check_mode: CheckMode | null
  check_subset_percent: number | null
  check_timeout_hours: number | null
  created_at: string
  updated_at: string
  next_run_time: string | null
  last_run: RunSummary | null
}

export interface BackupRun {
  id: string
  job_id: string
  kind: RunKind
  status: RunStatus
  reason: RunReason | null
  started_at: string
  finished_at: string | null
  duration_seconds: number | null
  snapshot_id: string | null
  files_new: number | null
  files_changed: number | null
  files_unmodified: number | null
  dirs_new: number | null
  dirs_changed: number | null
  dirs_unmodified: number | null
  data_added_bytes: number | null
  data_added_packed_bytes: number | null
  total_bytes_processed: number | null
  backup_output: string | null
  error_output: string | null
  prune_status: PruneStatus | null
  prune_error_output: string | null
  check_status: CheckStatus | null
  check_error_output: string | null
  triggered_by: TriggeredBy
  job_name?: string
}

/** One restic command a backup run of a job issues.
 *
 * Rendered server-side (app/services/job_commands.py) from the same builders
 * the runner execs — never re-assembled in the browser, or the page would
 * become a second source of truth and drift from what actually runs.
 * `command` is null when the job's configuration turns the step off entirely
 * (no retention policy, for instance); `condition` says when a step applies.
 */
export interface JobCommand {
  step: string
  title: string
  description: string
  /** `backup_run` — issued by a backup run itself, scheduled or manual.
   *  `on_demand` — issued only when the operator clicks Prune, Integrity
   *  Check or Unlock. The page must keep the two visibly apart: nothing in
   *  the second group ever happens on a schedule. */
  group: 'backup_run' | 'on_demand'
  runs: boolean
  condition: string | null
  env: Record<string, string>
  argv: string[]
  command: string | null
}

export interface Snapshot {
  snapshot_id: string
  snapshot_time: string
  hostname: string
  paths: string[]
  tags: string[] | null
  size_bytes: number | null
}

export interface AppSettings {
  id: number
  ntfy_server_url: string
  ntfy_topic: string
  ntfy_token: string | null
  notify_on_start: boolean
  notify_on_success: boolean
  notify_on_failure: boolean
  notify_on_warning: boolean
  notify_on_verification: boolean
  restic_version: string | null
  default_job_timeout_hours: number
  keep_last_runs: number
  auto_unlock: boolean
  metadata_timeout_seconds: number
}

export interface HealthStatus {
  scheduler_running: boolean
  restic_version: string | null
  db_ok: boolean
}

export interface ResticUpdateCheck {
  current: string | null
  latest: string | null
  update_available: boolean | null
}

export interface RenameDestinationResult {
  affected_jobs: Array<{ id: string; name: string }>
}
