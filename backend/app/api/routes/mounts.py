"""FastAPI routes for mount discovery and destination rename.

SOURCES_ROOT and DESTINATIONS_ROOT are module-level constants so that tests
can patch them via 'app.api.routes.mounts.SOURCES_ROOT', etc.
"""

import os
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas.mounts import RenameDestinationRequest, RenameDestinationResult
from app.core import fs
from app.core.logging import get_logger, log_call
from app.db.models import BackupJob
from app.services import backup_runner

logger = get_logger(__name__)

router = APIRouter(prefix="/mounts", tags=["mounts"])

# Default root paths for mounted sources and destinations.
SOURCES_ROOT = "/sources"
DESTINATIONS_ROOT = "/destinations"


@log_call
def _list_dirs(root: str) -> List[str]:
    """Return the names of all immediate subdirectories under root.

    Non-directory entries are silently filtered out.  Returns an empty list
    if the root directory does not exist.
    """
    try:
        with os.scandir(root) as it:
            return [entry.name for entry in it if entry.is_dir()]
    except FileNotFoundError:
        return []


# ── GET /api/mounts/sources ───────────────────────────────────────────────────


@router.get("/sources", response_model=List[str])
@log_call
async def list_sources() -> List[str]:
    """Return directory names found directly under SOURCES_ROOT.

    The scandir runs through fs.run_probe so a hung network mount can't
    freeze the event loop; a probe timeout yields an empty list.
    """
    return await fs.run_probe(_list_dirs, SOURCES_ROOT, default=[])


# ── GET /api/mounts/destinations ─────────────────────────────────────────────


@router.get("/destinations", response_model=List[str])
@log_call
async def list_destinations() -> List[str]:
    """Return directory names found directly under DESTINATIONS_ROOT.

    Probed like list_sources — a hung mount yields an empty list rather
    than a frozen event loop.
    """
    return await fs.run_probe(_list_dirs, DESTINATIONS_ROOT, default=[])


# ── POST /api/mounts/destinations/rename ─────────────────────────────────────


@router.post("/destinations/rename", response_model=RenameDestinationResult)
@log_call
async def rename_destination(
    body: RenameDestinationRequest,
    session: AsyncSession = Depends(get_session),
) -> RenameDestinationResult:
    """Rename a destination label in all BackupJob rows.

    The new destination directory must already be mounted.  No jobs using the
    old label may have an active run.  The old directory itself is not renamed
    on disk — only the DB references are updated.

    Returns the list of affected jobs.
    """
    # Validate that the new label is already mounted.
    new_path = os.path.join(DESTINATIONS_ROOT, body.new_label)
    if not await fs.run_probe(os.path.isdir, new_path, default=False):
        raise HTTPException(
            status_code=422,
            detail=f"New destination '{body.new_label}' is not mounted",
        )

    # Find all jobs that reference the old label.
    result = await session.execute(
        select(BackupJob).where(BackupJob.destination_label == body.old_label)
    )
    jobs = result.scalars().all()
    if not jobs:
        raise HTTPException(
            status_code=404,
            detail=f"No jobs found with destination_label='{body.old_label}'",
        )

    # Reject the rename if any of those jobs are currently running.
    active_job_ids = {uuid.UUID(j.id) for j in jobs}
    if active_job_ids & backup_runner.active_jobs:
        raise HTTPException(
            status_code=409,
            detail="A backup run is in progress for one or more affected jobs",
        )

    # Update all matching jobs.
    for job in jobs:
        job.destination_label = body.new_label
    await session.commit()
    logger.info(
        "destination renamed old=%s new=%s affected_jobs=%d",
        body.old_label,
        body.new_label,
        len(jobs),
    )

    return RenameDestinationResult(
        affected_jobs=[{"id": j.id, "name": j.name} for j in jobs]
    )
