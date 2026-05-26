"""Pydantic schemas for BackupJob requests and responses."""

import re
from datetime import datetime
from typing import Any, List, Optional

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.schemas.base import UTCDateTime
from app.db.models import CheckMode, CompressionMode, ScheduleType

_LABEL_RE = re.compile(r"^[^/]+$")
_INTERVAL_RE = re.compile(r"^([1-9][0-9]*)(h|d|m)$")

# Minimum cron interval: 1 hour (3600 seconds).
_MIN_CRON_INTERVAL_SECONDS = 3600

# Minimum interval schedule value in minutes.
_MIN_INTERVAL_MINUTES = 5


def _validate_label(value: str, field_name: str) -> str:
    """Reject labels that contain slashes or equal '..'."""
    if "/" in value or value == "..":
        raise ValueError(f"{field_name} must not contain '/' or be '..'")
    return value


def _validate_schedule_value(schedule_type: ScheduleType, schedule_value: str) -> None:
    """Validate schedule_value against schedule_type rules.

    Interval: must match r'^([1-9][0-9]*)(h|d|m)$'; minimum 5 minutes.
    Cron: must be a valid crontab expression; minimum hourly frequency.
    """
    if schedule_type == ScheduleType.interval:
        m = _INTERVAL_RE.match(schedule_value)
        if not m:
            raise ValueError(
                "Interval must be in the format '6h', '1d', or '30m' "
                "(positive integer followed by h/d/m)"
            )
        n, unit = int(m.group(1)), m.group(2)
        if unit == "m" and n < _MIN_INTERVAL_MINUTES:
            raise ValueError(f"Minimum interval is {_MIN_INTERVAL_MINUTES} minutes")

    elif schedule_type == ScheduleType.cron:
        # apscheduler lacks type stubs; trigger and its methods are Any here.
        cron: Any = CronTrigger
        try:
            trigger: Any = cron.from_crontab(schedule_value)
        except Exception:
            raise ValueError(f"Invalid cron expression: {schedule_value!r}")

        # Enforce hourly-or-less-frequent by checking the gap between the
        # next two fire times.
        from datetime import timezone

        now = datetime.now(timezone.utc)
        t1: datetime | None = trigger.get_next_fire_time(None, now)
        if t1 is not None:
            t2: datetime | None = trigger.get_next_fire_time(t1, t1)
            if t2 is not None:
                gap: float = (t2 - t1).total_seconds()
                if gap < _MIN_CRON_INTERVAL_SECONDS:
                    raise ValueError(
                        "Cron schedule fires more than once per hour; "
                        "minimum allowed frequency is hourly"
                    )


