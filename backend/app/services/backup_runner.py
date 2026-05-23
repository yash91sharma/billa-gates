import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.database import engine
from app.db.models import (
    AppSettings,
    BackupJob,
    BackupRun,
    CheckStatus,
    PruneStatus,
    RunStatus,
    Snapshot,
    TriggeredBy,
)
from app.services import restic
from app.services.notifications import send_notification

logger = get_logger(__name__)

active_jobs: Set[uuid.UUID] = set()
job_locks: Dict[uuid.UUID, asyncio.Lock] = {}


def _extract_failed_items(backup_stdout: str) -> List[str]:
    """Pull per-file error messages out of restic's --json output stream so
    the run record can show *which* files failed, not just that something did.
    """
    items: List[str] = []
    for line in backup_stdout.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("message_type") == "error":
            err = obj.get("error", {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            item = obj.get("item")
            if item:
                items.append(f"{item}: {msg}")
            elif msg:
                items.append(str(msg))
    return items


async def run_backup(job_id: uuid.UUID, run_id: Optional[uuid.UUID] = None) -> None:
    """12-step backup lifecycle orchestration."""
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    # Pre-step: Job lookup
    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))

    if not job:
        logger.warning(f"job_id={job_id} not found in database")
        return

    logger.info(f"job_id={job_id} run_id={run_id} backup_started")

    # Two invocation paths: scheduler (no run_id) or API (run_id provided)
    current_run_id: uuid.UUID | None = None
    try:
        if run_id is None:
            # Scheduler path: do concurrent run guard and create run row
            logger.debug(f"job_id={job_id} step=concurrent_guard acquiring lock")
            lock: asyncio.Lock = job_locks.setdefault(job_id, asyncio.Lock())
            async with lock:
                async with factory() as s:
                    result = await s.execute(
                        select(BackupRun).where(
                            BackupRun.job_id == str(job_id),
                            BackupRun.status == RunStatus.running,
                        )
                    )
                    running_run: BackupRun | None = result.scalars().first()

                if running_run:
                    current_run_id = uuid.UUID(str(uuid.uuid4()))
                    logger.info(
                        f"job_id={job_id} step=concurrent_guard "
                        f"reason=overlapping_run skipping"
                    )
                    async with factory() as s:
                        skipped_run: BackupRun = BackupRun(
                            id=str(current_run_id),
                            job_id=str(job_id),
                            status=RunStatus.skipped,
                            reason="overlapping_run",
                            started_at=datetime.now(timezone.utc),
                            finished_at=datetime.now(timezone.utc),
                            triggered_by=TriggeredBy.scheduler,
                            prune_status=PruneStatus.skipped,
                            check_status=CheckStatus.skipped,
                        )
                        s.add(skipped_run)
                        await s.commit()
                    return

                # Create running row
                started_now: datetime = datetime.now(timezone.utc)
                async with factory() as s:
                    new_run: BackupRun = BackupRun(
                        id=str(uuid.uuid4()),
                        job_id=str(job_id),
                        status=RunStatus.running,
                        started_at=started_now,
                        triggered_by=TriggeredBy.scheduler,
                    )
                    s.add(new_run)
                    await s.commit()
                    current_run_id = uuid.UUID(new_run.id)
                logger.debug(
                    f"job_id={job_id} run_id={current_run_id} step=concurrent_guard "
                    f"run_row_created"
                )
        else:
            # API path: run_id was provided
            current_run_id = run_id

        active_jobs.add(job_id)
        logger.debug(f"job_id={job_id} run_id={current_run_id} added to active_jobs")

        # Step 2: Validate password
        logger.debug(f"job_id={job_id} run_id={current_run_id} step=validate_password")
        job_password: str = job.restic_password
        if not job_password:
            logger.error(
                f"job_id={job_id} run_id={current_run_id} step=validate_password "
                f"error=no_password_configured"
            )
            async with factory() as s:
                failed_run: BackupRun | None = await s.get(
                    BackupRun, str(current_run_id)
                )
                if failed_run:
                    now_utc: datetime = datetime.now(timezone.utc)
                    failed_run.status = RunStatus.failed
                    failed_run.error_output = (
                        "No restic password configured for this job."
                    )
                    failed_run.finished_at = now_utc
                    failed_run.duration_seconds = 0
                    failed_run.prune_status = PruneStatus.skipped
                    failed_run.check_status = CheckStatus.skipped
                    await s.commit()
            return

        # Load settings once for use throughout
        settings_dict: Dict[str, Any] = {}
        async with factory() as s:
            settings_obj: AppSettings | None = await s.get(AppSettings, 1)
            if settings_obj:
                settings_dict = {
                    "ntfy_server_url": settings_obj.ntfy_server_url,
                    "ntfy_topic": settings_obj.ntfy_topic,
                    "ntfy_token": settings_obj.ntfy_token,
                    "notify_on_start": settings_obj.notify_on_start,
                    "notify_on_success": settings_obj.notify_on_success,
                    "notify_on_failure": settings_obj.notify_on_failure,
                    "notify_on_warning": settings_obj.notify_on_warning,
                    "notify_on_verification": settings_obj.notify_on_verification,
                    "default_job_timeout_hours": settings_obj.default_job_timeout_hours,
                }

        # Step 3: Start notification
        ntfy_topic: str | None = cast(str | None, settings_dict.get("ntfy_topic"))
        if settings_dict.get("notify_on_start") and ntfy_topic:
            logger.info(f"step=start_notification job_id={job_id}")
            src: str = job.source_label
            dst: str = job.destination_label
            await send_notification(
                cast(str | None, settings_dict.get("ntfy_server_url")),
                ntfy_topic,
                f"Starting backup: {job.name}",
                f"Source: {src}, Destination: {dst}",
                token=cast(str | None, settings_dict.get("ntfy_token")),
            )

        # Build repo and source paths
        job_dest_label: str = job.destination_label
        job_source_label: str = job.source_label
        job_source_subpath: str | None = job.source_subpath
        repo_path: str = f"/destinations/{job_dest_label}/{str(job_id)}"
        source_path: str = f"/sources/{job_source_label}"
        if job_source_subpath:
            source_path = f"{source_path}/{job_source_subpath}"

        # Step 4: Init check
        logger.debug(
            f"job_id={job_id} run_id={current_run_id} step=init_check "
            f"repo_path={repo_path}"
        )
        rc: int
        stderr: str
        rc, _, stderr = await restic.restic_cat_config(repo_path, job_password)
        if rc != 0:
            if "wrong password" in stderr.lower():
                logger.error(
                    f"job_id={job_id} run_id={current_run_id} step=init_check "
                    f"error=wrong_password"
                )
                async with factory() as s:
                    wrong_pwd_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if wrong_pwd_run:
                        now_utc = datetime.now(timezone.utc)
                        wrong_pwd_run.status = RunStatus.failed
                        wrong_pwd_run.error_output = stderr
                        wrong_pwd_run.finished_at = now_utc
                        wrong_pwd_run.duration_seconds = 0
                        wrong_pwd_run.prune_status = PruneStatus.skipped
                        wrong_pwd_run.check_status = CheckStatus.skipped
                        await s.commit()
                return

            # Try to init
            logger.info(
                f"job_id={job_id} run_id={current_run_id} step=init_check "
                f"repo_not_found initializing"
            )
            rc, _, init_stderr = await restic.restic_init(repo_path, job_password)
            if rc != 0:
                logger.error(
                    f"job_id={job_id} run_id={current_run_id} step=init_check "
                    f"error=init_failed"
                )
                async with factory() as s:
                    init_fail_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if init_fail_run:
                        now_utc = datetime.now(timezone.utc)
                        init_fail_run.status = RunStatus.failed
                        init_fail_run.error_output = init_stderr
                        init_fail_run.finished_at = now_utc
                        init_fail_run.duration_seconds = 0
                        init_fail_run.prune_status = PruneStatus.skipped
                        init_fail_run.check_status = CheckStatus.skipped
                        await s.commit()
                return
            logger.info(
                f"job_id={job_id} run_id={current_run_id} step=init_check "
                f"repo_initialized"
            )

        # Step 5: Backup
        job_timeout_hours: int | None = job.timeout_hours
        default_timeout: int = cast(
            int, settings_dict.get("default_job_timeout_hours", 24)
        )
        timeout_seconds: int = (job_timeout_hours or default_timeout) * 3600
        logger.info(
            f"job_id={job_id} run_id={current_run_id} step=backup_execution "
            f"source_path={source_path} timeout_seconds={timeout_seconds}"
        )
        backup_kwargs: Dict[str, Any] = {
            k: getattr(job, k)
            for k in [
                "exclude_patterns",
                "exclude_caches",
                "exclude_if_present",
                "one_file_system",
                "no_scan",
                "tags",
                "compression",
                "pack_size",
                "read_concurrency",
            ]
            if getattr(job, k) is not None
        }

        backup_success: bool = False
        # rc=3 means restic ran but some files couldn't be read; the snapshot
        # was still saved. We treat it as success-with-warnings: prune + sync
        # still run, but the final status is `warning` (not `success`).
        backup_warning: bool = False
        summary: Optional[Dict[str, Any]] = None
        stdout: str = ""

        try:
            rc, stdout, stderr, summary = await restic.restic_backup(
                repo_path,
                job_password,
                source_path,
                timeout_seconds,
                **backup_kwargs,
            )
            if rc == 0:
                backup_success = True
                logger.info(
                    f"job_id={job_id} run_id={current_run_id} step=backup_execution "
                    f"status=success"
                )
            elif rc == 3:
                backup_success = True
                backup_warning = True
                failed_items = _extract_failed_items(stdout)
                logger.warning(
                    f"job_id={job_id} run_id={current_run_id} step=backup_execution "
                    f"status=warning rc=3 failed_items={len(failed_items)}"
                )
                async with factory() as s:
                    warn_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if warn_run:
                        warn_run.error_output = (
                            "Partial backup: some files could not be read.\n"
                            + "\n".join(failed_items)
                        )
                        await s.commit()
            else:
                logger.error(
                    f"job_id={job_id} run_id={current_run_id} step=backup_execution "
                    f"status=failed rc={rc}"
                )
                async with factory() as s:
                    backup_fail_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if backup_fail_run:
                        backup_fail_run.status = RunStatus.failed
                        backup_fail_run.error_output = stderr
                        await s.commit()
        except asyncio.TimeoutError:
            hours: int = job_timeout_hours or default_timeout
            logger.error(
                f"job_id={job_id} run_id={current_run_id} step=backup_execution "
                f"error=timeout timeout_hours={hours}"
            )
            async with factory() as s:
                timeout_run: BackupRun | None = await s.get(
                    BackupRun, str(current_run_id)
                )
                if timeout_run:
                    timeout_error_msg: str = f"Backup timed out after {hours} hours"
                    timeout_run.status = RunStatus.failed
                    timeout_run.error_output = timeout_error_msg
                    await s.commit()

        # Step 6 & 7: Parse output and update stats (only if backup succeeded)
        if backup_success:
            async with factory() as s:
                stats_run: BackupRun | None = await s.get(
                    BackupRun, str(current_run_id)
                )
                if stats_run:
                    if summary:
                        files_new: int | None = summary.get("files_new")
                        files_changed: int | None = summary.get("files_changed")
                        files_unmodified: int | None = summary.get("files_unmodified")
                        dirs_new: int | None = summary.get("dirs_new")
                        dirs_changed: int | None = summary.get("dirs_changed")
                        dirs_unmodified: int | None = summary.get("dirs_unmodified")
                        data_added: int | None = summary.get("data_added")
                        data_added_packed: int | None = summary.get("data_added_packed")
                        total_bytes_proc: int | None = summary.get(
                            "total_bytes_processed"
                        )
                        snap_id: str | None = summary.get("snapshot_id")

                        stats_run.files_new = files_new
                        stats_run.files_changed = files_changed
                        stats_run.files_unmodified = files_unmodified
                        stats_run.dirs_new = dirs_new
                        stats_run.dirs_changed = dirs_changed
                        stats_run.dirs_unmodified = dirs_unmodified
                        stats_run.data_added_bytes = data_added
                        stats_run.data_added_packed_bytes = data_added_packed
                        stats_run.total_bytes_processed = total_bytes_proc
                        stats_run.snapshot_id = snap_id
                    stats_run.backup_output = stdout
                    await s.commit()

            # Step 8: Prune (only if backup succeeded)
            logger.debug(f"job_id={job_id} run_id={current_run_id} step=prune")
            retention_kwargs: Dict[str, Any] = {
                k: getattr(job, k)
                for k in [
                    "retain_keep_last",
                    "retain_keep_hourly",
                    "retain_keep_daily",
                    "retain_keep_weekly",
                    "retain_keep_monthly",
                    "retain_keep_yearly",
                    "retain_keep_within",
                    "retain_keep_within_hourly",
                    "retain_keep_within_daily",
                    "retain_keep_within_weekly",
                    "retain_keep_within_monthly",
                    "retain_keep_within_yearly",
                ]
                if getattr(job, k) is not None
            }

            prune_err: str = ""
            if retention_kwargs:
                logger.info(
                    f"job_id={job_id} run_id={current_run_id} step=prune "
                    f"mode=forget_prune"
                )
                rc, _, prune_err = await restic.restic_forget_prune(
                    repo_path,
                    job_password,
                    timeout_seconds,
                    **retention_kwargs,
                )
            else:
                logger.info(
                    f"job_id={job_id} run_id={current_run_id} step=prune "
                    f"mode=standard_prune"
                )
                rc, _, prune_err = await restic.restic_prune(
                    repo_path, job_password, timeout_seconds
                )

            async with factory() as s:
                prune_run: BackupRun | None = await s.get(
                    BackupRun, str(current_run_id)
                )
                if prune_run:
                    if rc == 0:
                        prune_run.prune_status = PruneStatus.passed
                        logger.info(
                            f"job_id={job_id} run_id={current_run_id} step=prune "
                            f"status=passed"
                        )
                    else:
                        prune_run.prune_status = PruneStatus.failed
                        prune_run.prune_error_output = prune_err
                        logger.warning(
                            f"job_id={job_id} run_id={current_run_id} step=prune "
                            f"status=failed error=pruning_failed"
                        )
                    await s.commit()

            # Step 9: Reconcile snapshots
            rc, snapshots, _ = await restic.restic_snapshots(repo_path, job_password)
            if rc == 0:
                async with factory() as s:
                    result = await s.execute(
                        select(Snapshot).where(Snapshot.job_id == str(job_id))
                    )
                    existing_snaps = result.scalars().all()
                    snap_ids: Set[str] = {snap["id"] for snap in snapshots}

                    # Delete pruned snapshots
                    for snap in existing_snaps:
                        if snap.snapshot_id not in snap_ids:
                            await s.delete(snap)

                    # Upsert snapshots
                    for snap in snapshots:
                        snap_time_str: Optional[str] = snap.get("time")
                        # snapshot_time and hostname/paths are NOT NULL in the
                        # DB; skip any malformed snapshot dict missing them.
                        if not snap_time_str:
                            continue
                        snap_time: datetime = datetime.fromisoformat(
                            snap_time_str.replace("Z", "+00:00")
                        )
                        hostname: str = snap.get("hostname") or ""
                        paths: list[str] = snap.get("paths") or []
                        tags: list[str] | None = snap.get("tags")
                        size_bytes: int | None = snap.get("total_size")

                        snap_result = await s.execute(
                            select(Snapshot).where(Snapshot.snapshot_id == snap["id"])
                        )
                        existing: Snapshot | None = snap_result.scalars().first()

                        if existing:
                            existing.snapshot_time = snap_time
                            existing.hostname = hostname
                            existing.paths = paths
                            existing.tags = tags
                            existing.size_bytes = size_bytes
                        else:
                            new_snap: Snapshot = Snapshot(
                                id=str(uuid.uuid4()),
                                job_id=str(job_id),
                                snapshot_id=snap["id"],
                                snapshot_time=snap_time,
                                hostname=hostname,
                                paths=paths,
                                tags=tags,
                                size_bytes=size_bytes,
                                captured_at=datetime.now(timezone.utc),
                            )
                            if summary and snap["id"] == summary.get("snapshot_id"):
                                new_snap.run_id = str(current_run_id)
                            s.add(new_snap)

                    await s.commit()
        else:
            # If backup failed, skip steps 8-9 and mark them as skipped
            async with factory() as s:
                skip_run: BackupRun | None = await s.get(BackupRun, str(current_run_id))
                if skip_run:
                    prune_status: Any = skip_run.prune_status
                    if not prune_status:
                        skip_run.prune_status = PruneStatus.skipped
                    check_status: Any = skip_run.check_status
                    if not check_status:
                        skip_run.check_status = CheckStatus.skipped
                    await s.commit()

        # Step 10: Finalize run
        now: datetime = datetime.now(timezone.utc)
        final_run: BackupRun | None = None
        async with factory() as s:
            final_run = await s.get(BackupRun, str(current_run_id))
            if final_run:
                final_status: Any = final_run.status
                if final_status == RunStatus.running:
                    final_run.status = (
                        RunStatus.warning if backup_warning else RunStatus.success
                    )
                final_run.finished_at = now
                now_naive: datetime = now.replace(tzinfo=None)
                duration_secs: int = int(
                    (now_naive - final_run.started_at).total_seconds()
                )
                final_run.duration_seconds = duration_secs
                final_check_status: Any = final_run.check_status
                if not final_check_status:
                    final_run.check_status = CheckStatus.skipped
                await s.commit()

        # Step 11: Completion notification
        ntfy_topic_send: str | None = cast(str | None, settings_dict.get("ntfy_topic"))
        if ntfy_topic_send and final_run:
            final_status_notify: Any = final_run.status
            if final_status_notify == RunStatus.success and settings_dict.get(
                "notify_on_success"
            ):
                msg: str = (
                    f"Duration: {final_run.duration_seconds}s, "
                    f"Files: {final_run.files_changed}"
                )
                await send_notification(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic_send,
                    f"Backup succeeded: {job.name}",
                    msg,
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )
            elif final_status_notify == RunStatus.warning and settings_dict.get(
                "notify_on_warning"
            ):
                warn_msg: str = (
                    f"Duration: {final_run.duration_seconds}s — some files "
                    f"could not be read; snapshot was still saved."
                )
                await send_notification(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic_send,
                    f"Backup completed with warnings: {job.name}",
                    warn_msg,
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )
            elif final_status_notify == RunStatus.failed and settings_dict.get(
                "notify_on_failure"
            ):
                error_output: str | None = final_run.error_output
                error_excerpt: str = (
                    (error_output or "")[:200] if error_output else "Unknown error"
                )
                await send_notification(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic_send,
                    f"Backup failed: {job.name}",
                    error_excerpt,
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )

        # Step 12: Integrity check
        job_check_enabled: bool = job.check_enabled
        # When check_enabled=True, the JobCreate schema validator guarantees
        # check_mode is non-None; assertion keeps pyright happy and surfaces a
        # clear error if the invariant is ever violated.
        if job_check_enabled and final_run and job.check_mode is not None:
            final_status_check: Any = final_run.status
            if final_status_check == RunStatus.success:
                job_check_mode: str = job.check_mode.value
                job_check_percent: int | None = job.check_subset_percent
                logger.info(
                    f"job_id={job_id} run_id={current_run_id} step=integrity_check "
                    f"mode={job_check_mode} enabled=true"
                )
                if settings_dict.get("notify_on_verification"):
                    await send_notification(
                        cast(str | None, settings_dict.get("ntfy_server_url")),
                        cast(str | None, settings_dict.get("ntfy_topic")),
                        f"Verification started: {job.name}",
                        "Running integrity check...",
                        token=cast(str | None, settings_dict.get("ntfy_token")),
                    )

                job_check_timeout: int | None = job.check_timeout_hours
                check_timeout: int = (job_check_timeout or default_timeout) * 3600
                rc, _, check_err = await restic.restic_check(
                    repo_path,
                    job_password,
                    job_check_mode,
                    job_check_percent,
                    check_timeout,
                )

                async with factory() as s:
                    check_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if check_run:
                        if rc == 0:
                            check_run.check_status = CheckStatus.passed
                            logger.info(
                                f"job_id={job_id} run_id={current_run_id} "
                                f"step=integrity_check status=passed"
                            )
                        else:
                            check_run.check_status = CheckStatus.failed
                            check_run.check_error_output = check_err
                            logger.warning(
                                f"job_id={job_id} run_id={current_run_id} "
                                f"step=integrity_check status=failed error=check_failed"
                            )
                        await s.commit()

                if settings_dict.get("notify_on_verification"):
                    status_str: str = "passed" if rc == 0 else "failed"
                    await send_notification(
                        cast(str | None, settings_dict.get("ntfy_server_url")),
                        cast(str | None, settings_dict.get("ntfy_topic")),
                        f"Verification {status_str}: {job.name}",
                        f"Check status: {status_str}",
                        token=cast(str | None, settings_dict.get("ntfy_token")),
                    )
        else:
            if not job_check_enabled:
                logger.debug(
                    f"job_id={job_id} run_id={current_run_id} step=integrity_check "
                    f"enabled=false skipped"
                )

    finally:
        # Cleanup: remove job from active set
        active_jobs.discard(job_id)
        if current_run_id is not None:
            await _trim_run_history(factory, str(job_id))
            logger.info(f"job_id={job_id} run_id={current_run_id} backup_completed")


async def _trim_run_history(
    factory: async_sessionmaker[AsyncSession], job_id: str
) -> None:
    """Delete the oldest backup_runs rows beyond AppSettings.keep_last_runs.

    Snapshots in the restic repo are untouched — this only bounds the row
    count in the `backup_runs` table so it doesn't grow forever.
    """
    from sqlalchemy import select

    async with factory() as s:
        settings: AppSettings | None = await s.get(AppSettings, 1)
        keep_n: int = settings.keep_last_runs if settings else 100

        ids_newest_first = (
            (
                await s.execute(
                    select(BackupRun.id)
                    .where(BackupRun.job_id == job_id)
                    .order_by(BackupRun.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if len(ids_newest_first) <= keep_n:
            return
        excess = ids_newest_first[keep_n:]
        for old_id in excess:
            old = await s.get(BackupRun, old_id)
            if old is not None:
                await s.delete(old)
        await s.commit()
        logger.info(f"job_id={job_id} trimmed_runs deleted={len(excess)} kept={keep_n}")
