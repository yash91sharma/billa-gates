import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Naive UTC datetime for SQLAlchemy column defaults.

    Returned as a naive datetime (no tzinfo) because the existing schema uses
    ``DateTime`` without ``timezone=True``; storing tz-aware values there would
    silently drop the tz on read.  Wrapping ``datetime.now(timezone.utc)`` keeps
    us off the deprecated ``datetime.utcnow()`` API while preserving wire
    compatibility.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class ScheduleType(str, Enum):
    cron = "cron"
    interval = "interval"


class RunStatus(str, Enum):
    running = "running"
    success = "success"
    warning = "warning"
    failed = "failed"
    skipped = "skipped"


class RunReason(str, Enum):
    overlapping_run = "overlapping_run"
    container_restart = "container_restart"


class TriggeredBy(str, Enum):
    scheduler = "scheduler"
    manual = "manual"


class PruneStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    skipped = "skipped"


class CheckStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    skipped = "skipped"


class CheckMode(str, Enum):
    structural = "structural"
    subset = "subset"
    full = "full"


class CompressionMode(str, Enum):
    auto = "auto"
    max = "max"
    off = "off"


class BackupJob(Base):
    __tablename__ = "backup_jobs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label: Mapped[str] = mapped_column(String(64), nullable=False)
    source_subpath: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    destination_label: Mapped[str] = mapped_column(String(64), nullable=False)
    restic_password: Mapped[str] = mapped_column(String, nullable=False)
    schedule_type: Mapped[ScheduleType] = mapped_column(
        SAEnum(ScheduleType), nullable=False
    )
    schedule_value: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    retain_keep_last: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retain_keep_hourly: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retain_keep_daily: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retain_keep_weekly: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retain_keep_monthly: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retain_keep_yearly: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retain_keep_within: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    retain_keep_within_hourly: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    retain_keep_within_daily: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    retain_keep_within_weekly: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    retain_keep_within_monthly: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    retain_keep_within_yearly: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )

    exclude_patterns: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    exclude_caches: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclude_if_present: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    one_file_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    no_scan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    compression: Mapped[Optional[CompressionMode]] = mapped_column(
        SAEnum(CompressionMode), nullable=True
    )
    pack_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    read_concurrency: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timeout_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    check_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    check_mode: Mapped[Optional[CheckMode]] = mapped_column(
        SAEnum(CheckMode), nullable=True
    )
    check_subset_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    check_timeout_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    runs: Mapped[List["BackupRun"]] = relationship(
        "BackupRun", back_populates="job", cascade="all, delete-orphan"
    )
    snapshots: Mapped[List["Snapshot"]] = relationship(
        "Snapshot", back_populates="job", cascade="all, delete-orphan"
    )


class BackupRun(Base):
    __tablename__ = "backup_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backup_jobs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(SAEnum(RunStatus), nullable=False)
    reason: Mapped[Optional[RunReason]] = mapped_column(
        SAEnum(RunReason), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    files_new: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    files_changed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    files_unmodified: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dirs_new: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dirs_changed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dirs_unmodified: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    data_added_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    data_added_packed_bytes: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    total_bytes_processed: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    backup_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prune_status: Mapped[Optional[PruneStatus]] = mapped_column(
        SAEnum(PruneStatus), nullable=True
    )
    prune_error_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    check_status: Mapped[Optional[CheckStatus]] = mapped_column(
        SAEnum(CheckStatus), nullable=True
    )
    check_error_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[TriggeredBy] = mapped_column(
        SAEnum(TriggeredBy), nullable=False
    )

    job: Mapped["BackupJob"] = relationship("BackupJob", back_populates="runs")
    snapshots: Mapped[List["Snapshot"]] = relationship("Snapshot", back_populates="run")


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("job_id", "snapshot_id"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backup_jobs.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("backup_runs.id"), nullable=True
    )
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    paths: Mapped[List[str]] = mapped_column(JSON, nullable=False)
    tags: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    job: Mapped["BackupJob"] = relationship("BackupJob", back_populates="snapshots")
    run: Mapped[Optional["BackupRun"]] = relationship(
        "BackupRun", back_populates="snapshots"
    )


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    ntfy_server_url: Mapped[str] = mapped_column(
        String(512), nullable=False, default="https://ntfy.sh"
    )
    ntfy_topic: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ntfy_token: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    notify_on_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_on_success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    notify_on_failure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    notify_on_warning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    notify_on_verification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    restic_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_job_timeout_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24
    )
    # Cap on rows kept in `backup_runs` per job — older rows are trimmed
    # oldest-first after each run finishes. Does not affect restic snapshots.
    keep_last_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # When True, `restic unlock` runs before every backup so a stale lock left
    # by an abrupt termination (container OOM, host reboot) doesn't break all
    # future backups for the job. Defaults True because this is a single-tenant
    # deployment — no other writer can hold a legitimate lock.
    auto_unlock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
