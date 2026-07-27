"""FastAPI routes for BackupJob CRUD and management sub-routes.

All write operations that touch the scheduler use the module-level
'scheduler' object from app.core.scheduler so that tests can patch it.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Any, List, Sequence

from apscheduler.jobstores.base import JobLookupError
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas.jobs import (
    JobCheckRequest,
    JobCommandResponse,
    JobCreate,
    JobResponse,
    JobUpdate,
    RunSummarySchema,
    SnapshotResponse,
    _validate_schedule_value,
)
from app.core import fs
from app.core import scheduler as scheduler_module
from app.core.logging import get_logger, log_call
from app.db.models import (
    AppSettings,
    BackupJob,
    BackupRun,
    RunReason,
    RunStatus,
    TriggeredBy,
)
from app.services import (
    backup_runner,
    job_commands,
    repository,
    restic,
    snapshot_listing,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Default mount root paths.  Not patched by tests (tests patch os.path.isdir
# globally), but kept here for consistency with the mounts module.
_SOURCES_ROOT = "/sources"
_DESTINATIONS_ROOT = "/destinations"


# ── Private helpers ───────────────────────────────────────────────────────────


@log_call
async def _get_job_or_404(job_id: str, session: AsyncSession) -> BackupJob:
    """Fetch a BackupJob by id or raise HTTP 404."""
    job = await session.get(BackupJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    return job


@log_call
async def _ensure_no_active_run(job_id: str, session: AsyncSession) -> None:
    """Raise HTTP 409 when a run is currently live for this job.

    Mirrors trigger_run's dual check: the in-memory active_jobs set covers
    runs dispatched by this process; a status=running DB row covers the
    window between row creation and pipeline start. Editing a job mid-run
    would race the pipeline, which re-reads job fields (paths, password,
    retention) between steps.
    """
    if uuid.UUID(job_id) in backup_runner.active_jobs:
        raise HTTPException(
            status_code=409,
            detail="A run is in progress for this job; cancel it before editing.",
        )
    result = await session.execute(
        select(BackupRun)
        .where(BackupRun.job_id == job_id, BackupRun.status == RunStatus.running)
        .limit(1)
    )
    if result.scalars().first() is not None:
        raise HTTPException(
            status_code=409,
            detail="A run is in progress for this job; cancel it before editing.",
        )


async def _raise_409_if_overlapping(run_id: str, session: AsyncSession) -> None:
    """Translate a skipped/overlapping_run trigger result into HTTP 409.

    The trigger functions record a skipped audit row (kept — it documents
    the attempt) and return its id; the manual API contract is 409 so the
    UI can tell the user a run is already active.
    """
    run = await session.get(BackupRun, run_id)
    if (
        run is not None
        and run.status == RunStatus.skipped
        and run.reason == RunReason.overlapping_run
    ):
        raise HTTPException(
            status_code=409,
            detail="A run is already in progress for this job",
        )


@log_call
async def _last_run(job_id: str, session: AsyncSession) -> RunSummarySchema | None:
    """Return the most recent BackupRun for the job, or None."""
    result = await session.execute(
        select(BackupRun)
        .where(BackupRun.job_id == job_id)
        .order_by(BackupRun.started_at.desc())
        .limit(1)
    )
    run = result.scalar_one_or_none()
    return RunSummarySchema.model_validate(run) if run else None


@log_call
def _next_run_time(job_id: str) -> datetime | None:
    """Look up the job's next scheduled fire time from APScheduler."""
    # apscheduler lacks type stubs; cast scheduler to Any once so the
    # Unknown-typed return doesn't cascade through pyright strict mode.
    sched: Any = scheduler_module.scheduler
    sched_job: Any = sched.get_job(job_id)
    if sched_job is None:
        return None
    next_run: datetime | None = sched_job.next_run_time
    return next_run


