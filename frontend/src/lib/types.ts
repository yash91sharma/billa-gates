export type ScheduleType = 'cron' | 'interval'
export type RunStatus = 'running' | 'success' | 'warning' | 'failed' | 'skipped'
export type RunReason = 'overlapping_run' | 'container_restart'
export type TriggeredBy = 'scheduler' | 'manual'
export type PruneStatus = 'passed' | 'failed' | 'skipped'
export type CheckStatus = 'passed' | 'failed' | 'skipped'
export type CheckMode = 'structural' | 'subset' | 'full'
export type CompressionMode = 'auto' | 'max' | 'off'

export interface RunSummary {
  id: string
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
  has_successful_run: boolean
}

export interface BackupRun {
  id: string
  job_id: string
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

export interface Snapshot {
  id: string
  job_id: string
  run_id: string | null
  snapshot_id: string
  snapshot_time: string
  hostname: string
  paths: string[]
  tags: string[] | null
  size_bytes: number | null
  captured_at: string
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
