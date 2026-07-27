import asyncio
import json
import os
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import fs
from app.core.logging import get_logger, log_call
from app.core.tasks import create_tracked_task
from app.db.database import engine
from app.db.models import (
    AppSettings,
    BackupJob,
    BackupRun,
    CheckStatus,
    PruneStatus,
    RunKind,
    RunReason,
    RunStatus,
    TriggeredBy,
)
from app.services import process_registry, repository, restic, snapshot_listing
from app.services.notifications import send_notification

logger = get_logger(__name__)


async def _try_notify(*args: Any, **kwargs: Any) -> None:
    """Wrapper around send_notification so a transient ntfy/network failure
    is logged but never crashes the backup pipeline. Notifications are a
    side-effect — a broken ntfy server must not strand a run row at
    status=running, which would otherwise lock the job out of every future
    trigger via trigger_run's overlap check.
    """
    try:
        await send_notification(*args, **kwargs)
    except Exception as exc:
        logger.warning(f"send_notification failed (non-fatal): {exc!r}")


active_jobs: Set[uuid.UUID] = set()
job_locks: Dict[uuid.UUID, asyncio.Lock] = {}

SOURCES_ROOT: str = "/sources"


@log_call
def build_source_path(source_label: str, source_subpath: Optional[str] = None) -> str:
    """Return the path a run actually reads: ``/sources/<label>[/<subpath>]``.

    Single source of truth for the effective backup source, so the path the
    sentinel is checked at can never drift from the path handed to restic.
    """
    if source_subpath:
        return os.path.join(SOURCES_ROOT, source_label, source_subpath)
    return os.path.join(SOURCES_ROOT, source_label)


@log_call
def check_mount_file_exists(
    source_label: str, source_subpath: Optional[str] = None
) -> bool:
    """Verify that the effective backup source contains the sentinel check file.

    Checks for .billa_gates_check at the root of the directory that is actually
    backed up — the mount root, or the subfolder when the job sets
    `source_subpath`.

    Checking only the mount root is not enough for a subpath job: the sentinel
    would prove the mount is live while saying nothing about the subfolder. An
    inner share that dropped, or a folder that was moved, leaves an empty
    directory that restic turns into a 0-file snapshot — and `restic forget`
    then prunes the real history against it.
    """
    check_file_path = os.path.join(
        build_source_path(source_label, source_subpath), ".billa_gates_check"
    )
    return os.path.exists(check_file_path)


# The job fields that become `restic backup` / `restic forget` inputs. Kept as
# named tuples (rather than inline lists at the call sites) so the command
# preview on the Job detail page is assembled from the same field set the
# pipeline passes to restic — a field added to one and not the other would
# make the page show a backup that isn't the one that runs.
BACKUP_OPTION_FIELDS: Tuple[str, ...] = (
    "exclude_patterns",
    "exclude_caches",
    "exclude_if_present",
    "one_file_system",
    "no_scan",
    "tags",
    "compression",
    "pack_size",
    "read_concurrency",
)

RETENTION_FIELDS: Tuple[str, ...] = (
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
)


@log_call
def build_backup_kwargs(job: BackupJob) -> Dict[str, Any]:
    """The `restic backup` option kwargs this job produces.

    Unset (None) fields are dropped so restic is never handed an empty flag.
    """
    return {
        field: getattr(job, field)
        for field in BACKUP_OPTION_FIELDS
        if getattr(job, field) is not None
    }


@log_call
def build_retention_kwargs(job: BackupJob) -> Dict[str, Any]:
    """The `restic forget` retention kwargs this job produces.

    An empty dict means no retention is configured — the pipeline then skips
    `restic forget` entirely, since it would be a no-op.
    """
    return {
        field: getattr(job, field)
        for field in RETENTION_FIELDS
        if getattr(job, field) is not None
    }


DESTINATIONS_ROOT: str = "/destinations"


@log_call
def check_destination_mount_file_exists(destination_label: str) -> bool:
    """Verify that the destination mount contains the required sentinel check file.

    Checks for .billa_gates_check at the root of the volume mount.
    """
    check_file_path = os.path.join(
        DESTINATIONS_ROOT, destination_label, ".billa_gates_check"
    )
    return os.path.exists(check_file_path)


# Restic exit codes (stable contract since 0.17). Branching on these is the
# only reliable way to classify a failure — stderr message wording is not a
# contract and has changed between restic releases (gaps.md H5). Defined in
# `repository`, which also provisions repos on those same codes; re-exported
# here so the pipeline reads naturally and existing patches keep working.
RESTIC_RC_REPO_NOT_FOUND: int = repository.RESTIC_RC_REPO_NOT_FOUND
RESTIC_RC_LOCK_FAILED: int = repository.RESTIC_RC_LOCK_FAILED
RESTIC_RC_WRONG_PASSWORD: int = repository.RESTIC_RC_WRONG_PASSWORD


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

    create_tracked_task(_run_with_cleanup(job_id, uuid.UUID(run_id_str)))
    return run_id_str


async def _run_with_cleanup(job_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Wrap run_backup so active_jobs is always discarded, even if the
    pipeline raises. Keeps the concurrency state in trigger_run's hands so
    the run_backup pipeline can stay focused on backup mechanics."""
    try:
        await run_backup(job_id, run_id)
    finally:
        active_jobs.discard(job_id)


async def _prune_with_cleanup(job_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Mirror of _run_with_cleanup for prune runs — keeps the active_jobs
    cleanup invariant identical between backup and prune dispatch paths."""
    try:
        await run_prune(job_id, run_id)
    finally:
        active_jobs.discard(job_id)


async def trigger_prune(
    job_id: uuid.UUID,
    triggered_by: TriggeredBy = TriggeredBy.manual,
) -> Optional[str]:
    """Unified entry point for starting a standalone `restic prune` run.

    Prune is decoupled from the backup pipeline (gaps.md H1) because it is
    the heaviest restic operation; bundling it into every backup window made
    backups unpredictably long. Sharing the per-job lock + active_jobs set
    with :func:`trigger_run` means prune and backup serialize on the same
    repo — restic cannot tolerate concurrent writers.

    Creates a `kind=prune` BackupRun row (running or skipped/overlapping)
    and fires :func:`run_prune` as a background task. Returns the run id,
    or None if the job does not exist.
    """
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))
    if not job:
        logger.warning(f"trigger_prune job_id={job_id} not_found")
        return None

    lock: asyncio.Lock = job_locks.setdefault(job_id, asyncio.Lock())
    now = datetime.now(timezone.utc)

    async with lock:
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
                    kind=RunKind.prune,
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
                    f"trigger_prune job_id={job_id} run_id={skipped.id} "
                    f"triggered_by={triggered_by.value} status=skipped "
                    f"reason=overlapping_run"
                )
                return skipped.id

            running = BackupRun(
                id=str(uuid.uuid4()),
                job_id=str(job_id),
                kind=RunKind.prune,
                status=RunStatus.running,
                started_at=now,
                triggered_by=triggered_by,
            )
            s.add(running)
            await s.commit()
            run_id_str = running.id
            active_jobs.add(job_id)
            logger.info(
                f"trigger_prune job_id={job_id} run_id={run_id_str} "
                f"triggered_by={triggered_by.value} status=dispatched"
            )

    create_tracked_task(_prune_with_cleanup(job_id, uuid.UUID(run_id_str)))
    return run_id_str