@log_call
async def _build_job_response(
    job: BackupJob, session: AsyncSession
) -> dict[str, object]:
    """Assemble a JobResponse dict with computed fields injected."""
    return {
        **{c.key: getattr(job, c.key) for c in job.__table__.columns},
        "restic_password": None,
        "next_run_time": _next_run_time(job.id),
        "last_run": await _last_run(job.id, session),
    }


@log_call
async def _validate_mounts(
    source_label: str,
    destination_label: str,
    source_subpath: str | None = None,
) -> None:
    """Raise HTTP 422 if either mount directory does not exist, or if its
    `.billa_gates_check` sentinel file is missing.

    isdir and the sentinel checks run through fs.run_probe: on a hung network
    mount the stat() would otherwise block the event loop and freeze the whole
    app. A probe timeout is reported as "not mounted".

    The sentinel gate is what tells a live drive apart from an empty mountpoint
    left behind by a detached drive — the directory exists either way, so
    without it job creation would `restic init` a phantom repository onto the
    container's ephemeral layer. Job creation reads the source and initializes
    the destination repo, so both sentinels are required here.

    The source sentinel is required at the *effective* source path
    (`/sources/<label>[/<subpath>]`) — the same path each run verifies — so a
    subpath job cannot be created against a folder that will fail every run.
    """
    if not await fs.run_probe(
        os.path.isdir, f"{_SOURCES_ROOT}/{source_label}", default=False
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Source mount '/sources/{source_label}' is not mounted",
        )
    if not await fs.run_probe(
        backup_runner.check_mount_file_exists,
        source_label,
        source_subpath,
        default=False,
    ):
        source_path = backup_runner.build_source_path(source_label, source_subpath)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Backup source '{source_path}' has no '.billa_gates_check' "
                f"sentinel file at its root — the drive is probably not "
                f"attached, or that folder is not the one you meant. Refusing "
                f"to create the job."
            ),
        )
    if not await fs.run_probe(
        os.path.isdir, f"{_DESTINATIONS_ROOT}/{destination_label}", default=False
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Destination mount '/destinations/{destination_label}' is not mounted"
            ),
        )
    if not await fs.run_probe(
        backup_runner.check_destination_mount_file_exists,
        destination_label,
        default=False,
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Destination mount '/destinations/{destination_label}' is "
                f"present but its '.billa_gates_check' sentinel file was not "
                f"found — the drive is probably not attached. Refusing to "
                f"initialize a repository there."
            ),
        )


@log_call
async def _check_duplicate(
    source_label: str,
    source_subpath: str | None,
    destination_label: str,
    session: AsyncSession,
    exclude_id: str | None = None,
) -> None:
    """Raise 409 if another job already uses the same (source_label,
    source_subpath, destination_label) tuple — per design doc §6."""
    stmt = select(BackupJob).where(
        BackupJob.source_label == source_label,
        BackupJob.source_subpath.is_(source_subpath)
        if source_subpath is None
        else BackupJob.source_subpath == source_subpath,
        BackupJob.destination_label == destination_label,
    )
    if exclude_id:
        stmt = stmt.where(BackupJob.id != exclude_id)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "A job with the same source label, source subpath, "
                    "and destination label already exists"
                ),
                "conflicting_job_id": existing.id,
                "conflicting_job_name": existing.name,
            },
        )


@log_call
async def _check_duplicate_name(
    name: str,
    destination_label: str,
    session: AsyncSession,
) -> None:
    """Raise 409 if another job already owns this repository directory.

    (destination_label, name) is the repo's on-disk address, so a collision
    would mean two jobs writing into one repository. The DB has a matching
    UniqueConstraint, but the comparison here is deliberately
    case-INsensitive: SQLite would happily store both 'Photos' and 'photos'
    while a case-insensitive destination filesystem (SMB, default APFS) maps
    them onto the same directory.
    """
    result = await session.execute(
        select(BackupJob).where(
            func.lower(BackupJob.name) == name.lower(),
            BackupJob.destination_label == destination_label,
        )
    )
    existing = result.scalars().first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"A job named '{existing.name}' already uses the repository "
                    f"at {repository.build_repo_path(destination_label, existing.name)}"
                ),
                "conflicting_job_id": existing.id,
                "conflicting_job_name": existing.name,
            },
        )