class JobCreate(BaseModel):
    """Fields required (or optionally provided) when creating a BackupJob."""

    # --- Core identity ---
    name: str = Field(max_length=128)
    source_label: str
    source_subpath: Optional[str] = None
    destination_label: str
    restic_password: str
    schedule_type: ScheduleType
    schedule_value: str
    enabled: bool = True

    # --- Retention policy ---
    retain_keep_last: Optional[int] = Field(None, ge=1, le=9999)
    retain_keep_hourly: Optional[int] = Field(None, ge=1, le=9999)
    retain_keep_daily: Optional[int] = Field(None, ge=1, le=9999)
    retain_keep_weekly: Optional[int] = Field(None, ge=1, le=9999)
    retain_keep_monthly: Optional[int] = Field(None, ge=1, le=9999)
    retain_keep_yearly: Optional[int] = Field(None, ge=1, le=9999)
    retain_keep_within: Optional[str] = None
    retain_keep_within_hourly: Optional[str] = None
    retain_keep_within_daily: Optional[str] = None
    retain_keep_within_weekly: Optional[str] = None
    retain_keep_within_monthly: Optional[str] = None
    retain_keep_within_yearly: Optional[str] = None

    # --- Backup options ---
    exclude_patterns: Optional[List[str]] = None
    exclude_caches: bool = False
    exclude_if_present: Optional[List[str]] = None
    one_file_system: bool = False
    no_scan: bool = False
    tags: Optional[List[str]] = None
    compression: Optional[CompressionMode] = None
    pack_size: Optional[int] = Field(None, ge=1, le=1500)
    read_concurrency: Optional[int] = None
    timeout_hours: Optional[int] = None

    # --- Integrity verification ---
    check_enabled: bool = False
    check_mode: Optional[CheckMode] = None
    check_subset_percent: Optional[int] = Field(None, ge=1, le=100)
    check_timeout_hours: Optional[int] = None

    @field_validator("source_label")
    @classmethod
    def validate_source_label(cls, v: str) -> str:
        return _validate_label(v, "source_label")

    @field_validator("destination_label")
    @classmethod
    def validate_destination_label(cls, v: str) -> str:
        return _validate_label(v, "destination_label")

    @field_validator("source_subpath")
    @classmethod
    def validate_source_subpath(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and "/" in v:
            raise ValueError("source_subpath must not contain '/'")
        return v

    @model_validator(mode="after")
    def validate_schedule(self) -> "JobCreate":
        """Validate schedule_value against the chosen schedule_type."""
        _validate_schedule_value(self.schedule_type, self.schedule_value)
        return self

    @model_validator(mode="after")
    def validate_check_settings(self) -> "JobCreate":
        """Validate check settings cross-dependencies."""
        if self.check_enabled and not self.check_mode:
            raise ValueError("check_mode is required when check_enabled is True")
        if self.check_mode == CheckMode.subset and self.check_subset_percent is None:
            raise ValueError(
                "check_subset_percent is required when check_mode is 'subset'"
            )
        return self


class JobUpdate(JobCreate):
    """All JobCreate fields, but restic_password is optional.

    Omitting restic_password leaves the stored password unchanged.
    The destination_label and (after a successful run) restic_password are
    enforced as immutable at the route layer, not here.
    """

    restic_password: Optional[str] = None  # type: ignore[assignment]


class RunSummarySchema(BaseModel):
    """Compact run record — excludes large output text fields."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    kind: str = "backup"
    status: str
    reason: Optional[str] = None
    triggered_by: str
    started_at: UTCDateTime
    finished_at: Optional[UTCDateTime] = None
    duration_seconds: Optional[int] = None
    snapshot_id: Optional[str] = None
    files_new: Optional[int] = None
    files_changed: Optional[int] = None
    files_unmodified: Optional[int] = None
    dirs_new: Optional[int] = None
    dirs_changed: Optional[int] = None
    dirs_unmodified: Optional[int] = None
    data_added_bytes: Optional[int] = None
    data_added_packed_bytes: Optional[int] = None
    total_bytes_processed: Optional[int] = None
    prune_status: Optional[str] = None
    check_status: Optional[str] = None
    # Populated only for /runs/recent responses (joined from BackupJob).
    job_name: Optional[str] = None


class SnapshotResponse(BaseModel):
    """Snapshot record sourced live from restic.

    Restic is the source of truth — there is no DB row to attach an internal
    UUID, run linkage, or `captured_at` to. The `BackupRun.snapshot_id`
    column links runs to their snapshot via this `snapshot_id`.
    """

    snapshot_id: str
    snapshot_time: UTCDateTime
    hostname: str
    paths: List[str]
    tags: Optional[List[str]] = None
    size_bytes: Optional[int] = None


class JobResponse(BaseModel):
    """Full job record returned by all job endpoints.

    restic_password is always None — it is never exposed via the API.
    has_successful_run, next_run_time, and last_run are computed at
    request time and injected by the route helpers.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_label: str
    source_subpath: Optional[str] = None
    destination_label: str
    restic_password: None = None
    schedule_type: str
    schedule_value: str
    enabled: bool

    retain_keep_last: Optional[int] = None
    retain_keep_hourly: Optional[int] = None
    retain_keep_daily: Optional[int] = None
    retain_keep_weekly: Optional[int] = None
    retain_keep_monthly: Optional[int] = None
    retain_keep_yearly: Optional[int] = None
    retain_keep_within: Optional[str] = None
    retain_keep_within_hourly: Optional[str] = None
    retain_keep_within_daily: Optional[str] = None
    retain_keep_within_weekly: Optional[str] = None
    retain_keep_within_monthly: Optional[str] = None
    retain_keep_within_yearly: Optional[str] = None

    exclude_patterns: Optional[List[str]] = None
    exclude_caches: bool = False
    exclude_if_present: Optional[List[str]] = None
    one_file_system: bool = False
    no_scan: bool = False
    tags: Optional[List[str]] = None
    compression: Optional[str] = None
    pack_size: Optional[int] = None
    read_concurrency: Optional[int] = None
    timeout_hours: Optional[int] = None

    check_enabled: bool = False
    check_mode: Optional[str] = None
    check_subset_percent: Optional[int] = None
    check_timeout_hours: Optional[int] = None

    created_at: UTCDateTime
    updated_at: UTCDateTime

    # Computed fields injected by the route layer.
    has_successful_run: bool = False
    next_run_time: Optional[UTCDateTime] = None
    last_run: Optional[RunSummarySchema] = None
