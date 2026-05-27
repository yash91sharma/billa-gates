"""FastAPI routes for BackupRun history and detail."""

import asyncio
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas.jobs import RunSummarySchema
from app.api.schemas.runs import RunDetailSchema
from app.core.logging import log_call
from app.db.models import BackupJob, BackupRun, RunStatus
from app.services import process_registry

router = APIRouter(prefix="/runs", tags=["runs"])


# ── GET /api/runs/recent ──────────────────────────────────────────────────────
# Must be defined BEFORE /{id} to prevent FastAPI from treating "recent" as an
# id path parameter.


@router.get("/recent", response_model=List[RunSummarySchema])
@log_call
async def recent_runs(
    limit: int = Query(10, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> List[RunSummarySchema]:
    """Return the most recent runs across all jobs, newest first.

    Each entry includes job_name (joined from BackupJob).
    Output fields (backup_output, error_output, …) are excluded.
    """
    result = await session.execute(
        select(BackupRun, BackupJob.name.label("job_name"))
        .join(BackupJob, BackupRun.job_id == BackupJob.id)
        .order_by(BackupRun.started_at.desc())
        .limit(limit)
    )
    rows = result.all()

    response: list[RunSummarySchema] = []
    for run, job_name in rows:
        data = RunSummarySchema.model_validate(run)
        data.job_name = job_name
        response.append(data)

    return response


# ── GET /api/runs/{id} ────────────────────────────────────────────────────────


@router.get("/{run_id}", response_model=RunDetailSchema)
@log_call
async def get_run(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> BackupRun:
    """Return a single run record with all output fields included."""
    run = await session.get(BackupRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Not found")
    return run


# ── POST /api/runs/{id}/cancel ────────────────────────────────────────────────


@router.post("/{run_id}/cancel", status_code=202)
@log_call
async def cancel_run(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    """Mark a running backup run for cancellation and SIGTERM its restic
    subprocess.

    Returns 404 when the run doesn't exist, 409 when the run has already
    finished (terminal status), and 202 when the cancel flag has been set
    and termination dispatched. The pipeline observes the flag between
    restic steps, finalizes the row to ``status=canceled,
    reason=user_canceled``, and stops further work.
    """
    run = await session.get(BackupRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Not found")
    if run.status != RunStatus.running:
        raise HTTPException(
            status_code=409,
            detail=f"Run is not running (status={run.status.value}); cannot cancel.",
        )

    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        # If the row exists but its id isn't a valid UUID (shouldn't happen —
        # we always create with UUIDs), treat as not-found rather than 500.
        raise HTTPException(status_code=404, detail="Not found")

    process_registry.mark_canceled(run_uuid)
    # Fire-and-forget the SIGTERM so the API returns immediately. The grace
    # window inside _terminate_then_kill is up to 10s; blocking the request
    # on that would make the UI feel sluggish.
    asyncio.create_task(process_registry.terminate(run_uuid))
    return Response(status_code=202)