async def _finalize_if_canceled(
    factory: async_sessionmaker[AsyncSession],
    job_name: str,
    run_id: uuid.UUID,
    *,
    kind_label: str,
) -> bool:
    """Finalize a prune/check run row as canceled if the cancel flag is set.

    Counterpart of run_backup's inner ``_was_canceled`` for the standalone
    prune and check pipelines: the cancel endpoint sets the flag and SIGTERMs
    the subprocess for *any* running run, so these pipelines must record the
    user's stop click as ``canceled/user_canceled`` — not as a failed run
    with the terminated process's stderr as the error. Clears the flag so
    the registry set stays bounded. Returns True when the row was finalized;
    the caller must then return immediately.
    """
    if not process_registry.is_canceled(run_id):
        return False

    now: datetime = datetime.now(timezone.utc)
    duration: int = 0
    async with factory() as s:
        run: BackupRun | None = await s.get(BackupRun, str(run_id))
        if run is not None:
            run.status = RunStatus.canceled
            run.reason = RunReason.user_canceled
            run.finished_at = now
            run.duration_seconds = int(
                (now.replace(tzinfo=None) - run.started_at).total_seconds()
            )
            if not run.prune_status:
                run.prune_status = PruneStatus.skipped
            if not run.check_status:
                run.check_status = CheckStatus.skipped
            if not run.error_output:
                run.error_output = "Canceled by user."
            duration = run.duration_seconds or 0
            await s.commit()

    # Mirror the backup pipeline's cancel notification (notify_on_warning).
    async with factory() as s:
        settings_obj: AppSettings | None = await s.get(AppSettings, 1)
    if settings_obj and settings_obj.notify_on_warning and settings_obj.ntfy_topic:
        await _try_notify(
            settings_obj.ntfy_server_url,
            settings_obj.ntfy_topic,
            f"{kind_label} canceled: {job_name}",
            f"Duration: {duration}s — canceled by user.",
            token=settings_obj.ntfy_token,
        )

    process_registry.clear_canceled(run_id)
    logger.info(f"run_id={run_id} status=canceled reason=user_canceled")
    return True


async def _fail_run_missing_destination_sentinel(
    factory: async_sessionmaker[AsyncSession],
    job: BackupJob,
    run_id: uuid.UUID,
    *,
    kind_label: str,
) -> None:
    """Finalize a prune/check run as failed because the destination
    `.billa_gates_check` sentinel is absent, and fire a notify_on_failure alert.

    A prune or check only touches the destination repository, so the
    destination sentinel is the "this drive is really mounted" proof these
    pipelines need before spawning restic. An empty mountpoint left behind by a
    detached drive otherwise looks like a healthy directory, and restic would
    silently operate against nothing. Mirrors run_backup's mount-check failure
    path (clear error naming the missing file + notify_on_failure).
    """
    error_msg = (
        f"Destination mount check failed: '.billa_gates_check' file was not "
        f"found at the root of the destination mount "
        f"'/destinations/{job.destination_label}' (or the mount did not respond "
        f"within the probe timeout)."
    )
    logger.error(
        f"job_id={job.id} run_id={run_id} step=verify_mount "
        f"error=destination_mount_check_failed "
        f"destination_label={job.destination_label}"
    )
    now = datetime.now(timezone.utc)
    async with factory() as s:
        run: BackupRun | None = await s.get(BackupRun, str(run_id))
        if run is not None:
            run.status = RunStatus.failed
            run.error_output = error_msg
            run.finished_at = now
            run.duration_seconds = int(
                (now.replace(tzinfo=None) - run.started_at).total_seconds()
            )
            if not run.prune_status:
                run.prune_status = PruneStatus.skipped
            if not run.check_status:
                run.check_status = CheckStatus.skipped
            await s.commit()

    async with factory() as s:
        settings_obj: AppSettings | None = await s.get(AppSettings, 1)
    if settings_obj and settings_obj.notify_on_failure and settings_obj.ntfy_topic:
        await _try_notify(
            settings_obj.ntfy_server_url,
            settings_obj.ntfy_topic,
            f"{kind_label} failed: {job.name}",
            error_msg[:200],
            token=settings_obj.ntfy_token,
        )