@log_call
async def _provision_repository(
    name: str, destination_label: str, password: str
) -> None:
    """Initialize or adopt the job's repository, or raise 422 explaining why not.

    Surfacing a bad password here is the whole point of provisioning at create
    time: the alternative is a job that looks fine until its first scheduled
    run fails at 3am.
    """
    repo_path = repository.build_repo_path(destination_label, name)
    outcome, detail = await repository.ensure_repository(repo_path, password)

    if outcome is repository.RepoOutcome.adopted:
        logger.info(
            "job create adopting existing repo repo=%s name=%s", repo_path, name
        )
        return
    if outcome is repository.RepoOutcome.initialized:
        logger.info("job create initialized repo repo=%s name=%s", repo_path, name)
        return

    messages = {
        repository.RepoOutcome.wrong_password: (
            f"A repository already exists at '{repo_path}' but the password "
            f"does not match it. Use the password that repository was created "
            f"with, or choose a different job name."
        ),
        repository.RepoOutcome.init_failed: (
            f"Failed to initialize a repository at '{repo_path}'."
        ),
        repository.RepoOutcome.unreachable: (
            f"Could not reach the repository at '{repo_path}'. The destination "
            f"may be disconnected or unresponsive."
        ),
    }
    message = messages[outcome]
    if detail:
        message = f"{message}\n\nrestic: {detail}"
    raise HTTPException(status_code=422, detail=message)


@log_call
def _register_in_scheduler(job: BackupJob) -> None:
    """Register or replace the job in APScheduler (only when scheduler is running)."""
    sched: Any = scheduler_module.scheduler
    if not sched.running:
        return

    trigger = scheduler_module.build_trigger(job.schedule_type, job.schedule_value)
    sched.add_job(
        backup_runner.trigger_run,
        trigger=trigger,
        args=[uuid.UUID(job.id), TriggeredBy.scheduler],
        id=job.id,
        replace_existing=True,
    )
    logger.info(
        "scheduler registered job_id=%s schedule=%s/%s",
        job.id,
        job.schedule_type,
        job.schedule_value,
    )


@log_call
def _remove_from_scheduler(job_id: str) -> None:
    """Remove a job from APScheduler; silently ignore if not found."""
    sched: Any = scheduler_module.scheduler
    if not sched.running:
        return
    try:
        sched.remove_job(job_id)
        logger.info("scheduler removed job_id=%s", job_id)
    except JobLookupError:
        pass


# ── GET /api/jobs ─────────────────────────────────────────────────────────────


