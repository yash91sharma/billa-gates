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
    RunReason,
    RunStatus,
    TriggeredBy,
)
from app.services import restic, snapshot_listing
from app.services.notifications import send_notification

logger = get_logger(__name__)

active_jobs: Set[uuid.UUID] = set()
job_locks: Dict[uuid.UUID, asyncio.Lock] = {}


async def trigger_run(
    job_id: uuid.UUID,
    triggered_by: TriggeredBy = TriggeredBy.scheduler,
) -> Optional[str]:
    """Unified entry point for starting a backup run.

    Acquires the per-job lock unconditionally and, under it, performs the
    overlap check and creates either a `running` row or a `skipped` row
    (`reason=overlapping_run`). The actual backup pipeline runs in a
    fire-and-forget background task once the row is committed.

    All triggers — manual API call, scheduled APScheduler fire — must go
    through this function so that a manual click and a scheduled tick can
    never both squeeze past the check and produce two concurrent restic
    processes against the same repository (C2).

    Returns the run id (skipped or running). Returns None only if the job
    does not exist.
    """
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))
    if not job:
        logger.warning(f"trigger_run job_id={job_id} not_found")
        return None

    lock: asyncio.Lock = job_locks.setdefault(job_id, asyncio.Lock())
    now = datetime.now(timezone.utc)

    async with lock:
        # Under the lock, the in-memory active_jobs set and any DB row with
        # status=running are equally authoritative for "is a run live?".
        async with factory() as s:
            result = await s.execute(
                select(BackupRun).where(
                    BackupRun.job_id == str(job_id),
                    BackupRun.status == RunStatus.running,
                )
            )
            running_row: BackupRun | None = result.scalars().first()

            if running_row is not None or job_id in active_jobs:
                skipped = BackupRun(
                    id=str(uuid.uuid4()),
                    job_id=str(job_id),
                    status=RunStatus.skipped,
                    reason=RunReason.overlapping_run,
                    started_at=now,
                    finished_at=now,
                    triggered_by=triggered_by,
                    prune_status=PruneStatus.skipped,
                    check_status=CheckStatus.skipped,
                )
                s.add(skipped)
                await s.commit()
                logger.info(
                    f"trigger_run job_id={job_id} run_id={skipped.id} "
                    f"triggered_by={triggered_by.value} status=skipped "
                    f"reason=overlapping_run"
                )
                return skipped.id

            running = BackupRun(
                id=str(uuid.uuid4()),
                job_id=str(job_id),
                status=RunStatus.running,
                started_at=now,
                triggered_by=triggered_by,
            )
            s.add(running)
            await s.commit()
            run_id_str = running.id
            active_jobs.add(job_id)
            logger.info(
                f"trigger_run job_id={job_id} run_id={run_id_str} "
                f"triggered_by={triggered_by.value} status=dispatched"
            )

    asyncio.create_task(_run_with_cleanup(job_id, uuid.UUID(run_id_str)))
    return run_id_str


async def _run_with_cleanup(job_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Wrap run_backup so active_jobs is always discarded, even if the
    pipeline raises. Keeps the concurrency state in trigger_run's hands so
    the run_backup pipeline can stay focused on backup mechanics."""
    try:
        await run_backup(job_id, run_id)
    finally:
        active_jobs.discard(job_id)


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


async def run_backup(job_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """12-step backup lifecycle orchestration.

    Always invoked by :func:`trigger_run` with a pre-created `running`
    BackupRun row. Concurrency gating (per-job lock, overlap check,
    active_jobs membership) lives in :func:`trigger_run`; this function
    runs the backup pipeline and finalizes the row.
    """
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

    current_run_id: uuid.UUID = run_id
    try:
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
                    "auto_unlock": settings_obj.auto_unlock,
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

        # Step 4.5: Auto-unlock — clear any stale lock left behind by an
        # abrupt termination (OOM, container kill, host reboot). Failure is
        # logged but non-fatal: a freshly initialized repo legitimately has
        # no lock to remove, and any deeper problem (missing restic binary,
        # network down) will be surfaced by the backup step that follows.
        if settings_dict.get("auto_unlock", True):
            try:
                unlock_rc, _, unlock_err = await restic.restic_unlock(
                    repo_path, job_password
                )
                if unlock_rc == 0:
                    logger.info(
                        f"job_id={job_id} run_id={current_run_id} step=auto_unlock "
                        f"status=ok"
                    )
                else:
                    logger.warning(
                        f"job_id={job_id} run_id={current_run_id} step=auto_unlock "
                        f"status=nonzero rc={unlock_rc} error={unlock_err!r}"
                    )
            except Exception as exc:
                logger.warning(
                    f"job_id={job_id} run_id={current_run_id} step=auto_unlock "
                    f"status=exception error={exc!r}"
                )

        # Step 5: Backup
        job_timeout_hours: int | None = job.timeout_hours
        default_timeout: int = cast(
            int, settings_dict.get("default_job_timeout_hours", 24)
        )
        timeout_seconds: int = (job_timeout_hours or default_timeout) * 3600

        # Look up the latest snapshot for this job so restic can do an
        # incremental rescan instead of re-reading every source file. Without
        # an explicit --parent, any change to host or paths (e.g.
        # source_subpath edit) makes restic treat the next backup as a fresh
        # first run (gaps.md C5). Returns None on genuine first run.
        parent_snapshot_id: Optional[str] = await restic.restic_latest_snapshot_id(
            repo_path, job_password, job_id=str(job_id)
        )
        logger.info(
            f"job_id={job_id} run_id={current_run_id} step=parent_lookup "
            f"parent_snapshot_id={parent_snapshot_id}"
        )

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
                job_id=str(job_id),
                parent_snapshot_id=parent_snapshot_id,
                **backup_kwargs,
            )
            # Exit code 11 = restic failed to acquire the repo lock. The most
            # common cause is a stale lock left by a previous abrupt
            # termination. Clear it and retry exactly once — never loop, or
            # a genuinely-contended repo would hang the runner forever.
            if rc == 11:
                logger.warning(
                    f"job_id={job_id} run_id={current_run_id} step=backup_execution "
                    f"rc=11 stale_lock_suspected attempting_unlock_and_retry"
                )
                try:
                    await restic.restic_unlock(repo_path, job_password)
                except Exception as exc:
                    logger.warning(
                        f"job_id={job_id} run_id={current_run_id} step=lock_retry "
                        f"unlock_exception error={exc!r}"
                    )
                rc, stdout, stderr, summary = await restic.restic_backup(
                    repo_path,
                    job_password,
                    source_path,
                    timeout_seconds,
                    job_id=str(job_id),
                    parent_snapshot_id=parent_snapshot_id,
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
                    job_id=str(job_id),
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

            # Step 9 (snapshot DB reconcile) removed — restic is the source of
            # truth and the snapshot listing route queries it on demand
            # (gaps.md C4-Alt). BackupRun.snapshot_id, set above from the
            # backup summary, is enough to link this run to the snapshot it
            # produced. Invalidate the snapshot listing cache so the UI sees
            # the new snapshot immediately rather than waiting for the TTL.
            snapshot_listing._clear_cache()
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
        # active_jobs cleanup is handled by _run_with_cleanup so the lifecycle
        # owner (trigger_run) holds full responsibility for the in-memory
        # state. run_backup focuses on the backup pipeline + history trim.
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