async def run_prune(job_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Execute `restic prune` for a job's repo and finalize the run row.

    Prune is the heaviest restic operation: it scans every pack file in the
    repo, rewrites packs to drop unreferenced blobs, and updates the index.
    It is invoked here as a standalone step — never from the backup
    pipeline (gaps.md H1).

    Concurrency gating (per-job lock, overlap check, active_jobs membership)
    lives in :func:`trigger_prune`; this function runs prune and updates
    the row. Failures land in prune_error_output; the run status reflects
    the prune outcome (success / failed).
    """
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))

    if not job:
        logger.warning(f"run_prune job_id={job_id} not found in database")
        return

    logger.info(f"job_id={job_id} run_id={run_id} prune_started")

    try:
        # Load default timeout once so a manual prune of a huge repo doesn't
        # need a per-job knob — falls back to the global setting.
        async with factory() as s:
            settings_obj: AppSettings | None = await s.get(AppSettings, 1)
            default_timeout: int = (
                settings_obj.default_job_timeout_hours if settings_obj else 24
            )

        # Per-job timeout_hours applies the same to prune as it does to
        # backup — operators tune one knob, not two. The pipeline still has
        # the 1-hr-fallback safety net via _terminate_then_kill inside the
        # restic wrapper.
        prune_timeout: int = (job.timeout_hours or default_timeout) * 3600
        repo_path: str = repository.build_repo_path(job.destination_label, job.name)

        # A cancel that lands before the subprocess spawns must stop the
        # (possibly hours-long) prune from starting at all.
        if await _finalize_if_canceled(factory, job.name, run_id, kind_label="Prune"):
            return

        # Verify the destination sentinel before touching restic: an empty
        # mountpoint left by a detached backup drive passes an isdir check but
        # is not the real repo. Probed on a worker thread so a hung mount can't
        # freeze the event loop.
        if not await fs.run_probe(
            check_destination_mount_file_exists, job.destination_label, default=False
        ):
            await _fail_run_missing_destination_sentinel(
                factory, job, run_id, kind_label="Prune"
            )
            return

        rc, _, prune_err = await restic.restic_prune(
            repo_path, job.restic_password, prune_timeout, run_id=run_id
        )
        if await _finalize_if_canceled(factory, job.name, run_id, kind_label="Prune"):
            return

        now: datetime = datetime.now(timezone.utc)
        async with factory() as s:
            final_run: BackupRun | None = await s.get(BackupRun, str(run_id))
            if final_run:
                if rc == 0:
                    final_run.status = RunStatus.success
                    final_run.prune_status = PruneStatus.passed
                    logger.info(
                        f"job_id={job_id} run_id={run_id} step=prune status=passed"
                    )
                else:
                    final_run.status = RunStatus.failed
                    final_run.prune_status = PruneStatus.failed
                    final_run.prune_error_output = prune_err
                    logger.warning(
                        f"job_id={job_id} run_id={run_id} step=prune "
                        f"status=failed rc={rc}"
                    )
                final_run.finished_at = now
                now_naive: datetime = now.replace(tzinfo=None)
                final_run.duration_seconds = int(
                    (now_naive - final_run.started_at).total_seconds()
                )
                # Prune runs don't drive a check; keep that column tidy so
                # the UI's existing "check_status missing" polling hook
                # doesn't wait forever on rows that will never have one.
                final_run.check_status = CheckStatus.skipped
                await s.commit()

    except Exception as exc:
        # Mirror of run_backup's top-level safety net. Without this, a crash
        # mid-prune (subprocess error, DB lock, network blip) would leave the
        # prune row stuck at status=running, and trigger_prune / trigger_run
        # would skip every future trigger as overlapping_run — locking the
        # job out of both prune and backup.
        logger.exception(
            f"job_id={job_id} run_id={run_id} prune_runner_crashed error={exc!r}"
        )
        try:
            crash_now: datetime = datetime.now(timezone.utc)
            async with factory() as s:
                crash_run: BackupRun | None = await s.get(BackupRun, str(run_id))
                if crash_run is not None:
                    crash_status: Any = crash_run.status
                    if crash_status == RunStatus.running:
                        tb: str = traceback.format_exc()
                        crash_run.status = RunStatus.failed
                        crash_run.prune_status = PruneStatus.failed
                        crash_run.prune_error_output = (
                            f"Prune runner crashed: {exc!r}\n\n{tb}"
                        )
                        crash_run.finished_at = crash_now
                        crash_started: datetime = crash_run.started_at
                        crash_run.duration_seconds = int(
                            (
                                crash_now.replace(tzinfo=None) - crash_started
                            ).total_seconds()
                        )
                        crash_run.check_status = CheckStatus.skipped
                        await s.commit()
        except Exception as recovery_exc:
            logger.error(
                f"job_id={job_id} run_id={run_id} "
                f"crash_recovery_failed error={recovery_exc!r}"
            )
    finally:
        await _trim_run_history(factory, str(job_id))
        logger.info(f"job_id={job_id} run_id={run_id} prune_completed")


async def _check_with_cleanup(
    job_id: uuid.UUID,
    run_id: uuid.UUID,
    check_mode: str,
    subset_percent: Optional[int],
    timeout_hours: Optional[int],
) -> None:
    """Mirror of _run_with_cleanup for check runs — keeps active_jobs identical."""
    try:
        await run_check(job_id, run_id, check_mode, subset_percent, timeout_hours)
    finally:
        active_jobs.discard(job_id)


async def trigger_check(
    job_id: uuid.UUID,
    triggered_by: TriggeredBy = TriggeredBy.manual,
    check_mode: str = "structural",
    subset_percent: Optional[int] = None,
    timeout_hours: Optional[int] = None,
) -> Optional[str]:
    """Unified entry point for starting a standalone `restic check` run.

    Decoupled from backup pipeline to run manually or separately. Shares locks and
    active_jobs to serialize operations on the repository.
    """
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))
    if not job:
        logger.warning(f"trigger_check job_id={job_id} not_found")
        return None

    lock: asyncio.Lock = job_locks.setdefault(job_id, asyncio.Lock())
    now = datetime.now(timezone.utc)

    async with lock:
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
                    kind=RunKind.check,
                    status=RunStatus.skipped,
                    reason=RunReason.overlapping_run,
                    started_at=now,
                    finished_at=now,
                    prune_status=PruneStatus.skipped,
                    check_status=CheckStatus.skipped,
                    triggered_by=triggered_by,
                )
                s.add(skipped)
                await s.commit()
                logger.info(
                    f"trigger_check job_id={job_id} run_id={skipped.id} "
                    f"triggered_by={triggered_by.value} status=skipped "
                    f"reason=overlapping_run"
                )
                return skipped.id

            running = BackupRun(
                id=str(uuid.uuid4()),
                job_id=str(job_id),
                kind=RunKind.check,
                status=RunStatus.running,
                started_at=now,
                triggered_by=triggered_by,
            )
            s.add(running)
            await s.commit()
            run_id_str = running.id
            active_jobs.add(job_id)
            logger.info(
                f"trigger_check job_id={job_id} run_id={run_id_str} "
                f"triggered_by={triggered_by.value} status=dispatched"
            )

    create_tracked_task(
        _check_with_cleanup(
            job_id,
            uuid.UUID(run_id_str),
            check_mode,
            subset_percent,
            timeout_hours,
        )
    )
    return run_id_str


async def run_check(
    job_id: uuid.UUID,
    run_id: uuid.UUID,
    check_mode: str,
    subset_percent: Optional[int],
    timeout_hours: Optional[int],
) -> None:
    """Execute `restic check` for a job's repo and finalize the run row."""
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))

    if not job:
        logger.warning(f"run_check job_id={job_id} not found in database")
        return

    logger.info(f"job_id={job_id} run_id={run_id} check_started mode={check_mode}")

    try:
        async with factory() as s:
            settings_obj: AppSettings | None = await s.get(AppSettings, 1)
            default_timeout: int = (
                settings_obj.default_job_timeout_hours if settings_obj else 24
            )
            ntfy_server_url = settings_obj.ntfy_server_url if settings_obj else None
            ntfy_topic = settings_obj.ntfy_topic if settings_obj else None
            ntfy_token = settings_obj.ntfy_token if settings_obj else None
            notify_on_verification = (
                settings_obj.notify_on_verification if settings_obj else False
            )

        hours = timeout_hours or job.check_timeout_hours or default_timeout
        check_timeout: int = hours * 3600
        repo_path: str = repository.build_repo_path(job.destination_label, job.name)

        # A cancel that lands before the subprocess spawns must stop the
        # check (and its start notification) from firing at all.
        if await _finalize_if_canceled(
            factory, job.name, run_id, kind_label="Verification"
        ):
            return

        # Verify the destination sentinel before touching restic (and before
        # any "started" notification): an empty mountpoint left by a detached
        # backup drive passes an isdir check but is not the real repo. Probed
        # on a worker thread so a hung mount can't freeze the event loop.
        if not await fs.run_probe(
            check_destination_mount_file_exists, job.destination_label, default=False
        ):
            await _fail_run_missing_destination_sentinel(
                factory, job, run_id, kind_label="Verification"
            )
            return

        if notify_on_verification and ntfy_topic:
            await _try_notify(
                ntfy_server_url,
                ntfy_topic,
                f"Verification started: {job.name}",
                f"Running integrity check (mode: {check_mode})...",
                token=ntfy_token,
            )

        rc, _, check_err = await restic.restic_check(
            repo_path,
            job.restic_password,
            check_mode,
            subset_percent,
            check_timeout,
            run_id=run_id,
        )
        if await _finalize_if_canceled(
            factory, job.name, run_id, kind_label="Verification"
        ):
            return

        now: datetime = datetime.now(timezone.utc)
        async with factory() as s:
            final_run: BackupRun | None = await s.get(BackupRun, str(run_id))
            if final_run:
                if rc == 0:
                    final_run.status = RunStatus.success
                    final_run.check_status = CheckStatus.passed
                    logger.info(
                        f"job_id={job_id} run_id={run_id} step=integrity_check "
                        f"status=passed"
                    )
                else:
                    final_run.status = RunStatus.failed
                    final_run.check_status = CheckStatus.failed
                    final_run.check_error_output = check_err
                    logger.warning(
                        f"job_id={job_id} run_id={run_id} step=integrity_check "
                        f"status=failed rc={rc}"
                    )
                final_run.finished_at = now
                now_naive: datetime = now.replace(tzinfo=None)
                final_run.duration_seconds = int(
                    (now_naive - final_run.started_at).total_seconds()
                )
                final_run.prune_status = PruneStatus.skipped
                await s.commit()

        if notify_on_verification and ntfy_topic:
            status_str: str = "passed" if rc == 0 else "failed"
            await _try_notify(
                ntfy_server_url,
                ntfy_topic,
                f"Verification {status_str}: {job.name}",
                f"Check status: {status_str}",
                token=ntfy_token,
            )

    except Exception as exc:
        logger.exception(
            f"job_id={job_id} run_id={run_id} check_runner_crashed error={exc!r}"
        )
        try:
            crash_now: datetime = datetime.now(timezone.utc)
            async with factory() as s:
                crash_run: BackupRun | None = await s.get(BackupRun, str(run_id))
                if crash_run is not None:
                    crash_status: Any = crash_run.status
                    if crash_status == RunStatus.running:
                        tb: str = traceback.format_exc()
                        crash_run.status = RunStatus.failed
                        crash_run.check_status = CheckStatus.failed
                        crash_run.check_error_output = (
                            f"Check runner crashed: {exc!r}\n\n{tb}"
                        )
                        crash_run.finished_at = crash_now
                        crash_started: datetime = crash_run.started_at
                        crash_run.duration_seconds = int(
                            (
                                crash_now.replace(tzinfo=None) - crash_started
                            ).total_seconds()
                        )
                        crash_run.prune_status = PruneStatus.skipped
                        await s.commit()
        except Exception as recovery_exc:
            logger.error(
                f"job_id={job_id} run_id={run_id} "
                f"crash_recovery_failed error={recovery_exc!r}"
            )
    finally:
        await _trim_run_history(factory, str(job_id))
        logger.info(f"job_id={job_id} run_id={run_id} check_completed")


