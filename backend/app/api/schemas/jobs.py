"""Pydantic schemas for BackupJob requests and responses."""

import re
from datetime import datetime
from typing import Any, List, Optional

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from app.api.schemas.base import UTCDateTime
from app.db.models import CheckMode, CompressionMode, ScheduleType

# name, the labels, and source_subpath are single path components concatenated
# into /sources/<label>[/<subpath>] and /destinations/<label>/<name>. The
# whitelist requires a leading word character (\w — unicode letters, digits,
# underscore) so '.', '..', and hidden '.name' directories are rejected, then
# allows word characters, spaces, inner dots, and hyphens. '/' is excluded
# entirely, so a value can never traverse outside its mount root.
#
# `name` is included because it names the job's repository directory — it is
# not a free-text label. That also rules out punctuation like the em-dash in
# 'Documents — Daily'.
_PATH_COMPONENT_RE = re.compile(r"^[\w][\w .-]*$")
_INTERVAL_RE = re.compile(r"^([1-9][0-9]*)(h|d|m)$")

# Minimum cron interval: 1 hour (3600 seconds).
_MIN_CRON_INTERVAL_SECONDS = 3600

# Minimum interval schedule value in minutes.
_MIN_INTERVAL_MINUTES = 5

# ── Limits restic itself imposes ─────────────────────────────────────────────
#
# A value restic refuses has to be caught here, not stored and then blown up on
# every run: a rejected --pack-size fails the whole backup, and a rejected
# --keep-within fails `restic forget` while the run still reports success — so
# retention silently stops applying and the repo grows forever. Verified
# against restic 0.19.1 — every limit below is unchanged from 0.18.1, rejected
# with the same messages.

# "pack size smaller than minimum of 4 MiB" / "larger than limit of 128 MiB".
_PACK_SIZE_MIN_MIB = 4
_PACK_SIZE_MAX_MIB = 128

# `--keep-within*` durations: one or more <integer><unit> pairs, units y/m/d/h,
# lowercase, no separators. Order and repeats are allowed by restic ('3h2y' and
# '1d1d' both parse); weeks are not a unit, and a bare number is rejected with
# "no unit found after number".
_KEEP_WITHIN_RE = re.compile(r"^(?:\d+[ymdh])+$")

# Not a restic limit: the run timeout drives asyncio.wait_for, so 0/negative
# would abort a run instantly. The ceiling matches AppSettings'
# default_job_timeout_hours (1 week).
_MIN_TIMEOUT_HOURS = 1
_MAX_TIMEOUT_HOURS = 168


def _validate_keep_within(value: str, field_name: str) -> str:
    """Validate one `--keep-within*` value against restic's duration parser."""
    if not _KEEP_WITHIN_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a restic duration: one or more "
            f"<number><unit> pairs using y, m, d, or h — for example '30d', "
            f"'48h', or '2y5m7d3h'. Weeks ('8w'), spaces, decimals, and bare "
            f"numbers are rejected by restic"
        )
    return value


def _validate_label(value: str, field_name: str) -> str:
    """Validate a mount label or subpath as one safe path component.

    Rejects the path-traversal vectors ('/', '.', '..') outright: a
    source_subpath of '..' would resolve /sources/<label>/.. to /sources and
    silently back up every mounted source. The remaining whitelist keeps any
    reasonable directory name working (unicode letters and digits included).
    """
    if not _PATH_COMPONENT_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} must start with a letter, digit, or underscore and "
            f"may contain only letters, digits, underscores, spaces, dots, "
            f"and hyphens ('/', '.', and '..' are not allowed)"
        )
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
    pack_size: Optional[int] = Field(None, ge=_PACK_SIZE_MIN_MIB, le=_PACK_SIZE_MAX_MIB)
    read_concurrency: Optional[int] = Field(None, ge=1)
    timeout_hours: Optional[int] = Field(
        None, ge=_MIN_TIMEOUT_HOURS, le=_MAX_TIMEOUT_HOURS
    )

    # --- Integrity verification ---
    check_enabled: bool = False
    check_mode: Optional[CheckMode] = None
    check_subset_percent: Optional[int] = Field(None, ge=1, le=100)
    check_timeout_hours: Optional[int] = Field(
        None, ge=_MIN_TIMEOUT_HOURS, le=_MAX_TIMEOUT_HOURS
    )

    @field_validator(
        "retain_keep_within",
        "retain_keep_within_hourly",
        "retain_keep_within_daily",
        "retain_keep_within_weekly",
        "retain_keep_within_monthly",
        "retain_keep_within_yearly",
    )
    @classmethod
    def validate_keep_within(
        cls, v: Optional[str], info: ValidationInfo
    ) -> Optional[str]:
        return v if v is None else _validate_keep_within(v, info.field_name or "value")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_label(v, "name")

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
        return v if v is None else _validate_label(v, "source_subpath")

    @model_validator(mode="after")
    def validate_schedule(self) -> "JobCreate":
        """Validate schedule_value against the chosen schedule_type."""
        _validate_schedule_value(self.schedule_type, self.schedule_value)
        return self


