"""APScheduler instance and startup/shutdown helpers.

The scheduler uses MemoryJobStore and is rebuilt from BackupJob DB rows on
every startup — no persistent APScheduler state survives a container restart.

Module-level names (engine, scheduler) are intentionally importable so that
tests can patch them via 'app.core.scheduler.<name>'.
"""

import re
import uuid
from typing import Any, Union

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger, log_call
from app.db.database import engine
from app.db.models import (
    AppSettings,
    BackupJob,
    BackupRun,
    CheckStatus,
    PruneStatus,
    RunReason,
    RunStatus,
    TriggeredBy,
)
from app.services import restic as restic_svc
from app.services.backup_runner import trigger_run

logger = get_logger(__name__)

scheduler = AsyncIOScheduler(
    job_defaults={"misfire_grace_time": 3600, "coalesce": True},
)

_INTERVAL_RE = re.compile(r"^([1-9][0-9]*)(h|d|m)$")


@log_call
def build_trigger(
    schedule_type: str, schedule_value: str
) -> Union[CronTrigger, IntervalTrigger]:
    """Convert a (schedule_type, schedule_value) pair into an APScheduler trigger.

    Interval format: '6h', '1d', '30m'  (positive integer + h/d/m).
    Cron format: standard crontab expression passed to CronTrigger.from_crontab.
    """
    if schedule_type == "cron":
        cron: Any = CronTrigger
        cron_trigger: CronTrigger = cron.from_crontab(schedule_value)
        return cron_trigger
    if schedule_type == "interval":
        m = _INTERVAL_RE.match(schedule_value)
        if not m:
            raise ValueError(f"Invalid interval value: {schedule_value!r}")
        n, unit = int(m.group(1)), m.group(2)
        if unit == "h":
            return IntervalTrigger(hours=n)
        if unit == "d":
            return IntervalTrigger(days=n)
        return IntervalTrigger(minutes=n)
    raise ValueError(f"Unknown schedule_type: {schedule_type!r}")


@log_call
async def start_scheduler() -> None:
    """Run all startup tasks and start the scheduler.

    1. Seed AppSettings(id=1) with defaults if it does not yet exist.
    2. Detect the installed restic version and persist it in AppSettings.
    3. Mark any BackupRun rows that were left in 'running' state as failed
       (reason=container_restart) — these represent runs interrupted by a
       previous container stop.
    4. Mark 'running' or 'success/failed' rows whose check_status is NULL as
       skipped — the check will not complete now that the container restarted.
    5. Register every enabled BackupJob with the scheduler.
    6. Start the scheduler (skipped if it is already running).
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        # ── 1. Seed AppSettings ───────────────────────────────────────────────
        settings = await session.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.sh",
                ntfy_topic="",
                default_job_timeout_hours=24,
            )
            session.add(settings)
            await session.flush()

        # ── 2. Detect restic version ──────────────────────────────────────────
        version = await restic_svc.restic_version()
        settings.restic_version = version

        # ── 3. Clean up stale 'running' rows ─────────────────────────────────
        stale_running = await session.execute(
            select(BackupRun).where(BackupRun.status == RunStatus.running)
        )
        for run in stale_running.scalars().all():
            run.status = RunStatus.failed
            run.reason = RunReason.container_restart
            run.prune_status = PruneStatus.skipped
            run.check_status = CheckStatus.skipped

        # ── 4. Clean up rows with null check_status ───────────────────────────
        null_check = await session.execute(
            select(BackupRun).where(
                BackupRun.status.in_([RunStatus.success, RunStatus.failed]),
                BackupRun.check_status.is_(None),
            )
        )
        for run in null_check.scalars().all():
            run.check_status = CheckStatus.skipped

        # ── 5. Register enabled jobs ──────────────────────────────────────────
        enabled_jobs = await session.execute(
            select(BackupJob).where(BackupJob.enabled == True)  # noqa: E712
        )
        jobs = enabled_jobs.scalars().all()

        await session.commit()

    # Register after the session is closed to avoid holding a DB connection
    # while APScheduler does its own async work.
    for job in jobs:
        _register_job(job)
    logger.info("scheduler registered %d enabled jobs at startup", len(jobs))

    # ── 6. Start scheduler ────────────────────────────────────────────────────
    if not scheduler.running:
        scheduler.start()
        logger.info("scheduler started")


@log_call
def _register_job(job: BackupJob) -> None:
    """Add a single BackupJob to the scheduler."""
    trigger = build_trigger(job.schedule_type, job.schedule_value)
    # apscheduler lacks type stubs; cast to Any at the boundary.
    sched: Any = scheduler
    sched.add_job(
        trigger_run,
        trigger=trigger,
        args=[uuid.UUID(job.id), TriggeredBy.scheduler],
        id=job.id,
        replace_existing=True,
    )


@log_call
async def shutdown_scheduler() -> None:
    """Gracefully stop the scheduler if it is running."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