# How many failing items are parsed out of the streams, and how many of those
# are rendered into `error_output`. The parse limit keeps a pathological run (a
# share that denies every one of a million files) from building a million-entry
# list; the render limit keeps the DB column — which is loaded on every
# run-detail fetch — small. Both are far above the count an operator will
# actually read before going to look at the mount.
_FAILED_ITEM_PARSE_LIMIT: int = 200
_MAX_REPORTED_FAILED_ITEMS: int = 50
# Fallback when restic said something we could not parse: keep the tail, since
# the fatal and the exit_error line arrive last.
_MAX_STDERR_TAIL_CHARS: int = 4000

# Written to BackupRun.prune_error_output when a partial backup withholds
# retention. `prune_status=skipped` alone is ambiguous — it is the same value a
# job with no retention policy gets — so without this note an operator reads a
# withheld policy as "nothing configured" and never learns the repository has
# stopped shrinking. The wording has to explain the trade rather than sound like
# a fault: nothing broke here.
RETENTION_SKIPPED_PARTIAL_NOTE: str = (
    "Retention (restic forget) was not applied because this backup was partial: "
    "some files could not be read, and an incomplete snapshot must not be "
    "allowed to push a complete one out of the retention policy. The snapshot "
    "itself was saved and nothing was deleted. Retention runs again after a "
    "backup that reads everything — until then this repository keeps growing, "
    "so fix the unreadable items above."
)


def _extract_failed_items(
    *streams: str, limit: int = _FAILED_ITEM_PARSE_LIMIT
) -> List[str]:
    """Pull per-file error messages out of restic's --json streams so the run
    record can show *which* items failed, not just that something did.

    **Both** streams must be passed for a partial backup. restic writes its
    `message_type=error` lines to stderr, not stdout — verified against restic
    0.18.1 and 0.19.1, where stdout carried only `status` and `summary`. This
    function used to be called with stdout alone, so every rc=3 run recorded zero
    failed items and the run page showed a bare "some files could not be read"
    with no paths after it. stdout is still scanned because it costs one pass
    over an already-bounded string and covers merged streams and older builds.

    One failure can be reported more than once — an unreadable directory comes
    back from both the scanner and the archiver (observed with 0.18.1 and 0.19.1) —
    so identical (item, message) pairs are collapsed into one entry and their
    phases merged. Counting the error *events* would report two failures for
    one folder and inflate the count on every real mount.

    Parsing stops at `limit` distinct items; the caller renders fewer still.
    """
    # Insertion-ordered: (item, message) -> phases seen, in the order restic
    # reported them.
    collected: Dict[Tuple[str, str], List[str]] = {}
    for stream in streams:
        for line in stream.split("\n"):
            if len(collected) >= limit:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("message_type") != "error":
                continue
            err = obj.get("error", {})
            raw_msg = err.get("message") if isinstance(err, dict) else err
            # Do not stringify before the emptiness check below: str(None) is
            # "None", which is truthy, so an error line carrying neither a path
            # nor a message used to survive the guard and be rendered to the
            # operator as a failed item literally named "None".
            msg = "" if raw_msg is None else str(raw_msg)
            item = str(obj.get("item") or "")
            if not item and not msg:
                continue
            phases: List[str] = collected.setdefault((item, msg), [])
            # `during` separates a file that could not be read (archival) from
            # a directory that could not even be listed (scan) — different
            # causes, different fixes.
            during = obj.get("during")
            if during and during not in phases:
                phases.append(str(during))

    items: List[str] = []
    for (item, msg), phases in collected.items():
        suffix: str = f" [{', '.join(phases)}]" if phases else ""
        items.append(f"{item}: {msg}{suffix}" if item else f"{msg}{suffix}")
    return items


def _at_least_suffix(count: int) -> str:
    """`+` when parsing stopped at the limit, so a count reads as "at least N".

    Shared by every place that prints one of these counts: the number is only
    ever a floor once `_extract_failed_items` has hit `_FAILED_ITEM_PARSE_LIMIT`,
    and a bare "200 items" would read as the whole truth.
    """
    return "+" if count >= _FAILED_ITEM_PARSE_LIMIT else ""