@router.get("", response_model=List[JobResponse])
@log_call
async def list_jobs(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    """Return all backup jobs with computed fields (next_run_time, last_run, etc.)."""
    result = await session.execute(select(BackupJob))
    jobs = result.scalars().all()
    return [await _build_job_response(job, session) for job in jobs]


# ── POST /api/jobs ────────────────────────────────────────────────────────────


@router.post("", response_model=JobResponse, status_code=201)
@log_call
async def create_job(
    body: JobCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    """Create a new BackupJob and provision its restic repository.

    Validates that both source and destination mounts exist, that no job with
    the same (source_label, destination_label) pair already exists, that no
    other job owns the same repository directory, and that the schedule/check
    configuration is internally consistent (done by the JobCreate schema).

    The repository at /destinations/<destination_label>/<name> is initialized
    here — or adopted if one already exists and the password matches, which is
    how a job is reconnected to its history after the database is lost.
    Provisioning happens before the row is inserted so a failure leaves no job
    behind, and it is the reason name/destination_label/restic_password are
    immutable afterwards: together they address the repo.
    """
    await _validate_mounts(
        body.source_label, body.destination_label, body.source_subpath
    )
    await _check_duplicate(
        body.source_label, body.source_subpath, body.destination_label, session
    )
    await _check_duplicate_name(body.name, body.destination_label, session)

    await _provision_repository(body.name, body.destination_label, body.restic_password)

    job = BackupJob(**body.model_dump())
    session.add(job)
    await session.commit()
    await session.refresh(job)
    logger.info(
        "job created job_id=%s name=%s source=%s dest=%s schedule=%s/%s enabled=%s",
        job.id,
        job.name,
        job.source_label,
        job.destination_label,
        job.schedule_type,
        job.schedule_value,
        job.enabled,
    )

    if job.enabled:
        _register_in_scheduler(job)

    return await _build_job_response(job, session)


# ── GET /api/jobs/{id} ────────────────────────────────────────────────────────


@router.get("/{job_id}", response_model=JobResponse)
@log_call
async def get_job(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    """Return a single BackupJob by id."""
    job = await _get_job_or_404(job_id, session)
    return await _build_job_response(job, session)


# ── PUT /api/jobs/{id} ────────────────────────────────────────────────────────


@router.put("/{job_id}", response_model=JobResponse)
@log_call
async def update_job(
    job_id: str,
    body: JobUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Update a BackupJob (partial update).

    Fields absent from the payload keep their stored values; an explicit
    null clears a nullable field. Rejected outright:
    - editing while a run is in progress (409 — cancel the run first),
    - changing destination_label (permanently immutable),
    - changing restic_password after a run has written to the repo
      (success/warning status or a recorded snapshot),
    - explicit null on a non-nullable field,
    - a merged schedule_type/schedule_value pair that is invalid.
    """
    job = await _get_job_or_404(job_id, session)

    # A live run reads job fields between pipeline steps — editing under it
    # would race the pipeline. The user must cancel the run first.
    await _ensure_no_active_run(job_id, session)

    # Only fields the client actually sent take effect (partial update).
    update_data = body.model_dump(exclude_unset=True)

    # restic_password: absent and explicit null both mean "keep the stored
    # password" (it is never echoed to the client, so the form cannot
    # round-trip it).
    if update_data.get("restic_password") is None:
        update_data.pop("restic_password", None)

    # Explicit null is only a valid "clear" instruction for nullable columns.
    for field, value in update_data.items():
        column = BackupJob.__table__.columns.get(field)
        if value is None and column is not None and not column.nullable:
            raise HTTPException(
                status_code=422,
                detail=f"{field} cannot be null",
            )

    # name, destination_label and restic_password together address the restic
    # repository, which is initialized at job creation. Changing any of them
    # would point the job at a different (or non-existent) repo, or strand the
    # existing one on a password nobody holds. All three are therefore
    # permanently immutable. Re-sending an identical value is a no-op, so the
    # edit form can keep round-tripping every field.
    if "destination_label" in update_data and (
        update_data["destination_label"] != job.destination_label
    ):
        raise HTTPException(
            status_code=422,
            detail="destination_label cannot be changed after job creation",
        )

    if "name" in update_data and update_data["name"] != job.name:
        raise HTTPException(
            status_code=422,
            detail=(
                "name cannot be changed after job creation — it names the "
                "repository directory on disk. Create a new job to use a "
                "different name."
            ),
        )

    if (
        "restic_password" in update_data
        and update_data["restic_password"] != job.restic_password
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "restic_password cannot be changed after job creation — the "
                "repository is encrypted with it"
            ),
        )

    # Validate the merged schedule pair — a partial edit of either half must
    # not leave an invalid combination in the DB (it would crash scheduler
    # registration).
    if "schedule_type" in update_data or "schedule_value" in update_data:
        effective_type = update_data.get("schedule_type", job.schedule_type)
        effective_value = update_data.get("schedule_value", job.schedule_value)
        try:
            _validate_schedule_value(effective_type, effective_value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # Uniqueness check (on merged values) comes before mount validation so a
    # conflict returns 409 even when the mount is not present (avoids a
    # misleading 422).
    await _check_duplicate(
        update_data.get("source_label", job.source_label),
        update_data.get("source_subpath", job.source_subpath),
        update_data.get("destination_label", job.destination_label),
        session,
        exclude_id=job_id,
    )

    # Re-validate source mount only when the label actually changes.
    if (
        "source_label" in update_data
        and update_data["source_label"] != job.source_label
    ):
        new_source = update_data["source_label"]
        if not await fs.run_probe(
            os.path.isdir, f"{_SOURCES_ROOT}/{new_source}", default=False
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Source mount '/sources/{new_source}' is not mounted",
            )

    for field, value in update_data.items():
        setattr(job, field, value)

    job.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(job)
    logger.info(
        "job updated job_id=%s fields=%s",
        job.id,
        sorted(update_data.keys()),
    )

    # Sync the scheduler with the job's new state. PUT can change both the
    # schedule and the enabled flag, so the scheduler entry must be brought
    # in line unconditionally: add_job(replace_existing=True) registers a
    # previously-disabled job and replaces the trigger of a registered one;
    # a disabled job is removed so it stops firing. Rescheduling only when
    # already registered (the previous behavior) silently desynced the
    # scheduler from the DB whenever `enabled` was flipped through PUT.
    if job.enabled:
        _register_in_scheduler(job)
    else:
        _remove_from_scheduler(job_id)

    return await _build_job_response(job, session)


# ── DELETE /api/jobs/{id} ─────────────────────────────────────────────────────


@router.delete("/{job_id}", status_code=204)
@log_call
async def delete_job(
    job_id: str,
    delete_repository: bool = False,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a BackupJob (and its runs via CASCADE).

    Returns 409 if a backup run is currently in progress for this job.

    The restic repository on disk is kept by default: it holds the snapshots,
    and because it is addressed by name, a new job with the same name and
    destination adopts it — that is the recovery path. Pass
    `delete_repository=true` to destroy it and all its snapshots instead;
    that is the only way to free the name when the repository's password has
    been lost.

    The repository is removed *before* the row, so a refused or failed removal
    leaves job and repo consistent rather than orphaning one.
    """
    job = await _get_job_or_404(job_id, session)

    if uuid.UUID(job_id) in backup_runner.active_jobs:
        raise HTTPException(
            status_code=409,
            detail="A backup run is in progress for this job",
        )

    if delete_repository:
        repo_path = repository.build_repo_path(job.destination_label, job.name)
        try:
            removed = await repository.remove_repository(repo_path)
        except repository.RepositoryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        logger.info(
            "job delete removed repo repo=%s job_id=%s existed=%s",
            repo_path,
            job_id,
            removed,
        )

    _remove_from_scheduler(job_id)
    # Per design doc §7: drop the per-job lock so deleted jobs don't leak.
    backup_runner.job_locks.pop(uuid.UUID(job_id), None)
    await session.delete(job)
    await session.commit()
    logger.info(
        "job deleted job_id=%s name=%s repo_deleted=%s",
        job_id,
        job.name,
        delete_repository,
    )


# ── POST /api/jobs/{id}/run ───────────────────────────────────────────────────


@router.post("/{job_id}/run")
@log_call
async def trigger_run(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Manually trigger a backup run for the given job.

    Delegates to :func:`backup_runner.trigger_run` which holds the single
    per-job critical section shared by manual and scheduled triggers. If a
    run is already in flight (in-memory active_jobs or a `status=running` DB
    row), a skipped/overlapping_run audit row is recorded and this endpoint
    returns 409 so the UI can tell the user a run is already active.
    """
    await _get_job_or_404(job_id, session)
    job_uuid = uuid.UUID(job_id)

    run_id = await backup_runner.trigger_run(job_uuid, TriggeredBy.manual)
    # _get_job_or_404 already enforced existence; trigger_run only returns
    # None if the job vanished mid-request — surface that as 404 too.
    if run_id is None:
        raise HTTPException(status_code=404, detail="Not found")
    await _raise_409_if_overlapping(run_id, session)
    return {"run_id": run_id}


# ── POST /api/jobs/{id}/prune ─────────────────────────────────────────────────


@router.post("/{job_id}/prune")
@log_call
async def trigger_prune(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Manually trigger a `restic prune` run for the given job.

    Prune is decoupled from the backup pipeline (gaps.md H1): the backup
    pipeline now runs only `restic forget` (or skips even that when no
    retention is configured). Prune itself — the heavy pack-rewrite step —
    is invoked here on operator demand.

    Shares the per-job lock + active_jobs set with backup runs, so a prune
    triggered while a backup is in flight (or vice versa) is recorded as a
    skipped/overlapping_run audit row and rejected with 409 instead of
    racing against the same repo.
    """
    await _get_job_or_404(job_id, session)
    job_uuid = uuid.UUID(job_id)

    run_id = await backup_runner.trigger_prune(job_uuid, TriggeredBy.manual)
    # Same race-condition handling as /run: 404 only if the job vanished
    # between the existence check and the trigger.
    if run_id is None:
        raise HTTPException(status_code=404, detail="Not found")
    await _raise_409_if_overlapping(run_id, session)
    return {"run_id": run_id}


# ── POST /api/jobs/{id}/check ─────────────────────────────────────────────────


@router.post("/{job_id}/check")
@log_call
async def trigger_check(
    job_id: str,
    body: JobCheckRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Manually trigger a `restic check` integrity verification run."""
    await _get_job_or_404(job_id, session)
    job_uuid = uuid.UUID(job_id)

    run_id = await backup_runner.trigger_check(
        job_uuid,
        TriggeredBy.manual,
        check_mode=body.check_mode.value,
        subset_percent=body.check_subset_percent,
        timeout_hours=body.timeout_hours,
    )
    if run_id is None:
        raise HTTPException(status_code=404, detail="Not found")
    await _raise_409_if_overlapping(run_id, session)
    return {"run_id": run_id}


# ── POST /api/jobs/{id}/enable ────────────────────────────────────────────────


@router.post("/{job_id}/enable")
@log_call
async def enable_job(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    """Set enabled=True on the job and register it with the scheduler."""
    job = await _get_job_or_404(job_id, session)
    job.enabled = True
    await session.commit()
    _register_in_scheduler(job)
    logger.info("job enabled job_id=%s", job_id)
    return {"id": job.id, "enabled": True}


# ── POST /api/jobs/{id}/disable ───────────────────────────────────────────────


@router.post("/{job_id}/disable")
@log_call
async def disable_job(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    """Set enabled=False on the job and remove it from the scheduler."""
    job = await _get_job_or_404(job_id, session)
    job.enabled = False
    await session.commit()
    _remove_from_scheduler(job_id)
    logger.info("job disabled job_id=%s", job_id)
    return {"id": job.id, "enabled": False}


# ── POST /api/jobs/{id}/unlock ────────────────────────────────────────────────


@router.post("/{job_id}/unlock")
@log_call
async def unlock_job(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    """Run 'restic unlock' on the job's repository.

    Returns 409 if a backup run is currently active (the lock may be in use).
    """
    job = await _get_job_or_404(job_id, session)

    if uuid.UUID(job_id) in backup_runner.active_jobs:
        raise HTTPException(
            status_code=409,
            detail="A backup run is in progress for this job",
        )

    settings = await session.get(AppSettings, 1)
    timeout = settings.metadata_timeout_seconds if settings else 600
    repo_path = repository.build_repo_path(job.destination_label, job.name)
    _rc, stdout, stderr = await restic.restic_unlock(
        repo_path=repo_path, password=job.restic_password, timeout_seconds=timeout
    )
    logger.info("repository unlocked job_id=%s", job_id)

    return {"output": stdout or stderr}


# ── GET /api/jobs/{id}/runs ───────────────────────────────────────────────────


@router.get("/{job_id}/runs", response_model=List[RunSummarySchema])
@log_call
async def list_job_runs(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> Sequence[BackupRun]:
    """Return all runs for a job ordered by started_at descending (newest first).

    Output fields (backup_output, error_output, …) are excluded; use
    GET /api/runs/{id} to fetch a run with full output.
    """
    await _get_job_or_404(job_id, session)
    result = await session.execute(
        select(BackupRun)
        .where(BackupRun.job_id == job_id)
        .order_by(BackupRun.started_at.desc())
    )
    return result.scalars().all()


# ── GET /api/jobs/{id}/commands ───────────────────────────────────────────────


@router.get("/{job_id}/commands", response_model=List[JobCommandResponse])
@log_call
async def list_job_commands(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> List[dict[str, object]]:
    """Return the restic commands a backup run of this job will issue.

    Derived from the stored job on every request — never cached, never stored
    — so an edit to the job is reflected immediately and the page can't show
    a command line the runner has stopped using. The repository password is
    replaced by a label; it is no more exposed here than anywhere else.
    """
    job = await _get_job_or_404(job_id, session)
    settings = await session.get(AppSettings, 1)
    # Auto-unlock decides whether `restic unlock` is issued on every run or
    # only as a stale-lock retry, so the preview needs the same default the
    # pipeline uses when no settings row exists yet.
    auto_unlock = settings.auto_unlock if settings else True
    return job_commands.build_job_commands(job, auto_unlock=auto_unlock)


# ── GET /api/jobs/{id}/snapshots ──────────────────────────────────────────────


@router.get("/{job_id}/snapshots", response_model=List[SnapshotResponse])
@log_call
async def list_job_snapshots(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> List[SnapshotResponse]:
    """Query restic live for this job's snapshots, newest first.

    Restic is the source of truth (gaps.md C4-Alt). The response is built by
    calling `restic snapshots --json --no-lock` — deliberately unfiltered: the
    repository at /destinations/<label>/<name> belongs to exactly one job, so
    the repo *is* the scope. Filtering by a per-job tag would hide every
    snapshot taken before the current job row existed (e.g. after a DB-loss
    recovery). Results are cached briefly per repo path to absorb dashboard
    refresh storms.
    """
    job = await _get_job_or_404(job_id, session)
    settings = await session.get(AppSettings, 1)
    timeout = settings.metadata_timeout_seconds if settings else 600
    repo_path = repository.build_repo_path(job.destination_label, job.name)
    try:
        raw = await snapshot_listing.list_snapshots(
            repo_path, job.restic_password, timeout_seconds=timeout
        )
    except snapshot_listing.SnapshotListingError as exc:
        # Every failure is an error, never an empty list. The repository is
        # created with the job, so "unable to open" means the drive is detached
        # or the directory moved — not that the job has no backups. Reporting
        # that as `200 []` told the user their snapshots were gone and invited
        # them to delete and recreate the job. An empty repository is a
        # different thing entirely: restic exits 0 with `[]` and never lands
        # here.
        logger.error(
            "snapshot listing failed job_id=%s repo=%s error=%s", job_id, repo_path, exc
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not list snapshots for the repository at '{repo_path}'. "
                f"The destination is probably not mounted — the snapshots "
                f"themselves are unaffected.\n\n{exc}"
            ),
        )
    # Restic returns snapshots oldest-first; flip so the UI shows newest-first.
    raw_sorted = sorted(raw, key=lambda s: s["snapshot_time"], reverse=True)
    return [SnapshotResponse.model_validate(s) for s in raw_sorted]
