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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas.jobs import (
    JobCheckRequest,
    JobCreate,
    JobResponse,
    JobUpdate,
    RunSummarySchema,
    SnapshotResponse,
)
from app.core import fs
from app.core import scheduler as scheduler_module
from app.core.logging import get_logger, log_call
from app.db.models import (
    AppSettings,
    BackupJob,
    BackupRun,
    RunStatus,
    TriggeredBy,
)
from app.services import backup_runner, restic, snapshot_listing

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
async def _has_successful_run(job_id: str, session: AsyncSession) -> bool:
    """Return True if the job has at least one run with status=success."""
    result = await session.execute(
        select(BackupRun)
        .where(BackupRun.job_id == job_id, BackupRun.status == RunStatus.success)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


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
        "has_successful_run": await _has_successful_run(job.id, session),
        "next_run_time": _next_run_time(job.id),
        "last_run": await _last_run(job.id, session),
    }


@log_call
async def _validate_mounts(source_label: str, destination_label: str) -> None:
    """Raise HTTP 422 if either mount directory does not exist.

    isdir runs through fs.run_probe: on a hung network mount the stat()
    would otherwise block the event loop and freeze the whole app. A probe
    timeout is reported as "not mounted".
    """
    if not await fs.run_probe(
        os.path.isdir, f"{_SOURCES_ROOT}/{source_label}", default=False
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Source mount '/sources/{source_label}' is not mounted",
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
    """Create a new BackupJob.

    Validates that both source and destination mounts exist, that no job with
    the same (source_label, destination_label) pair already exists, and that
    the schedule/check configuration is internally consistent (done by the
    JobCreate schema).
    """
    await _validate_mounts(body.source_label, body.destination_label)
    await _check_duplicate(
        body.source_label, body.source_subpath, body.destination_label, session
    )

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
    """Update a BackupJob.

    Enforces immutability rules:
    - destination_label cannot be changed after creation.
    - restic_password cannot be changed after the job has a successful run.
    """
    job = await _get_job_or_404(job_id, session)

    # Destination label is permanently immutable.
    if body.destination_label != job.destination_label:
        raise HTTPException(
            status_code=422,
            detail="destination_label cannot be changed after job creation",
        )

    # Password is immutable once the restic repo has a successful backup.
    if body.restic_password is not None and await _has_successful_run(job_id, session):
        raise HTTPException(
            status_code=422,
            detail="restic_password cannot be changed after a successful backup run",
        )

    # Uniqueness check comes before mount validation so a conflict returns 409
    # even when the mount is not present (avoids a misleading 422).
    await _check_duplicate(
        body.source_label,
        body.source_subpath,
        body.destination_label,
        session,
        exclude_id=job_id,
    )

    # Re-validate source mount only when the label actually changes.
    if body.source_label != job.source_label:
        if not await fs.run_probe(
            os.path.isdir, f"{_SOURCES_ROOT}/{body.source_label}", default=False
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Source mount '/sources/{body.source_label}' is not mounted",
            )

    # Apply all provided fields.
    update_data = body.model_dump(exclude_none=True)
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
async def delete_job(job_id: str, session: AsyncSession = Depends(get_session)) -> None:
    """Delete a BackupJob (and its runs/snapshots via CASCADE).

    Returns 409 if a backup run is currently in progress for this job.
    The restic repository on disk is NOT deleted.
    """
    job = await _get_job_or_404(job_id, session)

    if uuid.UUID(job_id) in backup_runner.active_jobs:
        raise HTTPException(
            status_code=409,
            detail="A backup run is in progress for this job",
        )

    _remove_from_scheduler(job_id)
    # Per design doc §7: drop the per-job lock so deleted jobs don't leak.
    backup_runner.job_locks.pop(uuid.UUID(job_id), None)
    await session.delete(job)
    await session.commit()
    logger.info("job deleted job_id=%s name=%s", job_id, job.name)


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
    row), a skipped/overlapping_run row is created instead of firing a
    duplicate backup.
    """
    await _get_job_or_404(job_id, session)
    job_uuid = uuid.UUID(job_id)

    run_id = await backup_runner.trigger_run(job_uuid, TriggeredBy.manual)
    # _get_job_or_404 already enforced existence; trigger_run only returns
    # None if the job vanished mid-request — surface that as 404 too.
    if run_id is None:
        raise HTTPException(status_code=404, detail="Not found")
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
    triggered while a backup is in flight (or vice versa) is recorded as
    a skipped/overlapping_run row instead of racing against the same repo.
    """
    await _get_job_or_404(job_id, session)
    job_uuid = uuid.UUID(job_id)

    run_id = await backup_runner.trigger_prune(job_uuid, TriggeredBy.manual)
    # Same race-condition handling as /run: 404 only if the job vanished
    # between the existence check and the trigger.
    if run_id is None:
        raise HTTPException(status_code=404, detail="Not found")
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
    repo_path = f"{_DESTINATIONS_ROOT}/{job.destination_label}/{job.id}"
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


# ── GET /api/jobs/{id}/snapshots ──────────────────────────────────────────────


@router.get("/{job_id}/snapshots", response_model=List[SnapshotResponse])
@log_call
async def list_job_snapshots(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> List[SnapshotResponse]:
    """Query restic live for this job's snapshots, newest first.

    Restic is the source of truth (gaps.md C4-Alt); the response is built
    by calling `restic snapshots --tag job:<id> --no-lock --json` and is
    cached briefly per repo path to absorb dashboard refresh storms.
    """
    job = await _get_job_or_404(job_id, session)
    settings = await session.get(AppSettings, 1)
    timeout = settings.metadata_timeout_seconds if settings else 600
    repo_path = snapshot_listing.build_repo_path(job.destination_label, job.id)
    try:
        raw = await snapshot_listing.list_snapshots(
            repo_path, job.restic_password, job_id=job.id, timeout_seconds=timeout
        )
    except snapshot_listing.SnapshotListingError as exc:
        # If the repo doesn't exist yet (genuine first run before any backup),
        # the UI should see "no snapshots" rather than a 503 — distinguish via
        # stderr containing "does not exist" / "no such file". Anything else
        # is a real failure the operator needs to see.
        msg = str(exc).lower()
        if "does not exist" in msg or "no such file" in msg or "unable to open" in msg:
            return []
        raise HTTPException(status_code=503, detail=f"snapshot listing failed: {exc}")
    # Restic returns snapshots oldest-first; flip so the UI shows newest-first.
    raw_sorted = sorted(raw, key=lambda s: s["snapshot_time"], reverse=True)
    return [SnapshotResponse.model_validate(s) for s in raw_sorted]