def _render_failed_items(failed_items: List[str]) -> List[str]:
    """The item lines allowed into `BackupRun.error_output`: at most
    `_MAX_REPORTED_FAILED_ITEMS`, followed by an honest "... and N more".

    **Every formatter must build its list through here.** They used to cap
    independently — the rc=3 path at this limit, the rc!=0 path not at all — so
    one flood of unreadable files wrote a few KiB into the run row if the backup
    half-succeeded and ~1.8 MiB if it failed outright, from the same source and
    the same parse limit. `error_output` is read on every run-detail fetch, so
    the bound has to hold whichever way the run ended, and one renderer is what
    keeps the two paths from drifting apart again.
    """
    shown: List[str] = failed_items[:_MAX_REPORTED_FAILED_ITEMS]
    lines: List[str] = list(shown)
    hidden: int = len(failed_items) - len(shown)
    if hidden > 0:
        lines.append(f"... and {hidden}{_at_least_suffix(len(failed_items))} more")
    return lines


def _format_partial_backup_error(failed_items: List[str], stderr: str) -> str:
    """Build the user-visible `error_output` for an rc=3 (partial) backup.

    The contract this enforces: the field is never uninformative. When restic
    named the items, they are listed (capped by `_render_failed_items`, with an
    honest count of what was not shown). When it did not, the retained stderr
    tail goes in verbatim rather than leaving the operator with a sentence they
    cannot act on.
    """
    count: int = len(failed_items)
    if count:
        parts: List[str] = [
            f"Partial backup: {count}{_at_least_suffix(count)} item(s) could "
            f"not be read; the snapshot was still saved."
        ]
        parts.extend(_render_failed_items(failed_items))
        return "\n".join(parts)

    parts = [
        "Partial backup: some files could not be read; the snapshot was still saved."
    ]
    tail: str = stderr.strip()
    if tail:
        parts.append("")
        parts.append("restic stderr:")
        parts.append(tail[-_MAX_STDERR_TAIL_CHARS:])
    return "\n".join(parts)


def _filter_backup_output(backup_stdout: str) -> str:
    """Strip restic's JSON progress lines (message_type=status) before the
    stdout is persisted to BackupRun.backup_output.

    The stored output exists to answer "what happened in this run" — error
    lines, the summary, and any non-JSON diagnostics. Progress lines are
    emitted throttled for the whole duration of the run and carry no
    post-mortem value; on a many-hour run they are thousands of lines that
    bloat the DB row and the run-detail page.
    """
    kept: List[str] = []
    for line in backup_stdout.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if obj.get("message_type") == "status":
                continue
        kept.append(line)
    return "\n".join(kept)