class JobUpdate(BaseModel):
    """Partial update for a BackupJob — every field is optional.

    The route applies ``model_dump(exclude_unset=True)``: a field absent from
    the payload keeps its stored value; an explicit null clears a nullable
    field (nulls on non-nullable columns are rejected at the route).
    restic_password is the one exception — both absent and null mean "keep
    the stored password" (it is never echoed to the client, so the edit form
    cannot round-trip it).

    destination_label immutability, password immutability after a successful
    run, and schedule validation against the merged (stored + updated) pair
    are enforced at the route layer, where the stored values are available.
    """

    name: Optional[str] = Field(None, max_length=128)
    source_label: Optional[str] = None
    source_subpath: Optional[str] = None
    destination_label: Optional[str] = None
    restic_password: Optional[str] = None
    schedule_type: Optional[ScheduleType] = None
    schedule_value: Optional[str] = None
    enabled: Optional[bool] = None

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

    exclude_patterns: Optional[List[str]] = None
    exclude_caches: Optional[bool] = None
    exclude_if_present: Optional[List[str]] = None
    one_file_system: Optional[bool] = None
    no_scan: Optional[bool] = None
    tags: Optional[List[str]] = None
    compression: Optional[CompressionMode] = None
    pack_size: Optional[int] = Field(None, ge=_PACK_SIZE_MIN_MIB, le=_PACK_SIZE_MAX_MIB)
    read_concurrency: Optional[int] = Field(None, ge=1)
    timeout_hours: Optional[int] = Field(
        None, ge=_MIN_TIMEOUT_HOURS, le=_MAX_TIMEOUT_HOURS
    )

    check_enabled: Optional[bool] = None
    check_mode: Optional[CheckMode] = None
    check_subset_percent: Optional[int] = Field(None, ge=1, le=100)
    check_timeout_hours: Optional[int] = Field(
        None, ge=_MIN_TIMEOUT_HOURS, le=_MAX_TIMEOUT_HOURS
    )

    @field_validator(
        "retain_keep_within",
        "retain_keep_within_hourly",
        "retain_keep_within_daily",
        "retain_keep_within_weekly",
        "retain_keep_within_monthly",
        "retain_keep_within_yearly",
    )
    @classmethod
    def validate_keep_within(
        cls, v: Optional[str], info: ValidationInfo
    ) -> Optional[str]:
        return v if v is None else _validate_keep_within(v, info.field_name or "value")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validate_label(v, "name")

    @field_validator("source_label")
    @classmethod
    def validate_source_label(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validate_label(v, "source_label")

    @field_validator("destination_label")
    @classmethod
    def validate_destination_label(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validate_label(v, "destination_label")

    @field_validator("source_subpath")
    @classmethod
    def validate_source_subpath(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validate_label(v, "source_subpath")


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
    next_run_time and last_run are computed at request time and injected by
    the route helpers.
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
    next_run_time: Optional[UTCDateTime] = None
    last_run: Optional[RunSummarySchema] = None


class JobCheckRequest(BaseModel):
    """Payload to trigger a manual integrity check."""

    check_mode: CheckMode = CheckMode.structural
    check_subset_percent: Optional[int] = Field(None, ge=1, le=100)
    timeout_hours: Optional[int] = Field(None, ge=1, le=168)

    @model_validator(mode="after")
    def validate_subset_percent(self) -> "JobCheckRequest":
        if self.check_mode == CheckMode.subset and self.check_subset_percent is None:
            raise ValueError(
                "check_subset_percent is required when check_mode is 'subset'"
            )
        return self