def _format_backup_error(rc: int, json_errors: List[str], stderr: str) -> str:
    """Build the user-visible error_output string for a failed backup run.

    Always includes the restic exit code and stderr. When restic emitted
    per-file JSON error lines on stdout before giving up, those are included
    too — they name the specific path/operation that caused the failure,
    which stderr (usually a single post-mortem fatal) does not. Order is
    chosen so the operator sees the high-level summary first, then the
    granular per-file context (gaps.md H5).

    The item list goes through `_render_failed_items` — the same renderer the
    partial-backup path uses. This one used to print every parsed item instead,
    so the two paths bounded the same DB column differently.
    """
    parts: List[str] = [f"Backup failed (restic exit code {rc})."]
    if stderr.strip():
        parts.append("")
        parts.append(stderr.strip())
    if json_errors:
        parts.append("")
        parts.append("Per-file errors:")
        parts.extend(_render_failed_items(json_errors))
    return "\n".join(parts)


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
                    "metadata_timeout_seconds": settings_obj.metadata_timeout_seconds,
                }

        # Step 2.5: Mount verification. The sentinel stat() runs through
        # fs.run_probe: on a mounted-but-hung SMB share the kernel call can
        # block for minutes, and doing that on the event loop would freeze
        # the whole app (API, scheduler, every other job). A probe timeout
        # is treated as "mount not verified" — exactly the don't-back-up-now
        # condition the sentinel exists to detect.
        logger.debug(f"job_id={job_id} run_id={current_run_id} step=verify_mount")
        # Resolved before the check so the path proven live below is byte-for-byte
        # the path handed to `restic backup` further down.
        source_path: str = build_source_path(job.source_label, job.source_subpath)
        if not await fs.run_probe(
            check_mount_file_exists,
            job.source_label,
            job.source_subpath,
            default=False,
        ):
            error_msg = (
                f"Mount check failed: '.billa_gates_check' file was not found "
                f"at the root of the backup source '{source_path}' "
                f"(or the mount did not respond within the probe timeout)."
            )
            logger.error(
                f"job_id={job_id} run_id={current_run_id} step=verify_mount "
                f"error=mount_check_failed source_label={job.source_label} "
                f"source_subpath={job.source_subpath}"
            )
            async with factory() as s:
                failed_run = await s.get(BackupRun, str(current_run_id))
                if failed_run:
                    now_utc = datetime.now(timezone.utc)
                    failed_run.status = RunStatus.failed
                    failed_run.error_output = error_msg
                    failed_run.finished_at = now_utc
                    failed_run.duration_seconds = 0
                    failed_run.prune_status = PruneStatus.skipped
                    failed_run.check_status = CheckStatus.skipped
                    await s.commit()

            # Notify operator if failure notifications are configured
            ntfy_topic = cast(str | None, settings_dict.get("ntfy_topic"))
            if settings_dict.get("notify_on_failure") and ntfy_topic:
                await _try_notify(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic,
                    f"Backup failed: {job.name}",
                    error_msg[:200],
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )
            return

        if not await fs.run_probe(
            check_destination_mount_file_exists, job.destination_label, default=False
        ):
            error_msg = (
                f"Destination mount check failed: '.billa_gates_check' file was "
                f"not found at the root of the destination mount "
                f"'/destinations/{job.destination_label}' "
                f"(or the mount did not respond within the probe timeout)."
            )
            logger.error(
                f"job_id={job_id} run_id={current_run_id} step=verify_mount "
                f"error=destination_mount_check_failed "
                f"destination_label={job.destination_label}"
            )
            async with factory() as s:
                failed_run = await s.get(BackupRun, str(current_run_id))
                if failed_run:
                    now_utc = datetime.now(timezone.utc)
                    failed_run.status = RunStatus.failed
                    failed_run.error_output = error_msg
                    failed_run.finished_at = now_utc
                    failed_run.duration_seconds = 0
                    failed_run.prune_status = PruneStatus.skipped
                    failed_run.check_status = CheckStatus.skipped
                    await s.commit()

            # Notify operator if failure notifications are configured
            ntfy_topic = cast(str | None, settings_dict.get("ntfy_topic"))
            if settings_dict.get("notify_on_failure") and ntfy_topic:
                await _try_notify(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic,
                    f"Backup failed: {job.name}",
                    error_msg[:200],
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )
            return

        # Step 3: Start notification
        ntfy_topic: str | None = cast(str | None, settings_dict.get("ntfy_topic"))
        if settings_dict.get("notify_on_start") and ntfy_topic:
            logger.info(f"step=start_notification job_id={job_id}")
            src: str = job.source_label
            dst: str = job.destination_label
            await _try_notify(
                cast(str | None, settings_dict.get("ntfy_server_url")),
                ntfy_topic,
                f"Starting backup: {job.name}",
                f"Source: {src}, Destination: {dst}",
                token=cast(str | None, settings_dict.get("ntfy_token")),
            )

        # Build repo path. `source_path` was resolved back at the mount check
        # (Step 2.5) — the verified path and the backed-up path are the same
        # string by construction.
        job_dest_label: str = job.destination_label
        repo_path: str = repository.build_repo_path(job_dest_label, job.name)

        # Step 4: Init check
        #
        # Branch strictly on restic's documented exit codes (≥0.17): 10 = repo
        # not found, 11 = lock failed, 12 = wrong password. Stderr substring
        # matching — the previous approach — silently misclassified errors
        # whenever restic changed its message wording, and could even trigger
        # `restic init` on top of a real-but-temporarily-unreachable repo
        # because the stderr happened not to contain "wrong password"
        # (gaps.md H5). Any unrecognized non-zero rc is treated as a generic
        # failure with stderr surfaced verbatim to the user.
        logger.debug(
            f"job_id={job_id} run_id={current_run_id} step=init_check "
            f"repo_path={repo_path}"
        )

        async def _fail_init_check(message: str, *, log_tag: str) -> None:
            logger.error(
                f"job_id={job_id} run_id={current_run_id} step=init_check "
                f"error={log_tag}"
            )
            async with factory() as s:
                run_row: BackupRun | None = await s.get(BackupRun, str(current_run_id))
                if run_row:
                    now_utc = datetime.now(timezone.utc)
                    run_row.status = RunStatus.failed
                    run_row.error_output = message
                    run_row.finished_at = now_utc
                    run_row.duration_seconds = 0
                    run_row.prune_status = PruneStatus.skipped
                    run_row.check_status = CheckStatus.skipped
                    await s.commit()

            # Notify operator if failure notifications are configured
            if settings_dict.get("notify_on_failure") and ntfy_topic:
                await _try_notify(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic,
                    f"Backup failed: {job.name}",
                    message[:200] if message else "Unknown error during init check",
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )

        async def _was_canceled() -> bool:
            """Detect user-initiated cancel and finalize the row as canceled.

            Returns True once the row has been written; the caller must then
            return immediately to short-circuit the rest of the pipeline.
            Idempotent — clears the registry flag so repeated checks after
            finalization don't re-fire the notification.
            """
            if not process_registry.is_canceled(current_run_id):
                return False

            now_c: datetime = datetime.now(timezone.utc)
            cancel_duration: int = 0
            async with factory() as s:
                cancel_run: BackupRun | None = await s.get(
                    BackupRun, str(current_run_id)
                )
                if cancel_run is not None:
                    # Always finalize as canceled even if an earlier
                    # intermediate write set status=failed (rc!=0 from a
                    # SIGTERM'd restic process); the user's stop click takes
                    # precedence — that's the whole point of the cancel
                    # action.
                    cancel_run.status = RunStatus.canceled
                    cancel_run.reason = RunReason.user_canceled
                    cancel_run.finished_at = now_c
                    cancel_run.duration_seconds = int(
                        (
                            now_c.replace(tzinfo=None) - cancel_run.started_at
                        ).total_seconds()
                    )
                    if not cancel_run.prune_status:
                        cancel_run.prune_status = PruneStatus.skipped
                    if not cancel_run.check_status:
                        cancel_run.check_status = CheckStatus.skipped
                    if not cancel_run.error_output:
                        cancel_run.error_output = "Canceled by user."
                    cancel_duration = cancel_run.duration_seconds or 0
                    await s.commit()

            ntfy_topic_cancel: str | None = cast(
                str | None, settings_dict.get("ntfy_topic")
            )
            if ntfy_topic_cancel and settings_dict.get("notify_on_warning"):
                await _try_notify(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic_cancel,
                    f"Backup canceled: {job.name}",
                    f"Duration: {cancel_duration}s — canceled by user.",
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )

            process_registry.clear_canceled(current_run_id)
            logger.info(
                f"job_id={job_id} run_id={current_run_id} status=canceled "
                f"reason=user_canceled"
            )
            return True

        rc: int
        stderr: str
        metadata_timeout = settings_dict.get("metadata_timeout_seconds", 600)
        rc, _, stderr = await restic.restic_cat_config(
            repo_path, job_password, metadata_timeout, run_id=current_run_id
        )
        if await _was_canceled():
            return

        # rc=11 means the repo metadata read was blocked by a stale lock. The
        # cheapest fix is to call unlock and retry once. Looping further would
        # hang the runner on a legitimately-contended repo, so we cap retries
        # at exactly one (matches the rc=11 retry policy on `restic backup`).
        if rc == RESTIC_RC_LOCK_FAILED:
            logger.warning(
                f"job_id={job_id} run_id={current_run_id} step=init_check "
                f"rc=11 stale_lock_suspected attempting_unlock_and_retry"
            )
            try:
                await restic.restic_unlock(
                    repo_path, job_password, metadata_timeout, run_id=current_run_id
                )
            except Exception as exc:
                logger.warning(
                    f"job_id={job_id} run_id={current_run_id} step=init_check "
                    f"unlock_exception error={exc!r}"
                )
            if await _was_canceled():
                return
            rc, _, stderr = await restic.restic_cat_config(
                repo_path, job_password, metadata_timeout, run_id=current_run_id
            )
            if await _was_canceled():
                return
            if rc == RESTIC_RC_LOCK_FAILED:
                await _fail_init_check(
                    f"Repository is locked and could not be unlocked: {stderr}",
                    log_tag="lock_failed",
                )
                return

        if rc == RESTIC_RC_WRONG_PASSWORD:
            await _fail_init_check(
                "Repository password is incorrect. Verify the password "
                "matches the one used when the repo was initialized.\n\n"
                f"restic stderr: {stderr}",
                log_tag="wrong_password",
            )
            return

        if rc == RESTIC_RC_REPO_NOT_FOUND:
            # A run never initializes a repository — that happens once, when
            # the job is created (app/services/repository.py::ensure_repository).
            # So rc=10 here is always a fault: the destination was swapped,
            # wiped, or renamed without moving the data. The sentinel mount
            # check cannot catch this because the mount itself is healthy.
            # Initializing would silently start an empty repo while the user
            # believes their snapshot history is intact.
            await _fail_init_check(
                f"Repository not found at '{repo_path}' (restic exit code 10). "
                f"It is created when the job is created, so it should be "
                f"present. Refusing to initialize a new, empty repository over "
                f"the job's history. If the backup disk was replaced or the "
                f"destination folder was renamed or moved, move the existing "
                f"repository directory to this path before running "
                f"again.\n\nrestic stderr: {stderr}",
                log_tag="repo_missing",
            )
            return
        elif rc != 0:
            # Generic failure: network blip, permission glitch, older restic
            # version returning rc=1 for something we'd otherwise classify.
            # Crucially we do NOT init here — that would corrupt the user's
            # mental model of "my repo wasn't found" when in fact the repo
            # is fine but temporarily unreachable.
            await _fail_init_check(
                f"Failed to access repository (restic exit code {rc}): {stderr}",
                log_tag=f"unrecognized_rc_{rc}",
            )
            return

        # Step 4.5: Auto-unlock — clear any stale lock left behind by an
        # abrupt termination (OOM, container kill, host reboot). Failure is
        # logged but non-fatal: a freshly initialized repo legitimately has
        # no lock to remove, and any deeper problem (missing restic binary,
        # network down) will be surfaced by the backup step that follows.
        if settings_dict.get("auto_unlock", True):
            try:
                unlock_rc, _, unlock_err = await restic.restic_unlock(
                    repo_path, job_password, metadata_timeout, run_id=current_run_id
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

        if await _was_canceled():
            return

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
        parent_lookup_success = True
        parent_snapshot_id: Optional[str] = None
        try:
            parent_snapshot_id = await restic.restic_latest_snapshot_id(
                repo_path,
                job_password,
                timeout_seconds=metadata_timeout,
                run_id=current_run_id,
            )
            logger.info(
                f"job_id={job_id} run_id={current_run_id} step=parent_lookup "
                f"parent_snapshot_id={parent_snapshot_id}"
            )
        except restic.ResticError as exc:
            logger.error(
                f"job_id={job_id} run_id={current_run_id} step=parent_lookup "
                f"status=failed error={exc!r}"
            )
            async with factory() as s:
                fail_run = await s.get(BackupRun, str(current_run_id))
                if fail_run:
                    fail_run.status = RunStatus.failed
                    fail_run.error_output = f"snapshots command failed: {exc}"
                    await s.commit()
            parent_lookup_success = False

        # The parent lookup is itself a restic subprocess, so Stop can land on
        # it — and terminating that lookup does nothing to prevent the backup.
        # Without this poll the cancel goes unnoticed until the (possibly
        # multi-hour) backup it was meant to prevent has already finished, and
        # the run is then marked canceled after the fact. Every gap between two
        # restic subprocesses needs one of these.
        if await _was_canceled():
            return

        async def _persist_output_snapshot(output_text: str) -> None:
            """Write a bounded progress snapshot to the run row mid-backup.

            RunDetail polls the run row for the whole (possibly multi-hour)
            run, but until now had nothing to show: backup_output was written
            only after restic exited. `restic_backup` hands us a small snapshot
            — the newest progress line plus any errors so far — every few
            seconds, and the existing poll surfaces it. The final value is
            still written by the stats step below, which wins.
            """
            async with factory() as s:
                progress_run: BackupRun | None = await s.get(
                    BackupRun, str(current_run_id)
                )
                if progress_run is not None:
                    progress_run.backup_output = output_text
                    await s.commit()

        backup_success: bool = False
        # rc=3 means restic ran but some files couldn't be read; the snapshot
        # was still saved. We treat it as success-with-warnings: the stats and
        # the snapshot are recorded, the final status is `warning` (not
        # `success`), and `restic forget` is withheld — see
        # RETENTION_SKIPPED_PARTIAL_NOTE.
        backup_warning: bool = False
        # `restic forget` *is* the retention policy — the only thing that drops
        # old snapshots. Its usual failure causes (stale lock, permissions,
        # disk full) persist across runs, so reporting such a run as `success`
        # meant the badge, the run list and the ntfy push all looked healthy
        # while the repo grew without bound until the destination filled up
        # months later. The snapshot *was* written, so the run is not `failed`
        # either — it is a `warning`.
        retention_failed: bool = False
        # Set when a partial backup withheld retention. Distinct from
        # `retention_failed` on purpose: nothing broke, the policy was held back
        # deliberately, and telling the operator "forget failed" would send them
        # hunting a stale lock that does not exist.
        retention_skipped_partial: bool = False
        # Carried out of the backup step so the completion push can say how
        # many items failed, not just that something did.
        failed_item_count: int = 0
        summary: Optional[Dict[str, Any]] = None
        stdout: str = ""

        if parent_lookup_success:
            logger.info(
                f"job_id={job_id} run_id={current_run_id} step=backup_execution "
                f"source_path={source_path} timeout_seconds={timeout_seconds}"
            )
            backup_kwargs: Dict[str, Any] = build_backup_kwargs(job)

            try:
                rc, stdout, stderr, summary = await restic.restic_backup(
                    repo_path,
                    job_password,
                    source_path,
                    timeout_seconds,
                    parent_snapshot_id=parent_snapshot_id,
                    run_id=current_run_id,
                    on_output=_persist_output_snapshot,
                    **backup_kwargs,
                )
                if await _was_canceled():
                    return
                # Exit code 11 = restic failed to acquire the repo lock. The most
                # common cause is a stale lock left by a previous abrupt
                # termination. Clear it and retry exactly once — never loop, or
                # a genuinely-contended repo would hang the runner forever.
                if rc == 11:
                    logger.warning(
                        f"job_id={job_id} run_id={current_run_id} "
                        f"step=backup_execution rc=11 stale_lock_suspected "
                        f"attempting_unlock_and_retry"
                    )
                    try:
                        await restic.restic_unlock(
                            repo_path,
                            job_password,
                            metadata_timeout,
                            run_id=current_run_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"job_id={job_id} run_id={current_run_id} step=lock_retry "
                            f"unlock_exception error={exc!r}"
                        )
                    if await _was_canceled():
                        return
                    rc, stdout, stderr, summary = await restic.restic_backup(
                        repo_path,
                        job_password,
                        source_path,
                        timeout_seconds,
                        parent_snapshot_id=parent_snapshot_id,
                        run_id=current_run_id,
                        **backup_kwargs,
                    )
                    if await _was_canceled():
                        return

                if rc == 0:
                    backup_success = True
                    logger.info(
                        f"job_id={job_id} run_id={current_run_id} "
                        f"step=backup_execution status=success"
                    )
                elif rc == 3:
                    backup_success = True
                    backup_warning = True
                    # stderr first: that is where restic puts the per-file
                    # error lines. Passing only stdout here (as this did) made
                    # every partial backup report zero failed items, so the run
                    # page named no file and the log recorded only a count.
                    failed_items = _extract_failed_items(stderr, stdout)
                    failed_item_count = len(failed_items)
                    logger.warning(
                        f"job_id={job_id} run_id={current_run_id} "
                        f"step=backup_execution status=warning rc=3 "
                        f"failed_items={failed_item_count} "
                        f"first_failed={failed_items[:3]}"
                    )
                    async with factory() as s:
                        warn_run: BackupRun | None = await s.get(
                            BackupRun, str(current_run_id)
                        )
                        if warn_run:
                            warn_run.error_output = _format_partial_backup_error(
                                failed_items, stderr
                            )
                            await s.commit()
                else:
                    # Restic's --json stream emits per-file `message_type=error`
                    # lines naming the path that failed; stderr usually only
                    # carries the final fatal. Stitching both into error_output
                    # gives the operator the *which file* context that pure
                    # stderr does not (gaps.md H5). Falls back gracefully when
                    # one or the other is empty.
                    logger.error(
                        f"job_id={job_id} run_id={current_run_id} "
                        f"step=backup_execution status=failed rc={rc}"
                    )
                    json_errors = _extract_failed_items(stdout)
                    error_msg = _format_backup_error(rc, json_errors, stderr)
                    async with factory() as s:
                        backup_fail_run: BackupRun | None = await s.get(
                            BackupRun, str(current_run_id)
                        )
                        if backup_fail_run:
                            backup_fail_run.status = RunStatus.failed
                            backup_fail_run.error_output = error_msg
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
                    stats_run.backup_output = _filter_backup_output(stdout)
                    await s.commit()

            # Step 8: Prune (only if backup succeeded)
            logger.debug(f"job_id={job_id} run_id={current_run_id} step=prune")
            retention_kwargs: Dict[str, Any] = build_retention_kwargs(job)

            # When retention is configured, run `restic forget` only — never
            # `restic prune`. Prune is the heaviest restic operation (rewrites
            # every pack file) and bundling it into the backup window made
            # backups unpredictably long (gaps.md H1). Prune is now manual
            # (POST /api/jobs/{id}/prune) or scheduled separately.
            # When no retention is set, forget would be a no-op and prune
            # without forget cannot reclaim anything (no snapshots removed),
            # so we skip the whole step.
            if retention_kwargs and backup_warning:
                # A partial snapshot is missing precisely the files restic could
                # not read. Counting it toward the policy deletes a complete
                # snapshot to make room for an incomplete one: with --keep-last 3
                # against a source that has started failing (bad sectors, a
                # permission change, a handle held open over SMB), three runs
                # leave nothing but partials and the last good copy of those
                # files is gone from the repository. Withholding instead trades
                # that silent, irreversible loss for repository growth, which is
                # visible — every such run is already a `warning`, and the note
                # below says so on the run page.
                retention_skipped_partial = True
                logger.warning(
                    f"job_id={job_id} run_id={current_run_id} step=forget "
                    f"skipped reason=partial_backup"
                )
                async with factory() as s:
                    partial_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if partial_run:
                        partial_run.prune_status = PruneStatus.skipped
                        partial_run.prune_error_output = RETENTION_SKIPPED_PARTIAL_NOTE
                        await s.commit()
            elif retention_kwargs:
                logger.info(
                    f"job_id={job_id} run_id={current_run_id} step=forget "
                    f"applying_retention"
                )
                rc, _, forget_err = await restic.restic_forget(
                    repo_path,
                    job_password,
                    timeout_seconds,
                    run_id=current_run_id,
                    **retention_kwargs,
                )
                if await _was_canceled():
                    return

                async with factory() as s:
                    forget_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if forget_run:
                        if rc == 0:
                            forget_run.prune_status = PruneStatus.passed
                            logger.info(
                                f"job_id={job_id} run_id={current_run_id} "
                                f"step=forget status=passed"
                            )
                        else:
                            forget_run.prune_status = PruneStatus.failed
                            forget_run.prune_error_output = forget_err
                            retention_failed = True
                            logger.warning(
                                f"job_id={job_id} run_id={current_run_id} "
                                f"step=forget status=failed rc={rc}"
                            )
                        await s.commit()
            else:
                logger.info(
                    f"job_id={job_id} run_id={current_run_id} step=forget "
                    f"skipped reason=no_retention"
                )
                async with factory() as s:
                    no_ret_run: BackupRun | None = await s.get(
                        BackupRun, str(current_run_id)
                    )
                    if no_ret_run:
                        no_ret_run.prune_status = PruneStatus.skipped
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
                        RunStatus.warning
                        if (backup_warning or retention_failed)
                        else RunStatus.success
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
                await _try_notify(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic_send,
                    f"Backup succeeded: {job.name}",
                    msg,
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )
            elif final_status_notify == RunStatus.warning and settings_dict.get(
                "notify_on_warning"
            ):
                # Name what actually went wrong. A run can be a warning because
                # files were unreadable, because retention failed, or both —
                # a body hardcoded to one of them misinforms the operator.
                reasons: List[str] = []
                if backup_warning and failed_item_count:
                    reasons.append(
                        f"{failed_item_count} item(s) could not be read; "
                        "snapshot was still saved"
                    )
                elif backup_warning:
                    reasons.append(
                        "some files could not be read; snapshot was still saved"
                    )
                if retention_failed:
                    reasons.append(
                        "retention (restic forget) failed — old snapshots were "
                        "not removed, so the repository will keep growing"
                    )
                if retention_skipped_partial:
                    reasons.append(
                        "retention was held back so an incomplete snapshot "
                        "could not push a complete one out of the policy — "
                        "the repository keeps growing until a clean backup runs"
                    )
                warn_msg: str = (
                    f"Duration: {final_run.duration_seconds}s — {'; '.join(reasons)}."
                )
                await _try_notify(
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
                await _try_notify(
                    cast(str | None, settings_dict.get("ntfy_server_url")),
                    ntfy_topic_send,
                    f"Backup failed: {job.name}",
                    error_excerpt,
                    token=cast(str | None, settings_dict.get("ntfy_token")),
                )

        # Step 12: Integrity check (removed from backup pipeline,
        # run manually/separately)
        pass

    except Exception as exc:
        # Top-level safety net: if any unhandled exception bubbles out of the
        # pipeline above (SQLite OperationalError, network failure, subprocess
        # crash, unforeseen edge case), the run row would otherwise stay at
        # status=running forever. trigger_run's overlap check queries the DB
        # for any running row independently of the in-memory active_jobs set
        # — so a stranded row locks the job out of every future trigger
        # (manual or scheduled) until the operator manually edits the DB or
        # restarts the container. Finalizing to `failed` here closes that
        # lock-up. The recovery DB write is itself wrapped: if the DB is
        # actually unreachable we have no recourse, but logging beats
        # crashing the cleanup wrapper above.
        logger.exception(
            f"job_id={job_id} run_id={current_run_id} backup_runner_crashed "
            f"error={exc!r}"
        )
        try:
            crash_now: datetime = datetime.now(timezone.utc)
            async with factory() as s:
                crash_run: BackupRun | None = await s.get(
                    BackupRun, str(current_run_id)
                )
                if crash_run is not None:
                    crash_status: Any = crash_run.status
                    if crash_status == RunStatus.running:
                        tb: str = traceback.format_exc()
                        crash_run.status = RunStatus.failed
                        crash_run.error_output = (
                            f"Backup runner crashed: {exc!r}\n\n{tb}"
                        )
                        crash_run.finished_at = crash_now
                        crash_started: datetime = crash_run.started_at
                        crash_run.duration_seconds = int(
                            (
                                crash_now.replace(tzinfo=None) - crash_started
                            ).total_seconds()
                        )
                        crash_prune: Any = crash_run.prune_status
                        if not crash_prune:
                            crash_run.prune_status = PruneStatus.skipped
                        crash_check: Any = crash_run.check_status
                        if not crash_check:
                            crash_run.check_status = CheckStatus.skipped
                        await s.commit()
        except Exception as recovery_exc:
            logger.error(
                f"job_id={job_id} run_id={current_run_id} "
                f"crash_recovery_failed error={recovery_exc!r}"
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
